#!/usr/bin/env python3
"""Exercise annotated screenshots and coordinate actions against a real browser.

Same contract as the other ``verify_*_e2e.py`` gates (temporary profile, loopback site,
``build_tools``; ``LYRA_BROWSER_DRIVER`` picks the driver). Asserted on what reached the
page and on the pixels:

- ``screenshot(annotate=true)``: a mark for every actionable element on screen, each box
  matching the element's rect (IoU >= 0.9), each ``ref`` clicking that element; the marks
  are drawn on the image and the page saw 0 DOM mutations;
- ``click(x, y)`` on a point drawn only on a canvas lands within 2 CSS px, at scale 1 and
  with a device pixel ratio of 2 downscaled to the max edge (scale reported and used);
- ``hover(x, y)`` reaches the element under the point; a point off screen answers
  ``outside_viewport``;
- a form sent by a coordinate click without ``submits=true`` is stopped as before.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect  # noqa: E402

PAGE = """<!doctype html><html><head><title>visual</title>
<style>body{margin:0;font-family:sans-serif;background:#fff}
.row{position:absolute;left:40px}
button{font-size:15px;padding:6px 12px;margin-right:10px}
#cv{position:absolute;left:520px;top:300px;border:1px solid #999}
#hov{position:absolute;left:60px;top:420px;padding:10px;background:#eee}
form{position:absolute;left:60px;top:520px}</style></head><body>
<div class="row" style="top:30px">
 <button id="b1">Alpha</button><button id="b2">Beta</button><button id="b3">Gamma</button>
 <button id="icon"><svg width="18" height="18" aria-hidden="true">
   <rect x="2" y="2" width="14" height="14" fill="#333"/></svg></button>
</div>
<div class="row" style="top:110px"><a id="lnk" href="#there">A link</a></div>
<div class="row" style="top:170px"><input id="in" aria-label="Field">
 <select id="sel" aria-label="Pick"><option>One</option><option>Two</option></select></div>
<canvas id="cv" width="400" height="200"></canvas>
<span id="hov">Hover zone</span>
<form method="post" action="/submitted"><button id="send" type="submit">Send</button></form>
<script>
const s = {muts: 0, hits: [], points: [], hovers: 0, what: []};
window.__s = s;
new MutationObserver((m) => { s.muts += m.length;
  for (const r of m) s.what.push(r.type + ':' + r.target.nodeName + ':' + (r.attributeName || '') +
    ':' + Array.from(r.addedNodes).map((n) => n.nodeName + (n.id ? '#' + n.id : '')).join(','));
})
  .observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
for (const b of document.querySelectorAll('.row button'))
  b.addEventListener('click', () => s.hits.push(b.id));
const cv = document.getElementById('cv'), g = cv.getContext('2d');
g.fillStyle = '#0a7'; g.beginPath(); g.arc(310, 140, 10, 0, 7); g.fill();
cv.addEventListener('click', (e) => {
  const r = cv.getBoundingClientRect(); s.points.push([e.clientX - r.left, e.clientY - r.top]); });
document.getElementById('hov').addEventListener('pointerenter', () => { s.hovers++; });
</script></body></html>"""

TARGET = (310.0, 140.0)  # the dot on the canvas, in canvas CSS px
RECT_JS = (
    "(el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }"
)
ID_JS = "(el) => el.id"
CANVAS_JS = (
    "() => { const r = document.getElementById('cv').getBoundingClientRect();"
    " return [r.left, r.top]; }"
)
HOV_JS = (
    "() => { const r = document.getElementById('hov').getBoundingClientRect();"
    " return [r.left + r.width / 2, r.top + r.height / 2]; }"
)
SEND_JS = (
    "() => { const r = document.getElementById('send').getBoundingClientRect();"
    " return [r.left + r.width / 2, r.top + r.height / 2]; }"
)
STATE_JS = "() => window.__s"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - http.server API
        self.server.posted = True  # type: ignore[attr-defined]
        body = b"<!doctype html><title>sent</title>sent"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def ev(page, js: str, arg=None):
    """Evaluate in the page's own world (the native driver defaults to an isolated one)."""
    try:
        if arg is None:
            return await page.evaluate(js, world="main")
        return await page.evaluate(js, arg, world="main")
    except TypeError:
        return await (page.evaluate(js) if arg is None else page.evaluate(js, arg))


def iou(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0.0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0.0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def mark_pixels(png: bytes) -> int:
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    return sum(1 for r, g, b in img.getdata() if r > 235 and 95 < g < 145 and b < 30)


async def canvas_click(tools, page, *, label: str) -> None:
    shot = await tools["screenshot"]()
    scale = shot.get("scale")
    expect(isinstance(scale, float) and scale > 0, f"{label}: the capture reports its scale", shot)
    left, top = await ev(page, CANVAS_JS)
    x, y = (left + TARGET[0]) * scale, (top + TARGET[1]) * scale
    res = await tools["click"](x=x, y=y)
    expect(res.get("status") == "ok", f"{label}: click(x, y) answers ok", res)
    px, py = (await ev(page, STATE_JS))["points"][-1]
    off = max(abs(px - TARGET[0]), abs(py - TARGET[1]))
    expect(off <= 2.0, f"{label}: the click landed within 2 CSS px of the dot", (px, py))


async def run_mode(base: str, server, *, headless: bool) -> None:
    from lyra_browser import cursor
    from lyra_browser.context import acquire_page

    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-visual-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless, consent_channel="autonomous")
    print(f"\n[{mode}]")
    try:
        expect((await tools["open_browser"]()).get("status") == "ok", "browser launches")
        expect((await tools["navigate"](url=base))["status"] == "ok", "page loads")
        page, _ = await acquire_page(ctx)

        muts_before = (await ev(page, STATE_JS))["muts"]
        shot = await tools["screenshot"](annotate=True)
        marks = shot.get("marks") or []
        expect(len(marks) == 8, "annotate returns a mark per actionable element on screen", marks)
        expect([m["n"] for m in marks] == list(range(1, len(marks) + 1)), "marks are numbered 1..N")
        state = await ev(page, STATE_JS)
        expect(
            state["muts"] == muts_before,
            "annotating wrote nothing to the page",
            (muts_before, state["muts"], state["what"]),
        )
        png = Path(shot["image_path"]).read_bytes()
        expect(mark_pixels(png) >= 200, "the marks are drawn on the image", mark_pixels(png))
        worst = 1.0
        for mark in marks:
            rect = await page.locator(f"aria-ref={mark['ref']}").first.evaluate(RECT_JS)
            worst = min(worst, iou(mark["box_css"], rect))
        expect(worst >= 0.9, "every mark's box matches its element (IoU >= 0.9)", worst)
        names = {m["name"] for m in marks}
        expect({"Alpha", "Beta", "Gamma", "Send"} <= names, "named controls are marked", names)

        clicked = []
        for mark in marks:
            loc = page.locator(f"aria-ref={mark['ref']}").first
            ident = await loc.evaluate(ID_JS)
            if ident in ("b1", "b2", "b3", "icon"):
                res = await tools["click"](selector=f"aria-ref={mark['ref']}")
                expect(res.get("status") == "ok", f"mark {mark['n']} clicks by its ref", res)
                clicked.append(ident)
        hits = (await ev(page, STATE_JS))["hits"]
        expect(hits == clicked and len(hits) == 4, "each ref clicked its own element", hits)

        await canvas_click(tools, page, label="scale 1")

        hx, hy = await ev(page, HOV_JS)
        shot = await tools["screenshot"]()
        res = await tools["hover"](x=hx * shot["scale"], y=hy * shot["scale"])
        expect(res.get("status") == "ok", "hover(x, y) answers ok", res)
        expect((await ev(page, STATE_JS))["hovers"] >= 1, "hover(x, y) reached the element")

        res = await tools["click"](x=99999, y=5)
        expect(res.get("status") == "outside_viewport", "a point off screen is refused", res)

        edge = ctx.config.capture_max_edge
        ctx.config.capture_max_edge = 1000
        shot = await tools["screenshot"]()
        expect(
            shot.get("width") == 1000 and abs(shot.get("scale", 0) - 1000 / 1280) < 0.005,
            "a capture over the max edge is downscaled and says by how much",
            (shot.get("width"), shot.get("scale")),
        )
        await canvas_click(tools, page, label="downscaled to 1000 px")
        ctx.config.capture_max_edge = edge

        await cursor._send(
            page,
            "Emulation.setDeviceMetricsOverride",
            {"width": 1280, "height": 800, "deviceScaleFactor": 2, "mobile": False},
        )
        shot = await tools["screenshot"]()
        expect(
            abs(shot.get("scale", 0) - shot.get("width", 0) / 1280) < 0.005,
            "at device pixel ratio 2 the scale is what the image measures",
            (shot.get("scale"), shot.get("width")),
        )
        await canvas_click(tools, page, label="device pixel ratio 2")
        # Back to a ratio of 1 at the same size (clearing the override would also drop
        # the viewport the session itself emulates).
        await cursor._send(
            page,
            "Emulation.setDeviceMetricsOverride",
            {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False},
        )

        sx, sy = await ev(page, SEND_JS)
        shot = await tools["screenshot"]()
        server.posted = False
        res = await tools["click"](x=sx * shot["scale"], y=sy * shot["scale"])
        expect(
            res.get("status") == "blocked_by_policy",
            "an undeclared form send by a coordinate click is stopped",
            res,
        )
        expect(not server.posted, "and the form never reached the server")
    finally:
        await tools["close_browser"]()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.posted = False  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    try:
        if not headful_only:
            await run_mode(base, server, headless=True)
        if not headless_only:
            await run_mode(base, server, headless=False)
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
