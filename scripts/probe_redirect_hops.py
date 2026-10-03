#!/usr/bin/env python3
"""Probe: what would answering every judged document from Node change?

``context.route`` only sees the first request of a redirect chain; Chrome follows the
rest itself. One way to judge every hop *before* it leaves is to stop letting Chrome
send the document request at all: the handler fetches it from Node with
``route.fetch(max_redirects=0)`` and hands the answer back with
``route.fulfill(response=...)`` ("relay"), instead of ``route.continue_()`` ("native").
This script measures what that swap changes for the page and for the server.

A *judged document* is a main-frame navigation request (``is_navigation_request()`` and
the frame has no parent) or any POST navigation. Every other request is always
``route.continue_()``. The handler is installed with ``context.route("**/*", handler)``
on a fresh Chrome (channel ``chrome``, headless unless ``--headful``) per variant.

Variants (``--variants``, default ``native,relay``):

  native          every request is ``route.continue_()``
  relay           judged documents go through ``route.fetch`` + ``route.fulfill``; if the
                  fetch raises, the handler records it and ``route.abort()``s
  relay-fallback  as ``relay``, but a failed fetch falls back to ``route.continue_()``
                  (only ``errors`` runs it by default)

Subcommands are experiments. Each prints what it measured, writes the raw rows to
``<out-dir>/<sub>-<driver>-<version>[-headful].json`` and asserts nothing about a
verdict; the ``vs native`` lines are a mechanical comparison of the fields that are not
timings, nothing more.

  hops       Loopback chain /r1 -302-> /r2 -302-> /final on one origin, and a cross-origin
             chain A -302-> B -302-> B. Which requests the route handler saw, which
             ``context.on("request")`` saw (with ``redirected_from``), what reached the
             servers.
  stop       Hop to another origin whose server delays its answer by --delays ms. From the
             ``request`` event of the hop, CDP ``Page.stopLoading`` through
             ``ctx.new_cdp_session(page)``. Per delay, --reps times: did the tab stay on
             the starting page, did goto raise ERR_ABORTED, did the hop still reach its
             server, event-to-stop latency. Run once with --headful under xvfb too.
  headers    The full request header list a loopback server receives for a typed
             navigation, a cross-site link click, a cross-site form POST and a same-site
             click, plus which of SameSite=Strict/Lax/None/unspecified cookies were sent.
  stream     Chunked response: headers + 1 KB at once, then 1 KB every 250 ms for 3 s.
             goto(wait_until="commit") time and when ``body.innerText`` first has text.
  download   A --mb (default 300) MB ``Content-Disposition: attachment`` body generated
             on the fly. Time to the download event and completion, peak RSS of the
             Playwright node driver and of this python process. Downloaded files are
             deleted.
  encoding   /gzip /deflate /chunked (/br when the ``brotli`` module exists): the rendered
             text, the decoded body and the response headers must match.
  cookies    ``sub.localhost`` (and ``localhost``) setting Domain=localhost, a cookie over
             4096 bytes, ``__Host-``/``__Secure-`` without Secure, a Max-Age=0 deletion
             and duplicate names; ``context.cookies()`` after, and what is sent back.
  errors     Exact goto / route.fetch exception text for an unresolvable host, a closed
             loopback port, a self-signed HTTPS loopback server and a server that never
             answers (goto timeout 5 s).
  multivalue Three Set-Cookie, two Link and one Vary header: ``headers_array()``.
  tls        Not loopback: ONE request per variant to https://tls.peet.ws/api/all, 6 s
             apart. Not part of ``all``.
  all        hops stop headers stream download encoding cookies errors multivalue.
  report     Render every JSON in --out-dir as one markdown file (``--md``), with the
             optional one-line verdicts of ``<out-dir>/verdicts.json``
             (``{"hops": "...", ...}``).

Run (``$PY`` = the repo test venv, ``$PYPR`` = a venv with patchright; both venvs have
lyra_browser installed editable, but this script needs only Playwright, not that
package)::

    $PY   scripts/probe_redirect_hops.py hops
    $PYPR scripts/probe_redirect_hops.py hops --driver patchright
    PYTHONPATH=/tmp/pw158 $PY scripts/probe_redirect_hops.py headers   # playwright 1.58
    xvfb-run -a -s "-screen 0 1920x1080x24" $PY scripts/probe_redirect_hops.py stop --headful
    $PY scripts/probe_redirect_hops.py report --out-dir /tmp/redir-exp/wire

Results are JSON under ``--out-dir`` (default ``testing/redirect_hops_out``, gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import html
import importlib.metadata
import importlib.util
import json
import os
import shutil
import socket
import ssl
import statistics
import subprocess
import tempfile
import threading
import time
import zlib
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "testing" / "redirect_hops_out"
TLS_ECHO = "https://tls.peet.ws/api/all"
EXPERIMENTS = [
    "hops",
    "stop",
    "headers",
    "stream",
    "download",
    "encoding",
    "cookies",
    "errors",
    "multivalue",
]

# --------------------------------------------------------------------------
# Loopback servers
# --------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Srv4(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: object) -> None:
        pass


class _Srv6(_Srv4):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        super().server_bind()


class _Handler(BaseHTTPRequestHandler):
    """Logs every arrival (headers in wire order, original case), then runs the app."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        self.body = self.rfile.read(length) if length else b""
        owner: Server = self.server.owner  # type: ignore[attr-defined]
        owner.log.append(
            {
                "t": time.monotonic(),
                "srv": owner.name,
                "host": self.headers.get("Host"),
                "method": self.command,
                "path": self.path,
                "version": self.request_version,
                "headers": [[k, v] for k, v in self.headers.items()],
                "body": self.body.decode("latin1"),
            }
        )
        try:
            owner.app(self)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = _dispatch
    do_POST = _dispatch

    def reply(
        self,
        status: int = 200,
        body: bytes | str = b"",
        ctype: str = "text/html; charset=utf-8",
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    def route(self) -> str:
        return urlsplit(self.path).path


class Server:
    """One loopback origin on 127.0.0.1 *and* ::1 (``localhost`` may resolve to either)."""

    def __init__(
        self,
        app: Callable[[_Handler], None],
        name: str = "S",
        log: list[dict] | None = None,
        tls: tuple[str, str] | None = None,
    ) -> None:
        self.app = app
        self.name = name
        self.log: list[dict] = log if log is not None else []
        self._servers: list[_Srv4] = []
        for _ in range(40):
            port = free_port()
            try:
                v4 = _Srv4(("127.0.0.1", port), _Handler)
            except OSError:
                continue
            self._servers = [v4]
            try:
                self._servers.append(_Srv6(("::1", port), _Handler))
            except OSError:
                pass  # no IPv6 loopback; v4 alone still serves 127.0.0.1 and localhost
            self.port = port
            break
        else:
            raise RuntimeError("no free port")
        for srv in self._servers:
            srv.owner = self  # type: ignore[attr-defined]
            if tls:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(*tls)
                srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.scheme = "https" if tls else "http"

    def url(self, host: str = "127.0.0.1") -> str:
        return f"{self.scheme}://{host}:{self.port}"

    def close(self) -> None:
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()


class HangServer:
    """Accepts connections, reads nothing, answers nothing."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self._held: list[socket.socket] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.accepted += 1
            self._held.append(conn)

    def close(self) -> None:
        self._sock.close()
        for conn in self._held:
            conn.close()


# --------------------------------------------------------------------------
# Driver, Chrome, and the route handler under test
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Driver:
    name: str
    api: object
    version: str


def load_driver(name: str) -> Driver:
    if name == "patchright":
        from patchright import async_api
    else:
        from playwright import async_api
    return Driver(name, async_api, importlib.metadata.version(name))


def judged(request: object) -> bool:
    """Main-frame navigation requests and any POST navigation."""
    if not request.is_navigation_request():  # type: ignore[attr-defined]
        return False
    if request.method == "POST":  # type: ignore[attr-defined]
        return True
    try:
        return request.frame.parent_frame is None  # type: ignore[attr-defined]
    except Exception:
        return True


class Rig:
    """The route handler of one variant, plus everything it and the context observed."""

    def __init__(self, ctx: object, variant: str) -> None:
        self.ctx = ctx
        self.variant = variant
        self.seen: list[dict] = []
        self.events: list[dict] = []
        self.fetch_errors: list[dict] = []
        self.pending = 0

    async def install(self) -> None:
        await self.ctx.route("**/*", self.handle)  # type: ignore[attr-defined]
        self.ctx.on("request", self._on_request)  # type: ignore[attr-defined]

    def _on_request(self, request: object) -> None:
        origin = request.redirected_from  # type: ignore[attr-defined]
        self.events.append(
            {
                "t": time.monotonic(),
                "method": request.method,  # type: ignore[attr-defined]
                "url": request.url,  # type: ignore[attr-defined]
                "nav": request.is_navigation_request(),  # type: ignore[attr-defined]
                "redirected_from": origin.url if origin else None,
            }
        )

    async def handle(self, route: object, request: object) -> None:
        self.pending += 1
        try:
            is_judged = judged(request)
            origin = request.redirected_from  # type: ignore[attr-defined]
            self.seen.append(
                {
                    "t": time.monotonic(),
                    "method": request.method,  # type: ignore[attr-defined]
                    "url": request.url,  # type: ignore[attr-defined]
                    "nav": request.is_navigation_request(),  # type: ignore[attr-defined]
                    "judged": is_judged,
                    "redirected_from": origin.url if origin else None,
                }
            )
            if self.variant == "native" or not is_judged:
                await route.continue_()  # type: ignore[attr-defined]
                return
            try:
                response = await route.fetch(max_redirects=0)  # type: ignore[attr-defined]
            except Exception as exc:
                self.fetch_errors.append(
                    {"url": request.url, "error": f"{type(exc).__name__}: {exc}"}  # type: ignore[attr-defined]
                )
                if self.variant == "relay-fallback":
                    await route.continue_()  # type: ignore[attr-defined]
                else:
                    await route.abort()  # type: ignore[attr-defined]
                return
            await route.fulfill(response=response)  # type: ignore[attr-defined]
        finally:
            self.pending -= 1

    async def idle(self, limit: float = 40.0) -> None:
        deadline = time.monotonic() + limit
        while self.pending and time.monotonic() < deadline:
            await asyncio.sleep(0.1)


class Chrome:
    def __init__(self, drv: Driver, pw: object, browser: object, headless: bool) -> None:
        self.drv = drv
        self.pw = pw
        self.browser = browser
        self.headless = headless

    async def rig(self, variant: str, **ctx_kwargs: object) -> Rig:
        ctx = await self.browser.new_context(**ctx_kwargs)  # type: ignore[attr-defined]
        rig = Rig(ctx, variant)
        await rig.install()
        return rig


@asynccontextmanager
async def chrome(drv: Driver, headless: bool = True, **launch: object):
    async with drv.api.async_playwright() as pw:  # type: ignore[attr-defined]
        browser = await pw.chromium.launch(channel="chrome", headless=headless, **launch)
        try:
            yield Chrome(drv, pw, browser, headless)
        finally:
            await browser.close()


# --------------------------------------------------------------------------
# Comparison and reporting helpers
# --------------------------------------------------------------------------


def label_url(url: str | None, servers: list[Server], hosts: tuple[str, ...] = ()) -> str | None:
    """``http://127.0.0.1:1234/x`` -> ``A:/x`` for the named servers."""
    if url is None:
        return None
    for srv in servers:
        parts = urlsplit(url)
        if parts.port == srv.port and parts.scheme == srv.scheme:
            tail = parts.path + (f"?{parts.query}" if parts.query else "")
            return f"{srv.name}:{tail}"
    return url


def arrivals(log: list[dict], skip_favicon: bool = True) -> list[str]:
    return [
        f"{a['srv']}:{a['method']} {a['path']}"
        for a in log
        if not (skip_favicon and a["path"].startswith("/favicon"))
    ]


def clip(value: object, n: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= n else text[: n - 1] + "…"


def diff_header_lists(native: list, other: list) -> list[str]:
    """Appear / disappear / case / value / order differences of two ``[name, value]`` lists."""

    def group(rows: list) -> dict[str, list[tuple[str, str]]]:
        out: dict[str, list[tuple[str, str]]] = {}
        for name, value in rows:
            out.setdefault(name.lower(), []).append((name, value))
        return out

    ga, gb = group(native), group(other)
    out: list[str] = []
    gone = [n for n in ga if n not in gb]
    new = [n for n in gb if n not in ga]
    if gone:
        out.append("disappear: " + ", ".join(gone))
    if new:
        out.append("appear: " + ", ".join(new))
    for name in ga:
        if name not in gb:
            continue
        ka, kb = [k for k, _ in ga[name]], [k for k, _ in gb[name]]
        if ka != kb:
            out.append(f"case/count {name}: native {ka} other {kb}")
        va, vb = [v for _, v in ga[name]], [v for _, v in gb[name]]
        if va != vb:
            out.append(f"value {name}: native {clip(va, 90)} other {clip(vb, 90)}")
    common_a = [n for n in ga if n in gb]
    common_b = [n for n in gb if n in ga]
    if common_a != common_b:
        out.append(f"order: native {common_a} other {common_b}")
    return out


def diff_cmp(native: dict, other: dict) -> list[str]:
    """Mechanical difference of two ``cmp`` dicts (the fields that are not timings)."""
    out: list[str] = []
    for key in dict.fromkeys([*native, *other]):
        a, b = native.get(key), other.get(key)
        if a == b:
            continue
        if key.endswith("headers") and isinstance(a, list) and isinstance(b, list):
            out.extend(f"{key}: {d}" for d in diff_header_lists(a, b))
        elif isinstance(a, list) and isinstance(b, list):
            sa = {json.dumps(x, default=str, sort_keys=True) for x in a}
            sb = {json.dumps(x, default=str, sort_keys=True) for x in b}
            only_a = [
                clip(x, 100) for x in a if json.dumps(x, default=str, sort_keys=True) not in sb
            ]
            only_b = [
                clip(x, 100) for x in b if json.dumps(x, default=str, sort_keys=True) not in sa
            ]
            if only_a or only_b:
                out.append(f"{key}: only native {only_a} only other {only_b}")
            else:
                out.append(f"{key}: same members, different order/count")
        else:
            out.append(f"{key}: native={clip(a, 120)} other={clip(b, 120)}")
    return out


def finish(
    experiment: str, drv: Driver, args: argparse.Namespace, rows: list[dict], note: str = ""
) -> dict:
    """Pair each non-native row with the native row of its scenario, write and print."""
    native = {r.get("scenario", ""): r for r in rows if r["variant"] == "native"}
    for row in rows:
        base = native.get(row.get("scenario", ""))
        if row["variant"] == "native" or base is None:
            continue
        diffs = diff_cmp(base["cmp"], row["cmp"])
        row["vs_native"] = {"equal": not diffs, "diffs": diffs}
    env = {
        "experiment": experiment,
        "driver": drv.name,
        "version": drv.version,
        "headless": not args.headful,
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": note,
        "rows": rows,
    }
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-headful" if args.headful else ""
    path = out_dir / f"{experiment}-{drv.name}-{drv.version}{suffix}.json"
    path.write_text(json.dumps(env, indent=1, default=str))
    print(
        f"\n== {experiment}  {drv.name} {drv.version}  {'headful' if args.headful else 'headless'}"
    )
    for row in rows:
        print(f"[{row['variant']:14}] {row.get('scenario', ''):18} {row['summary']}")
        vs = row.get("vs_native")
        if vs:
            print(f"{'':16}vs native: {'equal' if vs['equal'] else 'differs'}")
            for d in vs["diffs"]:
                print(f"{'':20}- {clip(d, 400)}")
    print(f"-> {path}")
    return env


def variants_of(args: argparse.Namespace, default: str = "native,relay") -> list[str]:
    return [v for v in (args.variants or default).split(",") if v]


def med(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def ms(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds * 1000, 1)


def sha(data: bytes | str) -> str:
    return sha1(data.encode() if isinstance(data, str) else data).hexdigest()[:12]


def header_names(row: dict) -> list[str]:
    return [k for k, _ in row["headers"]]


# --------------------------------------------------------------------------
# hops
# --------------------------------------------------------------------------


def hops_app(h: _Handler) -> None:
    path = h.route()
    if path == "/r1":
        h.redirect("/r2")
    elif path == "/r2":
        h.redirect("/final")
    elif path == "/c1":
        h.redirect(h.query()["to"])
    elif path == "/c2":
        h.redirect("/final")
    elif path == "/final":
        h.reply(200, "<title>FINAL</title><h1>final</h1>")
    else:
        h.reply(404, "nf")


async def exp_hops(args: argparse.Namespace, drv: Driver) -> None:
    log: list[dict] = []
    a = Server(hops_app, "A", log)
    b = Server(hops_app, "B", log)
    servers = [a, b]
    scenarios = {
        "same-origin": f"{a.url()}/r1",
        "cross-origin": f"{a.url()}/c1?to={b.url('localhost')}/c2",
    }
    rows: list[dict] = []
    try:
        for variant in variants_of(args):
            async with chrome(drv, not args.headful) as ch:
                rig = await ch.rig(variant)
                for scenario, url in scenarios.items():
                    page = await rig.ctx.new_page()
                    log.clear()
                    rig.seen.clear()
                    rig.events.clear()
                    err = status = None
                    try:
                        resp = await page.goto(url, timeout=args.timeout * 1000)
                        status = resp.status if resp else None
                    except Exception as exc:
                        err = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                    await asyncio.sleep(0.5)

                    def lab(u: str | None) -> str | None:
                        return label_url(u, servers)

                    data = {
                        "handler_saw": [
                            f"{s['method']} {lab(s['url'])}" + ("" if s["nav"] else " (sub)")
                            for s in rig.seen
                            if "favicon" not in s["url"]
                        ],
                        "handler_saw_redirected_from": [
                            lab(s["redirected_from"]) for s in rig.seen if s["redirected_from"]
                        ],
                        "events": [
                            f"{lab(e['url'])}"
                            + (f" <- {lab(e['redirected_from'])}" if e["redirected_from"] else "")
                            for e in rig.events
                            if "favicon" not in e["url"]
                        ],
                        "server_arrivals": arrivals(log),
                        "final_url": lab(page.url),
                        "title": await page.title(),
                        "status": status,
                        "goto_error": err,
                        "fetch_errors": rig.fetch_errors,
                    }
                    cmp = {k: v for k, v in data.items() if k != "fetch_errors"}
                    rows.append(
                        {
                            "variant": variant,
                            "scenario": scenario,
                            "summary": (
                                f"handler={data['handler_saw']} events={data['events']} "
                                f"server={data['server_arrivals']} final={data['final_url']} "
                                f"err={err}"
                            ),
                            "cmp": cmp,
                            "data": data,
                        }
                    )
                    await page.close()
    finally:
        for srv in servers:
            srv.close()
    finish("hops", drv, args, rows)


# --------------------------------------------------------------------------
# stop
# --------------------------------------------------------------------------


def stop_app(h: _Handler) -> None:
    path = h.route()
    if path == "/start":
        h.reply(200, "<title>START</title><h1>start</h1>")
    elif path == "/hop":
        h.redirect(h.query()["to"])
    elif path == "/target":
        time.sleep(int(h.query().get("d", "0")) / 1000)
        h.reply(200, "<title>TARGET</title><h1>target</h1>")
    else:
        h.reply(404, "nf")


async def stop_trial(
    ch: Chrome, variant: str, a: Server, b: Server, delay: int, tag: str, stop: bool
) -> dict:
    rig = await ch.rig(variant)
    page = await rig.ctx.new_page()
    start_url = f"{a.url()}/start"
    await page.goto(start_url)
    t: dict[str, float] = {}
    tasks: list[asyncio.Future] = []
    errors: list[str] = []

    async def do_stop() -> None:
        try:
            session = await rig.ctx.new_cdp_session(page)
            try:
                await session.send("Page.stopLoading")
            finally:
                await session.detach()
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:80]}")
        t["done"] = time.monotonic()

    def on_request(request: object) -> None:
        if request.redirected_from and request.is_navigation_request():  # type: ignore[attr-defined]
            t.setdefault("event", time.monotonic())
            if stop and "stopping" not in t:
                t["stopping"] = 1
                tasks.append(asyncio.ensure_future(do_stop()))

    rig.ctx.on("request", on_request)
    target = f"{b.url('localhost')}/target?d={delay}&t={tag}"
    err = None
    try:
        await page.goto(f"{a.url()}/hop?to={quote(target, safe='')}", timeout=10000)
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    await asyncio.sleep(0.6 + delay / 1000)
    for task in tasks:
        await task
    hits = [x for x in b.log if x["path"].endswith(f"t={tag}")]
    row = {
        "event_seen": "event" in t,
        "stayed_on_start": page.url == start_url,
        "url_after": page.url,
        "title_after": await page.title(),
        "goto_error": err,
        "aborted": bool(err and "ERR_ABORTED" in err),
        "server_saw_hop": len(hits),
        "event_to_stop_ms": ms(t["done"] - t["event"]) if "done" in t and "event" in t else None,
        "arrival_minus_event_ms": ms(hits[0]["t"] - t["event"]) if hits and "event" in t else None,
        "stop_error": errors[0] if errors else None,
    }
    await rig.ctx.close()
    return row


async def exp_stop(args: argparse.Namespace, drv: Driver) -> None:
    a = Server(stop_app, "A")
    b = Server(stop_app, "B")
    delays = [int(x) for x in args.delays.split(",")]
    rows: list[dict] = []
    try:
        for variant in variants_of(args):
            async with chrome(drv, not args.headful) as ch:
                control = await stop_trial(ch, variant, a, b, 0, f"{variant}-control", stop=False)
                rows.append(
                    {
                        "variant": variant,
                        "scenario": "control (no stop)",
                        "summary": (
                            f"stayed={control['stayed_on_start']} url={control['url_after']} "
                            f"aborted={control['aborted']} server_saw={control['server_saw_hop']} "
                            f"event_seen={control['event_seen']}"
                        ),
                        "cmp": {
                            k: control[k]
                            for k in ("stayed_on_start", "aborted", "server_saw_hop", "event_seen")
                        },
                        "data": [control],
                    }
                )
                for delay in delays:
                    trials = [
                        await stop_trial(ch, variant, a, b, delay, f"{variant}-{delay}-{i}", True)
                        for i in range(args.reps)
                    ]
                    n = len(trials)
                    stop_errors = [x["stop_error"] for x in trials if x["stop_error"]]

                    def count(key: str, ts: list[dict] = trials) -> int:
                        return sum(1 for x in ts if x[key])

                    lat = [
                        x["event_to_stop_ms"] for x in trials if x["event_to_stop_ms"] is not None
                    ]
                    early = [
                        x["arrival_minus_event_ms"]
                        for x in trials
                        if x["arrival_minus_event_ms"] is not None
                    ]
                    cmp = {
                        "event_seen": count("event_seen"),
                        "stayed_on_start": count("stayed_on_start"),
                        "aborted": count("aborted"),
                        "server_saw_hop": sum(1 for x in trials if x["server_saw_hop"]),
                    }
                    rows.append(
                        {
                            "variant": variant,
                            "scenario": f"delay={delay}ms",
                            "summary": (
                                f"event {cmp['event_seen']}/{n}; stayed on start "
                                f"{cmp['stayed_on_start']}/{n}; ERR_ABORTED {cmp['aborted']}/{n}; "
                                f"hop reached server {cmp['server_saw_hop']}/{n}; "
                                f"event->stop-done median {med(lat)} ms "
                                f"(min {min(lat, default=None)}"
                                f" max {max(lat, default=None)}); server arrival - event median "
                                f"{med(early)} ms; stop errors {stop_errors[:1]}"
                            ),
                            "cmp": cmp,
                            "data": trials,
                        }
                    )
    finally:
        a.close()
        b.close()
    finish("stop", drv, args, rows)


# --------------------------------------------------------------------------
# headers
# --------------------------------------------------------------------------

COOKIES = {"strict": "Strict", "lax": "Lax", "none": "None; Secure", "plain": None}


def headers_app(h: _Handler) -> None:
    path = h.route()
    if path == "/setcookies":
        sets = [
            (
                "Set-Cookie",
                f"{name}={name.upper()}; Path=/" + (f"; SameSite={ss}" if ss else ""),
            )
            for name, ss in COOKIES.items()
        ]
        h.reply(200, "<title>set</title>ok", headers=sets)
    elif path == "/echo":
        h.reply(200, "<title>echo</title><h1>echo</h1>")
    elif path == "/link":
        other = h.query()["other"]
        h.reply(
            200,
            f'<title>link</title><a id=go href="{other}/echo">go</a>'
            f'<form method=post action="{other}/echo"><input name=a value=1>'
            f"<input id=sub type=submit value=send></form>",
        )
    elif path == "/echo-post":
        h.reply(200, "<title>posted</title>")
    else:
        h.reply(404, "nf")


def header_row(variant: str, scenario: str, arrival: dict | None) -> dict:
    if arrival is None:
        return {
            "variant": variant,
            "scenario": scenario,
            "summary": "no request reached the server",
            "cmp": {"headers": [], "reached": False},
            "data": None,
        }
    lowered = {k.lower(): v for k, v in arrival["headers"]}
    cookie = lowered.get("cookie", "")
    sent = sorted(p.split("=", 1)[0].strip() for p in cookie.split(";") if p.strip())
    interesting = (
        "sec-fetch-site",
        "sec-fetch-mode",
        "sec-fetch-dest",
        "sec-fetch-user",
        "accept-language",
        "accept-encoding",
        "cache-control",
        "pragma",
        "referer",
        "origin",
        "connection",
        "content-type",
    )
    summary = (
        f"{arrival['method']} v={arrival['version']} cookies_sent={sent} "
        + " ".join(f"{k}={lowered[k]!r}" for k in interesting if k in lowered)
        + f" | names={[k for k, _ in arrival['headers']]}"
    )
    return {
        "variant": variant,
        "scenario": scenario,
        "summary": summary,
        "cmp": {
            "headers": arrival["headers"],
            "cookies_sent": sent,
            "method": arrival["method"],
            "version": arrival["version"],
            "body": arrival["body"],
        },
        "data": arrival,
    }


async def click_through(page: object, origin: str, target: str, selector: str) -> None:
    """Open the link page of ``origin`` and activate ``selector`` (link or submit button)."""
    await page.goto(f"{origin}/link?other={target}")  # type: ignore[attr-defined]
    async with page.expect_navigation():  # type: ignore[attr-defined]
        await page.click(selector)  # type: ignore[attr-defined]


async def header_scenario(
    rows: list[dict],
    log: list[dict],
    variant: str,
    name: str,
    action: Callable[[], object],
    path: str,
) -> None:
    log.clear()
    try:
        await action()
    except Exception as exc:
        first = str(exc).splitlines()[0]
        rows.append(
            {
                "variant": variant,
                "scenario": name,
                "summary": f"action failed: {first}",
                "cmp": {"error": first},
                "data": None,
            }
        )
        return
    await asyncio.sleep(0.3)
    hits = [x for x in log if x["path"].split("?")[0] == path]
    rows.append(header_row(variant, name, hits[-1] if hits else None))


async def exp_headers(args: argparse.Namespace, drv: Driver) -> None:
    log: list[dict] = []
    a = Server(headers_app, "A", log)
    b = Server(headers_app, "B", log)
    site_a, site_b = a.url(), b.url("localhost")
    rows: list[dict] = []
    try:
        for variant in variants_of(args):
            async with chrome(drv, not args.headful) as ch:
                rig = await ch.rig(variant)
                page = await rig.ctx.new_page()
                run = partial(header_scenario, rows, log, variant)
                await run(
                    "typed-empty-jar", partial(page.goto, f"{site_b}/setcookies"), "/setcookies"
                )
                jar = sorted((c["name"], c["sameSite"]) for c in await rig.ctx.cookies())
                rows[-1]["data"] = {**(rows[-1]["data"] or {}), "jar_after": jar}
                await run("typed", partial(page.goto, f"{site_b}/echo"), "/echo")
                await run(
                    "click-cross-site", partial(click_through, page, site_a, site_b, "#go"), "/echo"
                )
                await run(
                    "post-cross-site", partial(click_through, page, site_a, site_b, "#sub"), "/echo"
                )
                await run(
                    "click-same-site", partial(click_through, page, site_b, site_b, "#go"), "/echo"
                )
                await run(
                    "post-same-site", partial(click_through, page, site_b, site_b, "#sub"), "/echo"
                )
    finally:
        a.close()
        b.close()
    finish(
        "headers",
        drv,
        args,
        rows,
        note="A=127.0.0.1 page, B=localhost target (different sites); cookies set by B",
    )


# --------------------------------------------------------------------------
# stream
# --------------------------------------------------------------------------


def stream_app(h: _Handler) -> None:
    if h.route() != "/stream":
        h.reply(404, "nf")
        return
    h.send_response(200)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Transfer-Encoding", "chunked")
    h.end_headers()

    def chunk(data: bytes) -> None:
        h.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        h.wfile.flush()

    chunk(b"<!doctype html><meta charset=utf-8><body><pre>" + b"a" * 1024)
    for i in range(12):
        time.sleep(0.25)
        chunk(b"chunk %02d " % i + b"b" * 1000 + b"\n")
    h.wfile.write(b"0\r\n\r\n")


async def poll_text(page: object, t0: float, out: dict, stop: asyncio.Event) -> None:
    while not stop.is_set() and time.monotonic() - t0 < 25:
        try:
            n = await page.evaluate("document.body ? document.body.innerText.length : 0")  # type: ignore[attr-defined]
        except Exception:
            n = 0
        if n > 0:
            out["first_text"] = time.monotonic() - t0
            return
        await asyncio.sleep(0.1)


async def exp_stream(args: argparse.Namespace, drv: Driver) -> None:
    srv = Server(stream_app, "S")
    rows: list[dict] = []
    try:
        for variant in variants_of(args):
            async with chrome(drv, not args.headful) as ch:
                rig = await ch.rig(variant)
                trials = []
                for i in range(args.reps):
                    page = await rig.ctx.new_page()
                    got: dict[str, float] = {}
                    stop = asyncio.Event()
                    t0 = time.monotonic()
                    poller = asyncio.ensure_future(poll_text(page, t0, got, stop))
                    err = None
                    try:
                        await page.goto(
                            f"{srv.url()}/stream?i={variant}{i}", wait_until="commit", timeout=60000
                        )
                        got["commit"] = time.monotonic() - t0
                        await page.wait_for_load_state("load", timeout=60000)
                        got["load"] = time.monotonic() - t0
                    except Exception as exc:
                        err = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                    await poller
                    stop.set()
                    length = await page.evaluate("document.body.innerText.length")
                    trials.append(
                        {
                            "commit_ms": ms(got.get("commit")),
                            "first_text_ms": ms(got.get("first_text")),
                            "load_ms": ms(got.get("load")),
                            "final_text_len": length,
                            "error": err,
                        }
                    )
                    await page.close()
                commits = [t["commit_ms"] for t in trials if t["commit_ms"] is not None]
                firsts = [t["first_text_ms"] for t in trials if t["first_text_ms"] is not None]
                loads = [t["load_ms"] for t in trials if t["load_ms"] is not None]
                lens = sorted({t["final_text_len"] for t in trials})
                per_trial = [(t["commit_ms"], t["first_text_ms"], t["load_ms"]) for t in trials]
                rows.append(
                    {
                        "variant": variant,
                        "scenario": "chunked 3 s",
                        "summary": (
                            f"commit median {med(commits)} ms; first text median {med(firsts)} ms; "
                            f"load median {med(loads)} ms; final text length {lens}; "
                            f"trials={per_trial}"
                        ),
                        "cmp": {"final_text_len": lens},
                        "data": trials,
                    }
                )
    finally:
        srv.close()
    finish("stream", drv, args, rows)


# --------------------------------------------------------------------------
# download
# --------------------------------------------------------------------------


def children_of(pid: int) -> list[int]:
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        if int(stat.rsplit(")", 1)[1].split()[1]) == pid:
            out.append(int(entry))
    return out


def driver_pid() -> int | None:
    """The Playwright node driver: the child of this process running ``run-driver``."""
    for pid in children_of(os.getpid()):
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        if "run-driver" in cmd:
            return pid
    return None


def proc_kb(pid: int, key: str) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith(key + ":"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def reset_peak() -> None:
    Path("/proc/self/clear_refs").write_text("5")  # resets this process's VmHWM


def make_download_app(mb: int, stats: dict) -> Callable[[_Handler], None]:
    def app(h: _Handler) -> None:
        if h.route() != "/big":
            h.reply(404, "nf")
            return
        total = mb * 1024 * 1024
        block = b"0123456789abcdef" * 65536  # 1 MiB
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Disposition", 'attachment; filename="big.bin"')
        h.send_header("Content-Length", str(total))
        h.end_headers()
        sent = 0
        stats["started"] = time.monotonic()
        try:
            while sent < total:
                h.wfile.write(block)
                sent += len(block)
        finally:
            stats["sent"] = sent
            stats["ended"] = time.monotonic()

    return app


async def exp_download(args: argparse.Namespace, drv: Driver) -> None:
    rows: list[dict] = []
    for mb in [int(x) for x in args.mb.split(",")]:
        for variant in variants_of(args):
            stats: dict = {}
            srv = Server(make_download_app(mb, stats), "S")
            dl_dir = Path(tempfile.mkdtemp(prefix="redir-dl-"))
            try:
                async with chrome(drv, not args.headful, downloads_path=str(dl_dir)) as ch:
                    rig = await ch.rig(variant)
                    page = await rig.ctx.new_page()
                    node = driver_pid()
                    node_base = proc_kb(node, "VmRSS") if node else None
                    reset_peak()
                    py_base = proc_kb(os.getpid(), "VmRSS")
                    t0 = time.monotonic()
                    goto_error = fail = path_text = None
                    t_event = t_done = size = None
                    try:
                        async with page.expect_download(timeout=args.timeout * 1000) as info:
                            try:
                                await page.goto(f"{srv.url()}/big", timeout=args.timeout * 1000)
                            except Exception as exc:
                                goto_error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                        download = await info.value
                        t_event = time.monotonic() - t0
                        path = await asyncio.wait_for(download.path(), args.timeout)
                        t_done = time.monotonic() - t0
                        fail = await download.failure()
                        size = os.path.getsize(path) if path else None
                        path_text = str(path)
                        await download.delete()
                    except Exception as exc:
                        fail = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                    node_alive = bool(node) and Path(f"/proc/{node}").exists()
                    browser_connected = ch.browser.is_connected()
                    node_peak = proc_kb(node, "VmHWM") if node else None
                    py_peak = proc_kb(os.getpid(), "VmHWM")
                    data = {
                        "download_event_ms": ms(t_event),
                        "complete_ms": ms(t_done),
                        "size_bytes": size,
                        "expected_bytes": mb * 1024 * 1024,
                        "failure": fail,
                        "goto_error": goto_error,
                        "node_pid": node,
                        "node_alive_after": node_alive,
                        "browser_connected_after": browser_connected,
                        "node_rss_before_mb": round(node_base / 1024, 1) if node_base else None,
                        "node_peak_mb": round(node_peak / 1024, 1) if node_peak else None,
                        "python_rss_before_mb": round(py_base / 1024, 1) if py_base else None,
                        "python_peak_mb": round(py_peak / 1024, 1) if py_peak else None,
                        "server_sent_bytes": stats.get("sent"),
                        "fetch_errors": rig.fetch_errors,
                        "path": path_text,
                    }
                    rows.append(
                        {
                            "variant": variant,
                            "scenario": f"{mb} MB attachment",
                            "summary": (
                                f"download event {data['download_event_ms']} ms; complete "
                                f"{data['complete_ms']} ms; size {size}; node peak RSS "
                                f"{data['node_peak_mb']} MB (before {data['node_rss_before_mb']}); "
                                f"python peak RSS {data['python_peak_mb']} MB "
                                f"(before {data['python_rss_before_mb']}); "
                                f"node alive after {node_alive}, browser connected "
                                f"{browser_connected}; "
                                f"goto err: {goto_error}; failure: {fail}; fetch errors "
                                f"{[e['error'][:80] for e in rig.fetch_errors]}"
                            ),
                            "cmp": {
                                "goto_error": goto_error,
                                "failure": fail,
                                "size_bytes": size,
                            },
                            "data": data,
                        }
                    )
            finally:
                srv.close()
                shutil.rmtree(dl_dir, ignore_errors=True)
            rows[-1]["data"]["download_dir_removed"] = not dl_dir.exists()
    finish("download", drv, args, rows)


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------


def encoding_text() -> str:
    lines = [f'line {i:04d} Café ünï — 日本語 ✓ <&> "q" {"x" * (i % 17)}' for i in range(600)]
    body = html.escape("\n".join(lines))
    return f"<!doctype html><meta charset=utf-8><title>enc</title><pre>{body}"


def encoding_app(text: str) -> Callable[[_Handler], None]:
    raw = text.encode()
    has_br = importlib.util.find_spec("brotli") is not None

    def app(h: _Handler) -> None:
        path = h.route()
        if path == "/gzip":
            h.reply(200, gzip.compress(raw), headers=[("Content-Encoding", "gzip")])
        elif path == "/deflate":
            h.reply(200, zlib.compress(raw), headers=[("Content-Encoding", "deflate")])
        elif path == "/br" and has_br:
            import brotli  # type: ignore[import-not-found]

            h.reply(200, brotli.compress(raw), headers=[("Content-Encoding", "br")])
        elif path == "/identity":
            h.reply(200, raw)
        elif path == "/chunked":
            h.send_response(200)
            h.send_header("Content-Type", "text/html; charset=utf-8")
            h.send_header("Transfer-Encoding", "chunked")
            h.end_headers()
            step = 4093  # odd size: chunk boundaries fall inside multi-byte characters
            for i in range(0, len(raw), step):
                piece = raw[i : i + step]
                h.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            h.wfile.write(b"0\r\n\r\n")
        else:
            h.reply(404, "nf")

    return app


async def exp_encoding(args: argparse.Namespace, drv: Driver) -> None:
    text = encoding_text()
    log: list[dict] = []
    srv = Server(encoding_app(text), "S", log)
    paths = ["/identity", "/gzip", "/deflate", "/chunked"]
    notes = []
    if importlib.util.find_spec("brotli"):
        paths.append("/br")
    else:
        notes.append("/br skipped: no brotli module")
    rows: list[dict] = []
    try:
        for variant in variants_of(args):
            async with chrome(drv, not args.headful) as ch:
                rig = await ch.rig(variant)
                for path in paths:
                    page = await rig.ctx.new_page()
                    log.clear()
                    err = None
                    resp = None
                    try:
                        resp = await page.goto(f"{srv.url()}{path}", timeout=args.timeout * 1000)
                    except Exception as exc:
                        err = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                    body_text = await page.evaluate("document.body ? document.body.innerText : ''")
                    try:
                        body = await resp.body() if resp else b""
                    except Exception as exc:
                        body = f"<{exc}>".encode()
                    rheaders = {k: v for k, v in (resp.headers or {}).items()} if resp else {}
                    sent_ae = [
                        v
                        for k, v in (log[-1]["headers"] if log else [])
                        if k.lower() == "accept-encoding"
                    ]
                    cmp = {
                        "text_sha": sha(body_text),
                        "text_len": len(body_text),
                        "body_sha": sha(body),
                        "body_len": len(body),
                        "status": resp.status if resp else None,
                        "resp_content_encoding": rheaders.get("content-encoding"),
                        "resp_content_length": rheaders.get("content-length"),
                        "resp_transfer_encoding": rheaders.get("transfer-encoding"),
                        "goto_error": err,
                    }
                    rows.append(
                        {
                            "variant": variant,
                            "scenario": path,
                            "summary": (
                                f"status={cmp['status']} text_len={cmp['text_len']} "
                                f"text_sha={cmp['text_sha']} body_sha={cmp['body_sha']} "
                                f"content-encoding={cmp['resp_content_encoding']} "
                                f"content-length={cmp['resp_content_length']} "
                                f"transfer-encoding={cmp['resp_transfer_encoding']} "
                                f"server saw accept-encoding={sent_ae} err={err}"
                            ),
                            "cmp": {**cmp, "server_saw_accept_encoding": sent_ae},
                            "data": {**cmp, "fetch_errors": rig.fetch_errors},
                        }
                    )
                    await page.close()
    finally:
        srv.close()
    finish(
        "encoding",
        drv,
        args,
        rows,
        note="; ".join(notes),
    )


# --------------------------------------------------------------------------
# cookies
# --------------------------------------------------------------------------


def cookies_app(h: _Handler) -> None:
    path = h.route()
    host = (h.headers.get("Host") or "").rsplit(":", 1)[0]
    if path == "/pre":
        h.reply(
            200,
            "<title>pre</title>",
            headers=[("Set-Cookie", "gone=1; Path=/"), ("Set-Cookie", "keep=1; Path=/")],
        )
    elif path == "/set":
        sets = [
            "dom=1; Domain=localhost; Path=/",
            "own=1; Domain=" + host + "; Path=/",
            "big=" + "a" * 5000 + "; Path=/",
            "__Host-nosecure=1; Path=/",
            "__Secure-nosecure=1; Path=/",
            "__Host-ok=1; Secure; Path=/",
            "gone=; Max-Age=0; Path=/",
            "dup=1; Path=/",
            "dup=2; Path=/",
            "dup=3; Path=/a",
            "dup=4; Domain=localhost; Path=/",
        ]
        h.reply(200, "<title>set</title>", headers=[("Set-Cookie", s) for s in sets])
    elif path == "/echo":
        h.reply(200, "<title>echo</title>" + (h.headers.get("Cookie") or "").replace("<", ""))
    else:
        h.reply(404, "nf")


def cookie_names(arrival: dict) -> list[str]:
    """``name(len)`` of each cookie in the Cookie header of one arrival."""
    out = []
    for key, value in arrival["headers"]:
        if key.lower() == "cookie":
            for part in value.split("; "):
                name, _, val = part.partition("=")
                out.append(f"{name}({len(val)})")
    return sorted(out)


def resolves(host: str) -> str:
    try:
        return ",".join(sorted({ai[4][0] for ai in socket.getaddrinfo(host, 80)}))
    except OSError as exc:
        return f"does not resolve ({exc})"


async def exp_cookies(args: argparse.Namespace, drv: Driver) -> None:
    log: list[dict] = []
    srv = Server(cookies_app, "S", log)
    hosts = [h for h in args.cookie_hosts.split(",") if h]
    notes = [f"node resolves {h}: {resolves(h)}" for h in hosts]
    rows: list[dict] = []
    try:
        for host in hosts:
            for variant in variants_of(args):
                async with chrome(drv, not args.headful) as ch:
                    rig = await ch.rig(variant)
                    page = await rig.ctx.new_page()
                    base = srv.url(host)
                    errs = []
                    for path in ("/pre", "/set"):
                        try:
                            await page.goto(f"{base}{path}", timeout=args.timeout * 1000)
                        except Exception as exc:
                            errs.append(f"{path}: {str(exc).splitlines()[0]}")
                    log.clear()
                    echo = ""
                    try:
                        await page.goto(f"{base}/echo", timeout=args.timeout * 1000)
                        echo = await page.evaluate("document.body.innerText")
                    except Exception as exc:
                        errs.append(f"/echo: {str(exc).splitlines()[0]}")
                    sent = cookie_names(log[-1]) if log else []
                    jar = sorted(
                        [
                            c["name"],
                            c["domain"],
                            c["path"],
                            len(c["value"]),
                            c["secure"],
                            c["httpOnly"],
                            c["sameSite"],
                            c["expires"] > 0,
                        ]
                        for c in await rig.ctx.cookies()
                    )
                    rows.append(
                        {
                            "variant": variant,
                            "scenario": host,
                            "summary": (
                                f"jar={[f'{j[0]}@{j[1]}{j[2]}' for j in jar]} cookie_header_sent="
                                f"{sent} errors={errs} fetch_errors="
                                f"{[e['error'][:90] for e in rig.fetch_errors]}"
                            ),
                            "cmp": {"jar": jar, "cookie_sent": sent, "nav_errors": errs},
                            "data": {
                                "jar": jar,
                                "echo_len": len(echo),
                                "fetch_errors": rig.fetch_errors,
                            },
                        }
                    )
    finally:
        srv.close()
    finish("cookies", drv, args, rows, note="; ".join(notes))


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


def make_cert(directory: Path) -> tuple[str, str]:
    key, crt = directory / "key.pem", directory / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout", str(key), "-out", str(crt),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return str(crt), str(key)


async def exp_errors(args: argparse.Namespace, drv: Driver) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="redir-cert-"))
    tls = Server(lambda h: h.reply(200, "<title>tls</title>"), "T", tls=make_cert(tmp))
    hang = HangServer()
    closed_port = free_port()
    cases = {
        "unresolvable host": ("http://nonexistent-host-12345.invalid/", 20000),
        "closed port": (f"http://127.0.0.1:{closed_port}/", 20000),
        "self-signed https": (f"https://localhost:{tls.port}/", 20000),
        "never answers (5 s)": (f"http://127.0.0.1:{hang.port}/", 5000),
    }
    rows: list[dict] = []
    try:
        for variant in variants_of(args, "native,relay,relay-fallback"):
            async with chrome(drv, not args.headful) as ch:
                rig = await ch.rig(variant, ignore_https_errors=False)
                for scenario, (url, timeout) in cases.items():
                    page = await rig.ctx.new_page()
                    before = len(rig.fetch_errors)
                    t0 = time.monotonic()
                    error = None
                    try:
                        await page.goto(url, timeout=timeout)
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                    elapsed = time.monotonic() - t0
                    await rig.idle(40)
                    fetch_errors = [e["error"] for e in rig.fetch_errors[before:]]
                    first = error.splitlines()[0] if error else None
                    rows.append(
                        {
                            "variant": variant,
                            "scenario": scenario,
                            "summary": (
                                f"goto: {first} | page.url={page.url} | fetch raised: "
                                f"{[f.splitlines()[0] for f in fetch_errors]} | goto took "
                                f"{round(elapsed, 2)} s"
                            ),
                            "cmp": {"goto": first, "page_url": page.url},
                            "data": {
                                "goto_error_full": error,
                                "fetch_errors_full": fetch_errors,
                                "elapsed_s": round(elapsed, 2),
                                "page_url": page.url,
                            },
                        }
                    )
                    await page.close()
    finally:
        tls.close()
        hang.close()
        shutil.rmtree(tmp, ignore_errors=True)
    finish("errors", drv, args, rows)


