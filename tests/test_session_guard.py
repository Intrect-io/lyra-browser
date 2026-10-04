"""The session puts the guard in front of the browser — and closes the browser if it cannot.

With ``guard_backend="cdp"`` nothing is registered on the context: the sidecar
(``cdp_guard.CdpGuard``) watches the browser over a connection of its own. That is only
safe because of what the session does around it: it starts the sidecar on the window that
is really open, closes the window when the sidecar cannot start, and — when the connection
is lost while the browser runs — kills the browser and refuses every call until someone
acknowledges it. These tests drive the real ``BrowserSession`` against a fake driver and a
fake sidecar, and assert on what was asked of each (order included), not only on envelopes.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from types import SimpleNamespace

import pytest

from lyra_browser import session as session_mod
from lyra_browser.approval import CollaborationState
from lyra_browser.audit import AuditLog
from lyra_browser.cdp_guard import CdpGuardError
from lyra_browser.config import Config
from lyra_browser.context import ServerContext, acquire_page
from lyra_browser.session import BrowserSession, GuardLost, launch_kwargs

HEADLESS_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "HeadlessChrome/154.0.8037.57 Safari/537.36"
)


class Order:
    """One shared log, so the order of calls on different fakes can be asserted."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def add(self, event: str) -> None:
        self.events.append(event)


class FakeContext:
    def __init__(self, order: Order, close_error: Exception | None = None) -> None:
        self.pages = [SimpleNamespace(is_closed=lambda: False, url="about:blank")]
        self.routes: list = []
        self.closed = False
        self._order = order
        self._close_error = close_error

    async def close(self) -> None:
        self._order.add("context.close")
        self.closed = True
        if self._close_error is not None:
            raise self._close_error

    async def route(self, pattern, handler) -> None:
        self._order.add("context.route")
        self.routes.append(pattern)

    def on(self, event, handler) -> None:
        pass


class FakePlaywright:
    def __init__(self, order: Order) -> None:
        self.order = order
        self.launches: list[dict] = []
        self.contexts: list[FakeContext] = []
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)
        self.stopped = False
        self.close_error: Exception | None = None
        # Whether the endpoint file of the previous run was still there at each launch.
        self.stale_endpoint_at_launch: list[bool] = []

    async def _launch(self, **kwargs) -> FakeContext:
        self.order.add("launch")
        profile = kwargs["user_data_dir"]
        self.stale_endpoint_at_launch.append(
            os.path.exists(os.path.join(profile, "DevToolsActivePort"))
        )
        self.launches.append(kwargs)
        ctx = FakeContext(self.order, self.close_error)
        self.contexts.append(ctx)
        return ctx

    async def stop(self) -> None:
        self.stopped = True


class FakeSidecar:
    """Stands in for ``CdpGuard``: records what the session asks, answers as told."""

    instances: list[FakeSidecar] = []
    fail_start: Exception | None = None
    order: Order
    # What browser_exited answers, call by call (False once the list runs out), and the
    # timeouts it was asked with.
    answers: list[bool] = []
    timeouts: list[float] = []
    pages_attached = True  # what has_pages() says: a tab still attached at the socket's end
    exit_gate: asyncio.Event | None = None

    def __init__(self, judge, *, on_lost) -> None:
        self.judge = judge
        self.on_lost = on_lost
        self.profile = None
        FakeSidecar.instances.append(self)

    async def start(self, profile_dir) -> None:
        self.order.add("sidecar.start")
        self.profile = profile_dir
        if FakeSidecar.fail_start is not None:
            raise FakeSidecar.fail_start

    def begin_close(self) -> None:
        self.order.add("sidecar.begin_close")

    async def stop(self) -> None:
        self.order.add("sidecar.stop")

    async def browser_exited(self, timeout: float) -> bool:
        FakeSidecar.timeouts.append(timeout)
        if FakeSidecar.exit_gate is not None:
            await FakeSidecar.exit_gate.wait()
        return FakeSidecar.answers.pop(0) if FakeSidecar.answers else False

    def has_pages(self) -> bool:
        return FakeSidecar.pages_attached

    def kill_browser(self) -> bool:
        self.order.add("sidecar.kill")
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A real BrowserSession on the cdp backend over fake driver and sidecar."""
    order = Order()
    FakeSidecar.instances = []
    FakeSidecar.fail_start = None
    FakeSidecar.answers = []
    FakeSidecar.timeouts = []
    FakeSidecar.pages_attached = True
    FakeSidecar.exit_gate = None
    FakeSidecar.order = order
    monkeypatch.setattr(session_mod, "CdpGuard", FakeSidecar)

    def make(*, backend: str = "cdp", headless: bool = False, real_ua: str | None = None):
        cfg = Config(headless=headless, guard_backend=backend, channel="chrome")
        cfg.data_dir = tmp_path
        cfg.__post_init__()
        cfg.capture_dir = tmp_path / "captures"
        session = BrowserSession(cfg)
        pw = FakePlaywright(order)

        async def start_playwright():
            return pw

        async def real_user_agent():
            return real_ua

        monkeypatch.setattr(session, "_start_playwright", start_playwright)
        monkeypatch.setattr(session, "_real_user_agent", real_user_agent)
        ctx = ServerContext(
            config=cfg,
            session=session,
            audit=AuditLog(cfg.audit_path),
            collab=CollaborationState(require_approval=True),
        )
        return SimpleNamespace(ctx=ctx, session=session, pw=pw, order=order, cfg=cfg)

    return make


async def lose(session: BrowserSession, sidecar: FakeSidecar, reason: str = "connection closed"):
    """The sidecar's reader reports the end of its connection; wait for the verdict."""
    sidecar.on_lost(reason)
    if session._loss_task is not None:
        await session._loss_task


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


