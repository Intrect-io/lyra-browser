"""The CDP backend of the navigation guard, driven by a scripted Chrome.

``CdpGuard`` is a second DevTools connection: Chrome pauses every document request (and every
redirect hop) and waits for one answer. These tests replace the socket with a ``FakeChrome``
that answers each command, can hold a reply back, and pushes the events a real browser would
(``Target.attachedToTarget``, ``Fetch.requestPaused``, ``Page.frame*``). The judge behind the
guard is the real ``NavigationGuard`` with a real ``PermissionStore`` and ``AuditLog``, so the
verdicts and the audit rows asserted here are the ones a user would get.

What is pinned is what a person relying on the guard can see: which requests are refused, that
each is answered once and at once, that no target runs before it is guarded, and that any loss
of the connection (or of a target) closes the browser instead of leaving it unwatched.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lyra_browser import cdp_guard
from lyra_browser.approval import CollaborationState
from lyra_browser.audit import AuditLog
from lyra_browser.cdp_guard import (
    CdpGuard,
    CdpGuardError,
    FrameCache,
    PausedFrame,
    PausedRequest,
    prepare_profile,
    read_endpoint,
    shrink_oversize,
)
from lyra_browser.cdp_socket import WebSocketError
from lyra_browser.config import Config
from lyra_browser.enforcement import NavigationGuard, classify
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability, PermissionStore

PORT = 12345
UUID = "0b0b0b0b-1111-2222-3333-444444444444"
PATH = f"/devtools/browser/{UUID}"
ENDPOINT_FILE = f"{PORT}\n{PATH}"
A = parse_origin("https://a.example/")
# What the transport keeps of a message it will not hold whole: its first and last bytes.
HEAD, TAIL = 256 * 1024, 64 * 1024
MIB = 1024 * 1024

# Every command a target gets before and after it runs, in the order they are sent.
PAGE_SETUP = [
    "Fetch.enable",
    "Network.enable",
    "Network.setBypassServiceWorker",
    "Target.setAutoAttach",
    "Runtime.runIfWaitingForDebugger",
    "Page.enable",
    "Page.getFrameTree",
]


def dumps(message: dict) -> str:
    """JSON the way Chrome writes it: compact, UTF-8, keys in the order given."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


# --------------------------------------------------------------------------
# A scripted Chrome behind the transport
# --------------------------------------------------------------------------


class Ended:
    """The connection ends: what ``recv`` raises."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


class Oversize:
    """A message the transport would not keep whole: the hook is handed its two ends."""

    def __init__(self, raw: str) -> None:
        self.raw = raw.encode()


@dataclass
class Command:
    id: int
    method: str
    params: dict
    session: str
    during: str | None
    """The message the reader was handling when this was sent; None between messages."""


class FakeWebSocket:
    """What ``CdpGuard`` sees of the transport."""

    def __init__(self, chrome: FakeChrome) -> None:
        self.chrome = chrome
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closing = False
        self.closed = False
        self.aborted = False
        self.shrink: Callable[[bytes, bytes], str | None] | None = None
        self.handling: str | None = None

    def send_nowait(self, text: str) -> None:
        if self.closing:
            raise WebSocketError("connection closed")
        self.chrome.receive(json.loads(text), self.handling)

    async def recv(self) -> str:
        self.handling = None  # back for more: whatever it was handling is finished
        item = await self.inbox.get()
        self.chrome.activity += 1
        if isinstance(item, Ended):
            raise WebSocketError(item.reason)
        if isinstance(item, Oversize):
            if self.shrink is None:
                raise WebSocketError("message too large")
            item = self.shrink(item.raw[:HEAD], item.raw[-TAIL:])
            if item is None:
                raise WebSocketError("message too large")
        self.handling = item
        return item

    async def close(self) -> None:
        self.closing = self.closed = True
        self.inbox.put_nowait(Ended("connection closed"))

    def abort(self) -> None:
        self.closing = self.aborted = True
        self.inbox.put_nowait(Ended("connection closed"))


class Hold:
    """Withholds the replies to some commands, until released or until another one arrives."""

    def __init__(
        self, chrome: FakeChrome, methods: tuple[str, ...], session: str | None, until: str | None
    ) -> None:
        self.chrome = chrome
        self.methods = methods
        self.session = session
        self.until = until
        self.open = True
        self.replies: list[dict] = []

    def matches(self, cmd: Command) -> bool:
        return self.open and cmd.method in self.methods and self.session in (None, cmd.session)

    def triggered_by(self, cmd: Command) -> bool:
        return self.open and cmd.method == self.until and self.session in (None, cmd.session)

    def release(self) -> None:
        self.open = False
        for reply in self.replies:
            self.chrome.deliver(reply)
        self.replies.clear()


class FakeChrome:
    """A browser that answers every command and says what it was asked, and in what order."""

    def __init__(self) -> None:
        self.ws = FakeWebSocket(self)
        self.commands: list[Command] = []
        self.pages: list[tuple[str, str]] = []
        """(target id, session) of the tabs open when the guard starts."""
        self.listed_only: list[str] = []
        """Targets ``Target.getTargets`` lists that never attach."""
        self.results: dict[str, Any] = {}
        self.errors: dict[tuple[str, str], dict] = {}
        """(session, method) -> the error reply; session ``"*"`` means any."""
        self.trees: dict[str, dict] = {}
        self.holds: list[Hold] = []
        self.on_command: Callable[[Command], None] | None = None
        self.activity = 0

    # -- what the guard sends -----------------------------------------------------

    def receive(self, message: dict, during: str | None) -> None:
        cmd = Command(
            message["id"],
            message["method"],
            message.get("params", {}),
            message.get("sessionId", ""),
            during,
        )
        self.commands.append(cmd)
        self.activity += 1
        reply = self._answer(cmd)
        held = next((h for h in self.holds if h.matches(cmd)), None)
        if held is not None:
            held.replies.append(reply)
        else:
            self.deliver(reply)
        for hold in self.holds:
            if hold.triggered_by(cmd):
                hold.release()
        if cmd.method == "Target.setAutoAttach" and not cmd.session:
            for target_id, session in self.pages:  # tabs that exist are attached after the reply
                self.attach(session, "page", target_id, waiting=False)
        if self.on_command is not None:
            self.on_command(cmd)

    def _answer(self, cmd: Command) -> dict:
        error = self.errors.get((cmd.session, cmd.method)) or self.errors.get(("*", cmd.method))
        if error is not None:
            return {"id": cmd.id, "error": error}
        result = self.results.get(cmd.method)
        if callable(result):
            result = result(cmd)
        if result is None:
            result = self._default(cmd)
        return {"id": cmd.id, "result": result}

    def _default(self, cmd: Command) -> dict:
        if cmd.method == "Target.getTargets":
            infos = [{"targetId": t, "type": "page", "attached": True} for t, _ in self.pages]
            infos += [{"targetId": t, "type": "page", "attached": False} for t in self.listed_only]
            return {"targetInfos": infos}
        if cmd.method == "SystemInfo.getProcessInfo":
            return {"processInfo": [{"type": "gpu", "id": 11}, {"type": "browser", "id": 4242}]}
        if cmd.method == "Page.getFrameTree":
            root = {"frame": {"id": f"root-{cmd.session}", "url": "about:blank"}}
            return {"frameTree": self.trees.get(cmd.session) or root}
        return {}

    def sent(self, session: str | None = None, method: str | None = None) -> list[Command]:
        return [
            c
            for c in self.commands
            if (session is None or c.session == session) and (method is None or c.method == method)
        ]

    def methods(self, session: str) -> list[str]:
        return [c.method for c in self.commands if c.session == session]

    # -- what Chrome sends --------------------------------------------------------

    def deliver(self, message: dict | str | Ended | Oversize) -> None:
        if isinstance(message, dict):
            message = dumps(message)
        self.activity += 1
        self.ws.inbox.put_nowait(message)

    def event(self, method: str, params: dict | None = None, session: str = "") -> str:
        message: dict[str, Any] = {"method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        raw = dumps(message)
        self.deliver(raw)
        return raw

    def attach(
        self,
        session: str,
        kind: str = "page",
        target_id: str | None = None,
        *,
        waiting: bool = True,
        parent: str = "",
    ) -> None:
        info = {"targetId": target_id or f"T-{session}", "type": kind, "url": "about:blank"}
        params = {"sessionId": session, "targetInfo": info, "waitingForDebugger": waiting}
        self.event("Target.attachedToTarget", params, session=parent)

    def detach(self, session: str) -> None:
        self.pages = [(t, s) for t, s in self.pages if s != session]
        self.event("Target.detachedFromTarget", {"sessionId": session})

    def end(self, reason: str = "connection closed") -> None:
        self.deliver(Ended(reason))

    def paused(
        self,
        session: str,
        request_id: str,
        url: str,
        *,
        method: str = "GET",
        frame: str = "F1",
        body: str | None = None,
        request: dict | None = None,
        redirected_from: str | None = None,
        resource: str = "Document",
    ) -> str:
        """``Fetch.requestPaused`` as Chrome sends it; returns the raw message."""
        req: dict[str, Any] = {"url": url, "method": method, "headers": {"Accept": "text/html"}}
        if body is not None:
            req.update(postData=body, hasPostData=True)
        req.update(request or {})
        params: dict[str, Any] = {
            "requestId": request_id,
            "request": req,
            "frameId": frame,
            "resourceType": resource,
            "networkId": request_id,
        }
        if redirected_from is not None:
            params["redirectedRequestId"] = redirected_from
        return self.event("Fetch.requestPaused", params, session)

    def frame(self, session: str, frame_id: str, url: str, parent: str | None = None) -> None:
        """The events that put a frame, at a url, in a target's tree."""
        if parent is not None:
            self.event(
                "Page.frameAttached", {"frameId": frame_id, "parentFrameId": parent}, session
            )
        frame = {"id": frame_id, "url": url, **({"parentId": parent} if parent else {})}
        self.event("Page.frameNavigated", {"frame": frame}, session)

    def hold(self, *methods: str, session: str | None = None, until: str | None = None) -> Hold:
        hold = Hold(self, methods, session, until)
        self.holds.append(hold)
        return hold

    async def settle(self, quiet: int = 8) -> None:
        """Run the loop until the guard has nothing left to do that does not wait for a timer."""
        idle, seen = 0, -1
        while idle < quiet:
            await asyncio.sleep(0)
            idle, seen = (idle + 1, seen) if self.activity == seen else (0, self.activity)