# --------------------------------------------------------------------------
# multivalue
# --------------------------------------------------------------------------


def multivalue_app(h: _Handler) -> None:
    if h.route() == "/mv":
        h.reply(
            200,
            "<title>mv</title>mv",
            headers=[
                ("Set-Cookie", "a=1; Path=/"),
                ("Set-Cookie", "b=2; Path=/; HttpOnly"),
                ("Set-Cookie", "c=3; Path=/; SameSite=Lax"),
                ("Link", '<https://example.invalid/one>; rel="x-one"'),
                ("Link", '<https://example.invalid/two>; rel="x-two"'),
                ("Vary", "Accept-Encoding"),
                ("X-Dup", "first"),
                ("X-Dup", "second"),
            ],
        )
    else:
        h.reply(404, "nf")


async def exp_multivalue(args: argparse.Namespace, drv: Driver) -> None:
    log: list[dict] = []
    srv = Server(multivalue_app, "S", log)
    rows: list[dict] = []
    try:
        for variant in variants_of(args):
            async with chrome(drv, not args.headful) as ch:
                rig = await ch.rig(variant)
                page = await rig.ctx.new_page()
                resp = await page.goto(f"{srv.url()}/mv", timeout=args.timeout * 1000)
                array = [[h["name"], h["value"]] for h in await resp.headers_array()]
                array = [h for h in array if h[0].lower() != "date"]
                single = {k: v for k, v in (await resp.all_headers()).items() if k != "date"}
                jar = sorted(c["name"] for c in await rig.ctx.cookies())
                log.clear()
                await page.goto(f"{srv.url()}/mv?second", timeout=args.timeout * 1000)
                second = [v for k, v in log[-1]["headers"] if k.lower() == "cookie"] if log else []
                rows.append(
                    {
                        "variant": variant,
                        "scenario": "3x Set-Cookie, 2x Link, 1x Vary",
                        "summary": (
                            f"headers_array={array} jar={jar} cookie sent on 2nd visit={second}"
                        ),
                        "cmp": {
                            "headers_array": array,
                            "all_headers": single,
                            "jar": jar,
                            "cookie_second_visit": second,
                        },
                        "data": {"headers_array": array, "all_headers": single},
                    }
                )
    finally:
        srv.close()
    finish("multivalue", drv, args, rows)


