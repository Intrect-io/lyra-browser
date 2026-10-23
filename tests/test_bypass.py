"""Claude Code's bypassPermissions mode answers lyra's consent requests, and only it can.

Three parts, tested as three things that can each go wrong:

- the plugin's PreToolUse hook, the only writer of ``consent_override``: it adds the
  field in bypass mode and refuses a call that already carries it in any other mode;
- ``BypassMiddleware``, which takes the field out of the arguments before the tool sees
  them and marks the call, for the claude-code client only;
- ``ConsentBroker``, which answers a marked call with ``consent_channel=bypass``, PUBLISH
  included, while ``denied_origins``, opaque origins and a takeover still win.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from lyra_browser import bypass
from lyra_browser.approval import CollaborationState
from lyra_browser.audit import AuditLog
from lyra_browser.bypass import OVERRIDE_ARG, BypassMiddleware, bypass_active
from lyra_browser.config import CLIENTS, Config
from lyra_browser.consent import ConsentBroker
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability, PermissionStore

ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = ROOT / "scripts" / "claude-plugin"
HOOK_SH = HOOK_DIR / "consent-hook.sh"

SITE = parse_origin("https://example.com/")
LOCAL = parse_origin("file:///etc/passwd")
NAV_INPUT = {"url": "https://example.com/", "reason": "read docs", "confirm": False}


def _load_hook():
    spec = importlib.util.spec_from_file_location("consent_hook", HOOK_DIR / "consent_hook.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


def _event(mode: str, tool_input: dict) -> str:
    return json.dumps(
        {
            "session_id": "s",
            "hook_event_name": "PreToolUse",
            "permission_mode": mode,
            "tool_name": "mcp__plugin_lyra-browser_lyra__navigate",
            "tool_input": tool_input,
        }
    )


# --------------------------------------------------------------------------
# The hook
# --------------------------------------------------------------------------


def test_hook_in_bypass_mode_adds_the_field_and_keeps_every_argument():
    out = hook.decide(_event("bypassPermissions", NAV_INPUT))

    specific = out["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "allow"
    assert specific["updatedInput"] == {**NAV_INPUT, OVERRIDE_ARG: "bypassPermissions"}


def test_hook_in_bypass_mode_overwrites_a_value_the_model_chose():
    out = hook.decide(_event("bypassPermissions", {**NAV_INPUT, OVERRIDE_ARG: "nonsense"}))

    assert out["hookSpecificOutput"]["updatedInput"][OVERRIDE_ARG] == "bypassPermissions"


@pytest.mark.parametrize("mode", ["default", "plan", "acceptEdits", "auto", "dontAsk"])
def test_hook_outside_bypass_leaves_an_ordinary_call_alone(mode):
    assert hook.decide(_event(mode, NAV_INPUT)) is None


@pytest.mark.parametrize("mode", ["default", "plan", "acceptEdits", "auto", "dontAsk", None])
def test_hook_outside_bypass_refuses_a_forged_field(mode):
    forged = {**NAV_INPUT, OVERRIDE_ARG: "bypassPermissions"}

    out = hook.decide(_event(mode, forged))

    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "updatedInput" not in out["hookSpecificOutput"]


@pytest.mark.parametrize(
    ("raw", "denied"),
    [
        ("not json", False),
        ('{"tool_input": "consent_override"', True),
        ('["consent_override"]', True),
        ('{"permission_mode": "bypassPermissions", "tool_input": []}', False),
        ('{"permission_mode": "bypassPermissions"}', False),
    ],
)
def test_hook_never_allows_on_input_it_cannot_read(raw, denied):
    out = hook.decide(raw)

    if denied:
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert out is None


def _run_hook(stdin: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(HOOK_SH)], input=stdin, env=env, capture_output=True, text=True, timeout=30
    )


def test_hook_script_runs_end_to_end_with_the_system_python():
    assert HOOK_SH.stat().st_mode & 0o111, "hook script is not executable"
    env = {"PATH": os.environ["PATH"], "HOME": str(ROOT)}

    result = _run_hook(_event("bypassPermissions", NAV_INPUT), env)

    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["updatedInput"][OVERRIDE_ARG] == "bypassPermissions"
    assert _run_hook(_event("default", NAV_INPUT), env).stdout == ""


def test_hook_script_without_any_python_blocks_only_a_forged_call(tmp_path):
    """No Python to parse with: a call naming the field is blocked, the rest go on."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "env", "dirname", "grep"):
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}

    forged = _run_hook(_event("default", {**NAV_INPUT, OVERRIDE_ARG: "x"}), env)
    plain = _run_hook(_event("bypassPermissions", NAV_INPUT), env)

    assert forged.returncode == 2
    assert plain.returncode == 0 and plain.stdout == ""


