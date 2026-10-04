"""``wait_for`` and what ``navigate`` now reports.

Two holes are kept shut here. A page that draws itself after load looked empty
to ``read_page`` and the only stand-in for waiting was a click; and ``navigate``
awaited ``goto`` and dropped its response, so a 404, a 500 and a PDF all came back
as a bare ``ok``.

The driver owns the waiting, so the fake stands in for its answers and these tests
pin what the tool does around them: the clock it puts on a wait, what it makes of
the driver's verdict, what it refuses to ask at all, and that it stays a read.
Whether the conditions themselves hold in a real browser is
``scripts/verify_wait_e2e.py``'s question.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from conftest import FakeLocator, FakePage
from lyra_browser import downloads
from lyra_browser.tools import waiting
from lyra_browser.tools.waiting import _EXCERPT_JS, _TEXT_JS

WAIT_CALLS = {"wait_for_function", "wait_for_url", "wait_for_load_state", "wait_for"}
START = "https://start.example/"


class DriverError(Exception):
    """Stands in for the driver's base error."""


# Named like the driver's own: the tool recognises a timeout by class name because
# playwright and patchright each ship a TimeoutError of their own.
DriverTimeout = type("TimeoutError", (DriverError,), {})


class FakeResponse:
    def __init__(self, status: int = 200, content_type: str | None = "text/html") -> None:
        self.status = status
        self.headers = {} if content_type is None else {"Content-Type": content_type}


class WaitLocator(FakeLocator):
    async def wait_for(self, *, state: str | None = None, timeout: int | None = None) -> None:
        page, selector = self._page, self._selector
        page.calls.append(("wait_for", selector, state, timeout))
        gone = not page.has(selector)
        holds = {
            "visible": not gone and selector not in page.hidden,
            "hidden": gone or selector in page.hidden,
            "attached": not gone,
            "detached": gone,
        }[state]
        await page.settle(timeout, holds)


class WaitPage(FakePage):
    """The base fake plus the waits a real page offers and the response it returns."""

    def __init__(self) -> None:
        super().__init__()
        # Whether the page ever gets to what is awaited (selectors decide for
        # themselves, from ``present`` and ``hidden``).
        self.settles = True
        # How long the driver takes to answer; None means it uses the whole
        # timeout it was given, which is what a real driver does before it gives up.
        self.takes_s: float | None = 0.0
        self.driver_error: Exception | None = None
        # What goto/go_back/reload resolve with, and the wait_until each was given.
        self.response: FakeResponse | None = None
        self.wait_untils: list[str | None] = []
        # When set, the excerpt read never answers.
        self.text_hangs = False

    def locator(self, selector: str) -> WaitLocator:
        return WaitLocator(self, selector)

    async def settle(self, timeout: int | None, holds: bool | None = None) -> None:
        await asyncio.sleep((timeout or 0) / 1000 if self.takes_s is None else self.takes_s)
        if self.driver_error is not None:
            raise self.driver_error
        if not (self.settles if holds is None else holds):
            raise DriverTimeout(f"Timeout {timeout}ms exceeded.\nCall log:\n  - waiting")

    async def wait_for_function(self, expression, arg=None, timeout=None, polling=None) -> None:
        self.calls.append(("wait_for_function", expression, arg, timeout, polling))
        await self.settle(timeout)

    async def wait_for_url(self, url, timeout=None, wait_until=None) -> None:
        self.calls.append(("wait_for_url", url, timeout, wait_until))
        await self.settle(timeout)

    async def wait_for_load_state(self, state=None, timeout=None) -> None:
        self.calls.append(("wait_for_load_state", state, timeout))
        await self.settle(timeout)

    async def evaluate(self, expression, arg=None):
        result = await super().evaluate(expression, arg)
        if expression != _EXCERPT_JS:
            return result
        if self.text_hangs:
            await asyncio.sleep(60)
        return self.text

    async def goto(self, url, wait_until=None):
        await super().goto(url, wait_until)
        self.wait_untils.append(wait_until)
        return self.response

    async def go_back(self, wait_until=None):
        await super().go_back(wait_until)
        self.wait_untils.append(wait_until)
        return self.response

    async def reload(self, wait_until=None):
        await super().reload(wait_until)
        self.wait_untils.append(wait_until)
        return self.response


@pytest.fixture
def page() -> WaitPage:
    return WaitPage()


def _waits(page: WaitPage) -> list[tuple]:
    return [call for call in page.calls if call[0] in WAIT_CALLS]


def _audit(ctx) -> list[dict]:
    return [json.loads(line) for line in ctx.config.audit_path.read_text().splitlines()]


