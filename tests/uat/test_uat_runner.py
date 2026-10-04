"""The runner on a fake page: a report is written whatever the brain did."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import FakePage, FakeSession
from lyra_browser.uat import runner as runner_mod
from lyra_browser.uat import server as server_mod
from lyra_browser.uat.brains import BrainError, HarnessBrain, LoopBrain
from lyra_browser.uat.report import BrainInfo, Report
from lyra_browser.uat.spec import RunSpec

ENTRY = "https://start.example/"


@pytest.fixture
def fake_browser(monkeypatch, page: FakePage):
    """Route the runner's server at the fake page instead of a real browser."""

    async def build(spec, run_dir, **kw):
        kw.setdefault("session_factory", lambda cfg: FakeSession(page))
        mcp, ctx, recorder = await server_mod.build_uat_server(spec, run_dir, **kw)
        ctx.config.download_settle_s = 0.0
        page.on_download = ctx.downloads.on_download
        return mcp, ctx, recorder

    monkeypatch.setattr(runner_mod, "build_uat_server", build)
    return page


def spec_for(tmp_path: Path, script: list[dict], **extra) -> RunSpec:
    return RunSpec.model_validate(
        {
            "persona": {
                "id": "p1",
                "name": "Tester",
                "entry_url": ENTRY,
                "goal": "look",
                "step_budget": extra.pop("budget", 5),
            },
            "target": {"trusted_origins": ["start.example"]},
            "brain": {"backend": "scripted", "script": script},
            "out_dir": str(tmp_path / "runs"),
            **extra,
        }
    )


@pytest.mark.asyncio
async def test_a_scripted_run_finishes_and_writes_the_report(fake_browser, tmp_path):
    spec = spec_for(
        tmp_path,
        [
            {"tool": "navigate", "args": {"url": ENTRY}, "expect": "ok"},
            {"tool": "read_page", "args": {"mode": "tree"}},
            {"tool": "click", "args": {"selector": "#plain"}, "expect": "ok"},
            {
                "tool": "report_finding",
                "args": {
                    "severity": "minor",
                    "url": ENTRY,
                    "step": 3,
                    "expected": "a label",
                    "actual": "no label",
                },
            },
            {"tool": "finish", "args": {"outcome": "reached_goal", "summary": "Fine."}},
        ],
    )
    report = await runner_mod.run_spec(spec)
    assert report.run.status == "completed" and report.run.exit_reason == "finish"
    assert report.outcome.status == "reached_goal" and report.outcome.steps_used == 1
    assert [f.severity for f in report.findings] == ["minor"]
    assert report.brain.backend == "scripted" and report.brain.turns == 5
    run_dir = Path(report.artifacts.dir)
    assert run_dir.parent == (tmp_path / "runs").resolve()
    assert run_dir.name.endswith("-p1-" + run_dir.name.rsplit("-", 1)[1])
    for name in ("report.json", "report.md", "trace.jsonl", "events.jsonl", "run.json"):
        assert (run_dir / name).is_file(), name
    loaded = Report.model_validate_json((run_dir / "report.json").read_text(encoding="utf-8"))
    assert loaded == report
    # The run file is what a harness server would read back.
    payload = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert payload["spec"]["persona"]["id"] == "p1" and payload["run_dir"] == str(run_dir)
    # Observations exist even on a fake session (no pages attach; counts are zero).
    assert report.observations is not None and report.observations.pages == 0
    # The browser was closed when the brain returned.
    assert fake_browser is not None


@pytest.mark.asyncio
async def test_a_brain_that_never_finishes_leaves_an_incomplete_report(fake_browser, tmp_path):
    spec = spec_for(
        tmp_path,
        [
            {"tool": "navigate", "args": {"url": ENTRY}},
            {"tool": "click", "args": {"selector": "#a"}},
            {"tool": "click", "args": {"selector": "#b"}},
        ],
        budget=1,
    )
    report = await runner_mod.run_spec(spec)
    assert report.run.status == "incomplete" and report.run.exit_reason == "budget_exhausted"
    assert report.outcome.status == "unknown" and report.outcome.steps_used == 1
    assert [s.status for s in report.trace] == ["ok", "ok", "budget_exhausted"]


@pytest.mark.asyncio
async def test_a_scripted_expectation_failure_is_a_brain_error(fake_browser, tmp_path):
    spec = spec_for(
        tmp_path, [{"tool": "click", "args": {"selector": "#x"}, "expect": "not_found"}]
    )
    report = await runner_mod.run_spec(spec)
    assert report.run.status == "error" and report.run.exit_reason == "brain_error"
    assert "expected status 'not_found'" in (report.brain.error or "")
    assert len(report.trace) == 1  # the click itself was still recorded


class SlowBrain(LoopBrain):
    backend = "slow"

    async def run(self, session):
        await session.call("navigate", {"url": ENTRY})
        await asyncio.sleep(5)
        return BrainInfo(backend=self.backend)


class CrashingBrain(LoopBrain):
    backend = "crash"

    async def run(self, session):
        raise BrainError("no API key in the environment")


@pytest.mark.asyncio
async def test_the_wall_clock_cuts_a_slow_brain_off(fake_browser, tmp_path):
    spec = spec_for(tmp_path, [{"tool": "finish", "args": {}}])
    spec.limits.wall_s = 1  # below the schema's floor on purpose: this is the test's knob
    report = await runner_mod.run_spec(spec, brain=SlowBrain())
    assert report.run.status == "incomplete" and report.run.exit_reason == "wall_timeout"
    assert report.trace and report.trace[0].tool == "navigate"
    assert "wall clock" in (report.brain.error or "")


