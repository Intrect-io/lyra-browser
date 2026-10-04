"""The harness brains against stub executables: the command line they build, the
environment they pass, the result they read back."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from lyra_browser.uat.brains.claude_code import ClaudeCodeBrain
from lyra_browser.uat.brains.codex import CodexBrain
from lyra_browser.uat.brains.harness import child_env, server_command
from lyra_browser.uat.spec import BrainSpec, RunSpec

ENTRY = "https://start.example/"

CLAUDE_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$STUB_ARGV_FILE"
printf 'CLAUDECODE=%s\\nCLAUDE_CODE_X=%s\\nLYRA_UAT_RUN_DIR=%s\\nPWD=%s\\n' \\
  "${CLAUDECODE:-}" "${CLAUDE_CODE_X:-}" "${LYRA_UAT_RUN_DIR:-}" "$PWD" > "$STUB_ENV_FILE"
cat <<'EOF'
{
  "type": "result",
  "result": "done",
  "session_id": "sess-1",
  "total_cost_usd": 0.0421,
  "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 900},
  "num_turns": 7,
  "is_error": false,
  "permission_denials": []
}
EOF
"""

CODEX_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$STUB_ARGV_FILE"
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; fi
  prev="$a"
done
echo "final answer" > "$out"
echo '{"type":"thread.started","thread_id":"thread-9"}'
echo 'not json noise'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":500,' \\
  '"cached_input_tokens":100,"output_tokens":50}}'
