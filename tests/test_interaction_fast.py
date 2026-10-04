"""click / type_text fail fast, in words the caller can act on.

The old behaviour, measured against a page with a full-screen overlay: a click on
a covered control sat for 30.0s before Playwright gave up, a click on a control
that was not there did the same, and both had already asked the user for
permission (and bought a one-shot SUBMIT grant) for an action that could never
run. Each test here names one of those and asserts on what reached the page and
what was left behind, not only on the envelope.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from playwright.async_api import Error as DriverError
from playwright.async_api import TimeoutError as DriverTimeout

from conftest import FakeLocator
from lyra_browser.context import current_session_key
from lyra_browser.permission import Capability
from lyra_browser.tools import actionable, interaction
from lyra_browser.tools.actionable import describe_failure
from lyra_browser.tools.interaction import _DESCRIBE_JS

_ONE_SHOT = {Capability.SUBMIT, Capability.UPLOAD, Capability.PUBLISH}

# The two tools that take a selector, and the Playwright call each one makes.
ACTION = {"click": "click", "type_text": "fill"}

# Verbatim from a real Chrome run (trimmed): what Playwright says when the target
# is under an overlay, when the page goes away mid-click, and when a click landed
# and the navigation it started is still loading.
COVERED_LOG = """Locator.click: Timeout 10000ms exceeded.
Call log:
  - waiting for locator("#go").first
    - locator resolved to <button id="go">Go</button>
  - attempting click action
    2 × waiting for element to be visible, enabled and stable
      - element is visible, enabled and stable
      - scrolling into view if needed
      - done scrolling
      - <div id="overlay"></div> intercepts pointer events
    - retrying click action
      - waiting 500ms
"""
DELIVERED_LOG = """Locator.click: Timeout 10000ms exceeded.
Call log:
  - waiting for locator("#go").first
    - locator resolved to <a id="go" href="/slow">slow link</a>
  - attempting click action
    - waiting for element to be visible, enabled and stable
    - element is visible, enabled and stable
    - scrolling into view if needed
    - done scrolling
    - performing click action
    - click action done
    - waiting for scheduled navigations to finish
"""
NOT_THERE_LOG = """Locator.click: Timeout 10000ms exceeded.
Call log:
  - waiting for locator("#go").first
