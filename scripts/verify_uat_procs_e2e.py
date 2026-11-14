#!/usr/bin/env python3
"""Real-browser gate: a UAT process killed with SIGKILL leaves no Chrome behind.

A child process opens a real browser the way a run does (``build_tools`` +
``open_browser`` on a throwaway data dir), hands the data dir to the sweeper
(``lyra_browser.uat.procs.sweeper``), says it is ready, and waits. The gate kills it
with SIGKILL, which it cannot handle, and then counts the processes whose command line
points into that data dir. With the sweeper the count must reach 0; the control run
(``--control``: the same child, no sweeper) measures what is left without it.

Processes are found by the throwaway data dir only, never by program name: the
operator's own Chrome is never counted or touched. Loopback only, nothing is visited.

    .venv/bin/python scripts/verify_uat_procs_e2e.py
    LYRA_BROWSER_DRIVER=playwright .venv/bin/python scripts/verify_uat_procs_e2e.py --control
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent

CHILD = r"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from verify_browser_e2e import build_tools
from lyra_browser.uat import procs

data_dir, sweep = Path(sys.argv[1]), sys.argv[2] == "sweep"

async def main():
    if sweep:
        procs.sweeper.watch_dir(data_dir)
    ctx, tools = await build_tools(data_dir, headless=True)
    opened = await tools["open_browser"]()
    print("ready" if opened.get("status") == "ok" else f"failed {opened}", flush=True)
    await asyncio.sleep(600)

asyncio.run(main())
"""


def pids_under(directory: Path) -> list[int]:
    """Processes with ``directory`` anywhere in their command line (this script excluded)."""
    needle = os.fsencode(str(directory))
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as handle:
                cmdline = handle.read()
        except OSError:
            continue
        if needle in cmdline:
            found.append(int(name))
    return found


def run_once(sweep: bool, settle_s: float) -> tuple[int, int]:
    """(processes on the data dir before the kill, processes left ``settle_s`` after it)."""
    with tempfile.TemporaryDirectory(prefix="lyra-uat-procs-", ignore_cleanup_errors=True) as tmp:
        data_dir = Path(tmp)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "src"), *sys.path])}
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                CHILD,
                str(data_dir),
                "sweep" if sweep else "none",
                str(SCRIPTS),
            ],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            line = child.stdout.readline().strip()
            if line != "ready":
                raise AssertionError(f"the child did not open a browser: {line!r}")
            before = len(pids_under(data_dir))
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=10)
            deadline = time.monotonic() + settle_s
            left = pids_under(data_dir)
            while left and time.monotonic() < deadline:
                time.sleep(0.25)
                left = pids_under(data_dir)
            return before, len(left)
        finally:
            if child.poll() is None:
                child.kill()
            for pid in pids_under(data_dir):  # never leave a browser behind
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--settle", type=float, default=10.0, help="seconds to wait after the kill")
    parser.add_argument("--control", action="store_true", help="also measure without the sweeper")
    args = parser.parse_args()
    if not os.path.isdir("/proc"):
        print("this gate reads /proc (Linux)", file=sys.stderr)
        return 2
    driver = os.environ.get("LYRA_BROWSER_DRIVER", "auto")
    failed = False
    for i in range(args.runs):
        before, left = run_once(True, args.settle)
        ok = before > 0 and left == 0
        failed |= not ok
        print(
            f"{'PASS' if ok else 'FAIL'}  run {i + 1} (driver={driver}): {before} processes on the "
            f"data dir before SIGKILL, {left} left {args.settle:g}s after it, with the sweeper",
            flush=True,
        )
    if args.control:
        before, left = run_once(False, args.settle)
        print(
            f"NOTE  control (driver={driver}): {before} processes before SIGKILL, {left} left "
            f"{args.settle:g}s after it, without the sweeper",
            flush=True,
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
