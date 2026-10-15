"""Drive lyra through Claude Code as the plugin would be used, in `claude -p`.

    .venv/bin/python scripts/verify_claude_plugin_e2e.py          # a window opens if DISPLAY is set
    xvfb-run -a .venv/bin/python scripts/verify_claude_plugin_e2e.py

Loads the working tree with ``--plugin-dir``, serves the UAT demo site on
loopback, and runs two sessions against it:

* **approve** — a settings file registers an ``Elicitation`` hook that answers
  every lyra consent form with ``approve``, the way a person pressing Approve
  would. The session must navigate, read the title, and get a screenshot back
  as an MCP image block.
* **deny** — no hook and ``--permission-prompts none``, so the consent form is
  cancelled. The navigation must come back ``needs_approval`` and be audited as
  a denial that was asked, not a fallback to the model's ``confirm``.

Both go through the real plugin launcher, so the first run also builds the
plugin's environment under ``~/.claude/plugins/data/``. Exit status is the
verdict: 0 only if every check passes. Needs the Claude Code CLI on PATH (or
``LYRA_UAT_CLAUDE_BIN``) and a signed-in Claude Code.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TOOL_PREFIX = "mcp__plugin_lyra-browser_lyra__"
# A nested Claude Code refuses to start while these say it is already inside one.
SCRUB = ("CLAUDECODE", "CLAUDE_CODE_", "CODEX_")

HOOK = """\
import json, sys
request = json.load(sys.stdin)
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps(request) + "\\n")
fields = sorted((request.get("requested_schema") or {}).get("properties") or {})
if fields == ["value"]:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "Elicitation", "action": "accept", "content": {"value": "approve"}}}))
"""


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if not ok and detail else "")
        )
        if not ok:
            self.failed += 1
        return ok


def start_demo_site(port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "lyra_browser.uat", "demo-site", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as reply:
                if reply.status == 200:
                    return proc
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("demo site did not come up")


def child_env(data_dir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUB)}
    env["LYRA_BROWSER_DATA_DIR"] = str(data_dir)
    # A plugin server connects in the background and its tools sit behind tool
    # search; a one-prompt session would answer before either happened (measured:
    # "NO_TOOLS" in 4 s). Wait for the server and load its tools upfront.
    env["MCP_CONNECTION_NONBLOCKING"] = "0"
    env["ENABLE_TOOL_SEARCH"] = "false"
    return env


def run_claude(
    binary: str, prompt: str, *, env: dict[str, str], extra: list[str], max_turns: int
) -> tuple[list[dict], float, str]:
    argv = [
        binary,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--plugin-dir",
        str(REPO),
        "--setting-sources",
        "",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        f"{TOOL_PREFIX}*",
        *extra,
    ]
    started = time.monotonic()
    done = subprocess.run(
        argv, env=env, cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=600
    )
    events = []
    for line in done.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    return events, time.monotonic() - started, done.stderr


def tool_uses(events: list[dict]) -> list[dict]:
    uses = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_use":
                uses.append(block)
    return uses


def tool_results(events: list[dict]) -> list[dict]:
    results = []
    for event in events:
        if event.get("type") != "user":
            continue
        for block in event.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_result":
                results.append(block)
    return results


def result_blocks(result: dict) -> list[dict]:
    content = result.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def result_text(result: dict) -> str:
    return "\n".join(b.get("text", "") for b in result_blocks(result) if b.get("type") == "text")


def final_result(events: list[dict]) -> dict:
    for event in reversed(events):
        if event.get("type") == "result":
            return event
    return {}


def channel_of(record: dict) -> str | None:
    """The consent channel a decision record carries (under its ``args``)."""
    args = record.get("args")
    return args.get("consent_channel") if isinstance(args, dict) else None


def audit_records(data_dir: Path, action: str) -> list[dict]:
    path = data_dir / "audit.jsonl"
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("tool") == action:
            records.append(record)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--claude", default=os.environ.get("LYRA_UAT_CLAUDE_BIN", "claude"))
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--model", default=None, help="Pin the model; unpinned uses the default.")
    parser.add_argument("--keep", action="store_true", help="Keep the temp dir (data, hook log).")
    args = parser.parse_args()
    if shutil.which(args.claude) is None:
        print(f"claude not found: {args.claude}", file=sys.stderr)
        return 2

    checks = Checks()
    base = f"http://127.0.0.1:{args.port}"
    work = Path(tempfile.mkdtemp(prefix="lyra-plugin-e2e-"))
    print(f"work dir {work}")
    site = start_demo_site(args.port)
    pin = ["--model", args.model] if args.model else []
    try:
        # ---------------- approve: a person pressing Approve on each form ----------------
        data_a = work / "approve"
        hook_py = work / "approve_hook.py"
        hook_py.write_text(HOOK, encoding="utf-8")
        hook_log = work / "elicitations.jsonl"
        settings = work / "settings-approve.json"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Elicitation": [
                            {
                                "matcher": ".*",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": sys.executable,
                                        "args": [str(hook_py), str(hook_log)],
                                    }
                                ],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        prompt_a = (
            "Use only the lyra browser tools, in this order: open_browser; navigate to "
            f"{base}/ ; read_page; screenshot. Then reply with exactly one line: "
            "TITLE=<the page title read_page reported> DONE"
        )
        events, took, stderr = run_claude(
            args.claude,
            prompt_a,
            env=child_env(data_a),
            extra=["--settings", str(settings), *pin],
            max_turns=8,
        )
        final = final_result(events)
        uses = tool_uses(events)
        names = sorted({u["name"] for u in uses})
        print(
            f"approve run: {took:.1f}s, {len(uses)} tool calls, cost {final.get('total_cost_usd')}"
        )
        print(f"tools seen: {names}")
        checks.check("approve: session ran to a result", bool(final), stderr[-800:])
        checks.check(
            "approve: tools carry the plugin name",
            bool(names) and all(n.startswith(TOOL_PREFIX) for n in names),
            names,
        )
        nav = [u for u in uses if u["name"] == f"{TOOL_PREFIX}navigate"]
        checks.check("approve: navigate was called", bool(nav))
        asked = (
            [json.loads(line) for line in hook_log.read_text().splitlines()]
            if hook_log.exists()
            else []
        )
        checks.check(
            "approve: the consent form reached the Elicitation hook",
            any("navigate" in a.get("message", "") for a in asked),
            [a.get("message") for a in asked],
        )
        checks.check(
            "approve: the form is lyra's approve/deny field",
            all(
                sorted((a.get("requested_schema") or {}).get("properties") or {}) == ["value"]
                for a in asked
            )
            and bool(asked),
        )
        allowed = [r for r in audit_records(data_a, "navigate") if r.get("status") == "allowed"]
        checks.check(
            "approve: audit records the navigation as allowed through elicit",
            any(channel_of(r) == "elicit" for r in allowed),
            audit_records(data_a, "navigate"),
        )
        texts = "\n".join(result_text(r) for r in tool_results(events))
        checks.check("approve: read_page returned the demo page", "Demo home" in texts)
        images = [
            b for r in tool_results(events) for b in result_blocks(r) if b.get("type") == "image"
        ]
        checks.check("approve: a screenshot came back as an MCP image block", bool(images))
        checks.check(
            "approve: the model reported the title",
            "Demo home" in str(final.get("result", "")),
            final.get("result"),
        )

        # ---------------- deny: nobody answers, the form is cancelled ----------------
        data_b = work / "deny"
        prompt_b = (
            "Use only the lyra browser tools: open_browser, then navigate to "
            f"{base}/pricing . Do not retry or pass confirm. Reply with exactly one line: "
            "STATUS=<the status field of the navigate result>"
        )
        events, took, stderr = run_claude(
            args.claude,
            prompt_b,
            env=child_env(data_b),
            extra=["--permission-prompts", "none", *pin],
            max_turns=6,
        )
        final = final_result(events)
        calls = len(tool_uses(events))
        print(f"deny run: {took:.1f}s, {calls} tool calls, cost {final.get('total_cost_usd')}")
        checks.check("deny: session ran to a result", bool(final), stderr[-800:])
        nav_results = [
            r
            for u, r in zip(tool_uses(events), tool_results(events), strict=False)
            if u["name"] == f"{TOOL_PREFIX}navigate"
        ]
        statuses = []
        for r in nav_results:
            try:
                statuses.append(json.loads(result_text(r)).get("status"))
            except ValueError:
                statuses.append(result_text(r)[:80])
        checks.check(
            "deny: navigate answered needs_approval", "needs_approval" in statuses, statuses
        )
        records = audit_records(data_b, "navigate")
        denied = [r for r in records if r.get("status") == "denied"]
        checks.check(
            "deny: audit records a denial that was asked, not a confirm fallback",
            bool(denied)
            and all(channel_of(r) in {"denied", "elicit"} for r in denied)
            and not any(channel_of(r) == "legacy" for r in records),
            records,
        )
        checks.check(
            "deny: the model reported needs_approval",
            "needs_approval" in str(final.get("result", "")),
            final.get("result"),
        )
    finally:
        site.kill()
        if args.keep or checks.failed:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print(f"{'ALL PASS' if not checks.failed else f'{checks.failed} FAILED'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
