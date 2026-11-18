#!/usr/bin/env python3
"""Exercise named snapshots and before/after comparison against a real browser.

Same contract as the other ``verify_*_e2e.py`` gates (temporary profile, loopback site,
``build_tools``; ``LYRA_BROWSER_DRIVER`` picks the driver). Asserted on the page's own
geometry and on the pixels:

- ``compare_to`` a name never kept answers ``not_found``;
- a reload of an unchanged page compares as unchanged (ratio 0, no boxes);
- one tile moved by 3 CSS px, or recoloured slightly, gives exactly one box, overlapping
  that tile (IoU >= 0.5), outlined on the attached image;
- lyra's cursor moving between the two captures is not a change;
- a different scroll position or viewport answers ``mismatch`` and no boxes;
- snapshots are capped in count and gone after close_browser;
- the comparison's cost over N=20 is printed (p50).
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

TILES = "".join(f'<div class="tile" id="t{i}">Tile {i}</div>' for i in range(12))
PAGE = f"""<!doctype html><html><head><title>compare</title>
<style>body{{margin:0;padding:30px;background:#fff;font-family:sans-serif;height:2400px}}
#grid{{width:760px}}
.tile{{display:inline-block;width:160px;height:90px;margin:10px;background:#e5e7eb;
 border:1px solid #9ca3af;box-sizing:border-box;padding:8px;vertical-align:top}}
#quiet{{position:absolute;left:900px;top:500px;width:200px;height:120px;background:#fff}}
</style></head><body><div id="grid">{TILES}</div><div id="quiet"></div></body></html>"""

RECT_JS = (
    "(id) => { const r = document.getElementById(id).getBoundingClientRect();"
    " return [r.left, r.top, r.width, r.height]; }"
)


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


def iou(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0.0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0.0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


def outline_pixels(path: str) -> int:
    from PIL import Image

    from lyra_browser.compare import BOX_RGB

    img = Image.open(path).convert("RGB")
    return sum(1 for p in img.getdata() if p == BOX_RGB)


async def reload(tools, base: str) -> None:
    res = await tools["navigate"](url=base)
    expect(res["status"] == "ok", "the page loads again", res)


async def one_change(tools, page, base: str, js: str, tile: str, *, label: str) -> None:
    await reload(tools, base)
    expect((await tools["screenshot"](name="before")).get("saved_as") == "before", "kept")
    await ev(page, js)
    await asyncio.sleep(0.15)
    res = await tools["screenshot"](compare_to="before")
    diff = res.get("diff") or {}
    boxes = diff.get("boxes") or []
    expect(diff.get("changed") is True and len(boxes) == 1, f"{label}: exactly one box", diff)
    rect = await ev(page, RECT_JS, tile)
    score = iou(boxes[0]["box_css"], rect)
    expect(score >= 0.5, f"{label}: the box is on the tile that changed (IoU >= 0.5)", score)
    expect(outline_pixels(res["image_path"]) >= 100, f"{label}: the box is outlined", res)


async def run_mode(base: str, *, headless: bool) -> None:
    from lyra_browser import cursor
    from lyra_browser.context import acquire_page

    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-compare-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless, consent_channel="autonomous")
    print(f"\n[{mode}]")
    try:
        expect((await tools["open_browser"]()).get("status") == "ok", "browser launches")
        await reload(tools, base)
        page, _ = await acquire_page(ctx)

        res = await tools["screenshot"](compare_to="nothing")
        expect(res.get("status") == "not_found", "comparing with an unknown name is refused", res)

        await tools["screenshot"](name="before")
        await reload(tools, base)
        page, _ = await acquire_page(ctx)
        diff = (await tools["screenshot"](compare_to="before")).get("diff") or {}
        expect(
            diff.get("changed") is False and diff.get("ratio") == 0 and diff.get("boxes") == [],
            "a reload of an unchanged page compares as unchanged",
            diff,
        )

        await one_change(
            tools,
            page,
            base,
            "() => { document.getElementById('t5').style.transform = 'translateX(3px)'; }",
            "t5",
            label="a tile moved by 3 px",
        )
        await one_change(
            tools,
            page,
            base,
            "() => { document.getElementById('t7').style.background = '#d4d8de'; }",
            "t7",
            label="a tile recoloured slightly",
        )

        await reload(tools, base)
        await tools["hover"](selector="#t0")
        expect(cursor.position(page) is not None, "the cursor is on screen")
        await tools["screenshot"](name="cursor")
        q = await ev(page, RECT_JS, "quiet")
        await tools["hover"](selector="#quiet")
        moved = cursor.position(page)
        expect(moved is not None and moved[0] > q[0], "the cursor moved", moved)
        diff = (await tools["screenshot"](compare_to="cursor")).get("diff") or {}
        expect(diff.get("changed") is False, "the cursor moving is not a change", diff)
        expect(cursor.position(page) is not None, "and it is back on screen afterwards")

        await ev(page, "() => window.scrollTo(0, 300)")
        await asyncio.sleep(0.15)
        res = await tools["screenshot"](compare_to="cursor")
        diff = res.get("diff") or {}
        expect(
            diff.get("mismatch") == "scroll" and "boxes" not in diff,
            "a different scroll position is a mismatch, not a diff",
            diff,
        )
        await ev(page, "() => window.scrollTo(0, 0)")
        await asyncio.sleep(0.15)
        await cursor.send_cdp(
            page,
            "Emulation.setDeviceMetricsOverride",
            {"width": 1000, "height": 800, "deviceScaleFactor": 1, "mobile": False},
        )
        diff = (await tools["screenshot"](compare_to="cursor")).get("diff") or {}
        expect(diff.get("mismatch") == "viewport", "a different viewport is a mismatch", diff)
        await cursor.send_cdp(
            page,
            "Emulation.setDeviceMetricsOverride",
            {"width": 1280, "height": 800, "deviceScaleFactor": 1, "mobile": False},
        )

        times, diffs = [], []
        await tools["screenshot"](name="cost")
        for _ in range(20):
            started = time.monotonic()
            res = await tools["screenshot"](compare_to="cost")
            times.append((time.monotonic() - started) * 1000)
            diffs.append(res["diff"]["ms"])
        print(
            f"MEASURE compare ({mode}, N=20): screenshot(compare_to) p50 "
            f"{statistics.median(times):.0f} ms, of which the diff p50 "
            f"{statistics.median(diffs):.0f} ms"
        )

        dropped = []
        for i in range(10):
            dropped += (await tools["screenshot"](name=f"n{i}")).get("dropped_snapshots", [])
        names = ctx.snapshots.names()
        expect(len(names) <= 8 and dropped, "snapshots are capped (oldest dropped)", names)

        await tools["close_browser"]()
        expect((await tools["open_browser"]()).get("status") == "ok", "the browser opens again")
        await reload(tools, base)
        res = await tools["screenshot"](compare_to="n9")
        expect(res.get("status") == "not_found", "snapshots are gone after close_browser", res)
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
