#!/usr/bin/env python3
"""Exercise ``inspect_layout`` against a real browser.

Same contract as the other ``verify_*_e2e.py`` gates (temporary profile, loopback site,
``build_tools``; ``LYRA_BROWSER_DRIVER`` picks the driver). One page with planted faults and
look-alikes that are not faults:

- found: low-contrast text (also on an oklch background, as a placeholder, inside a shadow
  root), text cut off by its box or spilling out of its card, text with a box over it, a
  button and a password field under overlays (the typed value never in the reply);
- not reported: readable text, large text at 3.5:1 (checked, not skipped), white on an oklch
  dark background, a disabled control, text over a canvas or a gradient (counted as unknown
  backgrounds), rows scrolled out of a scroller, sr-only and faded-out text, a scrolling
  ``pre``, a child over its parent's text, a transparent stretched link, text under a fixed
  bar (counted as ``under_fixed``);
- ``selector`` takes CSS and ``text=``; a missing one is ``not_found``, an invalid one an
  ``error``; ``box_px`` maps into the latest screenshot; ``max_findings`` caps; the page
  sees 0 mutations; the scan's time over N=20 is printed (p50).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect  # noqa: E402

PAGE = """<!doctype html><html><head><title>layout</title>
<style>body{margin:0;padding:20px;font-family:sans-serif;font-size:15px;background:#fafafa;
 color:#222}
.card{display:inline-block;vertical-align:top;width:220px;height:110px;margin:10px;
 border:1px solid #888;background:#fafafa;padding:8px;box-sizing:border-box;position:relative}
</style></head><body>
<div class="card" id="c-low"><p id="lowc" style="color:#c2c2c2">Faint status line</p></div>
<div class="card" id="c-ok"><p id="okc">Readable status line</p></div>
<div class="card" id="c-large" style="background:#fff">
 <p id="large" style="font-size:26px;color:#8a8a8a;margin:0">Big grey</p></div>
<div class="card" id="c-grad" style="background:linear-gradient(#000,#fff)">
 <p id="grad" style="color:#777">On a gradient</p></div>
<div class="card" id="c-clip"><div id="clipbox"
 style="width:120px;overflow:hidden;white-space:nowrap">A long line that is cut off here</div></div>
<div class="card" id="c-spill"><p id="spill"
 style="white-space:nowrap;font-size:24px;letter-spacing:4px">Quarterly summary</p></div>
<div class="card" id="c-cover"><p id="coveredp">Status: all systems ready</p>
 <div style="position:absolute;left:100px;top:14px;width:110px;height:26px;background:#fafafa">
 </div></div>
<div class="card" id="c-btn"><button id="btncov">Save</button>
 <div style="position:absolute;left:0;top:0;width:100%;height:60px;background:rgba(0,0,0,.4)">
 </div></div>
<div class="card" id="clean" style="height:auto"><p>All good here</p><button>Fine</button>
 <p id="parent" style="position:relative">Parent text here
 <span style="position:absolute;left:0;top:0;background:#fafafa">child</span></p></div>
<div class="card" id="c-scroll"><div id="scroller" style="height:60px;overflow:auto">
 <p>row 1</p><p>row 2</p><p>row 3</p><p>row 4</p><p>row 5</p><p>row 6</p><p>row 7</p>
 <p>row 8</p><p>row 9</p><p>row 10</p></div></div>
<div class="card" id="c-sr"><button>Icon<span id="sr" style="position:absolute;width:1px;
 height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;
 border:0">opens in a new tab</span></button></div>
<div class="card" id="c-hero" style="padding:0">
 <canvas id="dark" width="218" height="108"></canvas>
 <h3 id="hero" style="position:absolute;left:12px;top:30px;margin:0;color:#fff">Welcome</h3></div>
<div class="card" id="c-fade"><p id="faded" style="opacity:0">Hidden tooltip</p></div>
<div class="card" id="c-stretch"><p id="stretchp">Card title</p>
 <a href="#x" id="stretch">Read</a>
 <style>#stretch::after{content:'';position:absolute;inset:0;z-index:1}</style></div>
<div class="card" id="c-pw"><input id="pw" type="password" aria-label="Password">
 <div style="position:absolute;left:0;top:0;width:100%;height:70px;background:#ddd"></div></div>
<div class="card" id="c-ok-dark" style="background:oklch(0.21 0.03 265)">
 <p id="okdark" style="color:#fff">White on oklch dark</p></div>
<div class="card" id="c-ok-light" style="background:oklch(0.97 0 0)">
 <p id="oklight" style="color:oklch(0.85 0 0)">Pale on oklch light</p></div>
<div class="card" id="c-ph" style="background:#fff"><input id="ph" placeholder="Email"
 style="color:#222;border:1px solid #888"><style>#ph::placeholder{color:#ccc}</style></div>
<div class="card" id="c-dis" style="background:#fff">
 <button id="dis" disabled style="color:#bbb;background:#fff">Disabled</button></div>
<div class="card" id="c-pre"><pre id="pre" style="overflow-x:auto;width:180px;margin:0">
a long line of code that scrolls sideways in its own box</pre></div>
<div class="card" id="c-shadow"><span id="host"></span></div>
<p id="underfixed" style="position:absolute;left:30px;top:calc(100vh - 35px);margin:0">
 Under the bar</p>
<div style="position:fixed;left:0;bottom:0;width:100%;height:50px;background:#333"></div>
<script>
document.getElementById('scroller').scrollTop = 200;
const dk = document.getElementById('dark').getContext('2d');
dk.fillStyle = '#111'; dk.fillRect(0, 0, 218, 108);
const sh = document.getElementById('host').attachShadow({mode: 'open'});
sh.innerHTML = '<span id="shadowtext" style="color:#ddd">Faint in a shadow root</span>';
document.getElementById('pw').value = 'hunter2-secret';
const s = {muts: 0};
window.__s = s;
new MutationObserver((m) => { s.muts += m.length; })
  .observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def ev(page, js: str):
    """Evaluate in the page's own world (the native driver defaults to an isolated one)."""
    try:
        return await page.evaluate(js, world="main")
    except TypeError:
        return await page.evaluate(js)


def of(findings: list[dict], kind: str, element_id: str) -> list[dict]:
    return [f for f in findings if f["kind"] == kind and f"#{element_id}" in f["element"]]


async def run_mode(base: str, *, headless: bool) -> None:
    from lyra_browser.context import acquire_page

    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-layout-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless, consent_channel="autonomous")
    print(f"\n[{mode}]")
    try:
        expect((await tools["open_browser"]()).get("status") == "ok", "browser launches")
        expect((await tools["navigate"](url=base))["status"] == "ok", "page loads")
        page, _ = await acquire_page(ctx)
        shot = await tools["screenshot"]()

        res = await tools["inspect_layout"]()
        expect(res.get("status") == "ok", "inspect_layout answers", res)
        found = res["findings"]
        low = of(found, "low_contrast", "lowc")
        expect(len(low) == 1 and low[0]["ratio"] < 2.0, "#c2c2c2 on #fafafa is low contrast", low)
        expect(not of(found, "low_contrast", "okc"), "#222 on #fafafa is not")
        expect(not of(found, "low_contrast", "large"), "26 px text at 3.5:1 passes as large text")
        expect(not of(found, "low_contrast", "grad"), "text on a gradient is not judged")
        expect(res["unknown_background"] >= 1, "and is counted as an unknown background", res)
        expect(of(found, "clipped", "clipbox"), "text cut off by overflow: hidden is clipped")
        expect(of(found, "clipped", "spill"), "text spilling out of its card is clipped")
        expect(not of(found, "clipped", "okc"), "text that fits is not")
        expect(of(found, "covered", "coveredp"), "text with a box over half of it is covered")
        expect(of(found, "covered", "btncov"), "a button under an overlay is covered")

        texts = [f.get("text", "") for f in found]
        expect(
            not [f for f in found if any(k in f["element"] for k in ("scroller", "#sr", "#faded"))]
            and not any(t.startswith("row ") for t in texts),
            "scrolled-away rows, sr-only and faded-out text are not faults",
            [f for f in found if "row " in f.get("text", "") or "#sr" in f["element"]],
        )
        expect(not of(found, "low_contrast", "hero"), "white text over a dark canvas is not judged")
        expect(
            not of(found, "covered", "stretchp") and not of(found, "covered", "stretch"),
            "a transparent click-catcher over a card is not a cover",
        )
        pw = of(found, "covered", "pw")
        expect(
            pw and "hunter2" not in str(found),
            "a covered password field: found, value not shown",
            pw,
        )
        expect(not of(found, "low_contrast", "okdark"), "white on an oklch dark background passes")
        expect(of(found, "low_contrast", "oklight"), "pale on an oklch light background is found")
        expect(of(found, "low_contrast", "ph"), "a pale placeholder is low contrast")
        expect(not of(found, "low_contrast", "dis"), "a disabled control is exempt")
        expect(not of(found, "clipped", "pre"), "a line that scrolls in its own box is not clipped")
        expect(of(found, "low_contrast", "shadowtext"), "faint text in a shadow root is found")
        expect(
            not of(found, "covered", "underfixed") and res["under_fixed"] >= 1,
            "text under a fixed bar is counted, not called covered",
            res.get("under_fixed"),
        )
        large = await tools["inspect_layout"](selector="#c-large")
        expect(
            large["findings"] == [] and large["contrast_checked"] >= 1,
            "26 px text at 3.5:1 was checked and passed",
            large,
        )
        ref = await tools["inspect_layout"](selector="text=Faint status line")
        expect(
            ref.get("status") == "ok" and ref["counts"].get("low_contrast") == 1,
            "a text= selector scopes it as the other tools' selectors do",
            ref,
        )
        bad = await tools["inspect_layout"](selector="[")
        expect(
            bad.get("status") == "error", "an invalid selector is an error, not unavailable", bad
        )

        clean = await tools["inspect_layout"](selector="#clean")
        expect(
            clean.get("status") == "ok" and clean["findings"] == [] and clean["scanned"] >= 3,
            "a clean region has no findings (a child over its parent's text is not a cover)",
            clean,
        )
        res2 = await tools["inspect_layout"](selector="#nothing-here")
        expect(res2.get("status") == "not_found", "a missing selector answers not_found", res2)

        target = low[0]
        want = [round(v * shot["scale"]) for v in target["box_css"]]
        expect(
            target.get("box_px") is not None
            and all(abs(a - b) <= 1 for a, b in zip(target["box_px"], want, strict=True)),
            "box_px is the finding's box in the latest screenshot",
            (target.get("box_px"), want),
        )
        capped = await tools["inspect_layout"](max_findings=2)
        expect(len(capped["findings"]) == 2 and capped["truncated"], "max_findings caps", capped)

        times = []
        for _ in range(20):
            started = time.monotonic()
            await tools["inspect_layout"]()
            times.append((time.monotonic() - started) * 1000)
        print(f"MEASURE inspect_layout ({mode}, N=20): p50 {statistics.median(times):.0f} ms")
        muts = await ev(page, "() => window.__s.muts")
        expect(muts == 0, "the page saw 0 DOM mutations", muts)
    finally:
        await tools["close_browser"]()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        if not headful_only:
            await run_mode(base, headless=True)
        if not headless_only:
            await run_mode(base, headless=False)
    finally:
        server.shutdown()
        server.server_close()
    print("ALL PASS")


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
