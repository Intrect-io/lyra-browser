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


# The parent a sweeper protects: it starts a stubborn harness group through ``spawn`` and a
# "browser" in a session of its own (as Playwright's driver launches Chrome) whose
# ``--user-data-dir`` points into a watched directory, reports every pid, and waits to be
# killed with SIGKILL, which it cannot handle.
KILLED_PARENT = textwrap.dedent(
    """
    import asyncio, os, subprocess, sys
    from pathlib import Path
    from lyra_browser.uat import procs

    stub, pids_out, profile = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    group_pids = Path(str(pids_out) + ".group")

    async def main():
        procs.sweeper.watch_dir(profile)
        await procs.spawn([sys.executable, stub, str(group_pids)],
                          stdout=asyncio.subprocess.DEVNULL)
        browser = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(300)",
             f"--user-data-dir={profile / 'profile'}"],
            start_new_session=True)
        while not group_pids.exists() or group_pids.read_text().count(" ") != 1:
            await asyncio.sleep(0.05)
        pids_out.write_text(f"{group_pids.read_text()} {browser.pid} {procs.sweeper.pid}")
        await asyncio.sleep(300)

    asyncio.run(main())
    """
)


@needs_proc
@pytest.mark.asyncio
async def test_a_sigkilled_parent_leaves_no_orphans(stub, tmp_path):
    """SIGKILL cannot be handled: the sweeper stops the parent's groups and its browsers."""
    import signal
    import subprocess

    argv, _ = stub(STUBBORN)
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(KILLED_PARENT)
    pids_file, profile = tmp_path / "all-pids", tmp_path / "run-profile"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    parent = subprocess.Popen(
        [sys.executable, str(parent_script), argv[1], str(pids_file), str(profile)], env=env
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if pids_file.exists() and len(pids_file.read_text().split()) == 4:
                break
            await asyncio.sleep(0.05)
        harness_pid, server_pid, browser_pid, sweeper_pid = map(int, pids_file.read_text().split())
        assert all(running(p) for p in (harness_pid, server_pid, browser_pid, sweeper_pid))
        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=5)
        survivors = await wait_dead([harness_pid, server_pid, browser_pid, sweeper_pid], 10.0)
        assert survivors == [], f"left running: {survivors}"
    finally:
        if parent.poll() is None:
            parent.kill()


@needs_proc
def test_a_clean_exit_tells_the_sweeper_to_leave_everything_alone(tmp_path):
    import subprocess

    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", f"--user-data-dir={tmp_path}/p"],
        start_new_session=True,
    )
    try:
        sweeper = procs.Sweeper()
        sweeper.watch_dir(tmp_path)
        sweeper.watch_group(victim.pid)
        proc = sweeper._proc
        sweeper.close()
        assert proc.returncode == 0
        time.sleep(0.3)
        assert running(victim.pid)
    finally:
        victim.kill()
        victim.wait()


@needs_proc
def test_the_sweeper_matches_only_flags_that_point_into_a_watched_dir(tmp_path):
    """Not by name and not by prefix of a sibling: ``<dir>-other`` is someone else's."""
    import subprocess

    watched = tmp_path / "run"
    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
    flag = f"--user-data-dir={watched}/profile"
    inside = subprocess.Popen([*sleep, flag], start_new_session=True)
    sibling = subprocess.Popen([*sleep, f"--user-data-dir={watched}-other"], start_new_session=True)
    positional = subprocess.Popen([*sleep, str(watched / "profile")], start_new_session=True)
    try:
        sweeper = procs.Sweeper()
        sweeper.watch_dir(watched)
        proc = sweeper._proc
        proc.stdin.close()  # what a dead parent looks like from the sweeper's side
        proc.wait(timeout=10)
        deadline = time.monotonic() + 5
        while running(inside.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not running(inside.pid)
        assert running(sibling.pid) and running(positional.pid)
    finally:
        for p in (inside, sibling, positional):
            p.kill()
            p.wait()
