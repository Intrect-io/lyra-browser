"""Things that make a run's evidence trustworthy: profiles, language, how it moved."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from lyra_browser.uat.prompt import build_system_prompt, build_task_prompt
from lyra_browser.uat.report import BrainInfo, Hooks, build_report
from lyra_browser.uat.spec import BatchSpec, RunSpec, load_batch_spec

PERSONA = {
    "id": "p1",
    "name": "Tester",
    "entry_url": "https://example.com/",
    "goal": "look around",
    "step_budget": 5,
}
TARGET = {"trusted_origins": ["example.com"]}


def spec(**persona_extra) -> dict:
    return {"persona": {**PERSONA, **persona_extra}, "target": TARGET}


def step(n: int, tool: str, **kw) -> dict:
    row = {
        "n": n,
        "ts": "t",
        "tool": tool,
        "args": {},
        "action": True,
        "system": False,
        "status": "ok",
        "counted": True,
        "url_before": "",
        "url_after": "",
        "result_head": "{}",
        "screenshot": None,
        "duration_ms": 1,
        "error": False,
    }
    row.update(kw)
    return row


def write_lines(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def report_for(tmp_path: Path, run_spec: RunSpec, trace: list[dict], events=(), observations=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    write_lines(run_dir / "trace.jsonl", trace)
    write_lines(run_dir / "events.jsonl", list(events))
    now = datetime(2026, 10, 4, tzinfo=UTC)
    return build_report(
        run_spec,
        run_dir,
        run_id="r",
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        brain=BrainInfo(backend="scripted"),
        hooks=Hooks(),
        audit_path=run_dir / "audit.jsonl",
        observations=observations,
    )


FINISH = {
    "type": "finish",
    "ts": "t",
    "step": 1,
    "outcome": "reached_goal",
    "summary": "ok",
    "what_worked": [],
    "purchase_path": [],
    "steps_used": 1,
    "step_budget": 5,
}


def test_a_preseeded_persona_needs_the_profile_that_holds_its_login(tmp_path):
    with pytest.raises(ValidationError, match="preseeded"):
        RunSpec.model_validate(spec(account="preseeded"))
    ok = RunSpec.model_validate({**spec(account="preseeded"), "data_dir": str(tmp_path)})
    assert ok.data_dir == tmp_path


def test_a_preseeded_run_in_an_empty_instance_profile_is_invalid_even_if_it_finished(tmp_path):
    """The shared profile was held by another browser, the session stepped aside to an
    empty one, and the persona ran as a stranger while the report said it was signed in."""
    run_spec = RunSpec.model_validate({**spec(account="preseeded"), "data_dir": str(tmp_path)})
    observations = {"browser": {"profile_mode": "instance"}, "counts": {}, "matched": []}
    report = report_for(
        tmp_path, run_spec, [step(1, "click")], events=[FINISH], observations=observations
    )
    assert report.run.status == "error" and report.run.exit_reason == "browser_error"
    assert any("not signed in" in w for w in report.warnings)

    shared = {"browser": {"profile_mode": "shared"}, "counts": {}, "matched": []}
    fine = report_for(
        tmp_path / "b", run_spec, [step(1, "click")], events=[FINISH], observations=shared
    )
    assert fine.run.status == "completed" and not fine.warnings

    # An anonymous persona does not care which profile it got.
    anon = RunSpec.model_validate(spec())
    still_fine = report_for(
        tmp_path / "c", anon, [step(1, "click")], events=[FINISH], observations=observations
    )
    assert still_fine.run.status == "completed"


def test_moving_by_url_only_is_called_out(tmp_path):
    run_spec = RunSpec.model_validate(spec())
    navigated = report_for(
        tmp_path,
        run_spec,
        [
            step(1, "navigate", args={"url": "https://example.com/pricing"}),
            step(2, "read_page", counted=False, action=False),
        ],
        events=[FINISH],
    )
    assert navigated.metrics.direct_navigations == 1 and navigated.metrics.clicks == 0
    assert any("never clicked" in w for w in navigated.warnings)
    assert navigated.metrics.actions_by_tool == {"navigate": 1}

    clicked = report_for(
        tmp_path / "b",
        run_spec,
        [step(1, "click"), step(2, "navigate")],
        events=[FINISH],
    )
    assert clicked.metrics.clicks == 1 and not clicked.warnings


def test_an_empty_unfinished_run_is_not_clean(tmp_path):
    report = report_for(tmp_path, RunSpec.model_validate(spec()), [])
    assert any("not a clean run" in w for w in report.warnings)


def test_observer_failures_are_surfaced(tmp_path):
    run_spec = RunSpec.model_validate(spec())
    observations = {
        "browser": {"profile_mode": "shared", "observer_errors": ["RuntimeError: boom"]},
        "counts": {},
        "matched": [],
    }
    report = report_for(
        tmp_path, run_spec, [step(1, "click")], events=[FINISH], observations=observations
    )
    assert any("observers failed" in w for w in report.warnings)


def test_prompts_name_the_report_language_and_the_click_rule():
    english = RunSpec.model_validate(spec())
    korean = RunSpec.model_validate({**spec(), "report_language": "Korean"})
    assert "in English, whatever language the site" in build_task_prompt(english)
    assert "in Korean, whatever language the site" in build_task_prompt(korean)
    system = " ".join(build_system_prompt(english).split())  # the prompt wraps its lines
    assert "click the links and buttons you can see" in system
    assert "navigate is for the entry URL" in system
    # Text misses images, canvases and layout: "missing" needs a screenshot first.
    assert "take a screenshot and look" in system


def test_batch_key_must_match_the_persona_id_inside_the_file(tmp_path):
    (tmp_path / "p1.json").write_text(json.dumps(PERSONA))
    batch = {
        "target": TARGET,
        "personas": {"p2": "p1.json"},  # key says p2, the file says p1
        "groups": [{"name": "g", "personas": ["p2"]}],
    }
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(batch))
    with pytest.raises(ValueError, match="does not match the id 'p1'"):
        load_batch_spec(path)


def test_batch_preseeded_persona_needs_a_data_dir_and_parallel_groups_cannot_share_one(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({**PERSONA, "id": "a", "account": "preseeded"}))
    (tmp_path / "b.json").write_text(json.dumps({**PERSONA, "id": "b", "account": "preseeded"}))
    base = {
        "target": TARGET,
        "personas": {"a": "a.json", "b": "b.json"},
        "groups": [{"name": "g", "mode": "parallel", "personas": ["a", "b"]}],
    }
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="preseeded"):
        load_batch_spec(path)

    shared = {**base, "data_dirs": {"a": str(tmp_path / "prof"), "b": str(tmp_path / "prof")}}
    with pytest.raises(ValidationError, match="same data_dir"):
        BatchSpec.model_validate(shared)

    sequential = {
        **shared,
        "groups": [{"name": "g", "mode": "sequential", "personas": ["a", "b"]}],
    }
    path.write_text(json.dumps(sequential))
    assert load_batch_spec(path).groups[0].mode == "sequential"

    own = {**base, "data_dirs": {"a": str(tmp_path / "pa"), "b": str(tmp_path / "pb")}}
    path.write_text(json.dumps(own))
    assert set(load_batch_spec(path).data_dirs) == {"a", "b"}

    with pytest.raises(ValidationError, match="data_dirs name personas"):
        BatchSpec.model_validate({**base, "data_dirs": {"zz": "/x"}})
