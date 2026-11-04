#!/usr/bin/env python3
"""Exercise the DevTools tools against a real installed browser, on both drivers.

Companion to ``verify_browser_e2e.py`` (same explicit-gate contract: temporary profile,
loopback site, not part of the unit suite; it reuses that script's ``build_tools``). One
run covers the native driver and Playwright, headless by default, and fails unless every
step passes on both.

What is proved, per driver:

- the console stays off until asked for, then reports ``console.error`` and an uncaught
  exception with its source;
- the network log has the document, a slow API call whose ``duration_ms`` matches the
  delay the server put in (browser clock, not Python's), a 404 and a refused connection;
- headers come back with credentials redacted; a body needs ``INSPECT`` and then arrives;
- ``page_performance`` reports navigation timing and paints;
- ``storage`` lists cookie and localStorage names, values only with ``INSPECT``;
- ``export_har`` writes a HAR with no credential in it, bodies only with ``INSPECT``;
- ``evaluate`` asks every time, runs in the page's own world, awaits a Promise, reports a
  thrown exception, and a ``fetch()`` POST it makes reaches the server (which is why it
  is gated).
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

API_DELAY_S = 0.3
COOKIE_VALUE = "s3cr3t-cookie-value"
CLOSED_PORT = free_port()  # bound and released: nothing listens there

HOME = f"""<!doctype html><title>devtools</title><h1>DevTools E2E</h1>
<p id="state">loading</p>
<script>
window.appState = {{ ready: true, answer: 42 }};
localStorage.setItem('theme', 'dark-{COOKIE_VALUE}');
console.log('demo log line');
console.error('demo console error');
fetch('/api?token=abc123').then((r) => r.json()).then((d) => {{
  document.getElementById('state').textContent = 'api:' + d.items;
}});
fetch('/missing');
fetch('http://127.0.0.1:{CLOSED_PORT}/refused').catch(() => {{}});
setTimeout(() => {{ throw new Error('kaboom from the page'); }}, 50);
</script>"""

POSTS: list[str] = []


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, ctype: str, extra: dict | None = None) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(
                200,
                HOME,
                "text/html; charset=utf-8",
                {"Set-Cookie": f"sid={COOKIE_VALUE}; Path=/"},
            )
        elif path == "/api":
            time.sleep(API_DELAY_S)
            self._send(
                200,
                json.dumps({"items": 3}),
                "application/json",
                {"X-Request-Id": "req-77", "X-Api-Key": "leaky-key"},
            )
        else:
            self._send(404, "not here", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        POSTS.append(raw.decode("utf-8", "replace"))
        self._send(200, "{}", "application/json")

    def log_message(self, *_args: object) -> None:
        pass


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        # A reload aborts the fixture's pending fetches mid-response: a broken pipe here
        # is the browser leaving, not a failure of the gate.
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


async def until(check, what: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = await check()
        if found:
            return found
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def run_driver(base: str, driver: str, *, headless: bool) -> None:
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-devtools-{driver}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    ctx, tools = await build_tools(data_dir, headless=headless, driver=driver)
    print(f"\n[{driver} {'headless' if headless else 'headful'}] data_dir={data_dir}")
    POSTS.clear()
    try:
        opened = await tools["navigate"](url=base, confirm=True)
        expect(opened.get("status") == "ok", f"{driver}: navigate to the fixture", opened)
        expect(
            ctx.session.active_driver == driver,
            f"{driver}: the driver asked for runs",
            ctx.session.active_driver,
        )

        # --- console ---------------------------------------------------------------
        off = await tools["console_messages"]()
        expect(off.get("status") == "console_off", f"{driver}: console is off by default", off)
        on = await tools["console_messages"](capture=True)
        expect(on.get("capture") is True, f"{driver}: console capture turns on", on)
        reloaded = await tools["reload_page"]()
        expect(reloaded.get("status") == "ok", f"{driver}: reload with console on", reloaded)

        async def console_complete():
            got = await tools["console_messages"]()
            levels = {e["level"] for e in got["entries"]}
            return got if {"error", "exception"} <= levels else None

        got = await until(console_complete, "console.error and the uncaught exception")
        errors = [e for e in got["entries"] if e["level"] == "error"]
        thrown = [e for e in got["entries"] if e["level"] == "exception"]
        expect(
            any("demo console error" in e["text"] for e in errors),
            f"{driver}: console.error is reported",
            errors,
        )
        expect(
            any("kaboom from the page" in e["text"] for e in thrown),
            f"{driver}: the uncaught exception is reported",
            thrown,
        )
        logs = await tools["console_messages"](levels=["log"])
        expect(
            any("demo log line" in e["text"] for e in logs["entries"]),
            f"{driver}: console.log is reported and the level filter works",
            logs,
        )

        # --- network ---------------------------------------------------------------
        async def api_finished():
            rows = (await tools["network_requests"](url_contains="/api"))["entries"]
            done = [r for r in rows if r["state"] == "finished"]
            return done[-1] if done else None

        api = await until(api_finished, "the /api request to finish")
        expect(
            api["status"] == 200 and "abc123" not in api["url"],
            f"{driver}: the API call is listed without its query values",
            api,
        )
        expect(
            api["duration_ms"] is not None and api["duration_ms"] >= API_DELAY_S * 1000 * 0.9,
            f"{driver}: duration_ms reflects the server's {API_DELAY_S * 1000:.0f} ms delay",
            api,
        )
        expect(
            api.get("ttfb_ms") is not None and api["ttfb_ms"] <= api["duration_ms"],
            f"{driver}: ttfb_ms is measured and not after the last byte",
            api,
        )
        docs = (await tools["network_requests"](resource_type="document"))["entries"]
        expect(
            any(d["status"] == 200 and d["duration_ms"] is not None for d in docs),
            f"{driver}: the document is listed with a duration",
            docs,
        )
        errors_4xx = (await tools["network_requests"](min_status=400))["entries"]
        expect(
            any(r["status"] == 404 and r["url"].endswith("/missing") for r in errors_4xx),
            f"{driver}: the 404 is listed by min_status",
            errors_4xx,
        )

        async def refused_failed():
            rows = (await tools["network_requests"](failed_only=True))["entries"]
            return [r for r in rows if "/refused" in r["url"]] or None

        failed = await until(refused_failed, "the refused connection to fail")
        expect(bool(failed[0].get("failure")), f"{driver}: a refused connection is failed", failed)

        detail = await tools["network_request"](id=api["id"])
        expect(
            detail["response_headers"].get("x-request-id") == "req-77"
            and detail["response_headers"].get("x-api-key") == "<redacted>",
            f"{driver}: response headers are shown with credentials redacted",
            detail.get("response_headers"),
        )
        expect("body" not in detail, f"{driver}: no body without asking", detail)
        refused = await tools["network_request"](id=api["id"], body=True)
        expect(
            refused.get("status") == "needs_approval",
            f"{driver}: a body needs INSPECT",
            refused,
        )
        body = await tools["network_request"](id=api["id"], body=True, confirm=True)
        expect(
            body.get("body") == '{"items": 3}' and "abc123" in body.get("url", ""),
            f"{driver}: with INSPECT the body and full URL arrive",
            body,
        )

        # --- performance -----------------------------------------------------------
        perf = await tools["page_performance"]()
        nav = perf.get("navigation") or {}
        expect(
            perf.get("status") == "ok" and nav.get("ttfb_ms") is not None,
            f"{driver}: navigation timing is reported",
            perf,
        )
        expect(
            (perf.get("vitals") or {}).get("fcp_ms") is not None,
            f"{driver}: first contentful paint is reported",
            perf.get("vitals"),
        )
        print(f"      vitals={perf.get('vitals')} navigation={nav}")

        # --- storage ---------------------------------------------------------------
        cookies = await tools["storage"](kind="cookies")
        sid = [c for c in cookies.get("items", []) if c["name"] == "sid"]
        expect(
            bool(sid) and "value" not in sid[0],
            f"{driver}: cookies are listed by name only",
            cookies,
        )
        local = await tools["storage"](kind="local")
        expect(
            any(i["name"] == "theme" and "value" not in i for i in local.get("items", [])),
            f"{driver}: localStorage keys are listed without values",
            local,
        )
        refused = await tools["storage"](kind="cookies", reveal=True)
        expect(refused.get("status") == "needs_approval", f"{driver}: values need INSPECT", refused)
        revealed = await tools["storage"](kind="cookies", reveal=True, confirm=True)
        expect(
            any(c.get("value") == COOKIE_VALUE for c in revealed.get("items", [])),
            f"{driver}: with INSPECT the cookie value arrives",
            revealed,
        )

        # --- HAR -------------------------------------------------------------------
        har = await tools["export_har"]()
        text = Path(har["har_path"]).read_text(encoding="utf-8")
        parsed = json.loads(text)
        expect(
            parsed["log"]["version"] == "1.2" and har["entries"] >= 4,
            f"{driver}: a HAR is written with the log",
            har,
        )
        expect(
            COOKIE_VALUE not in text and "leaky-key" not in text and "abc123" not in text,
            f"{driver}: the HAR carries no cookie, key or query value",
        )
        leased = await tools["export_har"](include_bodies=True)
        expect(
            leased.get("status") == "ok",
            f"{driver}: the INSPECT lease storage bought covers HAR bodies on the same site",
            leased,
        )
        # Forget every grant: what is left to prove is that bodies are asked for at all.
        ctx.perms.revoke_all()
        refused = await tools["export_har"](include_bodies=True)
        expect(
            refused.get("status") == "needs_approval",
            f"{driver}: without a grant HAR bodies need INSPECT",
            refused,
        )
        full = await tools["export_har"](include_bodies=True, confirm=True)
        full_text = Path(full["har_path"]).read_text(encoding="utf-8")
        expect(
            full["bodies"] >= 1 and '{\\"items\\": 3}' in full_text,
            f"{driver}: with INSPECT the HAR has bodies",
            full,
        )

        # --- evaluate --------------------------------------------------------------
        refused = await tools["evaluate"](expression="document.title")
        expect(refused.get("status") == "needs_approval", f"{driver}: evaluate asks", refused)
        title = await tools["evaluate"](expression="document.title", confirm=True)
        expect(
            json.loads(title.get("result", "null")) == "devtools",
            f"{driver}: evaluate returns a value",
            title,
        )
        again = await tools["evaluate"](expression="document.title")
        expect(again.get("status") == "needs_approval", f"{driver}: EVALUATE is never leased")
        main = await tools["evaluate"](expression="window.appState.answer", confirm=True)
        expect(
            main.get("result") == "42",
            f"{driver}: evaluate runs in the page's own world",
            main,
        )
        awaited = await tools["evaluate"](
            expression="new Promise((r) => setTimeout(() => r({ok: 1}), 20))", confirm=True
        )
        expect(
            json.loads(awaited.get("result", "null")) == {"ok": 1},
            f"{driver}: a Promise is awaited",
            awaited,
        )
        thrown = await tools["evaluate"](
            expression="(() => { throw new Error('nope from eval'); })()", confirm=True
        )
        expect(
            thrown.get("status") == "exception" and "nope from eval" in thrown.get("error", ""),
            f"{driver}: a thrown exception is reported",
            thrown,
        )
        posted = await tools["evaluate"](
            expression="fetch('/post', {method: 'POST', body: 'from-eval'}).then((r) => r.status)",
            confirm=True,
        )
        expect(
            posted.get("result") == "200" and POSTS == ["from-eval"],
            f"{driver}: an approved fetch() POST reaches the site (why EVALUATE is gated)",
            (posted, POSTS),
        )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(drivers: list[str], headless: bool) -> None:
    port = free_port()
    server = QuietServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    try:
        for driver in drivers:
            await run_driver(base, driver, headless=headless)
    finally:
        server.shutdown()
        server.server_close()
    print(f"\nALL PASS  drivers={drivers} headless={headless}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--drivers", default="native,playwright")
    parser.add_argument("--headful", action="store_true", help="open a window (xvfb-run -a)")
    args = parser.parse_args()
    drivers = [d.strip() for d in args.drivers.split(",") if d.strip()]
    asyncio.run(async_main(drivers, headless=not args.headful))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
