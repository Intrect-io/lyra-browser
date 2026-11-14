#!/usr/bin/env python3
"""Real-browser gate: an unattended sign-in with a password and a one-time code, a bot
check that is given up on, and no secret anywhere in what the run leaves.

The built-in demo site serves ``/login`` -> ``/2fa`` -> ``/account`` (the password and
the TOTP seed are fresh canaries for each run) and ``/gate``, a page that reads as a bot
check. A scripted brain plays the persona through the real runner and a real headless
Chrome:

1. sign-in run: ``fill_credential`` types the email, the password and the current code
   (the persona never sees them), the forms are sent, and the run ends on ``/account``.
   ``network_capture`` keeps the two POSTs whole, the way an analytics check would.
2. challenge run: ``/gate`` three times -> ``challenge``, ``challenge``,
   ``challenge_abandoned``; the next browser call is refused, and the report ends
   ``challenge_abandoned``.

Then every file of both runs (trace, events, audit, report, run file, network and console
logs, observations, harness and capture metadata) is searched for the password and the
seed: there must be none.

    python scripts/verify_unattended_e2e.py [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def expect(condition: bool, message: str, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail}")
    print(f"PASS  {message}")


def spec_for(base: str, out: Path, seed_file: Path, script: list[dict]):
    from lyra_browser.uat.spec import RunSpec

    origin = base.rstrip("/")
    return RunSpec.model_validate(
        {
            "persona": {
                "id": "signin",
                "name": "Returning customer",
                "entry_url": f"{base}login",
                "goal": "sign in and open the account page",
                "step_budget": 12,
            },
            "target": {
                "trusted_origins": [origin],
                "trusted_send_origins": [origin],
                "network_capture": ["/login", "/2fa"],
            },
            "brain": {"backend": "scripted", "script": script},
            "credentials": {
                "demo": {
                    "origin": origin,
                    "username": "demo@example.com",
                    "password_env": "LYRA_GATE_DEMO_PASSWORD",
                    "totp_file": str(seed_file),
                }
            },
            "out_dir": str(out),
            "headless": True,
        }
    )


def fill(field: str, selector: str, **extra) -> dict:
    return {
        "tool": "fill_credential",
        "args": {"name": "demo", "field": field, "selector": selector, **extra},
        "expect": "ok",
    }


async def signin_run(base: str, out: Path, seed_file: Path):
    from lyra_browser.uat.runner import run_spec

    script = [
        {"tool": "navigate", "args": {"url": f"{base}login"}, "expect": "ok"},
        fill("username", "#user"),
        fill("password", "#pass"),
        {"tool": "click", "args": {"selector": "#signin", "submits": True}, "expect": "ok"},
        {"tool": "wait_for", "args": {"text": "authenticator", "timeout_ms": 8000}},
        fill("totp", "#code", submit=True),
        {"tool": "wait_for", "args": {"text": "Signed in as", "timeout_ms": 8000}},
        {"tool": "get_url", "args": {}},
        {"tool": "finish", "args": {"outcome": "reached_goal", "summary": "Signed in."}},
    ]
    report = await run_spec(spec_for(base, out, seed_file, script))
    print(report.one_line())
    expect(report.run.exit_reason == "finish", "sign-in run finished", report.brain.error)
    fills = [s for s in report.trace if s.tool == "fill_credential"]
    expect([s.status for s in fills] == ["ok", "ok", "ok"], "three fields typed", fills)
    expect(
        all(set(s.args) <= {"name", "field", "selector", "submit"} for s in fills),
        "the trace holds the credential's name and field only",
        [s.args for s in fills],
    )
    last = report.trace[-2]
    expect(last.url_after.endswith("/account"), "the run ended signed in on /account", last)
    return report


async def challenge_run(base: str, out: Path, seed_file: Path):
    from lyra_browser.uat.runner import run_spec

    script = [{"tool": "navigate", "args": {"url": f"{base}login"}, "expect": "ok"}]
    script += [{"tool": "navigate", "args": {"url": f"{base}gate"}} for _ in range(3)]
    script += [{"tool": "read_page", "args": {}}, {"tool": "note", "args": {"text": "walled"}}]
    report = await run_spec(spec_for(base, out, seed_file, script))
    print(report.one_line())
    statuses = [s.status for s in report.trace]
    expect(
        statuses
        == ["ok", "challenge", "challenge", "challenge_abandoned", "challenge_abandoned", "ok"],
        "the same check: named twice, abandoned the third time, then refused",
        statuses,
    )
    expect(
        report.run.exit_reason == "challenge_abandoned" and report.run.status == "incomplete",
        "the run ends as challenge_abandoned",
        report.run,
    )
    return report


def leaks(run_dir: Path, needles: list[str]) -> list[str]:
    found = []
    for path in run_dir.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        found += [f"{path}: {n[:4]}…" for n in needles if n.encode() in data]
    return found


async def main(keep: bool) -> None:
    from lyra_browser.uat import demo_site

    password = "gate-" + secrets.token_urlsafe(12)
    seed = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
    demo_site.Handler.password = password
    demo_site.Handler.totp_seed = seed
    os.environ["LYRA_GATE_DEMO_PASSWORD"] = password
    server, base, _alt = demo_site.start()
    out = Path(tempfile.mkdtemp(prefix="lyra-unattended-e2e-"))
    seed_file = out / "seed.txt"
    seed_file.write_text(seed)
    try:
        print(f"demo site {base}, runs in {out}")
        signed = await signin_run(base, out / "runs", seed_file)
        walled = await challenge_run(base, out / "runs", seed_file)
        for report in (signed, walled):
            run_dir = Path(report.artifacts.dir)
            found = leaks(run_dir, [password, seed])
            expect(not found, f"no password or seed in any file of {run_dir.name}", found)
        captured = (Path(signed.artifacts.dir) / "network.jsonl").read_text(encoding="utf-8")
        expect(
            "/login" in captured and "<secret>" in captured,
            "the captured sign-in POST is kept, with the password replaced",
        )
    finally:
        server.shutdown()
        if keep:
            print(f"kept {out}")
        else:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", action="store_true", help="Keep the run directories.")
    args = ap.parse_args()
    asyncio.run(main(args.keep))
    print("ALL PASS")
