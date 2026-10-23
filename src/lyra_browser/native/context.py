"""``NativeContext``: the browser and its tabs, as Playwright's persistent ``BrowserContext``.

One process, one DevTools connection (``connection.py``), one ``NativePage`` per tab.
Tabs are found by ``Target.setAutoAttach`` at browser level, *without*
``waitForDebuggerOnStart``: ``cdp_guard`` holds every new tab until its document filter
is in place, and a second client releasing the tab first would let that first request
escape (measured in the phase-0 spike: no document escaped with this arrangement).

Downloads are allowed into a private directory and reported per tab (``download.py``);
dialogs go to the ``dialog`` listeners, and a dialog nobody listens for is dismissed,
as Playwright dismisses it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .connection import Connection
from .dialog import Dialog
from .download import Download
from .errors import Error, ProtocolError, TargetClosedError
from .launcher import BrowserProcess, launch
from .page import NativePage

_PAGE_ATTACH = {
    "autoAttach": True,
    "waitForDebuggerOnStart": False,
    "flatten": True,
    "filter": [{"type": "page"}, {"exclude": True}],
}
READY_TIMEOUT_S = 15.0
CLOSE_TIMEOUT_S = 5.0
# How long a navigation that ended in net::ERR_ABORTED waits for Chrome to report the
# download it became, before it is read as an ordinary abort.
DOWNLOAD_GRACE_S = 0.5


class NativeCDPSession:
    """``context.new_cdp_session(page)``: raw commands on the page's own session."""

    def __init__(self, page: NativePage) -> None:
        self._page = page

    async def send(self, method: str, params: dict | None = None) -> dict:
        return await self._page._send(method, params)

    async def detach(self) -> None:
        """Shared with the page, so there is nothing to detach."""


