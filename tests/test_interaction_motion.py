"""hover and scroll: move the pointer, move the page, and say where it ended.

Each test names a way these tools could quietly go wrong: hovering something that
is not there and asking permission for it anyway, a wheel number that scrolls a
whole feed, a reply that reports the position before the page finished moving, or
a scroll that did nothing and said "ok". They assert on what reached the page
(``page.calls``) and on what was left behind (grants), not only on the envelope.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from playwright.async_api import Error as DriverError

from conftest import FakeLocator, FakePage
from lyra_browser.context import current_session_key
from lyra_browser.tools import actionable, interaction
from lyra_browser.tools.interaction import _DESCRIBE_JS, MAX_SCROLL_PX

COVERED_LOG = """Locator.hover: Timeout 10000ms exceeded.
Call log:
  - waiting for locator("#menu").first
    - locator resolved to <a id="menu" href="#">Menu</a>
  - attempting hover action
    2 × waiting for element to be visible and stable
      - element is visible and stable
      - scrolling into view if needed
      - done scrolling
      - <div id="overlay"></div> intercepts pointer events
    - retrying hover action
"""


def _audit(ctx) -> list[dict]:
    return [json.loads(line) for line in Path(ctx.config.audit_path).read_text().splitlines()]


def _own_entries(ctx, tool: str) -> list[dict]:
    """The tool's own audit lines, not the consent broker's (which carry no ``to``/``selector``)."""
    return [e for e in _audit(ctx) if e["tool"] == tool and set(e["args"]) & {"to", "selector"}]


def _grants(ctx) -> list:
    return ctx.perms.live_grants(current_session_key(ctx))


def _calls(page: FakePage, *names: str) -> list[tuple]:
    return [c for c in page.calls if c[0] in names]


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    """A missing control is reported once the appear-wait runs out, and a scroll
    settles by waiting; these tests are about what happens next."""
    monkeypatch.setattr(actionable, "_APPEAR_BUDGET_S", 0.0)
    monkeypatch.setattr(interaction, "_SETTLE_STEP_S", 0.001)


# --------------------------------------------------------------------------
# hover
# --------------------------------------------------------------------------


async def test_hover_moves_the_pointer_over_the_first_match(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["hover"](selector="#menu")

    assert result["status"] == "ok"
    assert result["url"] == page.url
    assert _calls(page, "hover") == [("hover", "#menu")]
    assert _calls(page, "click", "fill", "key", "wheel") == [], "hovering presses nothing"


@pytest.mark.parametrize(("asked", "handed"), [(0, 1), (-5, 1), (2500, 2500), (10**9, 30_000)])
async def test_hover_hands_the_driver_a_bounded_timeout(
    make_ctx, tools_of, monkeypatch, asked, handed
):
    """To Playwright a timeout of 0 means *no* timeout; a caller must not get it."""
    seen: list[float | None] = []
    original = FakeLocator.hover

    async def spy(self, timeout=None):
        seen.append(timeout)
        await original(self, timeout)

    monkeypatch.setattr(FakeLocator, "hover", spy)
    await (await tools_of(make_ctx()))["hover"](selector="#menu", timeout_ms=asked)

    assert seen == [handed]


async def test_hover_reports_what_was_hovered_and_how_many_matched(
    make_ctx, tools_of, page, monkeypatch
):
    original = FakeLocator.evaluate

    async def evaluate(self, expression, arg=None):
        if expression == _DESCRIBE_JS:
            return {"tag": "a", "role": "link", "name": "  Products \n menu "}
        return await original(self, expression, arg)

    async def three(self):
        return 3

    monkeypatch.setattr(FakeLocator, "evaluate", evaluate)
    monkeypatch.setattr(FakeLocator, "count", three)
    result = await (await tools_of(make_ctx()))["hover"](selector="a.menu")

    assert result["hovered"] == {"tag": "a", "role": "link", "name": "Products menu"}
    assert "clicked" not in result, "nothing was clicked"
    assert result["matches"] == 3
    assert "first was hovered" in result["hint"] and "nth=" in result["hint"]


async def test_hover_is_audited(make_ctx, tools_of):
    ctx = make_ctx()
    await (await tools_of(ctx))["hover"](selector="#menu")

    entries = _own_entries(ctx, "hover")
    assert [e["args"] for e in entries] == [{"selector": "#menu"}]
    assert entries[0]["origin"] == "https://start.example"


async def test_a_hover_the_driver_cannot_perform_comes_back_as_an_envelope(
    make_ctx, tools_of, monkeypatch
):
    ctx = make_ctx()

    async def covered(self, timeout=None):
        raise DriverError(COVERED_LOG)

    monkeypatch.setattr(FakeLocator, "hover", covered)
    result = await (await tools_of(ctx))["hover"](selector="#menu")

    assert result["status"] == "element_not_actionable"
    assert "overlay" in result["hint"]
    assert _own_entries(ctx, "hover")[0]["status"] == "element_not_actionable"


# --------------------------------------------------------------------------
# hover / scroll(selector): an unusable target is named before anyone is asked
# --------------------------------------------------------------------------

BY_SELECTOR = [("hover", {"selector": "#menu"}), ("scroll", {"selector": "#menu"})]


def _make_unusable(page, reason: str) -> None:
    page.present = {"#menu"}
    if reason == "not_found":
        page.present = {"#elsewhere"}
    elif reason == "hidden":
        page.hidden = {"#menu"}
    else:
        page.disabled = {"#menu"}


@pytest.mark.parametrize(("tool", "kwargs"), BY_SELECTOR)
@pytest.mark.parametrize("reason", ["not_found", "hidden", "disabled"])
async def test_an_unusable_target_is_named_without_buying_a_grant(
    make_ctx, tools_of, page, tool, kwargs, reason
):
    """On a site the agent is not approved for, ``confirm=True`` would have bought
    an INTERACT lease before the action found out it could not run."""
    ctx = make_ctx(on_site=False)
    _make_unusable(page, reason)
    result = await (await tools_of(ctx))[tool](**kwargs, confirm=True)

    assert result["status"] == reason
    assert result["selector"] == "#menu"
    assert result["hint"]
    assert page.calls == []
    assert _grants(ctx) == []


async def test_a_missing_target_is_named_within_the_appear_window(
    make_ctx, tools_of, page, monkeypatch
):
    """Not the driver's 30s: the window is short and shared with the wait to appear."""
    monkeypatch.setattr(actionable, "_APPEAR_BUDGET_S", 0.2)
    monkeypatch.setattr(actionable, "_APPEAR_STEP_S", 0.02)
    page.present = set()
    started = time.monotonic()
    result = await (await tools_of(make_ctx()))["hover"](selector="#menu")

    assert result["status"] == "not_found"
    assert time.monotonic() - started < 1.0


