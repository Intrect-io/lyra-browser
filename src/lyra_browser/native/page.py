"""``NativePage``: one tab, driven over its own flat DevTools session.

The slice of Playwright's ``Page`` that lyra uses (``docs`` of the native driver list
it), built from CDP:

- navigation (``goto``/``go_back``/``reload``) from ``Page.navigate`` and the history
  commands, waited on through ``Page.lifecycleEvent`` for the loader the navigation
  created, and answered with the document's ``Network.responseReceived``;
- scripts through ``evaluate`` (see ``evaluate.py``: isolated world by default, never
  ``Runtime.enable``);
- dialogs, downloads, screenshots, and the tab itself (front, close, opener).

Errors are worded as Playwright words them, because the tools read the words
(``errors.py``).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .dialog import Dialog
from .errors import Error, ProtocolError, TargetClosedError, timeout_error, with_call_log
from .evaluate import (
    CONTEXT_DESTROYED,
    MAIN,
    NO_ARG,
    UTILITY_WORLD,
    WorldCache,
    call_argument,
    decode_result,
    is_function,
    is_stale_context,
)

if TYPE_CHECKING:
    from .context import NativeContext

INIT_TIMEOUT_S = 10.0
DEFAULT_TIMEOUT_MS = 30_000.0
# Page.lifecycleEvent names for each ``wait_until``. "commit" is the frame navigating.
_LIFECYCLE = {"domcontentloaded": "DOMContentLoaded", "load": "load", "networkidle": "networkIdle"}
_WAIT_UNTIL = frozenset({"commit", *_LIFECYCLE})


class Response:
    """The document response of a navigation: what ``navigation.py`` reads."""

    def __init__(self, params: dict) -> None:
        response = params.get("response") or {}
        self.url: str = response.get("url", "")
        self.status: int = int(response.get("status") or 0)
        self.status_text: str = response.get("statusText", "")
        self.headers: dict[str, str] = {
            str(k).lower(): str(v) for k, v in (response.get("headers") or {}).items()
        }

    @property
    def ok(self) -> bool:
        return self.status == 0 or 200 <= self.status < 300


class _Outcome:
    """What a history navigation (back, reload) turned into, as the frame reports it."""

    def __init__(self) -> None:
        self.future: asyncio.Future[tuple[str, str | None]] = (
            asyncio.get_running_loop().create_future()
        )

    def settle(self, kind: str, value: str | None = None) -> None:
        if not self.future.done():
            self.future.set_result((kind, value))


class _Unimplemented:
    """``keyboard``/``mouse`` until the input phase lands: every call says so."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, item: str):
        raise Error(f"{self._name}.{item} is not implemented by the native driver yet")