# --------------------------------------------------------------------------
# The guard under test, with the real judge
# --------------------------------------------------------------------------


class SpyJudge:
    """The real ``NavigationGuard``, remembering what ``CdpGuard`` handed it."""

    def __init__(self, real: NavigationGuard) -> None:
        self.real = real
        self.seen: list[Any] = []

    def decide(self, request: Any) -> bool:
        self.seen.append(request)
        return self.real.decide(request)

    def verdict_on_error(self, exc: Exception) -> bool:
        return self.real.verdict_on_error(exc)


class Rig:
    """One ``CdpGuard`` on a ``FakeChrome``, with real grants and a real audit trail."""

    def __init__(self, base: Path, monkeypatch, mode: str, fake_transport: bool) -> None:
        self.profile = base / "profile"
        self.profile.mkdir(parents=True)
        self.cfg = Config(data_dir=base / "data", enforcement_mode=mode, headless=True)
        self.perms = PermissionStore()
        self.collab = CollaborationState()
        self.navigation = NavigationGuard(
            self.cfg, self.perms, AuditLog(self.cfg.audit_path), mode=mode, collab=self.collab
        )
        self.judge = SpyJudge(self.navigation)
        self.chrome = FakeChrome()
        self.lost: list[str] = []
        self.pages_at_loss: list[bool] = []
        """``has_pages()`` as the owner reads it from inside ``on_lost``, once per call."""
        self.guard = CdpGuard(self.judge, on_lost=self._on_lost)
        self.connects: list[tuple] = []
        self.endpoint_file_at_connect: list[bool] = []
        if fake_transport:
            monkeypatch.setattr(cdp_guard, "WebSocket", self._transport())

    def _on_lost(self, reason: str) -> None:
        self.lost.append(reason)
        self.pages_at_loss.append(self.guard.has_pages())

    def _transport(self) -> type:
        rig = self

        class FakeTransport:
            @staticmethod
            async def connect(host: str, port: int, path: str, *, timeout: float):
                rig.connects.append((host, port, path, timeout))
                rig.endpoint_file_at_connect.append((rig.profile / "DevToolsActivePort").exists())
                return rig.chrome.ws

        return FakeTransport

    def write_endpoint(self, content: str | bytes = ENDPOINT_FILE) -> Path:
        file = self.profile / "DevToolsActivePort"
        file.write_bytes(content if isinstance(content, bytes) else content.encode())
        return file

    async def start(self, pages: tuple = (("T1", "S1"),), timeout: float = 5.0) -> None:
        self.chrome.pages = list(pages)
        self.write_endpoint()
        await asyncio.wait_for(self.guard.start(self.profile), timeout)

    def grant(self, capability: Capability, origin=A, initiator=None) -> None:
        self.perms.grant("default", origin, capability, initiator=initiator)

    def audit(self) -> list[dict]:
        """The audit rows so far, without their timestamps."""
        if not self.cfg.audit_path.exists():
            return []
        rows = [json.loads(x) for x in self.cfg.audit_path.read_text().splitlines() if x.strip()]
        for row in rows:
            row.pop("ts")
        return rows

    def answers(self, request_id: str | None = None, session: str | None = None) -> list[Command]:
        """The verdicts sent: every ``Fetch`` command that is not ``Fetch.enable``."""
        return [
            c
            for c in self.chrome.commands
            if c.method.startswith("Fetch.")
            and c.method != "Fetch.enable"
            and (request_id is None or c.params.get("requestId") == request_id)
            and (session is None or c.session == session)
        ]

    def verdict(self, request_id: str, session: str | None = None) -> str:
        """``continue`` or ``refuse`` (a 204), after checking it was answered exactly once."""
        [cmd] = self.answers(request_id, session)
        if cmd.method == "Fetch.continueRequest":
            assert cmd.params == {"requestId": request_id}
            return "continue"
        assert cmd.method == "Fetch.fulfillRequest", cmd.method
        assert cmd.params == {"requestId": request_id, "responseCode": 204}
        return "refuse"


@pytest.fixture
async def make_rig(tmp_path, monkeypatch):
    rigs: list[Rig] = []

    def make(mode: str = "enforce", *, fake_transport: bool = True) -> Rig:
        rig = Rig(tmp_path / f"rig{len(rigs)}", monkeypatch, mode, fake_transport)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        await rig.guard.stop()


@pytest.fixture
async def rig(make_rig) -> Rig:
    """An enforcing guard, started, with one tab (target T1, session S1) already guarded."""
    started = make_rig()
    await started.start()
    return started


@pytest.fixture
async def page_rig(rig) -> Rig:
    """``rig`` with the tab showing https://a.example/ in frame F1 and NAVIGATE granted there."""
    rig.chrome.frame("S1", "F1", "https://a.example/")
    rig.grant(Capability.NAVIGATE)
    await rig.chrome.settle()
    return rig


def printed_chain(exc: BaseException | None):
    """The exceptions a traceback shows: the cause, or the context unless it is suppressed."""
    while exc is not None:
        yield exc
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)


def assert_no_endpoint(exc: BaseException) -> None:
    """Nothing a log of this exception would print names where Chrome is listening."""
    for shown in printed_chain(exc):
        text = f"{shown} {shown!r} {''.join(traceback.format_exception_only(shown))}"
        for secret in (str(PORT), PATH, UUID):
            assert secret not in text


def new_tasks(before: set[asyncio.Task]) -> set[asyncio.Task]:
    """The tasks that were started since ``before`` and have not finished."""
    return asyncio.all_tasks() - before


# --------------------------------------------------------------------------
# Verdicts: one answer per paused request, at once
# --------------------------------------------------------------------------


async def test_an_allowed_request_is_continued_once_and_nothing_else_is_sent(page_rig):
    page_rig.chrome.paused("S1", "r1", "https://a.example/next")
    await page_rig.chrome.settle()

    [answer] = page_rig.answers("r1")
    assert (answer.session, answer.method, answer.params) == (
        "S1",
        "Fetch.continueRequest",
        {"requestId": "r1"},
    )
    assert page_rig.navigation.refusals == 0


async def test_a_refused_request_is_answered_with_a_204_and_never_failed_or_aborted(page_rig):
    page_rig.chrome.paused("S1", "r1", "https://b.example/elsewhere")
    await page_rig.chrome.settle()

    [answer] = page_rig.answers("r1")
    assert (answer.session, answer.method, answer.params) == (
        "S1",
        "Fetch.fulfillRequest",
        {"requestId": "r1", "responseCode": 204},
    )
    # A failed request sends the tab to chrome-error:// and the page the agent works on is gone.
    assert not [c for c in page_rig.chrome.commands if "fail" in c.method.lower()]
    assert not [c for c in page_rig.chrome.commands if "abort" in c.method.lower()]
    assert page_rig.navigation.refusals == 1
    assert page_rig.audit()[-1]["status"] == "denied"


async def test_each_paused_request_is_answered_once_inside_the_handling_of_its_own_event(page_rig):
    raw = {
        "r1": page_rig.chrome.paused("S1", "r1", "https://a.example/next"),
        "r2": page_rig.chrome.paused("S1", "r2", "https://b.example/elsewhere"),
        "r3": page_rig.chrome.paused("S1", "r3", "https://a.example/app.js", resource="Script"),
        "r4": page_rig.chrome.paused("S1", "r4", "https://a.example/s", method="POST", body="x=1"),
    }
    await page_rig.chrome.settle()

    for request_id, event in raw.items():
        [answer] = page_rig.answers(request_id)
        # Still inside the event's own handling: the reader had not asked for the next message.
        assert answer.during == event
    assert [page_rig.verdict(r) for r in raw] == ["continue", "refuse", "continue", "refuse"]


async def test_observe_mode_lets_a_refusable_request_through_and_says_it_would_have_refused(
    make_rig,
):
    rig = make_rig("observe")
    await rig.start()
    rig.chrome.frame("S1", "F1", "https://a.example/")

    rig.chrome.paused("S1", "r1", "https://b.example/elsewhere")
    await rig.chrome.settle()

    assert rig.verdict("r1") == "continue"
    assert [r["status"] for r in rig.audit()] == ["would_deny"]
    assert rig.navigation.refusals == 0


async def test_a_takeover_lets_the_users_own_navigation_through_and_records_who_drove(page_rig):
    page_rig.collab.takeover = True

    page_rig.chrome.paused("S1", "r1", "https://b.example/elsewhere")
    await page_rig.chrome.settle()

    assert page_rig.verdict("r1") == "continue"
    assert [r["status"] for r in page_rig.audit()] == ["user_driven"]
    assert page_rig.navigation.refusals == 0