class NativeContext:
    def __init__(self, process: BrowserProcess, conn: Connection, options: dict) -> None:
        self._process = process
        self._conn = conn
        self._options = options
        self._pages: dict[str, NativePage] = {}
        self._ready: dict[str, asyncio.Future[NativePage]] = {}
        self._handlers: dict[str, list[Callable]] = {}
        self._downloads: dict[str, Download] = {}
        self._download_log: list[tuple[str, str]] = []  # (page target id, guid), in order
        # TMPDIR as it is now, not as tempfile cached it at first use: a caller that
        # points TMPDIR at a private dir (the downloads gate does) gets the files there.
        self._download_dir = Path(
            tempfile.mkdtemp(prefix="lyra-native-downloads-", dir=os.environ.get("TMPDIR") or None)
        )
        self._started = False
        self._closing = False
        self._closed = False
        self._setups: set[asyncio.Task] = set()
        # Handed to cdp_guard so it never has to read the (already deleted) port file.
        self.debugging_endpoint: tuple[int, str] = process.endpoint

    # -- start -----------------------------------------------------------------------

    @classmethod
    async def launch(cls, options: dict) -> NativeContext:
        process = await launch(options)
        try:
            conn = await Connection.open(*process.endpoint)
        except BaseException:
            await process.reap()
            raise
        context = cls(process, conn, options)
        try:
            await context._start()
        except BaseException:
            await context.close()
            raise
        return context

    async def _start(self) -> None:
        conn = self._conn
        conn.on("Target.attachedToTarget", self._on_attached)
        conn.on("Target.detachedFromTarget", self._on_detached)
        conn.on("Target.targetInfoChanged", self._on_info_changed)
        conn.on("Browser.downloadWillBegin", self._on_download_begin)
        conn.on("Browser.downloadProgress", self._on_download_progress)
        conn.on_close(self._on_connection_closed)
        await conn.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "downloadPath": str(self._download_dir),
                "eventsEnabled": True,
            },
        )
        await conn.send("Target.setAutoAttach", _PAGE_ATTACH)
        # The tab Chrome opened with is attached by events that follow that reply;
        # Playwright returns a persistent context with it ready, and so does this.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_TIMEOUT_S
        while True:
            reply = await conn.send(
                "Target.getTargets", {"filter": [{"type": "page"}, {"exclude": True}]}
            )
            ids = [t["targetId"] for t in reply.get("targetInfos", [])]
            waiting = [self._ready_future(i) for i in ids]
            if waiting:
                remaining = deadline - loop.time()
                try:
                    await asyncio.wait_for(asyncio.gather(*waiting), max(remaining, 0.01))
                except TimeoutError:
                    raise Error("Timed out waiting for the browser's first tab") from None
                break
            if loop.time() >= deadline:
                raise Error("Timed out waiting for the browser's first tab")
            await asyncio.sleep(0.02)
        self._started = True

    def _ready_future(self, target_id: str) -> asyncio.Future[NativePage]:
        future = self._ready.get(target_id)
        if future is None:
            future = self._ready[target_id] = asyncio.get_running_loop().create_future()
        return future

    # -- targets ---------------------------------------------------------------------

    def _on_attached(self, params: dict, session: str | None) -> None:
        if session is not None:
            return  # a child of a page session; the page's own business
        info = params.get("targetInfo") or {}
        target_id = info.get("targetId", "")
        if info.get("type") != "page" or target_id in self._pages:
            return
        page = NativePage(
            self, target_id, params["sessionId"], info.get("openerId"), info.get("url", "")
        )
        self._pages[target_id] = page
        task = asyncio.ensure_future(self._setup(page))
        self._setups.add(task)
        task.add_done_callback(self._setups.discard)

    async def _setup(self, page: NativePage) -> None:
        headless = bool(self._options.get("headless"))
        try:
            await page._initialize(
                headless_viewport=self._options.get("viewport") if headless else None,
                screen=self._options.get("screen") if headless else None,
                block_service_workers=self._options.get("service_workers") == "block",
            )
        except (ProtocolError, TargetClosedError, TimeoutError):
            # Closed while being set up (a popup that shut itself): never reported.
            page._gone()
            self._pages.pop(page.target_id, None)
            future = self._ready_future(page.target_id)
            if not future.done():
                future.set_exception(TargetClosedError())
                future.exception()
            return
        future = self._ready_future(page.target_id)
        if not future.done():
            future.set_result(page)
        if self._started and not page.is_closed():
            self._emit("page", page)

    def _on_detached(self, params: dict, session: str | None) -> None:
        if session is not None:
            return
        target_id = params.get("targetId")
        page = self._pages.get(target_id or "")
        if page is None:
            page = next(
                (p for p in self._pages.values() if p._session == params.get("sessionId")), None
            )
        if page is not None:
            page._gone()

    def _on_info_changed(self, params: dict, session: str | None) -> None:
        info = params.get("targetInfo") or {}
        page = self._pages.get(info.get("targetId", ""))
        if page is not None and info.get("url") and not page._loader:
            page._url = info["url"]

    def _page_by_target(self, target_id: str) -> NativePage | None:
        return self._pages.get(target_id)

    # -- dialogs and downloads ---------------------------------------------------------

    def _dialog_opened(self, dialog: Dialog) -> None:
        if not self._handlers.get("dialog"):
            asyncio.ensure_future(dialog.dismiss())
            return
        self._emit("dialog", dialog)

    def _page_for_frame(self, frame_id: str) -> NativePage | None:
        page = self._pages.get(frame_id)
        if page is not None:
            return page
        return next((p for p in self._pages.values() if p.owns_frame(frame_id)), None)

    def _on_download_begin(self, params: dict, session: str | None) -> None:
        page = self._page_for_frame(params.get("frameId", ""))
        if page is None:
            live = [p for p in self._pages.values() if not p.is_closed()]
            page = live[-1] if live else None
        if page is None or not params.get("guid"):
            return
        download = Download(page, params, self._download_dir)
        self._downloads[download.guid] = download
        self._download_log.append((page.target_id, download.guid))
        del self._download_log[:-64]
        page._emit("download", download)

    def _on_download_progress(self, params: dict, session: str | None) -> None:
        download = self._downloads.get(params.get("guid", ""))
        if download is not None:
            download._progress(params.get("state", ""))

    def _download_count(self, page: NativePage) -> int:
        return sum(1 for target, _ in self._download_log if target == page.target_id)

    async def _download_started(self, page: NativePage, before: int) -> bool:
        """Whether ``page`` was offered a download since it had ``before`` of them."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DOWNLOAD_GRACE_S
        while loop.time() < deadline:
            if self._download_count(page) > before:
                return True
            await asyncio.sleep(0.02)
        return self._download_count(page) > before

    # -- the Playwright surface --------------------------------------------------------

    @property
    def pages(self) -> list[NativePage]:
        return [
            p
            for target_id, p in self._pages.items()
            if not p.is_closed()
            and self._ready_future(target_id).done()
            and not self._ready_future(target_id).exception()
        ]

    def on(self, event: str, handler: Callable) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Callable) -> None:
        with contextlib.suppress(KeyError, ValueError):
            self._handlers[event].remove(handler)

    def _emit(self, event: str, *args: Any) -> None:
        for handler in list(self._handlers.get(event, ())):
            try:
                handler(*args)
            except Exception:  # noqa: BLE001 — a listener's failure is its own
                pass

    async def new_page(self) -> NativePage:
        reply = await self._browser_send("Target.createTarget", {"url": "about:blank"})
        future = self._ready_future(reply["targetId"])
        try:
            return await asyncio.wait_for(asyncio.shield(future), READY_TIMEOUT_S)
        except TimeoutError:
            raise Error("BrowserContext.new_page: Timed out waiting for the new tab") from None

    async def new_cdp_session(self, page: NativePage) -> NativeCDPSession:
        return NativeCDPSession(page)

    async def route(self, *_args: Any, **_kwargs: Any) -> None:
        raise Error(
            "BrowserContext.route is not supported by the native driver; "
            "it runs with guard_backend='cdp'"
        )

    async def _browser_send(self, method: str, params: dict | None = None) -> dict:
        if self._closed:
            raise TargetClosedError()
        return await self._conn.send(method, params)

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        if not self._conn.closed:
            with contextlib.suppress(Exception):
                await self._conn.send("Browser.close", timeout=CLOSE_TIMEOUT_S)
        if not await self._process.wait(CLOSE_TIMEOUT_S):
            self._process.kill()
        await self._conn.close()
        await self._process.reap()
        for task in list(self._setups):
            task.cancel()
        self._on_connection_closed()
        shutil.rmtree(self._download_dir, ignore_errors=True)

    def _on_connection_closed(self) -> None:
        self._closed = True
        for page in list(self._pages.values()):
            page._gone()
        for download in self._downloads.values():
            download._browser_gone()
        for future in self._ready.values():
            if not future.done():
                future.set_exception(TargetClosedError())
                future.exception()
