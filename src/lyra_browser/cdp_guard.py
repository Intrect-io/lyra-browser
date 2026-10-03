"""The CDP backend of the navigation guard: documents only, from a second connection.

``context.route("**/*")`` is how the guard was installed, and Playwright turns it into
``Fetch.enable`` on *every* request plus ``Network.setCacheDisabled(true)``. DataDome
(etsy.com) refuses a browser launched that way: the cache switch makes the document
request itself carry ``Cache-Control: no-cache``. The route also
never sees an HTTP redirect hop, because Playwright continues those itself.

This backend replaces the route. Chrome is launched with ``--remote-debugging-port=0``
next to Playwright's own pipe, and this module opens a second DevTools connection to
it. Over that connection it

- auto-attaches to every page and every out-of-process iframe at *browser* level,
  with ``waitForDebuggerOnStart``, so a popup or an OOPIF is held until
  ``Fetch.enable`` for ``Document`` requests is in place on it. A session made through
  Playwright after the tab exists cannot promise that: the first request of every
  popup and every cross-site iframe request escaped (measured);
- receives ``Fetch.requestPaused`` for each document request *and each redirect
  hop*, builds the two things ``enforcement.classify`` reads (a request and a frame),
  and asks the one judgement, ``NavigationGuard.decide``, whether it may leave. The
  answer is ``Fetch.continueRequest`` or a 204 ``Fetch.fulfillRequest``, as the route
  adapter sends. Nothing here knows what a grant is;
- turns the service worker off for every guarded page (``Network.setBypassServiceWorker``),
  because a navigation a service worker answers never reaches ``Fetch`` at all: a
  pre-registered worker for another origin served that origin's page with the guard
  seeing nothing (measured, both backends). Playwright's ``service_workers="block"`` is
  an init script that a page can step around through the prototype.

**It fails closed.** Chrome resumes every paused request, and every target waiting for
the debugger, the moment this connection ends. So a lost connection is not "the guard
is off for a while": the browser is running unguarded. ``on_lost`` is how the session
learns of it, and the session kills the browser (see ``BrowserSession``). A target whose
``Fetch.enable`` fails is never resumed, and any other failure to set a target up
(seen only after the resume, see ``_setup``) counts as a lost connection.

**It is an open port.** ``--remote-debugging-port`` is an unauthenticated service on
``127.0.0.1``. Any local process that finds it can drive the browser with every login in
the profile. The port is random, bound to loopback by Chrome, the file that names it is
deleted as soon as it is read, and neither port nor path is ever put in an exception, a
log line or an audit row. What remains is in README.md ("Debugging port").
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .cdp_socket import WebSocket, WebSocketError

ACTIVE_PORT_FILE = "DevToolsActivePort"
_ENDPOINT_PATH = re.compile(r"/devtools/browser/[0-9A-Fa-f-]{8,64}")
_LOOPBACK = "127.0.0.1"

# Target types that get a session of their own with Fetch enabled. Workers never issue a
# document request; a service worker could answer one, which is what the bypass is for.
_GUARDED_TYPES = frozenset({"page", "iframe"})
_FETCH_DOCUMENTS = {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]}
# Network is enabled only because the service-worker bypass needs it; the events it
# produces are dropped unread, and the payload buffers are not needed.
_NETWORK_QUIET = {"maxTotalBufferSize": 0, "maxResourceBufferSize": 0}
_PAGE_ATTACH = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": True,
    "filter": [{"type": "page"}, {"exclude": True}],
}
_IFRAME_ATTACH = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": True,
    "filter": [{"type": "iframe"}, {"exclude": True}],
}

CONNECT_TIMEOUT_S = 10.0
SETUP_TIMEOUT_S = 10.0
# How long after the connection ended a browser gets to be gone by itself before the
# end is read as a failure of the guard. An ordinary close takes the socket and the
# process down within a few milliseconds of each other.
EXIT_GRACE_S = 0.3
# Redirect hops remembered so a hop can say where it came from; oldest forgotten first.
_CHAIN_MAX = 128

_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


class CdpGuardError(Exception):
    """The sidecar could not start, or can no longer vouch for the browser.

    Messages are fixed vocabulary. The debugging endpoint is a bearer credential and
    must not reach an envelope, an audit row or a traceback through this exception.
    """


class _CallError(CdpGuardError):
    """Chrome answered a command with an error."""

    def __init__(self, method: str, code: int | None, message: str) -> None:
        super().__init__(f"{method} failed")
        self.code = code
        self.message = message


def _gone(exc: _CallError) -> bool:
    """Whether the command failed because its target had already gone away."""
    text = exc.message.lower()
    return exc.code == -32001 or "not found" in text or "closed" in text


def _retrieve(fut: asyncio.Future) -> None:
    """Mark a command's outcome as seen, so one nobody awaited is not reported as unhandled."""
    if not fut.cancelled():
        fut.exception()