GARBLED = {
    "no url": {"method": "GET"},
    "no method": {"url": "https://a.example/x"},
    "no request at all": None,
    "request is null": "null",
}


@pytest.mark.parametrize(
    ("mode", "verdict"),
    [("enforce", "refuse"), ("observe", "continue")],
    ids=["enforce", "observe"],
)
@pytest.mark.parametrize("shape", GARBLED.values(), ids=GARBLED.keys())
async def test_a_request_the_guard_cannot_read_is_answered_as_its_own_failures_are(
    make_rig, mode, verdict, shape
):
    rig = make_rig(mode)
    await rig.start()
    params: dict[str, Any] = {"requestId": "r1", "frameId": "F1", "resourceType": "Document"}
    if shape is not None:
        params["request"] = None if shape == "null" else shape

    rig.chrome.event("Fetch.requestPaused", params, session="S1")
    await rig.chrome.settle()

    assert rig.verdict("r1") == verdict
    assert [r["status"] for r in rig.audit()] == ["guard_error"]
    assert rig.navigation.refusals == 0, "a broken guard is not a policy refusal"
    assert rig.lost == [], "one unreadable request does not end the guard"


IGNORED = {
    "session that is not attached": ("r1", "S-unknown"),
    "no request id": (None, "S1"),
    "request id that is not a string": (7, "S1"),
}


@pytest.mark.parametrize("ident", IGNORED.values(), ids=IGNORED.keys())
async def test_a_paused_event_that_cannot_be_answered_is_ignored(page_rig, ident):
    request_id, session = ident
    params = {"request": {"url": "https://b.example/x", "method": "GET"}, "frameId": "F1"}
    page_rig.chrome.event(
        "Fetch.requestPaused",
        {
            **params,
            "resourceType": "Document",
            **({} if request_id is None else {"requestId": request_id}),
        },
        session=session,
    )
    await page_rig.chrome.settle()

    assert page_rig.answers() == []
    assert page_rig.audit() == []
    assert page_rig.lost == []


async def test_a_paused_request_on_a_target_that_has_detached_is_ignored(page_rig):
    page_rig.chrome.detach("S1")
    page_rig.chrome.paused("S1", "r1", "https://b.example/elsewhere")
    await page_rig.chrome.settle()

    assert page_rig.answers() == []
    assert page_rig.audit() == []


async def test_a_verdict_chrome_could_not_apply_is_counted_and_does_not_end_the_guard(page_rig):
    # The request was cancelled by the page while the answer was on its way.
    page_rig.chrome.errors[("S1", "Fetch.continueRequest")] = {
        "code": -32602,
        "message": "Invalid InterceptionId.",
    }

    page_rig.chrome.paused("S1", "r1", "https://a.example/gone")
    page_rig.chrome.paused("S1", "r2", "https://b.example/elsewhere")
    await page_rig.chrome.settle()

    assert page_rig.guard.answer_errors == 1
    assert page_rig.lost == []
    assert page_rig.verdict("r2") == "refuse", "the next request is still judged"


# --------------------------------------------------------------------------
# A request body, however Chrome reports it
# --------------------------------------------------------------------------

BODIES = {
    "inline text": {"postData": "a=1", "hasPostData": True},
    "text alone": {"postData": "a=1"},
    "flag without the data (too large or binary)": {"hasPostData": True},
    "entries only": {"postDataEntries": [{"bytes": "YT0x"}]},
}
NO_BODY = {
    "nothing": {},
    "flag off": {"hasPostData": False},
    "empty text": {"postData": "", "hasPostData": False},
    "no entries": {"postDataEntries": []},
}


@pytest.mark.parametrize("fields", BODIES.values(), ids=BODIES.keys())
async def test_any_way_chrome_reports_a_body_makes_a_get_a_submission(page_rig, fields):
    # A GET, so that only the body can make it a submission; NAVIGATE on the site covers a
    # plain move, so only a missing SUBMIT grant can refuse it.
    page_rig.chrome.paused("S1", "r1", "https://a.example/s", method="GET", request=fields)
    await page_rig.chrome.settle()

    assert classify(page_rig.judge.seen[-1]).capability is Capability.SUBMIT
    assert page_rig.verdict("r1") == "refuse"
    assert page_rig.audit()[-1]["args"]["capability"] == "submit"


@pytest.mark.parametrize("fields", NO_BODY.values(), ids=NO_BODY.keys())
async def test_a_get_without_a_body_is_an_ordinary_move_within_the_site(page_rig, fields):
    page_rig.chrome.paused("S1", "r1", "https://a.example/s", method="GET", request=fields)
    await page_rig.chrome.settle()

    assert classify(page_rig.judge.seen[-1]).capability is Capability.INTERACT
    assert page_rig.verdict("r1") == "continue"


async def test_a_submission_is_recorded_without_what_was_submitted(page_rig):
    page_rig.chrome.paused(
        "S1", "r1", "https://a.example/login", method="POST", body="user=bob&password=hunter2"
    )
    await page_rig.chrome.settle()

    assert page_rig.verdict("r1") == "refuse"
    assert "hunter2" not in page_rig.cfg.audit_path.read_text()


# --------------------------------------------------------------------------
# Parity: a paused request is judged exactly as Playwright's route would judge it
# --------------------------------------------------------------------------


class FakeFrame:
    def __init__(self, url: str, parent: FakeFrame | None = None) -> None:
        self.url = url
        self.parent_frame = parent


class FakeRequest:
    """What Playwright hands the route handler."""

    def __init__(self, url, method, post_data, navigation, frame) -> None:
        self.url, self.method, self.post_data = url, method, post_data
        self._navigation, self._frame = navigation, frame

    def is_navigation_request(self) -> bool:
        return self._navigation

    @property
    def frame(self) -> FakeFrame:
        if self._frame is None:
            raise RuntimeError("Frame not available for this request")  # as Playwright does
        return self._frame


class FakeRoute:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def continue_(self) -> None:
        self.calls.append(("continue",))

    async def fulfill(self, **kwargs) -> None:
        self.calls.append(("fulfill", kwargs.get("status")))


@dataclass(frozen=True)
class Case:
    name: str
    url: str
    method: str
    body: str | None
    navigation: bool
    frame_url: str
    parent_url: str | None
    kind: str
    """``page``: a frame of a page target. ``oopif``: the root of an iframe target (its parent
    is in another process). ``unknown``: a frame the target's tree does not have."""
    expect: str
    """What NAVIGATE on https://a.example/ alone decides: ``continue`` or ``refuse``."""


# Copied from scripts/probe_document_intercept.py (_PARITY_CASES), each with its expected verdict.
PARITY_CASES = [
    Case("same-origin link", "https://a.example/x", "GET", None, True,
         "https://a.example/", None, "page", "continue"),
    Case("cross-origin link", "https://b.example/x", "GET", None, True,
         "https://a.example/", None, "page", "refuse"),
    Case("from about:blank", "https://a.example/x", "GET", None, True,
         "about:blank", None, "page", "continue"),
    Case("frame unknown", "https://a.example/x", "GET", None, True,
         "", None, "unknown", "continue"),
    Case("form post same origin", "https://a.example/s", "POST", "a=1", True,
         "https://a.example/", None, "page", "refuse"),
    Case("form post cross origin", "https://b.example/s", "POST", "a=1", True,
         "https://a.example/", None, "page", "refuse"),
    Case("get with a body", "https://a.example/s", "GET", "a=1", True,
         "https://a.example/", None, "page", "refuse"),
    Case("put", "https://a.example/s", "PUT", None, True,
         "https://a.example/", None, "page", "refuse"),
    Case("subresource", "https://a.example/app.js", "GET", None, False,
         "https://a.example/", None, "page", "continue"),
    Case("iframe get", "https://c.example/f", "GET", None, True,
         "https://a.example/", "https://a.example/", "page", "continue"),
    Case("iframe post", "https://a.example/s", "POST", "a=1", True,
         "https://c.example/f", "https://a.example/", "page", "refuse"),
    Case("blank iframe post", "https://a.example/s", "POST", "a=1", True,
         "about:blank", "https://a.example/", "page", "refuse"),
    Case("popup first post", "https://b.example/s", "POST", "a=1", True,
         "about:blank", None, "page", "refuse"),
    Case("oopif get", "https://c.example/f2", "GET", None, True,
         "https://c.example/f", "https://a.example/", "oopif", "continue"),
    Case("oopif post", "https://c.example/s", "POST", "a=1", True,
         "https://c.example/f", "https://a.example/", "oopif", "refuse"),
]  # fmt: skip


def playwright_request(case: Case) -> FakeRequest:
    if case.kind == "unknown":
        return FakeRequest(case.url, case.method, case.body, case.navigation, None)
    parent = FakeFrame(case.parent_url) if case.parent_url else None
    frame = FakeFrame(case.frame_url, parent)
    return FakeRequest(case.url, case.method, case.body, case.navigation, frame)


def guard_with_navigate_on_a(base: Path) -> tuple[NavigationGuard, Config]:
    cfg = Config(data_dir=base, enforcement_mode="enforce", headless=True)
    perms = PermissionStore()
    perms.grant("default", A, Capability.NAVIGATE)
    return NavigationGuard(cfg, perms, AuditLog(cfg.audit_path), mode="enforce"), cfg


def audit_rows(cfg: Config) -> list[dict]:
    if not cfg.audit_path.exists():
        return []
    rows = [json.loads(x) for x in cfg.audit_path.read_text().splitlines() if x.strip()]
    for row in rows:
        row.pop("ts")
    return rows


