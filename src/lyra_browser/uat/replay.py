"""Run a recorded run again, step for step, and compare the judgement.

A run's ``trace.jsonl`` holds every tool call the brain made, with its arguments, in
order; ``run.json`` holds the spec. Replaying feeds those calls to the ``scripted``
brain against the same spec, so no model is involved, and compares what decides a run:
its status and exit reason, the outcome ``finish`` reported, the steps it spent and every
assertion's status. Same judgement means the site behaved the same for the same actions.

The trace is written through the audit's redaction, which hides every typed value
(``type_text``'s ``value`` among them): such a step cannot be replayed as it was typed,
and typing the placeholder instead would be a different action. The replay stops before
the first one and says so (``exact`` False, ``stopped_at`` its step number); its
judgement is then that of a shorter run and is compared all the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .report import Report
from .server import RUN_FILE, read_run_file

REDACTED = "<redacted:"


def judgement(report: Report) -> dict[str, Any]:
    """What a replay must reproduce."""
    return {
        "run_status": report.run.status,
        "exit_reason": report.run.exit_reason,
        "outcome": report.outcome.status,
        "steps_used": report.outcome.steps_used,
        "asserts": {a.id: a.status for a in report.asserts},
        "asserts_passed": report.asserts_passed,
    }


def script_from(report: Report) -> tuple[list[dict[str, Any]], int | None]:
    """The trace as a scripted brain's script, up to the first step whose arguments were
    redacted (whose number is returned, None when every step is intact)."""
    script: list[dict[str, Any]] = []
    for step in report.trace:
        if any(isinstance(v, str) and v.startswith(REDACTED) for v in step.args.values()):
            return script, step.n
        script.append({"tool": step.tool, "args": dict(step.args)})
    return script, None


async def replay_run(run_dir: Path, *, out_dir: Path | None = None) -> dict[str, Any]:
    """Replay the run in ``run_dir`` into ``out_dir`` (default: next to it) and compare."""
    from .runner import run_spec

    run_dir = run_dir.resolve()
    original = Report.model_validate_json((run_dir / "report.json").read_text(encoding="utf-8"))
    spec, _ = read_run_file(run_dir / RUN_FILE)
    script, stopped_at = script_from(original)
    if not script:
        raise ValueError(f"{run_dir}: no step of the trace can be replayed")
    replayed_spec = spec.model_copy(
        update={
            "brain": spec.brain.model_copy(
                update={"backend": "scripted", "script": script, "model": None}
            ),
            "out_dir": out_dir or run_dir.parent,
        }
    )
    replayed = await run_spec(replayed_spec)
    before, after = judgement(original), judgement(replayed)
    differences = {
        k: {"original": before[k], "replay": after[k]} for k in before if before[k] != after[k]
    }
    return {
        "original": str(run_dir / "report.json"),
        "replay": replayed.artifacts.report_json,
        "same": not differences,
        "exact": stopped_at is None,
        "stopped_at": stopped_at,
        "judgement": before,
        "differences": differences,
    }
