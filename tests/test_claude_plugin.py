"""The repository is a Claude Code plugin and its own marketplace.

What Claude Code reads is the tracked tree copied into its plugin cache, so these
tests check the manifests, the launcher and the skills as files, and hand the copy
to ``claude plugin validate`` when that CLI is installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
HOOKS = ROOT / "hooks" / "hooks.json"
SKILLS = ROOT / "skills"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_names_the_plugin_the_marketplace_installs():
    manifest = _json(MANIFEST)
    marketplace = _json(MARKETPLACE)
    assert manifest["name"] == "lyra-browser"
    (entry,) = marketplace["plugins"]
    assert entry["name"] == manifest["name"], "the install key and the tool namespace differ"
    assert entry["source"] == ".", "the repository root is the plugin"
    assert marketplace["name"] == "lyra-browser"


def test_plugin_version_moves_with_the_package_version():
    """A plugin pinned to a version string only updates when that string changes."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert _json(MANIFEST)["version"] == project["project"]["version"]


def test_mcp_server_lives_in_the_manifest_not_a_root_mcp_json():
    """A root .mcp.json is also read as the repo's project server, where
    ${CLAUDE_PLUGIN_ROOT} is unset and the launch fails with ENOENT."""
    assert not (ROOT / ".mcp.json").exists()
    assert isinstance(_json(MANIFEST)["mcpServers"], dict)


def test_mcp_server_runs_the_launcher_and_only_declared_options():
    servers = _json(MANIFEST)["mcpServers"]
    (name,) = servers
    assert name == "lyra", "tool names are mcp__plugin_lyra-browser_lyra__<tool>"
    server = servers[name]
    command = server["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
    launcher = Path(command)
    assert launcher.is_file(), command
    assert launcher.stat().st_mode & 0o111, "launcher is not executable"
    declared = set(_json(MANIFEST)["userConfig"])
    referenced = set()
    for value in server.get("env", {}).values():
        referenced.update(re.findall(r"\$\{user_config\.(\w+)\}", value))
    assert referenced <= declared, referenced - declared
    assert declared <= referenced, "an option nobody reads: " + str(declared - referenced)
    assert server["env"]["LYRA_BROWSER_CONSENT_CHANNEL"] == "elicit"


def test_hook_file_wraps_the_event_map_and_quotes_the_plugin_root():
    hooks = _json(HOOKS)["hooks"]
    (entry,) = hooks["SessionStart"]
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert hook["command"].startswith('"${CLAUDE_PLUGIN_ROOT}"/'), hook["command"]
    script = ROOT / hook["command"].split("/", 1)[1]
    assert script.is_file() and script.stat().st_mode & 0o111, script


def _stamp_for(root: Path) -> str:
    """bootstrap.sh's stamp: pyproject + lock + the install path."""
    import hashlib

    h = hashlib.sha256()
    h.update((root / "pyproject.toml").read_bytes())
    h.update((root / "uv.lock").read_bytes())
    h.update(f"root={root}\n".encode())
    return h.hexdigest()


@pytest.fixture
def built_data_dir(tmp_path):
    """A data dir that looks already bootstrapped: a stub server and a matching stamp."""
    data = tmp_path / "data"
    stub = data / "venv" / "bin" / "lyra-browser"
    stub.parent.mkdir(parents=True)
    stub.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CLAUDE_PLUGIN_DATA/args"\n')
    stub.chmod(0o755)
    (data / "venv.stamp").write_text(_stamp_for(ROOT))
    return data


def _run(script: str, *args: str, data: Path | None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(data or ROOT), "CLAUDE_PLUGIN_ROOT": str(ROOT)}
    if data is not None:
        env["CLAUDE_PLUGIN_DATA"] = str(data)
    return subprocess.run(
        [str(ROOT / "scripts" / "claude-plugin" / script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_bootstrap_with_a_matching_stamp_is_silent_and_instant(built_data_dir):
    """The SessionStart hook's stdout enters the model's context: it must stay empty."""
    result = _run("bootstrap.sh", data=built_data_dir)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_launcher_keeps_stdout_for_the_transport_and_names_the_client(built_data_dir):
    result = _run("launch.sh", "--no-headless", data=built_data_dir)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "the launcher wrote to the MCP transport"
    assert (built_data_dir / "args").read_text().split() == [
        "--client",
        "claude-code",
        "--no-headless",
    ]


@pytest.mark.parametrize("script", ["bootstrap.sh", "launch.sh"])
def test_scripts_refuse_to_run_outside_claude_code(script, tmp_path):
    result = _run(script, data=None)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "CLAUDE_PLUGIN_DATA" in result.stderr


@pytest.mark.parametrize("skill", ["browser", "install-browser", "doctor"])
def test_each_skill_has_frontmatter_claude_code_reads(skill):
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "frontmatter must open on the first line"
    head = text.split("---\n", 2)[1]
    assert f"name: {skill}\n" in head
    assert "description:" in head
    assert "\n---\n" in text


def test_background_skill_hides_from_the_menu_and_names_the_tool_prefix():
    text = (SKILLS / "browser" / "SKILL.md").read_text(encoding="utf-8")
    assert "user-invocable: false" in text
    assert "mcp__plugin_lyra-browser_lyra__" in text


def _tracked_copy(dest: Path) -> None:
    """The files Claude Code would install: tracked plus new, never the local CLAUDE.md."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    for rel in filter(None, listed.decode().split("\0")):
        src = ROOT / rel
        if not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)


@pytest.mark.skipif(shutil.which("claude") is None, reason="Claude Code CLI not installed")
def test_claude_plugin_validate_passes_strictly_on_the_shipped_tree(tmp_path):
    copy = tmp_path / "lyra-browser"
    _tracked_copy(copy)
    result = subprocess.run(
        ["claude", "plugin", "validate", str(copy), "--strict"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