# --------------------------------------------------------------------------
# scroll: which arguments mean what
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"to": "bottom", "by_y": 100},
        {"to": "top", "selector": "#footer"},
        {"by_y": 100, "selector": "#footer"},
        {"to": "middle"},
        {"to": "BOTTOM"},
    ],
)
async def test_scroll_wants_exactly_one_thing_to_do(make_ctx, tools_of, page, kwargs):
    ctx = make_ctx(on_site=False)
    result = await (await tools_of(ctx))["scroll"](**kwargs, confirm=True)

    assert result["status"] == "error"
    assert result["reason"]
    assert page.calls == [], "an ambiguous scroll must not pick one of its readings"
    assert _grants(ctx) == []


async def test_scroll_to_bottom_jumps_to_the_end_and_says_so(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["scroll"](to="bottom")

    assert _calls(page, "scroll_to") == [("scroll_to", "bottom")]
    assert result == {
        "status": "ok",
        "url": page.url,
        "scroll_y": 4200,
        "scroll_height": 5000,
        "at_bottom": True,
    }


async def test_scroll_to_top_returns_to_the_start(make_ctx, tools_of, page):
    page.scroll_y = 3000
    result = await (await tools_of(make_ctx()))["scroll"](to="top")

    assert _calls(page, "scroll_to") == [("scroll_to", "top")]
    assert result["scroll_y"] == 0
    assert result["at_bottom"] is False


async def test_scroll_by_moves_with_the_wheel(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    down = await tools["scroll"](by_y=600)
    up = await tools["scroll"](by_y=-200)

    assert _calls(page, "wheel") == [("wheel", 0, 600), ("wheel", 0, -200)]
    assert (down["scroll_y"], up["scroll_y"]) == (600, 400)
    assert not down["at_bottom"]


@pytest.mark.parametrize(("asked", "sent"), [(10**7, MAX_SCROLL_PX), (-(10**7), -MAX_SCROLL_PX)])
async def test_one_scroll_cannot_cross_a_whole_feed(make_ctx, tools_of, page, asked, sent):
    page.scroll_height = 10**6
    page.scroll_y = 500_000
    await (await tools_of(make_ctx()))["scroll"](by_y=asked)

    assert _calls(page, "wheel") == [("wheel", 0, sent)]


async def test_scroll_to_a_selector_brings_it_into_view(make_ctx, tools_of, page):
    result = await (await tools_of(make_ctx()))["scroll"](selector="#footer")

    assert _calls(page, "scroll_into_view") == [("scroll_into_view", "#footer")]
    assert _calls(page, "wheel", "scroll_to") == []
    assert result["status"] == "ok" and "scroll_y" in result


async def test_scroll_into_view_takes_a_bounded_timeout(make_ctx, tools_of, monkeypatch):
    seen: list[float | None] = []

    async def spy(self, timeout=None):
        seen.append(timeout)

    monkeypatch.setattr(FakeLocator, "scroll_into_view_if_needed", spy)
    await (await tools_of(make_ctx()))["scroll"](selector="#footer", timeout_ms=10**9)

    assert seen == [30_000]


async def test_scroll_is_audited(make_ctx, tools_of):
    ctx = make_ctx()
    await (await tools_of(ctx))["scroll"](by_y=300)

    entries = _own_entries(ctx, "scroll")
    assert [e["args"]["by_y"] for e in entries] == [300]
    assert entries[0]["origin"] == "https://start.example"


# --------------------------------------------------------------------------
# scroll: the reply is where the page ended up, not where it was a frame ago
# --------------------------------------------------------------------------


async def test_scroll_replies_with_the_settled_position(make_ctx, tools_of, page, monkeypatch):
    """A lazy list grows a moment after the scroll lands. One quiet read is not
    "settled": here the page is still at 5000 for two reads, then loads."""
    heights = iter([5000, 5000, 5000])

    def growing() -> dict:
        return {"scroll_y": 4200, "scroll_height": next(heights, 7000), "at_bottom": False}

    monkeypatch.setattr(page, "scroll_state", growing)
    result = await (await tools_of(make_ctx()))["scroll"](to="bottom")

    assert result["scroll_height"] == 7000


async def test_scroll_stops_waiting_for_a_page_that_never_holds_still(
    make_ctx, tools_of, page, monkeypatch
):
    monkeypatch.setattr(interaction, "_SETTLE_STEP_S", 0.01)
    monkeypatch.setattr(interaction, "_SETTLE_MAX_S", 0.1)
    reads = {"n": 0}

    def restless() -> dict:
        reads["n"] += 1
        return {"scroll_y": reads["n"], "scroll_height": 9999, "at_bottom": False}

    monkeypatch.setattr(page, "scroll_state", restless)
    result = await (await tools_of(make_ctx()))["scroll"](to="bottom")

    assert result["status"] == "ok"
    assert 2 <= reads["n"] <= 20, "bounded by the settle window, not by the page"


async def test_a_scroll_that_moved_nothing_says_so(make_ctx, tools_of, page):
    """Content that scrolls inside its own panel: the window never moves."""
    page.scroll_locked = True
    tools = await tools_of(make_ctx())

    for kwargs in ({"to": "bottom"}, {"by_y": 500}):
        result = await tools["scroll"](**kwargs)
        assert result["status"] == "ok"
        assert "did not move" in result["hint"] and "selector" in result["hint"]


@pytest.mark.parametrize(
    ("start", "kwargs"),
    [(4200, {"to": "bottom"}), (4200, {"by_y": 500}), (0, {"to": "top"}), (0, {"by_y": -500})],
)
async def test_no_complaint_when_the_page_was_already_at_that_end(
    make_ctx, tools_of, page, start, kwargs
):
    page.scroll_y = start
    result = await (await tools_of(make_ctx()))["scroll"](**kwargs)

    assert result["status"] == "ok"
    assert "hint" not in result


async def test_scroll_into_view_never_claims_the_page_is_stuck(make_ctx, tools_of, page):
    page.scroll_locked = True
    result = await (await tools_of(make_ctx()))["scroll"](selector="#inner")

    assert "hint" not in result


async def test_a_scroll_the_driver_cannot_perform_comes_back_as_an_envelope(
    make_ctx, tools_of, monkeypatch
):
    async def gone(self, timeout=None):
        raise DriverError(COVERED_LOG)

    monkeypatch.setattr(FakeLocator, "scroll_into_view_if_needed", gone)
    result = await (await tools_of(make_ctx()))["scroll"](selector="#footer")

    assert result["status"] == "element_not_actionable"