@pytest.mark.parametrize("case", PARITY_CASES, ids=[c.name for c in PARITY_CASES])
async def test_a_paused_request_is_judged_exactly_as_the_playwright_route_judges_it(
    make_rig, tmp_path, case
):
    # The route, which is how this guard was installed before.
    route_guard, route_cfg = guard_with_navigate_on_a(tmp_path / "route")
    route = FakeRoute()
    request = playwright_request(case)
    await route_guard(route, request)
    route_verdict = "continue" if route.calls == [("continue",)] else "refuse"
    assert route.calls in ([("continue",)], [("fulfill", 204)])

    # The same request, described the way Chrome describes it.
    rig = make_rig()
    await rig.start()
    rig.grant(Capability.NAVIGATE)
    session = "S1"
    if case.kind == "oopif":
        session = "S2"
        rig.chrome.attach("S2", "iframe", "T2", waiting=True, parent="S1")
        await rig.chrome.settle()
        rig.chrome.frame("S2", "F2", case.frame_url)
    elif case.kind == "page":
        if case.parent_url is not None:
            rig.chrome.frame("S1", "F1", case.parent_url)
        rig.chrome.frame("S1", "F2", case.frame_url, parent="F1" if case.parent_url else None)
    rig.chrome.paused(
        session,
        "r1",
        case.url,
        method=case.method,
        frame="F2",
        body=case.body,
        resource="Document" if case.navigation else "Script",
    )
    await rig.chrome.settle()

    # Same reading of the request, same verdict, same refusal count, same audit row.
    assert classify(rig.judge.seen[-1]) == classify(request)
    assert rig.verdict("r1", session) == route_verdict == case.expect
    assert rig.navigation.refusals == route_guard.refusals
    assert rig.audit() == audit_rows(route_cfg)


# --------------------------------------------------------------------------
# Redirect hops: every hop of a chain is a navigation of its own
# --------------------------------------------------------------------------


def hop_request(status: int, first_method: str) -> tuple[str, str | None]:
    """The method and body of the request Chrome issues after a redirect with ``status``."""
    if first_method == "POST" and status in (307, 308):
        return "POST", "a=1"  # these keep the method and the body
    return "GET", None  # 301/302/303 turn a POST into a GET; a GET stays one


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("first_method", ["GET", "POST"])
async def test_a_hop_from_an_approved_origin_to_an_unapproved_one_is_refused_and_traced(
    page_rig, status, first_method
):
    first_url = f"https://a.example/r/{status}"
    hop_method, hop_body = hop_request(status, first_method)
    if first_method == "POST":
        page_rig.grant(Capability.SUBMIT, initiator=A)  # one use, spent by the first request

    page_rig.chrome.paused(
        "S1", "R1", first_url, method=first_method, body="a=1" if first_method == "POST" else None
    )
    page_rig.chrome.paused(
        "S1",
        "R2",
        "https://b.example/dest",
        method=hop_method,
        body=hop_body,
        redirected_from="R1",
    )
    await page_rig.chrome.settle()

    assert page_rig.verdict("R1") == "continue"
    assert page_rig.verdict("R2") == "refuse"
    first, hop = page_rig.audit()
    assert first["status"] == "allowed"
    assert "redirect_from" not in first["args"]
    assert hop["status"] == "denied"
    assert hop["args"]["redirect_from"] == first_url
    assert hop["args"]["capability"] == ("submit" if hop_body else "navigate")
    assert (hop["origin"], hop["initiator"]) == ("https://b.example", "https://a.example")
    assert page_rig.navigation.refusals == 1
    if hop_body:  # the form is being sent on to another site: the trail says so
        assert hop["args"]["cross_origin_send"] == "https://b.example"


async def test_a_hop_to_an_approved_origin_is_judged_and_let_through(page_rig):
    page_rig.grant(Capability.NAVIGATE, parse_origin("https://b.example/"))

    page_rig.chrome.paused("S1", "R1", "https://a.example/r/302")
    page_rig.chrome.paused("S1", "R2", "https://b.example/dest", redirected_from="R1")
    await page_rig.chrome.settle()

    assert page_rig.verdict("R2") == "continue"
    hop = page_rig.audit()[-1]
    assert (hop["status"], hop["args"]["redirect_from"]) == ("allowed", "https://a.example/r/302")


STAYS_PUT = {
    "same origin": (
        "https://a.example/",
        "https://a.example/r/302",
        "https://a.example/landing?x=1",
    ),
    "default port written out": (
        "https://a.example/",
        "https://a.example/r/302",
        "https://a.example:443/landing",
    ),
    "http upgraded to https on the same host": (
        "http://a.example/",
        "http://a.example/r/302",
        "https://a.example/landing",
    ),
}


@pytest.mark.parametrize("where", STAYS_PUT.values(), ids=STAYS_PUT.keys())
async def test_a_hop_that_stays_with_the_origin_that_issued_it_is_not_judged(make_rig, where):
    frame_url, first_url, hop_url = where
    rig = make_rig()
    await rig.start()
    rig.chrome.frame("S1", "F1", frame_url)
    rig.grant(Capability.NAVIGATE, parse_origin(frame_url))

    rig.chrome.paused("S1", "R1", first_url)
    rig.chrome.paused("S1", "R2", hop_url, redirected_from="R1")
    await rig.chrome.settle()

    assert [rig.verdict("R1"), rig.verdict("R2")] == ["continue", "continue"]
    assert len(rig.audit()) == 1, "only the first request was a decision"
    assert rig.navigation.refusals == 0


MOVES = {
    "https downgraded to http": ("https://a.example/", "http://a.example/landing"),
    "other port": ("https://a.example/", "https://a.example:8443/landing"),
    "http upgraded to a port that is not the default": (
        "http://a.example/",
        "https://a.example:8443/landing",
    ),
    "http on a port upgraded to https": ("http://a.example:8080/", "https://a.example/landing"),
    "other host": ("https://a.example/", "https://b.example/landing"),
}


@pytest.mark.parametrize("where", MOVES.values(), ids=MOVES.keys())
async def test_a_hop_that_goes_somewhere_new_is_judged_as_the_move_it_is(make_rig, where):
    frame_url, hop_url = where
    rig = make_rig()
    await rig.start()
    rig.chrome.frame("S1", "F1", frame_url)
    rig.grant(Capability.NAVIGATE, parse_origin(frame_url))

    rig.chrome.paused("S1", "R1", frame_url + "r/302")
    rig.chrome.paused("S1", "R2", hop_url, redirected_from="R1")
    await rig.chrome.settle()

    assert rig.verdict("R2") == "refuse"
    assert rig.audit()[-1]["args"]["redirect_from"] == frame_url + "r/302"


async def test_a_307_to_the_same_origin_does_not_spend_a_second_submit_grant(page_rig):
    page_rig.grant(Capability.SUBMIT, initiator=A)
    page_rig.grant(Capability.SUBMIT, initiator=A)  # two single-use grants; the form needs one

    page_rig.chrome.paused("S1", "R1", "https://a.example/s", method="POST", body="a=1")
    page_rig.chrome.paused(
        "S1", "R2", "https://a.example/s2", method="POST", body="a=1", redirected_from="R1"
    )
    await page_rig.chrome.settle()

    assert [page_rig.verdict("R1"), page_rig.verdict("R2")] == ["continue", "continue"]
    assert [r["status"] for r in page_rig.audit()] == ["allowed"], "the hop was not a decision"
    unspent = [
        g for g in page_rig.perms.live_grants("default") if g.capability is Capability.SUBMIT
    ]
    assert len(unspent) == 1, "one grant bought the form; its same-site hop took nothing more"


async def test_a_hop_whose_first_request_is_unknown_is_judged_as_a_first_request(page_rig):
    page_rig.chrome.paused("S1", "R9", "https://b.example/dest", redirected_from="never-seen")
    await page_rig.chrome.settle()

    assert page_rig.verdict("R9") == "refuse"
    row = page_rig.audit()[-1]
    assert (row["status"], "redirect_from" in row["args"]) == ("denied", False)


@pytest.mark.parametrize(
    ("others", "remembered"),
    [(cdp_guard._CHAIN_MAX - 1, True), (cdp_guard._CHAIN_MAX, False)],
    ids=["still remembered", "forgotten"],
)
async def test_a_chain_is_remembered_until_enough_newer_requests_push_it_out(
    page_rig, others, remembered
):
    page_rig.chrome.paused("S1", "R0", "https://a.example/r/302")
    for i in range(others):
        page_rig.chrome.paused("S1", f"X{i}", "https://a.example/page")
    page_rig.chrome.paused("S1", "H", "https://b.example/dest", redirected_from="R0")
    await page_rig.chrome.settle()

    assert page_rig.verdict("H") == "refuse"
    hop = page_rig.audit()[-1]
    assert hop["status"] == "denied"
    assert ("redirect_from" in hop["args"]) is remembered


