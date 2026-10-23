"""The native driver's pieces that need no browser: wording, scripts, switches, routing.

The tools read driver errors by class name and message text, so the wording here is a
contract with ``actionable.describe_failure``, ``waiting``, ``capture``, ``navigation``
and ``session``; these tests feed native errors to those readers instead of restating
the strings. The real-browser behaviour is covered by
``testing/native_driver_smoke_261023_v1.py`` until the parity suite exists.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from lyra_browser import session as session_mod
from lyra_browser.cdp_guard import CdpGuard
from lyra_browser.config import Config
from lyra_browser.native import async_native
from lyra_browser.native.connection import Connection
from lyra_browser.native.errors import (
    Error,
    ProtocolError,
    TargetClosedError,
    timeout_error,
    with_call_log,
)
from lyra_browser.native.evaluate import call_argument, decode_result, is_function
from lyra_browser.native.launcher import (
    DEBUGGING_PORT,
    DEFAULT_SWITCHES,
    HEADLESS_SWITCHES,
    NO_SANDBOX,
    build_args,
    find_executable,
)
from lyra_browser.tools import collaboration, forms, interaction, reading, waiting
from lyra_browser.tools.actionable import describe_failure

# --------------------------------------------------------------------------
# Errors, as the tools read them
# --------------------------------------------------------------------------


def test_a_target_closed_error_reads_as_page_closed():
    reason, _hint, _detail = describe_failure(TargetClosedError())
    assert reason == "page_closed"


def test_a_timeouts_call_log_names_the_check_that_kept_failing():
    exc = timeout_error("Locator.click", 5000, ["waiting for element", "element is not visible"])
    reason, hint, detail = describe_failure(exc, 5000)
    assert reason == "element_not_actionable"
    assert "invisible" in hint
    assert detail.startswith("Locator.click: Timeout 5000ms exceeded.")


def test_a_bare_timeout_is_a_timeout():
    reason, _hint, _detail = describe_failure(timeout_error("Page.goto", 30000, []), 30000)
    assert reason == "timeout"


def test_a_native_error_counts_as_the_driver_speaking():
    exc = Error(with_call_log("Locator.click", "Error: strict", ["element is not enabled"]))
    assert describe_failure(exc) is not None


def test_an_unrelated_lyra_error_is_not_mistaken_for_the_driver():
    class Unrelated(Exception):
        pass

    assert describe_failure(Unrelated("element is not visible")) is None


def test_closed_and_missing_errors_carry_the_words_session_looks_for():
    assert session_mod._is_closed_error(TargetClosedError())
    with pytest.raises(Error) as missing:
        find_executable("definitely-not-a-channel")
    assert session_mod._is_missing_browser(missing.value)


def test_a_protocol_error_keeps_chromes_words_for_the_screenshot_retry():
    exc = ProtocolError("Page.captureScreenshot", "Unable to capture screenshot")
    assert "Unable to capture screenshot" in str(exc)
    assert str(exc).startswith("Protocol error (Page.captureScreenshot): ")


# --------------------------------------------------------------------------
# Scripts: every one lyra sends is read the way Playwright reads it
# --------------------------------------------------------------------------

_FUNCTIONS = [
    collaboration._HIGHLIGHT_JS,
    interaction._DESCRIBE_JS,
    interaction._SCROLL_STATE_JS,
    interaction._SCROLL_TO_JS,
    forms._FORM_JS,
    forms._SET_HTML_JS,
    forms._SYNC_EDITOR_JS,
    forms._AFTER_SEND_JS,
    forms._DRAFT_JS,
    forms._IS_FRAME_JS,
    forms._PUBLISH_STATE_JS,
    reading._LINKS_JS,
    waiting._TEXT_JS,
    waiting._EXCERPT_JS,
    "async (x) => x",
    "function named(a) { return a }",
]


@pytest.mark.parametrize("source", _FUNCTIONS)
def test_lyras_functions_are_called(source):
    assert is_function(source)


@pytest.mark.parametrize(
    "source", [reading._VIEWPORT_JS, "document.title", "1 + 1", "(1 + 2) * 3", "window.x => y"]
)
def test_expressions_are_evaluated_as_they_stand(source):
    assert not is_function(source)


def test_results_decode_like_playwrights():
    assert decode_result("Page.evaluate", {"result": {"type": "undefined"}}) is None
    assert decode_result("Page.evaluate", {"result": {"value": [1, "a"]}}) == [1, "a"]
    assert decode_result("Page.evaluate", {"result": {"unserializableValue": "12n"}}) == 12
    with pytest.raises(Error, match=r"^Page.evaluate: ReferenceError: x is not defined"):
        decode_result(
            "Page.evaluate",
            {
                "exceptionDetails": {
                    "exception": {"description": "ReferenceError: x is not defined"}
                }
            },
        )


def test_arguments_are_json_values():
    assert call_argument({"cap": 100}) == {"value": {"cap": 100}}
    assert call_argument(float("inf")) == {"unserializableValue": "Infinity"}


# --------------------------------------------------------------------------
# Chrome's command line
# --------------------------------------------------------------------------


def _options(**extra) -> dict:
    return {
        "user_data_dir": "/tmp/profile",
        "args": ["--disable-blink-features=AutomationControlled"],
        **extra,
    }


def test_switches_keep_the_profile_readable_and_the_guard_whole():
    args = build_args(_options())
    for switch in (
        "--use-mock-keychain",
        "--password-store=basic",
        "--disable-back-forward-cache",
        "--disable-popup-blocking",
    ):
        assert switch in args, switch
    assert DEBUGGING_PORT in args
    assert "--user-data-dir=/tmp/profile" in args
    assert args[-1] == "about:blank"


def test_the_sandbox_stays_on_unless_turned_off():
    assert NO_SANDBOX not in build_args(_options())
    assert NO_SANDBOX in build_args(_options(chromium_sandbox=False))


def test_no_automation_flag_and_no_swiftshader():
    args = build_args(_options(ignore_default_args=["--enable-unsafe-swiftshader"]))
    assert "--enable-automation" not in args
    assert "--enable-unsafe-swiftshader" not in args


def test_headless_adds_playwrights_headless_switches_and_the_ua_override():
    args = build_args(_options(headless=True, user_agent="Mozilla/5.0 Chrome/155"))
    assert all(s in args for s in HEADLESS_SWITCHES)
    assert "--user-agent=Mozilla/5.0 Chrome/155" in args
    assert not any(s in build_args(_options()) for s in HEADLESS_SWITCHES)


def test_ignored_defaults_and_proxy():
    args = build_args(
        _options(ignore_default_args=["--disable-sync"], proxy={"server": "socks5://h:1"})
    )
    assert "--disable-sync" not in args
    assert "--proxy-server=socks5://h:1" in args
    assert len(DEFAULT_SWITCHES) == len(set(DEFAULT_SWITCHES))


def test_the_debugging_port_is_not_added_twice():
    args = build_args(_options(args=[DEBUGGING_PORT]))
    assert args.count(DEBUGGING_PORT) == 1


# --------------------------------------------------------------------------
# The connection: ids, sessions, events, the end
# --------------------------------------------------------------------------


class FakeSocket:
    """``cdp_socket.WebSocket``'s three calls, with Chrome played by the test."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()

    def send_nowait(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def recv(self) -> str:
        item = await self._inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    def push(self, message: dict) -> None:
        self._inbox.put_nowait(json.dumps(message))

    def end(self) -> None:
        self._inbox.put_nowait(ConnectionError("gone"))

    async def close(self) -> None:
        self.end()


async def _until(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never held")


async def test_replies_find_their_command_and_errors_become_protocol_errors():
    ws = FakeSocket()
    conn = Connection(ws)
    first = asyncio.ensure_future(conn.send("Page.enable", session="S1"))
    second = asyncio.ensure_future(conn.send("Page.reload"))
    await _until(lambda: len(ws.sent) == 2)
    assert ws.sent[0]["sessionId"] == "S1" and "sessionId" not in ws.sent[1]
    ws.push({"id": ws.sent[1]["id"], "error": {"code": -32000, "message": "nope"}})
    ws.push({"id": ws.sent[0]["id"], "result": {"ok": True}})
    assert await first == {"ok": True}
    with pytest.raises(ProtocolError, match=r"Protocol error \(Page.reload\): nope"):
        await second
    await conn.close()


async def test_events_reach_handlers_of_their_session_and_a_bad_handler_is_contained():
    ws = FakeSocket()
    conn = Connection(ws)
    seen: list[tuple[str, str | None]] = []
    conn.on("Page.loadEventFired", lambda p, s: seen.append(("S1", s)), session="S1")
    conn.on("Page.loadEventFired", lambda p, s: seen.append(("any", s)))
    conn.on("Page.loadEventFired", lambda p, s: 1 / 0)
    ws.push({"method": "Page.loadEventFired", "params": {}, "sessionId": "S2"})
    ws.push({"method": "Page.loadEventFired", "params": {}, "sessionId": "S1"})
    await _until(lambda: len(seen) == 3)
    assert seen == [("any", "S2"), ("S1", "S1"), ("any", "S1")]
    assert conn.handler_errors and "ZeroDivisionError" in conn.handler_errors[0]
    await conn.close()


async def test_the_end_of_the_stream_fails_every_waiting_call_as_target_closed():
    ws = FakeSocket()
    conn = Connection(ws)
    closed: list[bool] = []
    conn.on_close(lambda: closed.append(True))
    pending = asyncio.ensure_future(conn.send("Page.navigate", {"url": "x"}))
    await _until(lambda: ws.sent)
    ws.end()
    with pytest.raises(TargetClosedError):
        await pending
    assert closed == [True]
    with pytest.raises(TargetClosedError):
        await conn.send("Page.enable")


# --------------------------------------------------------------------------
# Wiring: config, the driver choice, the guard's endpoint
# --------------------------------------------------------------------------


def test_native_brings_the_cdp_guard_with_it():
    cfg = Config(driver="native", guard_backend="route")
    assert cfg.driver == "native" and cfg.guard_backend == "cdp"


def test_sandbox_setting_reaches_launch_options_for_native_only(tmp_path):
    cfg = Config(driver="native", sandbox=False)
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    assert session_mod.launch_kwargs(cfg, "chrome")["chromium_sandbox"] is False
    cfg.sandbox = True
    assert "chromium_sandbox" not in session_mod.launch_kwargs(cfg, "chrome")
    other = Config(driver="playwright", sandbox=False)
    assert "chromium_sandbox" not in session_mod.launch_kwargs(other, "chrome")


async def test_load_driver_hands_back_the_native_driver_by_name_only():
    name, starter = session_mod.load_driver("native")
    assert name == "native" and starter is async_native
    driver = await starter().start()
    assert hasattr(driver.chromium, "launch_persistent_context")
    await driver.stop()
    assert session_mod._DRIVER_ORDER["auto"] == ("patchright", "playwright")


async def test_the_guard_uses_a_handed_endpoint_instead_of_reading_the_file(monkeypatch, tmp_path):
    async def must_not_read(*_a, **_k):
        raise AssertionError("read_endpoint was called")

    connected: list[tuple] = []

    class Stop(Exception):
        pass

    async def fake_connect(host, port, path, **_kw):
        connected.append((host, port, path))
        raise Stop

    monkeypatch.setattr("lyra_browser.cdp_guard.read_endpoint", must_not_read)
    monkeypatch.setattr("lyra_browser.cdp_guard.WebSocket.connect", fake_connect)
    guard = CdpGuard(judge=object(), on_lost=lambda _r: None)  # type: ignore[arg-type]
    with pytest.raises(Stop):
        await guard.start(Path(tmp_path), endpoint=(4242, "/devtools/browser/abc"))
    assert connected == [("127.0.0.1", 4242, "/devtools/browser/abc")]


# --------------------------------------------------------------------------
# CKEditor scripts reach the page's own world on every driver
# --------------------------------------------------------------------------


def test_main_world_arguments_follow_the_drivers_signature():
    from lyra_browser.tools.forms import _main_world

    class Native:
        async def evaluate(self, expression, arg=None, *, world=None): ...

    class Patchright:
        async def evaluate(self, expression, arg=None, isolated_context=True): ...

    class Playwright:
        async def evaluate(self, expression, arg=None): ...

    assert _main_world(Native()) == {"world": "main"}
    assert _main_world(Patchright()) == {"isolated_context": False}
    assert _main_world(Playwright()) == {}


@pytest.mark.parametrize(
    ("output", "held"),
    [
        ("Opening in existing browser session.", True),
        (
            "[ERROR:process_singleton_posix.cc] Failed to create /p/SingletonLock",
            True,
        ),
        ("[1:2:ERROR:gpu_init.cc] something else", False),
        ("", False),
    ],
)
def test_a_held_profile_is_recognised_from_what_chrome_printed(output, held):
    from lyra_browser.native.launcher import _names_a_held_profile

    assert _names_a_held_profile(output) is held