"""


def _audit(ctx) -> list[dict]:
    return [json.loads(line) for line in Path(ctx.config.audit_path).read_text().splitlines()]


def _tool_entries(ctx, tool: str) -> list[dict]:
    """The tool's own audit lines. The consent broker writes its own under the
    same name (``allowed`` / ``denied``, no selector), which are not what is asserted."""
    return [e for e in _audit(ctx) if e["tool"] == tool and "selector" in e["args"]]


def _grants(ctx) -> list:
    return ctx.perms.live_grants(current_session_key(ctx))


def _live_one_shots(ctx) -> list:
    return [g for g in _grants(ctx) if g.uses_left and g.capability in _ONE_SHOT]


def _kwargs(tool: str, selector: str = "#go", **extra) -> dict:
    args = {"selector": selector, **extra}
    if tool == "type_text":
        args["value"] = "hello"
    return args


def _declares_a_send(tool: str) -> dict:
    return {"submit": True} if tool == "type_text" else {"submits": True}


def _target_closed(message: str = ""):
    # Not exported by playwright.async_api; if this import moves, describe_failure's
    # by-name match needs revisiting — which is exactly what this should flag.
    from playwright._impl._errors import TargetClosedError

    return TargetClosedError(message or None)


def _fails_with(monkeypatch, method: str, exc: Exception) -> None:
    async def boom(self, *args, **kwargs):
        raise exc

    monkeypatch.setattr(FakeLocator, method, boom)


@pytest.fixture(autouse=True)
def _no_appear_wait(monkeypatch):
    """A missing control is reported once the appear-wait runs out; these tests
    are about what happens next, not about sitting through it."""
    monkeypatch.setattr(actionable, "_APPEAR_BUDGET_S", 0.0)


@pytest.fixture
def action_timeouts(monkeypatch) -> list[tuple[str, float | None]]:
    """The ``timeout`` each Playwright action was handed, in order."""
    seen: list[tuple[str, float | None]] = []

    def spy(name: str) -> None:
        original = getattr(FakeLocator, name)

        async def wrapper(self, *args, timeout=None, **kwargs):
            seen.append((name, timeout))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(FakeLocator, name, wrapper)

    for name in ("click", "fill", "press"):
        spy(name)
    return seen


@pytest.fixture
def described(monkeypatch) -> dict:
    """Have the fake answer the click tool's element read the way a page would.

    ``answers['info']`` is what the page returns; an exception instance is raised
    instead. Reads are recorded in ``page.calls`` as ``("describe", selector)`` so
    their order against the click can be asserted.
    """
    answers: dict = {"info": {"tag": "button", "role": "button", "name": "Save"}}
    original = FakeLocator.evaluate

    async def evaluate(self, expression, arg=None):
        if expression != _DESCRIBE_JS:
            return await original(self, expression, arg)
        self._page.calls.append(("describe", self._selector))
        if isinstance(answers["info"], Exception):
            raise answers["info"]
        return answers["info"]

    monkeypatch.setattr(FakeLocator, "evaluate", evaluate)
    return answers


# --------------------------------------------------------------------------
# An unusable target is refused before anyone is asked or anything is bought
# --------------------------------------------------------------------------


def _make_unusable(page, monkeypatch, reason: str) -> None:
    page.present = {"#go"}
    if reason == "not_found":
        page.present = {"#elsewhere"}
    elif reason == "hidden":
        page.hidden = {"#go"}
    elif reason == "disabled":
        page.disabled = {"#go"}
    else:

        async def unreadable(self):
            raise RuntimeError("frame went away")

        monkeypatch.setattr(FakeLocator, "is_visible", unreadable)


@pytest.mark.parametrize("tool", sorted(ACTION))
@pytest.mark.parametrize("reason", ["not_found", "hidden", "disabled", "unreadable"])
async def test_an_unusable_control_is_named_and_the_page_is_left_alone(
    make_ctx, tools_of, page, monkeypatch, tool, reason
):
    ctx = make_ctx()
    _make_unusable(page, monkeypatch, reason)
    result = await (await tools_of(ctx))[tool](**_kwargs(tool))

    assert result["status"] == reason
    assert result["selector"] == "#go"
    assert result["url"] == page.url
    assert result["hint"], "the reason alone does not say what to do about it"
    assert page.calls == [], "nothing was clicked, typed, or even read from the element"
    entry = _tool_entries(ctx, tool)[0]
    assert entry["status"] == reason and entry["args"]["selector"] == "#go"


@pytest.mark.parametrize("tool", sorted(ACTION))
@pytest.mark.parametrize("reason", ["not_found", "hidden", "disabled"])
async def test_an_unusable_control_buys_no_grant(
    make_ctx, tools_of, page, monkeypatch, tool, reason
):
    """Nothing to spend later: not the one-shot SUBMIT, and not even the lease.

    On a site the agent has not been approved for, ``confirm=True`` would have
    bought an INTERACT lease plus a SUBMIT before the action found out it could
    not run.
    """
    ctx = make_ctx(on_site=False)
    _make_unusable(page, monkeypatch, reason)
    result = await (await tools_of(ctx))[tool](
        **_kwargs(tool, confirm=True, **_declares_a_send(tool))
    )

    assert result["status"] == reason
    assert _grants(ctx) == []


@pytest.mark.parametrize("tool", sorted(ACTION))
async def test_an_unusable_control_does_not_prompt(make_ctx, tools_of, page, tool):
    """A declared send with no approval answers ``needs_approval`` for a usable
    control; for one that cannot be used, the person is never asked."""
    page.present = {"#go"}
    page.hidden = {"#go"}
    result = await (await tools_of(make_ctx()))[tool](**_kwargs(tool, **_declares_a_send(tool)))

    assert result["status"] == "hidden"


@pytest.mark.parametrize("tool", sorted(ACTION))
@pytest.mark.parametrize("state", ["hidden", "disabled"])
async def test_a_control_that_is_not_ready_yet_is_used_once_it_is(
    make_ctx, tools_of, page, monkeypatch, tool, state
):
    """Playwright's own click waits for a control to become ready, and pages show
    one that is not yet: measured against a button enabled 0.8s after load, the
    original click succeeded after ~1s where a one-shot check refused it in 50ms."""
    monkeypatch.setattr(actionable, "_APPEAR_BUDGET_S", 2.0)
    monkeypatch.setattr(actionable, "_APPEAR_STEP_S", 0.01)
    looks = {"n": 0}

    async def ready_from_the_fourth_look(self):
        looks["n"] += 1
        return looks["n"] > 3

    monkeypatch.setattr(
        FakeLocator, "is_visible" if state == "hidden" else "is_enabled", ready_from_the_fourth_look
    )
    result = await (await tools_of(make_ctx()))[tool](**_kwargs(tool))

    assert result["status"] == "ok"
    assert looks["n"] == 4, "it looked until the control was ready, and not again"
    assert [c for c in page.calls if c[0] == ACTION[tool]], "and then it acted"


@pytest.mark.parametrize("tool", sorted(ACTION))
async def test_a_control_that_never_gets_ready_is_refused_when_the_window_closes(
    make_ctx, tools_of, page, monkeypatch, tool
):
    monkeypatch.setattr(actionable, "_APPEAR_BUDGET_S", 0.2)
    monkeypatch.setattr(actionable, "_APPEAR_STEP_S", 0.02)
    looks = {"n": 0}

    async def never(self):
        looks["n"] += 1
        return False

    monkeypatch.setattr(FakeLocator, "is_visible", never)
    started = time.monotonic()
    result = await (await tools_of(make_ctx()))[tool](**_kwargs(tool))
    took = time.monotonic() - started

    assert result["status"] == "hidden"
    assert looks["n"] > 3, "it kept looking through the window"
    assert took < 2.0, "and the window is bounded"
    assert page.calls == []


async def test_a_refused_type_text_does_not_write_the_typed_value_to_the_audit(
    make_ctx, tools_of, page
):
    ctx = make_ctx()
    page.present = set()
    await (await tools_of(ctx))["type_text"](selector="#pw", value="hunter2")

    assert "hunter2" not in Path(ctx.config.audit_path).read_text()


async def test_a_page_that_closes_during_the_look_is_page_closed(
    make_ctx, tools_of, page, monkeypatch
):
    ctx = make_ctx(on_site=False)

    async def gone(self):
        raise _target_closed()

    monkeypatch.setattr(FakeLocator, "count", gone)
    result = await (await tools_of(ctx))["click"](selector="#go", confirm=True, submits=True)

    assert result["status"] == "page_closed"
    assert _grants(ctx) == []


@pytest.mark.parametrize("tool", sorted(ACTION))
async def test_a_selector_the_driver_rejects_still_raises_and_buys_nothing(
    make_ctx, tools_of, monkeypatch, tool
):
    """Not every driver error is ours to dress up. A malformed selector is the
    caller's bug, and it stays an error rather than a status."""
    ctx = make_ctx(on_site=False)

    async def malformed(self):
        raise DriverError("Locator.count: SyntaxError: 'a:contains(x)' is not a valid selector.")

    monkeypatch.setattr(FakeLocator, "count", malformed)
    with pytest.raises(DriverError):
        await (await tools_of(ctx))[tool](**_kwargs(tool, confirm=True))

    assert _grants(ctx) == []


