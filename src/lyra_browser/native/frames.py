"""Which DevTools session each frame of a page lives in, out-of-process iframes included.

A same-site iframe renders in the page's own process and is reached through the page's
session with its ``frameId``. A cross-site one (an OOPIF) runs in another process: Chrome
gives it a target of its own, and every command about its document has to go to that
target's session. ``FrameTracker`` auto-attaches to those (without
``waitForDebuggerOnStart``, as the context does for tabs: ``cdp_guard`` holds them) and
keeps the map, so a locator or a snapshot asks one question, ``frame(frame_id)``, and gets
the session to use.

Coordinates differ the same way. Inside one session, ``DOM.getContentQuads`` answers in
the viewport of that session's top frame; an OOPIF's answers are in its own viewport.
``viewport_offset`` adds up the owner ``<iframe>`` boxes on the way out, so input lands
in the right place in the top-level window.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import ProtocolError, TargetClosedError

if TYPE_CHECKING:
    from .page import NativePage

_IFRAME_ATTACH = {
    "autoAttach": True,
    "waitForDebuggerOnStart": False,
    "flatten": True,
    "filter": [{"type": "iframe"}, {"type": "worker"}, {"exclude": True}],
}
# Dedicated workers are attached only to be let go: like a popup, one can arrive paused
# though nobody asked for that, and it then never runs (measured: a page's dedicated
# worker never started under the native driver until released).
_RELEASE_ONLY = frozenset({"worker"})
SETUP_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class FrameRef:
    """A frame and the session that speaks for its document."""

    id: str
    session: str


class FrameTracker:
    def __init__(self, page: NativePage) -> None:
        self._page = page
        # frame id -> session, for frames that live outside the page's own session
        self._session_of: dict[str, str] = {}
        # OOPIF session -> its root frame id
        self._root_of: dict[str, str] = {}
        # OOPIF root frame id -> the session whose document holds its <iframe>
        self._parent_of: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- lookup ---------------------------------------------------------------------

    def frame(self, frame_id: str) -> FrameRef:
        return FrameRef(frame_id, self._session_of.get(frame_id, self._page._session))

    def knows(self, frame_id: str) -> bool:
        return frame_id in self._session_of or self._page.owns_frame(frame_id)

    def is_out_of_process(self, frame_id: str) -> bool:
        return frame_id in self._parent_of

    # -- attach ---------------------------------------------------------------------

    async def watch(self, session: str) -> None:
        """Hear about the OOPIFs whose ``<iframe>`` lives in ``session``'s documents."""
        conn = self._page._context._conn
        conn.on("Target.attachedToTarget", self._on_attached, session=session)
        conn.on("Target.detachedFromTarget", lambda p, _s: self._on_detached(p), session=session)
        await conn.send(
            "Target.setAutoAttach", _IFRAME_ATTACH, session=session, timeout=SETUP_TIMEOUT_S
        )

    def _on_attached(self, params: dict, parent: str | None) -> None:
        info = params.get("targetInfo") or {}
        child = params.get("sessionId")
        if info.get("type") in _RELEASE_ONLY and child:
            conn = self._page._context._conn
            task = asyncio.ensure_future(
                conn.send("Runtime.runIfWaitingForDebugger", session=child)
            )
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            return
        if info.get("type") != "iframe" or not child or parent is None:
            return
        frame_id = info.get("targetId", "")
        self._session_of[frame_id] = child
        self._root_of[child] = frame_id
        self._parent_of[frame_id] = parent
        # Its world in the parent process went with the document that moved out.
        self._page._worlds.forget(frame_id)
        task = asyncio.ensure_future(self._setup(child))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _setup(self, session: str) -> None:
        page = self._page
        conn = page._context._conn

        def navigated(params: dict, _s: str | None) -> None:
            frame = params.get("frame") or {}
            if frame.get("id"):
                self._session_of.setdefault(frame["id"], session)
                page._worlds.forget(frame["id"])
                page._frame_urls[frame["id"]] = frame.get("url", "") + frame.get("urlFragment", "")

        def attached(params: dict, _s: str | None) -> None:
            if params.get("frameId"):
                self._session_of.setdefault(params["frameId"], session)

        def detached(params: dict, _s: str | None) -> None:
            frame_id = params.get("frameId", "")
            page._worlds.forget(frame_id)
            if self._session_of.get(frame_id) == session and frame_id not in self._parent_of:
                del self._session_of[frame_id]

        conn.on("Page.frameNavigated", navigated, session=session)
        conn.on("Page.frameAttached", attached, session=session)
        conn.on("Page.frameDetached", detached, session=session)
        try:
            await conn.send("Page.enable", session=session, timeout=SETUP_TIMEOUT_S)
            await self.watch(session)
            tree = await conn.send("Page.getFrameTree", session=session, timeout=SETUP_TIMEOUT_S)
        except (ProtocolError, TargetClosedError, TimeoutError):
            return  # gone again already (an ad that reloads itself away)
        self._absorb(tree.get("frameTree") or {}, session)

    def _absorb(self, node: dict, session: str) -> None:
        frame = node.get("frame") or {}
        if frame.get("id"):
            self._session_of.setdefault(frame["id"], session)
        for child in node.get("childFrames") or ():
            self._absorb(child, session)

    def _on_detached(self, params: dict) -> None:
        session = params.get("sessionId")
        root = self._root_of.pop(session or "", None)
        if root is None:
            return
        self._parent_of.pop(root, None)
        for frame_id in [f for f, s in self._session_of.items() if s == session]:
            del self._session_of[frame_id]
            self._page._worlds.forget(frame_id)

    # -- geometry -------------------------------------------------------------------

    async def frame_origin(self, frame: FrameRef) -> tuple[float, float]:
        """Where ``frame``'s viewport starts, in the coordinates of its own session.

        Zero for a session's top frame; for a same-process child, the content box of
        its ``<iframe>``.
        """
        if frame.id == self._page.main_frame_id or frame.id in self._parent_of:
            return 0.0, 0.0
        return await self._owner_content(frame.session, frame.id)

    async def viewport_offset(self, frame: FrameRef) -> tuple[float, float]:
        """What to add to ``frame.session`` coordinates to get top-level viewport ones."""
        x = y = 0.0
        session = frame.session
        seen: set[str] = set()
        while session != self._page._session and session not in seen:
            seen.add(session)
            root = self._root_of.get(session)
            parent = self._parent_of.get(root or "")
            if root is None or parent is None:
                break
            dx, dy = await self._owner_content(parent, root)
            x, y = x + dx, y + dy
            session = parent
        return x, y

    async def _owner_content(self, session: str, frame_id: str) -> tuple[float, float]:
        page = self._page
        with contextlib.suppress(ProtocolError):
            owner = await page._send_on(session, "DOM.getFrameOwner", {"frameId": frame_id})
            box = await page._send_on(
                session, "DOM.getBoxModel", {"backendNodeId": owner["backendNodeId"]}
            )
            content = box["model"]["content"]
            return float(content[0]), float(content[1])
        return 0.0, 0.0
