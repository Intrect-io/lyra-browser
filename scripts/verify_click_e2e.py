#!/usr/bin/env python3
"""Exercise fast, structured failures for click/type_text against a real browser.

Companion to ``verify_browser_e2e.py`` (same explicit-gate contract: temporary
profile, loopback site, not part of the unit suite; it reuses that script's
``build_tools``). Every scenario is one that used to end in Playwright's 30s
timeout or in a bare exception:

- a selector that matches nothing, and the grants it must not buy;
- hidden / disabled controls, and the prompt they must not raise;
- duplicate-text controls, and what ``click`` reports about the one it took;
- a control under an overlay, which must answer within ``timeout_ms`` + 2s;
- a click that lands and starts a slow navigation (the hint that stops a retry);
- a field that cannot be typed into;
- refs from ``read_page(mode="tree")`` passed as selectors, live and stale;
- the page closing while a call waits on it.

Assertions read what the *page* recorded (``window.hits``), not what the tool
claims, so a gate that answered and clicked anyway would fail here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

from lyra_browser.context import current_session_key  # noqa: E402
from lyra_browser.permission import Capability  # noqa: E402

HOME = """<!doctype html><title>click</title><h1>Click E2E</h1>
<button id="b1" onclick="hit('b1')">Save</button>
<button id="b2" onclick="hit('b2')">Save</button>
<button id="b3" onclick="hit('b3')">Save</button>
<button id="hid" style="display:none" onclick="hit('hid')">Ghost</button>
<button id="dis" disabled onclick="hit('dis')">Locked</button>
<input id="q" name="q">
<input id="ro" readonly value="fixed">
<div id="label">not a field</div>
<a id="slow" href="/slow">slow link</a>
<button id="late" disabled onclick="hit('late')">Late</button>
<button id="fade" style="visibility:hidden" onclick="hit('fade')">Fade</button>
<script>
  // Recorded in the DOM, not in a global: patchright runs the harness's own
  // evaluate() in an isolated world, where the page's globals do not exist.
  function hit(id) {
    const root = document.documentElement;
    root.dataset.hits = (root.dataset.hits ? root.dataset.hits + ',' : '') + id;
  }
  // Not ready at load, ready 1.2s later: an async validation, a hydrating menu.
  setTimeout(() => {
    document.getElementById('late').disabled = false;
    document.getElementById('fade').style.visibility = 'visible';
  }, 1200);
</script>"""

# A control under a full-screen overlay: it is visible, enabled and stable, and
# still cannot be clicked. Only the driver's own hit-target check sees that.
COVERED = """<!doctype html><title>covered</title>
<button id="covered" onclick="hit('covered')">Covered</button>
<div id="overlay" style="position:fixed;inset:0;z-index:9999"></div>
<script>
  function hit(id) { document.documentElement.dataset.hits = id; }