# --------------------------------------------------------------------------
# timeout_ms reaches the driver, bounded
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("asked", "reaches"),
    [
        (None, 10_000),
        (2_500, 2_500),
        (30_000, 30_000),
        (999_999, 30_000),
        (0, 1),  # to Playwright 0 means "no timeout" — the hang this exists to end
        (-5, 1),
    ],
)
async def test_click_hands_the_driver_a_bounded_timeout(
    make_ctx, tools_of, action_timeouts, asked, reaches
):
    extra = {} if asked is None else {"timeout_ms": asked}
    result = await (await tools_of(make_ctx()))["click"](selector="#go", **extra)

    assert result["status"] == "ok"
    assert action_timeouts == [("click", reaches)]


async def test_type_text_bounds_both_steps(make_ctx, tools_of, action_timeouts):
    result = await (await tools_of(make_ctx()))["type_text"](
        selector="#q", value="v", submit=True, confirm=True, timeout_ms=4_000
    )

    assert result["status"] == "ok"
    assert action_timeouts == [("fill", 4_000), ("press", 4_000)]


# --------------------------------------------------------------------------
# What goes wrong once the action runs comes back as an envelope
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(ACTION))
@pytest.mark.parametrize(
    ("error", "status", "in_hint"),
    [
        (DriverTimeout(COVERED_LOG), "element_not_actionable", '<div id="overlay"></div>'),
        (
            DriverError("ElementHandle.click: Element is not attached to the DOM"),
            ("element_not_actionable"),
            "removed",
        ),
        (DriverTimeout(NOT_THERE_LOG), "timeout", "10000ms"),
        (DriverTimeout(DELIVERED_LOG), "timeout", "Do not click again"),
        (None, "page_closed", "get_url"),
    ],
    ids=["covered", "detached", "slow", "landed", "closed"],
)
async def test_a_driver_failure_becomes_an_envelope(
    make_ctx, tools_of, page, monkeypatch, tool, error, status, in_hint
):
    ctx = make_ctx()
    _fails_with(monkeypatch, ACTION[tool], error or _target_closed(COVERED_LOG))
    result = await (await tools_of(ctx))[tool](**_kwargs(tool))

    assert result["status"] == status
    assert result["selector"] == "#go"
    assert result["url"] == page.url
    assert in_hint in result["hint"]
    entry = _tool_entries(ctx, tool)[0]
    assert entry["status"] == status, "the failure is on the trail, not a success"
    assert entry["detail"], "and says what the driver reported"
    assert [e["status"] for e in _tool_entries(ctx, tool)] == [status]


