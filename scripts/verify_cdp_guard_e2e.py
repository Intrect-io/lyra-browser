#!/usr/bin/env python3
"""Real-browser gate for the CDP guard backend.

``Config.guard_backend="cdp"`` replaces ``context.route`` with a second DevTools connection
(``cdp_guard.py``) that judges every document request, redirect hops included. This script
runs the product - real ``ServerContext`` / ``BrowserSession`` / ``NavigationGuard`` in
enforce mode - against a real Chrome on that backend, and checks what the design is for and
what it costs. Loopback only: nothing on the internet is contacted.

Sections (``--only a,b`` picks some; each uses its own data dir and always stops its browser):

  existing    the other gates (browser, scriptredirect, tabs) re-run in this process on cdp
  wire        browser command line, HTTP cache and request headers, the debugging port
  exposure    what another local process can do with that port (measured, documented cost)
  hops        HTTP redirect hops are judged: 301/302/303/307/308, GET and POST, takeover
  failclosed  a lost sidecar kills the browser; a sidecar that cannot start leaves no browser
  scenarios   the probe's fixture on the product, plus noopener/redirected popups, nested
              frames, workers and big uploads (--popup-runs N, --big-upload)
  workers_sw  a service worker that would answer a navigation does not get it past the guard
  speculative speculation rules are a measured limit; back and reload are judged, cache on
  perf        what the guard costs in page-load time against the route backend

Output: ``PASS`` lines are assertions (via ``expect``); ``NOTE`` / ``LIMIT`` / ``EXPOSURE``
lines are measurements that never fail the run. They are repeated at the end for the README.

    .venv/bin/python scripts/verify_cdp_guard_e2e.py --headless-only
    xvfb-run -a -s "-screen 0 1920x1080x24" .venv/bin/python \
        scripts/verify_cdp_guard_e2e.py --headful-only
    /path/to/venv-patchright/bin/python scripts/verify_cdp_guard_e2e.py \
        --driver patchright
    .venv/bin/python scripts/verify_cdp_guard_e2e.py --headless-only --only wire,hops
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import re
import secrets
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(SCRIPTS))

from probe_document_intercept import Scenario, build_scenarios  # noqa: E402
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402
from verify_tabs_e2e import wait_for  # noqa: E402

import lyra_browser  # noqa: E402
from lyra_browser.origin import parse_origin  # noqa: E402
from lyra_browser.permission import Capability  # noqa: E402

SECTION_TIMEOUT_S = 900
SESSION = "default"  # the key tools and the guard use when there is no live MCP request


# --------------------------------------------------------------------------
# Output: PASS lines are counted, NOTE / LIMIT / EXPOSURE lines are kept for the recap
# --------------------------------------------------------------------------


class Tee:
    """``sys.stdout`` that counts the PASS lines and remembers the measurement lines."""

    def __init__(self, stream) -> None:
        self.raw = stream
        self._partial = ""
        self.passed = 0
        self.report: list[str] = []

    def write(self, text: str) -> int:
        self.raw.write(text)
        *lines, self._partial = (self._partial + text).split("\n")
        for line in lines:
            if line.startswith("PASS  "):
                self.passed += 1
            elif line.startswith(("NOTE  ", "LIMIT  ", "EXPOSURE  ")):
                self.report.append(line)
        return len(text)

    def flush(self) -> None:
        self.raw.flush()

    def __getattr__(self, name: str):
        return getattr(self.raw, name)


def note(text: str) -> None:
    print(f"NOTE  {text}", flush=True)


def limit(text: str) -> None:
    print(f"LIMIT  {text}", flush=True)


def exposure(text: str) -> None:
    print(f"EXPOSURE  {text}", flush=True)


# --------------------------------------------------------------------------
# Linux process facts (the browser's command line and sockets)
# --------------------------------------------------------------------------


def read_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def process_alive(pid: int) -> bool:
    """Whether ``pid`` runs (a zombie awaiting its parent does not)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def _decode_address(hex_addr: str) -> str:
    raw = bytes.fromhex(hex_addr)
    if len(raw) == 4:
        return socket.inet_ntoa(raw[::-1])
    return socket.inet_ntop(
        socket.AF_INET6, b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
    )


def listening_sockets(pid: int) -> list[tuple[str, int]]:
    """TCP sockets in LISTEN state that process ``pid`` holds, as ``(address, port)``."""
    inodes = set()
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        with contextlib.suppress(OSError):
            target = os.readlink(fd)
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    found: list[tuple[str, int]] = []
    for table in ("tcp", "tcp6"):
        lines = Path(f"/proc/{pid}/net/{table}").read_text().splitlines()[1:]
        for line in lines:
            fields = line.split()
            if fields[3] == "0A" and fields[9] in inodes:  # 0A = TCP_LISTEN
                address, port = fields[1].rsplit(":", 1)
                found.append((_decode_address(address), int(port, 16)))
    return sorted(found)


def is_loopback(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


def browsers_of(data_dir: Path) -> list[int]:
    """Processes whose command line says they run on a profile under ``data_dir``."""
    wanted = f"--user-data-dir={data_dir}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(arg.startswith(wanted) for arg in args):
            found.append(int(entry.name))
    return found


def main_process(data_dir: Path) -> int | None:
    """The browser process itself (not a renderer or helper) on the profile under ``data_dir``."""
    for pid in browsers_of(data_dir):
        if not any(arg.startswith("--type=") for arg in read_cmdline(pid)):
            return pid
    return None


def launch_switches(cmd: list[str]) -> list[str]:
    """A browser command line without what differs from run to run (binary, profile path)."""
    return [
        "--user-data-dir=<profile>" if arg.startswith("--user-data-dir=") else arg
        for arg in cmd[1:]
    ]


async def no_browser_left(data_dir: Path, timeout: float = 6.0) -> list[int]:
    """Wait for every process on ``data_dir``'s profile to end; the ones that did not."""
    deadline = time.monotonic() + timeout
    while True:
        left = [pid for pid in browsers_of(data_dir) if process_alive(pid)]
        if not left or time.monotonic() >= deadline:
            return left
        await asyncio.sleep(0.1)


# --------------------------------------------------------------------------
# Three loopback origins and a log of what reached them
# --------------------------------------------------------------------------

SW_SCRIPT = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  if (new URL(e.request.url).pathname === '/js/swpage') {
    e.respondWith(new Response('<title>from-sw</title>synthesized by the worker',
      {headers: {'content-type': 'text/html'}}));
  }
});
"""

# The page-side half of the prototype trick: ``navigator.serviceWorker.register`` is replaced
# by Playwright's ``service_workers="block"`` init script, ``ServiceWorkerContainer.prototype``
# is not. The title is the page's way of saying the worker is active.
REGISTER_SW = """<!doctype html><title>reg</title><script>
ServiceWorkerContainer.prototype.register.call(
    navigator.serviceWorker, '/js/sw.js', {scope: '/js/'})
  .then(reg => {
    const w = reg.installing || reg.waiting || reg.active;
    const done = () => { document.title = 'registered'; };
    if (w.state === 'activated') return done();
    w.addEventListener('statechange', () => { if (w.state === 'activated') done(); });
  })
  .catch(e => { document.title = 'error ' + e; });
</script>"""

# 43 bytes: the smallest GIF, for the page with a hundred subresources.
GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b"
)


class QuietServer(ThreadingHTTPServer):
    """A browser that cancels a request (the next navigation does) is not a fixture error."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class World:
    """Three loopback origins and a log of what reached them.

    SITE and PEER share a host (one site to Chrome, two origins to the guard); AWAY is
    another host (``localhost``), so Chrome puts its frames in another process (an OOPIF).
    A request that reaches a server is the ground truth for "the guard did not stop it".
    ``/s/<name>`` serves ``pages[name]`` on every origin, ``/r/<code>?to=<url>`` answers that
    status with a ``Location`` (on GET and on POST), ``/js/<name>`` serves ``js[name]``.
    """

    def __init__(self) -> None:
        ports = [free_port() for _ in range(3)]
        self.site = f"http://127.0.0.1:{ports[0]}"
        self.peer = f"http://127.0.0.1:{ports[1]}"
        self.away = f"http://localhost:{ports[2]}"
        self.hits: list[str] = []
        self.log: list[dict] = []
        self.pages: dict[str, str] = {}
        self.js: dict[str, str] = {"sw.js": SW_SCRIPT}
        self.cookie = ""  # value of the HttpOnly session cookie /plant sets
        self._servers = [
            self._serve(label, port)
            for label, port in zip(("SITE", "PEER", "AWAY"), ports, strict=True)
        ]

    def expand(self, text: str) -> str:
        return (
            text.replace("{SITE}", self.site)
            .replace("{PEER}", self.peer)
            .replace("{AWAY}", self.away)
        )

    def since(self, mark: int) -> list[str]:
        return self.hits[mark:]

    def requests(self, mark: int = 0, *, label: str = "", path: str = "") -> list[dict]:
        return [
            r
            for r in self.log[mark:]
            if (not label or r["label"] == label) and (not path or r["path"] == path)
        ]

    def _serve(self, label: str, port: int) -> ThreadingHTTPServer:
        world = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(
                self,
                body: str | bytes,
                code: int = 200,
                headers: dict | None = None,
                ctype: str = "text/html; charset=utf-8",
            ) -> None:
                raw = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                url = urlparse(self.path)
                path, query = url.path, parse_qs(url.query)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                if path != "/favicon.ico" and not path.startswith("/img/"):
                    world.hits.append(f"{label} {method} {path}")
                    world.log.append(
                        {
                            "label": label,
                            "method": method,
                            "path": path,
                            "query": query,
                            "body": body,
                            "headers": {k.lower(): v for k, v in self.headers.items()},
                        }
                    )
                if path.startswith("/r/"):
                    target = world.expand(query.get("to", [""])[0])
                    self.send_response(int(path.split("/")[2]))
                    self.send_header("Location", target)
                    # A 301/308 is cacheable by default, and the HTTP cache works under this
                    # backend: the second run of a chain would never reach SITE.
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif method == "POST":
                    self._send(f"<!doctype html><title>posted</title>{label} posted")
                elif path.startswith("/s/"):
                    page = world.pages.get(path[3:], "<title>?</title>")
                    self._send(world.expand(page))
                elif path == "/js/swpage":
                    self._send("<!doctype html><title>from-network</title>served by the server")
                elif path.startswith("/js/"):
                    script = world.expand(world.js.get(path[4:], ""))
                    self._send(script, ctype="application/javascript")
                elif path == "/cacheable.js":
                    self._send(
                        "window.cacheable = 1;",
                        headers={"Cache-Control": "max-age=60"},
                        ctype="application/javascript",
                    )
                elif path == "/cached":
                    self._send(
                        "<!doctype html><title>cached</title>cacheable page",
                        headers={"Cache-Control": "max-age=600"},
                    )
                elif path.startswith("/img/"):
                    self._send(GIF, headers={"Cache-Control": "no-store"}, ctype="image/gif")
                elif path == "/plant":
                    self._send(
                        "<!doctype html><title>planted</title>logged in",
                        headers={"Set-Cookie": f"session={world.cookie}; HttpOnly; Path=/"},
                    )
                elif path in ("/framepost", "/framepost-late"):
                    delay = (
                        "setTimeout(()=>f.submit(),700)" if path.endswith("late") else "f.submit()"
                    )
                    self._send(
                        "<!doctype html><title>frame</title><form id=g method=post "
                        'action="/collect"><input name=leak value=x></form>'
                        f"<script>const f=document.getElementById('g');{delay}</script>"
                    )
                elif path == "/redir":
                    self._send("", 302, {"Location": f"{world.away}/dest"})
                else:
                    self._send(f"<!doctype html><title>{path}</title>{label} {path}")

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def log_message(self, *_args: object) -> None:
                pass

        server = QuietServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()


