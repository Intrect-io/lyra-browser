#!/usr/bin/env python3
"""Two servers on one data dir: exactly one gets the saved profile, neither crashes.

Spawns the real Hermes wrapper (``~/.hermes/scripts/lyra_browser_mcp.sh``) as stdio
MCP servers, the way Hermes does, against a throwaway data dir and a throwaway
Xvfb display, and a loopback site that sets and echoes a cookie. Not part of the
unit suite; it needs Chrome, Xvfb and the Hermes venv (``LYRA_BROWSER_VENV``).

    .venv/bin/python scripts/verify_profile_lock_e2e.py [--wrapper PATH] [--display :97]

Scenarios (each asserts what the *page* saw, not only the envelope):

- seed:         one server logs in (cookie) and exits cleanly;
- simultaneous: two servers spawned and opened at the same moment, three times —
                both ``open_browser`` succeed, exactly one reports
                ``profile: shared`` and sees the cookie, the other ``instance``
                and does not;
- sequential:   a second server spawned while the first holds a live Chrome;
- stale:        the holder is SIGKILLed (Chrome tree included); the next server
                gets the shared profile again, no manual cleanup;
- idle:         a server that never opens a browser does not keep the others out.

It starts Xvfb through the wrapper and kills it again, unless the display was
already running.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402
from verify_browser_e2e import expect  # noqa: E402


class Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        cookie = self.headers.get("Cookie", "")
        self.send_response(200)
        if self.path.startswith("/login"):
            self.send_header("Set-Cookie", "who=me; Max-Age=86400; Path=/")
        body = f"<title>t</title><p>COOKIE[{cookie}]</p>".encode()
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


class Server:
    """One wrapper process, driven over stdio."""

    def __init__(self, name: str, env: dict[str, str], wrapper: str, logdir: Path) -> None:
        transport = StdioTransport(
            "bash", [wrapper], env=env, log_file=logdir / f"{name}.stderr", keep_alive=False
        )
        self.name = name
        self.client = Client(transport, timeout=180)

    async def __aenter__(self) -> Server:
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.__aexit__(*exc)

    async def call(self, tool: str, **args: object) -> dict:
        result = await self.client.call_tool(tool, args, raise_on_error=False)
        if result.is_error:
            return {"status": "error", "error": str(result.content)[:300]}
        return dict(result.data)

    async def visit(self, base: str, path: str) -> tuple[dict, str]:
        opened = await self.call("open_browser")
        if opened.get("status") != "ok":
            return opened, ""
        await self.call("navigate", url=base + path, reason="verify")
        page = await self.call("read_page")
        return opened, str(page.get("text", ""))


def display_alive(display: str) -> bool:
    env = {**os.environ, "DISPLAY": display}
    return subprocess.run(["xdpyinfo"], env=env, capture_output=True).returncode == 0


def kill_display(display: str) -> None:
    subprocess.run(["pkill", "-f", f"Xvfb {display} "], check=False)
    subprocess.run(["pkill", "-f", f"_ {display} "], check=False)  # the wrapper's log sink
    Path(f"/tmp/.X11-unix/X{display.lstrip(':')}").unlink(missing_ok=True)


def kill_profile_users(data_dir: Path) -> None:
    """SIGKILL every server spawned for ``data_dir`` and every Chrome on a profile under it."""
    subprocess.run(["pkill", "-9", "-f", f"user-data-dir={data_dir}"], check=False)
    wanted = f"LYRA_BROWSER_DATA_DIR={data_dir}".encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if wanted in (entry / "environ").read_bytes().split(b"\0"):
                os.kill(int(entry.name), 9)
        except OSError:
            continue


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--wrapper", default=str(Path("~/.hermes/scripts/lyra_browser_mcp.sh").expanduser())
    )
    parser.add_argument("--display", default=":97")
    parser.add_argument("--venv", default=str(Path("/path/to/venv").expanduser()))
    args = parser.parse_args()

    site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_address[1]}"

    root = Path(tempfile.mkdtemp(prefix="lyra-profile-lock-"))
    data = root / "data"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "LYRA_BROWSER_DISPLAY": args.display,
        "LYRA_BROWSER_DATA_DIR": str(data),
        "LYRA_BROWSER_VENV": args.venv,
    }
    started_display = not display_alive(args.display)
    servers = 0

    def make(name: str) -> Server:
        nonlocal servers
        servers += 1
        return Server(name, env, args.wrapper, root)

    try:
        # --- seed: log in, exit cleanly so Chrome flushes the cookie to disk.
        async with make("seed") as seed:
            opened, text = await seed.visit(base, "/login")
            expect(
                opened["status"] == "ok" and opened["profile"] == "shared",
                "seed: shared profile",
                opened,
            )
            opened, text = await seed.visit(base, "/show")
            expect("who=me" in text, "seed: logged in", text)

        # --- simultaneous: three rounds, nothing orders the two servers.
        for round_no in range(1, 4):
            async with make(f"A{round_no}") as a, make(f"B{round_no}") as b:
                (oa, ta), (ob, tb) = await asyncio.gather(
                    a.visit(base, "/show"), b.visit(base, "/show")
                )
            expect(
                oa["status"] == "ok" and ob["status"] == "ok",
                f"simultaneous #{round_no}: both open",
                (oa, ob),
            )
            modes = sorted([oa["profile"], ob["profile"]])
            expect(
                modes == ["instance", "shared"],
                f"simultaneous #{round_no}: one shared, one instance",
                modes,
            )
            shared_text, instance_text = (ta, tb) if oa["profile"] == "shared" else (tb, ta)
            expect(
                "who=me" in shared_text,
                f"simultaneous #{round_no}: shared keeps the login",
                shared_text,
            )
            expect(
                "who=me" not in instance_text,
                f"simultaneous #{round_no}: instance has none",
                instance_text,
            )
            instance_open = oa if oa["profile"] == "instance" else ob
            expect(
                "no saved logins" in instance_open.get("note", ""),
                "the instance is told why",
                instance_open,
            )

        # --- sequential: the second spawns while the first holds a live Chrome.
        async with make("S1") as first:
            o1, t1 = await first.visit(base, "/show")
            async with make("S2") as second:
                o2, t2 = await second.visit(base, "/show")
            expect(
                o1["profile"] == "shared" and "who=me" in t1,
                "sequential: first holds shared + login",
                (o1, t1),
            )
            expect(
                o2["status"] == "ok" and o2["profile"] == "instance",
                "sequential: second is an instance",
                o2,
            )
            expect("who=me" not in t2, "sequential: second sees no login", t2)

        # --- stale: SIGKILL the holder and its whole Chrome tree.
        holder = make("K1")
        await holder.__aenter__()
        ok, _ = await holder.visit(base, "/show")
        expect(ok["profile"] == "shared", "stale: holder has the shared profile", ok)
        kill_profile_users(data)
        await asyncio.sleep(1)
        async with make("K2") as after:
            o, t = await after.visit(base, "/show")
        expect(
            o["status"] == "ok" and o["profile"] == "shared",
            "stale: next server gets shared again",
            o,
        )
        try:
            await holder.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001 — its process is gone; that is the premise
            pass

        # --- idle: a spawned-but-never-opened server must not hold the profile.
        async with make("I1") as idle:
            await idle.client.list_tools()
            async with make("W1") as worker:
                o, t = await worker.visit(base, "/show")
        expect(
            o["profile"] == "shared" and "who=me" in t,
            "idle: worker still gets shared + login",
            (o, t),
        )

        leftovers = (
            sorted(p.name for p in (root / "data-instances").glob("*"))
            if (root / "data-instances").exists()
            else []
        )
        print(f"INFO  instance dirs left after the run: {leftovers}")
        print(f"PASS  all scenarios ({servers} servers spawned)")
        return 0
    except AssertionError as exc:
        print(f"FAIL  {exc}")
        return 1
    finally:
        site.shutdown()
        kill_profile_users(root)
        if started_display:
            kill_display(args.display)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