async def test_typing_into_something_that_is_not_a_field_says_so(make_ctx, tools_of, monkeypatch):
    _fails_with(
        monkeypatch,
        "fill",
        DriverError(
            "Locator.fill: Error: Element is not an <input>, <textarea>, <select> or "
            "[contenteditable] and does not have a role allowing [aria-readonly]"
        ),
    )
    result = await (await tools_of(make_ctx()))["type_text"](selector="#label", value="x")

    assert result["status"] == "element_not_actionable"
    assert "cannot be typed into" in result["hint"]


async def test_a_closed_page_is_not_mistaken_for_a_covered_element(make_ctx, tools_of, monkeypatch):
    """The closed-target message carries the same call log a covered element
    does; matching the log first would blame an overlay for a window that is gone."""
    _fails_with(monkeypatch, "click", _target_closed(COVERED_LOG))
    result = await (await tools_of(make_ctx()))["click"](selector="#go")

    assert result["status"] == "page_closed"
    assert "overlay" not in result["hint"]


def test_the_last_reason_in_the_log_is_the_one_reported():
    """Playwright appends a line per retry; the last is why it finally gave up."""
    earlier = (
        "Call log:\n  - element is not stable\n  - <div id=x></div> intercepts pointer events\n"
    )
    later = "Call log:\n  - <div id=x></div> intercepts pointer events\n  - element is not stable\n"

    assert describe_failure(DriverTimeout(earlier))[0] == "element_not_actionable"
    assert "<div id=x></div>" in describe_failure(DriverTimeout(earlier))[1]
    assert "keeps moving" in describe_failure(DriverTimeout(later))[1]