_REQUEST_ID = re.compile(rb'"requestId":"([^"\\]{1,200})"')
_SESSION_ID = re.compile(rb'"sessionId":"([^"\\]{1,200})"\}\s*$')


def shrink_oversize(head: bytes, tail: bytes) -> str | None:
    """What a message too big to keep is reduced to: the same event without its upload.

    Chrome puts a navigation's whole body into ``Fetch.requestPaused`` — as text and again
    as base64, so an 8 MB form is a 20 MB message (measured; ``Network.enable``'s
    ``maxPostDataSize`` does not apply). Closing the connection over it would kill the
    browser for uploading a file, and holding it would be memory for nothing: ``classify``
    only asks whether a body exists. The transport hands over the first and last bytes of
    such a message; the body sits between ``"postData"`` and ``"initialPriority"``, and a
    JSON string cannot contain either key unescaped, so cutting there is exact.

    If the cut cannot be found (Chrome ordered its keys differently) the request is still
    named, with nothing else, and the judge refuses it as one it cannot read. Only an event
    that cannot even be named is an error, which closes the guard.
    """
    if head.startswith(b'{"method":"Network.'):
        return '{"method":"Network.dropped"}'
    if not head.startswith(b'{"method":"Fetch.requestPaused"'):
        return None
    request_id, session = _REQUEST_ID.search(head), _SESSION_ID.search(tail)
    if request_id is None or session is None:
        return None
    cut, resume = head.find(b',"postData":'), tail.find(b',"initialPriority":')
    if cut >= 0 and resume >= 0:
        return (head[:cut] + b',"hasPostData":true' + tail[resume:]).decode("utf-8", "replace")
    return json.dumps(
        {
            "method": "Fetch.requestPaused",
            "params": {"requestId": request_id.group(1).decode("ascii", "replace"), "request": {}},
            "sessionId": session.group(1).decode("ascii", "replace"),
        }
    )


class Judge(Protocol):
    """What ``CdpGuard`` needs from ``NavigationGuard``."""

    def decide(self, request: Any) -> bool: ...

    def verdict_on_error(self, exc: Exception) -> bool: ...


# --------------------------------------------------------------------------
# What enforcement.classify reads, built from CDP events
# --------------------------------------------------------------------------


class PausedFrame:
    """The slice of a Playwright Frame that ``enforcement`` reads."""

    __slots__ = ("parent_frame", "url")

    def __init__(self, url: str, parent_frame: PausedFrame | None) -> None:
        self.url = url
        self.parent_frame = parent_frame


class _RedirectSource:
    """The slice of ``Request.redirected_from`` that ``enforcement`` reads."""

    __slots__ = ("url",)

    def __init__(self, url: str) -> None:
        self.url = url