@contextlib.contextmanager
def world_up() -> Iterator[World]:
    # free_port() and the bind are two steps; on a busy host another process can take the
    # port in between, which is a collision of the host, not a finding.
    for attempt in range(5):
        try:
            world = World()
            break
        except OSError:
            if attempt == 4:
                raise
    try:
        yield world
    finally:
        world.close()


# --------------------------------------------------------------------------
# One browser under test
# --------------------------------------------------------------------------

TEMP_DIRS: list[str] = []


@dataclass
class Env:
    mode: str  # "headless" | "headful"
    driver: str
    popup_runs: int = 10  # how often every popup scenario of ``scenarios`` is run
    big_upload: bool = False  # ``scenarios`` also posts 40 MB (about 20 s)

    @property
    def headless(self) -> bool:
        return self.mode == "headless"


@contextlib.contextmanager
def guard_env(backend: str) -> Iterator[None]:
    """``LYRA_BROWSER_GUARD`` for the duration: build_tools reads it when it builds the Config."""
    old = os.environ.get("LYRA_BROWSER_GUARD")
    os.environ["LYRA_BROWSER_GUARD"] = backend
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("LYRA_BROWSER_GUARD", None)
        else:
            os.environ["LYRA_BROWSER_GUARD"] = old


def _must(condition: bool, message: str) -> None:
    """A precondition of the gate itself, not a result worth a PASS line."""
    if not condition:
        raise AssertionError(message)


class CheckedBuild:
    """``build_tools`` for any gate: pins the driver, and checks the backend it was built for.

    The gates this wraps do not know which backend runs them; this is what makes "ran on cdp"
    a fact instead of an environment variable. Every ``open_browser`` that succeeds must have
    left a sidecar on the session (cdp) or none (route).
    """

    def __init__(self, env: Env, backend: str = "cdp") -> None:
        self.env = env
        self.backend = backend
        self.contexts: list = []
        self.launches = 0

    async def __call__(self, data_dir: Path, *, headless: bool):
        TEMP_DIRS.append(str(data_dir))
        with guard_env(self.backend):
            ctx, tools = await build_tools(data_dir, headless=headless)
        _must(ctx.config.guard_backend == self.backend, f"backend is not {self.backend}")
        ctx.config.driver = self.env.driver
        self.contexts.append(ctx)
        tools = dict(tools)
        real_open = tools["open_browser"]

        async def open_browser(*args, **kwargs):
            result = await real_open(*args, **kwargs)
            if result.get("status") == "ok":
                sidecar = ctx.session._cdp_guard
                _must((sidecar is not None) == (self.backend == "cdp"), "wrong guard on session")
                if self.env.driver != "auto":
                    _must(result.get("driver") == self.env.driver, f"driver is {result}")
                self.launches += 1
            return result

        tools["open_browser"] = open_browser
        return ctx, tools


class Rig:
    """A browser built the way the gates build it, plus what this script wants to ask of it."""

    def __init__(self, ctx, tools: dict, data_dir: Path, backend: str, env: Env) -> None:
        self.ctx = ctx
        self.tools = tools
        self.data_dir = data_dir
        self.backend = backend
        self.env = env
        self.answers: list[tuple[str, str]] = []  # every tool answer, as JSON
        self.opened: dict = {}

    @property
    def guard(self):
        """The sidecar of the running browser (``cdp`` backend)."""
        return self.ctx.session._cdp_guard

    async def page(self):
        return await self.ctx.session.page()

    def audit(self, start: int = 0) -> list[dict]:
        path = self.ctx.config.audit_path
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()][start:]

    def mark(self) -> int:
        """Where the audit trail stands now, for ``nav_rows(since)``."""
        return len(self.audit())

    def nav_rows(self, start: int = 0) -> list[dict]:
        return [row for row in self.audit(start) if row["tool"] == "navigation"]

    def grant(self, url: str, capability: str) -> None:
        """An unconstrained grant, as the operator would hand out (single-use if it is one)."""
        self.ctx.perms.grant(SESSION, parse_origin(url), Capability(capability))

    def unspent(self) -> list:
        """One-shot grants (SUBMIT ...) that are still waiting to be spent."""
        return [
            g
            for g in self.ctx.perms.live_grants(SESSION)
            if g.uses_left is not None and g.uses_left > 0
        ]

    def transcript(self) -> str:
        """Everything this browser wrote down or said, for a search."""
        audit = self.ctx.config.audit_path
        said = "\n".join(text for _name, text in self.answers)
        return (audit.read_text() if audit.exists() else "") + "\n" + said


def recording(tools: dict, sink: list[tuple[str, str]]) -> dict:
    out = {}
    for name, fn in tools.items():

        async def call(*args, _fn=fn, _name=name, **kwargs):
            result = await _fn(*args, **kwargs)
            sink.append((_name, json.dumps(result, default=str)))
            return result

        out[name] = call
    return out


@contextlib.asynccontextmanager
async def rig(
    env: Env, tag: str, *, backend: str = "cdp", launch: bool = True, announce: bool = True
) -> AsyncIterator[Rig]:
    """A fresh data dir and browser; stopped, checked for leftovers and deleted on the way out."""
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-cdp-{tag}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    build = CheckedBuild(env, backend)
    ctx, tools = await build(data_dir, headless=env.headless)
    r = Rig(ctx, tools, data_dir, backend, env)
    r.tools = recording(tools, r.answers)
    print(f"\n[{env.mode}] {tag}: backend={backend} data_dir={data_dir}", flush=True)
    try:
        if launch:
            r.opened = await r.tools["open_browser"]()
            if announce:
                expect(r.opened.get("status") == "ok", f"{tag}: browser launches", r.opened)
                expect(
                    ctx.config.guard_backend == backend
                    and (r.guard is not None) == (backend == "cdp"),
                    f"{tag}: guard backend is {backend}"
                    + (" and its sidecar is attached" if backend == "cdp" else ""),
                    (ctx.config.guard_backend, r.guard),
                )
            else:
                _must(r.opened.get("status") == "ok", f"open_browser: {r.opened}")
        yield r
        await ctx.session.stop()
        left = await no_browser_left(data_dir)
        if announce:
            expect(not left, f"{tag}: closing the session leaves no browser process", left)
    finally:
        await ctx.session.stop()
        tmp.cleanup()
        for pid in browsers_of(data_dir):  # whatever a failure left: never keep a window
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

SECTIONS: dict[str, Callable[[Env], Awaitable[None]]] = {}


def section(fn: Callable[[Env], Awaitable[None]]) -> Callable[[Env], Awaitable[None]]:
    SECTIONS[fn.__name__.removeprefix("section_")] = fn
    return fn


# -- 1. existing --------------------------------------------------------------------------


# Gates that fail under patchright on the base commit too (the notes), not on the guard.
PATCHRIGHT_BROKEN = {
    "verify_browser_e2e": "Page.go_back times out under patchright, with either guard backend",
    "verify_tabs_e2e": "page.evaluate cannot read a page global in patchright's isolated world",
}


@section
async def section_existing(env: Env) -> None:
    """The other gates' scenarios (popup first request, undeclared POST, cross-origin, late
    redirect, takeover, lifecycle ...) on the cdp backend, in this process."""
    import verify_browser_e2e as browser_gate
    import verify_scriptredirect_e2e as redirect_gate
    import verify_tabs_e2e as tabs_gate

    flags = (env.headless, not env.headless)
    gates = (
        ("verify_browser_e2e", browser_gate, flags),
        ("verify_scriptredirect_e2e", redirect_gate, (*flags, env.driver)),
        ("verify_tabs_e2e", tabs_gate, flags),
    )
    for name, module, args in gates:
        if env.driver == "patchright" and name in PATCHRIGHT_BROKEN:
            note(f"{name} skipped under patchright: {PATCHRIGHT_BROKEN[name]}")
            continue
        print(f"\n-- gate {name}, {env.mode}, on the cdp backend", flush=True)
        build = CheckedBuild(env)
        saved = module.build_tools
        module.build_tools = build
        try:
            await module.async_main(*args)
        finally:
            module.build_tools = saved
        expect(
            build.launches >= 1 and all(c.config.guard_backend == "cdp" for c in build.contexts),
            f"{name}: its {build.launches} browser launches were all guarded by the cdp sidecar",
            (build.launches, [c.config.guard_backend for c in build.contexts]),
        )


# -- 2. wire ------------------------------------------------------------------------------


async def two_loads(r: Rig, world: World, url: str) -> int:
    """Load ``url`` twice with the tool; the log position before the first load."""
    mark = len(world.log)
    first = await r.tools["navigate"](url=url, reason="wire", confirm=True, wait_until="load")
    _must(first.get("status") == "ok", f"first load: {first}")
    second = await r.tools["navigate"](url=url, wait_until="load")
    _must(second.get("status") == "ok", f"second load: {second}")
    await asyncio.sleep(0.3)  # the subresource requests of the second load, if any
    return mark