async def test_a_typed_value_is_not_quoted_back_from_the_call_log(make_ctx, tools_of, monkeypatch):
    """Playwright's call log echoes the input (``- fill("...")``). A value that
    happens to contain a phrase the classifier looks for must not be lifted from
    that echo into the hint or the audit trail."""
    ctx = make_ctx()
    secret = "hunter2 intercepts pointer events"
    log = (
        "Locator.fill: Timeout 10000ms exceeded.\n"
        "Call log:\n"
        '  - waiting for locator("#pw").first\n'
        f'    - fill("{secret}")\n'
        "  - attempting fill action\n"
    )
    _fails_with(monkeypatch, "fill", DriverTimeout(log))
    result = await (await tools_of(ctx))["type_text"](selector="#pw", value=secret)

    assert result["status"] == "timeout", "the echoed input is not a reason"
    assert "hunter2" not in json.dumps(result)
    assert "hunter2" not in Path(ctx.config.audit_path).read_text()


@pytest.mark.parametrize("tool", sorted(ACTION))
async def test_a_failed_action_still_hands_back_what_it_bought(
    make_ctx, tools_of, monkeypatch, tool
):
    """The envelope replaces the exception, not the cleanup: a one-shot SUBMIT
    bought for an action that failed must not stay live for the next request."""
    ctx = make_ctx()
    ctx.config.scope_release_grace_s = 0  # drive the release, do not wait past it
    _fails_with(monkeypatch, ACTION[tool], DriverTimeout(COVERED_LOG))
    result = await (await tools_of(ctx))[tool](
        **_kwargs(tool, confirm=True, reason="send", **_declares_a_send(tool))
    )

    assert result["status"] == "element_not_actionable"
    assert _live_one_shots(ctx) == []


@pytest.mark.parametrize("tool", sorted(ACTION))
async def test_an_error_that_is_not_the_drivers_propagates_and_still_releases(
    make_ctx, tools_of, monkeypatch, tool
):
    ctx = make_ctx()
    ctx.config.scope_release_grace_s = 0
    _fails_with(monkeypatch, ACTION[tool], ValueError("a bug, not a page state"))
    with pytest.raises(ValueError):
        await (await tools_of(ctx))[tool](
            **_kwargs(tool, confirm=True, reason="send", **_declares_a_send(tool))
        )

    assert _live_one_shots(ctx) == []
    assert _tool_entries(ctx, tool) == [], "an error that is not ours leaves no line of its own"


# --------------------------------------------------------------------------
# click reports what it hit
# --------------------------------------------------------------------------


async def test_click_reports_the_one_element_it_hit(make_ctx, tools_of, page, described):
    result = await (await tools_of(make_ctx()))["click"](selector="#save")

    assert result["status"] == "ok" and result["url"] == page.url
    assert result["matches"] == 1
    assert result["clicked"] == {"tag": "button", "role": "button", "name": "Save"}
    assert "hint" not in result, "one match needs no warning"


async def test_click_on_duplicates_says_which_it_took(
    make_ctx, tools_of, page, described, monkeypatch
):
    async def three(self):
        return 3

    monkeypatch.setattr(FakeLocator, "count", three)
    result = await (await tools_of(make_ctx()))["click"](selector="text=Save")

    assert result["status"] == "ok"
    assert result["matches"] == 3
    assert result["clicked"]["name"] == "Save"
    assert "first" in result["hint"] and "3" in result["hint"]
    assert page.calls.count(("click", "text=Save")) == 1, "one click, not three"


async def test_the_element_is_read_before_it_is_clicked(make_ctx, tools_of, page, described):
    """A click that navigates or re-renders takes the element with it."""
    await (await tools_of(make_ctx()))["click"](selector="#save")

    assert page.calls.index(("describe", "#save")) < page.calls.index(("click", "#save"))