"""


def write_stub(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def spec_for(tmp_path: Path, backend: str) -> RunSpec:
    return RunSpec.model_validate(
        {
            "persona": {"id": "p1", "name": "T", "entry_url": ENTRY, "goal": "g", "step_budget": 4},
            "target": {"trusted_origins": ["start.example"]},
            "brain": {
                "backend": backend,
                "model": "some-model",
                "max_turns": 33,
                "max_budget_usd": 2.5,
            },
            "out_dir": str(tmp_path),
        }
    )


def test_child_env_scrubs_the_parents_claude_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CODEX_SANDBOX", "x")
    monkeypatch.setenv("KEEP_ME", "yes")
    env = child_env(tmp_path)
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "CODEX_SANDBOX" not in env and env["KEEP_ME"] == "yes"
    assert env["LYRA_UAT_RUN_DIR"] == str(tmp_path)


def test_server_command_is_this_interpreter_and_this_package(tmp_path):
    command, args = server_command(tmp_path / "run.json")
    assert command == sys.executable
    assert args == ["-m", "lyra_browser", "--uat-run", str(tmp_path / "run.json")]


@pytest.mark.asyncio
async def test_claude_code_builds_the_command_and_reads_the_result(tmp_path, monkeypatch):
    stub = write_stub(tmp_path / "claude", CLAUDE_STUB)
    argv_file, env_file = tmp_path / "argv.txt", tmp_path / "env.txt"
    monkeypatch.setenv("LYRA_UAT_CLAUDE_BIN", str(stub))
    monkeypatch.setenv("STUB_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("STUB_ENV_FILE", str(env_file))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_X", "leak")
    spec = spec_for(tmp_path, "claude-code")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_file = run_dir / "run.json"
    run_file.write_text("{}")

    info = await ClaudeCodeBrain(spec.brain).run(
        spec, run_dir, run_file, system_prompt="SYS PROMPT", task_prompt="TASK PROMPT"
    )
    assert info.backend == "claude-code" and info.model == "some-model"
    assert info.exit_code == 0 and info.error is None
    assert (info.turns, info.input_tokens, info.output_tokens, info.cache_read_tokens) == (
        7,
        1200,
        300,
        900,
    )
    assert info.cost_usd == 0.0421 and info.session_id == "sess-1"

    argv = argv_file.read_text().splitlines()
    assert argv[:2] == ["-p", "TASK PROMPT"]
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-turns") + 1] == "33"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5"
    assert argv[argv.index("--model") + 1] == "some-model"
    allowed = argv[argv.index("--allowedTools") + 1 :]
    allowed = [a for a in allowed if a.startswith("mcp__")]
    assert "mcp__lyra__navigate" in allowed and "mcp__lyra__finish" in allowed
    assert "mcp__lyra__publish" not in allowed and "mcp__lyra__upload_file" not in allowed
    system_file = Path(argv[argv.index("--system-prompt-file") + 1])
    assert system_file.read_text(encoding="utf-8") == "SYS PROMPT"
    mcp_config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
    assert mcp_config["mcpServers"]["lyra"]["command"] == sys.executable
    assert mcp_config["mcpServers"]["lyra"]["args"][-1] == str(run_file)

    env = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert env["CLAUDECODE"] == "" and env["CLAUDE_CODE_X"] == ""
    assert env["LYRA_UAT_RUN_DIR"] == str(run_dir)
    assert Path(env["PWD"]) == run_dir / "harness" / "cwd"
    assert (run_dir / "harness" / "stdout.log").is_file()


def test_claude_code_parse_reports_errors_and_denials():
    brain = ClaudeCodeBrain(BrainSpec(backend="claude-code"))
    broken = brain.parse("not json at all", 1, stderr="boom: no auth")
    assert broken.exit_code == 1 and "without a JSON result" in broken.error
    assert "boom: no auth" in broken.error
    errored = brain.parse(json.dumps({"is_error": True, "result": "Budget limit reached"}), 0)
    assert errored.error == "Budget limit reached"
    denied = brain.parse(
        json.dumps(
            {"is_error": False, "permission_denials": [{"tool_name": "mcp__lyra__publish"}]}
        ),
        0,
    )
    assert "permission denial" in denied.error and "mcp__lyra__publish" in denied.error


@pytest.mark.asyncio
async def test_codex_builds_the_command_and_reads_the_events(tmp_path, monkeypatch):
    stub = write_stub(tmp_path / "codex", CODEX_STUB)
    argv_file = tmp_path / "argv.txt"
    monkeypatch.setenv("LYRA_UAT_CODEX_BIN", str(stub))
    monkeypatch.setenv("STUB_ARGV_FILE", str(argv_file))
    spec = spec_for(tmp_path, "codex")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_file = run_dir / "run.json"
    run_file.write_text("{}")

    info = await CodexBrain(spec.brain).run(
        spec, run_dir, run_file, system_prompt="SYS PROMPT", task_prompt="TASK PROMPT"
    )
    assert info.backend == "codex" and info.exit_code == 0 and info.error is None
    assert (info.turns, info.input_tokens, info.output_tokens, info.cache_read_tokens) == (
        1,
        500,
        50,
        100,
    )
    assert info.session_id == "thread-9"

    argv = argv_file.read_text().splitlines()
    assert argv[0] == "exec" and "--json" in argv and "--ephemeral" in argv
    assert "--ignore-user-config" in argv and "--skip-git-repo-check" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "some-model"
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert 'approval_policy="never"' in overrides
    assert f"mcp_servers.lyra.command={json.dumps(sys.executable)}" in overrides
    assert any(o.startswith("mcp_servers.lyra.args=") and str(run_file) in o for o in overrides)
    # The prompt is the last argument and spans lines: role first, task last.
    recorded = argv_file.read_text().rstrip("\n")
    assert "SYS PROMPT" in recorded and recorded.endswith("TASK PROMPT")
    assert recorded.index("SYS PROMPT") < recorded.index("TASK PROMPT")
    last_message = Path(argv[argv.index("-o") + 1])
    assert last_message.read_text().strip() == "final answer"
    assert last_message.parent == run_dir / "harness"


def test_claude_code_parse_takes_the_result_from_a_message_array():
    """Measured with Claude Code 2.1.289: `--output-format json` can print the
    whole session as an array, init first, result last."""
    brain = ClaudeCodeBrain(BrainSpec(backend="claude-code"))
    array = json.dumps(
        [
            {"type": "system", "subtype": "init", "session_id": "s", "tools": []},
            {"type": "assistant", "message": {"content": []}},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "num_turns": 3,
                "session_id": "s",
                "total_cost_usd": 0.01,
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        ]
    )
    info = brain.parse(array, 0)
    assert info.turns == 3 and info.session_id == "s" and info.cost_usd == 0.01
    streamed = '{"type":"system"}\n{"type":"result","num_turns":1,"usage":{}}\n'
    assert brain.parse(streamed, 0).turns == 1


def test_codex_parse_flags_a_failed_or_silent_run():
    brain = CodexBrain(BrainSpec(backend="codex"))
    failed = brain.parse('{"type":"turn.completed"}', 2, stderr="error: model not found")
    assert failed.error.startswith("codex exited 2") and "model not found" in failed.error
    silent = brain.parse("", 0)
    assert silent.error == "codex produced no JSON events"


def test_missing_binaries_are_brain_errors(monkeypatch, tmp_path):
    from lyra_browser.uat.brains import BrainError

    monkeypatch.delenv("LYRA_UAT_CLAUDE_BIN", raising=False)
    monkeypatch.delenv("LYRA_UAT_CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # nothing on it
    spec = spec_for(tmp_path, "claude-code")
    run_dir = tmp_path / "r"
    run_dir.mkdir()
    import asyncio

    with pytest.raises(BrainError, match="claude CLI not found"):
        asyncio.run(
            ClaudeCodeBrain(spec.brain).run(
                spec, run_dir, run_dir / "run.json", system_prompt="", task_prompt=""
            )
        )
    with pytest.raises(BrainError, match="codex CLI not found"):
        asyncio.run(
            CodexBrain(BrainSpec(backend="codex")).run(
                spec, run_dir, run_dir / "run.json", system_prompt="", task_prompt=""
            )
        )
    assert os.environ["PATH"] == str(tmp_path)