@section
async def section_wire(env: Env) -> None:
    """The facts the design is for: Chrome's command line, the cache, the listening socket."""
    with world_up() as world:
        world.pages["wire"] = (
            '<!doctype html><title>wire</title><script src="/cacheable.js"></script>'
        )
        url = f"{world.site}/s/wire"
        async with (
            rig(env, "wire") as r,
            rig(env, "wire-route", backend="route", announce=False) as control,
        ):
            guard = r.guard
            pid = guard.browser_pid
            expect(isinstance(pid, int) and process_alive(pid), "the sidecar knows the pid", pid)

            # (a) the command line of the browser process
            if sys.platform.startswith("linux"):
                cmd = read_cmdline(pid)
                expect("--remote-debugging-port=0" in cmd, "chrome has the debugging port", cmd)
                expect("--remote-debugging-pipe" in cmd, "and still Playwright's own pipe", cmd)
                expect(
                    "--enable-unsafe-swiftshader" not in cmd,
                    "Playwright's --enable-unsafe-swiftshader is dropped",
                    [a for a in cmd if "swiftshader" in a],
                )
                expect(
                    "--disable-blink-features=AutomationControlled" in cmd,
                    "--disable-blink-features=AutomationControlled is still there",
                    cmd,
                )
                route_cmd = read_cmdline(main_process(control.data_dir) or 0)
                ours, theirs = launch_switches(cmd), launch_switches(route_cmd)
                added = [a for a in ours if a not in theirs]
                removed = [a for a in theirs if a not in ours]
                expect(
                    bool(route_cmd)
                    and added == ["--remote-debugging-port=0"]
                    and removed in ([], ["--enable-unsafe-swiftshader"])
                    and "--enable-unsafe-swiftshader" not in ours,
                    "the launch differs from the route backend's by the debugging port and "
                    "the swiftshader switch only (patchright's route launch has none to drop)",
                    (added, removed),
                )
                note(
                    "--enable-automation is "
                    + ("kept" if "--enable-automation" in cmd else "absent from this launch")
                    + " (route backend: "
                    + ("present" if "--enable-automation" in route_cmd else "absent")
                    + ")"
                )
            else:
                note("command line, cache and socket checks need /proc: skipped off Linux")
            impl = getattr(r.ctx.session._context, "_impl_obj", None)
            routes = getattr(impl, "_routes", None)
            if routes is None:
                note("the driver does not expose its routes: 'no context.route' not checked")
            else:
                expect(not routes, "no context.route is installed under the cdp backend", routes)

            # (b) the HTTP cache works, and the document carries nothing that disables it
            mark = await two_loads(r, world, url)
            scripts = world.requests(mark, path="/cacheable.js")
            docs = world.requests(mark, path="/s/wire")
            expect(len(docs) == 2, "both loads reached the server", world.since(mark))
            expect(
                len(scripts) == 1,
                "a cacheable script is requested once across two loads",
                len(scripts),
            )
            sent = sorted(
                {h for d in docs for h in d["headers"] if h in ("cache-control", "pragma")}
            )
            expect(not sent, "the document requests carry neither cache-control nor pragma", sent)
            cmark = await two_loads(control, world, url)
            c_scripts = world.requests(cmark, path="/cacheable.js")
            c_docs = world.requests(cmark, path="/s/wire")
            c_sent = sorted(
                {h for d in c_docs for h in d["headers"] if h in ("cache-control", "pragma")}
            )
            note(
                f"CONTROL route backend, same page loaded twice: cacheable script requested "
                f"{len(c_scripts)}x (cdp: {len(scripts)}x), document request headers "
                f"{c_sent or 'none'} (cdp: {sent or 'none'})"
            )

            # (c) the debugging port
            if sys.platform.startswith("linux"):
                socks = listening_sockets(pid)
                note(f"TCP listeners of the browser process: {socks}")
                expect(
                    bool(socks) and all(is_loopback(a) for a, _ in socks),
                    "every TCP listener of the browser is on loopback, none on 0.0.0.0 or ::",
                    socks,
                )
                expect(
                    len({p for _a, p in socks}) == 1,
                    "and the browser listens on exactly one port: the debugging one",
                    socks,
                )
                endpoint_file = r.ctx.config.profile_dir / "DevToolsActivePort"
                expect(
                    not endpoint_file.exists(),
                    "DevToolsActivePort is deleted once the endpoint was read",
                    endpoint_file,
                )
                port = socks[0][1]

                # (d) the endpoint never reaches an audit row or an answer
                await r.tools["get_url"]()
                await r.tools["read_page"]()
                text = r.transcript()
                leak = [
                    what
                    for what, found in (
                        (f":{port}", re.search(rf":{port}(?!\d)", text)),
                        ("/devtools/browser/", "/devtools/browser/" in text),
                    )
                    if found
                ]
                expect(
                    not leak,
                    "neither the debugging port nor the browser endpoint is in the audit or in "
                    "any tool answer",
                    leak,
                )


# -- 3. exposure --------------------------------------------------------------------------

# What a stranger on this machine does with nothing but the port number: stdlib plus this
# repo's own WebSocket client (``PYTHONPATH=src``), in a process of its own. It prints one JSON
# line when it is done and then keeps its connection open until it is told to leave, so the
# parent can check the guard while a hostile connection is still attached.
EXPOSURE_CLIENT = r'''
import asyncio, base64, json, os, sys, urllib.error, urllib.request
from lyra_browser.cdp_socket import WebSocket

PORT, SECRET, TAB = int(sys.argv[1]), sys.argv[2], sys.argv[3]
out = {}


def http_get(path, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


async def handshake(path, host=None, origin=None):
    """The status line of a raw WebSocket upgrade, with a Host / Origin of our choosing."""
    reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host or '127.0.0.1:%d' % PORT}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if origin:
        lines.append(f"Origin: {origin}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    except Exception as exc:
        return f"error {type(exc).__name__}"
    finally:
        writer.close()
    return head.split(b"\r\n", 1)[0].decode("latin-1")


class Cdp:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method, params=None, session=None):
        self.n += 1
        message = {"id": self.n, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.ws.send_nowait(json.dumps(message))
        while True:
            reply = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
            if reply.get("id") == self.n:
                return reply


async def main():
    status, body = http_get("/json/version")
    info = json.loads(body) if status == 200 else {}
    url = info.get("webSocketDebuggerUrl", "")
    out["version"] = {"status": status, "ws_url": url.startswith("ws://127.0.0.1:"),
                      "browser": info.get("Browser", "")}
    path = "/" + url.split("/", 3)[3] if url else ""
    out["foreign_host_http"] = http_get("/json/version", host="evil.example")[0]
    out["origin_header"] = await handshake(path, origin="http://evil.example")
    out["foreign_host"] = await handshake(path, host="evil.example")
    out["plain"] = await handshake(path)

    ws = await WebSocket.connect("127.0.0.1", PORT, path)
    cdp = Cdp(ws)
    infos = (await cdp.call("Target.getTargets"))["result"]["targetInfos"]
    pages = [t for t in infos if t["type"] == "page"]
    out["tabs"] = [t["url"] for t in pages]
    jar = (await cdp.call("Storage.getCookies"))["result"]["cookies"]
    mine = [c for c in jar if c["name"] == "session"]
    out["cookie"] = {
        "found": bool(mine),
        "http_only": bool(mine) and mine[0]["httpOnly"],
        "value_matches": bool(mine) and mine[0]["value"] == SECRET,
    }
    tab = next((t for t in pages if t["url"].endswith(TAB)), None)
    session = None
    if tab is not None:
        attached = await cdp.call(
            "Target.attachToTarget", {"targetId": tab["targetId"], "flatten": True}
        )
        session = attached["result"]["sessionId"]
        shown = await cdp.call(
            "Runtime.evaluate", {"expression": "document.title", "returnByValue": True}, session
        )
        out["evaluate"] = shown["result"]["result"].get("value")

    # Try to switch the guard off: everything a connection of one's own may do to a target
    sabotage = {}
    steps = [
        ("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session),
        ("Fetch.disable", {}, session),
        ("Target.setAutoAttach",
         {"autoAttach": False, "waitForDebuggerOnStart": False, "flatten": True}, None),
        ("Target.detachFromTarget", {"sessionId": session}, None),
    ]
    for method, params, owner in steps:
        reply = await cdp.call(method, params, owner)
        sabotage[method] = "error" not in reply
    out["sabotage"] = sabotage
    print(json.dumps(out), flush=True)
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
    await ws.close()


asyncio.run(main())
'''


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


@section
async def section_exposure(env: Env) -> None:
    """What another local process can do with the debugging port: the documented cost."""
    with world_up() as world:
        world.cookie = f"SECRET-{secrets.token_hex(8)}"
        async with rig(env, "exposure") as r:
            planted = await r.tools["navigate"](
                url=f"{world.site}/plant", reason="log in", confirm=True
            )
            expect(planted.get("title") == "planted", "the agent is logged in on SITE", planted)
            guard, pid = r.guard, r.guard.browser_pid
            # What a port scan of loopback finds: no endpoint file, no pid, no secret.
            scan = listening_sockets(pid)
            expect(len(scan) == 1, "a scan finds one listening port in the browser", scan)
            port = scan[0][1]
            tabs_before = {t.target_id for t in guard._targets.values()}
            hits_before = len(world.hits)

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                EXPOSURE_CLIENT,
                str(port),
                world.cookie,
                "/plant",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": str(REPO / "src")},
            )
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), 60)
                try:
                    got = json.loads(raw)
                except ValueError:
                    stderr = await asyncio.wait_for(proc.stderr.read(), 5)
                    raise AssertionError(
                        f"the stranger failed: {raw!r} {stderr.decode()[-800:]}"
                    ) from None

                version = got["version"]
                expect(
                    version["status"] == 200 and version["ws_url"],
                    "GET /json/version needs no secret: 200 and a webSocketDebuggerUrl",
                    version,
                )
                expect(
                    any(url.endswith("/plant") for url in got["tabs"]),
                    "Target.getTargets lists the tabs and their URLs",
                    got["tabs"],
                )
                jar = got["cookie"]
                expect(
                    jar == {"found": True, "http_only": True, "value_matches": True},
                    "Storage.getCookies returns the HttpOnly session cookie",
                    jar,
                )
                expect(
                    got.get("evaluate") == "planted",
                    "Runtime.evaluate runs script in the logged-in tab",
                    got.get("evaluate"),
                )
                expect(
                    " 403 " in got["origin_header"],
                    "a WebSocket handshake with an Origin header is refused (403)",
                    got["origin_header"],
                )
                expect(
                    " 101 " in got["plain"] and " 101 " not in got["foreign_host"],
                    "and so is one with a foreign Host (the plain handshake is accepted)",
                    (got["plain"], got["foreign_host"]),
                )
                expect(
                    got["foreign_host_http"] != 200,
                    "as is plain HTTP with a foreign Host",
                    got["foreign_host_http"],
                )

                # Its connection is still attached. The agent's next tool call is judged.
                expect(
                    all(got["sabotage"].values()),
                    "Fetch.enable / disable, setAutoAttach off and detach all succeeded for it",
                    got["sabotage"],
                )
                refusals = r.ctx.guard.refusals
                mark = r.mark()
                away = f"{world.site}/r/302?to={world.away}/dest"
                hop = await r.tools["navigate"](url=away)
                expect(
                    hop.get("status") == "blocked_by_policy",
                    "the guard still refuses an ungranted cross-origin hop",
                    hop,
                )
                denied = [
                    row
                    for row in r.nav_rows(mark)
                    if row["status"] == "denied" and row["args"].get("redirect_from")
                ]
                expect(
                    r.ctx.guard.refusals == refusals + 1
                    and bool(denied)
                    and not any(h.startswith("AWAY") for h in world.since(hits_before)),
                    "with a denied row, a counted refusal, and nothing at AWAY",
                    (denied, world.since(hits_before)),
                )
                tabs_after = {t.target_id for t in guard._targets.values()}
                expect(
                    tabs_after == tabs_before and guard.lost is None,
                    "the guard has the same targets and has not been lost",
                    (tabs_before, tabs_after, guard.lost),
                )
            finally:
                if proc.returncode is None:
                    with contextlib.suppress(OSError, ValueError):
                        proc.stdin.write(b"exit\n")
                        await proc.stdin.drain()
                    try:
                        await asyncio.wait_for(proc.wait(), 10)
                    except TimeoutError:
                        proc.kill()
                        await proc.wait()

            closed = await r.tools["close_browser"](reason="exposure done")
            expect(closed.get("status") == "ok", "close_browser closes the window", closed)
            await wait_for(lambda: not port_open(port), "the debugging port to close", 5.0)
            expect(not port_open(port), "the port is closed after close_browser", port)
            leaked = re.search(rf":{port}(?!\d)|/devtools/browser/", r.transcript())
            expect(leaked is None, "and the endpoint never reached the audit or an answer", leaked)

            exposure(
                "a random loopback port, no authentication, open for as long as the browser "
                "runs and closed after close_browser. A local process that finds it (a port "
                "scan) can, with "
                "no secret: read /json/version, list the tabs and their URLs, read the HttpOnly "
                "session cookie (Storage.getCookies) and run script in the logged-in tab "
                "(Runtime.evaluate) - all measured, all YES. A web page cannot: a handshake "
                "with an Origin header gets 403 and one with a foreign Host is refused. It "
                "cannot switch the guard off from its own connection (Fetch.enable/disable, "
                "setAutoAttach off, detach: a cross-origin hop was still refused, same "
                f"targets). {version['browser']}"
            )