def test_hooks_json_runs_the_hook_before_every_lyra_tool_and_nothing_else():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    (entry,) = hooks["PreToolUse"]
    (command,) = entry["hooks"]
    assert command["command"] == '"${CLAUDE_PLUGIN_ROOT}"/scripts/claude-plugin/consent-hook.sh'
    matcher = re.compile(entry["matcher"])
    assert matcher.search("mcp__plugin_lyra-browser_lyra__navigate")
    assert matcher.search("mcp__plugin_lyra-browser_lyra__publish")
    assert not matcher.search("mcp__lyra__navigate")
    assert not matcher.search("Bash")


# --------------------------------------------------------------------------
# The middleware
# --------------------------------------------------------------------------


def _probe_server(enabled: bool) -> FastMCP:
    mcp = FastMCP("probe")

    @mcp.tool
    def probe(url: str, confirm: bool = False) -> dict:
        return {"url": url, "bypass": bypass_active()}

    mcp.add_middleware(BypassMiddleware(enabled=enabled))
    return mcp


async def test_middleware_declares_the_field_so_a_strict_schema_accepts_it():
    async with Client(_probe_server(enabled=True)) as client:
        (tool,) = await client.list_tools()

    assert tool.inputSchema["additionalProperties"] is False
    assert OVERRIDE_ARG in tool.inputSchema["properties"]


@pytest.mark.parametrize(
    ("enabled", "value", "marked"),
    [
        (True, "bypassPermissions", True),
        (True, "bypass", False),
        (True, "", False),
        (False, "bypassPermissions", False),
    ],
)
async def test_middleware_marks_only_the_hooks_exact_value_when_enabled(enabled, value, marked):
    async with Client(_probe_server(enabled=enabled)) as client:
        result = await client.call_tool("probe", {"url": "u", OVERRIDE_ARG: value})

    assert result.data == {"url": "u", "bypass": marked}


async def test_middleware_leaves_an_unmarked_call_unmarked():
    async with Client(_probe_server(enabled=True)) as client:
        result = await client.call_tool("probe", {"url": "u"})

    assert result.data == {"url": "u", "bypass": False}
    assert bypass_active() is False


async def test_only_the_claude_code_client_gets_the_middleware(tmp_path):
    from lyra_browser.server import build_server

    for client in sorted(CLIENTS):
        cfg = Config(client=client, headless=True)
        cfg.data_dir = tmp_path / client
        cfg.__post_init__()
        mcp = build_server(cfg)
        installed = any(isinstance(m, BypassMiddleware) for m in mcp.middleware)
        assert installed is (client == "claude-code"), client
        tools = {t.name: t for t in await mcp.list_tools()}
        navigate = tools["navigate"].to_mcp_tool().inputSchema
        # A client without the hook is never shown the field.
        assert (OVERRIDE_ARG in navigate["properties"]) is (client == "claude-code"), client


async def test_claude_code_server_lists_the_field_on_every_tool(tmp_path):
    from lyra_browser.server import build_server

    cfg = Config(client="claude-code", headless=True)
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    async with Client(build_server(cfg)) as client:
        tools = await client.list_tools()

    assert tools
    assert all(OVERRIDE_ARG in t.inputSchema.get("properties", {}) for t in tools)


# --------------------------------------------------------------------------
# The broker
# --------------------------------------------------------------------------


