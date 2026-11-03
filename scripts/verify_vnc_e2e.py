#!/usr/bin/env python3
"""Gate: lyra on a host with no display opens its window on the persistent VNC display.

    .venv/bin/python scripts/verify_vnc_e2e.py [--driver native|playwright|patchright]

What ``lyra-browser vnc install`` sets up, without systemd and without touching the
host's own install: a private ``Xvfb`` on a free display number and an ``x11vnc`` on
a free loopback port, started with the exact command lines the units carry
(``vnc_cli.xvfb_command`` / ``x11vnc_command``), a throwaway sidecar and password
(``LYRA_BROWSER_VNC_DIR``), and a throwaway data dir. Then a real ``lyra-browser
--client claude-code`` over stdio, with ``DISPLAY`` and ``WAYLAND_DISPLAY`` removed
from its environment, must:

* report ``attended=true`` and ``watch.vnc`` with that address, and no password;
* render a page that a real RFB client, authenticated with the stored password,
  sees in the framebuffer (a wrong password must be refused);
* after the X server is gone, open headless with ``display_lost`` instead of failing —
  both when its socket is gone and when a killed server left the socket behind.

Loopback only. Skips (exit 0, "SKIP") without Xvfb, x11vnc or Linux. Exit status is
the verdict otherwise: 0 only if every check passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES  # noqa: E402
from cryptography.hazmat.primitives.ciphers import Cipher, modes  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

from lyra_browser import vnc_cli  # noqa: E402

# The page fills the window with one colour nothing else on screen has.
MARK = (255, 0, 170)
PAGE = b"""<!doctype html><title>vnc gate</title>
<style>html,body{margin:0;height:100%;background:rgb(255,0,170)}</style><body></body>"""


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if not ok else ""))
        if not ok:
            self.failed += 1
        return ok


# --------------------------------------------------------------------------
# A minimal RFB 3.8 client: VNC authentication, one raw framebuffer update
# --------------------------------------------------------------------------


def _recv(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"closed after {len(buf)} of {n} bytes")
        buf += chunk
    return buf


def _vnc_response(password: str, challenge: bytes) -> bytes:
    """DES of the challenge, keyed by the password with each byte's bits reversed."""
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in password.encode()[:8].ljust(8, b"\0"))
    enc = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return enc.update(challenge) + enc.finalize()


def rfb_session(port: int, password: str) -> tuple[bool, dict]:
    """Authenticate; on success grab the whole framebuffer. ``(authenticated, info)``."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        version = _recv(sock, 12)
        sock.sendall(b"RFB 003.008\n")
        count = _recv(sock, 1)[0]
        if count == 0:
            return False, {"error": "server refused", "version": version}
        types = _recv(sock, count)
        if 2 not in types:
            return False, {"error": f"no VNC auth offered: {list(types)}"}
        sock.sendall(b"\x02")
        challenge = _recv(sock, 16)
        sock.sendall(_vnc_response(password, challenge))
        if struct.unpack(">I", _recv(sock, 4))[0] != 0:
            return False, {"error": "authentication failed"}

        sock.sendall(b"\x01")  # ClientInit, shared
        width, height = struct.unpack(">HH", _recv(sock, 4))
        fmt = _recv(sock, 16)
        name_len = struct.unpack(">I", _recv(sock, 4))[0]
        name = _recv(sock, name_len).decode(errors="replace")
        bpp, _depth, big_endian, true_colour = fmt[0], fmt[1], fmt[2], fmt[3]
        rmax, gmax, bmax = struct.unpack(">HHH", fmt[4:10])
        rshift, gshift, bshift = fmt[10], fmt[11], fmt[12]
        if bpp != 32 or not true_colour:
            return True, {"error": f"unexpected pixel format bpp={bpp} tc={true_colour}"}

        sock.sendall(struct.pack(">BBHi", 2, 0, 1, 0))  # SetEncodings: raw only
        sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))  # full, not incremental
        sock.settimeout(30)
        while True:
            msg = _recv(sock, 1)[0]
            if msg == 0:
                break
            if msg == 2:  # bell
                continue
            if msg == 1:  # colour map entries: skip
                _pad, _first, n = struct.unpack(">BHH", _recv(sock, 5))
                _recv(sock, n * 6)
                continue
            if msg == 3:  # server cut text
                _recv(sock, 3)
                _recv(sock, struct.unpack(">I", _recv(sock, 4))[0])
                continue
            return True, {"error": f"unexpected message {msg}"}
        _pad, rects = struct.unpack(">BH", _recv(sock, 3))
        order = ">" if big_endian else "<"
        marked = total = 0
        for _ in range(rects):
            _x, _y, w, h, enc = struct.unpack(">HHHHi", _recv(sock, 12))
            if enc != 0:
                return True, {"error": f"non-raw rect encoding {enc}"}
            raw = _recv(sock, w * h * 4)
            for (px,) in struct.iter_unpack(order + "I", raw):
                total += 1
                rgb = (
                    (px >> rshift & rmax) * 255 // rmax,
                    (px >> gshift & gmax) * 255 // gmax,
                    (px >> bshift & bmax) * 255 // bmax,
                )
                if all(abs(a - b) <= 8 for a, b in zip(rgb, MARK, strict=True)):
                    marked += 1
        return True, {"size": (width, height), "name": name, "marked": marked, "total": total}


# --------------------------------------------------------------------------
# Fixtures: page, X server, VNC server, lyra over stdio
# --------------------------------------------------------------------------


class _Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *_args) -> None:
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def free_display() -> int:
    for number in range(140, 200):
        if (
            not Path(f"/tmp/.X11-unix/X{number}").exists()
            and not Path(f"/tmp/.X{number}-lock").exists()
        ):
            return number
    raise RuntimeError("no free X display number in 140..199")


def x_proc_dead(procs: list[subprocess.Popen]) -> bool:
    """Whether the gate's own Xvfb is gone, so its socket and lock are leftovers."""
    return bool(procs) and procs[0].poll() is not None


def wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def rfb_banner(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
            return s.recv(12).startswith(b"RFB ")
    except OSError:
        return False


async def call(client: Client, tool: str, **args) -> dict:
    result = await client.call_tool(tool, args, raise_on_error=False)
    data = result.structured_content or {}
    return dict(data.get("result", data)) if isinstance(data, dict) else {}


async def run(checks: Checks, root: Path, driver: str | None) -> None:
    xvfb, x11vnc = shutil.which("Xvfb"), shutil.which("x11vnc")
    assert xvfb and x11vnc
    number, vnc_port, web_port = free_display(), free_port(), free_port()
    settings = vnc_cli.Settings(
        display=f":{number}", port=vnc_port, listen="127.0.0.1", screen="1600x1000x24"
    )
    vnc_dir = root / "vnc"
    password = vnc_cli.generate_password()
    vnc_cli.write_password(vnc_dir / vnc_cli.PASSWORD_NAME, password)
    (vnc_dir / "vnc.json").write_text(
        json.dumps({"display": settings.display, "port": vnc_port, "listen": "127.0.0.1"})
    )

    web = ThreadingHTTPServer(("127.0.0.1", web_port), _Page)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    procs: list[subprocess.Popen] = []
    logs = root / "logs"
    logs.mkdir()
    try:
        x = subprocess.Popen(
            vnc_cli.xvfb_command(xvfb, settings),
            stdout=subprocess.DEVNULL,
            stderr=(logs / "xvfb.log").open("w"),
        )
        procs.append(x)
        checks.check(
            "Xvfb is up on a private display",
            wait_for(lambda: Path(f"/tmp/.X11-unix/X{number}").exists(), 10),
        )
        v = subprocess.Popen(
            vnc_cli.x11vnc_command(x11vnc, settings, vnc_dir / vnc_cli.PASSWORD_NAME),
            stdout=subprocess.DEVNULL,
            stderr=(logs / "x11vnc.log").open("w"),
        )
        procs.append(v)
        checks.check("x11vnc answers RFB on loopback", wait_for(lambda: rfb_banner(vnc_port), 15))

        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        env.update(
            {
                "LYRA_BROWSER_VNC_DIR": str(vnc_dir),
                "LYRA_BROWSER_DATA_DIR": str(root / "data"),
                "LYRA_BROWSER_TRUSTED_ORIGINS": f"http://127.0.0.1:{web_port}",
                "LYRA_BROWSER_HEADLESS": "",
            }
        )
        env.pop("LYRA_BROWSER_DISPLAY", None)
        if driver:
            env["LYRA_BROWSER_DRIVER"] = driver
        transport = StdioTransport(
            sys.executable,
            ["-m", "lyra_browser", "--client", "claude-code"],
            env=env,
            cwd=str(REPO),
            log_file=logs / "server.stderr",
            keep_alive=False,
        )
        async with Client(transport, timeout=180) as client:
            opened = await call(client, "open_browser")
            checks.check("open_browser ok", opened.get("status") == "ok", opened)
            checks.check("attended is true with no DISPLAY", opened.get("attended") is True, opened)
            checks.check(
                "watch names the VNC address and display",
                opened.get("watch")
                == {"vnc": f"127.0.0.1:{vnc_port}", "display": settings.display},
                opened.get("watch"),
            )
            checks.check("no password in the result", password not in json.dumps(opened))

            nav = await call(client, "navigate", url=f"http://127.0.0.1:{web_port}/")
            checks.check("navigate ok", nav.get("status") == "ok", nav)
            await asyncio.sleep(1.5)  # let the compositor-less X server paint

            ok, info = rfb_session(vnc_port, "wrongpw0" if password != "wrongpw0" else "x")
            checks.check("a wrong VNC password is refused", not ok, info)
            ok, info = rfb_session(vnc_port, password)
            checks.check("the stored password authenticates", ok, info)
            marked, total = info.get("marked", 0), info.get("total", 0)
            # The window is 1280x800 by default on a 1600x1000 screen; the page fills
            # most of it below the toolbar. A quarter of the screen is a wide margin.
            checks.check(
                "the page is on the framebuffer the user sees",
                total > 0 and marked >= total // 4,
                f"{marked}/{total} pixels match {MARK}; {info}",
            )
            closed = await call(client, "close_browser")
            checks.check("close_browser ok", closed.get("status") == "ok", closed)

            # SIGKILL leaves the X socket behind: the display looks alive, and only
            # Chrome's own "Missing X server" can tell. That is what killed the
            # hand-started pair; the session must fall back to headless, not fail.
            sock_path = Path(f"/tmp/.X11-unix/X{number}")
            x.kill()
            x.wait(10)
            checks.check("a killed Xvfb leaves its socket behind", sock_path.exists())
            stale = await call(client, "open_browser")
            checks.check(
                "open_browser ok on a dead X server behind a live socket",
                stale.get("status") == "ok",
                stale,
            )
            checks.check(
                "it fell back to headless and says the display was lost",
                stale.get("attended") is False
                and stale.get("display_lost") == settings.display
                and "watch" not in stale,
                stale,
            )
            await call(client, "close_browser")

            # And with the socket gone (a clean stop), the launch never tries X.
            for leftover in (sock_path, Path(f"/tmp/.X{number}-lock")):
                leftover.unlink(missing_ok=True)
            again = await call(client, "open_browser")
            checks.check(
                "open_browser still ok with the display gone", again.get("status") == "ok", again
            )
            checks.check("it is headless now", again.get("attended") is False, again)
            checks.check(
                "and says the display was lost",
                again.get("display_lost") == settings.display and "watch" not in again,
                again,
            )
            await call(client, "close_browser")
    finally:
        web.shutdown()
        for leftover in (Path(f"/tmp/.X11-unix/X{number}"), Path(f"/tmp/.X{number}-lock")):
            if x_proc_dead(procs):
                leftover.unlink(missing_ok=True)
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--driver",
        choices=["native", "playwright", "patchright"],
        default=None,
        help="LYRA_BROWSER_DRIVER for the server (default: its own choice)",
    )
    args = parser.parse_args()
    missing = [n for n in ("Xvfb", "x11vnc") if not shutil.which(n)]
    if not sys.platform.startswith("linux") or missing:
        print(f"SKIP  needs Linux with Xvfb and x11vnc (missing: {missing or sys.platform})")
        return 0
    checks = Checks()
    root = Path(tempfile.mkdtemp(prefix="lyra-vnc-e2e-"))
    try:
        print(f"driver: {args.driver or 'default'}")
        asyncio.run(run(checks, root, args.driver))
    except Exception as exc:  # noqa: BLE001 — a crash is a failed gate, with its reason
        checks.check("gate ran to the end", False, repr(exc))
    print(f"\n{'OK' if not checks.failed else f'{checks.failed} FAILED'}  (logs: {root / 'logs'})")
    if not checks.failed:
        shutil.rmtree(root, ignore_errors=True)
    return 1 if checks.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
