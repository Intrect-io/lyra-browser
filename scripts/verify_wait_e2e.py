#!/usr/bin/env python3
"""Exercise ``wait_for`` and ``navigate``'s response facts against a real browser.

Companion to ``verify_browser_e2e.py`` (same explicit-gate contract: temporary
profile, loopback site, not part of the unit suite). It reuses that script's
``build_tools`` so both drive the server the same way, and passes ``confirm=True``
only where a gate is expected — nothing here is masked by loosened permissions.

The fixture site draws its content *after* ``domcontentloaded``, answers with the
statuses and media types the tools now report, and serves a file download. The
assertions read what the tools return and what ``read_page`` then sees, not what
the DOM claims.

``--check-cap`` adds the one scenario that costs real time: a wait that is asked
to last far longer than allowed must come back after the 30 second ceiling.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

HOME = """<!doctype html><title>home</title><h1>Wait E2E</h1>
<a id="go" href="/slow">Slow page</a>"""

# Five items drawn `ms` after DOMContentLoaded: to read_page straight after
# navigate the list is empty, which is the whole problem.
LATE = """<!doctype html><title>late {ms}</title><h1>Late {ms}</h1><ul id="items"></ul>
<script>
document.addEventListener('DOMContentLoaded', () => setTimeout(() => {{
  const list = document.querySelector('#items');
  for (let n = 1; n <= 5; n++) {{
    const li = document.createElement('li');
    li.textContent = 'Item ' + n;
    list.append(li);
  }}
}}, {ms}));
</script>"""

SPINNER = """<!doctype html><title>spinner</title><h1>Spinner</h1>
<div id="spin">Loading...</div>
<div id="result" style="display:none">Result ready</div>
<script>
setTimeout(() => {
  document.querySelector('#spin').remove();
  document.querySelector('#result').style.display = 'block';
}, 400);
</script>"""

# A non-breaking space and a <br>: the text a person copies from read_page has
# ordinary spaces and a newline where the DOM has neither.
SPACES = (
    "<!doctype html><title>spaces</title><p>Total:&nbsp;<b>42</b></p><p>Line one<br>line two</p>"
)

# A page that forbids all script but its own inline one, and draws late text.
# The wait must still work: it runs from the driver, not from the page.
CSP = """<!doctype html><title>csp</title><h1>Strict CSP</h1>
<script>setTimeout(() => document.body.append('LATE-TEXT'), 300)</script>"""

# The smallest thing Chrome's viewer accepts; the response headers are what is under test.
PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 200 200]/Parent 2 0 R>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF"
)


class Handler(BaseHTTPRequestHandler):
    def _send(
        self,
        body: str | bytes,
        *,
        code: int = 200,
        ctype: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(HOME)
        elif path == "/late":
            ms = int(parse_qs(parsed.query).get("ms", ["300"])[0])
            self._send(LATE.format(ms=ms))
        elif path == "/status/404":
            self._send("<title>gone</title><h1>Not here</h1>", code=404)
        elif path == "/status/500":
            self._send("<title>boom</title><h1>Server broke</h1>", code=500)
        elif path == "/doc.pdf":
            self._send(PDF, ctype="application/pdf")
        elif path == "/attach":
            self._send(
                b"not a page",
                ctype="application/octet-stream",
                headers={"Content-Disposition": 'attachment; filename="report.bin"'},
            )
        elif path == "/slow":
            time.sleep(0.4)
            self._send("<title>slow</title><h1>Slow arrived</h1>")
        elif path == "/spinner":
            self._send(SPINNER)
        elif path == "/spaces":
            self._send(SPACES)
        elif path == "/csp":
            self._send(
                CSP,
                headers={
                    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'"
                },
            )
        else:
            self.send_error(404)

    def log_message(self, *_args: object) -> None:
        pass


def items_seen(read: dict) -> int:
    return len(re.findall(r"Item \d", read.get("text", "")))


async def timed(coro) -> tuple[dict, float]:
    """The result of a tool call and how long it really took, in milliseconds."""
    started = time.monotonic()
    result = await coro
    return result, (time.monotonic() - started) * 1000


async def run_mode(base: str, *, headless: bool, check_cap: bool) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-wait-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    ctx, tools = await build_tools(data_dir, headless=headless)
    print(f"\n[{mode}] data_dir={data_dir}")
    try:
        await tools["open_browser"]()

        # --- What the server answered ------------------------------------
        home = await tools["navigate"](url=base, reason="wait E2E", confirm=True)
        expect(home.get("status") == "ok", "first site navigates once approved", home)
        expect(home.get("http_status") == 200, "normal page reports http_status 200", home)
        expect(
            home.get("content_type") == "text/html",
            "text/html page reports content_type text/html (charset dropped)",
            home,
        )

        missing = await tools["navigate"](url=f"{base}status/404")
        expect(missing.get("status") == "ok", "a 404 is still a navigation that happened", missing)
        expect(missing.get("http_status") == 404, "404 page reports http_status 404", missing)
        broken = await tools["navigate"](url=f"{base}status/500")
        expect(broken.get("http_status") == 500, "500 page reports http_status 500", broken)
        reloaded = await tools["reload_page"]()
        expect(reloaded.get("http_status") == 500, "reload_page reports the status too", reloaded)

        pdf = await tools["navigate"](url=f"{base}doc.pdf")
        expect(
            pdf.get("content_type") == "application/pdf",
            "PDF response reports content_type application/pdf",
            pdf,
        )
        expect(pdf.get("http_status") == 200, "PDF response reports http_status 200", pdf)

        back = await tools["go_back"]()
        expect(back.get("status") == "ok", "go_back answers", back)
        expect("http_status" in back and "content_type" in back, "go_back carries the facts", back)
        print(f"INFO  go_back reported http_status={back.get('http_status')!r}")

        await tools["navigate"](url=base)
        held = await tools["navigate"](url=f"{base}attach")
        # Since then an undeclared download is cancelled and reported as blocked;
        # download_started survives only when the driver announces one the browser
        # never reports.
        expect(
            held.get("status") in {"download_blocked", "download_started"},
            "a download is a result, not an error",
            held,
        )
        expect(held.get("url") == base, "the tab did not move for a download", held)
        expect((await tools["get_url"]()).get("url") == base, "the page is still usable")
        expect(
            not list(data_dir.rglob("report.bin")),
            "navigate saved nothing to the data dir",
            sorted(p.name for p in data_dir.rglob("report.*")),
        )

        blank = await tools["navigate"](url="about:blank", confirm=True)
        expect(
            blank.get("status") == "ok"
            and blank.get("http_status") is None
            and blank.get("content_type") is None,
            "about:blank has no HTTP response and says so",
            blank,
        )

        # --- A refused argument must not navigate ---------------------------
        await tools["navigate"](url=base, confirm=True)
        typo = await tools["navigate"](url=f"{base}status/404", wait_until="bogus")
        expect(typo.get("status") == "error", "an unknown wait_until is an error", typo)
        expect(
            (await tools["get_url"]()).get("url") == base,
            "and nothing navigated because of it",
        )
        loaded = await tools["navigate"](url=f"{base}late?ms=300", wait_until="load")
        expect(loaded.get("status") == "ok", "wait_until=load navigates", loaded)
        committed = await tools["navigate"](url=base, wait_until="commit")
        expect(committed.get("status") == "ok", "wait_until=commit navigates", committed)

        # --- Content drawn after the page loaded ----------------------------
        for ms in (300, 1500):
            nav = await tools["navigate"](url=f"{base}late?ms={ms}")
            expect(nav.get("status") == "ok", f"late-{ms}ms page loads", nav)
            straight_after = items_seen(await tools["read_page"]())
            print(f"INFO  read_page straight after navigate sees {straight_after}/5 items")
            waited, elapsed = await timed(tools["wait_for"](text="Item 5", timeout_ms=8000))
            expect(
                waited.get("status") == "ok", f"wait_for(text) waits out the {ms}ms render", waited
            )
            expect(
                items_seen(await tools["read_page"]()) == 5,
                f"after wait_for, read_page sees 5/5 items ({ms}ms render)",
            )
            if ms >= 1500:
                expect(
                    waited["waited_ms"] >= 500,
                    "the wait really waited (it did not return early)",
                    waited,
                )
            expect(waited["waited_ms"] <= elapsed + 50, "waited_ms is the real time", waited)

        idle = await tools["navigate"](url=f"{base}late?ms=300", wait_until="networkidle")
        expect(idle.get("status") == "ok", "wait_until=networkidle navigates", idle)
        expect(
            items_seen(await tools["read_page"]()) == 5,
            "networkidle lets a 300ms render finish before navigate returns",
        )

        # --- The other conditions ----------------------------------------------
        await tools["navigate"](url=f"{base}spinner")
        gone = await tools["wait_for"](text="Loading...", state="hidden", timeout_ms=5000)
        expect(gone.get("status") == "ok", "text + state=hidden waits for a phrase to go", gone)
        shown = await tools["wait_for"](text="Result ready", timeout_ms=5000)
        expect(shown.get("status") == "ok", "the phrase that replaced it is there", shown)
        await tools["navigate"](url=f"{base}spinner")
        vanished = await tools["wait_for"](selector="#spin", state="hidden", timeout_ms=5000)
        expect(
            vanished.get("status") == "ok", "selector + state=hidden waits for removal", vanished
        )
        appeared = await tools["wait_for"](selector="#result", timeout_ms=5000)
        expect(appeared.get("status") == "ok", "selector waits for visibility by default", appeared)
        await tools["navigate"](url=f"{base}spinner")
        detached = await tools["wait_for"](selector="#spin", state="detached", timeout_ms=5000)
        expect(detached.get("status") == "ok", "selector + state=detached", detached)

        await tools["navigate"](url=f"{base}spaces")
        spaced = await tools["wait_for"](text="Total: 42", timeout_ms=3000)
        expect(spaced.get("status") == "ok", "a non-breaking space matches a plain one", spaced)
        lined = await tools["wait_for"](text="Line one line two", timeout_ms=3000)
        expect(lined.get("status") == "ok", "a line break matches a plain space", lined)
        cased = await tools["wait_for"](text="total: 42", timeout_ms=500)
        expect(cased.get("status") == "timeout", "text is matched case-sensitively", cased)

        await tools["navigate"](url=f"{base}csp")
        strict = await tools["wait_for"](text="LATE-TEXT", timeout_ms=5000)
        expect(strict.get("status") == "ok", "wait_for works on a page with a strict CSP", strict)

        # A wait that starts while the page is still on its way somewhere else.
        page = await ctx.session.page()
        await tools["navigate"](url=base)
        await page.evaluate("setTimeout(() => { location.href = '/slow' }, 100)")
        crossed = await tools["wait_for"](text="Slow arrived", timeout_ms=8000)
        expect(crossed.get("status") == "ok", "text wait survives a navigation mid-wait", crossed)
        await tools["navigate"](url=base)
        await page.evaluate("setTimeout(() => { location.href = '/slow' }, 100)")
        arrived = await tools["wait_for"](url=f"{base}slow", timeout_ms=8000)
        expect(arrived.get("status") == "ok", "url wait sees the tab arrive", arrived)
        expect((await tools["get_url"]()).get("url") == f"{base}slow", "and it is where it says")
        globbed = await tools["wait_for"](url="**/slow", timeout_ms=2000)
        expect(globbed.get("status") == "ok", "a glob matches the whole URL", globbed)

        for state in ("domcontentloaded", "load", "networkidle"):
            reached = await tools["wait_for"](load_state=state, timeout_ms=8000)
            expect(reached.get("status") == "ok", f"load_state={state}", reached)

        # --- Timeouts come back, quickly, with something to look at -------------
        await tools["navigate"](url=base)
        result, elapsed = await timed(
            tools["wait_for"](text="text that is nowhere", timeout_ms=1200)
        )
        expect(result.get("status") == "timeout", "an unmet text wait times out", result)
        expect(elapsed < 1200 + 2000, "and returns inside timeout_ms + 2s", (elapsed, result))
        expect(result.get("waited_ms", 0) >= 1100, "having waited it out", result)
        expect("Wait E2E" in result.get("last_seen", ""), "last_seen shows the page text", result)

        result, elapsed = await timed(tools["wait_for"](selector="#never-here", timeout_ms=1000))
        expect(result.get("status") == "timeout", "a selector that never appears times out", result)
        expect(elapsed < 1000 + 2000, "inside timeout_ms + 2s", (elapsed, result))

        result, elapsed = await timed(tools["wait_for"](url="**/never**", timeout_ms=800))
        expect(result.get("status") == "timeout", "an unmet url wait times out", result)
        expect(result.get("last_seen") == base, "and last_seen is the URL", result)
        expect(elapsed < 800 + 2000, "inside timeout_ms + 2s", (elapsed, result))

        result, elapsed = await timed(tools["wait_for"](selector="#never-here", state="detached"))
        expect(result.get("status") == "ok" and elapsed < 1500, "detached is met at once", result)

        result, elapsed = await timed(tools["wait_for"](text="Wait E2E", timeout_ms=10**9))
        expect(
            result.get("status") == "ok" and elapsed < 2000, "a huge timeout is accepted", result
        )

        for bad in (
            {},
            {"url": "dashboard"},
            {"text": "x", "timeout_ms": 0},
            {"load_state": "idle"},
        ):
            result, elapsed = await timed(tools["wait_for"](**bad))
            expect(
                result.get("status") == "error" and elapsed < 500,
                f"{bad} is refused at once",
                result,
            )
        broken_selector = await tools["wait_for"](selector="div[", timeout_ms=1000)
        expect(
            broken_selector.get("status") == "error" and broken_selector.get("reason"),
            "a selector the engine cannot parse is an error with a reason",
            broken_selector,
        )

        # --- It only reads --------------------------------------------------------
        ctx.collab.takeover = True
        try:
            during = await tools["wait_for"](text="Wait E2E", timeout_ms=2000)
            refused = await tools["click"](selector="#go")
        finally:
            ctx.collab.takeover = False
        expect(during.get("status") == "ok", "wait_for works during a takeover", during)
        expect(refused.get("status") == "takeover_active", "while a click is refused", refused)

        if check_cap:
            print("INFO  --check-cap: waiting out the 30s ceiling ...")
            capped, elapsed = await timed(
                tools["wait_for"](selector="#never-here", timeout_ms=10**9)
            )
            expect(
                capped.get("status") == "timeout", "an absurd timeout_ms still times out", capped
            )
            expect(
                29_000 <= capped["waited_ms"] <= 32_000 and elapsed < 34_000,
                "at the 30s ceiling",
                (capped, elapsed),
            )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool, check_cap: bool) -> None:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    try:
        if not headful_only:
            await run_mode(base, headless=True, check_cap=check_cap)
        if not headless_only:
            await run_mode(base, headless=False, check_cap=check_cap)
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    parser.add_argument("--check-cap", action="store_true", help="also wait out the 30s ceiling")
    args = parser.parse_args()
    asyncio.run(async_main(args.headless_only, args.headful_only, args.check_cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
