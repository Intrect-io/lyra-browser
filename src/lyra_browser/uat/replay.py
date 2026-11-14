"""Run a recorded run again, step for step, and compare the judgement.

A run's ``trace.jsonl`` holds every tool call the brain made, with its arguments, in
order; ``run.json`` holds the spec. Replaying feeds those calls to the ``scripted``
brain against the same spec, so no model is involved, and compares what decides a run:
its status and exit reason, the outcome ``finish`` reported, the steps it spent and every
assertion's status. Same judgement means the site behaved the same for the same actions.

Arguments the audit redacts (typed values of password-like fields) cannot be replayed as
typed; a replay that contains one says so (``exact`` is False) rather than pretend.
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


def script_from(report: Report) -> tuple[list[dict[str, Any]], bool]:
    """The trace as a scripted brain's script, and whether every argument is intact."""
    script, exact = [], True
    for step in report.trace:
        if any(isinstance(v, str) and v.startswith(REDACTED) for v in step.args.values()):
            exact = False
        script.append({"tool": step.tool, "args": dict(step.args)})
    return script, exact


async def replay_run(run_dir: Path, *, out_dir: Path | None = None) -> dict[str, Any]:
    """Replay the run in ``run_dir`` into ``out_dir`` (default: next to it) and compare."""
    from .runner import run_spec

    run_dir = run_dir.resolve()
    original = Report.model_validate_json((run_dir / "report.json").read_text(encoding="utf-8"))
    spec, _ = read_run_file(run_dir / RUN_FILE)
    script, exact = script_from(original)
    if not script:
        raise ValueError(f"{run_dir}: the trace is empty, there is nothing to replay")
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
        "exact": exact,
        "judgement": before,
        "differences": differences,
    }
