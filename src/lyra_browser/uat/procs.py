"""Child processes that are stopped as a group.

A harness brain is three processes deep — the harness, the MCP server it
spawns, the browser the server launches — and the batch runner adds a fourth
level above them. Signalling only the direct child leaves the rest running if it
ignores the signal or is killed: the server and its browser would be orphaned
with a profile locked. Each child is started in a session of its own, so its
process group is exactly its descendants, and the group is what gets
interrupted, then killed.

POSIX only (``start_new_session``, ``killpg``). A process killed outright (SIGKILL) cannot
stop anything itself: the sweeper at the end of this module does it for it.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
from typing import Any

POLL_S = 0.2
# After the direct child is gone, how long its descendants get to leave on their own
# before they are terminated, and then killed.
LINGER_S = 3.0
KILL_AFTER_S = 3.0


async def spawn(argv: list[str], **kwargs: Any) -> asyncio.subprocess.Process:
    """Start ``argv`` as the leader of a new session (so its pgid is its pid).

    The group is handed to the sweeper until ``stop_group`` / ``reap_group`` has seen it
    gone, so it is stopped even when this process is killed outright."""
    proc = await asyncio.create_subprocess_exec(*argv, start_new_session=True, **kwargs)
    sweeper.watch_group(proc.pid)
    return proc


_HAVE_PROC = os.path.isdir("/proc/self")


def group_alive(pgid: int) -> bool:
    """Whether any process of the group still runs.

    ``killpg(pgid, 0)`` answers on every POSIX system. Where ``/proc`` exists (Linux) a
    group holding only zombies — orphans nobody reaped — is told apart and counts as gone;
    without it (macOS) a zombie keeps the group "alive" until init reaps it, which is
    quick, and every wait here is bounded either way."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if not _HAVE_PROC:
        return True
    return any(_running(pid) for pid in _members(pgid))


def _members(pgid: int) -> list[int]:
    found: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return [pgid]  # no /proc: killpg(…, 0) already said something is there
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8") as handle:
                stat = handle.read()
        except OSError:
            continue
        # pid (comm) state ppid pgrp ...; comm may contain spaces and parentheses.
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) > 2 and int(fields[2]) == pgid:
            found.append(int(name))
    return found


def _running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            stat = handle.read()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :].split()[0] != "Z"


def _signal_group(pgid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


async def _wait_gone(pgid: int, seconds: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while group_alive(pgid):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(POLL_S)
    return True


async def stop_group(proc: asyncio.subprocess.Process, *, grace_s: float) -> None:
    """Interrupt the whole group, give it ``grace_s`` to wind down, then kill what is left.

    Interrupting first lets a harness end its turn and close its MCP server, which closes
    the browser properly; the kill is for one that is stuck. Reaps the direct child.
    """
    pgid = proc.pid
    if _signal_group(pgid, signal.SIGINT):
        # The leader's own exit is what the caller waits on; the group is what must go.
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_s)
        except TimeoutError:
            pass
        if not await _wait_gone(pgid, max(0.0, LINGER_S)):
            _signal_group(pgid, signal.SIGKILL)
    if proc.returncode is None:
        await proc.wait()
    if await _wait_gone(pgid, KILL_AFTER_S):
        sweeper.unwatch_group(pgid)


async def reap_group(pgid: int) -> None:
    """After a child exited on its own: whatever it left behind goes too.

    A harness that finished but did not close its server would otherwise leave a browser
    open on the run's profile.
    """
    if not await _wait_gone(pgid, LINGER_S):
        _signal_group(pgid, signal.SIGTERM)
        if not await _wait_gone(pgid, KILL_AFTER_S):
            _signal_group(pgid, signal.SIGKILL)
            if not await _wait_gone(pgid, KILL_AFTER_S):
                return  # still there: the sweeper keeps it
    sweeper.unwatch_group(pgid)


# --- the sweeper: what is left when this process is killed outright ---------------------
#
# Every group above is stopped by *this* process. Killed with SIGKILL it can stop nothing,
# and a child started in a session of its own (the point of ``spawn``) survives it, as does
# a browser a driver launched in a group of its own. The sweeper is a small process of its
# own session that holds the read end of a pipe from this one. It learns the groups and
# browser profile directories to clean up as they come and go; when the pipe closes without
# a goodbye — this process died, however — it terminates, then kills, every group it still
# knows and every process whose ``--flag=<path>`` argument points into a known directory
# (Chrome's ``--user-data-dir``, its crash handler's ``--database``). It never matches by
# program name: the operator's own Chrome is never a candidate.

_SWEEPER_SRC = r"""
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
groups, dirs = set(), set()
for line in sys.stdin:
    verb, _, arg = line.rstrip("\n").partition(" ")
    if verb == "group":
        groups.add(int(arg))
    elif verb == "ungroup":
        groups.discard(int(arg))
    elif verb == "dir":
        dirs.add(arg.rstrip("/"))
    elif verb == "undir":
        dirs.discard(arg.rstrip("/"))
    elif verb == "bye":
        sys.exit(0)


def inside(path):
    return any(path == d or path.startswith(d + "/") for d in dirs)


def browsers():
    if not dirs or not os.path.isdir("/proc"):
        return []
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue
        for arg in argv:
            flag, eq, value = arg.partition(b"=")
            if eq and flag.startswith(b"--") and inside(os.fsdecode(value)):
                found.append(int(name))
                break
    return found


for sig, pause in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 0.5), (signal.SIGKILL, 0.0)):
    for pgid in groups:
        try:
            os.killpg(pgid, sig)
        except OSError:
            pass
    for pid in browsers():
        try:
            os.kill(pid, sig)
        except OSError:
            pass
    time.sleep(pause)
"""


class Sweeper:
    """The parent side of the sweeper. Started on first use; every call is best effort
    (a sweeper that cannot be started or reached must not fail the run it protects)."""

    def __init__(self) -> None:
        self._proc: Any = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def _send(self, line: str) -> None:
        import subprocess
        import sys

        if self._proc is None:
            try:
                self._proc = subprocess.Popen(
                    [sys.executable, "-I", "-c", _SWEEPER_SRC],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                return
        try:
            self._proc.stdin.write(f"{line}\n".encode())
            self._proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def watch_group(self, pgid: int) -> None:
        self._send(f"group {pgid}")

    def unwatch_group(self, pgid: int) -> None:
        if self._proc is not None:
            self._send(f"ungroup {pgid}")

    def watch_dir(self, path: os.PathLike[str] | str) -> None:
        self._send(f"dir {os.path.abspath(path)}")

    def unwatch_dir(self, path: os.PathLike[str] | str) -> None:
        if self._proc is not None:
            self._send(f"undir {os.path.abspath(path)}")

    def close(self) -> None:
        """A clean exit: the sweeper leaves without touching anything."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.write(b"bye\n")
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - exiting anyway
            pass


sweeper = Sweeper()
atexit.register(sweeper.close)
