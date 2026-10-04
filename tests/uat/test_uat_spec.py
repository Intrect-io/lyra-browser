"""Run and batch specs: what is accepted, what is refused, how files compose."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lyra_browser.uat.spec import (
    DEFAULT_TOOLS,
    BatchSpec,
    BrainSpec,
    Persona,
    RunSpec,
    Target,
    Viewport,
    load_batch_spec,
    load_persona,
    load_run_spec,
)

PERSONA = {
    "id": "p1",
    "name": "Mobile visitor",
    "entry_url": "https://example.com/",
    "viewport": "390x844",
    "goal": "Find the free demo without paying.",
    "step_budget": 12,
    "must_not": ["sign up"],
}
TARGET = {"trusted_origins": ["example.com", "*.example.com"]}


def _write(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_viewport_parses_the_wxh_form_and_refuses_nonsense():
    assert Viewport.parse("390x844") == Viewport(width=390, height=844)
    assert str(Viewport.parse(" 1280 × 900 ")) == "1280x900"
    with pytest.raises(ValueError):
        Viewport.parse("wide")
    with pytest.raises(ValidationError):
        Persona.model_validate({**PERSONA, "viewport": "10x10"})


def test_persona_id_and_entry_url_are_checked():
    with pytest.raises(ValidationError):
        Persona.model_validate({**PERSONA, "id": "P 1"})
    with pytest.raises(ValidationError):
        Persona.model_validate({**PERSONA, "entry_url": "file:///etc/passwd"})


def test_unknown_keys_are_refused_everywhere():
    """A misspelt field must fail loudly, not silently drop a budget or a guard."""
    with pytest.raises(ValidationError):
        Persona.model_validate({**PERSONA, "step_bugdet": 3})
    with pytest.raises(ValidationError):
        Target.model_validate({**TARGET, "proxies": []})
    with pytest.raises(ValidationError):
        RunSpec.model_validate({"persona": PERSONA, "target": TARGET, "brains": {}})


def test_target_needs_at_least_one_origin_and_valid_capture_regexes():
    with pytest.raises(ValidationError):
        Target.model_validate({"trusted_origins": []})
    with pytest.raises(ValidationError):
        Target.model_validate({**TARGET, "network_capture": ["("]})
    assert Target.model_validate({**TARGET, "network_capture": [r"/g/collect"]})


def test_brain_backends_demand_what_they_need():
    with pytest.raises(ValidationError):
        BrainSpec(backend="openai-compat")
    assert BrainSpec(backend="openai-compat", base_url="http://127.0.0.1:1/v1")
    with pytest.raises(ValidationError):
        BrainSpec(backend="scripted")
    assert BrainSpec(backend="scripted", script=[{"tool": "finish", "args": {}}])
    assert BrainSpec().backend == "claude-code"


def test_offered_tools_default_set_uploads_and_deny(tmp_path):
    spec = RunSpec.model_validate({"persona": PERSONA, "target": TARGET})
    assert spec.offered_tools() == list(DEFAULT_TOOLS)
    assert "upload_file" not in spec.offered_tools()
    assert "publish" not in spec.offered_tools()

    with_uploads = RunSpec.model_validate(
        {"persona": {**PERSONA, "uploads_allowed_dir": str(tmp_path)}, "target": TARGET}
    )
    assert with_uploads.offered_tools()[-1] == "upload_file"

    narrowed = RunSpec.model_validate(
        {"persona": PERSONA, "target": TARGET, "tools": {"deny": ["hover", "tabs"]}}
    )
    assert "hover" not in narrowed.offered_tools()
    assert "click" in narrowed.offered_tools()


def test_all_trusted_origins_adds_the_personas_extras():
    spec = RunSpec.model_validate(
        {
            "persona": {**PERSONA, "extra_trusted_origins": ["checkout.example.net"]},
            "target": TARGET,
        }
    )
    assert spec.all_trusted_origins() == ["example.com", "*.example.com", "checkout.example.net"]


def test_run_file_composes_from_referenced_files(tmp_path):
    """One target and one brief serve many personas: the run file points at them."""
    _write(tmp_path / "p1.json", PERSONA)
    _write(tmp_path / "target.json", TARGET)
    (tmp_path / "brief.md").write_text("# Round 5\nOpen checkouts only.\n", encoding="utf-8")
    _write(tmp_path / "prior.json", [{"id": "F1", "actual": "button did nothing"}])
    run = _write(
        tmp_path / "run.json",
        {
            "persona_file": "p1.json",
            "target_file": "target.json",
            "brief_file": "brief.md",
            "prior_findings_file": "prior.json",
            "brain": {"backend": "scripted", "script": [{"tool": "finish", "args": {}}]},
        },
    )
    spec = load_run_spec(run)
    assert spec.persona.id == "p1"
    assert spec.target.trusted_origins == ["example.com", "*.example.com"]
    assert spec.brief == "# Round 5\nOpen checkouts only."
    assert [f.id for f in spec.prior_findings] == ["F1"]
    assert spec.brain.backend == "scripted"


def test_run_file_refuses_both_inline_and_file_for_one_key(tmp_path):
    _write(tmp_path / "target.json", TARGET)
    run = _write(
        tmp_path / "run.json",
        {"persona": PERSONA, "target": TARGET, "target_file": "target.json"},
    )
    with pytest.raises(ValueError, match="either `target` or `target_file`"):
        load_run_spec(run)


def test_persona_only_file_with_target_override(tmp_path):
    """The persona-per-file layout: the target comes from the command line."""
    persona = _write(tmp_path / "p1.json", PERSONA)
    spec = load_run_spec(persona, target=TARGET, brain={"backend": "codex", "model": "o4"})
    assert spec.persona.name == "Mobile visitor"
    assert spec.brain.backend == "codex" and spec.brain.model == "o4"
    # A None override is "not given", not "set to null".
    assert load_run_spec(persona, target=TARGET, brain=None).brain.backend == "claude-code"


def test_overrides_merge_into_mappings_rather_than_replace(tmp_path):
    run = _write(
        tmp_path / "run.json",
        {"persona": PERSONA, "target": TARGET, "brain": {"backend": "codex", "max_turns": 7}},
    )
    spec = load_run_spec(run, brain={"model": "gpt-x"})
    assert spec.brain.backend == "codex"
    assert spec.brain.max_turns == 7
    assert spec.brain.model == "gpt-x"


def test_load_persona_accepts_bare_and_nested_forms(tmp_path):
    bare = _write(tmp_path / "bare.json", PERSONA)
    nested = _write(tmp_path / "nested.json", {"persona": PERSONA})
    assert load_persona(bare).id == load_persona(nested).id == "p1"


def test_batch_resolves_persona_paths_and_checks_groups(tmp_path):
    _write(tmp_path / "p1.json", PERSONA)
    _write(tmp_path / "p2.json", {**PERSONA, "id": "p2"})
    batch = _write(
        tmp_path / "batch.json",
        {
            "target": TARGET,
            "personas": {"p1": "p1.json", "p2": "p2.json"},
            "groups": [
                {"name": "anonymous", "mode": "parallel", "personas": ["p1"]},
                {"name": "signed-in", "personas": ["p2"]},
            ],
        },
    )
    spec = load_batch_spec(batch)
    assert spec.personas["p2"] == (tmp_path / "p2.json").resolve()
    assert spec.groups[1].mode == "sequential"

    with pytest.raises(ValidationError, match="not in `personas`"):
        BatchSpec.model_validate(
            {
                "target": TARGET,
                "personas": {"p1": "p1.json"},
                "groups": [{"name": "g", "personas": ["p9"]}],
            }
        )


def test_yaml_files_load_when_pyyaml_is_present(tmp_path):
    yaml = pytest.importorskip("yaml")
    run = tmp_path / "run.yaml"
    run.write_text(
        yaml.safe_dump({"persona": PERSONA, "target": TARGET}, sort_keys=False), encoding="utf-8"
    )
    assert load_run_spec(run).persona.viewport.width == 390