# -- 4. hops ------------------------------------------------------------------------------

HOP_CODES = (301, 302, 303, 307, 308)
BODY = "sensitive-body"


def hop_url(world: World, code: int, to: str = "") -> str:
    """SITE answers ``code`` with a Location: AWAY/dest unless told where else."""
    return f"{world.site}/r/{code}?to={to or world.away + '/dest'}"


def hop_rows(r: Rig, mark: int, world: World, code: int) -> list[dict]:
    """Navigation rows for the hop that SITE's ``/r/<code>`` redirect produced.

    ``args.redirect_from`` is the URL of the answering request as the audit writes it: query
    values stripped (``loggable_url``).
    """
    source = f"{world.site}/r/{code}?<redacted: to>"
    return [row for row in r.nav_rows(mark) if row["args"].get("redirect_from") == source]


async def stand_on_site(r: Rig, world: World) -> None:
    """Every grant gone, then the agent on SITE with the one lease a navigate buys."""
    r.ctx.perms.revoke_all()
    home = await r.tools["navigate"](url=f"{world.site}/s/home", reason="hops", confirm=True)
    _must(home.get("status") == "ok", f"cannot stand on SITE: {home}")


async def hop_get(r: Rig, world: World, code: int) -> None:
    """The navigate tool follows a chain SITE -> AWAY: refused, then granted."""
    await stand_on_site(r, world)
    home = f"{world.site}/s/home"
    away = parse_origin(world.away).describe()
    refusals, mark, seen = r.ctx.guard.refusals, r.mark(), len(world.hits)
    refused = await r.tools["navigate"](url=hop_url(world, code))
    expect(
        refused.get("status") == "blocked_by_policy" and refused.get("url") == home,
        f"{code} GET: the hop to AWAY is refused and the page stays where it was",
        refused,
    )
    rows = hop_rows(r, mark, world, code)
    reached = [h for h in world.since(seen) if h.startswith("AWAY")]
    expect(
        not reached
        and world.since(seen) == [f"SITE GET /r/{code}"]
        and len(rows) == 1
        and rows[0]["status"] == "denied"
        and rows[0]["args"]["capability"] == "navigate"
        and rows[0]["args"]["method"] == "GET"
        and rows[0]["origin"] == away
        and r.ctx.guard.refusals == refusals + 1,
        f"{code} GET: AWAY saw nothing, the audit has a denied row naming the SITE url, "
        "the guard counted one refusal",
        (world.since(seen), rows, r.ctx.guard.refusals - refusals),
    )
    r.grant(world.away, "navigate")
    mark, seen = r.mark(), len(world.hits)
    landed = await r.tools["navigate"](url=hop_url(world, code))
    rows = [row for row in hop_rows(r, mark, world, code) if row["status"] == "allowed"]
    expect(
        landed.get("status") == "ok"
        and landed.get("url") == f"{world.away}/dest"
        and world.since(seen) == [f"SITE GET /r/{code}", "AWAY GET /dest"]
        and len(rows) == 1
        and rows[0]["args"]["capability"] == "navigate",
        f"{code} GET: with AWAY granted the same chain lands there, and the hop is an allowed row",
        (landed, world.since(seen), rows),
    )


async def hop_post(r: Rig, world: World, code: int) -> None:
    """A form POST to SITE that SITE redirects to AWAY, sent with the click tool."""
    keeps_body = code in (307, 308)  # 301/302/303 turn a POST into a body-less GET
    world.pages[f"hopform{code}"] = (
        "<!doctype html><title>hopform</title>"
        f'<form method=post action="/r/{code}?to={{AWAY}}/dest">'
        f'<input name=x value="{BODY}"><button id=go>go</button></form>'
    )
    form = f"{world.site}/s/hopform{code}"
    await stand_on_site(r, world)
    opened = await r.tools["navigate"](url=form)
    _must(opened.get("status") == "ok", f"cannot open the form: {opened}")

    async def submit() -> tuple[int, int]:
        """One declared submission; the audit mark and the server mark from before it."""
        mark, seen = r.mark(), len(world.hits)
        clicked = await r.tools["click"](
            selector="#go", submits=True, confirm=True, reason="hop e2e"
        )
        # The hop here is refused, and a click answers that: blocked_by_policy, naming where
        # the site sent the browser (an approved hop would be ok).
        _must(
            clicked.get("status") in ("ok", "blocked_by_policy"),
            f"click: {clicked}",
        )
        await wait_for(lambda: hop_rows(r, mark, world, code), f"the {code} hop to be judged")
        await asyncio.sleep(0.3)  # whatever the verdict did not stop is at AWAY by now
        return mark, seen

    wanted = "submit" if keeps_body else "navigate"
    mark, seen = await submit()
    rows = hop_rows(r, mark, world, code)
    first = [row for row in r.nav_rows(mark) if row["args"]["method"] == "POST"][:1]
    expect(
        len(first) == 1
        and first[0]["status"] == "allowed"
        and len(rows) == 1
        and rows[0]["status"] == "denied"
        and rows[0]["args"]["capability"] == wanted
        and rows[0]["args"]["method"] == ("POST" if keeps_body else "GET"),
        f"{code} POST: the form's own POST is paid by the one-shot SUBMIT; "
        f"the hop is a {'POST with the body' if keeps_body else 'body-less GET'} "
        f"and needs {wanted}: denied",
        (first, rows),
    )
    page = await r.page()
    expect(
        world.since(seen) == [f"SITE POST /r/{code}"] and page.url == form and not r.unspent(),
        f"{code} POST: AWAY never saw the body, the form page stays, no one-shot grant is left",
        (world.since(seen), page.url, r.unspent()),
    )

    if keeps_body:
        # AWAY's navigate grant does not pay for a hop that carries a body: that is a send,
        # judged on the sender, and the sender's one-shot SUBMIT was spent by the first POST.
        r.grant(world.away, "navigate")
        mark, seen = await submit()
        rows = hop_rows(r, mark, world, code)
        expect(
            len(rows) == 1
            and rows[0]["status"] == "denied"
            and rows[0]["args"]["capability"] == "submit"
            and world.since(seen) == [f"SITE POST /r/{code}"],
            f"{code} POST: granting AWAY navigation does not let the body through",
            (rows, world.since(seen)),
        )
        r.grant(world.site, "submit")  # a second one-shot, so the hop can be paid for too
        mark, seen = await submit()
        arrived = world.requests(seen, label="AWAY")
        rows = hop_rows(r, mark, world, code)
        expect(
            len(arrived) == 1
            and arrived[0]["method"] == "POST"
            and BODY in arrived[0]["body"]
            and len(rows) == 1
            and rows[0]["status"] == "allowed"
            and rows[0]["args"]["capability"] == "submit"
            and not r.unspent(),
            f"{code} POST: a second SUBMIT lets the hop through and the body arrives",
            (arrived, rows, r.unspent()),
        )
    else:
        r.grant(world.away, "navigate")
        mark, seen = await submit()
        arrived = world.requests(seen, label="AWAY")
        rows = hop_rows(r, mark, world, code)
        page = await r.page()
        expect(
            len(arrived) == 1
            and arrived[0]["method"] == "GET"
            and not arrived[0]["body"]
            and len(rows) == 1
            and rows[0]["status"] == "allowed"
            and page.url == f"{world.away}/dest"
            and not r.unspent(),
            f"{code} POST: with AWAY granted the hop is allowed and arrives as a GET, no body",
            (arrived, rows, page.url),
        )


async def hop_same_origin(r: Rig, world: World) -> None:
    """A redirect that stays on its origin is the same request carrying on: no new decision."""
    await stand_on_site(r, world)
    mark, seen = r.mark(), len(world.hits)
    res = await r.tools["navigate"](url=hop_url(world, 302, to=f"{world.site}/dest"))
    rows = r.nav_rows(mark)
    expect(
        res.get("status") == "ok"
        and res.get("url") == f"{world.site}/dest"
        and world.since(seen) == ["SITE GET /r/302", "SITE GET /dest"]
        and len(rows) == 1
        and not rows[0]["args"].get("redirect_from"),
        "302 to the same origin: completes, and the hop adds no audit row",
        (res, world.since(seen), rows),
    )

    world.pages["hopform-same"] = (
        "<!doctype html><title>hopform</title>"
        f'<form method=post action="/r/307?to={{SITE}}/dest">'
        f'<input name=x value="{BODY}"><button id=go>go</button></form>'
    )
    await stand_on_site(r, world)
    await r.tools["navigate"](url=f"{world.site}/s/hopform-same")
    mark, seen = r.mark(), len(world.hits)
    clicked = await r.tools["click"](selector="#go", submits=True, confirm=True, reason="same")
    _must(clicked.get("status") == "ok", f"click: {clicked}")
    await wait_for(lambda: "SITE POST /dest" in world.since(seen), "the 307 to be followed")
    await asyncio.sleep(0.3)
    rows = r.nav_rows(mark)
    landed = world.requests(seen, label="SITE", path="/dest")
    expect(
        world.since(seen) == ["SITE POST /r/307", "SITE POST /dest"]
        and len(landed) == 1
        and BODY in landed[0]["body"]
        and [(row["status"], row["args"]["capability"]) for row in rows] == [("allowed", "submit")]
        and not r.unspent(),
        "307 POST to the same origin: both requests complete, one SUBMIT paid for the lot, "
        "no extra audit row for the hop",
        (world.since(seen), rows, r.unspent()),
    )