class _NoPrompt:
    """A live request context that fails the test if anything asks the user."""

    request_id = "req-1"

    class session:  # noqa: N801 — attribute access shape of an MCP session
        prompts: list = []

        @staticmethod
        def check_client_capability(capability) -> bool:
            return True

        @classmethod
        async def elicit(cls, message, requestedSchema, related_request_id=None):  # noqa: N803
            cls.prompts.append(message)

            class Declined:
                action = "decline"
                content: dict = {}

            return Declined()


@pytest.fixture
def prompts(monkeypatch):
    _NoPrompt.session.prompts = []
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: _NoPrompt)
    return _NoPrompt.session.prompts


@pytest.fixture
def marked():
    token = bypass._bypass.set(True)
    yield
    bypass._bypass.reset(token)


@pytest.fixture
def make_broker(tmp_path):
    def _make(channel: str = "elicit", follow: bool = True, collab=None):
        cfg = Config(consent_channel=channel, follow_bypass=follow)
        cfg.data_dir = tmp_path
        cfg.__post_init__()
        cfg.consent_channel = channel
        audit = AuditLog(cfg.audit_path)
        return ConsentBroker(cfg, PermissionStore(ttl_s=600.0), audit, collab=collab), cfg

    return _make


def _channels(cfg) -> list[str]:
    rows = [json.loads(x) for x in cfg.audit_path.read_text().splitlines() if x.strip()]
    return [r["args"].get("consent_channel") for r in rows]


@pytest.mark.parametrize("capability", list(Capability))
async def test_bypass_answers_every_scope_without_asking(make_broker, prompts, marked, capability):
    broker, cfg = make_broker()

    decision = await broker.request(action="x", origin=SITE, capability=capability)

    assert decision.allowed
    assert decision.channel == "bypass"
    assert decision.granted is not None
    assert prompts == []
    assert _channels(cfg)[-1] == "bypass"


async def test_bypass_publishes_even_where_autonomous_would_refuse(make_broker, prompts, marked):
    broker, _ = make_broker("autonomous")

    decision = await broker.request(action="publish", origin=SITE, capability=Capability.PUBLISH)

    assert decision.allowed and decision.channel == "bypass"


async def test_without_the_mark_lyra_asks_as_before(make_broker, prompts):
    broker, _ = make_broker()

    decision = await broker.request(action="x", origin=SITE, capability=Capability.NAVIGATE)

    assert not decision.allowed
    assert len(prompts) == 1


async def test_follow_bypass_off_ignores_the_mark(make_broker, prompts, marked):
    broker, _ = make_broker(follow=False)

    decision = await broker.request(action="x", origin=SITE, capability=Capability.NAVIGATE)

    assert decision.channel != "bypass"
    assert len(prompts) == 1


async def test_a_local_file_is_still_asked_about(make_broker, prompts, marked):
    broker, _ = make_broker()

    decision = await broker.request(action="x", origin=LOCAL, capability=Capability.NAVIGATE)

    assert decision.channel != "bypass"
    assert len(prompts) == 1


async def test_a_takeover_is_released_by_the_person_not_by_bypass(make_broker, prompts, marked):
    broker, _ = make_broker(collab=CollaborationState(takeover=True))

    decision = await broker.request(
        action="resume_after_takeover", origin=SITE, capability=Capability.INTERACT
    )

    assert decision.channel != "bypass"
    assert len(prompts) == 1


async def test_a_denied_origin_beats_bypass(make_broker, prompts, marked):
    broker, cfg = make_broker()
    cfg.denied_origins = ("example.com",)

    decision = await broker.request(action="x", origin=SITE, capability=Capability.NAVIGATE)

    assert not decision.allowed
    assert "denied_origins" in decision.detail
    assert prompts == []


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, True), ("", True), ("  ", True), ("true", True), ("false", False), ("0", False)],
)
def test_follow_bypass_is_on_unless_turned_off(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("LYRA_BROWSER_FOLLOW_BYPASS", raising=False)
    else:
        monkeypatch.setenv("LYRA_BROWSER_FOLLOW_BYPASS", value)

    assert Config.from_env(client="claude-code").follow_bypass is expected