async def test_equal_request_ids_on_two_targets_do_not_share_a_chain(page_rig):
    page_rig.chrome.attach("S2", "page", "T2", waiting=True)
    await page_rig.chrome.settle()
    page_rig.chrome.frame("S2", "F5", "https://c.example/")
    page_rig.grant(Capability.NAVIGATE, parse_origin("https://c.example/"))

    page_rig.chrome.paused("S1", "R1", "https://a.example/r/1")
    page_rig.chrome.paused("S2", "R1", "https://c.example/r/1", frame="F5")
    page_rig.chrome.paused("S1", "R2", "https://b.example/dest", redirected_from="R1")
    page_rig.chrome.paused("S2", "R2", "https://d.example/dest", frame="F5", redirected_from="R1")
    await page_rig.chrome.settle()

    assert page_rig.verdict("R2", "S1") == page_rig.verdict("R2", "S2") == "refuse"
    hops = [r for r in page_rig.audit() if r["status"] == "denied"]
    assert [(h["initiator"], h["args"]["redirect_from"]) for h in hops] == [
        ("https://a.example", "https://a.example/r/1"),
        ("https://c.example", "https://c.example/r/1"),
    ]


async def test_a_hop_names_the_request_just_before_it_not_the_start_of_the_chain(page_rig):
    page_rig.chrome.paused("S1", "R1", "https://a.example/r/1")
    page_rig.chrome.paused("S1", "R2", "https://a.example/r/2", redirected_from="R1")
    page_rig.chrome.paused("S1", "R3", "https://b.example/dest", redirected_from="R2")
    await page_rig.chrome.settle()

    assert [page_rig.verdict(r) for r in ("R1", "R2", "R3")] == ["continue", "continue", "refuse"]
    rows = page_rig.audit()
    assert len(rows) == 2, "the same-origin hop in the middle is not a decision"
    assert rows[-1]["args"]["redirect_from"] == "https://a.example/r/2"


# --------------------------------------------------------------------------
# The frame cache
# --------------------------------------------------------------------------


def test_loading_a_tree_gives_every_frame_its_url_and_its_parent():
    cache = FrameCache()
    cache.load(
        {
            "frame": {"id": "F1", "url": "https://a.example/"},
            "childFrames": [
                {
                    "frame": {"id": "F2", "parentId": "F1", "url": "https://c.example/f"},
                    "childFrames": [
                        {"frame": {"id": "F3", "parentId": "F2", "url": "about:blank"}}
                    ],
                }
            ],
        }
    )

    f = cache.frames
    assert (f["F1"].url, f["F1"].parent_frame) == ("https://a.example/", None)
    assert f["F2"].parent_frame is f["F1"] and f["F2"].url == "https://c.example/f"
    assert f["F3"].parent_frame is f["F2"]


def test_a_frame_attached_under_an_unknown_parent_gets_a_stand_in_without_an_origin():
    cache = FrameCache()
    cache.attached({"frameId": "F2", "parentFrameId": "FX"})

    stand_in = cache.frames["F2"].parent_frame
    assert stand_in is cache.frames["FX"]
    assert (stand_in.url, stand_in.parent_frame) == ("", None)

    cache.navigated({"frame": {"id": "FX", "url": "https://a.example/"}})
    assert cache.frames["F2"].parent_frame.url == "https://a.example/", "the stand-in becomes real"


def test_navigation_updates_a_known_frame_and_introduces_an_unknown_one():
    cache = FrameCache()
    cache.load({"frame": {"id": "F1", "url": "about:blank"}})

    cache.navigated({"frame": {"id": "F1", "url": "https://a.example/next"}})
    cache.navigated({"frame": {"id": "F2", "parentId": "F1", "url": "https://c.example/"}})

    assert cache.frames["F1"].url == "https://a.example/next"
    assert cache.frames["F2"].parent_frame is cache.frames["F1"]


def test_a_same_document_navigation_updates_only_a_frame_that_is_known():
    cache = FrameCache()
    cache.load({"frame": {"id": "F1", "url": "https://a.example/page"}})

    cache.within_document({"frameId": "F1", "url": "https://a.example/page#top"})
    cache.within_document({"frameId": "F9", "url": "https://a.example/#x"})

    assert cache.frames["F1"].url == "https://a.example/page#top"
    assert "F9" not in cache.frames, "a frame invented here would have no parent: a main frame"


def test_a_detached_frame_is_forgotten():
    cache = FrameCache()
    cache.load({"frame": {"id": "F1", "url": "https://a.example/"}})

    cache.detached({"frameId": "F1"})
    cache.detached({"frameId": "never-there"})

    assert cache.frames == {}


def test_the_root_of_an_iframe_target_is_a_subframe_whose_parent_lives_elsewhere():
    tree = {"frame": {"id": "F2", "url": "https://c.example/f"}}
    oopif, page = FrameCache(root_is_subframe=True), FrameCache()
    oopif.load(tree)
    page.load(tree)

    parent = oopif.frames["F2"].parent_frame
    assert parent is not None and (parent.url, parent.parent_frame) == ("", None)
    assert page.frames["F2"].parent_frame is None


async def test_a_subframe_request_is_not_judged_unless_it_carries_a_body(page_rig):
    page_rig.chrome.event("Page.frameAttached", {"frameId": "F2", "parentFrameId": "FX"}, "S1")

    page_rig.chrome.paused("S1", "r1", "https://c.example/widget", frame="F2")
    await page_rig.chrome.settle()
    assert page_rig.verdict("r1") == "continue"
    assert page_rig.audit() == []

    page_rig.chrome.paused("S1", "r2", "https://c.example/s", method="POST", body="a=1", frame="F2")
    await page_rig.chrome.settle()
    assert page_rig.verdict("r2") == "refuse"
    assert [r["status"] for r in page_rig.audit()] == ["denied"]


async def test_a_redirect_hop_inside_an_iframe_is_not_judged_either(page_rig):
    page_rig.chrome.frame("S1", "F2", "https://c.example/f", parent="F1")

    page_rig.chrome.paused("S1", "R1", "https://c.example/r/302", frame="F2")
    page_rig.chrome.paused("S1", "R2", "https://d.example/widget", frame="F2", redirected_from="R1")
    await page_rig.chrome.settle()

    assert [page_rig.verdict("R1"), page_rig.verdict("R2")] == ["continue", "continue"]
    assert page_rig.audit() == []


async def test_a_same_document_navigation_of_an_unseen_frame_does_not_invent_it(page_rig):
    page_rig.chrome.event(
        "Page.navigatedWithinDocument", {"frameId": "F9", "url": "https://a.example/#x"}, "S1"
    )

    page_rig.chrome.paused("S1", "r1", "https://a.example/y", frame="F9")
    await page_rig.chrome.settle()

    row = page_rig.audit()[-1]
    assert (row["initiator"], row["args"]["capability"]) == ("(unknown)", "navigate")


# --------------------------------------------------------------------------
# Target setup: guarded before it runs
# --------------------------------------------------------------------------


async def test_the_browser_wide_auto_attach_holds_every_page_until_it_is_guarded(make_rig):
    rig = make_rig()
    await rig.start(pages=())

    first = rig.chrome.commands[0]
    assert (first.session, first.method) == ("", "Target.setAutoAttach")
    assert first.params == {
        "autoAttach": True,
        "waitForDebuggerOnStart": True,
        "flatten": True,
        "filter": [{"type": "page"}, {"exclude": True}],
    }


async def test_a_page_held_for_the_debugger_is_guarded_in_this_exact_order(rig):
    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    assert rig.chrome.methods("S2") == PAGE_SETUP
    fetch, network, bypass, auto = rig.chrome.sent("S2")[:4]
    assert fetch.params == {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]}
    assert network.params["maxTotalBufferSize"] == 0
    assert bypass.params == {"bypass": True}
    assert auto.params == {
        "autoAttach": True,
        "waitForDebuggerOnStart": True,
        "flatten": True,
        "filter": [{"type": "iframe"}, {"exclude": True}],
    }
    before_resume = PAGE_SETUP[: PAGE_SETUP.index("Runtime.runIfWaitingForDebugger")]
    assert not {"Page.enable", "Page.getFrameTree", "Runtime.evaluate"} & set(before_resume)


async def test_a_page_that_is_not_held_is_never_told_to_resume(rig):
    rig.chrome.attach("S2", "page", "T2", waiting=False)
    await rig.chrome.settle()

    assert rig.chrome.methods("S2") == [m for m in PAGE_SETUP if "runIfWaiting" not in m]


async def test_an_iframe_target_is_guarded_like_a_page(rig):
    rig.chrome.attach("S3", "iframe", "T3", waiting=True, parent="S1")
    await rig.chrome.settle()

    assert rig.chrome.methods("S3") == PAGE_SETUP


@pytest.mark.parametrize("waiting", [True, False])
@pytest.mark.parametrize("kind", ["service_worker", "worker", "shared_worker"])
async def test_a_target_of_another_type_is_released_and_detached_and_never_given_fetch(
    rig, kind, waiting
):
    rig.chrome.attach("S9", kind, "T9", waiting=waiting)
    await rig.chrome.settle()

    assert rig.chrome.methods("S9") == (["Runtime.runIfWaitingForDebugger"] if waiting else [])
    [detach] = rig.chrome.sent(method="Target.detachFromTarget")
    assert detach.params == {"sessionId": "S9"}
    if waiting:
        order = [c.method for c in rig.chrome.commands]
        assert order.index("Runtime.runIfWaitingForDebugger") < order.index(
            "Target.detachFromTarget"
        )
    rig.chrome.paused("S9", "r1", "https://b.example/x")
    await rig.chrome.settle()
    assert rig.answers() == [], "nothing it says is judged"


async def test_nothing_else_is_sent_to_a_target_until_fetch_enable_is_answered(rig):
    hold = rig.chrome.hold("Fetch.enable", session="S2")
    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    assert rig.chrome.methods("S2") == ["Fetch.enable"]

    hold.release()
    await rig.chrome.settle()
    assert rig.chrome.methods("S2") == PAGE_SETUP