# --------------------------------------------------------------------------
# tls
# --------------------------------------------------------------------------


def tls_summary(echo: dict) -> dict:
    tls = echo.get("tls") or {}
    h2 = echo.get("http2") or {}
    order: list[str] = []
    for frame in h2.get("sent_frames") or []:
        if frame.get("frame_type") == "HEADERS":
            order = [
                str(x).split(":", 1)[0] or ":" + str(x).split(":")[1]
                for x in frame.get("headers", [])
            ]
            break
    return {
        "http_version": echo.get("http_version"),
        "ja3_hash": tls.get("ja3_hash"),
        "ja4": tls.get("ja4"),
        "peetprint_hash": tls.get("peetprint_hash"),
        "akamai_hash": h2.get("akamai_fingerprint_hash"),
        "h2_header_order": order,
        "user_agent": (echo.get("user_agent") or "")[:80],
    }


async def exp_tls(args: argparse.Namespace, drv: Driver) -> None:
    rows: list[dict] = []
    for n, variant in enumerate(variants_of(args)):
        if n:
            await asyncio.sleep(args.gap)
        async with chrome(drv, not args.headful) as ch:
            rig = await ch.rig(variant)
            page = await rig.ctx.new_page()
            await page.goto(TLS_ECHO, wait_until="domcontentloaded", timeout=args.timeout * 1000)
            echo = json.loads(await page.inner_text("body"))
            summary = tls_summary(echo)
            echo.pop("ip", None)
            rows.append(
                {
                    "variant": variant,
                    "scenario": "tls.peet.ws",
                    "summary": " ".join(
                        f"{k}={v}" for k, v in summary.items() if k != "user_agent"
                    ),
                    "cmp": summary,
                    "data": echo,
                }
            )
    finish("tls", drv, args, rows)


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def cmd_report(args: argparse.Namespace) -> None:
    out_dir: Path = args.out_dir
    verdicts_path = out_dir / "verdicts.json"
    verdicts = json.loads(verdicts_path.read_text()) if verdicts_path.exists() else {}
    envs = [json.loads(p.read_text()) for p in sorted(out_dir.glob("*-*-*.json"))]
    envs = [e for e in envs if "experiment" in e and "rows" in e]
    order = [*EXPERIMENTS, "tls"]
    envs.sort(
        key=lambda e: (order.index(e["experiment"]), e["driver"], e["version"], e["headless"])
    )
    lines = ["# redirect-hops wire cost: native vs relay", ""]
    if verdicts:
        lines += ["## Verdicts", "", "| experiment | verdict |", "|---|---|"]
        lines += [f"| {k} | {cell(v)} |" for k, v in verdicts.items()]
        lines.append("")
    for experiment in order:
        group = [e for e in envs if e["experiment"] == experiment]
        if not group:
            continue
        lines += [f"## {experiment}", ""]
        if experiment in verdicts:
            lines += [f"**{cell(verdicts[experiment])}**", ""]
        for env in group:
            if env.get("note"):
                lines += [f"_{env['driver']} {env['version']}: {cell(env['note'])}_", ""]
        lines += [
            "| driver | mode | scenario | variant | measured | vs native |",
            "|---|---|---|---|---|---|",
        ]
        for env in group:
            who = f"{env['driver']} {env['version']}"
            mode = "headless" if env["headless"] else "headful"
            for row in env["rows"]:
                vs = row.get("vs_native")
                if vs is None:
                    verdict = "(reference)" if row["variant"] == "native" else ""
                elif vs["equal"]:
                    verdict = "equal"
                else:
                    verdict = "differs: " + "; ".join(clip(d, 260) for d in vs["diffs"])
                lines.append(
                    f"| {who} | {mode} | {cell(row.get('scenario', ''))} | {row['variant']} | "
                    f"{cell(clip(row['summary'], 900))} | {cell(verdict)} |"
                )
        lines.append("")
    md = Path(args.md) if args.md else out_dir / "results.md"
    md.write_text("\n".join(lines))
    print(f"wrote {md} ({len(envs)} result files)")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