async def hop_takeover(r: Rig, world: World) -> None:
    """The user's own navigation through a cross-origin hop: judged as theirs, not refused."""
    if r.env.headless:
        note("takeover needs an attended window: the user_driven hop is checked headful only")
        return
    await stand_on_site(r, world)
    taken = await r.tools["request_takeover"](reason="hops e2e")
    expect(taken.get("status") == "takeover_active", "takeover starts", taken)
    page = await r.page()
    mark, seen = r.mark(), len(world.hits)
    await page.goto(hop_url(world, 302), wait_until="load", timeout=10000)  # the user's hand
    rows = r.nav_rows(mark)
    expect(
        page.url == f"{world.away}/dest"
        and world.since(seen) == ["SITE GET /r/302", "AWAY GET /dest"]
        and len(rows) == 2
        and all(row["status"] == "user_driven" for row in rows)
        and bool(rows[-1]["args"].get("redirect_from")),
        "during a takeover a cross-origin hop is user_driven and arrives",
        (page.url, world.since(seen), rows),
    )
    resumed = await r.tools["resume_after_takeover"](reason="hops e2e")
    expect(resumed.get("status") == "ok", "control returns to the agent", resumed)
    await stand_on_site(r, world)
    seen = len(world.hits)
    again = await r.tools["navigate"](url=hop_url(world, 302))
    expect(
        again.get("status") == "blocked_by_policy"
        and not any(h.startswith("AWAY") for h in world.since(seen)),
        "and once it has, the same hop is refused again",
        (again, world.since(seen)),
    )


@section
async def section_hops(env: Env) -> None:
    """HTTP redirect hops are judged, for every status code, GET and POST."""
    with world_up() as world:
        world.pages["home"] = "<!doctype html><title>home</title>home"
        async with rig(env, "hops") as r:
            for code in HOP_CODES:
                await hop_get(r, world, code)
            for code in HOP_CODES:
                await hop_post(r, world, code)
            await hop_same_origin(r, world)
            await hop_takeover(r, world)
        # Control, for the README: what today's backend does with the same chain.
        async with rig(env, "hops-route", backend="route", announce=False) as c:
            await c.tools["navigate"](url=f"{world.site}/s/home", reason="control", confirm=True)
            seen, mark = len(world.hits), c.mark()
            res = await c.tools["navigate"](url=hop_url(world, 302))
            reached = [h for h in world.since(seen) if h.startswith("AWAY")]
            hop_rows_seen = [x for x in c.nav_rows(mark) if x["args"].get("redirect_from")]
            note(
                "CONTROL route backend, navigate to SITE/r/302 -> AWAY with only SITE granted: "
                f"status={res.get('status')} "
                f"landed_on_away={res.get('url', '').startswith(world.away)} "
                f"AWAY requests={reached} audit rows for the hop={len(hop_rows_seen)}"
            )


# -- 5. scenarios -------------------------------------------------------------------------


@dataclass
class Case(Scenario):
    """A scenario of the probe's fixture, plus what a run on the product can ask of it."""

    title: str = ""  # what the page's title must say afterwards: its workers reported back


# Pages and scripts the extra cases need next to the scenarios' own HTML.
SUPPORT_PAGES = {
    # the middle frame of the nested case: an AWAY page that frames a SITE page which POSTs
    "nested-mid": '<!doctype html><title>mid</title><iframe src="{SITE}/framepost"></iframe>',
}
SUPPORT_JS = {
    "dedicated.js": (
        "fetch('{AWAY}/worker-fetch', {mode: 'no-cors'})"
        ".then(() => postMessage('ran'), () => postMessage('failed'));"
    ),
    "shared.js": (
        "onconnect = e => { const port = e.ports[0];"
        " fetch('{AWAY}/shared-fetch', {mode: 'no-cors'})"
        ".then(() => port.postMessage('ran'), () => port.postMessage('failed')); };"
    ),
}
WORKERS_PAGE = (
    "<!doctype html><title>workers</title><script>"
    "const ran = [];"
    "const said = who => e => {"
    " ran.push(who + ':' + e.data); document.title = ran.sort().join(','); };"
    "new Worker('/js/dedicated.js').onmessage = said('dedicated');"
    "const shared = new SharedWorker('/js/shared.js'); shared.port.onmessage = said('shared');"
    "shared.port.start();"
    "</script>"
)


def _opener(call: str) -> str:
    return f'<button id=go onclick="{call}">go</button>'


def _post_via_link(action: str) -> str:
    """A form for a new tab, sent by the script of a link: ``target=_blank`` and a POST."""
    return (
        f'<form id=f method=post action="{action}" target=_blank>'
        "<input name=x value=1></form>"
        '<a id=go href="#" onclick="document.getElementById(\'f\').submit();return false">'
        "go</a>"
    )


def extra_cases() -> list[Case]:
    """What the probe's fixture does not have: noopener popups (no opener to ride on), a popup
    that is redirected, frames inside frames, and workers."""
    away, away_post = ("AWAY GET /dest",), ("AWAY POST /collect",)
    return [
        Case(
            "noopener-open-away",
            _opener("window.open('{AWAY}/dest','_blank','noopener')"),
            "#go",
            forbidden=away,
            race=True,
        ),
        Case(
            "noopener-open-ok",
            _opener("window.open('/dest','_blank','noopener')"),
            "#go",
            required=("SITE GET /dest",),
            race=True,
        ),
        Case(
            "noopener-anchor-away",
            '<a id=go target=_blank rel=noopener href="{AWAY}/dest">go</a>',
            "#go",
            forbidden=away,
            race=True,
        ),
        Case(
            "noopener-anchor-ok",
            '<a id=go target=_blank rel=noopener href="/dest">go</a>',
            "#go",
            required=("SITE GET /dest",),
            race=True,
        ),
        Case(
            "popup-form-link-away",
            _post_via_link("{AWAY}/collect"),
            "#go",
            forbidden=away_post,
            race=True,
        ),
        Case(
            "popup-form-link-granted",
            _post_via_link("/collect"),
            "#go",
            grant_submit=True,
            required=("SITE POST /collect",),
            race=True,
        ),
        Case(
            "popup-redirect-anchor",
            '<a id=go target=_blank href="/redir">go</a>',
            "#go",
            required=("SITE GET /redir",),
            forbidden=away,
            race=True,
        ),
        Case(
            "popup-redirect-open",
            _opener("window.open('/redir')"),
            "#go",
            required=("SITE GET /redir",),
            forbidden=away,
            race=True,
        ),
        Case(
            "iframe-nested-post",
            '<iframe src="{AWAY}/s/nested-mid"></iframe>',
            required=("AWAY GET /s/nested-mid", "SITE GET /framepost"),
            forbidden=("SITE POST /collect",),
            wait=1.5,
        ),
        Case(
            "workers-fetch-away",
            WORKERS_PAGE,
            required=("AWAY GET /worker-fetch", "AWAY GET /shared-fetch"),
            wait=1.5,
            title="dedicated:ran,shared:ran",
        ),
    ]


def build_cases() -> list[Case]:
    return [Case(**vars(sc)) for sc in build_scenarios()] + extra_cases()


def soft(failures: list[str], condition: bool, message: str, detail: object = "") -> None:
    """``expect`` that goes on after a failure: the section prints every failed check and
    raises once, at the end, so one bad scenario does not hide the rest of the table."""
    try:
        expect(condition, message, detail)
    except AssertionError as exc:
        failures.append(message)
        print(f"FAIL  {exc}", flush=True)


async def reset_tabs(r: Rig, page) -> None:
    """Every popup closed and the working tab blank, as the next scenario expects it."""
    for other in list(r.ctx.session._context.pages):
        if other is not page:
            with contextlib.suppress(Exception):
                await other.close()
    with contextlib.suppress(Exception):
        await page.goto("about:blank", timeout=10000)


async def run_case(r: Rig, world: World, page, sc: Case) -> dict:
    """One run of a scenario under enforce mode with NAVIGATE on SITE and nothing else."""
    mark, audit_mark, refusals = len(world.hits), r.mark(), r.ctx.guard.refusals
    # No grant left over from an earlier run may pay for this one (a single-use SUBMIT that
    # a popup never spent would otherwise let a later, unrelated POST through).
    r.ctx.perms.revoke_all()
    r.grant(world.site, "navigate")
    if sc.grant_submit:
        r.grant(world.site, "submit")
    error = ""
    try:
        await page.goto(f"{world.site}/s/{sc.name}", wait_until="commit", timeout=15000)
        if sc.click:
            for _ in range(sc.clicks):
                await page.click(sc.click, no_wait_after=True, timeout=5000)
                await asyncio.sleep(0.3)
    except Exception as exc:  # noqa: BLE001 - a refused navigation may surface here
        error = f"{type(exc).__name__}: {str(exc)[:80]}"
    await asyncio.sleep(sc.wait)
    hits = world.since(mark)
    try:
        title = await asyncio.wait_for(page.evaluate("document.title"), 5)
    except Exception:  # noqa: BLE001 - a dead page is a result
        title = None
    guard = r.guard
    run = {
        "hits": hits,
        "escaped": [h for h in sc.forbidden if h in hits],
        "missing": [h for h in sc.required if h not in hits],
        "miscount": [h for h, n in sc.exactly if hits.count(h) != n],
        "refusals": r.ctx.guard.refusals - refusals,
        "denied": [row for row in r.nav_rows(audit_mark) if row["status"] == "denied"],
        "alive": isinstance(title, str),
        "title": title,
        "unspent": [repr(g) for g in r.unspent()],
        "lost": guard.lost,
        "kinds": {t.kind for t in guard._targets.values()},
        "error": error,
    }
    await reset_tabs(r, page)
    return run


def case_problems(sc: Case, run: dict) -> list[str]:
    found = []
    if run["escaped"]:
        found.append(f"ESCAPED {run['escaped']}")
    if run["missing"]:
        found.append(f"MISSING {run['missing']} (hits {run['hits']}, {run['error'] or 'no error'})")
    if run["miscount"]:
        found.append(f"COUNT {run['miscount']}")
    if sc.forbidden and not run["refusals"]:
        found.append("nothing was refused")
    if run["refusals"] and not run["denied"]:
        found.append("a refusal without a denied audit row")
    if not run["alive"]:
        found.append("the page is dead")
    if run["unspent"]:
        found.append(f"unspent one-shot grants {run['unspent']}")
    if run["lost"]:
        found.append(f"the guard is lost: {run['lost']}")
    if not run["kinds"] <= {"page", "iframe"}:
        found.append(f"guarded target kinds {sorted(run['kinds'])}")
    if sc.title and run["title"] != sc.title:
        found.append(f"title {run['title']!r}, wanted {sc.title!r}")
    return found


