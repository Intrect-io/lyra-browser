"""Stored logins, secret scrubbing and giving up on a bot check, in a UAT run.

The run goes through the real runner and server on the fake page, so what is checked is
what a model and the run's files would hold: the persona names a credential, the server
types it, and the value is nowhere but in the field it was typed into.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from conftest import FakePage, FakeSession
from lyra_browser import challenge as ch
from lyra_browser.scrub import MARK, Scrubber
from lyra_browser.totp import totp
from lyra_browser.uat import runner as runner_mod
from lyra_browser.uat import server as server_mod
from lyra_browser.uat.spec import RunSpec

ENTRY = "https://start.example/"
PASSWORD = "canary-Pw-7f3a9c!"
SEED = base64.b32encode(b"canary-seed-0123456789").decode().rstrip("=")


@pytest.fixture
def fake_browser(monkeypatch, page: FakePage):
    async def build(spec, run_dir, **kw):
        kw.setdefault("session_factory", lambda cfg: FakeSession(page))
        mcp, ctx, recorder = await server_mod.build_uat_server(spec, run_dir, **kw)
        ctx.config.download_settle_s = 0.0
        return mcp, ctx, recorder

    monkeypatch.setattr(runner_mod, "build_uat_server", build)
    return page


@pytest.fixture
def secrets_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CANARY_PASSWORD", PASSWORD)
    seed_file = tmp_path / "seed.txt"
    seed_file.write_text(SEED + "\n")
    return seed_file


def spec_for(tmp_path: Path, script: list[dict], seed_file: Path, **extra) -> RunSpec:
    return RunSpec.model_validate(
        {
            "persona": {
                "id": "p1",
                "name": "Tester",
                "entry_url": ENTRY,
                "goal": "sign in",
                "step_budget": 10,
            },
            "target": {
                "trusted_origins": ["start.example"],
                "trusted_send_origins": ["start.example"],
            },
            "brain": {"backend": "scripted", "script": script},
            "credentials": {
                "demo": {
                    "origin": "https://start.example",
                    "username": "demo@example.com",
                    "password_env": "CANARY_PASSWORD",
                    "totp_file": str(seed_file),
                },
                "elsewhere": {"origin": "https://other.example", "password_env": "CANARY_PASSWORD"},
                "missing": {"origin": "https://start.example", "password_env": "NOT_SET_ANYWHERE"},
            },
            "out_dir": str(tmp_path / "runs"),
            **extra,
        }
    )


def fill(name: str, field: str, selector: str) -> dict:
    return {"tool": "fill_credential", "args": {"name": name, "field": field, "selector": selector}}


def all_text(run_dir: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in run_dir.rglob("*") if p.is_file()
    )


@pytest.mark.asyncio
async def test_a_login_is_typed_and_its_secrets_are_nowhere_in_the_run(
    fake_browser, tmp_path, secrets_env, monkeypatch
):
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    script = [
        {"tool": "navigate", "args": {"url": ENTRY}, "expect": "ok"},
        fill("demo", "username", "#u"),
        fill("demo", "password", "#p"),
        fill("demo", "totp", "#c"),
        fill("elsewhere", "password", "#p"),
        fill("missing", "password", "#p"),
        fill("nobody", "password", "#p"),
        {"tool": "finish", "args": {"outcome": "reached_goal", "summary": "Signed in."}},
    ]
    spec = spec_for(tmp_path, script, secrets_env)
    assert "fill_credential" in spec.offered_tools()
    report = await runner_mod.run_spec(spec)

    fills = [c for c in fake_browser.calls if c[0] == "fill"]
    assert fills[0] == ("fill", "#u", "demo@example.com")
    assert fills[1] == ("fill", "#p", PASSWORD)
    assert fills[2][1] == "#c" and fills[2][2] in {totp(SEED, at=t) for t in _now_window()}
    assert len(fills) == 3, "nothing is typed on another site or for a missing secret"

    statuses = [s.status for s in report.trace if s.tool == "fill_credential"]
    assert statuses == ["ok", "ok", "ok", "wrong_origin", "credential_unavailable", "error"]
    unavailable = next(s for s in report.trace if s.status == "credential_unavailable")
    assert "NOT_SET_ANYWHERE" in unavailable.result_head

    run_dir = Path(report.artifacts.dir)
    everything = all_text(run_dir)
    assert PASSWORD not in everything and SEED not in everything
    assert "CANARY_PASSWORD" in (run_dir / "run.json").read_text(), "only the reference is kept"


def _now_window():
    import time

    now = time.time()
    return (now - 30, now, now + 30)


@pytest.mark.asyncio
async def test_a_page_that_echoes_the_password_is_scrubbed_before_the_model_reads_it(
    fake_browser, tmp_path, secrets_env, monkeypatch
):
    from lyra_browser.uat import recorder as recorder_mod

    seen: list[str] = []
    original = recorder_mod.scrub_result

    def spy(result, scrub):
        out = original(result, scrub)
        seen.extend(getattr(item, "text", "") for item in out.content)
        return out

    monkeypatch.setattr(recorder_mod, "scrub_result", spy)
    fake_browser._title = f"Welcome {PASSWORD}"
    script = [
        {"tool": "navigate", "args": {"url": ENTRY}, "expect": "ok"},
        {"tool": "get_url", "args": {}},
        {"tool": "finish", "args": {"outcome": "partial", "summary": "Looked."}},
    ]
    report = await runner_mod.run_spec(spec_for(tmp_path, script, secrets_env))
    assert seen and all(PASSWORD not in text for text in seen)
    assert any(MARK in text for text in seen)
    assert PASSWORD not in all_text(Path(report.artifacts.dir))


@pytest.mark.asyncio
async def test_the_same_check_three_times_ends_the_run_as_challenge_abandoned(
    fake_browser, tmp_path, secrets_env, monkeypatch
):
    from lyra_browser.tools import navigation

    async def observe(page, http_status=None):
        return ch.Challenge("cloudflare", ch.INTERACTIVE, "title")

    monkeypatch.setattr(navigation, "observe", observe)
    script = [{"tool": "navigate", "args": {"url": ENTRY}} for _ in range(3)] + [
        {"tool": "click", "args": {"selector": "#a"}},
        {"tool": "note", "args": {"text": "walled"}},
    ]
    report = await runner_mod.run_spec(spec_for(tmp_path, script, secrets_env))
    statuses = [s.status for s in report.trace]
    walled = "challenge_abandoned"
    assert statuses == ["challenge", "challenge", walled, walled, "ok"]
    assert report.run.status == "incomplete" and report.run.exit_reason == "challenge_abandoned"
    assert any(p.kind == "challenge_abandoned" for p in report.policy.events)


def test_the_scrubber_replaces_every_encoding_of_a_secret():
    scrub = Scrubber([PASSWORD, "abc"])  # too short to hold
    assert scrub.text(f"pw={PASSWORD}") == f"pw={MARK}"
    assert scrub.text("pass=canary-Pw-7f3a9c%21") == f"pass={MARK}"
    assert scrub.text("abc stays") == "abc stays"
    assert scrub.data({"a": [f"x {PASSWORD}"]}) == {"a": [f"x {MARK}"]}
    assert not Scrubber([])


def test_a_credential_spec_names_one_source_per_secret():
    from pydantic import ValidationError

    from lyra_browser.uat.spec import CredentialRef

    with pytest.raises(ValidationError):
        CredentialRef(origin="https://a.example", password_env="X", password_file="/tmp/x")
    with pytest.raises(ValidationError):
        CredentialRef(origin="https://a.example", totp_env="X", totp_file="/tmp/x")
