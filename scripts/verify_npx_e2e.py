"""Run lyra the way an npx user does, and measure what that costs. Linux only.

    .venv/bin/python scripts/verify_npx_e2e.py       # pack npm/, build the wheel, test both
    .venv/bin/python scripts/verify_npx_e2e.py --package @intrect/lyra-browser@0.1.2

The second form runs the published package against PyPI instead (after publishing).

Packs ``npm/`` into a tarball and builds the wheel into a scratch directory, then
starts the server with ``npx -y --package=<tarball> lyra-browser`` and
``LYRA_BROWSER_PY_SPEC=<wheel>`` (the package is not on PyPI yet), first **cold**
(empty npm and uv caches, so every wheel is downloaded), then **warm** in four
sessions. Each session speaks raw MCP over stdio.

* stdout carries JSON-RPC lines and nothing else (an ``npm notice`` or a uv
  progress line leaking there would break the client's parser);
* ``initialize`` answers, and ``tools/list`` is the in-process server's list;
* the environment uvx built holds only versions ``constraints.txt`` pins;
* ``open_browser`` launches headless Chrome and takes the profile lock;
* each way a client can stop a server (close its stdin; SIGTERM to npx; SIGTERM to
  the shim; SIGKILL to the shim and then close stdin) leaves no server, no Chrome and
  no held profile lock. A leftover lock sends the next session to a profile with no
  logins, and npm runs a bin through ``sh -c``, which dies on SIGTERM without
  passing it on;
* ``install-browser``, the documented way to get a bundled browser, resolves (dry run);
* with ``--claude``: a Claude Code session registers the server through ``--mcp-config``
  and calls ``open_browser`` (needs the ``claude`` CLI, signed in; one short model turn).

The times are reported, not judged: Claude Code gives an MCP server 30 s to connect
(``MCP_TIMEOUT``). Exit status is the verdict on the rest. Needs node, npm, uv and
Chrome.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
NPM = REPO / "npm"
SHIPPED = ["LICENSE", "README.md", "bin/lyra-browser.js", "constraints.txt", "package.json"]
PROTOCOL = "2025-06-18"
# A nested Claude Code refuses to start while these say it is already inside one.
SCRUB = ("CLAUDECODE", "CLAUDE_CODE_", "CODEX_")


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if not ok and detail else "")
        )
        if not ok:
            self.failed += 1
        return ok

    @staticmethod
    def info(text: str) -> None:
        print(f"INFO  {text}")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=True, **kw)


def pack(dest: Path) -> tuple[Path, list[str]]:
    dest.mkdir(parents=True)
    stdout = run(["npm", "pack", "--json", "--pack-destination", str(dest)], cwd=NPM).stdout
    # A lifecycle banner ("> pkg prepack ...") may precede the JSON.
    entry = json.loads(stdout[stdout.index("[") :])[0]
    return dest / entry["filename"], sorted(f["path"] for f in entry["files"])


def build_wheel(dest: Path) -> Path:
    run(["uv", "build", "--wheel", "--out-dir", str(dest)], cwd=REPO)
    (wheel,) = dest.glob("lyra_browser-*.whl")
    return wheel


async def in_process_tools(data_dir: Path) -> list[str]:
    from lyra_browser.config import Config
    from lyra_browser.server import build_server

    cfg = Config()
    object.__setattr__(cfg, "data_dir", data_dir)
    cfg.__post_init__()
    return sorted(tool.name for tool in await build_server(cfg).list_tools())


# ---- processes ------------------------------------------------------------


def holders(marker: bytes) -> list[tuple[int, int, str]]:
    """(pid, ppid, command) of every process whose environment carries ``marker``."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if marker not in (entry / "environ").read_bytes():
                continue
            stat = (entry / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        found.append((int(entry.name), ppid, cmd.strip()[:400]))
    return sorted(found)


def lock_free(data_dir: Path) -> bool:
    path = data_dir / "profile.lock"
    if not path.exists():
        return True
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def wait_until(predicate, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


class Server:
    """One ``npx -y <package>`` session, spoken to over raw newline-delimited JSON-RPC."""

    def __init__(self, label: str, root: Path, argv: list[str], env: dict[str, str]) -> None:
        self.label = label
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True)
        self.marker = f"LYRA_BROWSER_DATA_DIR={self.data_dir}".encode()
        env = {**env, "LYRA_BROWSER_DATA_DIR": str(self.data_dir), "LYRA_BROWSER_HEADLESS": "true"}
        self.stderr_path = root / "stderr.log"
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_path.open("wb"),
            env=env,
            start_new_session=True,
        )
        self.lines: queue.Queue[bytes] = queue.Queue()
        self.seen: list[bytes] = []
        threading.Thread(target=self._pump, daemon=True).start()
        self.started = time.monotonic()

    def _pump(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            self.lines.put(line)

    def call(self, ident: int, method: str, params: dict, timeout: float) -> dict | None:
        assert self.proc.stdin
        msg = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None:
                    return None
                continue
            self.seen.append(line)
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == ident:
                return reply
        return None

    def notify(self, method: str) -> None:
        assert self.proc.stdin
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method}) + "\n").encode())
        self.proc.stdin.flush()

    def stdout_is_only_jsonrpc(self) -> tuple[bool, list[bytes]]:
        stray = []
        for line in self.seen:
            try:
                ok = json.loads(line).get("jsonrpc") == "2.0"
            except (ValueError, AttributeError):
                ok = False
            if not ok:
                stray.append(line[:120])
        return not stray, stray

    def handshake(self, timeout: float) -> float | None:
        reply = self.call(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "verify-npx", "version": "0"},
            },
            timeout,
        )
        if reply is None or "result" not in reply:
            return None
        elapsed = time.monotonic() - self.started
        self.notify("notifications/initialized")
        return elapsed

    def alive(self) -> list[tuple[int, int, str]]:
        return holders(self.marker)

    def none_alive(self) -> bool:
        return not self.alive()

    def cleanup(self) -> None:
        # A scenario usually ends with everything already gone; that is the passing case,
        # so only "no such process" is ignored and a permission error still surfaces.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # the group already exited
        for pid, _, _ in self.alive():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # exited between the scan and the kill