def case_verdict(sc: Case, problems: list[str]) -> str:
    if problems:
        return "FAIL " + "; ".join(sorted({p.split(" (")[0] for p in problems}))
    return (
        ", ".join((["loads"] if sc.required else []) + (["refused"] if sc.forbidden else []))
        or "ok"
    )


def upload_page(size: int) -> str:
    """Posts a multipart form with one textarea of ``size`` bytes, the moment it loads."""
    return (
        "<!doctype html><title>upload</title>"
        "<form id=f method=post enctype='multipart/form-data' action='/collect'>"
        "<textarea name=t id=t></textarea></form>"
        f"<script>document.getElementById('t').value = 'x'.repeat({size});"
        "document.getElementById('f').submit()</script>"
    )


async def upload_case(
    r: Rig, world: World, page, megabytes: int, failures: list[str]
) -> tuple[str, str]:
    """A navigation that carries its whole upload in Fetch.requestPaused, judged and then sent."""
    size = megabytes << 20
    name = f"upload-{megabytes}MB"
    world.pages[name] = upload_page(size)
    url = f"{world.site}/s/{name}"
    wait = 30.0 if megabytes <= 12 else 90.0

    # Ungranted: refused, and the server never sees a byte of it.
    r.ctx.perms.revoke_all()
    r.grant(world.site, "navigate")
    mark, seen, refusals = r.mark(), len(world.log), r.ctx.guard.refusals
    started = time.monotonic()
    await page.goto(url, wait_until="commit", timeout=60000)
    denied = lambda: [x for x in r.nav_rows(mark) if x["status"] == "denied"]  # noqa: E731
    await wait_for(denied, f"{name}: the refusal", wait)
    refused_s = time.monotonic() - started
    await asyncio.sleep(1.0)
    soft(
        failures,
        not world.requests(seen, path="/collect")
        and denied()[0]["args"]["method"] == "POST"
        and r.ctx.guard.refusals == refusals + 1
        and page.url == url
        and r.guard.lost is None,
        f"{name} ungranted: refused with a denied POST row, no server saw it ({refused_s:.1f}s)",
        (world.since(0)[-3:], denied(), r.guard.lost),
    )

    # Granted: the one-shot SUBMIT pays for it and every byte arrives.
    r.grant(world.site, "submit")
    mark, seen = r.mark(), len(world.log)
    started = time.monotonic()
    await page.goto(url, wait_until="commit", timeout=60000)
    await wait_for(lambda: world.requests(seen, path="/collect"), f"{name}: the body", wait)
    arrived_s = time.monotonic() - started
    got = world.requests(seen, path="/collect")
    await wait_for(lambda: page.url.endswith("/collect"), f"{name}: the answer page", wait)
    soft(
        failures,
        len(got) == 1
        and got[0]["method"] == "POST"
        and len(got[0]["body"]) > size
        and int(got[0]["headers"]["content-length"]) > size
        and not r.unspent()
        and r.guard.lost is None,
        f"{name} granted SUBMIT: the server saw the {size:,}-byte body in {arrived_s:.1f}s, "
        "the grant is spent, the guard is intact",
        ([(x["method"], len(x["body"])) for x in got], r.unspent(), r.guard.lost),
    )
    await reset_tabs(r, page)
    return name, f"refused in {refused_s:.1f}s, sent in {arrived_s:.1f}s"


def print_table(rows: list[tuple[str, str, str]]) -> None:
    width = max(len(name) for name, _runs, _verdict in rows)
    print(f"\n  {'scenario':{width}}  runs  result", flush=True)
    for name, runs, verdict in rows:
        print(f"  {name:{width}}  {runs:>4}  {verdict}", flush=True)


@section
async def section_scenarios(env: Env) -> None:
    """The probe's scenarios on the product, and what only a long run can show: popups many
    times over, nested frames, workers, and uploads that are tens of megabytes."""
    cases = build_cases()
    failures: list[str] = []
    table: list[tuple[str, str, str]] = []
    with world_up() as world:
        for case in cases:
            world.pages[case.name] = case.html
        world.pages.update(SUPPORT_PAGES)
        world.js.update(SUPPORT_JS)
        async with rig(env, "scenarios") as r:
            page = await r.page()
            escapes = 0
            for case in cases:
                count = env.popup_runs if case.race else 1
                runs = [await run_case(r, world, page, case) for _ in range(count)]
                problems = [p for run in runs for p in case_problems(case, run)]
                escapes += sum(1 for run in runs if run["escaped"])
                verdict = case_verdict(case, problems)
                if problems:
                    verdict += f" ({sum(1 for run in runs if case_problems(case, run))}/{count})"
                table.append((case.name, str(count), verdict))
                print(f"  {case.name:26} x{count:<3} {verdict}", flush=True)
                soft(
                    failures,
                    not problems,
                    f"{case.name} x{count}: {verdict}",
                    sorted(set(problems)),
                )
            soft(
                failures,
                escapes == 0,
                f"no forbidden request reached any server in {len(cases)} scenarios "
                f"({sum(env.popup_runs if c.race else 1 for c in cases)} runs)",
                escapes,
            )
            sizes = (3, 12, 40) if env.big_upload else (3, 12)
            try:
                for megabytes in sizes:
                    name, verdict = await upload_case(r, world, page, megabytes, failures)
                    table.append((name, "1", verdict))
            finally:
                print_table(table)
    if failures:
        raise AssertionError(f"{len(failures)} checks failed: {failures}")


# -- 6. workers_sw ------------------------------------------------------------------------


async def register_worker(r: Rig, world: World, page) -> None:
    """AWAY registers its service worker from a page of its own. The context blocks
    ``navigator.serviceWorker.register``; the one on the prototype is not blocked."""
    r.ctx.perms.revoke_all()
    r.grant(world.away, "navigate")
    await page.goto(f"{world.away}/s/reg", wait_until="load", timeout=15000)

    async def answered() -> bool:
        return await page.title() != "reg"

    await wait_for(answered, "AWAY's worker to register", 15.0)
    title = await page.title()
    _must(title == "registered", f"AWAY's service worker did not become active: {title!r}")
    await asyncio.sleep(0.5)  # it claims the open clients


async def tab_states(r: Rig) -> list[tuple[str, str]]:
    """``(url, title)`` of every tab of the browser."""
    states = []
    for tab in list(r.ctx.session._context.pages):
        with contextlib.suppress(Exception):
            states.append((tab.url, await asyncio.wait_for(tab.title(), 3)))
    return states


async def ask_for_swpage(r: Rig, world: World, page, via: str, granted: bool) -> dict:
    """From a SITE page, ask for AWAY/js/swpage, which AWAY's worker would answer itself:
    with ``page.goto`` or with a click on a ``target=_blank`` link."""
    r.ctx.perms.revoke_all()
    r.grant(world.site, "navigate")
    start = f"{world.site}/s/{'swlink' if via == 'link' else 'home'}"
    await page.goto(start, wait_until="load", timeout=15000)
    if granted:
        r.grant(world.away, "navigate")
    mark, seen, refusals = r.mark(), len(world.hits), r.ctx.guard.refusals
    error = ""
    try:
        if via == "link":
            await page.click("#go", no_wait_after=True, timeout=5000)
        else:
            await page.goto(f"{world.away}/js/swpage", wait_until="commit", timeout=4000)
    except Exception as exc:  # noqa: BLE001 - a refused navigation may surface as an error
        error = f"{type(exc).__name__}: {str(exc)[:80]}"
    deadline = time.monotonic() + (5.0 if granted else 1.2)
    tabs: list[tuple[str, str]] = []
    while time.monotonic() < deadline:
        tabs = await tab_states(r)
        if any(title in ("from-network", "from-sw") for _url, title in tabs):
            break
        await asyncio.sleep(0.1)
    away = parse_origin(world.away).describe()
    result = {
        "start": start,
        "opener": (page.url, await page.title()),
        "tabs": tabs,
        "server": world.requests(seen, label="AWAY"),
        "rows": [row for row in r.nav_rows(mark) if row["origin"] == away],
        "refusals": r.ctx.guard.refusals - refusals,
        "error": error,
    }
    await reset_tabs(r, page)
    return result


@section
async def section_workers_sw(env: Env) -> None:
    """A service worker that would answer a navigation does not get it past the guard:
    the cdp backend bypasses workers, so the document request reaches Fetch and is judged."""
    failures: list[str] = []
    with world_up() as world:
        world.pages["reg"] = REGISTER_SW
        world.pages["home"] = "<!doctype html><title>home</title>home"
        world.pages["swlink"] = (
            '<!doctype html><title>swlink</title><a id=go target=_blank href="{AWAY}/js/swpage">'
            "go</a>"
        )
        async with rig(env, "workers-sw") as r:
            page = await r.page()
            await register_worker(r, world, page)
            for via in ("goto", "link"):
                res = await ask_for_swpage(r, world, page, via, granted=False)
                titles = [title for _url, title in res["tabs"]]
                denied = [row for row in res["rows"] if row["status"] == "denied"]
                soft(
                    failures,
                    not res["server"]
                    and "from-sw" not in titles
                    and "from-network" not in titles
                    and not any(url.startswith(world.away) for url, _title in res["tabs"])
                    and res["opener"] == (res["start"], "swlink" if via == "link" else "home")
                    and len(denied) == 1
                    and res["refusals"] == 1,
                    f"{via}: AWAY not granted: the navigation the worker would answer is judged "
                    "(denied row, one refusal), AWAY's server saw nothing, the page is unchanged",
                    res,
                )
                res = await ask_for_swpage(r, world, page, via, granted=True)
                titles = [title for _url, title in res["tabs"]]
                allowed = [row for row in res["rows"] if row["status"] == "allowed"]
                soft(
                    failures,
                    "from-network" in titles
                    and "from-sw" not in titles
                    and [h["path"] for h in res["server"]] == ["/js/swpage"]
                    and len(allowed) == 1
                    and res["refusals"] == 0,
                    f"{via}: AWAY granted NAVIGATE: the page comes from the network (the server "
                    "was hit, the title is the server's, not the worker's)",
                    res,
                )
            expect(r.guard.lost is None, "the guard stayed intact", r.guard.lost)
        # Control, for the README: today's backend, the same worker.
        async with rig(env, "workers-sw-route", backend="route", announce=False) as c:
            cpage = await c.page()
            await register_worker(c, world, cpage)
            res = await ask_for_swpage(c, world, cpage, "goto", granted=False)
            note(
                "CONTROL route backend, AWAY's worker registered, a SITE page navigates to "
                "AWAY/js/swpage with only SITE granted: "
                f"tabs={[title for _url, title in res['tabs']]} "
                f"worker_answered={any(title == 'from-sw' for _url, title in res['tabs'])} "
                f"AWAY requests={len(res['server'])} "
                f"audit rows for AWAY={[row['status'] for row in res['rows']]} "
                f"refusals={res['refusals']} (a worker's answer never reaches the route "
                "handler: unjudged)"
            )
    if failures:
        raise AssertionError(f"{len(failures)} checks failed: {failures}")


