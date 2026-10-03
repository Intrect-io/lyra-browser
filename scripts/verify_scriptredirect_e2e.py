#!/usr/bin/env python3
"""A page that sends itself away while it is still loading must not strand the tool.

Found by ``verify_realsites.py``: neverssl.com runs ``location.href = '<random>.neverssl.com'``
in an inline script, before DOMContentLoaded. The guard refuses that with a 204, which
commits nothing, and ``goto`` went on waiting for the navigation that was gone until its own
timeout (30s) and then raised a bare TimeoutError - not the ``blocked_by_policy`` envelope the
model is told to expect. ``reload_page``, ``go_back`` and a form the page posts on its own
hung the same way. A redirect from a timer *after* the load was never a problem: the call had
returned by then.

This gate serves those shapes from two loopback origins (``127.0.0.1`` and ``localhost``) and
checks, in a real browser, that each one:

- is answered ``blocked_by_policy`` within a few seconds - the driver's own timeout is set to
  6s here, so a regression shows as a slow call or a raised TimeoutError, never as a pass;
- names where the tab stands, and leaves it standing: the page is still readable;
- never reached the destination (the server that would have received it saw nothing);
- leaves the tab usable: the next navigation is not held up by the call that was let go;
- is recoverable the way the envelope says, by asking for the destination explicitly.

And that a slow load nobody refused anything on is left alone, and a redirect from a timer is
still ``ok`` with the refusal recorded afterwards.

    .venv/bin/python scripts/verify_scriptredirect_e2e.py
    xvfb-run -a .venv/bin/python scripts/verify_scriptredirect_e2e.py --headful-only
    <venv with patchright>/bin/python scripts/verify_scriptredirect_e2e.py --driver patchright
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

DRIVER_TIMEOUT_MS = 6000  # what a hung call used to cost, shortened from the default 30s
ANSWER_WITHIN_S = 3.0  # refusal grace (1s) plus polling, with room for a loaded machine
SLOW_SECONDS = 2.5  # slower than the grace, so abandoning on time alone would show

# FAR is the other origin: a navigation to it is leaving, so it is refused.
SYNC = """<!doctype html><title>ENTRY</title><h1>entry page</h1>
<script>location.href = "http://localhost:FAR/final";</script>"""
LATE = """<!doctype html><title>LATE</title><h1>late page</h1>
<script>setTimeout(function () { location.href = "http://localhost:FAR/final"; },
  150);</script>"""
POST = """<!doctype html><title>POST</title><h1>post page</h1>
<form id="f" method="post" action="http://localhost:FAR/final"><input name="a" value="1"></form>
<script>document.getElementById("f").submit();</script>"""
PLAIN = "<!doctype html><title>{title}</title><h1>{title}</h1>"


class Near(BaseHTTPRequestHandler):
    far_port = 0

    def _send(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        pages = {"/sync": SYNC, "/late": LATE, "/post": POST}
        if path in pages:
            self._send(pages[path].replace("FAR", str(self.far_port)))
        elif path == "/slow":
            time.sleep(SLOW_SECONDS)
            self._send(PLAIN.format(title="SLOW"))
        elif path == "/ok":
            self._send(PLAIN.format(title="OK"))
        else:
            self.send_error(404)

    def log_message(self, *_args: object) -> None:
        pass


class Far(Near):
    """The destination the pages try to reach. Whatever arrives here is recorded."""

    seen: list[str] = []  # shared with the checks on purpose

    def _record(self) -> None:
        Far.seen.append(f"{self.command} {self.path}")

    def do_GET(self) -> None:  # noqa: N802
        self._record()
        self._send(PLAIN.format(title="FINAL"))

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._record()
        self._send(PLAIN.format(title="FINAL"))


async def timed(awaitable) -> tuple[dict, float]:
    started = time.monotonic()
    result = await awaitable
    return result, time.monotonic() - started


def audit_rows(ctx) -> list[dict]:
    path = ctx.config.audit_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def refused(ctx, capability: str) -> list[dict]:
    """Navigation rows the guard turned away for ``capability``, as the trail has them."""
    return [
        row
        for row in audit_rows(ctx)
        if row["tool"] == "navigation"
        and row["status"] == "denied"
        and row["args"].get("capability") == capability
    ]


async def run_mode(base: str, *, headless: bool, driver: str) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-redirect-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless)
    ctx.config.driver = driver
    far = Near.far_port
    print(f"\n[{mode}] driver={driver} data_dir={tmp.name}")
    try:
        opened = await tools["open_browser"]()
        expect(opened.get("status") == "ok", "browser launches", opened)
        page = await ctx.session.page()
        page.context.set_default_timeout(DRIVER_TIMEOUT_MS)
        Far.seen.clear()

        # -- an inline redirect, before DOMContentLoaded: the case that hung ---------
        sync, took = await timed(tools["navigate"](url=base + "sync", reason="e2e", confirm=True))
        expect(sync.get("status") == "blocked_by_policy", "navigate answers the refusal", sync)
        expect(
            took < ANSWER_WITHIN_S,
            f"within {ANSWER_WITHIN_S:.0f}s, not the driver's "
            f"{DRIVER_TIMEOUT_MS // 1000}s ({took:.1f}s)",
        )
        expect(sync.get("url") == base + "sync", "and says where the tab stands", sync)
        where = await tools["get_url"]()
        text = await tools["read_page"]()
        expect(
            where["url"] == base + "sync" and "entry page" in text["text"],
            "the page is still standing and readable",
            (where, text),
        )
        expect(
            len(refused(ctx, "navigate")) == 1,
            "the trail holds the refusal",
            refused(ctx, "navigate"),
        )
        blocked = [r for r in audit_rows(ctx) if r["tool"] == "navigate"]
        expect(
            blocked and blocked[-1]["status"] == "blocked_by_policy",
            "and the navigate that met it",
            blocked,
        )
        expect(Far.seen == [], "the refused redirect never reached its destination", Far.seen)

        # -- the call that was let go does not hold the tab --------------------------
        ok, took = await timed(tools["navigate"](url=base + "ok", confirm=True))
        expect(
            ok.get("status") == "ok" and ok.get("title") == "OK", "the next navigation works", ok
        )
        expect(took < 2.0, f"and is not held up by the abandoned one ({took:.1f}s)")

        # -- go_back into it, and reload on it ---------------------------------------
        back, took = await timed(tools["go_back"](confirm=True))
        expect(back.get("status") == "blocked_by_policy", "go_back into it is answered", back)
        expect(took < ANSWER_WITHIN_S, f"within {ANSWER_WITHIN_S:.0f}s ({took:.1f}s)")
        again, took = await timed(tools["reload_page"](confirm=True))
        expect(again.get("status") == "blocked_by_policy", "reload_page on it is answered", again)
        expect(took < ANSWER_WITHIN_S, f"within {ANSWER_WITHIN_S:.0f}s ({took:.1f}s)")
        expect(Far.seen == [], "and nothing got through", Far.seen)

        # -- a form the page posts on its own ----------------------------------------
        post, took = await timed(tools["navigate"](url=base + "post", confirm=True))
        expect(
            post.get("status") == "blocked_by_policy", "a form posted during load is answered", post
        )
        expect(took < ANSWER_WITHIN_S, f"within {ANSWER_WITHIN_S:.0f}s ({took:.1f}s)")
        expect(len(refused(ctx, "submit")) == 1, "as a refused submission", refused(ctx, "submit"))
        expect(Far.seen == [], "that never left the browser", Far.seen)

        # -- the same redirect from a timer, after the load: never a problem ----------
        before = len(refused(ctx, "navigate"))
        late, took = await timed(tools["navigate"](url=base + "late", confirm=True))
        expect(
            late.get("status") == "ok" and late.get("title") == "LATE",
            "a late redirect is ok",
            late,
        )
        expect(took < 2.0, f"and immediate ({took:.1f}s)")
        await asyncio.sleep(1.0)
        expect(
            (await tools["get_url"]())["url"] == base + "late"
            and len(refused(ctx, "navigate")) == before + 1,
            "the tab stays and the refusal is recorded afterwards",
            refused(ctx, "navigate"),
        )

        # -- a slow load nobody refused anything on is left alone ---------------------
        slow, took = await timed(tools["navigate"](url=base + "slow", confirm=True))
        expect(
            slow.get("status") == "ok" and slow.get("title") == "SLOW", "a slow load is ok", slow
        )
        expect(took >= SLOW_SECONDS - 0.2, f"and was waited for, not cut short ({took:.1f}s)")

        # -- the refusal is recoverable, as the envelope says --------------------------
        # Last, because it buys the lease that would let the redirects above through.
        final, _ = await timed(
            tools["navigate"](url=f"http://localhost:{far}/final", reason="e2e", confirm=True)
        )
        expect(
            final.get("status") == "ok" and final.get("title") == "FINAL",
            "asking for the destination explicitly works",
            final,
        )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool, driver: str) -> None:
    near_port, far_port = free_port(), free_port()
    Near.far_port = far_port
    servers = []
    for port, handler in ((near_port, Near), (far_port, Far)):
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    base = f"http://127.0.0.1:{near_port}/"
    try:
        if not headful_only:
            await run_mode(base, headless=True, driver=driver)
        if not headless_only:
            await run_mode(base, headless=False, driver=driver)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    parser.add_argument("--driver", choices=["auto", "playwright", "patchright"], default="auto")
    args = parser.parse_args()
    asyncio.run(async_main(args.headless_only, args.headful_only, args.driver))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
