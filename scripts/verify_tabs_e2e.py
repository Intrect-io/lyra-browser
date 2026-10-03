#!/usr/bin/env python3
"""Exercise popup recovery and the ``tabs`` tool against a real installed browser.

Companion to ``verify_browser_e2e.py`` (same explicit-gate contract: temporary
profile, loopback site, not part of the unit suite). It reuses that script's
``build_tools``, so the server is driven exactly as the other gates drive it,
with Config defaults: enforcement on, approval on. Where a gate is expected the
call says so with ``confirm=True``.

One thing is set up by hand. A popup's first request comes from a frame that
does not exist yet, so the guard cannot name who sent it (the audit trail says
``initiator: (unknown)``) and lets it through only when a NAVIGATE grant with no
initiator covers the destination. No tool buys such a grant, so ``allow_popups``
plays the operator who has approved the site — and the script first shows the
same popup being refused without it, so the guard is seen doing its job rather
than being switched off.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
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

from lyra_browser.origin import parse_origin  # noqa: E402
from lyra_browser.permission import Capability  # noqa: E402

# The opener keeps its state in the page, so a reload would lose it: that is what
# "the opener comes back intact" is checked against.
OPENER = """<!doctype html><title>opener</title><h1>Opener</h1>
<script>
window.__state = {n: 41, messages: []};
window.addEventListener('message', (e) => {
  window.__state.messages.push(e.data);
  window.__state.n += 1;
});
</script>
<button id="oauth"
  onclick="window.open('/oauth/popup', 'oauth', 'width=420,height=520')">sign in</button>
<a id="blank" target="_blank" href="/second">second</a>
<button id="cross" onclick="window.open('__ALT__cross')">cross</button>
<button id="busy" onclick="window.open('__ALT__busy')">busy</button>"""

# What a sign-in window does: report back to whoever opened it, then close itself.
OAUTH_POPUP = """<!doctype html><title>oauth</title>
<script>window.opener.postMessage('oauth-done', '*'); window.close();</script>"""

# A tab whose script never gives the thread back: its title cannot be read. It is
# opened on the other site so it gets its own process — same-site tabs that share an
# opener share a main thread, and would hang the opener's title along with it.
BUSY = """<!doctype html><title>busy</title>
<script>
setTimeout(() => { const t = Date.now(); while (Date.now() - t < 7000) {} }, 200);
</script>"""


class Handler(BaseHTTPRequestHandler):
    alt_base = ""

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        pages = {
            "/": OPENER.replace("__ALT__", Handler.alt_base),
            "/oauth/popup": OAUTH_POPUP,
            "/second": "<!doctype html><title>second</title><h1>Second</h1>",
            "/cross": "<!doctype html><title>cross</title><h1>Cross</h1>",
            "/busy": BUSY,
        }
        if path not in pages:
            self.send_error(404)
            return
        raw = pages[path].encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: object) -> None:
        pass


def audit_rows(ctx) -> list[dict]:
    lines = ctx.config.audit_path.read_text().splitlines()
    return [json.loads(line) for line in lines if line]


def refusals_of(ctx, suffix: str) -> list[dict]:
    """Navigations the guard turned away whose URL ends with ``suffix``."""
    return [
        row
        for row in audit_rows(ctx)
        if row["tool"] == "navigation"
        and row["status"] == "denied"
        and row["args"].get("url", "").endswith(suffix)
    ]


async def wait_for(check, what: str, timeout: float = 8.0):
    """Poll ``check`` (plain or async) until it returns something truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if inspect.isawaitable(found):
            found = await found
        if found:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