RUNNERS: dict[str, Callable[[argparse.Namespace, Driver], object]] = {
    "hops": exp_hops,
    "stop": exp_stop,
    "headers": exp_headers,
    "stream": exp_stream,
    "download": exp_download,
    "encoding": exp_encoding,
    "cookies": exp_cookies,
    "errors": exp_errors,
    "multivalue": exp_multivalue,
    "tls": exp_tls,
}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--driver", choices=["playwright", "patchright"], default="playwright")
        p.add_argument("--variants", default="", help="comma list; default native,relay")
        p.add_argument("--headful", action="store_true", help="needs xvfb-run on a headless host")
        p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
        p.add_argument("--timeout", type=float, default=60.0, help="seconds per navigation")
        p.add_argument("--reps", type=int, default=0, help="repetitions (stop 5, stream 3)")
        p.add_argument("--delays", default="0,30,150,600", help="stop: hop server delays in ms")
        p.add_argument("--mb", default="300", help="download: body sizes in MB, comma list")
        p.add_argument("--cookie-hosts", default="sub.localhost,localhost")
        p.add_argument("--gap", type=float, default=6.0, help="tls: seconds between requests")
        return p

    for name in [*EXPERIMENTS, "tls", "all"]:
        common(sub.add_parser(name))
    rep = sub.add_parser("report")
    rep.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    rep.add_argument("--md", default="", help="markdown path; default <out-dir>/results.md")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "report":
        cmd_report(args)
        return 0
    drv = load_driver(args.driver)
    names = EXPERIMENTS if args.cmd == "all" else [args.cmd]
    for name in names:
        run_args = argparse.Namespace(**vars(args))
        if name == "stop" and not run_args.reps:
            run_args.reps = 5
        if name == "stream" and not run_args.reps:
            run_args.reps = 3
        asyncio.run(RUNNERS[name](run_args, drv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