def server_python(chain: list[tuple[int, int, str]]) -> str | None:
    """The interpreter uvx built, by where it lives. /proc/<pid>/exe would name the real
    binary behind the environment's symlink, so read argv[0] instead."""
    for pid, _, _ in chain:
        try:
            argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode()
        except OSError:
            continue
        if "/archive-v0/" in argv0 and Path(argv0).name.startswith("python"):
            return argv0
    return None


def shim_of(chain: list[tuple[int, int, str]]) -> tuple[int, int, str] | None:
    return next(
        (
            p
            for p in chain
            if p[2].split()[0].endswith("node") and "lyra-browser" in p[2] and ".bin/" in p[2]
        ),
        None,
    )


def installed_in(python: str) -> dict[str, str]:
    code = (
        "import importlib.metadata as m, json; "
        "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions()}))"
    )
    return {norm(k): v for k, v in json.loads(run([python, "-I", "-c", code]).stdout).items()}


def pins() -> dict[str, str]:
    out = {}
    for line in (NPM / "constraints.txt").read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)", line)
        if match:
            out[norm(match[1])] = match[2]
    return out


def fresh_env(caches: Path) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")
    }
    env["npm_config_cache"] = str(caches / "npm")
    env["UV_CACHE_DIR"] = str(caches / "uv")
    return env