async def run_lifecycle(base: str, *, headless: bool) -> None:
    """What happens when the last tab goes: by the agent's hand, and by the user's.

    A browser of its own, apart from ``run_mode``. A popup the guard refuses leaves
    a blank window behind that Chrome counts and Playwright does not list, and one
    such window keeps headful Chrome alive when its last listed tab closes — which
    would let these checks pass for the wrong reason.
    """
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-tabs-life-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless)
    print(f"\n[{mode}] lifecycle, data_dir={tmp.name}")
    try:
        await tools["open_browser"]()
        entered = await tools["navigate"](url=base, reason="tabs E2E", confirm=True)
        expect(entered.get("title") == "opener", "fixture page loads", entered)

        # --- the agent closes the last tab ------------------------------------
        browser = ctx.session._context
        last = await tools["tabs"](action="close", index=0)
        expect(
            last.get("status") == "ok" and last["url"] == "about:blank" and last["tab_count"] == 1,
            "closing the last tab leaves a blank one",
            last,
        )
        expect(
            ctx.session._context is browser,
            "and the browser stayed up while it did (headful Chrome quits with its last window)",
        )
        blank = await tools["get_url"]()
        expect(blank.get("url") == "about:blank", "the blank page is the active one", blank)
        again = await tools["navigate"](url=base, reason="tabs E2E", confirm=True)
        expect(again.get("title") == "opener", "and it is usable: navigation works from it", again)

        # --- the user closes the last window ----------------------------------
        page = await ctx.session.page()
        quit_browser = ctx.session._context
        await page.close()
        await asyncio.sleep(0.5)  # closed some time ago, not this very instant

        # Reading does not bring it back. The user closed that window, and a read is
        # no reason to put one on their screen again.
        for name in ("get_url", "read_page", "screenshot", "tabs"):
            closed = await tools[name]()
            expect(closed.get("status") == "browser_closed", f"{name} reports it closed", closed)
        expect("open_browser" in closed.get("hint", ""), "and says how to start a new one", closed)
        expect(
            ctx.session._context is quit_browser and not quit_browser.pages,
            "the reads opened no browser, no window and no tab",
            quit_browser.pages,
        )

        # A claiming call does: it is the agent asking for a browser.
        revived = await tools["open_browser"]()
        expect(revived.get("status") == "ok", "open_browser works after the window closed", revived)
        live = await ctx.session.page()
        expect(not live.is_closed(), "and the page it hands back is open")
        if not headless:
            expect(
                ctx.session._context is not quit_browser,
                "headful Chrome quit with its last window, so open_browser started a new browser",
            )
        expect((await tools["get_url"]()).get("url") == "about:blank", "reads work on it again")
        navigated = await tools["navigate"](url=base, reason="tabs E2E", confirm=True)
        expect(navigated.get("title") == "opener", "and it is a usable page", navigated)

        # --- the agent closes the browser on purpose --------------------------
        shut = await tools["close_browser"](reason="tabs E2E")
        expect(shut.get("status") == "ok", "close_browser closes the browser", shut)
        for name in ("get_url", "tabs"):
            closed = await tools[name]()
            expect(closed.get("status") == "browser_closed", f"{name} reports it closed", closed)
        expect(not ctx.session.started, "and no read started another one")
        again = await tools["open_browser"]()
        expect(again.get("status") == "ok", "open_browser starts one when it is asked to", again)
        expect((await tools["get_url"]()).get("url") == "about:blank", "reads work on it again")
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def run_mode(base: str, alt_base: str, *, headless: bool) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-tabs-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    ctx, tools = await build_tools(data_dir, headless=headless)
    print(f"\n[{mode}] data_dir={data_dir}")
    site, alt_site = parse_origin(base), parse_origin(alt_base)

    def allow_popups(*sites) -> None:
        """The operator's approval for these sites to be opened from a page."""
        for origin in sites:
            ctx.perms.grant("default", origin, Capability.NAVIGATE)

    def tabs_open() -> int:
        return ctx.session.tab_info()["tab_count"]

    try:
        await tools["open_browser"]()
        entered = await tools["navigate"](url=base, reason="tabs E2E", confirm=True)
        expect(entered.get("title") == "opener", "fixture page loads", entered)
        opener = await ctx.session.page()
        opened_pages: list = []
        ctx.session._context.on("page", lambda p: opened_pages.append(p))

        async def messages():
            return await opener.evaluate("window.__state.messages")

        listed = await tools["tabs"]()
        expect(
            listed.get("status") == "ok"
            and listed["tab_count"] == 1
            and listed["tabs"][0]["active"]
            and listed["tabs"][0]["url"] == base,
            "one tab is listed, and it is the active one",
            listed,
        )

        # --- the guard still judges a popup, exactly as before ----------------
        await tools["click"](selector="#oauth")
        refused = await wait_for(lambda: refusals_of(ctx, "/oauth/popup"), "the guard's verdict")
        expect(bool(refused), "a popup's first navigation is judged by the guard", refused)
        expect(not opened_pages and tabs_open() == 1, "an unapproved popup opens no tab")
        expect((await tools["get_url"]())["url"] == base, "the session is still on the opener")
        allow_popups(site)

        # --- a sign-in popup that closes itself -------------------------------
        await tools["click"](selector="#oauth")
        told = await wait_for(messages, "the popup to report back")
        expect(told == ["oauth-done"], "the approved popup loaded and reported back", told)
        judged = [
            row
            for row in audit_rows(ctx)
            if row["tool"] == "navigation"
            and row["status"] == "allowed"
            and row["args"].get("url", "").endswith("/oauth/popup")
        ]
        expect(bool(judged), "and the guard judged that request too, on the approval", judged)
        await wait_for(lambda: opened_pages and opened_pages[0].is_closed(), "the popup to close")
        expect(len(opened_pages) == 1, "it opened one tab, which has closed itself")
        seen = await tools["get_url"]()
        expect(
            seen.get("url") == base and seen.get("title") == "opener",
            "get_url reports the opener after the popup closed",
            seen,
        )
        state = await opener.evaluate("window.__state")
        expect(
            state == {"n": 42, "messages": ["oauth-done"]},
            "the opener's script state survived the popup",
            state,
        )
        expect(tabs_open() == 1, "only the opener is left", ctx.session.tab_info())
        reopened = await tools["open_browser"]()
        expect(
            reopened.get("status") == "ok" and reopened.get("url") == base,
            "open_browser reports the live page, not the dead popup",
            reopened,
        )
        allow_popups(site)  # open_browser starts over, grants included

        # --- a link that opens a new tab --------------------------------------
        await tools["click"](selector="#blank")
        await wait_for(lambda: tabs_open() == 2, "the link's tab")
        listed = await tools["tabs"]()
        rows = listed["tabs"]
        expect(
            [t["url"] for t in rows] == [base, base + "second"]
            and [t["title"] for t in rows] == ["opener", "second"],
            "the new tab is listed, in opening order, with titles",
            rows,
        )
        expect(
            listed["active_index"] == 1 and rows[1]["active"] and not rows[0]["active"],
            "the session followed onto the new tab",
            listed,
        )
        expect((await tools["get_url"]())["title"] == "second", "reads now come from the new tab")

        switched = await tools["tabs"](action="switch", index=0)
        expect(
            switched.get("status") == "ok"
            and switched["url"] == base
            and switched["active_index"] == 0,
            "switch returns to the first tab",
            switched,
        )
        expect((await tools["get_url"]())["title"] == "opener", "reads follow the switch")

        # --- a popup to another site is still judged --------------------------
        await tools["click"](selector="#cross")
        await wait_for(lambda: refusals_of(ctx, "/cross"), "the guard's verdict on the cross popup")
        expect(tabs_open() == 2, "a popup to an unapproved site opens no tab")
        allow_popups(alt_site)
        await tools["click"](selector="#cross")
        await wait_for(lambda: tabs_open() == 3, "the cross-site tab")
        expect(
            (await tools["tabs"]())["active_index"] == 2,
            "an approved popup to another site is followed onto",
        )

        # --- switching and closing ask for the site of the tab they act on ----
        ctx.perms.revoke_all()  # the leases lapse; only the second site stays approved
        allow_popups(alt_site)
        held = await tools["tabs"](action="switch", index=0)
        expect(held.get("status") == "needs_approval", "switching to an unapproved site asks", held)
        expect((await tools["tabs"]())["active_index"] == 2, "a refused switch changed nothing")
        went = await tools["tabs"](action="switch", index=0, reason="tabs E2E", confirm=True)
        expect(
            went.get("status") == "ok" and went["active_index"] == 0, "approved, it switches", went
        )
        back = await tools["tabs"](action="switch", index=2)
        expect(
            back.get("status") == "ok" and back["url"] == alt_base + "cross",
            "the approval that counts is the target tab's site, not the active page's",
            back,
        )

        # --- closing ----------------------------------------------------------
        closed = await tools["tabs"](action="close", index=2)
        expect(closed.get("status") == "ok", "the active tab closes", closed)
        expect(
            closed["url"] == base and closed["title"] == "opener" and closed["active_index"] == 0,
            "closing it returns to the tab that opened it, not the newest one",
            closed,
        )
        expect(closed["tab_count"] == 2, "and one tab fewer is open", closed)

        ctx.perms.revoke_all()
        held = await tools["tabs"](action="close", index=1)
        expect(held.get("status") == "needs_approval", "closing asks first", held)
        expect(tabs_open() == 2, "a refused close closed nothing")
        gone = await tools["tabs"](action="close", index=1, reason="tabs E2E", confirm=True)
        expect(
            gone.get("status") == "ok"
            and gone["closed"]["url"] == base + "second"
            and gone["tab_count"] == 1
            and gone["active_index"] == 0,
            "closing another tab leaves the active one alone",
            gone,
        )
        missing = await tools["tabs"](action="close", index=5)
        expect(missing.get("status") == "not_found", "a stale index is reported", missing)
        expect(tabs_open() == 1, "and closes nothing")

        # --- a tab that hangs must not hang the list --------------------------
        allow_popups(site, alt_site)
        await tools["click"](selector="#busy")
        await wait_for(lambda: tabs_open() == 2, "the busy tab")
        await asyncio.sleep(0.8)  # let its script start spinning
        began = time.monotonic()
        listed = await tools["tabs"]()
        took = time.monotonic() - began
        expect(took < 4.0, "listing returns although one tab never answers", round(took, 2))
        titles = [t["title"] for t in listed["tabs"]]
        expect(titles == ["opener", ""], "the tab that cannot answer has no title", titles)
        cleared = await tools["tabs"](action="close", index=1, reason="tabs E2E", confirm=True)
        expect(
            cleared.get("status") == "ok" and cleared["title"] == "opener" and tabs_open() == 1,
            "and it can still be closed",
            cleared,
        )

        # --- the trail --------------------------------------------------------
        rows = [row for row in audit_rows(ctx) if row["tool"] == "tabs" and "action" in row["args"]]
        done = [(r["args"]["action"], r["args"]["index"]) for r in rows if r["status"] == "ok"]
        expect(
            ("switch", 0) in done and ("close", 2) in done and ("close", 1) in done,
            "switches and closes are audited",
            done,
        )
        expect(
            any(r["status"] == "needs_approval" for r in rows),
            "and so are the ones that were refused",
        )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool) -> None:
    port = free_port()
    Handler.alt_base = f"http://localhost:{port}/"
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    try:
        if not headful_only:
            await run_lifecycle(base, headless=True)
            await run_mode(base, Handler.alt_base, headless=True)
        if not headless_only:
            await run_lifecycle(base, headless=False)
            await run_mode(base, Handler.alt_base, headless=False)
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(async_main(args.headless_only, args.headful_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