# --------------------------------------------------------------------------
# wait_for: a met condition, and nothing else
# --------------------------------------------------------------------------


async def test_a_met_text_wait_reports_ok_and_does_nothing_else(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](text="Item 5")

    assert result["status"] == "ok"
    assert isinstance(result["waited_ms"], int)
    # Only the wait reached the page: no click, navigation or typing stood in for it.
    (call,) = page.calls
    assert call[:4] == ("wait_for_function", _TEXT_JS, {"text": "Item 5", "present": True}, 10000)
    # Polled by number, not the driver's per-frame default, which stalls in a
    # background tab.
    assert isinstance(call[4], int | float) and call[4] > 0


async def test_a_hidden_text_wait_waits_for_the_phrase_to_go(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    await tools["wait_for"](text="Loading...", state="hidden")

    (call,) = _waits(page)
    assert call[2] == {"text": "Loading...", "present": False}


# --------------------------------------------------------------------------
# wait_for: the clock
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asked", "given"), [(999_999, 30_000), (30_001, 30_000), (30_000, 30_000), (2_500, 2_500)]
)
async def test_the_timeout_is_capped_at_thirty_seconds(make_ctx, tools_of, page, asked, given):
    """The driver reads no limit as forever, and a model will ask for an hour."""
    page.settles = False
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](text="never", timeout_ms=asked)

    assert result["status"] == "timeout"
    assert _waits(page)[0][3] == given


async def test_a_timeout_is_an_answer_with_the_time_actually_spent(make_ctx, tools_of, page):
    page.settles = False
    page.takes_s = 0.06
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](text="Item 5", timeout_ms=5000)

    assert result["status"] == "timeout"
    # Measured, not the 5000 that was asked for.
    assert 50 <= result["waited_ms"] < 2000


async def test_a_selector_that_never_appears_returns_inside_the_timeout(make_ctx, tools_of, page):
    page.present = set()  # nothing on this page matches anything
    page.takes_s = None  # the driver uses up whatever it was given
    tools = await tools_of(make_ctx())

    started = time.monotonic()
    result = await tools["wait_for"](selector="#nope", timeout_ms=150)
    elapsed_ms = (time.monotonic() - started) * 1000

    # A tool that handed the driver more than it was given, or waited again once
    # the driver had given up, would overshoot this.
    assert result["status"] == "timeout"
    assert 140 <= result["waited_ms"] < 150 + 1000
    assert elapsed_ms < 150 + 1000


@pytest.mark.parametrize(
    "driver_timeout",
    [
        DriverTimeout,
        TimeoutError,
        # A driver is free to specialise its timeout; it is still one.
        type("NavigationTimeout", (DriverTimeout,), {}),
    ],
    ids=["driver", "builtin", "subclass"],
)
async def test_any_drivers_timeout_class_counts_as_a_timeout(
    make_ctx, tools_of, page, driver_timeout
):
    page.driver_error = driver_timeout("gave up")
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](text="x")

    assert result["status"] == "timeout"


async def test_an_error_from_the_driver_is_reported_with_its_first_line(make_ctx, tools_of, page):
    first = 'Locator.wait_for: Unexpected token "" while parsing css selector "div["'
    page.driver_error = DriverError(f"{first}\nCall log:\n  - waiting for locator")
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](selector="div[")

    assert result == {"status": "error", "reason": first}


# --------------------------------------------------------------------------
# wait_for: what a timeout shows
# --------------------------------------------------------------------------


async def test_a_timeout_shows_the_head_of_the_page_text(make_ctx, tools_of, page):
    page.settles = False
    page.text = "Loading\n\n  items...   please   wait"
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](text="Item 5", timeout_ms=50)

    assert result["last_seen"] == "Loading items... please wait"


async def test_a_long_page_is_cut_to_a_short_excerpt(make_ctx, tools_of, page):
    page.settles = False
    page.text = "word " * 500
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](text="Item 5", timeout_ms=50)

    assert result["last_seen"].endswith("…")
    assert len(result["last_seen"]) == waiting._EXCERPT_CHARS + 1


async def test_a_url_wait_that_times_out_says_where_the_tab_is(make_ctx, tools_of, page):
    page.settles = False
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](url="**/checkout**", timeout_ms=50)

    assert result["status"] == "timeout"
    assert result["last_seen"] == START
    assert not any(call[0] == "evaluate" for call in page.calls), "the URL is the answer"


async def test_a_blank_page_is_reported_by_its_url(make_ctx, tools_of, page):
    page.settles = False
    page.text = "  \n "
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](text="Item 5", timeout_ms=50)

    assert result["last_seen"] == START


