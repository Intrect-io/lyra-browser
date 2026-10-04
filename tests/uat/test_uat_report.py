"""The report: assembled from the run's files, the same whatever brain ran."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lyra_browser.uat.report import (
    BrainInfo,
    Hooks,
    Report,
    build_report,
    render_markdown,
    write_report,
)
from lyra_browser.uat.spec import RunSpec

PERSONA = {
    "id": "p2",
    "name": "Creator about to subscribe",
    "entry_url": "https://example.com/pricing",
    "goal": "pick a plan",
    "step_budget": 3,
    "must_not": ["use a live checkout"],
}


def spec_with(**extra) -> RunSpec:
    return RunSpec.model_validate(
        {
            "persona": PERSONA,
            "target": {"trusted_origins": ["example.com"], "trusted_send_origins": ["example.com"]},
            "brain": {"backend": "scripted", "script": [{"tool": "finish", "args": {}}]},
            **extra,
        }
    )


def step(n: int, tool: str, status: str = "ok", **kw) -> dict:
    row = {
        "n": n,
        "ts": f"2026-10-04T10:00:0{n}.000+00:00",
        "tool": tool,
        "args": kw.pop("args", {}),
        "action": tool != "read_page",
        "system": False,
        "status": status,
        "counted": tool != "read_page" and status == "ok",
        "url_before": "https://example.com/pricing",
        "url_after": "https://example.com/pricing",
        "result_head": "{}",
        "screenshot": None,
        "duration_ms": 12,
        "error": False,
    }
    row.update(kw)
    return row


def write_lines(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def make_run(tmp_path: Path, *, events: list[dict], trace: list[dict] | None = None):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    write_lines(
        run_dir / "trace.jsonl",
        trace
        if trace is not None
        else [
            step(1, "navigate", system=True, counted=False, args={"url": PERSONA["entry_url"]}),
            step(2, "read_page"),
            step(3, "click", args={"selector": "aria-ref=e4"}, screenshot="/x/captures/a.png"),
        ],
    )
    write_lines(run_dir / "events.jsonl", events)
    audit = run_dir / "audit.jsonl"
    write_lines(
        audit,
        [
            {
                "ts": "2026-10-04T10:00:02+00:00",
                "tool": "click",
                "args": {"capability": "submit", "consent_channel": "trusted_send"},
                "status": "allowed",
                "detail": None,
                "origin": "https://example.com",
            },
            {
                "ts": "2026-10-04T10:00:03+00:00",
                "tool": "navigate",
                "args": {"capability": "navigate", "consent_channel": "denied"},
                "status": "denied",
                "origin": "https://other.example",
            },
        ],
    )
    return run_dir, audit


def build(spec: RunSpec, run_dir: Path, audit: Path, **kw) -> Report:
    started = datetime(2026, 10, 4, 10, 0, tzinfo=UTC)
    return build_report(
        spec,
        run_dir,
        run_id="20261004T100000Z-p2-abc123",
        started_at=started,
        finished_at=started + timedelta(seconds=90),
        brain=kw.pop("brain", BrainInfo(backend="scripted", turns=3)),
        hooks=kw.pop("hooks", Hooks()),
        audit_path=audit,
        **kw,
    )


FINISH = {
    "type": "finish",
    "ts": "t",
    "step": 3,
    "outcome": "partial",
    "summary": "Found the plans; checkout opened a live page.",
    "what_worked": ["plan names are clear"],
    "purchase_path": ["pricing -> Upgrade -> checkout overlay"],
    "steps_used": 1,
    "step_budget": 3,
}
FINDING = {
    "type": "finding",
    "ts": "t",
    "step": 3,
    "id": "F1",
    "severity": "major",
    "url": "https://example.com/pricing",
    "at_step": 3,
    "expected": "a sandbox checkout",
    "actual": "a live checkout",
    "evidence": ["/x/captures/a.png"],
}


def test_a_finished_run_is_completed_and_carries_everything(tmp_path):
    spec = spec_with(prior_findings=[{"id": "R1-03", "actual": "no cancel link"}])
    run_dir, audit = make_run(
        tmp_path,
        events=[
            {"type": "note", "ts": "t", "step": 2, "text": "two plans, same price"},
            FINDING,
            {
                "type": "verdict",
                "ts": "t",
                "step": 3,
                "finding_id": "R1-03",
                "status": "FIXED",
                "evidence": "cancel link present",
            },
            {
                "type": "verdict",
                "ts": "t",
                "step": 3,
                "finding_id": "R1-03",
                "status": "STILL_THERE",
                "evidence": "on reload it was gone again",
            },
            {"type": "policy", "ts": "t", "step": 1, "kind": "budget_exhausted", "detail": "x"},
            FINISH,
        ],
    )
    report = build(spec, run_dir, audit, exit_reason="wall_timeout")  # finish outranks it
    assert report.run.status == "completed" and report.run.exit_reason == "finish"
    assert report.run.duration_s == 90.0
    assert report.outcome.status == "partial" and report.outcome.steps_used == 1
    assert report.outcome.attempts == 3 and report.outcome.step_budget == 3
    assert [f.id for f in report.findings] == ["F1"] and report.findings[0].step == 3
    # The last verdict wins and carries the prior finding it judged.
    assert [(v.finding_id, v.status) for v in report.verdicts] == [("R1-03", "STILL_THERE")]
    assert report.verdicts[0].prior and report.verdicts[0].prior.actual == "no cancel link"
    assert report.notes[0].text == "two plans, same price"
    assert report.what_worked == ["plan names are clear"]
    assert report.purchase_path == ["pricing -> Upgrade -> checkout overlay"]
    assert [e.kind for e in report.policy.events] == ["budget_exhausted"]
    assert report.policy.prompt_only == ["use a live checkout"]
    assert any("step_budget=3" in rule for rule in report.policy.enforced)
    # Only allowed send grants become side effects; the denied navigation does not.
    assert [s.capability for s in report.side_effects.submits] == ["submit"]
    assert report.side_effects.submits[0].consent_channel == "trusted_send"
    assert report.severity_counts() == {"blocker": 0, "major": 1, "minor": 0}
    assert (
        "p2: partial" in report.one_line() and "verdicts FIXED=0 STILL_THERE=1" in report.one_line()
    )


def test_without_finish_the_run_is_incomplete_and_says_why(tmp_path):
    spec = spec_with()
    run_dir, audit = make_run(tmp_path, events=[])
    assert build(spec, run_dir, audit).run.exit_reason == "no_finish"
    assert build(spec, run_dir, audit).run.status == "incomplete"
    assert build(spec, run_dir, audit).outcome.status == "unknown"

    run_dir2, audit2 = make_run(
        tmp_path / "b",
        events=[{"type": "policy", "ts": "t", "step": 3, "kind": "budget_exhausted", "detail": ""}],
    )
    assert build(spec, run_dir2, audit2).run.exit_reason == "budget_exhausted"
    assert (
        build(spec, run_dir2, audit2, exit_reason="wall_timeout").run.exit_reason == "wall_timeout"
    )
    errored = build(
        spec,
        run_dir2,
        audit2,
        exit_reason="brain_error",
        brain=BrainInfo(backend="anthropic", error="401"),
    )
    assert errored.run.status == "error" and errored.brain.error == "401"


def test_report_round_trips_through_json_and_schema(tmp_path):
    spec = spec_with()
    run_dir, audit = make_run(tmp_path, events=[FINDING, FINISH])
    report = build(
        spec,
        run_dir,
        audit,
        observations={
            "network_file": None,
            "console_file": None,
            "counts": {"requests": 4, "failed": 1},
            "matched": [{"pattern": "/g/collect", "count": 2}],
            "pages": 1,
        },
    )
    json_path, md_path = write_report(report, run_dir)
    assert json_path.is_file() and md_path.is_file()
    assert not list(run_dir.glob("*.part"))
    loaded = Report.model_validate_json(json_path.read_text(encoding="utf-8"))
    assert loaded == report
    assert loaded.artifacts.report_json == str(json_path)
    schema = Report.model_json_schema()
    assert schema["properties"]["schema_version"]["default"] == "1"
    assert "trace" in schema["required"] and "findings" in schema["required"]
    # A hand-written report that violates the schema is refused.
    broken = json.loads(json_path.read_text(encoding="utf-8"))
    broken["outcome"]["status"] = "great"
    try:
        Report.model_validate(broken)
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown outcome status was accepted")


def test_markdown_reads_like_the_testers_report(tmp_path):
    spec = spec_with(prior_findings=[{"id": "R1-03", "actual": "no cancel link"}])
    run_dir, audit = make_run(
        tmp_path,
        events=[
            FINDING,
            {
                "type": "verdict",
                "ts": "t",
                "step": 3,
                "finding_id": "R1-03",
                "status": "FIXED",
                "evidence": "link present",
            },
            FINISH,
        ],
    )
    text = render_markdown(build(spec, run_dir, audit))
    assert text.startswith("# p2 — Creator about to subscribe\n")
    assert "Outcome: **partial**" in text and "steps 1/3 (3 calls)" in text
    assert (
        "## Trace" in text and "1. `navigate` url=https://example.com/pricing → ok (free)" in text
    )
    assert "[/x/captures/a.png]" in text
    assert (
        "- **[major]** F1 https://example.com/pricing step 3: expected a sandbox checkout" in text
    )
    assert "| R1-03 — no cancel link | FIXED | link present |" in text
    assert "## What worked" in text and "## Purchase path" in text
    assert "## Approved sends" in text


def test_markdown_flags_a_clean_run_without_a_trace(tmp_path):
    spec = spec_with()
    run_dir, audit = make_run(tmp_path, events=[FINISH], trace=[])
    text = render_markdown(build(spec, run_dir, audit))
    assert "none reported (and no trace: not a clean run)" in text