# -- 7. speculative -----------------------------------------------------------------------


async def speculate(r: Rig, world: World, page, kind: str) -> None:
    """A SITE page speculates on AWAY (``prefetch`` or ``prerender``), then the link is clicked.
    What Chrome does with speculation never reaches Fetch (measured, both backends): this
    records what that leaves of the guard, and asserts only that nothing hangs."""
    world.pages[f"spec-{kind}"] = (
        '<!doctype html><title>spec</title><script type="speculationrules">'
        f'{{"{kind}":[{{"source":"list","urls":["{{AWAY}}/dest-spec"]}}]}}</script>'
        '<a id=go href="{AWAY}/dest-spec">go</a>'
    )
    r.ctx.perms.revoke_all()
    r.grant(world.site, "navigate")
    seen = len(world.hits)
    await page.goto(f"{world.site}/s/spec-{kind}", wait_until="load", timeout=15000)
    await asyncio.sleep(2.0)  # Chrome speculates when it is idle
    reached = bool(world.requests(seen, label="AWAY"))
    mark, refusals, error = r.mark(), r.ctx.guard.refusals, ""
    try:
        await page.click("#go", no_wait_after=True, timeout=5000)
    except Exception as exc:  # noqa: BLE001 - a refused navigation may surface as an error
        error = f"{type(exc).__name__}: {str(exc)[:60]}"
    await asyncio.sleep(1.5)
    away = parse_origin(world.away).describe()
    rows = [row for row in r.nav_rows(mark) if row["origin"] == away]
    landed = page.url.startswith(world.away)
    limit(
        f"speculation rules ({kind}) on AWAY/dest-spec from a SITE page, only SITE granted: "
        f"the {kind} reached the AWAY server: {reached}; the click that activates it was judged "
        f"by the guard: {bool(rows)} (audit rows for AWAY: {[row['status'] for row in rows]}, "
        f"refusals +{r.ctx.guard.refusals - refusals}); the page ended on AWAY: {landed}"
        + (f" [{error}]" if error else "")
    )
    usable = await asyncio.wait_for(page.evaluate("1 + 1"), 5)
    r.ctx.perms.revoke_all()
    r.grant(world.site, "navigate")
    again = await r.tools["navigate"](url=f"{world.site}/s/home", reason="speculative")
    expect(
        usable == 2 and again.get("status") == "ok",
        f"{kind}: nothing hangs, the tab answers and the next navigation works",
        (usable, again),
    )
    await reset_tabs(r, page)


async def back_and_reload(r: Rig, world: World, page) -> None:
    """History navigation and reload are document requests the HTTP cache could answer
    (``/cached`` is cacheable for ten minutes); the guard must still be asked."""
    away = parse_origin(world.away).describe()
    cached = f"{world.away}/cached"
    r.ctx.perms.revoke_all()
    r.grant(world.away, "navigate")
    r.grant(world.site, "navigate")
    await page.goto(cached, wait_until="load", timeout=15000)
    await page.goto(f"{world.site}/s/home", wait_until="load", timeout=15000)

    # Back into AWAY, which nothing approves any more: refused.
    r.ctx.perms.revoke_all()
    r.grant(world.site, "navigate")
    r.grant(world.site, "interact")  # what the go_back tool itself asks for
    mark, seen, refusals = r.mark(), len(world.hits), r.ctx.guard.refusals
    res = await r.tools["go_back"]()
    denied = [row for row in r.nav_rows(mark) if row["status"] == "denied"]
    expect(
        res.get("status") == "blocked_by_policy"
        and page.url == f"{world.site}/s/home"
        and len(denied) == 1
        and denied[0]["origin"] == away
        and r.ctx.guard.refusals == refusals + 1
        and not world.requests(seen, label="AWAY"),
        "go_back into an origin nothing approves is refused and audited, with the HTTP cache on",
        (res, page.url, denied, world.since(seen)),
    )

    # The same step with AWAY granted: judged (an allowed row) even if the cache answers it.
    r.grant(world.away, "navigate")
    mark, seen = r.mark(), len(world.hits)
    res = await r.tools["go_back"]()
    allowed = [row for row in r.nav_rows(mark) if row["status"] == "allowed"]
    expect(
        res.get("status") == "ok" and page.url == cached and len(allowed) == 1,
        "go_back with AWAY granted lands there, and the guard judged that request",
        (res, page.url, allowed),
    )
    note(
        "go_back to a cacheable AWAY page (max-age=600): AWAY server requests="
        f"{len(world.requests(seen, label='AWAY'))} (0 = the cache answered), "
        f"http_status={res.get('http_status')}"
    )

    # Reload of that page: with the INTERACT lease the tool asks for it passes the guard,
    # and is judged; without the lease the guard refuses the reload itself.
    r.ctx.perms.revoke_all()
    r.grant(world.away, "interact")
    mark, seen = r.mark(), len(world.hits)
    res = await r.tools["reload_page"]()
    allowed = [row for row in r.nav_rows(mark) if row["status"] == "allowed"]
    expect(
        res.get("status") == "ok" and len(allowed) == 1 and page.url == cached,
        "reload_page is judged by the guard (an allowed row) and reloads",
        (res, allowed),
    )
    r.ctx.perms.revoke_all()
    mark, seen, refusals = r.mark(), len(world.hits), r.ctx.guard.refusals
    with contextlib.suppress(Exception):  # the refused reload leaves the tab where it is
        await page.reload(wait_until="commit", timeout=4000)
    denied = [row for row in r.nav_rows(mark) if row["status"] == "denied"]
    expect(
        len(denied) == 1
        and denied[0]["origin"] == away
        and r.ctx.guard.refusals == refusals + 1
        and not world.since(seen)
        and page.url == cached,
        "a reload nothing approves is refused and audited, and reaches no server",
        (denied, world.since(seen), page.url),
    )
    expect(r.guard.lost is None, "and the guard is intact", r.guard.lost)


@section
async def section_speculative(env: Env) -> None:
    """Speculation rules are a measured limit; back and reload are judged with the cache on."""
    with world_up() as world:
        world.pages["home"] = "<!doctype html><title>home</title>home"
        async with rig(env, "speculative") as r:
            page = await r.page()
            for kind in ("prefetch", "prerender"):
                await speculate(r, world, page, kind)
            if env.driver == "patchright":
                note("history and reload checks skipped under patchright: go_back times out there")
            else:
                await back_and_reload(r, world, page)


# -- 8. failclosed ------------------------------------------------------------------------


async def process_gone_ms(pid: int, started: float, timeout: float = 3.0) -> float | None:
    """Milliseconds from ``started`` until ``pid`` is gone, polled every millisecond."""
    while time.monotonic() - started < timeout:
        if not process_alive(pid):
            return (time.monotonic() - started) * 1000
        await asyncio.sleep(0.001)
    return None


async def answers_guard_lost(r: Rig, world: World) -> None:
    """Every tool says ``guard_lost`` until close_browser; the session is still held."""
    calls: list[tuple[str, dict]] = [
        ("get_url", {}),
        ("read_page", {}),
        ("screenshot", {}),
        ("tabs", {}),
        ("navigate", {"url": f"{world.site}/s/home"}),
        ("click", {"selector": "body"}),
        ("open_browser", {}),
    ]
    if not r.env.headless:
        calls.append(("request_takeover", {"reason": "after the loss"}))
    try:
        for name, kwargs in calls:
            res = await r.tools[name](**kwargs)
            expect(res.get("status") == "guard_lost", f"{name} answers guard_lost", res)
    finally:
        r.ctx.collab.takeover = False  # a takeover the loss did not stop would block close_browser
    expect(r.ctx.session.live, "the session still counts as live: nobody else may take it")
    import lyra_browser.context as context_module

    real = context_module._live_session_id
    context_module._live_session_id = lambda: "another-session"
    try:
        other = await r.tools["get_url"]()
    finally:
        context_module._live_session_id = real
    expect(
        other.get("status") == "session_conflict",
        "a second session gets session_conflict, not guard_lost",
        other,
    )


async def kill_the_socket(env: Env) -> None:
    """The sidecar's connection ends while a page that would send something is loaded."""
    with world_up() as world:
        world.pages["home"] = "<!doctype html><title>home</title>home"
        world.pages["timer"] = (
            "<!doctype html><title>timer</title>"
            '<form id=f method=post action="{AWAY}/collect"><input name=x value=1></form>'
            "<script>setTimeout(() => document.getElementById('f').submit(), 1500)</script>"
        )
        async with rig(env, "failclosed-socket") as r:
            ok = await r.tools["navigate"](url=f"{world.site}/s/timer", reason="e2e", confirm=True)
            expect(ok.get("title") == "timer", "the page with the delayed POST loads", ok)
            sidecar = r.guard
            pid = sidecar.browser_pid
            started = time.monotonic()
            sidecar.abort_connection()
            gone = await process_gone_ms(pid, started)
            expect(gone is not None, "within 3s the browser process is gone", gone)
            await wait_for(lambda: r.ctx.session._context is None, "the context to be released")
            await answers_guard_lost(r, world)
            rows = [x for x in r.nav_rows() if x["status"] == "guard_lost"]
            expect(
                len(rows) == 1 and re.fullmatch(r"[A-Za-z ]+", rows[0]["detail"] or "0"),
                "the audit has one guard_lost row with a fixed phrase, no port",
                rows,
            )
            await asyncio.sleep(2.5)  # the page's timer would have fired at 1.5s
            expect(
                not any("POST" in h for h in world.hits if h.startswith("AWAY")),
                "and AWAY recorded no POST after the kill",
                world.hits,
            )
            closed = await r.tools["close_browser"](reason="acknowledged")
            expect(closed.get("status") == "ok", "close_browser acknowledges the loss", closed)
            reopened = await r.tools["open_browser"]()
            fresh = r.guard.browser_pid if r.guard else None
            expect(
                reopened.get("status") == "ok" and fresh not in (None, pid),
                "open_browser then starts a new browser with a new sidecar",
                (reopened, pid, fresh),
            )
            await stand_on_site(r, world)
            blocked = await r.tools["navigate"](url=hop_url(world, 302))
            expect(
                blocked.get("status") == "blocked_by_policy",
                "and the new guard judges a cross-origin hop again",
                blocked,
            )