</script>"""

SLOW_SECONDS = 3.0


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(HOME)
        elif path in ("/covered", "/popup"):
            self._send(COVERED)
        elif path == "/slow":
            time.sleep(SLOW_SECONDS)
            self._send("<title>slow</title><h1>Slow</h1>")
        else:
            self.send_error(404)

    def log_message(self, *_args: object) -> None:
        pass


async def timed(awaitable) -> tuple[dict, float]:
    started = time.monotonic()
    result = await awaitable
    return result, time.monotonic() - started


async def hits(ctx) -> list:
    """What the page recorded, read from the DOM (see the fixture's ``hit``)."""
    page = await ctx.session.page()
    raw = await page.evaluate("document.documentElement.dataset.hits || ''")
    return raw.split(",") if raw else []


def live_grants(ctx) -> list:
    return ctx.perms.live_grants(current_session_key(ctx))


async def run_mode(base: str, *, headless: bool) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-click-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    ctx, tools = await build_tools(data_dir, headless=headless)
    print(f"\n[{mode}] data_dir={data_dir}")
    try:
        await tools["open_browser"]()
        entered = await tools["navigate"](url=base, reason="click E2E", confirm=True)
        expect(entered.get("title") == "click", "fixture page loads", entered)

        # -- a control that is present but not ready yet is waited for, briefly ------
        # Playwright's own click does this; a one-shot look would refuse both (the
        # fixture enables one button and reveals the other 1.2s after load).
        for late in ("late", "fade"):
            await tools["navigate"](url=base, confirm=True)  # the timers restart at load
            ready, took = await timed(tools["click"](selector=f"#{late}"))
            expect(ready.get("status") == "ok", f"#{late} is clicked once it is ready", ready)
            expect(0.2 < took < 3.0, f"after waiting for it, within the window (took {took:.2f}s)")
            expect(await hits(ctx) == [late], "and the page recorded it")
        await tools["navigate"](url=base, confirm=True)  # a fresh page, fresh hits

        # -- what click reports about what it hit -----------------------------
        one = await tools["click"](selector="#b2")
        expect(one.get("status") == "ok" and one.get("matches") == 1, "single match", one)
        expect(
            one.get("clicked") == {"tag": "button", "role": "button", "name": "Save"},
            "click reports tag, role and name of the element",
            one,
        )
        expect("hint" not in one, "one match carries no warning", one)
        expect(await hits(ctx) == ["b2"], "and the page recorded exactly that click")

        dup = await tools["click"](selector="text=Save")
        expect(dup.get("status") == "ok", "duplicate-text click succeeds", dup)
        expect(dup.get("matches") == 3, "matches counts every duplicate", dup)
        expect((dup.get("clicked") or {}).get("name") == "Save", "clicked name is reported", dup)
        expect("first" in dup.get("hint", ""), "hint says the first match was used", dup)
        expect(await hits(ctx) == ["b2", "b1"], "only the first duplicate was clicked")

        # -- refs from read_page(mode="tree") arrive as selectors ------------------
        # The same snapshot API a tree read uses, so these are real refs. Two of
        # the failures below (an unknown ref, a ref into a frame that is gone) used
        # to be a 30s wait and an exception respectively.
        page = await ctx.session.page()
        tree = await page.aria_snapshot(mode="ai")
        # A button that has focus reads `button "Save" [active] [ref=e3]`.
        saves = re.findall(r'button "Save"[^\n]*?\[ref=(\w+)\]', tree)
        boxes = re.findall(r"textbox[^\n]*?\[ref=(\w+)\]", tree)
        expect(len(saves) == 3 and boxes, "the tree read hands out refs", tree)
        by_ref = await tools["click"](selector=f"aria-ref={saves[2]}")
        expect(
            by_ref.get("status") == "ok" and by_ref.get("matches") == 1,
            "a ref clicks",
            by_ref,
        )
        expect(
            (by_ref.get("clicked") or {}).get("name") == "Save",
            "and reports what it named",
            by_ref,
        )
        expect(await hits(ctx) == ["b2", "b1", "b3"], "the page recorded that very element")
        via_ref = await tools["type_text"](selector=f"aria-ref={boxes[0]}", value="e2e-typed-ref")
        expect(via_ref.get("status") == "ok", "type_text accepts a ref", via_ref)
        expect(await page.input_value("#q") == "e2e-typed-ref", "and the field holds it")
        for ref in ("aria-ref=e999", "aria-ref=f9e3"):
            for name, kwargs in (("click", {}), ("type_text", {"value": "x"})):
                dead, took = await timed(tools[name](selector=ref, **kwargs))
                expect(
                    dead.get("status") == "not_found" and 'read_page(mode="tree")' in dead["hint"],
                    f"{name} on {ref} is not_found and says to read the tree again",
                    dead,
                )
                expect(took < 1.0, f"immediately, without the appear-wait (took {took:.2f}s)")

        # -- a selector that matches nothing, and what it must not buy -----------
        ctx.perms.revoke_all(current_session_key(ctx))  # a clean slate to buy on
        missing, took = await timed(
            tools["click"](selector="#nope", submits=True, reason="send", confirm=True)
        )
        expect(missing.get("status") == "not_found", "missing selector is not_found", missing)
        expect(took < 5, f"and answers in under 5s (took {took:.1f}s)")
        expect(
            missing.get("selector") == "#nope" and missing.get("hint"),
            "with the selector echoed and a hint",
            missing,
        )
        expect(
            live_grants(ctx) == [], "no lease and no one-shot grant was bought", live_grants(ctx)
        )
        # Grants were wiped on purpose, and re-entering reloads the page (fresh hits).
        await tools["navigate"](url=base, confirm=True)

        # -- hidden / disabled: named, and never asked about --------------------
        hidden, took = await timed(tools["click"](selector="#hid", submits=True))
        expect(hidden.get("status") == "hidden", "hidden control is named", hidden)
        expect(took < 5, f"hidden answers in under 5s (took {took:.1f}s)")
        expect(hidden.get("status") != "needs_approval", "and nobody was asked to approve it")
        disabled = await tools["click"](selector="#dis")
        expect(disabled.get("status") == "disabled", "disabled control is named", disabled)
        expect(await hits(ctx) == [], "neither was clicked")

        # -- type_text ------------------------------------------------------------
        typed = await tools["type_text"](selector="#q", value="e2e-typed-value")
        expect(typed.get("status") == "ok", "type_text fills a real field", typed)
        page = await ctx.session.page()
        expect(await page.input_value("#q") == "e2e-typed-value", "and the field holds it")
        gone = await tools["type_text"](selector="#nope", value="x")
        expect(gone.get("status") == "not_found", "type_text names a missing field", gone)

        label, took = await timed(tools["type_text"](selector="#label", value="x", timeout_ms=1500))
        expect(
            label.get("status") == "element_not_actionable"
            and "typed into" in label.get("hint", ""),
            "a non-field says it cannot be typed into",
            label,
        )
        expect(took < 3.5, f"and does so within timeout_ms + 2s (took {took:.1f}s)")
        readonly, took = await timed(tools["type_text"](selector="#ro", value="x", timeout_ms=1500))
        expect(
            readonly.get("status") == "element_not_actionable",
            "a read-only field is not_actionable, not a 30s wait",
            readonly,
        )
        expect(took < 3.5, f"within timeout_ms + 2s (took {took:.1f}s)")

        # -- a covered control: answers inside timeout_ms + 2s, clicks nothing ----
        await tools["navigate"](url=base + "covered", confirm=True)
        covered, took = await timed(tools["click"](selector="#covered", timeout_ms=2000))
        expect(
            covered.get("status") in ("timeout", "element_not_actionable"),
            "a covered control fails structurally",
            covered,
        )
        expect(took < 4.0, f"within timeout_ms + 2s, not 30s (took {took:.1f}s)")
        expect(
            covered.get("status") == "element_not_actionable" and "overlay" in covered["hint"],
            "and names what is in the way",
            covered,
        )
        expect(await hits(ctx) == [], "the covered control was not clicked")

        # A SUBMIT bought for a click that then failed is handed back once the
        # release grace has passed, not left for the next request to spend.
        await tools["click"](
            selector="#covered", submits=True, reason="send", confirm=True, timeout_ms=800
        )
        await asyncio.sleep(ctx.config.scope_release_grace_s + 0.5)
        spent = [g for g in live_grants(ctx) if g.capability is Capability.SUBMIT and g.uses_left]
        expect(spent == [], "a failed declared click leaves no live SUBMIT grant", spent)

        # -- a click that lands and starts a slow navigation ------------------------
        await tools["navigate"](url=base, confirm=True)
        late, took = await timed(tools["click"](selector="#slow", timeout_ms=800))
        expect(late.get("status") == "timeout", "a click held up by a slow load times out", late)
        expect("Do not click again" in late.get("hint", ""), "and says the click landed", late)
        expect(took < 2.8, f"in {took:.1f}s, well before the 30s default")
        page = await ctx.session.page()
        await page.wait_for_url("**/slow", timeout=int((SLOW_SECONDS + 3) * 1000))
        expect(await page.title() == "slow", "and it really had: the page went on to load")

        # -- the page closing while a call waits on it ------------------------------
        # A second page is adopted as the current one exactly as a real popup is
        # (see ``BrowserSession._adopt_page``). Opening it here, and loading it
        # through the navigate tool, keeps this about the closed target rather
        # than about whether the guard lets an unknown-initiator popup navigate.
        await tools["navigate"](url=base, confirm=True)
        opener = await ctx.session.page()
        popup = await opener.context.new_page()
        expect(await ctx.session.page() is popup, "the new page became the current page")
        await tools["navigate"](url=base + "popup", confirm=True)

        async def close_soon() -> None:
            await asyncio.sleep(1.0)
            await popup.close()

        closer = asyncio.create_task(close_soon())
        closed, took = await timed(tools["click"](selector="#covered", timeout_ms=8000))
        await closer
        expect(
            closed.get("status") == "page_closed", "a page closed mid-call is page_closed", closed
        )
        expect(took < 4.0, f"and reported promptly, not after 8s (took {took:.1f}s)")

        # -- the trail ----------------------------------------------------------------
        raw = ctx.config.audit_path.read_text()
        rows = [json.loads(line) for line in raw.splitlines() if line]
        seen = {row.get("status") for row in rows if row.get("tool") in ("click", "type_text")}
        expect(
            {"not_found", "hidden", "disabled", "element_not_actionable", "timeout", "page_closed"}
            <= seen,
            "every failure is on the audit trail",
            sorted(s for s in seen if s),
        )
        expect(
            "e2e-typed-value" not in raw and "e2e-typed-ref" not in raw,
            "and the typed values are not",
        )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool) -> None:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    try:
        if not headful_only:
            await run_mode(base, headless=True)
        if not headless_only:
            await run_mode(base, headless=False)
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