class PausedRequest:
    """The slice of a Playwright Request that ``enforcement.classify`` reads.

    Built from one ``Fetch.requestPaused`` event. ``frame`` raises for a frame this
    session's tree does not know, exactly as Playwright does for some navigation
    requests, so the guard's fail-closed branch (an opaque initiator) is the one that
    runs. A body too large or binary to come inline is reported by Chrome as
    ``hasPostData`` and becomes a placeholder here: ``classify`` only asks whether a
    body exists, never what it says.
    """

    def __init__(
        self, event: dict, frames: dict[str, PausedFrame], redirect_from: str = ""
    ) -> None:
        req = event["request"]
        self.url: str = req["url"]
        self.method: str = req["method"]
        body = req.get("postData")
        if not body and (req.get("hasPostData") or req.get("postDataEntries")):
            body = "<body>"
        self.post_data: str | None = body or None
        self._document = event.get("resourceType") == "Document"
        self._frame = frames.get(event.get("frameId", ""))
        self.redirected_from = _RedirectSource(redirect_from) if redirect_from else None

    def is_navigation_request(self) -> bool:
        return self._document

    @property
    def frame(self) -> PausedFrame:
        if self._frame is None:
            raise RuntimeError("Frame not available for this request")
        return self._frame


class FrameCache:
    """Which frames a session has, and where each points, kept from ``Page.*`` events.

    Renderer-bound commands (``Page.getFrameTree``, ``Runtime.evaluate``) do not answer
    while a *main-frame* navigation request is paused: measured, they sit for as long
    as the request is held. The judge therefore never asks the page anything; it reads
    this, which the event stream keeps current (Playwright's own frame model is fed by
    the same events).
    """

    def __init__(self, *, root_is_subframe: bool = False) -> None:
        self.frames: dict[str, PausedFrame] = {}
        # The root frame of an iframe target (an out-of-process iframe) is a subframe
        # whose parent lives in another session.
        self._root_is_subframe = root_is_subframe

    def _node(self, frame_id: str, parent_id: str | None) -> PausedFrame:
        node = self.frames.get(frame_id)
        if node is not None:
            return node
        parent = None
        if parent_id is not None:
            parent = self.frames.get(parent_id)
            if parent is None:  # the parent is in another process: a stand-in, no origin
                parent = self.frames[parent_id] = PausedFrame("", None)
        elif self._root_is_subframe:
            parent = PausedFrame("", None)
        node = self.frames[frame_id] = PausedFrame("", parent)
        return node

    def load(self, tree: dict) -> None:
        frame = tree["frame"]
        self._node(frame["id"], frame.get("parentId")).url = frame.get("url", "")
        for child in tree.get("childFrames", []):
            self.load(child)

    def attached(self, params: dict) -> None:
        self._node(params["frameId"], params.get("parentFrameId"))

    def navigated(self, params: dict) -> None:
        frame = params["frame"]
        self._node(frame["id"], frame.get("parentId")).url = frame.get("url", "")

    def within_document(self, params: dict) -> None:
        """``history.pushState`` and fragment changes: same document, new URL.

        Only for a frame already known: creating one here would give it no parent, and
        a subframe without a parent reads as the main frame.
        """
        node = self.frames.get(params["frameId"])
        if node is not None:
            node.url = params.get("url", "")

    def detached(self, params: dict) -> None:
        self.frames.pop(params["frameId"], None)


class _Target:
    """One guarded page or iframe target and its flat session."""

    __slots__ = ("frames", "kind", "session", "target_id")

    def __init__(self, session: str, kind: str, target_id: str) -> None:
        self.session = session
        self.kind = kind
        self.target_id = target_id
        self.frames = FrameCache(root_is_subframe=kind == "iframe")


# --------------------------------------------------------------------------
# The endpoint file
# --------------------------------------------------------------------------


def prepare_profile(profile_dir: Path) -> None:
    """Before launch: no stale endpoint file, and a profile only its owner can enter.

    Chrome leaves ``DevToolsActivePort`` behind when it exits (measured), and writes
    the new one a moment after it starts, so reading the file without removing the old
    one first could connect to a port that is now someone else's. The file itself is
    created world-readable; the directory is what keeps other users out of it, and the
    profile holds real logins besides, so it is made private.
    """
    with contextlib.suppress(OSError):
        (profile_dir / ACTIVE_PORT_FILE).unlink()
    with contextlib.suppress(OSError):
        os.chmod(profile_dir, 0o700)


