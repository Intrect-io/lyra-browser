"""ask_user_to_do hands the window to the user until the agent acts again.

The hole this closes: the agent asked the user to sign in, the user clicked
"Log in" in the shared window, and nothing happened. ask_user_to_do never told
the guard that a person was driving, so the user's own clicks were measured
against the agent's grants: the same-site GET once its INTERACT lease had
expired, and every login POST, which no SUBMIT grant covered. A takeover never
had the problem; a requested step is the same situation without the freeze.
"""

from __future__ import annotations

import json

import pytest

from lyra_browser.context import release_ownership
from test_tool_gates import MUTATING_TOOLS

LOGIN_PAGE = "https://start.example/login"


class _Route:
    def __init__(self) -> None:
        self.calls: list = []

    async def continue_(self) -> None:
        self.calls.append("continue")

    async def abort(self, error_code: str = "failed") -> None:
        self.calls.append(("abort", error_code))


class _Frame:
    def __init__(self, url: str) -> None:
        self.url = url
        self.parent_frame = None


class _Nav:
    def __init__(self, url: str, method: str = "GET", frame_url: str = LOGIN_PAGE) -> None:
        self.url = url
        self.method = method
        self.post_data = "user=me&password=secret" if method == "POST" else None
        self.frame = _Frame(frame_url)

    def is_navigation_request(self) -> bool:
        return True


def _navigation_rows(ctx) -> list[dict]:
    lines = ctx.config.audit_path.read_text().splitlines()
    return [row for row in map(json.loads, filter(None, lines)) if row["tool"] == "navigation"]


async def _judge(ctx, request: _Nav) -> list:
    route = _Route()
    await ctx.guard(route, request)
    return route.calls


# The two requests the user's sign-in made in the field: the form's POST, and a
# same-site GET ("Sign in with Google") after the agent's INTERACT lease had lapsed.
SIGN_IN = {
    "login_post": _Nav("https://start.example/login", method="POST"),
    "same_site_get": _Nav("https://start.example/api/auth/signin/google"),
}


@pytest.mark.parametrize("name", sorted(SIGN_IN))
async def test_the_users_sign_in_goes_through_after_ask_user_to_do(make_ctx, tools_of, name):
    ctx = make_ctx(on_site=False)
    tools = await tools_of(ctx)
    assert await _judge(ctx, SIGN_IN[name]) == [("abort", "aborted")], "the agent alone is judged"

    asked = await tools["ask_user_to_do"](instruction="Please sign in.")

    assert asked["status"] == "awaiting_user"
    assert await _judge(ctx, SIGN_IN[name]) == ["continue"], "the person at the keyboard"
    row = _navigation_rows(ctx)[-1]
    assert row["status"] == "user_driven"
    assert row["args"]["reason"] == "ask_user_to_do", "told apart from a takeover on the trail"


async def test_a_takeover_is_named_as_such_on_the_trail(make_ctx):
    ctx = make_ctx(on_site=False)
    ctx.collab.takeover = True

    assert await _judge(ctx, SIGN_IN["login_post"]) == ["continue"]
    assert _navigation_rows(ctx)[-1]["args"]["reason"] == "takeover"


ENDS_THE_TURN = sorted(set(MUTATING_TOOLS) - {"highlight_element", "ask_user_to_do"})


@pytest.mark.parametrize("name", ENDS_THE_TURN)
async def test_the_agents_next_action_takes_the_window_back(make_ctx, tools_of, name):
    ctx = make_ctx(on_site=False)
    tools = await tools_of(ctx)
    await tools["ask_user_to_do"](instruction="Please sign in.")

    await tools[name](**MUTATING_TOOLS[name])

    assert not ctx.collab.user_turn, f"{name} acted, so the window is the agent's again"
    ctx.perms.revoke_all()  # whatever the call itself bought, the next request has none
    assert await _judge(ctx, SIGN_IN["login_post"]) == [("abort", "aborted")]


async def test_evaluate_takes_the_window_back(make_ctx, tools_of):
    ctx = make_ctx(on_site=False)
    tools = await tools_of(ctx)
    await tools["ask_user_to_do"](instruction="Please sign in.")

    await tools["evaluate"](expression="1 + 1", confirm=True)

    assert not ctx.collab.user_turn


async def test_reading_and_pointing_leave_the_turn_with_the_user(make_ctx, tools_of):
    ctx = make_ctx(on_site=False)
    tools = await tools_of(ctx)
    await tools["ask_user_to_do"](instruction="Please sign in.", selector="#plain")

    await tools["read_page"]()
    await tools["get_url"]()
    await tools["screenshot"]()
    await tools["highlight_element"](selector="#plain")

    assert ctx.collab.user_turn
    assert await _judge(ctx, SIGN_IN["login_post"]) == ["continue"]


async def test_letting_go_of_the_browser_ends_the_turn(make_ctx, tools_of):
    """The next session to hold the browser must not inherit a step asked of the user."""
    ctx = make_ctx(on_site=False)
    tools = await tools_of(ctx)
    await tools["ask_user_to_do"](instruction="Please sign in.")

    await release_ownership(ctx, close=False)

    assert not ctx.collab.user_turn


async def test_resuming_after_a_takeover_ends_the_turn(make_ctx, tools_of):
    ctx = make_ctx(on_site=False)
    tools = await tools_of(ctx)
    await tools["ask_user_to_do"](instruction="Please sign in.")
    ctx.collab.takeover = True

    await tools["resume_after_takeover"](reason="done")

    assert not ctx.collab.takeover
    assert not ctx.collab.user_turn


async def test_a_refused_ask_hands_nothing_over(make_ctx, tools_of):
    headless = make_ctx(headless=True, on_site=False)
    held = make_ctx(on_site=False)
    held.collab.takeover = True

    for ctx in (headless, held):
        tools = await tools_of(ctx)
        result = await tools["ask_user_to_do"](instruction="Please sign in.")
        assert result["status"] in ("unattended", "takeover_active")
        assert not ctx.collab.user_turn


async def test_a_download_started_on_the_users_turn_is_theirs(make_ctx, tools_of, make_download):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    await tools["ask_user_to_do"](instruction="Download the invoice.")
    theirs = make_download()

    ctx.downloads.on_download(theirs)
    await ctx.downloads.idle()

    lines = ctx.config.audit_path.read_text().splitlines()
    rows = [row for row in map(json.loads, filter(None, lines)) if row["tool"] == "download"]
    assert rows[-1]["status"] == "user_driven", "not cancelled as a download nobody declared"
