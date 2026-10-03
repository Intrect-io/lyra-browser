#!/usr/bin/env python3
"""Exercise hover and scroll against a real browser.

Companion to ``verify_click_e2e.py`` (same explicit-gate contract: temporary
profile, loopback site, not part of the unit suite; it reuses
``verify_browser_e2e.build_tools``). Scenarios:

- a menu whose flyout opens only while the pointer is on its ``<a href>``
  trigger: the flyout item is not clickable before ``hover``, is after it, and the
  page records the click — while the trigger itself was never pressed;
- ``scroll(to='bottom')`` called repeatedly loads an IntersectionObserver list,
  until the reply says ``at_bottom`` with every batch loaded;
- ``scroll(by_y=...)`` moves with the wheel, ``scroll(selector=...)`` brings an
  element that starts far below the fold into view;
- a selector that matches nothing answers ``not_found`` in a few seconds and buys
  no grant;
- both tools refuse during a takeover and leave the page where it was.

Assertions read what the *page* did (the DOM), not what a tool claims.
"""

from __future__ import annotations

import argparse
import asyncio
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

# The flyout opens on :hover of the wrapper, and the trigger is a real link — the
# shape a click cannot stand in for (clicking it would jump to its href).
MENU = """<!doctype html><title>menu</title>
<style>
  .nav { position: relative; display: inline-block; margin: 40px; }
  #flyout { display: none; position: absolute; top: 100%; left: 0; background: #eee;
            padding: 8px; white-space: nowrap; }
  .nav:hover #flyout { display: block; }
</style>
<div class="nav">
  <a id="products" href="#products-page">Products</a>
  <div id="flyout"><a id="item" href="#item-page" onclick="hit('item')">Plugins</a></div>
</div>
<script>
  function hit(id) {
    const root = document.documentElement;
    root.dataset.hits = (root.dataset.hits ? root.dataset.hits + ',' : '') + id;
  }
</script>"""

BATCHES = 5
PER_BATCH = 20

# A list that grows when its sentinel scrolls into view, a little after the
# scroll (as a real feed fetching its next page does). The first batch is taller
# than any viewport, so nothing loads until the page is scrolled.
LAZY = f"""<!doctype html><title>lazy</title>
<style>body {{ margin: 0 }} li {{ height: 100px; list-style: none }}</style>
<ul id="list"></ul><div id="sentinel">loading...</div>
<script>
  let batches = 0;
  function more() {{
    const list = document.getElementById('list');
    for (let i = 0; i < {PER_BATCH}; i++) {{
      const li = document.createElement('li');
      li.textContent = 'item ' + (batches * {PER_BATCH} + i);
      list.appendChild(li);
    }}
    if (++batches >= {BATCHES}) {{
      observer.disconnect();
      document.getElementById('sentinel').remove();
    }}
  }}
  const observer = new IntersectionObserver((entries) => {{
    if (entries.some((e) => e.isIntersecting)) setTimeout(more, 100);
  }});
  more();
  observer.observe(document.getElementById('sentinel'));
</script>"""

FAR = """<!doctype html><title>far</title>
<div style="height:5000px">spacer</div>
<div id="far">Far away</div><div style="height:400px"></div>"""

LONG = """<!doctype html><title>long</title><body style="margin:0">
<div style="height:6000px">tall</div></body>"""

PAGES = {"/": MENU, "/lazy": LAZY, "/far": FAR, "/long": LONG}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = PAGES.get(urlparse(self.path).path)
        if body is None:
            self.send_error(404)
            return
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: object) -> None:
        pass


async def timed(awaitable) -> tuple[dict, float]:
    started = time.monotonic()
    result = await awaitable
    return result, time.monotonic() - started


async def dom(ctx, expression: str):
    page = await ctx.session.page()
    return await page.evaluate(expression)


def live_grants(ctx) -> list:
    return ctx.perms.live_grants(current_session_key(ctx))


