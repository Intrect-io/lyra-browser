"""Stopping a harness: the whole process group goes, not just the child it started.

A harness is three processes deep (harness, MCP server, browser). These tests use a
stub that ignores SIGINT and SIGTERM and leaves a grandchild behind, which is what a
stuck harness looks like, and check that nothing survives."""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from lyra_browser.uat import procs
from lyra_browser.uat.batch import _run_child
from lyra_browser.uat.brains import harness

STUBBORN = textwrap.dedent(
    """
    import os, signal, subprocess, sys, time
    from pathlib import Path
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pids = Path(sys.argv[1])
    # A grandchild in the same group, as an MCP server would be; it ignores them too.
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids.write_text(f"{os.getpid()} {child.pid}")
    time.sleep(300)
    """
)

LEAVER = textwrap.dedent(
    """
    import os, subprocess, sys
    from pathlib import Path
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}")
    """
)


pytestmark = []  # the CLI signal test below needs only POSIX; the process tests need /proc
needs_proc = pytest.mark.skipif(
    not os.path.isdir("/proc/self"), reason="the stubs' liveness check reads /proc"
)


def running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


async def wait_for_pids(path: Path, timeout: float = 10.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip().count(" ") == 1:
            return [int(p) for p in path.read_text().split()]
        await asyncio.sleep(0.05)
    raise AssertionError("the stub never reported its pids")


async def wait_dead(pids: list[int], timeout: float = 8.0) -> list[int]:
    deadline = time.monotonic() + timeout
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [p for p in alive if running(p)]
        await asyncio.sleep(0.05)
    return alive


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(procs, "LINGER_S", 0.5)
    monkeypatch.setattr(procs, "KILL_AFTER_S", 2.0)
    monkeypatch.setattr(harness, "GRACE_S", 0.5)


@pytest.fixture
def stub(tmp_path):
    def make(body: str) -> tuple[list[str], Path]:
        script = tmp_path / "stub.py"
        script.write_text(body)
        pids = tmp_path / "pids"
        return [sys.executable, str(script), str(pids)], pids

    return make


@needs_proc
@pytest.mark.asyncio
async def test_stop_group_kills_a_harness_that_ignores_signals_and_its_children(stub):
    argv, pids_file = stub(STUBBORN)
    proc = await procs.spawn(argv, stdout=asyncio.subprocess.DEVNULL)
    pids = await wait_for_pids(pids_file)
    assert all(running(p) for p in pids)

    await procs.stop_group(proc, grace_s=0.5)
    survivors = await wait_dead(pids)
    assert survivors == [], f"left running: {survivors}"


@needs_proc
@pytest.mark.asyncio
async def test_a_cancelled_run_process_takes_the_whole_group_with_it(stub, tmp_path):
    argv, pids_file = stub(STUBBORN)
    task = asyncio.create_task(
        harness.run_process(argv, cwd=tmp_path, env=dict(os.environ), log_dir=tmp_path)
    )
    pids = await wait_for_pids(pids_file)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    survivors = await wait_dead(pids)
    assert survivors == [], f"left running: {survivors}"


@needs_proc
@pytest.mark.asyncio
async def test_what_a_finished_harness_left_running_is_stopped_too(stub, tmp_path):
    """The harness exits 0 without closing its server: the server must not outlive the run."""
    argv, pids_file = stub(LEAVER)
    result = await harness.run_process(argv, cwd=tmp_path, env=dict(os.environ), log_dir=tmp_path)
    assert result.exit_code == 0
    pids = [int(p) for p in pids_file.read_text().split()]
    survivors = await wait_dead(pids)
    assert survivors == [], f"left running: {survivors}"


@needs_proc
@pytest.mark.asyncio
async def test_a_batch_child_that_hangs_is_killed_with_its_descendants(stub, tmp_path, monkeypatch):
    from lyra_browser.uat import batch

    monkeypatch.setattr(batch, "CHILD_STOP_GRACE_S", 0.5)
    argv, pids_file = stub(STUBBORN)
    code, stdout, stderr, timed_out = await _run_child(
        argv,
        cwd=tmp_path,
        log_base=tmp_path / "child",
        timeout_s=1.0,
        env=dict(os.environ),
    )
    assert timed_out is True
    pids = [int(p) for p in pids_file.read_text().split()]
    survivors = await wait_dead(pids)
    assert survivors == [], f"left running: {survivors}"
    assert (tmp_path / "child.out.log").exists()


def test_group_alive_ignores_zombies_and_missing_groups():
    assert procs.group_alive(2**22 - 3) is False  # no such group


CLI_SCRIPT = textwrap.dedent(
    """
    import asyncio, signal, sys
    # What a background job of a non-interactive shell inherits: SIGINT ignored.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    from lyra_browser.uat.cli import run_with_signals

    async def work():
        print("ready", flush=True)
        await asyncio.sleep(60)

    try:
        run_with_signals(work())
    except asyncio.CancelledError:
        print("cancelled", flush=True)
        sys.exit(130)
    """
)


@pytest.mark.parametrize("sig", ["SIGINT", "SIGTERM"])
def test_the_cli_cancels_on_sigint_and_sigterm_even_if_sigint_was_inherited_as_ignored(sig):
    """`kill -INT` on a backgrounded run must clean up and exit, not be ignored; a plain
    `kill` must not end the process without its cleanup."""
    import signal
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", CLI_SCRIPT], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(getattr(signal, sig))
        out, _ = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130 and "cancelled" in out
