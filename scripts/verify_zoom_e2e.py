#!/usr/bin/env python3
"""Exercise zoomed region captures and coordinate actions on them against a real browser.

Same contract as the other ``verify_*_e2e.py`` gates (temporary profile, loopback site,
``build_tools``; ``LYRA_BROWSER_DRIVER`` picks the driver). Asserted on the pixels and on
what reached the page:

- ``screenshot(region=...)`` before any screenshot of the page is refused, and so are an
  empty region and one off screen;
- a zoomed region shows what the full capture shows there (mean pixel difference after
  bringing it back to the full capture's size), at device pixel ratio 1 and 2;
- it carries more detail, not just more pixels: 1 CSS px stripes that a downscaled full
  capture blurs are resolved in the zoom, at >= 2x the full capture's density;
- ``click``/``hover`` at a point of the zoomed image land within 1 CSS px of a 4x4 CSS px
  target, also after the page scrolled and at device pixel ratio 2;
- the page sees 0 DOM mutations from any of it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect  # noqa: E402

PAGE = """<!doctype html><html><head><title>zoom</title>
<style>body{margin:0;background:#fff;height:2400px;font-family:sans-serif}
.t{position:absolute;width:4px;height:4px;background:#c00}
#block{position:absolute;left:80px;top:80px;width:120px;height:80px}
#block div{float:left;width:60px;height:40px}
#stripes{position:absolute;left:300px;top:80px;width:120px;height:60px;
 background:repeating-linear-gradient(90deg,#000 0 1px,#fff 1px 2px)}</style></head><body>
<div id="block"><div style="background:#e11d48"></div><div style="background:#2563eb"></div>
<div style="background:#16a34a"></div><div style="background:#f59e0b"></div></div>
<div id="stripes"></div>
<div class="t" id="t1" style="left:700px;top:300px"></div>
<div class="t" id="t2" style="left:500px;top:1500px"></div>
<script>
const s = {muts: 0, points: {}, hovers: 0};
window.__s = s;
new MutationObserver((m) => { s.muts += m.length; })
  .observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
for (const t of document.querySelectorAll('.t')) {
  t.addEventListener('click', (e) => { const r = t.getBoundingClientRect();
    s.points[t.id] = [e.clientX - r.left, e.clientY - r.top]; });
  t.addEventListener('pointerenter', () => { s.hovers++; });
}
</script></body></html>"""

RECT_JS = (
    "(id) => { const r = document.getElementById(id).getBoundingClientRect();"
    " return [r.left, r.top, r.width, r.height]; }"
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


def _img(path: str):
    from PIL import Image

    return Image.open(path).convert("RGB")


def mean_diff(a, b) -> float:
    """Mean absolute channel difference of two same-size images."""
    pa, pb = a.tobytes(), b.tobytes()
    return sum(abs(x - y) for x, y in zip(pa, pb, strict=True)) / len(pa)


def transitions(img, y: int) -> int:
    """Dark/light changes along row ``y`` (a resolved stripe pattern has many)."""
    dark = [sum(img.getpixel((x, y))) / 3 < 128 for x in range(img.width)]
    return sum(1 for a, b in zip(dark, dark[1:], strict=False) if a != b)


def px_box(rect, scale: float) -> list[float]:
    """A viewport CSS rect as a region in image px of a viewport capture at ``scale``."""
    left, top, width, height = rect
    return [left * scale, top * scale, width * scale, height * scale]


async def region_matches_full(tools, page, *, label: str) -> None:
    """The zoom of #block, brought back to the full capture's size, equals the full one."""
    full = await tools["screenshot"]()
    scale = full["scale"]
    region = px_box(await ev(page, RECT_JS, "block"), scale)
    zoom = await tools["screenshot"](region=region)
    expect("image_path" in zoom, f"{label}: the zoom answers an image", zoom)
    expect(zoom["scale"] > scale, f"{label}: the zoom is larger than the capture", zoom["scale"])
    box = [round(v) for v in region]
    crop = _img(full["image_path"]).crop((box[0], box[1], box[0] + box[2], box[1] + box[3]))
    back = _img(zoom["image_path"]).resize(crop.size)
    diff = mean_diff(crop, back)
    expect(diff < 8.0, f"{label}: the zoom shows what the full capture shows there", diff)


async def click_zoomed(tools, page, target: str, *, label: str) -> None:
    """Zoom around ``target`` and click its centre in the zoomed image's pixels."""
    full = await tools["screenshot"]()
    rect = await ev(page, RECT_JS, target)
    pad = 30.0
    around = [rect[0] - pad, rect[1] - pad, rect[2] + 2 * pad, rect[3] + 2 * pad]
    zoom = await tools["screenshot"](region=px_box(around, full["scale"]))
    ox, oy = zoom["origin"]
    cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    res = await tools["click"](x=(cx - ox) * zoom["scale"], y=(cy - oy) * zoom["scale"])
    expect(res.get("status") == "ok", f"{label}: click at a point of the zoom answers ok", res)
    got = (await ev(page, STATE_JS))["points"].get(target)
    expect(got is not None, f"{label}: the click reached the 4x4 px target", got)
    off = max(abs(got[0] - rect[2] / 2), abs(got[1] - rect[3] / 2))
    expect(off <= 1.0, f"{label}: it landed within 1 CSS px of its centre", (got, off))


async def refusals(tools) -> None:
    res = await tools["screenshot"](region=[0, 0, 100, 100])
    expect(res.get("status") == "error", "a region before any screenshot is refused", res)
    await tools["screenshot"]()
    res = await tools["screenshot"](region=[10, 10, 0, 50])
    expect(res.get("status") == "empty", "an empty region is refused", res)
    res = await tools["screenshot"](region=[99999, 10, 50, 50])
    expect(res.get("status") == "outside_viewport", "an off-screen region is refused", res)


async def detail(ctx, tools, page) -> None:
    """1 px stripes: blurred in a capture downscaled to 1000 px, resolved in the zoom."""
    edge = ctx.config.capture_max_edge
    ctx.config.capture_max_edge = 1000
    full = await tools["screenshot"]()
    region = px_box(await ev(page, RECT_JS, "stripes"), full["scale"])
    box = [round(v) for v in region]
    crop = _img(full["image_path"]).crop((box[0], box[1], box[0] + box[2], box[1] + box[3]))
    ctx.config.capture_max_edge = edge
    zoom = await tools["screenshot"](region=region)
    zimg = _img(zoom["image_path"])
    density = zoom["scale"] / full["scale"]
    expect(density >= 2.0, "the zoom has >= 2x the downscaled capture's density", density)
    seen_full = transitions(crop, crop.height // 2)
    seen_zoom = transitions(zimg, zimg.height // 2)
    expect(
        seen_zoom >= 200 and seen_zoom > 2 * seen_full,
        "1 px stripes blurred in the downscaled capture are resolved in the zoom",
        (seen_full, seen_zoom),
    )


async def hover_zoomed(tools, page) -> None:
    full = await tools["screenshot"]()
    rect = await ev(page, RECT_JS, "t1")
    around = [rect[0] - 20, rect[1] - 20, rect[2] + 40, rect[3] + 40]
    zoom = await tools["screenshot"](region=px_box(around, full["scale"]))
    ox, oy = zoom["origin"]
    hovers = (await ev(page, STATE_JS))["hovers"]
    # Away first (in the zoom's own pixels: its top-left corner), so entering is new.
    await tools["hover"](x=1, y=1)
    res = await tools["hover"](
        x=(rect[0] + 2 - ox) * zoom["scale"], y=(rect[1] + 2 - oy) * zoom["scale"]
    )
    expect(res.get("status") == "ok", "hover at a point of the zoom answers ok", res)
    expect((await ev(page, STATE_JS))["hovers"] > hovers, "the hover reached the target")


async def set_ratio(page, ratio: int) -> None:
    from lyra_browser import cursor

    await cursor.send_cdp(
        page,
        "Emulation.setDeviceMetricsOverride",
        {"width": 1280, "height": 800, "deviceScaleFactor": ratio, "mobile": False},
    )


async def run_mode(base: str, *, headless: bool) -> None:
    from lyra_browser.context import acquire_page

    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-zoom-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless, consent_channel="autonomous")
    print(f"\n[{mode}]")
    try:
        expect((await tools["open_browser"]()).get("status") == "ok", "browser launches")
        expect((await tools["navigate"](url=base))["status"] == "ok", "page loads")
        page, _ = await acquire_page(ctx)
        await set_ratio(page, 1)  # the same viewport headless and headful
        await refusals(tools)
        await region_matches_full(tools, page, label="ratio 1")
        await detail(ctx, tools, page)
        await click_zoomed(tools, page, "t1", label="ratio 1")
        await hover_zoomed(tools, page)
        await ev(page, "() => window.scrollTo(0, 1200)")
        await asyncio.sleep(0.2)
        await click_zoomed(tools, page, "t2", label="after scrolling")
        await ev(page, "() => window.scrollTo(0, 0)")
        await asyncio.sleep(0.2)
        await set_ratio(page, 2)
        await region_matches_full(tools, page, label="ratio 2")
        await click_zoomed(tools, page, "t1", label="ratio 2")
        await set_ratio(page, 1)
        muts = (await ev(page, STATE_JS))["muts"]
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