async def test_setup_completes_when_the_renderer_bound_replies_only_follow_the_resume(rig):
    # A popup opened by a link has no renderer yet: these three answer once it is resumed.
    rig.chrome.hold(
        "Network.enable",
        "Network.setBypassServiceWorker",
        "Target.setAutoAttach",
        session="S2",
        until="Runtime.runIfWaitingForDebugger",
    )
    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    assert rig.chrome.methods("S2") == PAGE_SETUP, "setup did not wait for them before resuming"
    rig.grant(Capability.NAVIGATE)
    rig.chrome.frame("S2", "F7", "https://a.example/")
    rig.chrome.paused("S2", "r1", "https://b.example/x", frame="F7")
    await rig.chrome.settle()
    assert rig.verdict("r1", "S2") == "refuse", "and the target is guarded"
    assert rig.lost == []


HELD_DURING_SETUP = [
    "Fetch.enable",
    "Network.enable",
    "Runtime.runIfWaitingForDebugger",
    "Page.enable",
    "Page.getFrameTree",
]


@pytest.mark.parametrize("held", HELD_DURING_SETUP)
async def test_a_target_that_detaches_during_setup_ends_its_setup_without_a_loss(rig, held):
    hold = rig.chrome.hold(held, session="S2")
    before = asyncio.all_tasks()
    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()
    assert len(new_tasks(before)) == 1, "its setup waits for a reply that is not coming"

    rig.chrome.detach("S2")
    await rig.chrome.settle()
    assert not new_tasks(before), "the target is gone, so its setup ends"
    hold.release()  # the reply that was held turns up late
    await rig.chrome.settle()

    assert rig.lost == [] and rig.guard.lost is None


async def test_a_failure_that_arrives_after_the_target_detached_is_ignored(rig):
    rig.chrome.errors[("S2", "Network.enable")] = {"code": -32000, "message": "boom"}
    hold = rig.chrome.hold("Network.enable", session="S2")
    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    hold.release()  # the failure and the detach reach the guard back to back
    rig.chrome.detach("S2")
    await rig.chrome.settle()

    assert rig.lost == [] and rig.guard.lost is None


# --------------------------------------------------------------------------
# Start-up
# --------------------------------------------------------------------------


async def test_start_connects_to_loopback_only_and_asks_in_this_order(make_rig):
    rig = make_rig()
    await rig.start(pages=())

    assert [c.method for c in rig.chrome.commands] == [
        "Target.setAutoAttach",
        "Target.getTargets",
        "SystemInfo.getProcessInfo",
    ]
    assert rig.chrome.commands[1].params == {"filter": [{"type": "page"}, {"exclude": True}]}
    assert rig.connects[0][:3] == ("127.0.0.1", PORT, PATH)


async def test_the_endpoint_file_is_gone_before_the_connection_is_made(make_rig):
    rig = make_rig()
    file = rig.write_endpoint()
    rig.chrome.pages = []

    await rig.guard.start(rig.profile)

    assert rig.endpoint_file_at_connect == [False]
    assert not file.exists()


async def test_start_returns_only_after_every_open_page_is_guarded(make_rig):
    rig = make_rig()
    rig.chrome.pages = [("T1", "S1"), ("T2", "S2")]
    hold = rig.chrome.hold("Page.getFrameTree", session="S2")
    rig.write_endpoint()

    starting = asyncio.ensure_future(rig.guard.start(rig.profile))
    await asyncio.sleep(0.15)  # several polls of Target.getTargets
    await rig.chrome.settle()
    assert not starting.done(), "a tab is still being set up"
    assert rig.chrome.methods("S2")[-1] == "Page.getFrameTree"

    hold.release()
    await asyncio.wait_for(starting, 5)
    assert rig.chrome.methods("S1") == PAGE_SETUP[:4] + PAGE_SETUP[5:], "both were set up"


async def test_start_times_out_when_a_listed_page_never_attaches(make_rig):
    rig = make_rig()
    rig.chrome.listed_only = ["T9"]
    rig.write_endpoint()

    with pytest.raises(CdpGuardError) as info:
        await rig.guard.start(rig.profile, timeout=0.2)

    assert_no_endpoint(info.value)
    assert rig.lost == [], "start() raised; on_lost is for a guard that was running"
    assert rig.chrome.ws.closed, "and a guard that did not start does not hold the socket"


async def test_no_endpoint_file_is_an_error_and_nothing_is_connected(make_rig):
    rig = make_rig()

    with pytest.raises(CdpGuardError) as info:
        await rig.guard.start(rig.profile, timeout=0.1)

    assert_no_endpoint(info.value)
    assert rig.connects == []


async def test_the_endpoint_is_read_and_the_file_deleted(tmp_path):
    file = tmp_path / "DevToolsActivePort"
    file.write_text(ENDPOINT_FILE)

    assert await read_endpoint(tmp_path, 1.0) == (PORT, PATH)
    assert not file.exists()


@pytest.mark.parametrize(
    "partial",
    [
        "",
        "12345",
        "12345\n",
        "12345\n/devtools/br",
        "12345\n/devtools/browser/",
        "12345\n/devtools/browser/0b0b",
    ],
    ids=["empty", "port only", "port and newline", "path cut in its prefix", "no id", "short id"],
)
async def test_a_half_written_endpoint_file_is_waited_on(tmp_path, partial):
    file = tmp_path / "DevToolsActivePort"
    file.write_text(partial)

    waiting = asyncio.ensure_future(read_endpoint(tmp_path, 5.0))
    await asyncio.sleep(0.15)
    assert not waiting.done(), "a partial file is not an endpoint"
    assert file.exists()

    file.write_text(ENDPOINT_FILE)
    assert await asyncio.wait_for(waiting, 5) == (PORT, PATH)


GARBAGE = [
    f"abc\n{PATH}",
    f"0\n{PATH}",
    f"70000\n{PATH}",
    "12345\n/etc/passwd",
    "12345\n/devtools/browser/../../x",
    f"12345\n//evil.example{PATH}",
    b"\xff\xfe\x00\x01",
]


@pytest.mark.parametrize("content", GARBAGE, ids=range(len(GARBAGE)))
async def test_a_file_that_does_not_name_a_browser_endpoint_is_never_connected_to(
    make_rig, content
):
    rig = make_rig()
    rig.write_endpoint(content)

    with pytest.raises(CdpGuardError):
        await rig.guard.start(rig.profile, timeout=0.2)

    assert rig.connects == []