async def test_the_name_is_short_and_on_one_line(make_ctx, tools_of, described):
    described["info"] = {"tag": "a", "role": "link", "name": "Read\n   the  " + "x" * 300}
    result = await (await tools_of(make_ctx()))["click"](selector="a")

    assert len(result["clicked"]["name"]) == 80
    assert result["clicked"]["name"].startswith("Read the xxx")


@pytest.mark.parametrize("answer", [RuntimeError("context destroyed"), None, True, "button"])
async def test_an_element_that_cannot_be_read_costs_the_report_not_the_click(
    make_ctx, tools_of, page, described, answer
):
    described["info"] = answer
    result = await (await tools_of(make_ctx()))["click"](selector="#save")

    assert result["status"] == "ok"
    assert "clicked" not in result
    assert ("click", "#save") in page.calls


async def test_a_count_that_cannot_be_read_costs_the_report_not_the_click(
    make_ctx, tools_of, page, described, monkeypatch
):
    calls = {"n": 0}

    async def flaky(self):
        calls["n"] += 1
        if calls["n"] > 1:  # the first is the look before the click
            raise RuntimeError("frame went away")
        return 1

    monkeypatch.setattr(FakeLocator, "count", flaky)
    result = await (await tools_of(make_ctx()))["click"](selector="#save")

    assert result["status"] == "ok" and "matches" not in result
    assert result["clicked"]["name"] == "Save"
    assert ("click", "#save") in page.calls


async def test_a_read_that_hangs_does_not_hold_the_click(make_ctx, tools_of, page, monkeypatch):
    """If the element vanished after the look, waiting for it to come back would
    undo the fast failure. The click has its own budget; the report gets a second."""
    monkeypatch.setattr(interaction, "_DESCRIBE_BUDGET_S", 0.05)
    original = FakeLocator.evaluate

    async def hangs(self, expression, arg=None):
        if expression == _DESCRIBE_JS:
            await asyncio.sleep(3600)
        return await original(self, expression, arg)

    monkeypatch.setattr(FakeLocator, "evaluate", hangs)
    result = await asyncio.wait_for((await tools_of(make_ctx()))["click"](selector="#save"), 5)

    assert result["status"] == "ok" and "clicked" not in result
    assert ("click", "#save") in page.calls


async def test_a_refused_click_does_not_read_the_element(make_ctx, tools_of, page, described):
    result = await (await tools_of(make_ctx()))["click"](selector="#go", submits=True)

    assert result["status"] == "needs_approval"
    assert page.calls == [], "reading the page is an act too, and it waits for permission"


# --------------------------------------------------------------------------
# Refs from read_page(mode="tree") arrive as selectors
# --------------------------------------------------------------------------

# Verbatim from Playwright 1.63: what a ref into a frame that is gone raises, from
# count() and from the action (which sees the ``.first`` the tool appended).
STALE_FRAME_COUNT = 'Locator.count: Invalid frame in aria-ref selector "aria-ref=f1e3"'
STALE_FRAME_ACTION = 'Locator.click: Invalid frame in aria-ref selector "aria-ref=f1e3 >> nth=0"'
TREE_READ = 'read_page(mode="tree")'


@pytest.mark.parametrize("tool", sorted(ACTION))
async def test_an_unknown_ref_is_not_found_without_the_appear_wait(
    make_ctx, tools_of, page, monkeypatch, tool
):
    """A ref cannot show up later, so the wait for late-built controls only delays
    the "read the page again" it needs. If this waited, it would sit for a minute."""
    monkeypatch.setattr(actionable, "_APPEAR_BUDGET_S", 60.0)
    page.present = set()
    result = await asyncio.wait_for(
        (await tools_of(make_ctx()))[tool](**_kwargs(tool, "aria-ref=e999")), 5
    )

    assert result["status"] == "not_found"
    assert TREE_READ in result["hint"]
    assert page.calls == []


