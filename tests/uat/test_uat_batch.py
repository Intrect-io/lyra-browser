"""The batch runner: group order and parallelism, isolation, the summary matrix.

Children are stub processes (a script that writes a real report), so what is
tested is the batch's own logic — scheduling, spec files, reading reports back,
crashes — not a browser.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from lyra_browser.uat.batch import BatchSummary, run_batch
from lyra_browser.uat.report import Report
from lyra_browser.uat.spec import BatchSpec

STUB = textwrap.dedent(
    """
    import json, os, sys, time
    from datetime import UTC, datetime
    from pathlib import Path
    from lyra_browser.uat.report import BrainInfo, Hooks, build_report, write_report
    from lyra_browser.uat.spec import RunSpec

    spec = RunSpec.model_validate(json.loads(Path(sys.argv[2]).read_text()))
    pid = spec.persona.id
    log = Path(os.environ["STUB_LOG"])
    def mark(what):
        with log.open("a") as fh:
            fh.write(f"{what} {pid} {time.monotonic():.3f}\\n")
    mark("start")
    mode = spec.persona.notes
    if mode == "crash":
        print("boom: no browser", file=sys.stderr)
        sys.exit(3)
    if mode == "crash_once":
        marker = log.with_name(f"crashed-{pid}")
        if not marker.exists():
            marker.write_text("x")
            print("boom: first attempt", file=sys.stderr)
            sys.exit(3)
    if mode == "profile":
        # What the run found in its profile, then dirt for the next one to find.
        found = sorted(q.name for q in spec.data_dir.iterdir()) if spec.data_dir else None
        with log.with_name(f"profile-{pid}").open("a") as fh:
            fh.write(json.dumps({"dir": str(spec.data_dir), "found": found}) + "\\n")
        (spec.data_dir / "dirt").write_text("left by a run")
    time.sleep(float(spec.persona.goal))
    run_dir = spec.out_dir / f"stub-{pid}-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    events = []
    if mode != "unfinished":
        events.append({"type": "finish", "ts": "t", "step": 0, "outcome": "partial",
                       "summary": f"{pid} done", "what_worked": [], "purchase_path": [],
                       "steps_used": 0, "step_budget": 5})
    for f in spec.prior_findings:
        if pid == "a" and f.id == "R1":
            events.append({"type": "verdict", "ts": "t", "step": 0, "finding_id": "R1",
                           "status": "FIXED", "evidence": "fine"})
        if pid == "b" and f.id == "R1":
            events.append({"type": "verdict", "ts": "t", "step": 0, "finding_id": "R1",
                           "status": "STILL_THERE", "evidence": "still"})
    (run_dir / "trace.jsonl").write_text("")
    (run_dir / "events.jsonl").write_text("".join(json.dumps(e) + "\\n" for e in events))
    now = datetime.now(UTC)
    report = build_report(
        spec, run_dir, run_id=f"stub-{pid}", started_at=now, finished_at=now,
        brain=BrainInfo(backend="stub", cost_usd=0.5), hooks=Hooks(),
        audit_path=run_dir / "audit.jsonl",
    )
    write_report(report, run_dir)
    print(report.one_line())
    print(report.artifacts.report_json)
    mark("end")
    """
)


def persona(pid: str, delay: float = 0.0, mode: str = "") -> dict:
    return {
        "id": pid,
        "name": f"Persona {pid}",
        "entry_url": "https://example.com/",
        # The stub reads these two: how long to sleep, and how to misbehave.
        "goal": str(delay),
        "notes": mode,
        "step_budget": 5,
    }


def make_batch(tmp_path: Path, personas: dict[str, dict], groups: list[dict], **extra) -> BatchSpec:
    files = {}
    for pid, data in personas.items():
        path = tmp_path / f"{pid}.json"
        path.write_text(json.dumps(data))
        files[pid] = path
    return BatchSpec.model_validate(
        {
            "target": {"trusted_origins": ["example.com"]},
            "brain": {"backend": "scripted", "script": [{"tool": "finish", "args": {}}]},
            "personas": {pid: str(p) for pid, p in files.items()},
            "groups": groups,
            "out_dir": str(tmp_path / "out"),
            **extra,
        }
    )


@pytest.fixture
def stub_launcher(tmp_path, monkeypatch):
    script = tmp_path / "stub_run.py"
    script.write_text(STUB)
    log = tmp_path / "stub.log"
    monkeypatch.setenv("STUB_LOG", str(log))

    def launcher(spec_path: Path) -> list[str]:
        return [sys.executable, str(script), "run", str(spec_path)]

    launcher.log = log
    return launcher


def events(log: Path) -> dict[tuple[str, str], float]:
    out = {}
    for line in log.read_text().splitlines():
        what, pid, t = line.split()
        out[(what, pid)] = float(t)
    return out


@pytest.mark.asyncio
async def test_parallel_group_overlaps_and_sequential_group_does_not(tmp_path, stub_launcher):
    batch = make_batch(
        tmp_path,
        {pid: persona(pid, 0.7) for pid in ("a", "b", "c", "d")},
        [
            {"name": "anon", "mode": "parallel", "personas": ["a", "b"]},
            {"name": "account", "mode": "sequential", "personas": ["c", "d"]},
        ],
    )
    summary = await run_batch(batch, launcher=stub_launcher)
    t = events(stub_launcher.log)
    # Parallel: both started before either ended.
    assert t[("start", "a")] < t[("end", "b")] and t[("start", "b")] < t[("end", "a")]
    # Sequential: d starts only after c ended; and the groups ran in order.
    assert t[("end", "c")] <= t[("start", "d")]
    assert max(t[("end", "a")], t[("end", "b")]) <= t[("start", "c")]
    assert [r.persona_id for r in summary.results] == ["a", "b", "c", "d"]
    assert summary.all_completed and summary.total_cost_usd == 2.0


@pytest.mark.asyncio
async def test_concurrency_limit_serialises_a_parallel_group(tmp_path, stub_launcher):
    batch = make_batch(
        tmp_path,
        {pid: persona(pid, 0.5) for pid in ("a", "b")},
        [{"name": "anon", "mode": "parallel", "personas": ["a", "b"]}],
        concurrency=1,
    )
    await run_batch(batch, launcher=stub_launcher)
    t = events(stub_launcher.log)
    first, second = sorted(("a", "b"), key=lambda p: t[("start", p)])
    assert t[("end", first)] <= t[("start", second)]


@pytest.mark.asyncio
async def test_each_persona_gets_its_own_spec_file_and_run_directory(tmp_path, stub_launcher):
    batch = make_batch(
        tmp_path,
        {"a": persona("a"), "b": persona("b")},
        [{"name": "g", "personas": ["a", "b"]}],
        report_language="Korean",
        brief="Round brief.",
    )
    summary = await run_batch(batch, launcher=stub_launcher)
    batch_dir = Path(summary.dir)
    for pid in ("a", "b"):
        spec = json.loads((batch_dir / "specs" / f"{pid}.json").read_text())
        assert spec["persona"]["id"] == pid
        assert spec["report_language"] == "Korean" and spec["brief"] == "Round brief."
        assert Path(spec["out_dir"]) == batch_dir / "runs"
        assert (batch_dir / "logs" / f"{pid}.out.log").is_file()
    a, b = summary.results
    assert a.run_dir != b.run_dir and Path(a.report_json).is_file()


@pytest.mark.asyncio
async def test_a_crashed_child_is_a_result_not_a_batch_failure(tmp_path, stub_launcher):
    batch = make_batch(
        tmp_path,
        {"a": persona("a", mode="crash"), "b": persona("b", mode="unfinished")},
        [{"name": "g", "mode": "parallel", "personas": ["a", "b"]}],
    )
    summary = await run_batch(batch, launcher=stub_launcher)
    by_id = {r.persona_id: r for r in summary.results}
    assert by_id["a"].status == "crashed" and "boom: no browser" in by_id["a"].error
    assert "exited 3" in by_id["a"].error and by_id["a"].report_json is None
    assert by_id["b"].status == "incomplete" and by_id["b"].exit_reason == "no_finish"
    assert not summary.all_completed
    assert "crashed=1" in summary.one_line()


@pytest.mark.asyncio
async def test_summary_has_the_verdict_matrix_and_is_written_as_json_and_markdown(
    tmp_path, stub_launcher
):
    batch = make_batch(
        tmp_path,
        {pid: persona(pid) for pid in ("a", "b", "c")},
        [{"name": "g", "mode": "parallel", "personas": ["a", "b", "c"]}],
        prior_findings=[
            {"id": "R1", "actual": "no cancel link"},
            {"id": "R2", "actual": "price unclear"},
        ],
    )
    summary = await run_batch(batch, launcher=stub_launcher)
    rows = {r.finding_id: r for r in summary.matrix}
    assert rows["R1"].FIXED == ["a"] and rows["R1"].STILL_THERE == ["b"]
    assert rows["R1"].unjudged == ["c"]
    assert rows["R2"].unjudged == ["a", "b", "c"]
    loaded = BatchSummary.model_validate_json(Path(summary.path).read_text())
    assert loaded.batch_id == summary.batch_id and len(loaded.results) == 3
    md = Path(summary.md_path).read_text()
    assert "| a | g | partial | completed (finish) |" in md
    assert "| R1 — no cancel link | a | b | - | c |" in md
    assert not list(Path(summary.dir).glob("*.part"))
    # Each persona's report is a normal report.
    assert Report.model_validate_json(Path(summary.results[0].report_json).read_text())


@pytest.mark.asyncio
async def test_only_restricts_the_run_and_unknown_ids_are_refused(tmp_path, stub_launcher):
    batch = make_batch(
        tmp_path,
        {"a": persona("a"), "b": persona("b")},
        [{"name": "g1", "personas": ["a"]}, {"name": "g2", "personas": ["b"]}],
    )
    summary = await run_batch(batch, only=["b"], launcher=stub_launcher)
    assert [r.persona_id for r in summary.results] == ["b"]
    with pytest.raises(ValueError, match="not in the batch"):
        await run_batch(batch, only=["zz"], launcher=stub_launcher)


@pytest.mark.asyncio
async def test_a_crash_is_retried_and_the_attempts_are_counted(tmp_path, stub_launcher):
    batch = make_batch(
        tmp_path,
        {"a": persona("a", mode="crash_once"), "b": persona("b", mode="crash")},
        [{"name": "g", "personas": ["a", "b"]}],
        retries=2,
        retry_delay_s=0,
    )
    summary = await run_batch(batch, launcher=stub_launcher)
    by_id = {r.persona_id: r for r in summary.results}
    assert by_id["a"].status == "completed" and by_id["a"].attempts == 2
    assert any("attempt 1 failed" in w for w in by_id["a"].warnings)
    # A child that always crashes is tried 1 + retries times, then reported as crashed.
    assert by_id["b"].status == "crashed" and by_id["b"].attempts == 3
    batch_dir = Path(summary.dir)
    assert (batch_dir / "specs" / "a-attempt2.json").is_file()
    assert (batch_dir / "logs" / "b-attempt3.err.log").is_file()


@pytest.mark.asyncio
async def test_what_the_persona_met_is_not_retried(tmp_path, stub_launcher):
    batch = make_batch(
        tmp_path,
        {"a": persona("a", mode="unfinished")},
        [{"name": "g", "personas": ["a"]}],
        retries=3,
        retry_delay_s=0,
    )
    summary = await run_batch(batch, launcher=stub_launcher)
    (result,) = summary.results
    assert result.exit_reason == "no_finish" and result.attempts == 1


def test_should_retry_only_infrastructure_failures():
    from lyra_browser.uat.batch import PersonaResult, should_retry

    def r(status, reason=None):
        return PersonaResult(persona_id="a", group="g", status=status, exit_reason=reason)

    assert should_retry(r("crashed"))
    assert should_retry(r("error", "browser_error"))
    assert should_retry(r("incomplete", "brain_error"))
    assert should_retry(r("error", "harness_exit"))
    for reason in ("finish", "budget_exhausted", "wall_timeout", "no_finish", "interrupted"):
        assert not should_retry(r("incomplete", reason))


@pytest.mark.asyncio
async def test_start_interval_spaces_the_children_of_a_parallel_group(tmp_path, stub_launcher):
    # Three children of a parallel group, one start per 0.8 s: the batch cannot finish
    # before the third has been let through (a child's own clock is too noisy to compare:
    # its interpreter takes a varying time to start).
    batch = make_batch(
        tmp_path,
        {pid: persona(pid) for pid in ("a", "b", "c")},
        [{"name": "anon", "mode": "parallel", "personas": ["a", "b", "c"]}],
        start_interval_s=0.8,
    )
    summary = await run_batch(batch, launcher=stub_launcher)
    assert summary.all_completed and summary.duration_s >= 1.6


@pytest.mark.asyncio
async def test_start_limiter_lets_one_start_through_per_interval():
    import asyncio
    import time

    from lyra_browser.uat.batch import StartLimiter

    limiter = StartLimiter(0.2)
    times: list[float] = []

    async def start() -> None:
        await limiter.wait()
        times.append(time.monotonic())

    await asyncio.gather(*(start() for _ in range(4)))
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    assert len(gaps) == 3 and all(gap >= 0.19 for gap in gaps), gaps


@pytest.mark.asyncio
async def test_start_limiter_without_an_interval_does_not_wait():
    import time

    from lyra_browser.uat.batch import StartLimiter

    limiter = StartLimiter(0)
    started = time.monotonic()
    for _ in range(5):
        await limiter.wait()
    assert time.monotonic() - started < 0.05


@pytest.mark.asyncio
async def test_disposable_profiles_copy_the_seed_and_leave_nothing_behind(tmp_path, stub_launcher):
    seed = tmp_path / "seed"
    (seed / "profile" / "Default").mkdir(parents=True)
    (seed / "profile" / "Default" / "Cookies").write_text("login")
    (seed / "profile" / "SingletonLock").write_text("held")
    (seed / "profile.lock").write_text("")
    batch = make_batch(
        tmp_path,
        {"a": persona("a", mode="profile"), "b": persona("b", mode="profile")},
        [{"name": "g", "personas": ["a", "b", "a"]}],
        data_dirs={"a": str(seed)},
        disposable_profiles=True,
    )
    summary = await run_batch(batch, launcher=stub_launcher)
    assert summary.all_completed
    seen_a = [json.loads(x) for x in (tmp_path / "profile-a").read_text().splitlines()]
    seen_b = [json.loads(x) for x in (tmp_path / "profile-b").read_text().splitlines()]
    # Both runs of a got a fresh copy of the seed: no lock files, and no dirt from the first.
    assert [s["found"] for s in seen_a] == [["profile"], ["profile"]]
    assert all(Path(s["dir"]) != seed for s in seen_a)
    # b has no seed: an empty profile of its own.
    assert seen_b[0]["found"] == []
    # The seed was never written to, and every copy is gone.
    assert sorted(q.name for q in (seed / "profile").iterdir()) == ["Default", "SingletonLock"]
    assert not (seed / "dirt").exists()
    assert not any((Path(summary.dir) / "profiles").iterdir())


def test_make_disposable_profile_skips_lock_files(tmp_path):
    from lyra_browser.uat.batch import make_disposable_profile

    seed = tmp_path / "seed"
    (seed / "profile").mkdir(parents=True)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "Local State"):
        (seed / "profile" / name).write_text("x")
    (seed / "profile.lock").write_text("")
    copy = make_disposable_profile(seed, tmp_path / "profiles", "a")
    assert sorted(q.name for q in copy.rglob("*")) == ["Local State", "profile"]