async def test_cdp_backend_registers_no_route_and_guards_the_window_that_is_open(env):
    """The route is what DataDome refuses, so nothing may install it. And the sidecar
    has to watch the relaunched window (headless UA fix), not the one closed for it."""
    e = env(headless=True, real_ua=HEADLESS_UA)

    await e.session.start()

    assert [c.routes for c in e.pw.contexts] == [[], []], "no context.route on either launch"
    assert e.order.events == ["launch", "context.close", "launch", "sidecar.start"]
    assert FakeSidecar.instances[0].profile == e.cfg.profile_dir
    assert FakeSidecar.instances[0].judge is e.ctx.guard, "the one judgement the route uses"


async def test_route_backend_keeps_the_route_and_never_starts_a_sidecar(env):
    e = env(backend="route")

    await e.session.start()

    assert e.pw.contexts[0].routes == ["**/*"]
    assert FakeSidecar.instances == []


async def test_launch_clears_the_endpoint_file_of_the_last_run_and_closes_the_profile(env):
    """Chrome leaves ``DevToolsActivePort`` behind when it exits. Reading it before this
    run's Chrome has rewritten it would connect to a port that is someone else's now."""
    e = env()
    e.cfg.ensure_dirs()
    stale = e.cfg.profile_dir / "DevToolsActivePort"
    stale.write_text("1\n/devtools/browser/00000000-0000-0000-0000-000000000000")
    e.cfg.profile_dir.chmod(0o755)

    await e.session.start()

    assert e.pw.stale_endpoint_at_launch == [False]
    assert stat.S_IMODE(e.cfg.profile_dir.stat().st_mode) == 0o700, "the profile holds logins"


# --------------------------------------------------------------------------
# A guard that cannot start must not leave a window
# --------------------------------------------------------------------------


async def test_a_sidecar_that_cannot_start_closes_the_window_and_says_so(env):
    e = env()
    FakeSidecar.fail_start = CdpGuardError("no debugging endpoint")

    with pytest.raises(GuardLost) as raised:
        await e.session.start()

    assert raised.value.envelope()["status"] == "guard_lost"
    assert e.pw.contexts[0].closed, "an open window nobody guards is the one outcome to avoid"
    assert e.pw.stopped and not e.session.started
    assert [(r["status"], r["detail"]) for r in _audit(e)] == [
        ("guard_lost", "no debugging endpoint")
    ]


async def test_a_failed_start_is_not_sticky_the_next_call_tries_again(env):
    e = env()
    FakeSidecar.fail_start = CdpGuardError("setup timed out")
    with pytest.raises(GuardLost):
        await e.session.start()
    assert not e.session.live, "nothing is running, nothing is held"

    FakeSidecar.fail_start = None
    await e.session.start()

    assert e.session.started and len(FakeSidecar.instances) == 2
    assert e.session._guard_lost is None


async def test_open_browser_answers_guard_lost_when_the_sidecar_cannot_start(env):
    e = env()
    FakeSidecar.fail_start = CdpGuardError("setup timed out")

    page, envelope = await acquire_page(e.ctx)

    assert page is None
    assert envelope["status"] == "guard_lost"
    assert "close_browser" in envelope["hint"]


# --------------------------------------------------------------------------
# A guard lost while the browser runs
# --------------------------------------------------------------------------


async def test_a_lost_sidecar_kills_the_browser_before_the_tidy_close(env):
    e = env()
    await e.session.start()
    sidecar = FakeSidecar.instances[0]
    e.order.events.clear()

    await lose(e.session, sidecar)

    assert e.order.events == [
        "sidecar.kill",
        "sidecar.begin_close",
        "context.close",
        "sidecar.stop",
    ], (
        "Chrome has released everything it held; every moment it runs is a moment unjudged "
        "requests can leave, so it dies first and is tidied after"
    )
    assert e.pw.contexts[0].closed and e.pw.stopped
    assert not e.session.started, "no window is left"


