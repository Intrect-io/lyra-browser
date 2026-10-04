#!/usr/bin/env python3
"""Exercise UAT mode against a real installed browser.

An explicit runtime gate, not part of the unit suite. The built-in demo site
(``lyra_browser.uat.demo_site``) stands in for the product; a scripted brain
stands in for the model, so the run is the same every time and what is checked
is the server side of a run: the trace, the budget, the guards, the captures,
the observers, the hooks and the report — in a real headless Chromium, through
the real runner.

    python scripts/verify_uat_e2e.py            # headless (the UAT default)
    python scripts/verify_uat_e2e.py --keep     # leave the run directories behind
"""

from __future__ import annotations

import argparse
import asyncio
import json
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


def spec_for(base: str, out: Path, *, budget: int, script: list[dict], hooks: dict | None = None):
    from lyra_browser.uat.spec import RunSpec

    origin = base.rstrip("/")
    return RunSpec.model_validate(
        {
            "persona": {
                "id": "e2e",
                "name": "Gate persona",
                "entry_url": base,
                "viewport": "390x844",
                "goal": "see the pricing and try the card field",
                "step_budget": budget,
                "must_not": ["enter a card number"],
            },
            "target": {
                "trusted_origins": [origin],
                "trusted_send_origins": [origin],
                "network_capture": ["/collect"],
            },
            "brain": {"backend": "scripted", "script": script},
            "hooks": hooks or {},
            "out_dir": str(out),
            "headless": True,
        }
    )


async def completed_run(base: str, alt_base: str, out: Path) -> None:
    from lyra_browser.uat.report import Report
    from lyra_browser.uat.runner import run_spec

    script = [
        {"tool": "navigate", "args": {"url": base}, "expect": "ok"},
        {"tool": "read_page", "args": {"mode": "tree"}},
        {"tool": "click", "args": {"selector": "#pricing"}, "expect": "ok"},
        {"tool": "wait_for", "args": {"text": "Pricing", "timeout_ms": 5000}},
        {
            "tool": "type_text",
            "args": {"selector": "#card", "value": "4242 4242 4242 4242"},
            "expect": "blocked_by_uat_policy",
        },
        {"tool": "navigate", "args": {"url": alt_base}, "expect": "needs_approval"},
        {
            "tool": "navigate",
            "args": {"url": alt_base, "confirm": True},
            "expect": "needs_approval",
        },
        {"tool": "click", "args": {"selector": "#buy"}, "expect": "ok"},
        {"tool": "screenshot", "args": {}, "expect": "ok"},
        {
            "tool": "report_finding",
            "args": {
                "severity": "major",
                "url": f"{base}pricing",
                "step": 3,
                "expected": "a plan comparison",
                "actual": "two prices without features",
                "evidence": ["pricing page text"],
            },
            "expect": "ok",
        },
        {"tool": "note", "args": {"text": "nothing says what Studio adds"}},
        {
            "tool": "finish",
            "args": {
                "outcome": "partial",
                "summary": "Reached pricing; the card field was off limits.",
            },
            "expect": "ok",
        },
    ]
    spec = spec_for(
        base,
        out,
        budget=10,
        script=script,
        hooks={"before": "echo seeded $LYRA_UAT_PERSONA", "after": "echo checked"},
    )
    report = await run_spec(spec)
    run_dir = Path(report.artifacts.dir)
    print(report.one_line())

    expect(report.run.status == "completed" and report.run.exit_reason == "finish", "run completed")
    expect(report.outcome.status == "partial", "outcome is what finish said", report.outcome)
    expect(
        report.outcome.steps_used == 2, "two counted actions (pricing click, buy)", report.outcome
    )
    expect(
        report.metrics.clicks == 2 and report.metrics.direct_navigations == 0,
        "metrics",
        report.metrics,
    )
    expect(not report.warnings, "no warnings on a clean run", report.warnings)
    first = report.trace[0]
    expect(first.tool == "navigate" and first.system and not first.counted, "entry navigation free")
    statuses = [s.status for s in report.trace]
    expect(statuses.count("needs_approval") == 2, "leaving the target refused twice", statuses)
    expect(
        all(not s.counted for s in report.trace if s.status == "needs_approval"),
        "refused navigations are not counted",
    )
    expect("blocked_by_uat_policy" in statuses, "card number refused by policy", statuses)
    kinds = [e.kind for e in report.policy.events]
    expect("guard_blocked_input" in kinds, "policy event recorded", kinds)
    shots = [s.screenshot for s in report.trace if s.counted]
    expect(
        all(shots) and all(Path(p).is_file() for p in shots), "every counted action has a capture"
    )
    expect(
        all(Path(p).parent == run_dir / "captures" for p in shots), "captures live in the run dir"
    )
    expect([f.severity for f in report.findings] == ["major"], "finding recorded", report.findings)
    expect(report.notes and "Studio" in report.notes[0].text, "note recorded")
    obs = report.observations
    expect(obs is not None and obs.counts["requests"] > 0, "requests observed", obs)
    expect(
        any(m["pattern"] == "/collect" and m["count"] >= 1 for m in obs.matched),
        "analytics hit kept whole",
        obs.matched,
    )
    hits = [
        json.loads(line)
        for line in (run_dir / "network.jsonl").read_text().splitlines()
        if "/collect" in line
    ]
    expect(any("en=page_view" in h.get("url", "") for h in hits), "hit query string retained", hits)
    expect(obs.counts["console_errors"] >= 1, "console error counted", obs.counts)
    expect(obs.counts["page_errors"] >= 1, "page error counted", obs.counts)
    expect(obs.browser.get("profile_mode") == "shared", "own profile, not a fallback", obs.browser)
    expect(
        "seeded e2e" in report.hooks.before.stdout_tail, "before hook ran with env", report.hooks
    )
    expect("checked" in report.hooks.after.stdout_tail, "after hook ran", report.hooks)
    loaded = Report.model_validate_json((run_dir / "report.json").read_text(encoding="utf-8"))
    expect(loaded == report, "report.json validates and round-trips")
    expect(
        (run_dir / "report.md").read_text().startswith("# e2e — Gate persona"), "report.md rendered"
    )
    expect((run_dir / "browser" / "audit.jsonl").is_file(), "run has its own audit log")
    expect((run_dir / "browser" / "profile").is_dir(), "run has its own browser profile")


async def budget_run(base: str, out: Path) -> None:
    from lyra_browser.uat.runner import run_spec

    script = [
        {"tool": "navigate", "args": {"url": base}, "expect": "ok"},
        {"tool": "click", "args": {"selector": "#pricing"}, "expect": "ok"},
        {"tool": "click", "args": {"selector": "#buy"}, "expect": "budget_exhausted"},
        {"tool": "read_page", "args": {}},
    ]
    report = await run_spec(spec_for(base, out, budget=1, script=script))
    print(report.one_line())
    expect(report.run.status == "incomplete", "no finish: incomplete", report.run)
    expect(report.run.exit_reason == "budget_exhausted", "exit reason is the budget", report.run)
    expect(report.outcome.status == "unknown", "outcome unknown without finish")
    expect(
        report.trace[-1].tool == "read_page" and report.trace[-1].status == "ok", "reads still work"
    )


async def main(keep: bool) -> None:
    from lyra_browser.uat import demo_site

    server, base, alt_base = demo_site.start()
    out = Path(tempfile.mkdtemp(prefix="lyra-uat-e2e-"))
    try:
        print(f"demo site {base}, runs in {out}")
        await completed_run(base, alt_base, out)
        await budget_run(base, out)
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
