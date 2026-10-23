"""``Locator`` and ``FrameLocator``: lazy element queries, acted on the way Playwright acts.

A locator is a selector, not a handle. Every call resolves it afresh in the frame's
isolated world (``selectors.py`` parses it, ``injected.py`` runs the steps, the role
step asks Chrome's accessibility tree), then acts on the first match. Actions wait and
retry until the timeout, the way Playwright's do, and log each check that held them up
in Playwright's words, because ``actionable.describe_failure`` reads those words:

- click: attached, visible, enabled, stable, scrolled into view, and the centre of the
  element is what a click there would hit (else ``<x> intercepts pointer events``);
- hover: the same without enabled; fill: visible, enabled, editable.

Remote objects are created in a per-attempt object group and released after it, so a
long session does not accumulate references to dead nodes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import Error, ProtocolError, timeout_error, with_call_log
from .evaluate import MAIN, NO_ARG, call_argument, decode_result, is_function
from .injected import (
    FILE_INPUT_JS,
    FILL_JS,
    FOCUS_JS,
    HIT_JS,
    INNER_TEXT_JS,
    INPUT_VALUE_JS,
    SELECT_JS,
    SET_VALUE_JS,
    STABLE_JS,
    STATE_JS,
    STEP_JS,
)
from .selectors import Part, describe, parse

if TYPE_CHECKING:
    from .page import NativePage

DEFAULT_TIMEOUT_MS = 30_000.0
_RETRY_DELAYS_S = (0.0, 0.02, 0.05, 0.1, 0.1, 0.5)
_STABLE_BUDGET_MS = 500
# What Chrome says about an object or node that went away under a call.
_DETACHED = (
    "could not find object with given id",
    "cannot find context with specified id",
    "execution context was destroyed",
    "node with given id does not belong to the document",
    "no node with given id found",
    "node is detached from document",
    "cannot find object with given id",
)
_groups = itertools.count(1)


class _Retry:
    """Returned by an attempt that should be tried again."""


RETRY = _Retry()


def _is_detached(exc: BaseException) -> bool:
    return isinstance(exc, ProtocolError) and any(m in exc.reason.lower() for m in _DETACHED)


def _states_match(node: dict, wanted: tuple[tuple[str, str], ...]) -> bool:
    props = {p.get("name"): (p.get("value") or {}).get("value") for p in node.get("properties", [])}
    for name, value in wanted:
        actual = props.get(name)
        if actual is None:
            actual = False
        if str(actual).lower() != value:
            return False
    return True


class FrameLocator:
    """``page.frame_locator(selector)``: locators inside the matched ``<iframe>``."""

    def __init__(self, page: NativePage, frames: tuple[str, ...]) -> None:
        self._page = page
        self._frames = frames

    def locator(self, selector: str) -> Locator:
        return Locator(self._page, selector, self._frames)

    def frame_locator(self, selector: str) -> FrameLocator:
        return FrameLocator(self._page, (*self._frames, selector))


class Locator:
    def __init__(self, page: NativePage, selector: str, frames: tuple[str, ...] = ()) -> None:
        self._page = page
        self._selector = selector
        self._frames = frames

    def __repr__(self) -> str:
        return f"<Locator {describe(self._selector)}>"

    # -- composition --------------------------------------------------------------

    @property
    def first(self) -> Locator:
        return Locator(self._page, f"{self._selector} >> nth=0", self._frames)

    @property
    def last(self) -> Locator:
        return Locator(self._page, f"{self._selector} >> nth=-1", self._frames)

    def nth(self, index: int) -> Locator:
        return Locator(self._page, f"{self._selector} >> nth={index}", self._frames)

    def locator(self, selector: str) -> Locator:
        return Locator(self._page, f"{self._selector} >> {selector}", self._frames)

    # -- resolution ---------------------------------------------------------------

    async def _resolve(self, group: str) -> tuple[str, list[str]]:
        """The frame the elements live in, and the matched elements (isolated world)."""
        page = self._page
        frame_id = page.main_frame_id
        for frame_selector in self._frames:
            owners = await self._run(frame_id, [*parse(frame_selector), Part("nth", "0")], group)
            if not owners:
                return frame_id, []
            node = (await page._send("DOM.describeNode", {"objectId": owners[0]}))["node"]
            child = node.get("frameId")
            if not child or node.get("nodeName", "").upper() not in ("IFRAME", "FRAME"):
                raise Error(f"Selector {describe(frame_selector)} does not match an <iframe>")
            frame_id = child
        return frame_id, await self._run(frame_id, parse(self._selector), group)

    async def _run(self, frame_id: str, steps: list[Part], group: str) -> list[str]:
        page = self._page
        context_id = await page._context_id(frame_id)
        current: list[str] | None = None
        for step in steps:
            if step.engine == "nth":
                items = current or []
                index = int(step.body)
                current = [items[index]] if -len(items) <= index < len(items) else []
            elif step.engine == "role":
                current = await self._role_step(context_id, step, current, group)
            elif step.engine == "aria-ref":
                current = await page._resolve_ref(step.body, frame_id, context_id, group)
            else:
                current = await self._js_step(context_id, step, current, group)
            if not current:
                return []
        return current or []

    async def _js_step(
        self, context_id: int, step: Part, scopes: list[str] | None, group: str
    ) -> list[str]:
        page = self._page
        arguments = [{"value": step.as_js()}, *({"objectId": o} for o in scopes or ())]
        reply = await page._send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": STEP_JS,
                "executionContextId": context_id,
                "arguments": arguments,
                "returnByValue": False,
                "objectGroup": group,
            },
        )
        if reply.get("exceptionDetails"):
            decode_result("Locator", reply)  # raises with the JS error's words
        array_id = (reply.get("result") or {}).get("objectId")
        if not array_id:
            return []
        props = await page._send(
            "Runtime.getProperties", {"objectId": array_id, "ownProperties": True}
        )
        items = [
            (int(p["name"]), p["value"]["objectId"])
            for p in props.get("result", [])
            if p.get("name", "").isdigit() and (p.get("value") or {}).get("objectId")
        ]
        return [object_id for _i, object_id in sorted(items)]

    async def _role_step(
        self, context_id: int, step: Part, scopes: list[str] | None, group: str
    ) -> list[str]:
        page = self._page
        if scopes is None:
            reply = await page._send(
                "Runtime.evaluate",
                {"expression": "document", "contextId": context_id, "objectGroup": group},
            )
            scopes = [reply["result"]["objectId"]]
        found: list[str] = []
        seen: set[int] = set()
        for scope in scopes:
            nodes = (
                await page._send(
                    "Accessibility.queryAXTree", {"objectId": scope, "role": step.role}
                )
            ).get("nodes", [])
            for node in nodes:
                backend = node.get("backendDOMNodeId")
                if not backend or backend in seen:
                    continue
                if node.get("ignored") and not step.include_hidden:
                    continue
                name = (node.get("name") or {}).get("value", "") or ""
                if step.name is not None and not step.name.matches(str(name)):
                    continue
                if not _states_match(node, step.states):
                    continue
                seen.add(backend)
                resolved = await page._send(
                    "DOM.resolveNode",
                    {
                        "backendNodeId": backend,
                        "executionContextId": context_id,
                        "objectGroup": group,
                    },
                )
                found.append(resolved["object"]["objectId"])
        return found

    # -- helpers on one element -----------------------------------------------------

    async def _call(
        self, element: str, source: str, *args: Any, by_value: bool = True, api: str = "Locator"
    ) -> Any:
        reply = await self._page._send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": source,
                "objectId": element,
                "arguments": [call_argument(a) for a in args],
                "returnByValue": by_value,
                "awaitPromise": True,
            },
        )
        return decode_result(api, reply)

    async def _point(self, element: str) -> tuple[float, float] | None:
        """The centre of the element in viewport coordinates, if it is inside the viewport."""
        page = self._page
        try:
            quads = (await page._send("DOM.getContentQuads", {"objectId": element})).get(
                "quads", []
            )
        except ProtocolError as exc:
            if _is_detached(exc):
                raise
            return None
        if not quads:
            return None
        quad = quads[0]
        x = sum(quad[0::2]) / 4
        y = sum(quad[1::2]) / 4
        metrics = await page._send("Page.getLayoutMetrics")
        viewport = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
        width, height = viewport.get("clientWidth", 0), viewport.get("clientHeight", 0)
        if width and height and not (0 <= x < width and 0 <= y < height):
            return None
        return x, y

    async def _scroll_into_view(self, element: str) -> None:
        with contextlib.suppress(ProtocolError):
            await self._page._send("DOM.scrollIntoViewIfNeeded", {"objectId": element})

    # -- the retry loop -----------------------------------------------------------

    async def _attempts(
        self,
        api: str,
        timeout: float | None,
        attempt: Callable[[str, str, list[str]], Awaitable[Any]],
        *,
        need_element: bool = True,
    ) -> Any:
        """Resolve and run ``attempt(frame_id, element, log)`` until it is not ``RETRY``."""
        page = self._page
        loop = asyncio.get_running_loop()
        timeout_ms = DEFAULT_TIMEOUT_MS if timeout is None else float(timeout)
        deadline = loop.time() + timeout_ms / 1000 if timeout_ms > 0 else float("inf")
        log: list[str] = [f"waiting for {describe(self._selector)}"]
        for round_no in itertools.count():
            group = f"lyra-{next(_groups)}"
            try:
                frame_id, elements = await self._resolve(group)
                if elements or not need_element:
                    result = await attempt(frame_id, elements[0] if elements else "", log)
                    if result is not RETRY:
                        return result
            except ProtocolError as exc:
                if not _is_detached(exc):
                    raise Error(with_call_log(api, str(exc), log)) from None
                page._worlds.clear()
                _note(log, "  - element was detached from the DOM, retrying")
            finally:
                page._release_group(group)
            if loop.time() >= deadline:
                raise timeout_error(api, timeout_ms, log)
            delay = _RETRY_DELAYS_S[min(round_no, len(_RETRY_DELAYS_S) - 1)]
            await asyncio.sleep(max(0.0, min(delay, deadline - loop.time())))
        raise AssertionError("unreachable")

    async def _state(self, element: str, log: list[str]) -> dict:
        state = await self._call(element, STATE_JS)
        _note(log, f"  - locator resolved to {state.get('html', '')}", once=True)
        return state

    async def _ready(
        self, element: str, log: list[str], *, enabled: bool = False, editable: bool = False
    ) -> bool:
        state = await self._state(element, log)
        if not state.get("connected"):
            _note(log, "  - element is not attached to the DOM")
            return False
        if not state.get("visible"):
            _note(log, "  - element is not visible")
            return False
        if enabled and not state.get("enabled"):
            _note(log, "  - element is not enabled")
            return False
        if editable and not state.get("editable"):
            _note(log, "  - element is not editable")
            return False
        return True

    async def _pointer(
        self, element: str, log: list[str], action: str, *, enabled: bool
    ) -> tuple[float, float] | _Retry:
        """Every check a pointer action makes, and the point it acts at."""
        if not await self._ready(element, log, enabled=enabled):
            return RETRY
        await self._scroll_into_view(element)
        if not await self._call(element, STABLE_JS, _STABLE_BUDGET_MS):
            _note(log, "  - element is not stable")
            return RETRY
        point = await self._point(element)
        if point is None:
            _note(log, "  - element is outside of the viewport")
            return RETRY
        hit = await self._call(element, HIT_JS)
        if not hit.get("ok"):
            if hit.get("by"):
                _note(log, f"  - {hit['by']} intercepts pointer events")
            else:
                _note(log, "  - element is outside of the viewport")
            return RETRY
        _note(log, f"  - performing {action} action")
        return point

    # -- the Playwright surface -----------------------------------------------------

    async def count(self) -> int:
        group = f"lyra-{next(_groups)}"
        try:
            _frame, elements = await self._resolve(group)
            return len(elements)
        except ProtocolError as exc:
            if _is_detached(exc):
                self._page._worlds.clear()
                return 0
            raise Error(f"Locator.count: {exc}") from None
        finally:
            self._page._release_group(group)

    async def is_visible(self) -> bool:
        group = f"lyra-{next(_groups)}"
        try:
            _frame, elements = await self._resolve(group)
            if not elements:
                return False
            return bool((await self._call(elements[0], STATE_JS)).get("visible"))
        finally:
            self._page._release_group(group)

    async def is_enabled(self, *, timeout: float | None = None) -> bool:
        async def attempt(_frame: str, element: str, log: list[str]) -> bool:
            return bool((await self._state(element, log)).get("enabled"))

        return await self._attempts("Locator.is_enabled", timeout, attempt)

    async def click(self, *, timeout: float | None = None, **_ignored: Any) -> None:
        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            point = await self._pointer(element, log, "click", enabled=True)
            if point is RETRY:
                return RETRY
            await self._page.mouse.click(*point)
            _note(log, "  - click action done")
            return None

        await self._attempts("Locator.click", timeout, attempt)

    async def hover(self, *, timeout: float | None = None, **_ignored: Any) -> None:
        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            point = await self._pointer(element, log, "hover", enabled=False)
            if point is RETRY:
                return RETRY
            await self._page.mouse.move(*point)
            _note(log, "  - hover action done")
            return None

        await self._attempts("Locator.hover", timeout, attempt)

    async def fill(self, value: str, *, timeout: float | None = None, **_ignored: Any) -> None:
        api = "Locator.fill"

        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            if not await self._ready(element, log, enabled=True, editable=True):
                return RETRY
            prepared = await self._call(element, FILL_JS, api=api)
            if prepared.get("error"):
                raise Error(with_call_log(api, f"Error: {prepared['error']}", log))
            _note(log, '  - fill("…")')
            if prepared.get("set"):
                await self._call(element, SET_VALUE_JS, value, api=api)
            elif value:
                await self._page.keyboard.insert_text(value)
            else:
                await self._page.keyboard.press("Delete")
            return None

        await self._attempts(api, timeout, attempt)

    async def press(self, key: str, *, timeout: float | None = None, **_ignored: Any) -> None:
        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            await self._call(element, FOCUS_JS)
            await self._page.keyboard.press(key)
            return None

        await self._attempts("Locator.press", timeout, attempt)

    async def check(self, *, timeout: float | None = None, **_ignored: Any) -> None:
        api = "Locator.check"

        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            state = await self._state(element, log)
            if state.get("checked") is None:
                raise Error(with_call_log(api, "Error: Not a checkbox or radio button", log))
            if state["checked"]:
                return None
            point = await self._pointer(element, log, "click", enabled=True)
            if point is RETRY:
                return RETRY
            await self._page.mouse.click(*point)
            if not (await self._call(element, STATE_JS)).get("checked"):
                raise Error(
                    with_call_log(api, "Clicking the checkbox did not change its state", log)
                )
            return None

        await self._attempts(api, timeout, attempt)

    async def select_option(
        self,
        value: str | None = None,
        *,
        label: str | None = None,
        index: int | None = None,
        timeout: float | None = None,
        **_ignored: Any,
    ) -> list[str]:
        api = "Locator.select_option"
        if value is not None:
            want: dict = {"value": value}
        elif label is not None:
            want = {"label": label}
        elif index is not None:
            want = {"index": index}
        else:
            raise Error(f"{api}: no option to select was given")

        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            if not await self._ready(element, log, enabled=True):
                return RETRY
            result = await self._call(element, SELECT_JS, want, api=api)
            if result.get("error"):
                raise Error(with_call_log(api, f"Error: {result['error']}", log))
            if result.get("missing"):
                _note(log, "  - did not find some options")
                return RETRY
            return list(result.get("values") or [])

        return await self._attempts(api, timeout, attempt)

    async def input_value(self, *, timeout: float | None = None) -> str:
        async def attempt(_frame: str, element: str, log: list[str]) -> str:
            return str(await self._call(element, INPUT_VALUE_JS, api="Locator.input_value"))

        return await self._attempts("Locator.input_value", timeout, attempt)

    async def set_input_files(
        self, files: Any, *, timeout: float | None = None, **_ignored: Any
    ) -> None:
        api = "Locator.set_input_files"
        paths = [files] if isinstance(files, (str, Path)) else list(files)
        resolved = [str(Path(p).resolve()) for p in paths]

        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            problem = await self._call(element, FILE_INPUT_JS, len(resolved), api=api)
            if problem:
                raise Error(with_call_log(api, f"Error: {problem}", log))
            await self._page._send(
                "DOM.setFileInputFiles", {"files": resolved, "objectId": element}
            )
            return None

        await self._attempts(api, timeout, attempt)

    async def inner_text(self, *, timeout: float | None = None) -> str:
        async def attempt(_frame: str, element: str, log: list[str]) -> str:
            return str(await self._call(element, INNER_TEXT_JS, api="Locator.inner_text") or "")

        return await self._attempts("Locator.inner_text", timeout, attempt)

    async def evaluate(
        self,
        expression: str,
        arg: Any = NO_ARG,
        *,
        world: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        """The element is the function's first parameter; ``world="main"`` as on ``Page``."""
        api = "Locator.evaluate"
        source = (
            expression if is_function(expression) else f"function () {{ return ({expression}); }}"
        )

        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            target = element
            if world == MAIN:
                node = (await self._page._send("DOM.describeNode", {"objectId": element}))["node"]
                target = (
                    await self._page._send(
                        "DOM.resolveNode", {"backendNodeId": node["backendNodeId"]}
                    )
                )["object"]["objectId"]
            arguments = [{"objectId": target}]
            if arg is not NO_ARG:
                arguments.append(call_argument(arg))
            reply = await self._page._send(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": source,
                    "objectId": target,
                    "arguments": arguments,
                    "returnByValue": True,
                    "awaitPromise": True,
                    "userGesture": True,
                },
            )
            return decode_result(api, reply)

        return await self._attempts(api, timeout, attempt)

    async def scroll_into_view_if_needed(self, *, timeout: float | None = None) -> None:
        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            if not await self._ready(element, log):
                return RETRY
            await self._page._send("DOM.scrollIntoViewIfNeeded", {"objectId": element})
            return None

        await self._attempts("Locator.scroll_into_view_if_needed", timeout, attempt)

    async def screenshot(self, *, timeout: float | None = None, **_ignored: Any) -> bytes:
        api = "Locator.screenshot"

        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            if not await self._ready(element, log):
                return RETRY
            await self._scroll_into_view(element)
            quads = (await self._page._send("DOM.getContentQuads", {"objectId": element})).get(
                "quads", []
            )
            if not quads:
                _note(log, "  - element is not visible")
                return RETRY
            xs = [v for q in quads for v in q[0::2]]
            ys = [v for q in quads for v in q[1::2]]
            metrics = await self._page._send("Page.getLayoutMetrics")
            visual = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
            clip = {
                "x": min(xs) + visual.get("pageX", 0),
                "y": min(ys) + visual.get("pageY", 0),
                "width": max(xs) - min(xs),
                "height": max(ys) - min(ys),
                "scale": 1,
            }
            try:
                reply = await self._page._send(
                    "Page.captureScreenshot",
                    {"format": "png", "clip": clip, "captureBeyondViewport": True},
                )
            except ProtocolError as exc:
                if _is_detached(exc):
                    raise
                raise Error(f"{api}: {exc}") from None
            return base64.b64decode(reply.get("data", ""))

        return await self._attempts(api, timeout, attempt)

    async def wait_for(self, *, state: str = "visible", timeout: float | None = None) -> None:
        api = "Locator.wait_for"
        if state not in ("attached", "detached", "visible", "hidden"):
            raise Error(f"{api}: state: expected one of (attached|detached|visible|hidden)")

        async def attempt(_frame: str, element: str, log: list[str]) -> Any:
            if not element:
                return None if state in ("detached", "hidden") else RETRY
            if state == "attached":
                return None
            if state == "detached":
                return RETRY
            visible = bool((await self._state(element, log)).get("visible"))
            return None if visible == (state == "visible") else RETRY

        await self._attempts(api, timeout, attempt, need_element=False)

    async def aria_snapshot(self, *, timeout: float | None = None) -> str:
        raise Error("Locator.aria_snapshot is not implemented by the native driver yet")


def _note(log: list[str], line: str, *, once: bool = False) -> None:
    """Add a call-log line, once if it repeats the last one (or is a ``once`` line seen before)."""
    if once and line in log:
        return
    if log and log[-1] == line:
        return
    log.append(line)
    del log[1:-40]