async def test_after_a_loss_every_call_is_refused_whoever_asks_and_however(env):
    e = env()
    await e.session.start()
    await lose(e.session, FakeSidecar.instances[0], "connection closed")

    for revive in (True, False):
        with pytest.raises(GuardLost):
            await e.session.page(revive=revive)
    with pytest.raises(GuardLost):
        await e.session.start()
    for claim in (True, False):
        page, envelope = await acquire_page(e.ctx, claim=claim)
        assert page is None and envelope["status"] == "guard_lost"
    assert e.session.live, "the holder has not been told, and nobody else may take the browser"
    assert len(e.pw.launches) == 1, "and no call relaunched a browser behind the refusal"


async def test_the_loss_is_on_the_audit_trail_with_a_reason_and_nothing_else(env):
    e = env()
    await e.session.start()

    await lose(e.session, FakeSidecar.instances[0], "connection closed")

    assert [(r["status"], r["detail"]) for r in _audit(e)] == [("guard_lost", "connection closed")]


async def test_close_browser_acknowledges_a_lost_guard_and_the_next_browser_is_guarded(
    env, tools_of
):
    e = env()
    await e.session.start()
    await lose(e.session, FakeSidecar.instances[0])
    tools = await tools_of(e.ctx)

    closed = await tools["close_browser"](reason="the guard was lost")
    opened = await tools["open_browser"]()

    assert closed["status"] == "ok"
    assert opened["status"] == "ok"
    assert len(FakeSidecar.instances) == 2, "a fresh browser gets a fresh sidecar"
    assert e.session._guard_lost is None


async def test_a_second_session_is_told_the_browser_is_held_not_that_the_guard_is_lost(env):
    """The loss belongs to the session that held the browser; nobody else may take it, and
    nobody else learns more than that it is busy."""
    e = env()
    await e.session.start()
    held_by_someone_else = "holder"
    e.ctx.owner_session = held_by_someone_else
    e.ctx.owner_seen_at = time.monotonic()
    await lose(e.session, FakeSidecar.instances[0])

    page, envelope = await acquire_page(e.ctx)

    assert page is None
    assert envelope["status"] == "session_conflict"
    assert e.session.live


async def test_a_browser_that_quit_by_itself_is_not_a_lost_guard(env):
    """The user closes the last window: the socket ends because the browser did. That is
    ``browser_closed``, which a claiming call may repair — not ``guard_lost``."""
    e = env()
    await e.session.start()
    FakeSidecar.answers = [True]  # the process is already gone when the socket's end is seen

    await lose(e.session, FakeSidecar.instances[0])

    assert e.session._guard_lost is None
    assert "sidecar.kill" not in e.order.events
    assert not e.pw.contexts[0].closed, "closing what already left is the session's usual path"
    assert _audit(e) == []


async def test_a_page_still_attached_when_the_socket_ends_is_unguarded_and_the_browser_dies_at_once(
    env,
):
    """Chrome released everything it held when the socket ended. With a tab open that tab
    can send unjudged requests, so there is no moment to wait for the process to leave
    (measured: waiting 300 ms let a hostile page post 72 forms through)."""
    e = env()
    await e.session.start()

    await lose(e.session, FakeSidecar.instances[0])

    assert FakeSidecar.timeouts == [0], "asked once, without a grace"
    assert "sidecar.kill" in e.order.events and e.session._guard_lost is not None


async def test_a_browser_with_no_page_attached_is_given_a_moment_to_finish_closing(env):
    """The user closed the last window: the socket ends a few milliseconds before the
    process does, and no page is left that could send anything."""
    from lyra_browser.cdp_guard import EXIT_GRACE_S

    e = env()
    await e.session.start()
    FakeSidecar.pages_attached = False
    FakeSidecar.answers = [False, True]  # still running at the socket's end; gone in the grace

    await lose(e.session, FakeSidecar.instances[0])

    assert FakeSidecar.timeouts == [0, EXIT_GRACE_S]
    assert e.session._guard_lost is None and "sidecar.kill" not in e.order.events


async def test_a_browser_with_no_page_attached_that_does_not_leave_is_killed_after_the_moment(env):
    e = env()
    await e.session.start()
    FakeSidecar.pages_attached = False

    await lose(e.session, FakeSidecar.instances[0])

    assert len(FakeSidecar.timeouts) == 2
    assert "sidecar.kill" in e.order.events and e.session._guard_lost is not None