async def test_only_a_ref_skips_the_appear_wait(make_ctx, tools_of, page, monkeypatch):
    monkeypatch.setattr(actionable, "_APPEAR_BUDGET_S", 0.1)
    monkeypatch.setattr(actionable, "_APPEAR_STEP_S", 0.01)
    page.present = set()
    polls: list[str] = []
    original = FakeLocator.count

    async def counting(self):
        polls.append(self._selector)
        return await original(self)

    monkeypatch.setattr(FakeLocator, "count", counting)
    tools = await tools_of(make_ctx())
    plain = await tools["click"](selector="#late")
    ref = await tools["click"](selector="aria-ref=e999")

    assert polls.count("#late") > 1, "an ordinary selector is still polled for a moment"
    assert polls.count("aria-ref=e999") == 1, "a ref is looked at once"
    assert TREE_READ not in plain["hint"], "and only a ref is told to read the tree again"
    assert TREE_READ in ref["hint"]


async def test_a_ref_that_resolves_is_clicked_like_any_selector(make_ctx, tools_of, page):
    page.present = {"aria-ref=e5"}
    result = await (await tools_of(make_ctx()))["click"](selector="aria-ref=e5")

    assert result["status"] == "ok" and result["matches"] == 1
    assert ("click", "aria-ref=e5") in page.calls


@pytest.mark.parametrize("tool", sorted(ACTION))
async def test_a_ref_into_a_frame_that_is_gone_is_not_found_and_buys_nothing(
    make_ctx, tools_of, page, monkeypatch, tool
):
    """The driver refuses this one outright. Left to propagate it would arrive as
    an exception, after a prompt and a one-shot grant, for a ref that was dead."""
    ctx = make_ctx(on_site=False)

    async def refused(self):
        raise DriverError(STALE_FRAME_COUNT)

    monkeypatch.setattr(FakeLocator, "count", refused)
    result = await (await tools_of(ctx))[tool](
        **_kwargs(tool, "aria-ref=f1e3", confirm=True, **_declares_a_send(tool))
    )

    assert result["status"] == "not_found"
    assert result["selector"] == "aria-ref=f1e3"
    assert TREE_READ in result["hint"]
    assert page.calls == [] and _grants(ctx) == []
    entry = _tool_entries(ctx, tool)[0]
    assert entry["status"] == "not_found" and "aria-ref" in entry["detail"]


@pytest.mark.parametrize("tool", sorted(ACTION))
async def test_a_ref_that_dies_between_the_look_and_the_action_is_not_found(
    make_ctx, tools_of, monkeypatch, tool
):
    ctx = make_ctx()
    ctx.config.scope_release_grace_s = 0
    _fails_with(monkeypatch, ACTION[tool], DriverError(STALE_FRAME_ACTION))
    result = await (await tools_of(ctx))[tool](
        **_kwargs(tool, "aria-ref=f1e3", confirm=True, reason="send", **_declares_a_send(tool))
    )

    assert result["status"] == "not_found"
    assert TREE_READ in result["hint"]
    assert _live_one_shots(ctx) == []


async def test_a_ref_to_a_removed_element_reads_as_hidden_and_says_to_read_again(
    make_ctx, tools_of, page, monkeypatch
):
    """A removed element keeps its ref and still counts; it is just not visible.
    ``:visible`` advice would be nonsense for a ref, and a wait would be too: a ref
    cannot become visible later."""
    monkeypatch.setattr(actionable, "_APPEAR_BUDGET_S", 60.0)  # a wait here sits for a minute
    page.present = {"aria-ref=e5"}
    page.hidden = {"aria-ref=e5"}
    result = await asyncio.wait_for(
        (await tools_of(make_ctx()))["click"](selector="aria-ref=e5"), 5
    )

    assert result["status"] == "hidden"
    assert TREE_READ in result["hint"] and ":visible" not in result["hint"]
    assert page.calls == []