def claude_session(argv: list[str], server_env: dict[str, str], scratch: Path) -> tuple[bool, str]:
    """Register the server with a real Claude Code and have it call ``open_browser``."""
    binary = os.environ.get("LYRA_UAT_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        return False, "the claude CLI is not on PATH (or LYRA_UAT_CLAUDE_BIN)"
    config = scratch / "claude-mcp.json"
    config.write_text(
        json.dumps(
            {"mcpServers": {"lyra": {"command": argv[0], "args": argv[1:], "env": server_env}}}
        )
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUB)}
    # A background-connecting server's tools sit behind tool search; a one-prompt session
    # would answer first (see verify_claude_plugin_e2e.py).
    env["MCP_CONNECTION_NONBLOCKING"] = "0"
    env["ENABLE_TOOL_SEARCH"] = "false"
    done = subprocess.run(
        [
            binary, "-p", "Call the open_browser tool once, then say DONE.",
            "--mcp-config", str(config), "--strict-mcp-config",
            "--setting-sources", "", "--tools", "", "--permission-mode", "dontAsk",
            "--allowedTools", "mcp__lyra__open_browser", "--no-session-persistence",
            "--output-format", "stream-json", "--verbose", "--max-turns", "4",
        ],
        env=env, cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    called, failed = None, None
    for line in done.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        content = (event.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use" and block.get("name") == "mcp__lyra__open_browser":
                called = block["id"]
            if block.get("type") == "tool_result" and called and block.get("tool_use_id") == called:
                failed = bool(block.get("is_error"))
    return called is not None and failed is False, (done.stdout + done.stderr)[-500:]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--package", help="run this published npm package instead of the packed tarball"
    )
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    parser.add_argument(
        "--claude", action="store_true", help="also drive it from a Claude Code session"
    )
    args = parser.parse_args()

    checks = Checks()
    scratch = Path(tempfile.mkdtemp(prefix="lyra-npx-e2e-"))
    servers: list[Server] = []
    try:
        extra: dict[str, str] = {}
        wheel: Path | None = None
        if args.package:
            package = args.package
        else:
            tarball, files = pack(scratch / "pack")
            checks.check("npm pack ships exactly the shim's files", files == SHIPPED, files)
            wheel = build_wheel(scratch / "dist")
            package = str(tarball)
            extra["LYRA_BROWSER_PY_SPEC"] = str(wheel)
            Checks.info(f"tarball {tarball.name}, wheel {wheel.name}")
        # npx reads a bare path as a command to run, so a tarball goes through --package.
        argv = (
            ["npx", "-y", package]
            if args.package
            else ["npx", "-y", f"--package={package}", "lyra-browser"]
        )
        expected = asyncio.run(in_process_tools(scratch / "inproc"))
        caches = scratch / "caches"

        # ---- cold: empty npm and uv caches ------------------------------------
        cold = Server("cold", scratch / "cold", argv, {**fresh_env(caches), **extra})
        servers.append(cold)
        took = cold.handshake(300)
        if not checks.check(
            "cold: initialize answers", took is not None, cold.stderr_path.read_text()[-600:]
        ):
            return 1
        Checks.info(f"cold start to initialize: {took:.1f} s (Claude Code MCP_TIMEOUT: 30 s)")
        listed = cold.call(2, "tools/list", {}, 60)
        names = sorted(t["name"] for t in (listed or {}).get("result", {}).get("tools", []))
        checks.check(
            "cold: tools/list equals the in-process server's",
            names == expected and bool(names),
            (len(names), len(expected)),
        )

        opened = cold.call(3, "tools/call", {"name": "open_browser", "arguments": {}}, 90)
        launched = bool(opened) and "result" in opened and not opened["result"].get("isError")
        checks.check("cold: open_browser launches headless Chrome", launched, opened)
        checks.check(
            "cold: the running server holds the profile lock", not lock_free(cold.data_dir)
        )

        chain = cold.alive()
        Checks.info(
            "process chain: "
            + " | ".join(f"{pid}<-{ppid} {cmd[:48]}" for pid, ppid, cmd in chain[:6])
        )
        python = server_python(chain)
        if checks.check("cold: a Python server process is running", python is not None, chain):
            have = installed_in(python)
            have.pop("lyra-browser", None)
            want = pins()
            wrong = {k: (v, want.get(k)) for k, v in have.items() if want.get(k) != v}
            checks.check(
                "cold: uvx's environment holds only what constraints.txt pins", not wrong, wrong
            )
            Checks.info(
                f"{Path(python).resolve().name} interpreter; {len(have)} packages installed, "
                f"{len(want)} pinned (markers drop the rest)"
            )

        ok, stray = cold.stdout_is_only_jsonrpc()
        checks.check("cold: stdout held nothing but JSON-RPC", ok, stray)
        cold.cleanup()

        # ---- warm, and every way a client can stop a server -------------------
        # The client keeps its end of stdin open in all but the first: a client that
        # only signals the process must not leave a server and Chrome behind.
        def sigterm_npx(server: Server) -> None:
            os.kill(server.proc.pid, signal.SIGTERM)

        def sigterm_shim(server: Server) -> None:
            os.kill(shim_of(server.alive())[0], signal.SIGTERM)

        def close_stdin(server: Server) -> None:
            assert server.proc.stdin
            server.proc.stdin.close()

        def sigkill_shim_then_close_stdin(server: Server) -> None:
            os.kill(shim_of(server.alive())[0], signal.SIGKILL)
            close_stdin(server)

        scenarios = [
            ("the client closes stdin", close_stdin),
            ("SIGTERM to npx (stdin left open)", sigterm_npx),
            ("SIGTERM to the shim (stdin left open)", sigterm_shim),
            ("SIGKILL to the shim, then stdin closed", sigkill_shim_then_close_stdin),
        ]
        times = []
        for label, stop in scenarios:
            server = Server(
                label, scratch / f"warm-{len(servers)}", argv, {**fresh_env(caches), **extra}
            )
            servers.append(server)
            took = server.handshake(120)
            if not checks.check(
                f"{label}: initialize answers",
                took is not None,
                server.stderr_path.read_text()[-600:],
            ):
                continue
            times.append(took)
            opened = server.call(3, "tools/call", {"name": "open_browser", "arguments": {}}, 90)
            ok = bool(opened) and "result" in opened and not opened["result"].get("isError")
            checks.check(
                f"{label}: open_browser launched Chrome and took the profile lock",
                ok and not lock_free(server.data_dir),
                opened,
            )
            clean, stray = server.stdout_is_only_jsonrpc()
            checks.check(f"{label}: stdout held nothing but JSON-RPC", clean, stray)
            if shim_of(server.alive()) is None and "shim" in label:
                checks.check(f"{label}: the shim is in the chain", False, server.alive())
                continue
            # How long the profile stays locked decides whether a client that starts a new
            # server straight away still gets the logged-in profile.
            began = time.monotonic()
            stop(server)
            unlocked = gone_at = None
            while time.monotonic() - began < 25 and (unlocked is None or gone_at is None):
                now = time.monotonic() - began
                if unlocked is None and lock_free(server.data_dir):
                    unlocked = now
                if gone_at is None and not server.alive():
                    gone_at = now
                time.sleep(0.05)
            checks.check(f"{label}: no server, no Chrome left", gone_at is not None, server.alive())
            checks.check(f"{label}: profile lock released", unlocked is not None)
            if unlocked is not None and gone_at is not None:
                Checks.info(
                    f"{label}: profile lock free after {unlocked:.2f} s, "
                    f"nothing running after {gone_at:.2f} s"
                )
        if times:
            Checks.info("warm start to initialize: " + ", ".join(f"{t:.1f} s" for t in times))

        # ---- two sessions starting at once on empty caches ---------------------
        # Two clients opening on the same machine for the first time share npm's _npx
        # directory and uv's cache; neither may break the other.
        shared = scratch / "caches-concurrent"
        pair = [
            Server(
                f"concurrent {n}", scratch / f"concurrent-{n}", argv, {**fresh_env(shared), **extra}
            )
            for n in "AB"
        ]
        servers.extend(pair)
        started = time.monotonic()
        answered = [server.handshake(300) for server in pair]
        checks.check(
            "two cold starts at once: both answer initialize",
            all(t is not None for t in answered),
            [server.stderr_path.read_text()[-400:] for server in pair],
        )
        Checks.info(
            f"two cold starts at once: {time.monotonic() - started:.1f} s until both had answered"
        )
        for server in pair:
            server.cleanup()

        # ---- the documented browser install resolves --------------------------
        done = subprocess.run(
            [*argv, "install-browser", "--dry-run"],
            capture_output=True,
            text=True,
            env={**fresh_env(caches), **extra},
            timeout=300,
        )
        checks.check(
            "`install-browser --dry-run` resolves Playwright's pinned Chromium",
            done.returncode == 0 and "chromium" in done.stdout.lower(),
            (done.stderr or done.stdout)[-400:],
        )
        if args.claude:
            server_env = {
                **{
                    k: v
                    for k, v in fresh_env(caches).items()
                    if k.startswith(("npm_config_", "UV_"))
                },
                **extra,
                "LYRA_BROWSER_DATA_DIR": str(scratch / "claude-data"),
                "LYRA_BROWSER_HEADLESS": "true",
            }
            ok, tail = claude_session(argv, server_env, scratch)
            checks.check("Claude Code registers the server and open_browser answers", ok, tail)
    finally:
        for server in servers:
            server.cleanup()
        if args.keep:
            print(f"scratch kept: {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    print(f"\n{'ALL PASS' if not checks.failed else f'{checks.failed} FAILED'}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