async def test_a_call_that_arrives_mid_verdict_waits_for_it(env):
    """The socket just ended; whether the browser is leaving or the guard failed is not
    known yet. A call must not be told either before it is."""
    e = env()
    await e.session.start()
    FakeSidecar.exit_gate = asyncio.Event()
    FakeSidecar.instances[0].on_lost("connection closed")
    call = asyncio.ensure_future(e.session.page())
    await asyncio.sleep(0.05)
    assert not call.done(), "it waits for the verdict"

    FakeSidecar.exit_gate.set()  # the browser did not exit: it was a failure of the guard
    with pytest.raises(GuardLost):
        await call


async def test_a_sidecar_from_an_earlier_browser_cannot_condemn_the_next_one(env):
    e = env()
    await e.session.start()
    old = FakeSidecar.instances[0]
    await e.session.stop()
    await e.session.start()

    await lose(e.session, old)  # a late callback from the browser that is gone

    assert e.session._guard_lost is None and e.session.started
    assert not e.pw.contexts[1].closed


async def test_a_loss_tidies_up_even_when_the_browser_will_not_close_cleanly(env):
    e = env()
    e.pw.close_error = RuntimeError("Browser.close: pipe is broken")
    await e.session.start()

    await lose(e.session, FakeSidecar.instances[0])

    assert e.session._guard_lost is not None
    assert e.session._context is None and e.session._pw is None, "state is cleared regardless"
    assert e.session._loss_task is None


# --------------------------------------------------------------------------
# An ordinary close
# --------------------------------------------------------------------------


async def test_stop_tells_the_sidecar_before_the_window_goes_and_stops_it_after(env):
    """The socket ends when the browser does; that must not read as a loss."""
    e = env()
    await e.session.start()
    e.order.events.clear()

    await e.session.stop()

    assert e.order.events == ["sidecar.begin_close", "context.close", "sidecar.stop"]


async def test_stop_clears_a_lost_guard_so_the_next_start_is_clean(env):
    e = env()
    await e.session.start()
    await lose(e.session, FakeSidecar.instances[0])

    await e.session.stop()
    await e.session.start()

    assert e.session.started and e.session._guard_lost is None


# --------------------------------------------------------------------------
# Launch options
# --------------------------------------------------------------------------


@pytest.mark.parametrize("headless", [False, True])
def test_cdp_launch_opens_a_debugging_port_and_drops_swiftshader(tmp_path, headless):
    cfg = Config(headless=headless, guard_backend="cdp")
    cfg.data_dir = tmp_path
    cfg.__post_init__()

    kwargs = launch_kwargs(cfg, "chrome")

    assert "--remote-debugging-port=0" in kwargs["args"], "random port, chosen by Chrome"
    assert kwargs["ignore_default_args"] == ["--enable-unsafe-swiftshader"]
    assert "--disable-blink-features=AutomationControlled" in kwargs["args"]
    assert not any(a.startswith("--remote-debugging-address") for a in kwargs["args"])


@pytest.mark.parametrize("headless", [False, True])
def test_route_launch_has_no_debugging_port(tmp_path, headless):
    cfg = Config(headless=headless, guard_backend="route")
    cfg.data_dir = tmp_path
    cfg.__post_init__()

    kwargs = launch_kwargs(cfg, "chrome")

    assert not any(a.startswith("--remote-debugging") for a in kwargs["args"])
    assert "ignore_default_args" not in kwargs, (
        "dropping swiftshader costs headful WebGL and buys nothing under the route, which "
        "DataDome refuses on its cache-disable alone (measured)"
    )


def test_guard_backend_from_the_environment(monkeypatch):
    assert Config.from_env().guard_backend == "route"
    monkeypatch.setenv("LYRA_BROWSER_GUARD", "cdp")
    assert Config.from_env().guard_backend == "cdp"
    monkeypatch.setenv("LYRA_BROWSER_GUARD", "something-else")
    assert Config.from_env().guard_backend == "route", "an unreadable value is the default"


def test_the_envelope_names_a_reason_and_the_way_out_and_no_endpoint():
    envelope = GuardLost("a target could not be guarded").envelope()

    assert envelope["status"] == "guard_lost"
    assert envelope["detail"] == "a target could not be guarded"
    assert "close_browser" in envelope["hint"] and "open_browser" in envelope["hint"]


def _audit(e) -> list[dict]:
    if not e.cfg.audit_path.exists():
        return []
    rows = [json.loads(line) for line in e.cfg.audit_path.read_text().splitlines() if line]
    return [r for r in rows if r["tool"] == "navigation"]


async def test_a_takeover_cannot_be_requested_from_a_window_that_was_closed(env, tools_of):
    """``request_takeover`` never touches the page, so it must meet a lost guard on its own."""
    e = env()
    e.cfg.headless = False
    await e.session.start()
    await lose(e.session, FakeSidecar.instances[0])
    tools = await tools_of(e.ctx)

    reply = await tools["request_takeover"](reason="the user should look")

    assert reply["status"] == "guard_lost"
    assert not e.ctx.collab.takeover, "no lock was taken on a window that is gone"