async def loss_window(env: Env, run: int) -> None:
    """A hostile page POSTs to AWAY every 4 ms; the connection is cut; none may get out."""
    with world_up() as world:
        world.pages["leak"] = (
            "<title>leak</title><iframe name=sink></iframe>"
            "<form id=f method=post target=sink><input name=x value=1></form>"
            "<script>let i=0; setInterval(()=>{ f.action='{AWAY}/leak?i='+(i++); f.submit(); },"
            " 4)</script>"
        )
        async with rig(env, f"failclosed-window{run}", announce=run == 1) as r:
            r.grant(world.site, "navigate")
            await r.tools["navigate"](url=f"{world.site}/s/leak")
            # Until the page has been refused a fair number of times: how many a second it
            # manages depends on the host (97-150 on a quiet one, a handful at load 40).
            deadline = time.monotonic() + 20
            while r.ctx.guard.refusals <= 20 and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.3)
            refused = r.ctx.guard.refusals
            before = [h for h in world.hits if h.startswith("AWAY")]
            if refused <= 20:
                note(
                    f"run {run}: only {refused} attempts were refused before the abort: the host "
                    "is too loaded for the 4 ms page (97-150 on a quiet one); proves less"
                )
            expect(
                not before and refused >= 1,
                f"run {run}: the guard refused every POST of the hostile page "
                f"({refused} refusals, AWAY got {len(before)})",
                (refused, before),
            )
            mark, pid, started = len(world.hits), r.guard.browser_pid, time.monotonic()
            r.guard.abort_connection()
            gone = await process_gone_ms(pid, started)
            await asyncio.sleep(0.5)
            leaked = [h for h in world.since(mark) if h.startswith("AWAY")]
            note(
                f"fail-closed: abort -> process gone {'never' if gone is None else round(gone)} "
                f"ms, POSTs that reached AWAY after the abort: {len(leaked)}"
            )
            expect(
                gone is not None and gone < 250 and not leaked,
                f"run {run}: the browser is gone within 250 ms and nothing reached AWAY",
                (gone, leaked),
            )


async def cannot_start(env: Env) -> None:
    """No endpoint to connect to: open_browser says guard_lost and no Chrome is left."""
    from lyra_browser import cdp_guard

    async with rig(env, "failclosed-nostart", launch=False) as r:
        real = cdp_guard.read_endpoint

        async def missing(*_args, **_kwargs):
            raise cdp_guard.CdpGuardError("no debugging endpoint")

        cdp_guard.read_endpoint = missing
        try:
            opened = await r.tools["open_browser"]()
        finally:
            cdp_guard.read_endpoint = real
        expect(opened.get("status") == "guard_lost", "open_browser answers guard_lost", opened)
        left = await no_browser_left(r.data_dir)
        rows = [x for x in r.nav_rows() if x["status"] == "guard_lost"]
        expect(
            not r.ctx.session.started and not left,
            "no session and no Chrome left on that profile",
            (r.ctx.session.started, left),
        )
        expect(
            len(rows) == 1 and rows[0]["detail"] == "no debugging endpoint",
            "the audit has the row",
            rows,
        )
        again = await r.tools["open_browser"]()
        expect(
            again.get("status") == "ok" and r.guard is not None,
            "with the endpoint back, open_browser works",
            again,
        )


async def user_closes_window(env: Env) -> None:
    """A user closing the last window is not a guard loss."""
    async with rig(env, "failclosed-userclose") as r:
        pages = list(r.ctx.session._context.pages)
        for page in pages:
            await page.close()
        await asyncio.sleep(0.8)
        answers = [(await r.tools[n]()).get("status") for n in ("get_url", "read_page", "tabs")]
        expect(
            all(a == "browser_closed" for a in answers) and r.ctx.session._guard_lost is None,
            "reads answer browser_closed, never guard_lost",
            answers,
        )
        reopened = await r.tools["open_browser"]()
        expect(reopened.get("status") == "ok", "and open_browser starts again", reopened)


async def unguardable_target(env: Env) -> None:
    """A target the sidecar cannot put Fetch on is never left running: the window is killed."""
    from lyra_browser import cdp_guard

    with world_up() as world:
        world.pages["opener"] = (
            "<title>opener</title><button id=go onclick=\"window.open('/dest')\">go</button>"
        )
        async with rig(env, "failclosed-setup") as r:
            r.grant(world.site, "navigate")
            await r.tools["navigate"](url=f"{world.site}/s/opener")
            pid = r.guard.browser_pid
            real = cdp_guard.CdpGuard._call

            async def refuses_fetch(self, method, params=None, session=""):
                if method == "Fetch.enable" and session:
                    raise cdp_guard._CallError(method, -32000, "injected")
                return await real(self, method, params, session)

            cdp_guard.CdpGuard._call = refuses_fetch
            try:
                page = await r.page()
                started = time.monotonic()
                with contextlib.suppress(Exception):
                    await page.click("#go", no_wait_after=True, timeout=5000)
                gone = await process_gone_ms(pid, started)
            finally:
                cdp_guard.CdpGuard._call = real
            expect(gone is not None, "the window is killed when a popup cannot be guarded", gone)
            expect(
                "SITE GET /dest" not in world.hits,
                "and the popup never ran: its request was never sent",
                world.hits,
            )
            await wait_for(lambda: r.ctx.session._context is None, "the context to be released")
            res = await r.tools["get_url"]()
            rows = [x for x in r.nav_rows() if x["status"] == "guard_lost"]
            expect(
                res.get("status") == "guard_lost"
                and len(rows) == 1
                and rows[0]["detail"] == "a target could not be guarded",
                "tools answer guard_lost and the audit says why",
                (res, rows),
            )


@section
async def section_failclosed(env: Env) -> None:
    """Losing the sidecar closes the browser; a sidecar that cannot start leaves none."""
    for run in (1, 2, 3):
        await loss_window(env, run)
    await cannot_start(env)
    await user_closes_window(env)
    await unguardable_target(env)
    await kill_the_socket(env)  # last: headful, it ends on request_takeover (see the report)


# -- 9. perf ------------------------------------------------------------------------------

PERF_RUNS = 15


async def load_times(page, url: str, runs: int) -> list[float]:
    """Milliseconds of ``page.goto(wait_until="load")`` ``runs`` times, after one load that is
    not counted. The tab is blank before each timed load, so none is a same-URL reload."""
    await page.goto(url, wait_until="load", timeout=30000)
    samples = []
    for _ in range(runs):
        await page.goto("about:blank")
        started = time.perf_counter()
        await page.goto(url, wait_until="load", timeout=30000)
        samples.append((time.perf_counter() - started) * 1000)
    return samples


@section
async def section_perf(env: Env) -> None:
    """What the cdp guard costs in time, against the route backend: NOTE lines, no gate."""
    with world_up() as world:
        world.pages["perf"] = "<!doctype html><title>perf</title>perf"
        world.pages["many"] = "<!doctype html><title>many</title>" + "".join(
            f'<img src="/img/{i}.gif" width=1 height=1>' for i in range(100)
        )
        urls = {"simple page": f"{world.site}/s/perf", "100 subresources": f"{world.site}/s/many"}
        medians: dict[str, dict[str, float]] = {}
        for backend in ("cdp", "route"):
            async with rig(env, f"perf-{backend}", backend=backend, announce=False) as r:
                r.grant(world.site, "navigate")
                page = await r.page()
                medians[backend] = {}
                for label, url in urls.items():
                    samples = await load_times(page, url, PERF_RUNS)
                    medians[backend][label] = statistics.median(samples)
                loaded = await page.evaluate(
                    "[...document.images].filter(i => i.complete && i.naturalWidth === 1).length"
                )
                if loaded != 100:  # the host was too loaded to serve them all: say so, do not fail
                    note(
                        f"{backend}: only {loaded} of 100 images loaded (host load); rough timings"
                    )
        for label in urls:
            cdp, route = medians["cdp"][label], medians["route"][label]
            note(
                f"perf ({env.mode}), page.goto(load) median of {PERF_RUNS} runs, {label}: "
                f"cdp {cdp:.1f} ms, route {route:.1f} ms, delta {cdp - route:+.1f} ms "
                f"({(cdp - route) / route:+.0%})"
            )


# -- main ---------------------------------------------------------------------------------


async def run_sections(env: Env, names: list[str], tee: Tee) -> list[str]:
    """Run the sections in order; a failed one is reported and the rest still run."""
    failures: list[str] = []
    for name in names:
        before, started = tee.passed, time.monotonic()
        print(f"\n===== [{env.mode}] {name} =====", flush=True)
        try:
            await asyncio.wait_for(SECTIONS[name](env), SECTION_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - one broken section must not hide the others
            failures.append(f"{env.mode}/{name}")
            print(f"FAIL  [{env.mode}] {name}: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
        print(
            f"----- [{env.mode}] {name}: {tee.passed - before} checks passed "
            f"in {time.monotonic() - started:.1f}s",
            flush=True,
        )
    return failures


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


async def async_main(args: argparse.Namespace, tee: Tee) -> int:
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(SECTIONS)
    unknown = [n for n in names if n not in SECTIONS]
    if unknown:
        print(f"unknown section(s) {unknown}; known: {list(SECTIONS)}", file=sys.stderr)
        return 2
    modes = [
        mode
        for mode, only in (("headless", args.headless_only), ("headful", args.headful_only))
        if only or not (args.headless_only or args.headful_only)
    ]
    if "headful" in modes and not has_display():
        if args.headful_only:
            print("--headful-only needs a display: run it under xvfb-run", file=sys.stderr)
            return 2
        note("no DISPLAY: the headful pass is skipped")
        modes.remove("headful")
    failures: list[str] = []
    for mode in modes:
        started, before = time.monotonic(), tee.passed
        env = Env(
            mode=mode,
            driver=args.driver,
            popup_runs=args.popup_runs,
            big_upload=args.big_upload,
        )
        failures += await run_sections(env, names, tee)
        print(
            f"\n##### [{mode}] {tee.passed - before} checks passed in "
            f"{time.monotonic() - started:.0f}s",
            flush=True,
        )
    # To the raw stream: through the tee these lines would be collected again, forever.
    print("\n##### measurements, for the README", file=tee.raw)
    for line in list(tee.report):
        print(line, file=tee.raw)
    print(f"\n##### {tee.passed} checks passed in total; failed sections: {failures or 'none'}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Real-browser gate for the CDP guard backend.",
        epilog="Sections: " + ", ".join(SECTIONS),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    parser.add_argument("--driver", choices=["auto", "playwright", "patchright"], default="auto")
    parser.add_argument(
        "--popup-runs", type=int, default=10, help="runs of every popup scenario (scenarios)"
    )
    parser.add_argument(
        "--big-upload", action="store_true", help="also post 40 MB in scenarios (about 20 s)"
    )
    parser.add_argument("--only", default="", help="comma separated section names (default: all)")
    args = parser.parse_args()
    # Everything is built from this: build_tools reads it when it makes a Config.
    os.environ["LYRA_BROWSER_GUARD"] = "cdp"
    under_test = Path(lyra_browser.__file__).resolve()
    if REPO / "src" not in under_test.parents:
        print(f"lyra_browser is imported from {under_test}, not from {REPO}/src", file=sys.stderr)
        return 2
    tee = Tee(sys.stdout)
    sys.stdout = tee
    note(f"lyra_browser under test: {under_test.parent}")
    try:
        return asyncio.run(async_main(args, tee))
    finally:
        sys.stdout = tee.raw


if __name__ == "__main__":
    raise SystemExit(main())
