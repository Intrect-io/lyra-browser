"""Several personas against one target, in groups, one process each.

A batch writes a run spec per persona and starts ``lyra-uat run`` for each as a
child process. A process per persona is the isolation: its own event loop, its
own browser and profile, and a crash that cannot take a neighbour down — the
server owns one MCP session and one profile at a time, so two personas in one
process would be the bug it exists to prevent.

Groups run in the order listed. A ``parallel`` group starts its personas
together (anonymous visitors); a ``sequential`` group runs them one after
another (personas that share an account or a profile). ``concurrency`` caps how
many children run at once.

Afterwards ``summary.json`` and ``summary.md`` say what each persona did and
lay the earlier findings out as a matrix: which persona judged which finding
FIXED, STILL_THERE or COULD_NOT_CHECK, and which never judged it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from .procs import reap_group, spawn, stop_group, sweeper
from .report import Model, Report
from .spec import BatchSpec, RunSpec, load_persona

Launcher = Callable[[Path], list[str]]

# Slack on top of a run's own wall clock and hooks before the parent gives up on a
# child: the child enforces both itself, this only catches one that hangs outside them.
CHILD_GRACE_S = 180
# How long an interrupted child gets to close its own browser and write its report.
CHILD_STOP_GRACE_S = 30.0


class PersonaResult(Model):
    persona_id: str
    group: str
    status: str  # completed | incomplete | error | crashed
    exit_reason: str | None = None
    outcome: str | None = None
    summary: str = ""
    steps_used: int | None = None
    step_budget: int | None = None
    findings: dict[str, int] = Field(default_factory=dict)
    verdicts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    duration_s: float | None = None
    run_dir: str | None = None
    report_json: str | None = None
    error: str | None = None
    attempts: int = 1


class MatrixRow(Model):
    finding_id: str
    actual: str = ""
    FIXED: list[str] = Field(default_factory=list)
    STILL_THERE: list[str] = Field(default_factory=list)
    COULD_NOT_CHECK: list[str] = Field(default_factory=list)
    unjudged: list[str] = Field(default_factory=list)


class BatchSummary(Model):
    batch_id: str
    started_at: str
    finished_at: str
    duration_s: float
    brain: str
    results: list[PersonaResult]
    matrix: list[MatrixRow] = Field(default_factory=list)
    total_cost_usd: float | None = None
    dir: str
    path: str | None = None
    md_path: str | None = None

    @property
    def all_completed(self) -> bool:
        return bool(self.results) and all(r.status == "completed" for r in self.results)

    def one_line(self) -> str:
        counts = Counter(r.status for r in self.results)
        parts = [f"batch {self.batch_id}: {len(self.results)} personas"]
        parts.append(" ".join(f"{k}={counts[k]}" for k in sorted(counts)))
        findings = sum(sum(r.findings.values()) for r in self.results)
        parts.append(f"findings {findings}")
        if self.total_cost_usd is not None:
            parts.append(f"${self.total_cost_usd:.2f}")
        return " | ".join(parts)


def new_batch_id(now: datetime | None = None) -> str:
    return f"{(now or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')}-batch-{uuid.uuid4().hex[:6]}"


def default_launcher(spec_path: Path) -> list[str]:
    return [sys.executable, "-m", "lyra_browser.uat", "run", str(spec_path)]


# Chrome's markers of a profile in use; a copy must not carry them, or the copy's
# browser would think another one holds it.
_PROFILE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie", "profile.lock")


def make_disposable_profile(source: Path | None, profiles_dir: Path, name: str) -> Path:
    """A profile for one attempt: a copy of ``source`` without its lock files, or empty."""
    target = profiles_dir / name
    if source is not None and source.exists():
        shutil.copytree(
            source, target, symlinks=True, ignore=shutil.ignore_patterns(*_PROFILE_LOCKS)
        )
    else:
        target.mkdir(parents=True)
    return target


class StartLimiter:
    """At most one child start per ``interval_s`` across the batch."""

    def __init__(self, interval_s: float) -> None:
        self.interval_s = interval_s
        self._lock = asyncio.Lock()
        self._last: float | None = None

    async def wait(self) -> None:
        if self.interval_s <= 0:
            return
        async with self._lock:
            if self._last is not None:
                delay = self._last + self.interval_s - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last = time.monotonic()


RETRIED_EXITS = frozenset({"browser_error", "brain_error", "harness_exit"})


def should_retry(result: PersonaResult) -> bool:
    """A crash or an infrastructure failure, not something the persona met on the site."""
    return result.status == "crashed" or result.exit_reason in RETRIED_EXITS


def build_run_spec(batch: BatchSpec, persona_id: str, runs_dir: Path) -> RunSpec:
    """One persona's run spec: its own file, the batch's target, brain and limits."""
    persona = load_persona(batch.personas[persona_id])
    return RunSpec(
        persona=persona,
        target=batch.target,
        brain=batch.brain,
        brief=batch.brief,
        prior_findings=batch.prior_findings,
        hooks=batch.hooks,
        limits=batch.limits,
        tools=batch.tools,
        out_dir=runs_dir,
        data_dir=batch.data_dirs.get(persona_id),
        headless=batch.headless,
        guard_backend=batch.guard_backend,
        driver=batch.driver,
        report_language=batch.report_language,
    )


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


async def _run_child(
    argv: list[str], *, cwd: Path, log_base: Path, timeout_s: float, env: dict[str, str]
) -> tuple[int | None, str, str, bool]:
    proc = await spawn(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
        # The child is a whole run (a harness, its server, a browser): interrupt the group
        # so the run can close its own browser and write its report, then kill what is left.
        await stop_group(proc, grace_s=CHILD_STOP_GRACE_S)
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        await asyncio.shield(stop_group(proc, grace_s=CHILD_STOP_GRACE_S))
        raise
    else:
        await reap_group(proc.pid)
    stdout, stderr = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    log_base.with_suffix(".out.log").write_text(stdout, encoding="utf-8")
    log_base.with_suffix(".err.log").write_text(stderr, encoding="utf-8")
    return proc.returncode, stdout, stderr, timed_out


def _find_report(
    stdout: str, runs_dir: Path, persona_id: str, *, since: float = 0.0
) -> Path | None:
    """The report the child printed, else the newest one of the persona written after
    ``since`` (a retry must never read an earlier attempt's report as its own)."""
    for line in reversed(stdout.strip().splitlines()):
        candidate = Path(line.strip())
        if candidate.name == "report.json" and candidate.is_file():
            return candidate
    matches = sorted(
        p for p in runs_dir.glob(f"*-{persona_id}-*/report.json") if p.stat().st_mtime >= since
    )
    return matches[-1] if matches else None


def _result_from_report(report: Report, group: str, path: Path) -> PersonaResult:
    verdicts = Counter(v.status for v in report.verdicts)
    return PersonaResult(
        persona_id=report.persona.id,
        group=group,
        status=report.run.status,
        exit_reason=report.run.exit_reason,
        outcome=report.outcome.status,
        summary=report.outcome.summary,
        steps_used=report.outcome.steps_used,
        step_budget=report.outcome.step_budget,
        findings=report.severity_counts(),
        verdicts={k: verdicts.get(k, 0) for k in ("FIXED", "STILL_THERE", "COULD_NOT_CHECK")},
        warnings=list(report.warnings),
        cost_usd=report.brain.cost_usd,
        duration_s=report.run.duration_s,
        run_dir=report.artifacts.dir,
        report_json=str(path),
    )


async def run_persona(
    batch: BatchSpec,
    persona_id: str,
    group: str,
    *,
    batch_dir: Path,
    launcher: Launcher,
    semaphore: asyncio.Semaphore,
    limiter: StartLimiter | None = None,
) -> tuple[PersonaResult, Report | None]:
    """Run one persona, again on a crash or an infrastructure failure (``batch.retries``)."""
    attempt = 1
    earlier: list[str] = []
    while True:
        result, report = await _attempt(
            batch,
            persona_id,
            group,
            attempt=attempt,
            batch_dir=batch_dir,
            launcher=launcher,
            semaphore=semaphore,
            limiter=limiter,
        )
        result.attempts = attempt
        result.warnings[:0] = earlier
        if attempt > batch.retries or not should_retry(result):
            return result, report
        why = result.error or result.exit_reason or result.status
        earlier = [*result.warnings, f"attempt {attempt} failed ({result.status}: {why})"]
        await asyncio.sleep(batch.retry_delay_s)
        attempt += 1


async def _attempt(
    batch: BatchSpec,
    persona_id: str,
    group: str,
    *,
    attempt: int,
    batch_dir: Path,
    launcher: Launcher,
    semaphore: asyncio.Semaphore,
    limiter: StartLimiter | None,
) -> tuple[PersonaResult, Report | None]:
    runs_dir = batch_dir / "runs"
    spec = build_run_spec(batch, persona_id, runs_dir)
    name = persona_id if attempt == 1 else f"{persona_id}-attempt{attempt}"
    profile: Path | None = None
    async with semaphore:
        if limiter is not None:
            await limiter.wait()
        if batch.disposable_profiles:
            profile = make_disposable_profile(
                batch.data_dirs.get(persona_id), batch_dir / "profiles", name
            )
            spec.data_dir = profile
            sweeper.watch_dir(profile)
        spec_path = batch_dir / "specs" / f"{name}.json"
        spec_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        argv = launcher(spec_path)
        timeout_s = spec.limits.wall_s + 2 * spec.hooks.timeout_s + CHILD_GRACE_S
        env = {**os.environ, "LYRA_UAT_BATCH_DIR": str(batch_dir)}
        started = time.time()
        try:
            code, stdout, stderr, timed_out = await _run_child(
                argv,
                cwd=batch_dir,
                log_base=batch_dir / "logs" / name,
                timeout_s=timeout_s,
                env=env,
            )
        finally:
            if profile is not None:
                sweeper.unwatch_dir(profile)
                shutil.rmtree(profile, ignore_errors=True)
    report_path = _find_report(stdout, runs_dir, persona_id, since=started - 1)
    if report_path is not None:
        try:
            report = Report.model_validate_json(report_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            report = None
            error = f"report.json is not valid: {exc}"
        else:
            result = _result_from_report(report, group, report_path)
            if timed_out:
                result.warnings.append("child process outlived its wall clock and was killed")
            return result, report
    else:
        report = None
        error = None
    if error is None:
        error = (
            f"no report: child {'timed out' if timed_out else f'exited {code}'}: "
            f"{_tail(stderr) or _tail(stdout)}"
        )
    return (
        PersonaResult(persona_id=persona_id, group=group, status="crashed", error=error),
        None,
    )


def build_matrix(batch: BatchSpec, reports: dict[str, Report]) -> list[MatrixRow]:
    """Earlier findings down, personas across: who judged what.

    Personas are taken in the batch's own order, not the order their children happened
    to finish in, so the same batch reads the same way every time."""
    rows: list[MatrixRow] = []
    ordered = [p for group in batch.groups for p in group.personas if p in reports]
    for prior in batch.prior_findings:
        row = MatrixRow(finding_id=prior.id, actual=prior.actual)
        for persona_id in ordered:
            report = reports[persona_id]
            verdict = next((v for v in report.verdicts if v.finding_id == prior.id), None)
            if verdict is None:
                row.unjudged.append(persona_id)
            else:
                getattr(row, verdict.status).append(persona_id)
        # Personas that never produced a report judged nothing either.
        rows.append(row)
    return rows


def render_markdown(summary: BatchSummary) -> str:
    lines = [
        f"# Batch {summary.batch_id}",
        "",
        f"{len(summary.results)} personas · {summary.duration_s}s · brain {summary.brain}"
        + (f" · ${summary.total_cost_usd:.2f}" if summary.total_cost_usd is not None else ""),
        "",
        "| persona | group | outcome | run | steps | findings B/M/m | verdicts F/S/C | cost |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in summary.results:
        f = r.findings
        v = r.verdicts
        steps = f"{r.steps_used}/{r.step_budget}" if r.steps_used is not None else "-"
        found = f"{f.get('blocker', 0)}/{f.get('major', 0)}/{f.get('minor', 0)}" if f else "-"
        judged = (
            f"{v.get('FIXED', 0)}/{v.get('STILL_THERE', 0)}/{v.get('COULD_NOT_CHECK', 0)}"
            if v
            else "-"
        )
        cost = f"${r.cost_usd:.3f}" if r.cost_usd is not None else "-"
        run = f"{r.status}" + (f" ({r.exit_reason})" if r.exit_reason else "")
        lines.append(
            f"| {r.persona_id} | {r.group} | {r.outcome or '-'} | {run} | {steps} | {found} | "
            f"{judged} | {cost} |"
        )
    problems = [r for r in summary.results if r.error or r.warnings]
    if problems:
        lines += ["", "## Problems", ""]
        for r in problems:
            for text in ([r.error] if r.error else []) + r.warnings:
                lines.append(f"- **{r.persona_id}**: {text}")
    if summary.matrix:
        lines += [
            "",
            "## Earlier findings",
            "",
            "| finding | FIXED | STILL_THERE | COULD_NOT_CHECK | unjudged |",
            "|---|---|---|---|---|",
        ]
        for row in summary.matrix:
            name = f"{row.finding_id} — {row.actual}" if row.actual else row.finding_id
            lines.append(
                f"| {name} | {', '.join(row.FIXED) or '-'} | {', '.join(row.STILL_THERE) or '-'} | "
                f"{', '.join(row.COULD_NOT_CHECK) or '-'} | {', '.join(row.unjudged) or '-'} |"
            )
    lines += ["", f"Reports under {summary.dir}/runs/", ""]
    return "\n".join(lines)


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


async def run_batch(
    batch: BatchSpec,
    *,
    only: list[str] | None = None,
    launcher: Launcher = default_launcher,
) -> BatchSummary:
    """Run the batch's groups in order and write its summary.

    ``only`` restricts the run to those persona ids (groups keep their order and
    mode; a group with none of them is skipped). ``launcher`` builds the child's
    command line from its spec path — the default is ``lyra-uat run``.
    """
    if only:
        unknown = sorted(set(only) - set(batch.personas))
        if unknown:
            raise ValueError(f"--only names personas not in the batch: {unknown}")
    started = datetime.now(UTC)
    batch_id = new_batch_id(started)
    batch_dir = (batch.out_dir / batch_id).resolve()
    for sub in ("specs", "logs", "runs"):
        (batch_dir / sub).mkdir(parents=True, exist_ok=False if sub == "specs" else True)
    semaphore = asyncio.Semaphore(batch.concurrency)
    limiter = StartLimiter(batch.start_interval_s)
    # Killed outright, the batch leaves its children's browsers to the sweeper: every
    # profile a child launches lives under the batch directory unless data_dirs says so.
    sweeper.watch_dir(batch_dir)
    for directory in batch.data_dirs.values():
        sweeper.watch_dir(directory)

    results: list[PersonaResult] = []
    reports: dict[str, Report] = {}

    async def one(persona_id: str, group: str) -> None:
        result, report = await run_persona(
            batch,
            persona_id,
            group,
            batch_dir=batch_dir,
            launcher=launcher,
            semaphore=semaphore,
            limiter=limiter,
        )
        results.append(result)
        if report is not None:
            reports[persona_id] = report

    for group in batch.groups:
        members = [p for p in group.personas if not only or p in only]
        if not members:
            continue
        if group.mode == "parallel":
            await asyncio.gather(*(one(p, group.name) for p in members))
        else:
            for persona_id in members:
                await one(persona_id, group.name)

    finished = datetime.now(UTC)
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    summary = BatchSummary(
        batch_id=batch_id,
        started_at=started.isoformat(timespec="seconds"),
        finished_at=finished.isoformat(timespec="seconds"),
        duration_s=round((finished - started).total_seconds(), 1),
        brain=f"{batch.brain.backend}/{batch.brain.model or '-'}",
        results=sorted(results, key=_order_key(batch)),
        matrix=build_matrix(batch, reports),
        total_cost_usd=round(sum(costs), 4) if costs else None,
        dir=str(batch_dir),
    )
    json_path, md_path = batch_dir / "summary.json", batch_dir / "summary.md"
    summary.path, summary.md_path = str(json_path), str(md_path)
    _write_atomic(json_path, summary.model_dump_json(indent=2))
    _write_atomic(md_path, render_markdown(summary))
    sweeper.unwatch_dir(batch_dir)
    for directory in batch.data_dirs.values():
        sweeper.unwatch_dir(directory)
    return summary


def _order_key(batch: BatchSpec) -> Callable[[PersonaResult], tuple[int, int]]:
    """Results in the batch's own order, whatever order the children finished in."""
    order: dict[tuple[str, str], tuple[int, int]] = {}
    for gi, group in enumerate(batch.groups):
        for pi, persona_id in enumerate(group.personas):
            order[(group.name, persona_id)] = (gi, pi)
    return lambda r: order.get((r.group, r.persona_id), (len(order), 0))


def batch_payload(summary: BatchSummary) -> dict[str, Any]:
    return summary.model_dump(mode="json")