def test_a_stale_endpoint_is_removed_before_launch_and_the_profile_made_private(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    os.chmod(profile, 0o755)
    (profile / "DevToolsActivePort").write_text(ENDPOINT_FILE)

    prepare_profile(profile)

    assert not (profile / "DevToolsActivePort").exists()
    if os.name == "posix":
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700


PROCESS_INFO = {
    "the browser among others": (
        {"processInfo": [{"type": "gpu", "id": 11}, {"type": "browser", "id": 4242}]},
        4242,
    ),
    "no browser entry": ({"processInfo": [{"type": "gpu", "id": 11}]}, None),
    "pid that is not a number": ({"processInfo": [{"type": "browser", "id": "4242"}]}, None),
    "nothing": ({}, None),
}


@pytest.mark.parametrize(("reply", "pid"), PROCESS_INFO.values(), ids=PROCESS_INFO.keys())
async def test_the_browser_pid_comes_from_the_process_info(make_rig, reply, pid):
    rig = make_rig()
    rig.chrome.results["SystemInfo.getProcessInfo"] = reply

    await rig.start(pages=())

    assert rig.guard.browser_pid == pid


async def test_a_browser_that_will_not_say_its_pid_leaves_it_unknown_and_still_starts(make_rig):
    rig = make_rig()
    rig.chrome.errors[("", "SystemInfo.getProcessInfo")] = {"code": -32601, "message": "no"}

    await rig.start(pages=())

    assert rig.guard.browser_pid is None


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


async def test_a_connection_that_ends_while_running_is_reported_once_with_its_reason(rig):
    rig.chrome.end("the peer went away")
    await rig.chrome.settle()

    assert rig.lost == ["the peer went away"]
    assert rig.guard.lost == "the peer went away"


@pytest.mark.parametrize("how", ["begin_close", "stop"])
async def test_a_connection_that_ends_because_the_browser_is_closing_is_not_a_loss(rig, how):
    if how == "begin_close":
        rig.guard.begin_close()
        rig.chrome.end()
    else:
        await rig.guard.stop()
    await rig.chrome.settle()

    assert rig.lost == [] and rig.guard.lost is None


@pytest.mark.parametrize(
    "ends_on",
    ["Target.setAutoAttach", "Target.getTargets"],
    ids=["first command", "after the target list"],
)
async def test_a_connection_that_ends_during_start_fails_start_and_is_not_a_loss(make_rig, ends_on):
    rig = make_rig()
    rig.chrome.on_command = lambda cmd: rig.chrome.end() if cmd.method == ends_on else None
    rig.write_endpoint()

    with pytest.raises(CdpGuardError) as info:
        await asyncio.wait_for(rig.guard.start(rig.profile), 1)

    assert_no_endpoint(info.value)
    assert rig.lost == []


async def test_a_target_that_cannot_be_guarded_during_start_fails_start_promptly(make_rig):
    rig = make_rig()
    rig.chrome.errors[("S1", "Fetch.enable")] = {"code": -32000, "message": "boom"}
    rig.chrome.pages = [("T1", "S1")]
    rig.write_endpoint()

    with pytest.raises(CdpGuardError) as info:
        await asyncio.wait_for(rig.guard.start(rig.profile, timeout=5), 2)

    assert str(info.value) == rig.guard.lost, "start() says why"
    assert rig.lost == []
    assert not rig.chrome.ws.aborted


@pytest.mark.parametrize(
    "method",
    [
        "Fetch.enable",
        "Network.enable",
        "Network.setBypassServiceWorker",
        "Target.setAutoAttach",
        "Runtime.runIfWaitingForDebugger",
    ],
)
async def test_a_target_that_cannot_be_set_up_is_a_loss_and_the_connection_stays_open(rig, method):
    rig.chrome.errors[("S2", method)] = {"code": -32000, "message": "boom"}

    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    assert rig.guard.lost and rig.lost == [rig.guard.lost]
    # Open: Chrome releases everything it holds the moment the socket closes.
    assert not rig.chrome.ws.closing


async def test_a_target_whose_fetch_cannot_be_enabled_is_never_resumed(rig):
    rig.chrome.errors[("S2", "Fetch.enable")] = {"code": -32000, "message": "boom"}

    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    assert rig.chrome.methods("S2") == ["Fetch.enable"]


GONE = {
    "target not found code": {"code": -32001, "message": "whatever"},
    "not found message": {"code": -32000, "message": "Not found"},
    "closed message": {
        "code": -32000,
        "message": "Session closed. Most likely the page has been closed.",
    },
}


@pytest.mark.parametrize("error", GONE.values(), ids=GONE.keys())
@pytest.mark.parametrize(
    "method", ["Fetch.enable", "Network.enable", "Runtime.runIfWaitingForDebugger"]
)
async def test_a_setup_command_that_fails_because_the_target_is_gone_is_not_a_loss(
    rig, method, error
):
    rig.chrome.errors[("S2", method)] = error

    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    assert rig.lost == [] and rig.guard.lost is None


@pytest.mark.parametrize("method", ["Page.enable", "Page.getFrameTree"])
async def test_a_target_whose_frame_tree_cannot_be_read_stays_guarded(rig, method):
    rig.chrome.errors[("S2", method)] = {"code": -32000, "message": "boom"}
    rig.grant(Capability.NAVIGATE)

    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()
    rig.chrome.frame("S2", "F7", "https://a.example/")
    rig.chrome.paused("S2", "r1", "https://b.example/x", frame="F7")
    await rig.chrome.settle()

    assert rig.lost == []
    assert rig.verdict("r1", "S2") == "refuse", "judged from the events alone"


@pytest.mark.parametrize("raw", ["{not json", "", "[1, 2]", "42", "null", '"text"'])
async def test_a_message_that_is_not_a_json_object_is_a_lost_connection(rig, raw):
    rig.chrome.deliver(raw)
    await rig.chrome.settle()

    assert len(rig.lost) == 1 and rig.guard.lost == rig.lost[0]


UNREADABLE = {
    "attach without its target": ("Target.attachedToTarget", {"sessionId": "S2"}, ""),
    "frame event without its frame": ("Page.frameNavigated", {}, "S1"),
}


@pytest.mark.parametrize("event", UNREADABLE.values(), ids=UNREADABLE.keys())
async def test_an_event_the_guard_cannot_read_is_a_loss_not_a_skip(rig, event):
    method, params, session = event
    rig.chrome.event(method, params, session)
    await rig.chrome.settle()

    assert len(rig.lost) == 1 and rig.guard.lost == rig.lost[0]


async def test_a_loss_fails_the_commands_in_flight_instead_of_leaving_them_waiting(rig):
    rig.chrome.hold("Page.enable", session="S2")
    before = asyncio.all_tasks()
    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()
    assert len(new_tasks(before)) == 1

    rig.chrome.end()
    await rig.chrome.settle()

    assert not new_tasks(before), "the setup that was waiting has ended"
    assert len(rig.lost) == 1


async def test_the_first_reason_is_kept_and_the_owner_hears_once(rig):
    rig.chrome.errors[("S2", "Fetch.enable")] = {"code": -32000, "message": "boom"}
    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()
    first = rig.guard.lost

    rig.chrome.end("then the connection went too")
    await rig.chrome.settle()

    assert rig.guard.lost == first
    assert rig.lost == [first]


async def test_after_the_connection_ended_nothing_more_is_answered(page_rig):
    page_rig.chrome.end()
    await page_rig.chrome.settle()
    page_rig.chrome.paused("S1", "r1", "https://b.example/elsewhere")
    await page_rig.chrome.settle()

    assert page_rig.guard.lost
    assert page_rig.answers() == []


# --------------------------------------------------------------------------
# Noise on the wire
# --------------------------------------------------------------------------


async def test_network_events_are_dropped_without_being_parsed(page_rig):
    sent = len(page_rig.chrome.commands)
    # Not JSON, and two megabytes long: parsing it would end the guard, and take a while.
    page_rig.chrome.deliver(
        '{"method":"Network.requestWillBeSent","params":{"blob":"' + "x" * 2_000_000
    )
    page_rig.chrome.deliver(
        dumps({"method": "Network.loadingFinished", "params": {}, "sessionId": "S1"})
    )

    page_rig.chrome.paused("S1", "r1", "https://a.example/next")
    await page_rig.chrome.settle()

    assert page_rig.lost == [] and page_rig.guard.answer_errors == 0
    assert len(page_rig.chrome.commands) == sent + 1, "the one verdict, nothing else"
    assert page_rig.verdict("r1") == "continue", "and the stream is still being read"


async def test_events_and_replies_the_guard_does_not_know_are_ignored(page_rig):
    sent = len(page_rig.chrome.commands)
    for message in [
        {"method": "Page.lifecycleEvent", "params": {"name": "load"}, "sessionId": "S1"},
        {"method": "Target.targetCreated", "params": {"targetInfo": {}}},
        {"method": "Fetch.authRequired", "params": {}, "sessionId": "S1"},
        {"method": "Page.frameNavigated", "params": {"frame": {"id": "F1"}}, "sessionId": "nope"},
        {"id": 987654, "result": {}},
    ]:
        page_rig.chrome.deliver(message)
    await page_rig.chrome.settle()

    assert page_rig.lost == [] and page_rig.guard.lost is None
    assert len(page_rig.chrome.commands) == sent


# --------------------------------------------------------------------------
# No endpoint in any error (it is a bearer credential for an open debugging port)
# --------------------------------------------------------------------------


async def test_a_refused_connection_does_not_name_the_endpoint(make_rig):
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    rig = make_rig(fake_transport=False)
    rig.write_endpoint(f"{port}\n{PATH}")

    with pytest.raises(CdpGuardError) as info:
        await rig.guard.start(rig.profile, timeout=2)

    assert str(port) not in f"{info.value} {info.value!r}"
    assert_no_endpoint(info.value)


async def test_a_refused_handshake_does_not_name_the_endpoint(make_rig):
    async def refuse(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(refuse, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    rig = make_rig(fake_transport=False)
    rig.write_endpoint(f"{port}\n{PATH}")
    try:
        with pytest.raises(CdpGuardError) as info:
            await rig.guard.start(rig.profile, timeout=2)
    finally:
        server.close()

    assert str(port) not in f"{info.value} {info.value!r}"
    assert_no_endpoint(info.value)


async def test_the_audit_trail_never_holds_the_endpoint(page_rig):
    page_rig.chrome.paused("S1", "r1", "https://b.example/elsewhere")
    page_rig.chrome.event(
        "Fetch.requestPaused",
        {"requestId": "r2", "frameId": "F1", "resourceType": "Document"},
        "S1",
    )
    page_rig.chrome.end()
    await page_rig.chrome.settle()

    trail = page_rig.cfg.audit_path.read_text()
    assert {r["status"] for r in page_rig.audit()} == {"denied", "guard_error"}
    assert not any(secret in trail for secret in (str(PORT), PATH, UUID))


# --------------------------------------------------------------------------
# An upload is a message too big to keep
# --------------------------------------------------------------------------


def big_text(size: int) -> str:
    return ("".join(chr(33 + (i * 7) % 90) for i in range(90)) * (size // 90 + 1))[:size]


def chrome_event(
    *,
    url: str = "https://a.example/s",
    method: str = "POST",
    body: str | None = None,
    session: str | None = "S1",
    headers: dict | None = None,
    request_id: str | None = "interception-job-7.0",
    order: tuple = ("postData", "hasPostData", "postDataEntries", "initialPriority"),
) -> tuple[str, dict]:
    """A ``Fetch.requestPaused`` as Chrome serialises it, and the event without its upload."""
    body = big_text(MIB) if body is None else body
    pieces = {
        "postData": body,
        "hasPostData": True,
        "postDataEntries": [{"bytes": base64.b64encode(body.encode()).decode()}],
        "initialPriority": "VeryHigh",
    }
    request = {
        "url": url,
        "method": method,
        "headers": headers or {"Content-Type": "application/x-www-form-urlencoded"},
        **{key: pieces[key] for key in order},
        "referrerPolicy": "strict-origin-when-cross-origin",
    }
    params: dict[str, Any] = {} if request_id is None else {"requestId": request_id}
    params |= {"request": request, "frameId": "F1", "resourceType": "Document", "networkId": "7.1"}
    event: dict[str, Any] = {"method": "Fetch.requestPaused", "params": params}
    if session is not None:
        event["sessionId"] = session
    slim = json.loads(dumps(event))
    for key in ("postData", "postDataEntries"):
        del slim["params"]["request"][key]
    return dumps(event), slim


def ends(raw: str) -> tuple[bytes, bytes]:
    data = raw.encode()
    return data[:HEAD], data[-TAIL:]


@pytest.mark.parametrize("method", ["POST", "GET"], ids=["post", "get with a body"])
def test_an_oversize_event_is_cut_back_to_the_same_event_without_its_upload(method):
    raw, slim = chrome_event(method=method)
    assert len(raw) > 2 * MIB

    got = json.loads(shrink_oversize(*ends(raw)))

    assert got == slim
    assert got["params"]["request"]["hasPostData"] is True
    frames = {"F1": PausedFrame("https://a.example/", None)}
    assert classify(PausedRequest(got["params"], frames)).capability is Capability.SUBMIT


def test_text_that_looks_like_the_cut_markers_inside_a_url_header_or_body_does_not_move_the_cut():
    decoy = ',"postData":"evil","initialPriority":"x"'
    raw, slim = chrome_event(
        url=f"https://a.example/s?q={decoy}",
        headers={"Referer": f"https://a.example/{decoy}", "X-Note": '"initialPriority"'},
        body=decoy + big_text(MIB),
    )

    got = json.loads(shrink_oversize(*ends(raw)))

    assert got == slim
    assert got["params"]["request"]["url"].endswith(decoy)


def test_keys_in_an_unexpected_order_leave_an_event_that_only_names_the_request():
    raw, _ = chrome_event(order=("initialPriority", "postData", "hasPostData", "postDataEntries"))

    got = json.loads(shrink_oversize(*ends(raw)))

    assert got == {
        "method": "Fetch.requestPaused",
        "params": {"requestId": "interception-job-7.0", "request": {}},
        "sessionId": "S1",
    }


@pytest.mark.parametrize("missing", ["request id", "session"])
def test_an_event_that_cannot_be_named_is_not_cut_back(missing):
    raw, _ = chrome_event(
        request_id=None if missing == "request id" else "r1",
        session=None if missing == "session" else "S1",
    )

    assert shrink_oversize(*ends(raw)) is None


def test_a_network_event_too_big_to_keep_is_reduced_to_a_tiny_event_the_guard_drops_unparsed():
    raw = '{"method":"Network.dataReceived","params":{"data":"' + "x" * (2 * MIB) + '"}}'

    got = shrink_oversize(*ends(raw))

    assert len(got) < 100 and got.startswith('{"method":"Network.')


@pytest.mark.parametrize(
    "start",
    ['{"method":"Page.screencastFrame","params":{"data":"', '{"id":9,"result":{"data":"'],
    ids=["event", "reply"],
)
def test_a_huge_message_that_is_not_a_paused_request_is_not_cut_back(start):
    assert shrink_oversize(*ends(start + "x" * (2 * MIB) + '"}}')) is None


@pytest.mark.parametrize("method", ["POST", "GET"], ids=["post", "get with a body"])
async def test_an_oversize_upload_is_judged_as_a_submission_and_the_guard_carries_on(
    page_rig, method
):
    raw, _ = chrome_event(method=method)
    page_rig.chrome.deliver(Oversize(raw))
    await page_rig.chrome.settle()

    assert page_rig.verdict("interception-job-7.0") == "refuse"
    assert page_rig.audit()[-1]["args"]["capability"] == "submit"
    assert page_rig.lost == []

    page_rig.grant(Capability.SUBMIT, initiator=A)
    page_rig.chrome.deliver(Oversize(chrome_event(request_id="again")[0]))
    await page_rig.chrome.settle()
    assert page_rig.verdict("again") == "continue", "and a granted upload goes through"


@pytest.mark.parametrize(("mode", "verdict"), [("enforce", "refuse"), ("observe", "continue")])
async def test_an_oversize_event_that_cannot_be_read_is_answered_as_a_guard_error(
    make_rig, mode, verdict
):
    rig = make_rig(mode)
    await rig.start()
    raw, _ = chrome_event(order=("initialPriority", "postData", "hasPostData", "postDataEntries"))

    rig.chrome.deliver(Oversize(raw))
    await rig.chrome.settle()

    assert rig.verdict("interception-job-7.0", "S1") == verdict
    assert [r["status"] for r in rig.audit()] == ["guard_error"]
    assert rig.lost == []


async def test_an_oversize_message_that_is_not_a_paused_request_ends_the_guard(rig):
    rig.chrome.deliver(Oversize('{"id":9,"result":{"data":"' + "x" * (2 * MIB) + '"}}'))
    await rig.chrome.settle()

    assert len(rig.lost) == 1 and rig.guard.lost == rig.lost[0]


# --------------------------------------------------------------------------
# The browser process
# --------------------------------------------------------------------------


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


async def test_a_browser_is_seen_alive_then_killed_then_seen_gone():
    guard = CdpGuard(SpyJudge(None), on_lost=lambda _reason: None)  # type: ignore[arg-type]
    proc = sleeper()
    try:
        guard.browser_pid = proc.pid
        assert not await guard.browser_exited(0.05)

        assert guard.kill_browser()
        proc.wait(timeout=5)  # reaped, as the owner of a real browser process would
        assert await guard.browser_exited(2)
        assert not guard.kill_browser(), "already gone"
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="reads /proc for the state")
async def test_a_killed_browser_that_is_not_yet_reaped_counts_as_exited():
    guard = CdpGuard(SpyJudge(None), on_lost=lambda _reason: None)  # type: ignore[arg-type]
    proc = sleeper()
    try:
        guard.browser_pid = proc.pid
        assert guard.kill_browser()
        assert await guard.browser_exited(2), "a zombie awaiting its parent is not running"
    finally:
        proc.kill()
        proc.wait()


async def test_with_no_pid_known_nothing_is_killed_and_nothing_is_seen_to_exit():
    guard = CdpGuard(SpyJudge(None), on_lost=lambda _reason: None)  # type: ignore[arg-type]

    assert guard.browser_pid is None
    assert not guard.kill_browser()
    assert not await guard.browser_exited(0.05)


# --------------------------------------------------------------------------
# Quitting, or failing with tabs open: what the guard knows about its pages
# --------------------------------------------------------------------------


async def test_a_guard_that_has_seen_no_page_has_no_pages(make_rig):
    rig = make_rig()
    assert not rig.guard.has_pages(), "not started"

    await rig.start(pages=())

    assert not rig.guard.has_pages(), "started, with no tab open"


async def test_a_page_counts_from_the_moment_it_attaches_not_from_the_end_of_its_setup(make_rig):
    rig = make_rig()
    await rig.start(pages=())
    hold = rig.chrome.hold("Fetch.enable", session="S2")

    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    assert rig.chrome.methods("S2") == ["Fetch.enable"], "its setup is still waiting"
    assert rig.guard.has_pages()
    hold.release()
    await rig.chrome.settle()
    assert rig.guard.has_pages()


async def test_an_iframe_is_guarded_but_is_not_a_page(make_rig):
    rig = make_rig()
    await rig.start(pages=())

    rig.chrome.attach("S3", "iframe", "T3", waiting=True, parent="S1")
    await rig.chrome.settle()

    assert rig.chrome.methods("S3") == PAGE_SETUP, "it is registered and guarded all the same"
    assert not rig.guard.has_pages()


async def test_a_page_stops_counting_when_it_detaches_whatever_is_still_attached_under_it(rig):
    rig.chrome.attach("S3", "iframe", "T3", waiting=True, parent="S1")
    await rig.chrome.settle()
    assert rig.guard.has_pages()

    rig.chrome.detach("S1")
    await rig.chrome.settle()

    assert not rig.guard.has_pages(), "the iframe is still attached; it is not a tab"


async def test_closing_one_of_two_windows_leaves_the_other_counting(make_rig):
    rig = make_rig()
    await rig.start(pages=(("T1", "S1"), ("T2", "S2")))

    rig.chrome.detach("S1")
    await rig.chrome.settle()
    assert rig.guard.has_pages()

    rig.chrome.detach("S2")
    await rig.chrome.settle()
    assert not rig.guard.has_pages()


async def test_a_browser_that_quits_detaches_every_page_before_its_connection_ends(make_rig):
    rig = make_rig()
    await rig.start(pages=(("T1", "S1"), ("T2", "S2")))

    rig.chrome.detach("S1")
    rig.chrome.detach("S2")
    rig.chrome.end()
    await rig.chrome.settle()

    assert len(rig.lost) == 1
    assert rig.pages_at_loss == [False], "no tab was open when the owner was told"


async def test_a_connection_that_fails_with_a_tab_open_is_reported_while_the_page_is_attached(rig):
    rig.chrome.end()
    await rig.chrome.settle()

    assert len(rig.lost) == 1
    assert rig.pages_at_loss == [True]


async def test_a_window_closed_earlier_does_not_make_a_later_failure_look_like_a_quit(make_rig):
    rig = make_rig()
    await rig.start(pages=(("T1", "S1"), ("T2", "S2")))

    rig.chrome.detach("S1")  # the user closed one window
    rig.chrome.end()  # and then the connection failed with the other one open
    await rig.chrome.settle()

    assert len(rig.lost) == 1
    assert rig.pages_at_loss == [True]


async def test_a_page_that_cannot_be_guarded_is_reported_while_it_is_still_attached(make_rig):
    rig = make_rig()
    await rig.start(pages=())
    rig.chrome.errors[("S2", "Fetch.enable")] = {"code": -32000, "message": "boom"}

    rig.chrome.attach("S2", "page", "T2", waiting=True)
    await rig.chrome.settle()

    assert len(rig.lost) == 1
    assert rig.pages_at_loss == [True]