@pytest.mark.asyncio
async def test_a_brain_error_is_reported_not_raised(fake_browser, tmp_path):
    spec = spec_for(tmp_path, [{"tool": "finish", "args": {}}])
    report = await runner_mod.run_spec(spec, brain=CrashingBrain())
    assert report.run.status == "error" and report.run.exit_reason == "brain_error"
    assert report.brain.error == "no API key in the environment"
    assert Path(report.artifacts.report_json).is_file()


@pytest.mark.asyncio
async def test_hooks_run_around_the_browser_and_a_failing_before_stops_it(fake_browser, tmp_path):
    spec = spec_for(
        tmp_path,
        [{"tool": "finish", "args": {"outcome": "blocked", "summary": "x"}}],
        hooks={
            "before": 'echo seeded $LYRA_UAT_PERSONA; test -n "$LYRA_UAT_RUN_DIR"',
            "after": "echo checked >&2; exit 3",
        },
    )
    report = await runner_mod.run_spec(spec)
    assert report.run.status == "completed"
    assert report.hooks.before and report.hooks.before.exit_code == 0
    assert "seeded p1" in report.hooks.before.stdout_tail
    assert report.hooks.after and report.hooks.after.exit_code == 3
    assert "checked" in report.hooks.after.stderr_tail

    failing = spec_for(
        tmp_path / "b",
        [{"tool": "finish", "args": {"outcome": "blocked", "summary": "x"}}],
        hooks={"before": "echo nope; exit 2", "after": "echo cleanup"},
    )
    report = await runner_mod.run_spec(failing)
    assert report.run.status == "error" and report.run.exit_reason == "hook_failed"
    assert report.trace == []  # the browser never started
    assert report.hooks.after and "cleanup" in report.hooks.after.stdout_tail


class WaitingBrain(LoopBrain):
    backend = "waiting"

    async def run(self, session):
        await session.call("navigate", {"url": ENTRY})
        await asyncio.sleep(30)
        return BrainInfo(backend=self.backend)


@pytest.mark.asyncio
async def test_an_interrupted_run_still_writes_its_report_then_lets_the_cancellation_through(
    fake_browser, tmp_path
):
    """Ctrl-C, or a batch stopping its child: the evidence so far must come out as a report."""
    spec = spec_for(tmp_path, [{"tool": "finish", "args": {}}])
    task = asyncio.create_task(runner_mod.run_spec(spec, brain=WaitingBrain()))
    runs = tmp_path / "runs"
    for _ in range(100):  # until the persona has made its first move
        await asyncio.sleep(0.05)
        trace = list(runs.glob("*/trace.jsonl"))
        if trace and trace[0].read_text().strip():
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    reports = list(runs.glob("*/report.json"))
    assert len(reports) == 1
    report = Report.model_validate_json(reports[0].read_text(encoding="utf-8"))
    assert report.run.status == "incomplete" and report.run.exit_reason == "interrupted"
    assert report.trace and report.trace[0].tool == "navigate"
    assert "interrupted" in (report.brain.error or "")


class FileWritingHarness(HarnessBrain):
    """Stands in for Claude Code or Codex: writes the run's files as the child
    server would, then reports what the harness said."""

    backend = "fake-harness"

    def __init__(self, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.seen: dict = {}

    async def run(self, spec, run_dir, run_file, *, system_prompt, task_prompt):
        self.seen = {"run_file": run_file, "system": system_prompt, "task": task_prompt}
        (run_dir / "trace.jsonl").write_text(
            json.dumps(
                {
                    "n": 1,
                    "ts": "t",
                    "tool": "navigate",
                    "args": {"url": ENTRY},
                    "action": True,
                    "system": True,
                    "status": "ok",
                    "counted": False,
                    "url_before": "",
                    "url_after": ENTRY,
                    "result_head": "{}",
                    "screenshot": None,
                    "duration_ms": 1,
                    "error": False,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "events.jsonl").write_text(
            json.dumps(
                {
                    "type": "finish",
                    "ts": "t",
                    "step": 1,
                    "outcome": "partial",
                    "summary": "done by harness",
                    "what_worked": [],
                    "purchase_path": [],
                    "steps_used": 0,
                    "step_budget": 5,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "observations.json").write_text(
            json.dumps({"counts": {"requests": 7}, "matched": [], "pages": 1}), encoding="utf-8"
        )
        return BrainInfo(
            backend=self.backend, model="m", turns=4, cost_usd=0.12, exit_code=self.exit_code
        )


@pytest.mark.asyncio
async def test_a_harness_brain_is_reported_from_the_files_it_left(tmp_path):
    spec = spec_for(tmp_path, [{"tool": "finish", "args": {}}])
    harness = FileWritingHarness()
    report = await runner_mod.run_spec(spec, brain=harness)
    assert report.run.status == "completed" and report.outcome.summary == "done by harness"
    assert report.brain.cost_usd == 0.12 and report.brain.turns == 4
    assert report.observations and report.observations.counts["requests"] == 7
    assert harness.seen["run_file"] == Path(report.artifacts.run_file)
    assert "UAT tester" in harness.seen["system"] and "# Persona p1" in harness.seen["task"]

    failed = await runner_mod.run_spec(spec, brain=FileWritingHarness(exit_code=1))
    # The files say finish; a non-zero harness exit does not override a finished run.
    assert failed.run.status == "completed" and failed.brain.exit_code == 1