class NativePage:
    def __init__(
        self, context: NativeContext, target_id: str, session: str, opener_id: str | None, url: str
    ) -> None:
        self._context = context
        self.target_id = target_id
        self._session = session
        self._opener_id = opener_id
        self._url = url
        self._closed = False
        self._handlers: dict[str, list[Callable]] = {}
        self._worlds = WorldCache()
        self._frames: set[str] = {target_id}
        self._loader: str | None = None
        # Main-frame commits, numbered, so a navigation can tell its own commit from
        # one that replaced it.
        self._commit_seq = 0
        self._commits: list[tuple[int, str]] = []
        self._lifecycle: dict[str, set[str]] = {}
        self._responses: dict[str, Response] = {}
        self._waiters: list[Callable[[], None]] = []
        self._outcome: _Outcome | None = None
        self._failed_loads: dict[str, str] = {}
        self.keyboard = _Unimplemented("Keyboard")
        self.mouse = _Unimplemented("Mouse")

    # -- plumbing -------------------------------------------------------------------

    @property
    def context(self) -> NativeContext:
        return self._context

    @property
    def url(self) -> str:
        return self._url

    @property
    def main_frame_id(self) -> str:
        return self.target_id

    def owns_frame(self, frame_id: str) -> bool:
        return frame_id in self._frames

    async def _send(
        self, method: str, params: dict | None = None, *, timeout: float | None = None
    ) -> dict:
        if self._closed:
            raise TargetClosedError()
        return await self._context._conn.send(
            method, params, session=self._session, timeout=timeout
        )

    def _on(self, method: str, handler: Callable[[dict], None]) -> None:
        self._context._conn.on(method, lambda params, _s: handler(params), session=self._session)

    def _emit(self, event: str, *args: Any) -> None:
        for handler in list(self._handlers.get(event, ())):
            try:
                handler(*args)
            except Exception:  # noqa: BLE001 — a listener's failure is its own
                pass

    def _notify(self) -> None:
        for waiter in list(self._waiters):
            waiter()

    async def _initialize(
        self, *, headless_viewport: dict | None, screen: dict | None, block_service_workers: bool
    ) -> None:
        """Subscribe and enable what this page needs, in one round of commands."""
        self._on("Page.frameNavigated", self._on_frame_navigated)
        self._on("Page.navigatedWithinDocument", self._on_within_document)
        self._on("Page.frameAttached", lambda p: self._frames.add(p.get("frameId", "")))
        self._on("Page.frameDetached", self._on_frame_detached)
        self._on("Page.lifecycleEvent", self._on_lifecycle)
        self._on("Page.frameStoppedLoading", self._on_stopped_loading)
        self._on("Page.javascriptDialogOpening", self._on_dialog)
        self._on("Network.responseReceived", self._on_response)
        self._on("Network.loadingFailed", self._on_loading_failed)
        commands = [
            self._send("Page.enable", timeout=INIT_TIMEOUT_S),
            self._send("Page.setLifecycleEventsEnabled", {"enabled": True}, timeout=INIT_TIMEOUT_S),
            self._send("Network.enable", timeout=INIT_TIMEOUT_S),
        ]
        if block_service_workers:
            commands.append(
                self._send(
                    "Network.setBypassServiceWorker", {"bypass": True}, timeout=INIT_TIMEOUT_S
                )
            )
        if headless_viewport:
            metrics = {
                "width": headless_viewport["width"],
                "height": headless_viewport["height"],
                "deviceScaleFactor": 1,
                "mobile": False,
            }
            if screen:
                metrics["screenWidth"] = screen["width"]
                metrics["screenHeight"] = screen["height"]
            commands.append(
                self._send("Emulation.setDeviceMetricsOverride", metrics, timeout=INIT_TIMEOUT_S)
            )
        await asyncio.gather(*commands)
        tree = await self._send("Page.getFrameTree", timeout=INIT_TIMEOUT_S)
        self._absorb_tree(tree.get("frameTree") or {})

    def _absorb_tree(self, node: dict) -> None:
        frame = node.get("frame") or {}
        if frame.get("id"):
            self._frames.add(frame["id"])
            if not frame.get("parentId"):
                self._url = frame.get("url", self._url) + frame.get("urlFragment", "")
                self._loader = frame.get("loaderId", self._loader)
        for child in node.get("childFrames") or ():
            self._absorb_tree(child)

    # -- events -----------------------------------------------------------------------

    def _on_frame_navigated(self, params: dict) -> None:
        frame = params.get("frame") or {}
        frame_id = frame.get("id", "")
        self._frames.add(frame_id)
        self._worlds.forget(frame_id)
        if frame.get("parentId"):
            return
        self._url = frame.get("url", "") + frame.get("urlFragment", "")
        self._loader = frame.get("loaderId")
        self._commit_seq += 1
        self._commits.append((self._commit_seq, self._loader or ""))
        del self._commits[:-32]
        if self._outcome is not None and self._loader:
            self._outcome.settle("navigated", self._loader)
        self._notify()

    def _on_within_document(self, params: dict) -> None:
        if params.get("frameId") == self.target_id:
            self._url = params.get("url", self._url)
            if self._outcome is not None:
                self._outcome.settle("same_document")
            self._notify()

    def _on_frame_detached(self, params: dict) -> None:
        frame_id = params.get("frameId", "")
        self._worlds.forget(frame_id)
        if frame_id != self.target_id:
            self._frames.discard(frame_id)

    def _on_lifecycle(self, params: dict) -> None:
        if params.get("frameId") != self.target_id:
            return
        loader = params.get("loaderId", "")
        name = params.get("name", "")
        if name == "init":
            self._lifecycle[loader] = set()
        else:
            self._lifecycle.setdefault(loader, set()).add(name)
        self._notify()

    def _on_stopped_loading(self, params: dict) -> None:
        if params.get("frameId") == self.target_id and self._outcome is not None:
            self._outcome.settle("stopped")

    def _on_response(self, params: dict) -> None:
        if params.get("type") == "Document" and params.get("frameId") == self.target_id:
            loader = params.get("loaderId") or params.get("requestId", "")
            self._responses[loader] = Response(params)
            # Only the latest few matter: a goto reads its own right after it lands.
            while len(self._responses) > 16:
                self._responses.pop(next(iter(self._responses)))

    def _on_loading_failed(self, params: dict) -> None:
        if params.get("type") == "Document":
            self._failed_loads[params.get("requestId", "")] = params.get("errorText", "")
            while len(self._failed_loads) > 16:
                self._failed_loads.pop(next(iter(self._failed_loads)))

    def _on_dialog(self, params: dict) -> None:
        self._context._dialog_opened(Dialog(self, params))

    def _gone(self) -> None:
        """The target detached or the browser went away."""
        if self._closed:
            return
        self._closed = True
        if self._outcome is not None:
            self._outcome.settle("closed")
        self._notify()
        self._emit("close", self)

    # -- events for callers -----------------------------------------------------------

    def on(self, event: str, handler: Callable) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable) -> None:
        with contextlib.suppress(KeyError, ValueError):
            self._handlers[event].remove(handler)

    # -- navigation -------------------------------------------------------------------

    async def _wait(
        self, ready: Callable[[], bool], deadline: float, api: str, log: list[str]
    ) -> None:
        """Until ``ready()`` holds, the page closes (raise) or ``deadline`` passes (raise)."""
        loop = asyncio.get_running_loop()
        while not ready():
            if self._closed:
                raise TargetClosedError(
                    with_call_log(api, "Target page, context or browser has been closed", log)
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise timeout_error(api, self._timeout_ms, log)
            event = asyncio.Event()
            self._waiters.append(event.set)
            try:
                await asyncio.wait_for(event.wait(), remaining)
            except TimeoutError:
                pass
            finally:
                self._waiters.remove(event.set)

    def _first_commit_since(self, since: int) -> str | None:
        return next((loader for seq, loader in self._commits if seq > since), None)

    def _interrupted(self, api: str, url: str, log: list[str]) -> Error:
        return Error(
            with_call_log(
                api,
                f'Navigation to "{url}" is interrupted by another navigation to "{self._url}"',
                log,
            )
        )

    async def _settle_loader(
        self,
        loader: str,
        since: int,
        wait_until: str,
        deadline: float,
        api: str,
        log: list[str],
        url: str,
    ) -> Response | None:
        """Wait for ``loader`` to commit and reach ``wait_until``; its document response.

        The first main-frame commit after ``since`` decides: ours, or another navigation
        that got there first (Playwright's "interrupted by another navigation").
        """
        await self._wait(lambda: self._first_commit_since(since) is not None, deadline, api, log)
        if self._first_commit_since(since) != loader:
            raise self._interrupted(api, url, log)
        name = _LIFECYCLE.get(wait_until)
        if name is not None:
            await self._wait(
                lambda: name in self._lifecycle.get(loader, ()) or self._loader != loader,
                deadline,
                api,
                log,
            )
            if self._loader != loader:
                raise self._interrupted(api, url, log)
        return self._responses.get(loader)

    def _deadline(self, timeout: float | None) -> float:
        self._timeout_ms = DEFAULT_TIMEOUT_MS if timeout is None else float(timeout)
        if self._timeout_ms <= 0:
            return float("inf")
        return asyncio.get_running_loop().time() + self._timeout_ms / 1000

    @staticmethod
    def _check_wait_until(api: str, wait_until: str) -> None:
        if wait_until not in _WAIT_UNTIL:
            raise Error(
                f"{api}: waitUntil: expected one of (load|domcontentloaded|networkidle|commit)"
            )

    async def goto(
        self,
        url: str,
        *,
        wait_until: str = "load",
        timeout: float | None = None,
        referer: str | None = None,
    ) -> Response | None:
        api = "Page.goto"
        self._check_wait_until(api, wait_until)
        deadline = self._deadline(timeout)
        log = [f'navigating to "{url}", waiting until "{wait_until}"']
        downloads_before = self._context._download_count(self)
        since = self._commit_seq
        params: dict = {"url": url, "frameId": self.target_id}
        if referer:
            params["referrer"] = referer
        result = await self._send("Page.navigate", params)
        error_text = result.get("errorText")
        if error_text:
            if await self._context._download_started(self, downloads_before):
                raise Error(with_call_log(api, "Download is starting", log))
            raise Error(with_call_log(api, f"{error_text} at {url}", log))
        loader = result.get("loaderId")
        if not loader:
            return None  # same-document: nothing new loads, Playwright answers null
        return await self._settle_loader(loader, since, wait_until, deadline, api, log, url)

    async def _history(
        self, api: str, command: Callable[[], Any], wait_until: str, timeout: float | None
    ) -> Response | None:
        self._check_wait_until(api, wait_until)
        deadline = self._deadline(timeout)
        log = [f'waiting for navigation until "{wait_until}"']
        failed_before = set(self._failed_loads)
        since = self._commit_seq
        self._outcome = outcome = _Outcome()
        try:
            await command()
            loop = asyncio.get_running_loop()
            try:
                kind, loader = await asyncio.wait_for(
                    asyncio.shield(outcome.future), max(0.0, deadline - loop.time())
                )
            except TimeoutError:
                raise timeout_error(api, self._timeout_ms, log) from None
        finally:
            self._outcome = None
        if kind == "closed":
            raise TargetClosedError(
                with_call_log(api, "Target page, context or browser has been closed", log)
            )
        if kind == "same_document":
            return None
        if kind == "stopped":
            new_failures = [
                self._failed_loads[k] for k in self._failed_loads if k not in failed_before
            ]
            reason = new_failures[-1] if new_failures else "net::ERR_ABORTED"
            raise Error(with_call_log(api, f"{reason}; maybe frame was detached?", log))
        assert loader is not None
        return await self._settle_loader(loader, since, wait_until, deadline, api, log, self._url)

    async def go_back(
        self, *, wait_until: str = "load", timeout: float | None = None
    ) -> Response | None:
        history = await self._send("Page.getNavigationHistory")
        index = history.get("currentIndex", 0)
        entries = history.get("entries") or []
        if index <= 0 or index > len(entries) - 1:
            return None
        entry_id = entries[index - 1]["id"]
        return await self._history(
            "Page.goBack",
            lambda: self._send("Page.navigateToHistoryEntry", {"entryId": entry_id}),
            wait_until,
            timeout,
        )

    async def reload(
        self, *, wait_until: str = "load", timeout: float | None = None
    ) -> Response | None:
        return await self._history(
            "Page.reload", lambda: self._send("Page.reload"), wait_until, timeout
        )

    async def wait_for_timeout(self, timeout: float) -> None:
        await asyncio.sleep(max(0.0, timeout) / 1000)

    # -- scripts ----------------------------------------------------------------------

    async def _context_id(self, frame_id: str) -> int:
        cached = self._worlds.get(frame_id)
        if cached is not None:
            return cached
        reply = await self._send(
            "Page.createIsolatedWorld", {"frameId": frame_id, "worldName": UTILITY_WORLD}
        )
        context_id = int(reply["executionContextId"])
        self._worlds.put(frame_id, context_id)
        return context_id

    async def _main_world_global(self) -> str:
        reply = await self._send("Runtime.evaluate", {"expression": "globalThis"})
        object_id = (reply.get("result") or {}).get("objectId")
        if not object_id:
            raise Error("could not reach the page's main world")
        return object_id

    async def _run(
        self,
        api: str,
        source: str,
        arg: Any,
        world: str | None,
        frame_id: str,
        this_object: str | None = None,
    ) -> Any:
        """One evaluation, retried once if the frame's world went with a navigation."""
        for attempt in (0, 1):
            try:
                return await self._run_once(api, source, arg, world, frame_id, this_object)
            except ProtocolError as exc:
                if not is_stale_context(exc):
                    raise
                self._worlds.forget(frame_id)
                if attempt or this_object is not None:
                    raise Error(f"{api}: {CONTEXT_DESTROYED}") from None
        raise AssertionError("unreachable")

    async def _run_once(
        self,
        api: str,
        source: str,
        arg: Any,
        world: str | None,
        frame_id: str,
        this_object: str | None,
    ) -> Any:
        common = {"returnByValue": True, "awaitPromise": True, "userGesture": True}
        if is_function(source) or this_object is not None:
            arguments = [] if arg is NO_ARG else [call_argument(arg)]
            params: dict = {"functionDeclaration": source, "arguments": arguments, **common}
            if this_object is not None:
                params["objectId"] = this_object
            elif world == MAIN:
                params["objectId"] = await self._main_world_global()
            else:
                params["executionContextId"] = await self._context_id(frame_id)
            return decode_result(api, await self._send("Runtime.callFunctionOn", params))
        params = {"expression": source, **common}
        if world != MAIN:
            params["contextId"] = await self._context_id(frame_id)
        return decode_result(api, await self._send("Runtime.evaluate", params))

    async def evaluate(
        self, expression: str, arg: Any = NO_ARG, *, world: str | None = None
    ) -> Any:
        """Playwright's ``page.evaluate``; ``world="main"`` reaches the page's own globals."""
        return await self._run("Page.evaluate", expression, arg, world, self.target_id)

    async def title(self) -> str:
        return str(await self.evaluate("document.title") or "")

    # -- capture ----------------------------------------------------------------------

    async def screenshot(
        self, *, full_page: bool = False, path: str | None = None, **_ignored: Any
    ) -> bytes:
        params: dict = {"format": "png", "fromSurface": True}
        if full_page:
            metrics = await self._send("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            params["captureBeyondViewport"] = True
            params["clip"] = {
                "x": 0,
                "y": 0,
                "width": size.get("width", 0),
                "height": size.get("height", 0),
                "scale": 1,
            }
        try:
            reply = await self._send("Page.captureScreenshot", params)
        except ProtocolError as exc:
            raise Error(f"Page.screenshot: {exc}") from None
        png = base64.b64decode(reply.get("data", ""))
        if path:
            await asyncio.to_thread(_write, path, png)
        return png

    # -- the tab ----------------------------------------------------------------------

    def is_closed(self) -> bool:
        return self._closed

    async def opener(self) -> NativePage | None:
        if not self._opener_id:
            return None
        page = self._context._page_by_target(self._opener_id)
        return page if page is not None and not page.is_closed() else None

    async def bring_to_front(self) -> None:
        await self._send("Page.bringToFront")

    async def close(self, *, run_before_unload: bool = False, **_ignored: Any) -> None:
        if self._closed:
            return
        try:
            await self._context._browser_send("Target.closeTarget", {"targetId": self.target_id})
        except (ProtocolError, TargetClosedError):
            pass
        with contextlib.suppress(Exception):
            await self._wait(
                lambda: self._closed, asyncio.get_running_loop().time() + 5, "Page.close", []
            )

    def __repr__(self) -> str:  # never the endpoint, only what the user can see
        return f"<NativePage url={self._url!r}>"

    _timeout_ms: float = DEFAULT_TIMEOUT_MS


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)