async def test_a_page_that_will_not_give_its_text_does_not_stretch_the_timeout(
    make_ctx, tools_of, page, monkeypatch
):
    """A blocked page is when a wait is most likely to time out — and the last
    place a second, unbounded read should be trusted."""
    monkeypatch.setattr(waiting, "_EXCERPT_BUDGET_S", 0.05)
    page.settles = False
    page.text_hangs = True
    tools = await tools_of(make_ctx())

    started = time.monotonic()
    result = await tools["wait_for"](text="Item 5", timeout_ms=50)

    assert time.monotonic() - started < 2
    assert result["status"] == "timeout"
    assert result["last_seen"] == START


# --------------------------------------------------------------------------
# wait_for: arguments that are not a wait
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "given"),
    [
        ({}, []),
        ({"text": "a", "selector": "#b"}, ["text", "selector"]),
        ({"url": "**/x**", "load_state": "load", "text": "t"}, ["text", "url", "load_state"]),
        ({"text": "  "}, []),
    ],
)
async def test_exactly_one_condition_is_required(make_ctx, tools_of, page, kwargs, given):
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](**kwargs)

    assert result["status"] == "error"
    assert result["given"] == given
    assert page.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"selector": "#x", "state": "gone"},
        {"text": "x", "state": "attached"},
        {"url": "**/x**", "state": "hidden"},
        {"load_state": "commit"},
        {"url": "/dashboard"},
        {"text": "x", "timeout_ms": 0},
    ],
)
async def test_arguments_the_driver_could_not_honour_are_refused_before_it_is_asked(
    make_ctx, tools_of, page, kwargs
):
    """Each of these would otherwise cost a full timeout to say "no" — or, for a
    timeout of 0, never come back."""
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](**kwargs)

    assert result["status"] == "error"
    assert result["reason"]
    assert page.calls == []


@pytest.mark.parametrize("pattern", ["https://site.example/cart", "**/checkout**", "about:blank"])
async def test_a_url_that_can_match_is_not_refused(make_ctx, tools_of, pattern):
    tools = await tools_of(make_ctx())

    result = await tools["wait_for"](url=pattern)

    assert result["status"] == "ok"


# --------------------------------------------------------------------------
# wait_for: a read, not an action
# --------------------------------------------------------------------------


async def test_wait_for_needs_no_approval_and_carries_on_through_a_takeover(make_ctx, tools_of):
    ctx = make_ctx(on_site=False)  # no grant on this site: any gate would refuse
    ctx.collab.takeover = True
    tools = await tools_of(ctx)

    result = await tools["wait_for"](text="x")

    assert result["status"] == "ok"
    assert ctx.owner_session is None, "waiting is not driving: it must not claim the browser"


async def test_wait_for_answers_to_whoever_holds_the_browser(make_ctx, tools_of, page):
    ctx = make_ctx()
    ctx.owner_session = "someone-else"
    ctx.owner_seen_at = time.monotonic()
    tools = await tools_of(ctx)

    result = await tools["wait_for"](text="x")

    assert result["status"] == "session_conflict"
    assert page.calls == []


async def test_the_audit_keeps_the_condition_but_not_a_url_secret(make_ctx, tools_of, page):
    page.settles = False
    ctx = make_ctx()
    tools = await tools_of(ctx)

    await tools["wait_for"](url="https://site.example/cb?token=s3cret**", timeout_ms=30)

    (entry,) = (e for e in _audit(ctx) if e["tool"] == "wait_for")
    assert entry["status"] == "timeout"
    assert entry["args"]["timeout_ms"] == 30
    assert "s3cret" not in json.dumps(entry)


# --------------------------------------------------------------------------
# navigate / go_back / reload_page: what the server answered
# --------------------------------------------------------------------------


async def test_navigate_reports_the_status_the_server_answered_with(make_ctx, tools_of, page):
    """A 404 is still a navigation that happened — ``ok`` — so the status has to
    ride along, or it cannot be told apart from the page the caller wanted."""
    page.response = FakeResponse(404)
    tools = await tools_of(make_ctx())

    result = await tools["navigate"](url="https://start.example/x")

    assert result["status"] == "ok"
    assert result["http_status"] == 404


@pytest.mark.parametrize(
    ("header", "reported"),
    [
        ("application/pdf", "application/pdf"),
        ("Text/HTML; charset=UTF-8", "text/html"),
        ("  image/PNG ", "image/png"),
        (None, None),
    ],
)
async def test_navigate_reports_the_media_type_lower_cased(
    make_ctx, tools_of, page, header, reported
):
    page.response = FakeResponse(content_type=header)
    tools = await tools_of(make_ctx())

    result = await tools["navigate"](url="https://start.example/doc")

    assert result["content_type"] == reported