async def read_endpoint(profile_dir: Path, timeout: float) -> tuple[int, str]:
    """``(port, path)`` of the browser endpoint Chrome wrote, and delete the file.

    Deleted at once: it names an unauthenticated port and has no further use. (A
    process that scans loopback finds the port anyway; this only stops the file being
    the easy way.)
    """
    file = profile_dir / ACTIVE_PORT_FILE
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            lines = file.read_text(encoding="ascii").splitlines()
        except (OSError, ValueError):
            lines = []
        # Two lines, the second complete: Chrome may be half-way through writing them.
        if len(lines) >= 2 and lines[0].isdigit() and _ENDPOINT_PATH.fullmatch(lines[1]):
            port = int(lines[0])
            if 0 < port < 65536:
                with contextlib.suppress(OSError):
                    file.unlink()
                return port, lines[1]
        if loop.time() >= deadline:
            raise CdpGuardError("no debugging endpoint")
        await asyncio.sleep(0.05)


def _alive(pid: int) -> bool:
    """Whether ``pid`` is a running process (a zombie awaiting its parent is not)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            state = fh.read().rsplit(b")", 1)[1].split()[0]
        return state not in (b"Z", b"X")
    except (OSError, IndexError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


class CdpGuard:
    """Judges the document requests of every page and iframe of one browser.

    ``judge`` is the ``NavigationGuard``; ``on_lost(reason)`` is called once, from the
    reader task, if the connection ends or the guard fails while it is not being closed
    on purpose. It must only start the work of closing the browser.
    """

    def __init__(self, judge: Judge, *, on_lost: Callable[[str], None]) -> None:
        self._judge = judge
        self._on_lost = on_lost
        self._ws: WebSocket | None = None
        self._reader: asyncio.Task | None = None
        self._next_id = 0
        # id -> (future, method, session). The future is None for a command whose answer
        # nobody awaits (the verdicts): only an error in it is counted.
        self._pending: dict[int, tuple[asyncio.Future | None, str, str]] = {}
        self._targets: dict[str, _Target] = {}
        self._setups: set[asyncio.Task] = set()
        self._guarded: set[str] = set()
        self._chain: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._starting = False
        self._closing = False
        self.lost: str | None = None
        """Why the guard stopped vouching for the browser; None while it does."""
        self.browser_pid: int | None = None
        self.answer_errors = 0
        """Verdicts Chrome refused to apply (a request that was cancelled meanwhile)."""
        self._events: dict[str, Callable[[str, dict], None]] = {
            "Fetch.requestPaused": self._on_paused,
            "Target.attachedToTarget": self._on_attached,
            "Target.detachedFromTarget": self._on_detached,
            "Page.frameAttached": self._frames_event("attached"),
            "Page.frameNavigated": self._frames_event("navigated"),
            "Page.navigatedWithinDocument": self._frames_event("within_document"),
            "Page.frameDetached": self._frames_event("detached"),
        }

    # -- lifecycle ----------------------------------------------------------------

    async def start(self, profile_dir: Path, *, timeout: float = SETUP_TIMEOUT_S) -> None:
        """Connect, guard every existing tab, and return only when new ones will be too.

        Raises ``CdpGuardError``. The browser is left as it is: it is the caller's to
        close, because an unguarded window must not stay open.
        """
        self._starting = True
        try:
            port, path = await read_endpoint(profile_dir, timeout)
            try:
                self._ws = await WebSocket.connect(_LOOPBACK, port, path, timeout=CONNECT_TIMEOUT_S)
                self._ws.shrink = shrink_oversize
            except WebSocketError as exc:
                raise CdpGuardError(str(exc)) from None
            self._reader = asyncio.create_task(self._read_loop())
            await asyncio.wait_for(self._attach_everything(), timeout)
        except TimeoutError:
            await self.stop()
            raise CdpGuardError("setup timed out") from None
        except CdpGuardError:
            await self.stop()
            raise
        except BaseException:
            await self.stop()
            raise
        finally:
            self._starting = False
        if self.lost:
            await self.stop()
            raise CdpGuardError(self.lost)

    async def _attach_everything(self) -> None:
        await self._call("Target.setAutoAttach", _PAGE_ATTACH)
        # Tabs that exist now are attached by events that follow that reply; the first
        # navigation must not start before they are guarded.
        while True:
            if self.lost:
                raise CdpGuardError(self.lost)
            reply = await self._call(
                "Target.getTargets", {"filter": [{"type": "page"}, {"exclude": True}]}
            )
            existing = {t["targetId"] for t in reply.get("targetInfos", [])}
            if existing <= self._guarded:
                break
            await asyncio.sleep(0.02)
        self.browser_pid = await self._find_browser_pid()

    async def _find_browser_pid(self) -> int | None:
        try:
            reply = await self._call("SystemInfo.getProcessInfo")
        except _CallError:
            return None
        for proc in reply.get("processInfo", []):
            if proc.get("type") == "browser" and isinstance(proc.get("id"), int):
                return proc["id"]
        return None

    def begin_close(self) -> None:
        """The browser is about to be closed on purpose: its socket ending is not a loss."""
        self._closing = True

    async def stop(self) -> None:
        self._closing = True
        for task in list(self._setups):
            task.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(self._reader, 2.0)

    def has_pages(self) -> bool:
        """Whether a page target is still attached: the sidecar's own, ordered view of the tabs.

        A browser that quits detaches every page over this connection before the socket
        ends, so ``False`` at the moment the socket ends means it was closing; a guard that
        failed with tabs open still has them. Playwright's view cannot tell the two apart
        in time: its pages close a moment after this socket does, and under load that
        moment is long enough to read a user closing the last window as a failure.
        """
        return any(target.kind == "page" for target in self._targets.values())

    def abort_connection(self) -> None:
        """Drop the socket without a goodbye: a connection lost, as far as the guard can tell."""
        if self._ws is not None:
            self._ws.abort()

    async def browser_exited(self, timeout: float) -> bool:
        """Whether the browser process is gone within ``timeout`` (unknown pid: no)."""
        pid = self.browser_pid
        if pid is None:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while _alive(pid):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    def kill_browser(self) -> bool:
        """SIGKILL the browser process. False when its pid is unknown or it is already gone."""
        pid = self.browser_pid
        if pid is None or not _alive(pid):
            return False
        with contextlib.suppress(OSError):
            os.kill(pid, _KILL)
            return True
        return False

    # -- the connection -----------------------------------------------------------

    def _send(self, method: str, params: dict | None = None, session: str = "") -> asyncio.Future:
        """Queue a command now. The future is its result, or ``_CallError`` if Chrome refused it."""
        ws = self._ws
        if ws is None:
            raise CdpGuardError("not connected")
        if self._reader is not None and self._reader.done():
            # Nobody is reading any more, so no answer can come: the peer's end of the
            # socket does not make the writer refuse, and the command would wait forever.
            raise CdpGuardError("connection lost")
        self._next_id += 1
        mid = self._next_id
        message: dict[str, Any] = {"id": mid, "method": method}
        if params:
            message["params"] = params
        if session:
            message["sessionId"] = session
        try:
            ws.send_nowait(json.dumps(message, separators=(",", ":")))
        except WebSocketError:
            raise CdpGuardError("connection lost") from None
        fut = asyncio.get_running_loop().create_future()
        fut.add_done_callback(_retrieve)
        self._pending[mid] = (fut, method, session)
        return fut

    async def _call(self, method: str, params: dict | None = None, session: str = "") -> dict:
        return await self._send(method, params, session)

    def _fire(self, session: str, method: str, params: dict) -> None:
        """Send a command and do not wait for it: the verdicts, which must not queue."""
        assert self._ws is not None
        self._next_id += 1
        self._pending[self._next_id] = (None, method, session)
        message = {"id": self._next_id, "method": method, "params": params, "sessionId": session}
        self._ws.send_nowait(json.dumps(message, separators=(",", ":")))

    async def _read_loop(self) -> None:
        assert self._ws is not None
        reason = "connection closed"
        try:
            while True:
                self._dispatch(await self._ws.recv())
        except WebSocketError as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001 — whatever it was, the guard can no longer vouch
            reason = f"reader failed: {type(exc).__name__}"
        finally:
            for fut, _method, _session in self._pending.values():
                if fut is not None and not fut.done():
                    fut.set_exception(CdpGuardError("connection lost"))
            self._pending.clear()
        self._lose(reason)

    def _lose(self, reason: str) -> None:
        """Record that the guard is gone and tell the owner, once."""
        if self._closing or self.lost is not None:
            return
        self.lost = reason
        if not self._starting:
            with contextlib.suppress(Exception):
                self._on_lost(reason)

    def _fail(self, reason: str) -> None:
        """Give the guard up while the connection is still alive: a target it cannot guard.

        The connection is deliberately left open. Chrome releases every request it holds
        the moment it closes, so closing first would unguard the browser a moment before
        it is killed; open, whatever is paused stays paused until the owner has closed
        the window.
        """
        self._lose(reason)

    def _dispatch(self, raw: str) -> None:
        # Network.* is enabled for the service-worker bypass and is by far the loudest
        # thing on the wire; Chrome puts "method" first, and anything else still parses.
        if raw.startswith('{"method":"Network.'):
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            raise WebSocketError("message is not json") from None
        if not isinstance(msg, dict):
            raise WebSocketError("message is not an object")
        mid = msg.get("id")
        if mid is not None:
            entry = self._pending.pop(mid, None)
            if entry is None:
                return
            fut, method, _session = entry
            error = msg.get("error")
            if fut is None:
                if error:
                    self.answer_errors += 1
            elif not fut.done():
                if error:
                    detail = error if isinstance(error, dict) else {}
                    fut.set_exception(
                        _CallError(method, detail.get("code"), str(detail.get("message", "")))
                    )
                else:
                    fut.set_result(msg.get("result", {}))
            return
        handler = self._events.get(msg.get("method", ""))
        if handler is None:
            return
        params = msg.get("params")
        handler(msg.get("sessionId", ""), params if isinstance(params, dict) else {})

    # -- requests -----------------------------------------------------------------

    def _on_paused(self, session: str, params: dict) -> None:
        """Judge one paused document request and answer it before reading anything else.

        Synchronous end to end: ``decide`` does not wait, and the answer is queued on the
        socket. A request this cannot judge is answered as the guard answers its own
        failures: refused when enforcing, let through when only observing.
        """
        request_id = params.get("requestId")
        target = self._targets.get(session)
        if not isinstance(request_id, str) or target is None:
            return  # a target that has gone: Chrome resumed what it was holding
        try:
            view = PausedRequest(
                params, target.frames.frames, self._redirect_source(session, params)
            )
            allow = self._judge.decide(view)
        except Exception as exc:  # noqa: BLE001 — a broken guard must not open the gate
            allow = self._judge.verdict_on_error(exc)
        if allow:
            self._fire(session, "Fetch.continueRequest", {"requestId": request_id})
        else:
            # 204, never an abort: an abort sends the tab to chrome-error:// and the DOM
            # the agent was working with is gone. The page stays where it was.
            self._fire(
                session, "Fetch.fulfillRequest", {"requestId": request_id, "responseCode": 204}
            )

    def _redirect_source(self, session: str, params: dict) -> str:
        """The URL of the request whose redirect produced this one, "" for a first request."""
        source = ""
        previous = params.get("redirectedRequestId")
        if previous:
            source = self._chain.pop((session, previous), "")
        self._chain[(session, params["requestId"])] = params["request"]["url"]
        while len(self._chain) > _CHAIN_MAX:
            self._chain.popitem(last=False)
        return source

    # -- targets ------------------------------------------------------------------

    def _frames_event(self, name: str) -> Callable[[str, dict], None]:
        def handle(session: str, params: dict) -> None:
            target = self._targets.get(session)
            if target is not None:
                getattr(target.frames, name)(params)

        return handle

    def _on_attached(self, _parent: str, params: dict) -> None:
        session, info = params["sessionId"], params["targetInfo"]
        waiting = bool(params.get("waitingForDebugger"))
        kind = info.get("type", "")
        if kind not in _GUARDED_TYPES:
            # Outside the filter, so not expected. Whatever it is must not be left held
            # for a debugger that will never come, nor kept attached.
            if waiting:
                self._fire(session, "Runtime.runIfWaitingForDebugger", {})
            self._fire("", "Target.detachFromTarget", {"sessionId": session})
            return
        target = _Target(session, kind, info.get("targetId", ""))
        # Registered here, not in the task: events for the session may follow before the
        # task first runs.
        self._targets[session] = target
        task = asyncio.ensure_future(self._setup(target, waiting))
        self._setups.add(task)
        task.add_done_callback(self._setups.discard)

    def _on_detached(self, _parent: str, params: dict) -> None:
        session = params.get("sessionId", "")
        target = self._targets.pop(session, None)
        if target is not None:
            self._guarded.discard(target.target_id)
        for key in [k for k in self._chain if k[0] == session]:
            del self._chain[key]
        # A target that is gone answers nothing: what was asked of it fails now, as
        # "gone", instead of waiting for a reply that cannot come.
        for mid, (fut, method, owner) in list(self._pending.items()):
            if owner == session and fut is not None:
                del self._pending[mid]
                if not fut.done():
                    fut.set_exception(_CallError(method, -32001, "target detached"))

    async def _setup(self, target: _Target, waiting: bool) -> None:
        """Put the guard on a target before it can run, then let it run.

        ``Fetch.enable`` is the one command that must be applied before the target runs,
        and it is answered by the browser process. The rest are queued behind it and
        answered whenever Chrome can: for a popup with no renderer yet (a link's
        ``target=_blank`` opens its own process) they are renderer-bound, and their
        replies only come after the resume that is ours to give — awaiting them first
        would wait for ourselves. Chrome applies commands in the order it receives them,
        so the service-worker bypass is in force before the page's first request
        (measured: a ``target=_blank`` link to an origin with a worker is still judged).
        """
        session = target.session
        try:
            await self._call("Fetch.enable", _FETCH_DOCUMENTS, session)
            queued = [
                self._send("Network.enable", _NETWORK_QUIET, session),
                self._send("Network.setBypassServiceWorker", {"bypass": True}, session),
                self._send("Target.setAutoAttach", _IFRAME_ATTACH, session),
            ]
            if waiting:
                await self._call("Runtime.runIfWaitingForDebugger", None, session)
            for fut in queued:
                await fut
        except _CallError as exc:
            # A target that went away while being set up needs no guard. Any other
            # failure leaves one that is running without it, and nothing may do that.
            if not _gone(exc) and session in self._targets:
                self._fail("a target could not be guarded")
            return
        except CdpGuardError:
            return  # the connection is gone; the reader reports it
        try:
            await self._call("Page.enable", None, session)
            reply = await self._call("Page.getFrameTree", None, session)
            target.frames.load(reply["frameTree"])
        except (CdpGuardError, KeyError, TypeError):
            # Requests are judged without frames until events fill them in: an unknown
            # frame is an opaque initiator, which is the stricter reading.
            pass
        self._guarded.add(target.target_id)
