"""Headless as a first-class mode, not just a hidden window.

Headless means *nobody is watching*. The collaboration primitives all assume a
human at the screen, so in headless mode they must say they cannot reach one
rather than reporting success for something no one will see. ``request_takeover``
is the sharp edge: granting a takeover no human can release would block every
mutation for the rest of the run.
"""

from __future__ import annotations

import pytest

from lyra_browser.__main__ import build_parser, config_from_args
from lyra_browser.config import Config

COLLABORATION_TOOLS = {
    "highlight_element": {"selector": "#target"},
    "ask_user_to_do": {"instruction": "log in"},
    "request_takeover": {"reason": "needs a human"},
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_attended_is_the_inverse_of_headless():
    assert Config(headless=False).attended is True
    assert Config(headless=True).attended is False


# --------------------------------------------------------------------------
# CLI flag: explicit beats environment, absent defers to it
# --------------------------------------------------------------------------


def test_headless_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_HEADLESS", "false")
    args = build_parser().parse_args(["--headless"])
    assert config_from_args(args).headless is True


def test_no_headless_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_HEADLESS", "true")
    args = build_parser().parse_args(["--no-headless"])
    assert config_from_args(args).headless is False


@pytest.mark.parametrize(("env", "expected"), [("true", True), ("false", False)])
def test_absent_flag_follows_env(monkeypatch, env, expected):
    monkeypatch.setenv("LYRA_BROWSER_HEADLESS", env)
    args = build_parser().parse_args([])
    assert config_from_args(args).headless is expected


# --------------------------------------------------------------------------
# Collaboration tools refuse when no one is watching
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "kwargs"), sorted(COLLABORATION_TOOLS.items()))
async def test_collaboration_tools_report_unattended(make_ctx, tools_of, page, name, kwargs):
    tools = await tools_of(make_ctx(headless=True))

    result = await tools[name](**kwargs)

    assert result["status"] == "unattended"
    assert result["reason"] and result["hint"]
    assert page.calls == [], f"{name} touched the page with nobody watching"


@pytest.mark.parametrize(("name", "kwargs"), sorted(COLLABORATION_TOOLS.items()))
async def test_collaboration_tools_work_when_attended(make_ctx, tools_of, name, kwargs):
    tools = await tools_of(make_ctx(headless=False))

    result = await tools[name](**kwargs)

    assert result["status"] != "unattended"


async def test_headless_takeover_does_not_deadlock_the_run(make_ctx, tools_of):
    """A refused takeover must leave the agent free to keep working."""
    ctx = make_ctx(headless=True)
    tools = await tools_of(ctx)

    result = await tools["request_takeover"](reason="needs a human")

    assert result["status"] == "unattended"
    assert ctx.collab.takeover is False
    # The very failure mode this guards: mutations still run afterwards.
    assert (await tools["navigate"](url="https://start.example/next"))["status"] == "ok"


async def test_unattended_refusals_are_audited(make_ctx, tools_of):
    ctx = make_ctx(headless=True)
    tools = await tools_of(ctx)

    await tools["ask_user_to_do"](instruction="log in")

    assert "unattended" in ctx.config.audit_path.read_text()


# --------------------------------------------------------------------------
# The agent is told which mode it is in
# --------------------------------------------------------------------------


async def test_open_browser_reports_headless(make_ctx, tools_of):
    tools = await tools_of(make_ctx(headless=True))

    result = await tools["open_browser"]()

    assert result["attended"] is False
    assert "note" in result


async def test_open_browser_reports_attended(make_ctx, tools_of):
    tools = await tools_of(make_ctx(headless=False))

    result = await tools["open_browser"]()

    assert result["attended"] is True
    assert "note" not in result


async def test_reading_and_navigation_still_work_headless(make_ctx, tools_of, page):
    """Headless disables collaboration only — the browsing tools are unaffected."""
    tools = await tools_of(make_ctx(headless=True))

    assert (await tools["read_page"]())["text"] == "page text"
    assert (await tools["navigate"](url="https://start.example/x"))["status"] == "ok"
    assert ("goto", "https://start.example/x") in page.calls