async def run_mode(base: str, *, headless: bool) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-motion-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    ctx, tools = await build_tools(data_dir, headless=headless)
    print(f"\n[{mode}] data_dir={data_dir}")
    try:
        await tools["open_browser"]()
        entered = await tools["navigate"](url=base, reason="motion E2E", confirm=True)
        expect(entered.get("title") == "menu", "menu page loads", entered)

        # -- a flyout that only exists while the trigger is hovered --------------------
        expect(
            await dom(ctx, "getComputedStyle(document.getElementById('flyout')).display") == "none",
            "the flyout starts closed",
        )
        early = await tools["click"](selector="#item")
        expect(early.get("status") == "hidden", "its item cannot be clicked yet", early)

        hovered = await tools["hover"](selector="#products")
        expect(hovered.get("status") == "ok", "hover succeeds", hovered)
        expect(
            hovered.get("hovered") == {"tag": "a", "role": "link", "name": "Products"},
            "hover reports what it landed on",
            hovered,
        )
        expect(hovered.get("matches") == 1, "and how many matched", hovered)
        expect(
            await dom(ctx, "getComputedStyle(document.getElementById('flyout')).display")
            == "block",
            "the page opened the flyout",
        )
        expect(
            await dom(ctx, "location.hash") == "",
            "and the trigger link itself was not pressed",
        )

        item = await tools["click"](selector="#item")
        expect(item.get("status") == "ok", "the flyout item is clicked", item)
        expect(await dom(ctx, "document.documentElement.dataset.hits") == "item", "page saw it")

        # -- a list that loads as the page is scrolled to its end --------------------
        await tools["navigate"](url=base + "lazy", confirm=True)
        expect(
            await dom(ctx, "document.querySelectorAll('#list li').length") == PER_BATCH,
            "only the first batch is loaded at first",
        )
        first = await tools["scroll"](to="bottom")
        expect(first.get("status") == "ok", "scroll to bottom answers ok", first)
        expect(first["scroll_y"] > 0 and first["scroll_height"] > first["scroll_y"], "moved", first)
        calls = 1
        last = first
        while not last["at_bottom"] and calls < 20:
            last = await tools["scroll"](to="bottom")
            calls += 1
        loaded = await dom(ctx, "document.querySelectorAll('#list li').length")
        expect(last["at_bottom"], f"repeating reaches at_bottom ({calls} calls)", last)
        expect(loaded == BATCHES * PER_BATCH, "with every batch loaded", loaded)
        expect(calls >= 2, "which took more than one scroll", calls)
        expect(
            last["scroll_height"] > first["scroll_height"],
            "and the reply's scroll_height grew with the list",
            (first, last),
        )
        top = await tools["scroll"](to="top")
        expect(top["scroll_y"] == 0 and not top["at_bottom"], "to='top' returns to the start", top)
        stuck = await tools["scroll"](to="top")
        expect("hint" not in stuck, "already there is not reported as a stuck page", stuck)

        # -- by pixels, with the wheel -------------------------------------------------
        await tools["navigate"](url=base + "long", confirm=True)
        down = await tools["scroll"](by_y=700)
        expect(down["status"] == "ok" and down["scroll_y"] == 700, "by_y=700 moves 700px", down)
        real = await dom(ctx, "Math.round(document.scrollingElement.scrollTop)")
        expect(real == down["scroll_y"], "and the reply is where the page really is", real)
        up = await tools["scroll"](by_y=-300)
        expect(up["scroll_y"] == 400, "a negative by_y scrolls back up", up)
        huge = await tools["scroll"](by_y=10**7)
        expect(huge["scroll_y"] <= 400 + 20_000, "one call is clamped", huge)
        expect(huge["at_bottom"], "and this page is short enough to end", huge)

        # -- an element far below the fold ---------------------------------------------
        await tools["navigate"](url=base + "far", confirm=True)
        in_view = (
            "(() => { const r = document.getElementById('far').getBoundingClientRect();"
            " return r.top >= 0 && r.bottom <= window.innerHeight; })()"
        )
        expect(not await dom(ctx, in_view), "the target starts offscreen")
        brought = await tools["scroll"](selector="#far")
        expect(brought.get("status") == "ok", "scroll(selector) succeeds", brought)
        expect(brought["scroll_y"] > 0, "and moved the page", brought)
        expect(await dom(ctx, in_view), "the element is now in view")

        # -- a selector that matches nothing buys nothing ------------------------------
        ctx.perms.revoke_all(current_session_key(ctx))  # a clean slate to buy on
        for name, kwargs in (("hover", {}), ("scroll", {})):
            missing, took = await timed(
                tools[name](selector="#nope", reason="e2e", confirm=True, **kwargs)
            )
            expect(missing.get("status") == "not_found", f"{name} on a missing selector", missing)
            expect(took < 5, f"answers within a few seconds (took {took:.1f}s)")
            expect(missing.get("hint"), "and says what to do", missing)
            expect(live_grants(ctx) == [], f"{name} bought no grant", live_grants(ctx))

        # -- takeover ------------------------------------------------------------------
        await tools["navigate"](url=base + "far", confirm=True)
        ctx.collab.takeover = True
        try:
            refused = {
                "hover": await tools["hover"](selector="#far"),
                "scroll(to)": await tools["scroll"](to="bottom"),
                "scroll(by_y)": await tools["scroll"](by_y=500),
                "scroll(selector)": await tools["scroll"](selector="#far"),
            }
        finally:
            ctx.collab.takeover = False
        for name, reply in refused.items():
            expect(reply.get("status") == "takeover_active", f"{name} refused in takeover", reply)
        expect(
            await dom(ctx, "Math.round(document.scrollingElement.scrollTop)") == 0,
            "and the page did not move",
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
