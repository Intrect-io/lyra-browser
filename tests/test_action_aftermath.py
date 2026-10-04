"""What an action set off is part of its reply (and the click that answered ``ok``).

Measured on MDN: a click on a link in an ad frame answered ``{status: "ok"}`` while the
guard had refused the navigation it caused, so the tab stayed put and the only account of
why was an audit row the agent does not read. And a click that opened a tab reported the
opener's URL as if nothing had happened.

Each test here drives a tool against the fake page and has the page do what the real one
does in answer (``page.reactions``): the guard counts a refusal, the session adopts a tab.
The real browser, with real timing, is ``scripts/verify_click_refusal_e2e.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeLocator, FakePage
from lyra_browser.enforcement import NavigationGuard, refused_envelope
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability
from lyra_browser.tools import aftermath
from lyra_browser.tools.interaction import _DESCRIBE_JS

# The trigger the fake page reacts to, and the tool call that causes it.
ACTIONS = {
    "click": (("click", "#go"), {"selector": "#go"}),
    "type_text": (("fill", "#go"), {"selector": "#go", "value": "text"}),
    "press_key": (("key", "Enter"), {"key": "Enter"}),
    "hover": (("hover", "#go"), {"selector": "#go"}),
}

ELSEWHERE = "https://elsewhere.example/"


class NavigationRequest:
    """The slice of a Playwright request the guard reads: a link out of the page."""

    method = "GET"
    post_data = None

    def __init__(self, url: str, frame_url: str) -> None:
        self.url = url
        self.frame = type("Frame", (), {"url": frame_url, "parent_frame": None})()

    def is_navigation_request(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def describes_the_target(monkeypatch):
    """Answer the element read the way a page would, so ``clicked`` can be asserted on."""
    original = FakeLocator.evaluate

    async def evaluate(self, expression, arg=None):
        if expression == _DESCRIBE_JS:
            return {"tag": "a", "role": "link", "name": "Pricing"}
        return await original(self, expression, arg)

    monkeypatch.setattr(FakeLocator, "evaluate", evaluate)


def _audit(ctx) -> list[dict]:
    return [json.loads(line) for line in Path(ctx.config.audit_path).read_text().splitlines()]


def _refuse(ctx, hop: str = ""):
    """What the guard does when it turns a navigation away (see ``NavigationGuard._judge``)."""

    def refused() -> None:
        ctx.guard.refusals += 1
        ctx.guard.refused_hop = hop

    return refused


def _background_tab(ctx, url: str) -> FakePage:
    """A tab that was open before the action, and is not the one acted on."""
    tab = FakePage(url)
    ctx.session.tabs.append(tab)
    return tab


def _link_out(ctx, page):
    """The page sends itself to another site, and the real guard judges the request."""

    def clicked() -> None:
        ctx.guard.decide(NavigationRequest(ELSEWHERE, page.url))

    return clicked


# --------------------------------------------------------------------------
# A navigation the guard refused is the answer, not an `ok` with nothing changed
# --------------------------------------------------------------------------


async def test_a_click_whose_navigation_was_refused_is_blocked_by_policy(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.reactions[("click", "#go")] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "blocked_by_policy"
    assert result["url"] == page.url, "and says where the tab still stands"
    assert "navigate" in result["hint"], "and what to do: ask for the destination"
    assert result["matches"] == 1 and result["clicked"]["name"] == "Pricing"
    assert page.calls.count(("click", "#go")) == 1


async def test_the_audit_row_of_a_blocked_click_says_so(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.reactions[("click", "#go")] = _refuse(ctx)
    tools = await tools_of(ctx)

    await tools["click"](selector="#go")

    rows = [r for r in _audit(ctx) if r["tool"] == "click" and "selector" in r["args"]]
    assert [r["status"] for r in rows] == ["blocked_by_policy"]
    assert rows[0]["args"] == {"selector": "#go"}, "the action itself is still on the trail"


@pytest.mark.parametrize("tool", sorted(ACTIONS))
async def test_every_acting_tool_reports_a_refusal_its_action_caused(
    make_ctx, tools_of, page, tool
):
    trigger, kwargs = ACTIONS[tool]
    ctx = make_ctx()
    page.reactions[trigger] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools[tool](**kwargs)

    assert result["status"] == "blocked_by_policy"
    assert result["url"] == page.url
    assert "navigate" in result["hint"]
    assert _audit(ctx)[-1]["status"] == "blocked_by_policy"


@pytest.mark.parametrize("tool", sorted(ACTIONS))
async def test_an_action_that_caused_no_refusal_stays_ok_and_says_nothing_more(
    make_ctx, tools_of, page, tool
):
    ctx = make_ctx()
    ctx.guard.refusals = 3  # a page's own redirect, refused before this call
    tools = await tools_of(ctx)

    result = await tools[tool](**ACTIONS[tool][1])

    assert result["status"] == "ok"
    assert not {"hint", "new_tab", "tab_count", "redirected_to"} & result.keys()
    assert _audit(ctx)[-1]["status"] == "ok"


async def test_a_refused_redirect_names_where_it_was_going(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.reactions[("click", "#go")] = _refuse(ctx, hop="https://elsewhere.example")
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "blocked_by_policy"
    assert result["redirected_to"] == "https://elsewhere.example"
    assert "https://elsewhere.example" in result["reason"]


@pytest.mark.parametrize(
    ("tool", "kwargs", "advice"),
    [
        ("click", {"selector": "#go"}, "submits=true"),
        ("press_key", {"key": "Enter"}, "submits=true"),
        ("type_text", {"selector": "#go", "value": "x"}, "submit=true"),
    ],
)
async def test_the_hint_offers_the_declaration_a_refused_form_needed(
    make_ctx, tools_of, page, tool, kwargs, advice
):
    """A form the page sends is refused the same way a link is, and ``navigate`` is the
    wrong remedy for it: the agent has to declare the send."""
    ctx = make_ctx()
    page.reactions[ACTIONS[tool][0]] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools[tool](**kwargs)

    assert advice in result["hint"]


async def test_a_hover_has_no_form_to_declare(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.reactions[("hover", "#go")] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools["hover"](selector="#go")

    assert result["status"] == "blocked_by_policy"
    assert "submit" not in result["hint"]


async def test_a_send_that_was_declared_is_not_told_to_declare_it_again(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.reactions[("click", "#go")] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go", submits=True, confirm=True)

    assert result["status"] == "blocked_by_policy"
    assert "submits=true" not in result["hint"]


async def test_the_matches_warning_survives_next_to_the_refusal(
    make_ctx, tools_of, page, monkeypatch
):
    async def three(self):
        return 3

    monkeypatch.setattr(FakeLocator, "count", three)
    ctx = make_ctx()
    page.reactions[("click", "#go")] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "blocked_by_policy" and result["matches"] == 3
    assert "navigate" in result["hint"] and "nth=N" in result["hint"]


async def test_dialogs_the_action_raised_are_still_reported_when_it_was_refused(
    make_ctx, tools_of, page
):
    ctx = make_ctx()
    page.dialogs_on_click = ["Leaving so soon?"]
    page.reactions[("click", "#go")] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "blocked_by_policy"
    assert [d["message"] for d in result["dialogs"]] == ["Leaving so soon?"]


# --------------------------------------------------------------------------
# A verdict that is still on its way: waited for briefly, and only when nothing has shown
# --------------------------------------------------------------------------


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    """How long the aftermath check slept, call by call. Real sleeping still happens."""
    naps: list[float] = []
    real = asyncio.sleep

    async def sleep(seconds: float) -> None:
        naps.append(seconds)
        await real(seconds)

    monkeypatch.setattr(aftermath, "asyncio", SimpleNamespace(sleep=sleep))
    return naps


def _late(ctx, after_s: float):
    """The navigation reaches the guard ``after_s`` after the call returned."""

    def started() -> None:
        asyncio.get_running_loop().call_later(after_s, _refuse(ctx))

    return started


@pytest.mark.parametrize("tool", ["hover", "type_text"])
async def test_a_refusal_that_lands_just_after_the_call_returned_is_still_reported(
    make_ctx, tools_of, page, slept, tool
):
    """Neither is held by the driver for the navigation it causes: it reaches the guard a few
    ms after they return (measured 0.2 to 8 ms)."""
    trigger, kwargs = ACTIONS[tool]
    ctx = make_ctx()
    ctx.config.download_settle_s = 0.1
    page.reactions[trigger] = _late(ctx, 0.012)
    tools = await tools_of(ctx)

    result = await tools[tool](**kwargs)

    assert result["status"] == "blocked_by_policy"


async def test_a_quiet_hover_waits_a_bounded_moment_and_a_quiet_click_waits_for_nothing_more(
    make_ctx, tools_of, page, slept
):
    ctx = make_ctx()
    ctx.config.download_settle_s = 0.1
    tools = await tools_of(ctx)

    await tools["hover"](selector="#go")
    hovered = list(slept)
    slept.clear()
    started = asyncio.get_running_loop().time()
    await tools["click"](selector="#go")
    took = asyncio.get_running_loop().time() - started

    assert hovered and sum(hovered) <= aftermath.LINGER_S + 0.005, "a hover lingers, boundedly"
    assert slept == [], "a click has listened for the settle window already"
    assert took < 0.1 + 0.1, "and the click took the settle window, not more"


async def test_the_wait_ends_the_moment_something_shows(make_ctx, tools_of, page, slept):
    ctx = make_ctx()
    ctx.config.download_settle_s = 0.1

    def moves_on() -> None:
        page.url = "https://start.example/next"

    page.reactions[("hover", "#go")] = moves_on
    tools = await tools_of(ctx)

    result = await tools["hover"](selector="#go")

    assert result["status"] == "ok" and result["url"] == "https://start.example/next"
    assert slept == [], "a page that visibly moved is not waited on"


async def test_typing_enter_listens_as_long_as_a_click_does(make_ctx, tools_of, page, slept):
    """Enter starts a navigation on purpose - a redirect chain can take longer than a fill's
    few ms to reach the guard - so it gets the whole settle window; plain typing does not."""
    ctx = make_ctx()
    ctx.config.download_settle_s = 0.1
    page.reactions[("press", "#go", "Enter")] = _late(ctx, 0.07)
    page.reactions[("fill", "#go")] = _late(ctx, 0.07)
    tools = await tools_of(ctx)

    submitted = await tools["type_text"](selector="#go", value="x", submit=True, confirm=True)
    await asyncio.sleep(0.1)
    typed = await tools["type_text"](selector="#go", value="x")

    assert submitted["status"] == "blocked_by_policy"
    assert typed["status"] == "ok", "a plain fill lingers LINGER_S, not the settle window"
    await asyncio.sleep(0.1)


async def test_a_verdict_later_than_the_bound_is_not_waited_for(make_ctx, tools_of, page, slept):
    ctx = make_ctx()
    ctx.config.download_settle_s = 0.1
    page.reactions[("hover", "#go")] = _late(ctx, 0.2)
    tools = await tools_of(ctx)

    result = await tools["hover"](selector="#go")

    assert result["status"] == "ok", "a page's own long timer is on the trail, not in the reply"
    await asyncio.sleep(0.25)  # let the late refusal land so the loop is left clean


async def test_an_operator_who_turned_post_action_listening_off_gets_no_wait(
    make_ctx, tools_of, page, slept
):
    ctx = make_ctx()
    ctx.config.download_settle_s = 0.0
    tools = await tools_of(ctx)

    await tools["hover"](selector="#go")

    assert slept == []


# --------------------------------------------------------------------------
# Downloads: a refusal explains a file that never came, but never hides one that did
# --------------------------------------------------------------------------


async def test_a_refusal_explains_a_download_that_never_started(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.reactions[("click", "#dl")] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#dl", download=True, confirm=True, timeout_ms=50)

    assert result["status"] == "blocked_by_policy", "not download_not_started: it was refused"


async def test_a_file_that_was_saved_is_the_result_whatever_else_the_page_did(
    make_ctx, make_download, tools_of, page
):
    ctx = make_ctx()
    page.reactions[("click", "#dl")] = _refuse(ctx)
    page.downloads_on[("click", "#dl")] = make_download()
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#dl", download=True, confirm=True)

    assert result["status"] == "ok" and result["download"]["filename"]


async def test_a_download_the_call_cancelled_keeps_its_own_status(
    make_ctx, make_download, tools_of, page
):
    ctx = make_ctx()
    page.reactions[("click", "#dl")] = _refuse(ctx)
    page.downloads_on[("click", "#dl")] = make_download()
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#dl")  # the file was not asked for

    assert result["status"] == "download_blocked"


# --------------------------------------------------------------------------
# The guard's own verdicts, not a counter the test sets
# --------------------------------------------------------------------------


async def test_a_click_that_leaves_for_an_unapproved_site_is_blocked(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.reactions[("click", "#go")] = _link_out(ctx, page)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "blocked_by_policy"
    rows = [r for r in _audit(ctx) if r["tool"] == "navigation"]
    assert [r["status"] for r in rows] == ["denied"], "the guard's row and the reply agree"


async def test_a_click_that_leaves_for_an_approved_site_is_ok(make_ctx, tools_of, page):
    ctx = make_ctx()
    ctx.perms.grant("default", parse_origin(ELSEWHERE), Capability.NAVIGATE)
    page.reactions[("click", "#go")] = _link_out(ctx, page)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "ok"


async def test_in_observe_mode_the_would_be_refusal_is_recorded_and_the_click_stays_ok(
    make_ctx, tools_of, page
):
    ctx = make_ctx()
    ctx.guard = NavigationGuard(ctx.config, ctx.perms, ctx.audit, mode="observe", collab=ctx.collab)
    page.reactions[("click", "#go")] = _link_out(ctx, page)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "ok"
    assert [r["status"] for r in _audit(ctx) if r["tool"] == "navigation"] == ["would_deny"]


async def test_a_takeover_answers_before_the_action_and_the_guard_is_not_consulted(
    make_ctx, tools_of, page
):
    ctx = make_ctx()
    ctx.collab.takeover = True
    page.reactions[("click", "#go")] = _refuse(ctx)
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "takeover_active"
    assert page.calls == [] and ctx.guard.refusals == 0


# --------------------------------------------------------------------------
# A tab the action opened
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(ACTIONS))
async def test_an_action_that_opens_a_tab_says_so(make_ctx, tools_of, page, tool):
    trigger, kwargs = ACTIONS[tool]
    ctx = make_ctx()
    page.reactions[trigger] = lambda: ctx.session.adopt("https://start.example/popup")
    tools = await tools_of(ctx)

    result = await tools[tool](**kwargs)

    assert result["status"] == "ok"
    assert result["new_tab"] is True and result["tab_count"] == 2
    assert "tabs(" in result["hint"]
    assert _audit(ctx)[-1]["status"] == "ok"


async def test_the_reply_url_stays_the_page_the_click_was_made_on(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.reactions[("click", "#go")] = lambda: ctx.session.adopt("https://start.example/popup")
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["url"] == page.url == "https://start.example/"
    assert (await ctx.session.page()).url == "https://start.example/popup"


async def test_a_tab_that_was_already_open_is_not_new(make_ctx, tools_of, page):
    ctx = make_ctx()
    _background_tab(ctx, "https://start.example/earlier")
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert "new_tab" not in result and "tab_count" not in result


async def test_a_tab_that_closed_is_not_a_new_one(make_ctx, tools_of, page):
    ctx = make_ctx()
    _background_tab(ctx, "https://start.example/earlier")
    page.reactions[("click", "#go")] = lambda: ctx.session.tabs.pop()
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "ok" and "new_tab" not in result


async def test_a_tab_that_replaces_another_is_still_a_new_tab(make_ctx, tools_of, page):
    """A sign-in popup closing as the next one opens leaves the count where it was."""
    ctx = make_ctx()
    _background_tab(ctx, "https://start.example/first-popup")

    def swap() -> None:
        ctx.session.tabs.pop()
        ctx.session.adopt("https://start.example/second-popup")

    page.reactions[("click", "#go")] = swap
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["new_tab"] is True and result["tab_count"] == 2


async def test_an_action_can_both_open_a_tab_and_be_refused(make_ctx, tools_of, page):
    ctx = make_ctx()

    def both() -> None:
        ctx.session.adopt("https://start.example/popup")
        _refuse(ctx)()

    page.reactions[("click", "#go")] = both
    tools = await tools_of(ctx)

    result = await tools["click"](selector="#go")

    assert result["status"] == "blocked_by_policy"
    assert result["new_tab"] is True and result["tab_count"] == 2
    assert "navigate" in result["hint"] and "tabs(" in result["hint"]


# --------------------------------------------------------------------------
# Reads report how many tabs there are
# --------------------------------------------------------------------------


async def test_get_url_says_how_many_tabs_are_open(make_ctx, tools_of, page):
    ctx = make_ctx()
    ctx.session.adopt("https://start.example/popup")
    tools = await tools_of(ctx)

    result = await tools["get_url"]()

    assert result == {"url": "https://start.example/popup", "title": "Start", "tab_count": 2}


async def test_open_browser_says_how_many_tabs_are_open(make_ctx, tools_of, page):
    ctx = make_ctx()
    tools = await tools_of(ctx)

    result = await tools["open_browser"]()

    assert result["status"] == "ok" and result["tab_count"] == 1


# --------------------------------------------------------------------------
# The envelope itself
# --------------------------------------------------------------------------


def test_an_envelope_for_an_action_says_how_to_get_the_destination_approved():
    plain = refused_envelope("https://page.example/")
    acted = refused_envelope("https://page.example/", caused_by="click", declare="submits=true")

    assert acted["status"] == plain["status"] == "blocked_by_policy"
    assert acted["url"] == "https://page.example/"
    assert "click" in acted["hint"] and "navigate" in acted["hint"]
    assert "submits=true" in acted["hint"]
    assert acted["hint"] != plain["hint"]


def test_a_refused_redirect_keeps_its_own_hint_whoever_acted():
    envelope = refused_envelope(
        "https://page.example/", redirected_to="https://b.example", caused_by="click"
    )

    assert envelope["redirected_to"] == "https://b.example"
    assert "that destination" in envelope["hint"]