async def test_a_navigation_with_no_http_response_reports_none(make_ctx, tools_of, page):
    page.response = None  # what about:blank and data: resolve with
    tools = await tools_of(make_ctx())

    result = await tools["navigate"](url="about:blank", confirm=True)

    assert result["status"] == "ok"
    assert result["http_status"] is None
    assert result["content_type"] is None


async def test_the_status_zero_a_driver_may_report_is_not_an_http_status(make_ctx, tools_of, page):
    page.response = FakeResponse(status=0)
    tools = await tools_of(make_ctx())

    result = await tools["navigate"](url="https://start.example/x")

    assert result["http_status"] is None


async def test_navigate_still_waits_for_the_document_by_default(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    await tools["navigate"](url="https://start.example/x")

    assert page.wait_untils == ["domcontentloaded"]


async def test_navigate_waits_as_far_as_it_was_asked(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    await tools["navigate"](url="https://start.example/x", wait_until="networkidle")

    assert page.wait_untils == ["networkidle"]


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("navigate", {"url": "https://other.example/"}),
        ("go_back", {}),
        ("reload_page", {}),
    ],
)
async def test_an_unknown_wait_until_is_refused_before_anything_happens(
    make_ctx, tools_of, page, name, kwargs
):
    """Before the approval prompt too: a typo must not cost the user a question,
    and the cross-site URL here would otherwise have been asked about."""
    ctx = make_ctx()
    tools = await tools_of(ctx)

    result = await tools[name](**kwargs, wait_until="idle")

    assert result["status"] == "error"
    assert result["given"] == "idle"
    assert page.calls == []
    assert not ctx.config.audit_path.exists()


@pytest.mark.parametrize("name", ["go_back", "reload_page"])
async def test_going_back_and_reloading_report_what_the_server_answered(
    make_ctx, tools_of, page, name
):
    page.response = FakeResponse(500, "text/html; charset=utf-8")
    tools = await tools_of(make_ctx())

    result = await tools[name](wait_until="load")

    assert result["status"] == "ok"
    assert (result["http_status"], result["content_type"]) == (500, "text/html")
    assert page.wait_untils == ["load"]


@pytest.mark.parametrize("name", ["go_back", "reload_page"])
async def test_history_steps_with_no_response_report_none(make_ctx, tools_of, page, name):
    """A page restored from the back/forward cache never asks the server."""
    tools = await tools_of(make_ctx())

    result = await tools[name]()

    assert result["status"] == "ok"
    assert result["http_status"] is None
    assert result["content_type"] is None


# --------------------------------------------------------------------------
# navigate: a download is a result, not a crash
# --------------------------------------------------------------------------

DOWNLOAD = 'Page.goto: Download is starting\nCall log:\n  - navigating to "{}", waiting until'


@pytest.mark.parametrize("name", ["navigate", "go_back", "reload_page"])
async def test_a_download_comes_back_as_a_result_and_saves_nothing(
    make_ctx, tools_of, page, name, monkeypatch
):
    """The driver says a download is starting and the browser never reports it, so
    there is nothing to cancel and nothing to claim. (When the browser does report
    it, ``tests/test_downloads.py`` has the replies.)"""
    monkeypatch.setattr(downloads, "EVENT_WAIT_S", 0.05)
    ctx = make_ctx()
    url = "https://start.example/report.zip"

    async def starts_a_download(*args, **kwargs):
        page.calls.append((name,))
        raise RuntimeError(DOWNLOAD.format(url))

    for method in ("goto", "go_back", "reload"):
        setattr(page, method, starts_a_download)
    tools = await tools_of(ctx)
    kwargs = {"url": url} if name == "navigate" else {}

    result = await tools[name](**kwargs)

    assert result["status"] == "download_started"
    assert result["url"] == START, "the tab did not move"
    if name == "navigate":
        assert result["requested_url"] == url
    else:
        assert "requested_url" not in result
    # The attempt and nothing else: no download listener, no save, no capture.
    assert page.calls == [(name,)]
    assert _audit(ctx)[-1]["status"] == "download_started"


async def test_only_a_download_is_swallowed_other_navigation_errors_still_raise(
    make_ctx, tools_of, page
):
    async def unreachable(url, wait_until=None):
        raise RuntimeError("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://nowhere.invalid/")

    page.goto = unreachable
    tools = await tools_of(make_ctx())

    with pytest.raises(RuntimeError, match="ERR_NAME_NOT_RESOLVED"):
        await tools["navigate"](url="https://start.example/x")
