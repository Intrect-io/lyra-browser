"""A run as one flat, versioned record for a benchmark or a dashboard.

``report.json`` is the whole evidence of a run and changes as lyra-uat grows. An export is
the stable part a scorer reads: which task, whether it succeeded by the spec's own
assertions, what the persona claimed, how long and how much it cost, and the steps it
took. The shape is ``ExportV1``; its JSON Schema is committed next to this module as
``schemas/export.v1.json`` (``lyra-uat schema export`` prints it), and a test keeps the
two the same, so a change of shape is a new version, never a silent edit.

A benchmark's own result format is a mapping from this record, kept with the adapter
for that benchmark rather than here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .report import AssertResult, Model, Report

EXPORT_FORMAT = "lyra-uat-export"
EXPORT_VERSION = 1
SCHEMA_FILE = Path(__file__).with_name("schemas") / "export.v1.json"


class ExportStep(Model):
    n: int
    tool: str
    args: dict[str, Any]
    status: str
    url_after: str
    counted: bool
    duration_ms: int = 0
    screenshot: str | None = None


class ExportV1(Model):
    format: Literal["lyra-uat-export"] = EXPORT_FORMAT
    version: Literal[1] = EXPORT_VERSION
    task_id: str = Field(description="the persona id: what was asked")
    run_id: str
    started_at: str
    finished_at: str
    duration_s: float
    run_status: Literal["completed", "incomplete", "error"]
    exit_reason: str
    # The judgement a scorer should use: the spec's assertions. None when the spec has none,
    # in which case only ``claimed_outcome`` is known, and that is the model's own word.
    success: bool | None
    asserts: list[AssertResult]
    claimed_outcome: Literal["reached_goal", "partial", "blocked", "unknown"]
    summary: str
    final_url: str
    steps_used: int
    step_budget: int
    tool_calls: int
    findings: int
    brain_backend: str
    brain_model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    lyra_browser_version: str
    steps: list[ExportStep]
    report_json: str | None


def export_report(report: Report) -> ExportV1:
    final_url = next((s.url_after for s in reversed(report.trace) if s.url_after), "")
    return ExportV1(
        task_id=report.persona.id,
        run_id=report.run.id,
        started_at=report.run.started_at,
        finished_at=report.run.finished_at,
        duration_s=report.run.duration_s,
        run_status=report.run.status,
        exit_reason=report.run.exit_reason,
        success=report.asserts_passed,
        asserts=list(report.asserts),
        claimed_outcome=report.outcome.status,
        summary=report.outcome.summary,
        final_url=final_url,
        steps_used=report.outcome.steps_used,
        step_budget=report.outcome.step_budget,
        tool_calls=report.outcome.attempts,
        findings=len(report.findings),
        brain_backend=report.brain.backend,
        brain_model=report.brain.model,
        input_tokens=report.brain.input_tokens,
        output_tokens=report.brain.output_tokens,
        cost_usd=report.brain.cost_usd,
        lyra_browser_version=report.run.lyra_browser_version,
        steps=[
            ExportStep(
                n=s.n,
                tool=s.tool,
                args=s.args,
                status=s.status,
                url_after=s.url_after,
                counted=s.counted,
                duration_ms=s.duration_ms,
                screenshot=s.screenshot,
            )
            for s in report.trace
        ],
        report_json=report.artifacts.report_json,
    )


def export_schema() -> dict[str, Any]:
    return ExportV1.model_json_schema()


def load_report(path: Path) -> Report:
    """``report.json`` itself, or the run directory that holds it."""
    if path.is_dir():
        path = path / "report.json"
    return Report.model_validate_json(path.read_text(encoding="utf-8"))


def write_export(report: Report, out: Path) -> Path:
    out.write_text(export_report(report).model_dump_json(indent=2), encoding="utf-8")
    return out


def committed_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
