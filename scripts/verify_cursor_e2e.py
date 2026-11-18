#!/usr/bin/env python3
"""Exercise the virtual cursor against a real browser.

Same contract as the other ``verify_*_e2e.py`` gates (temporary profile, loopback site,
``build_tools`` from ``verify_browser_e2e``; ``LYRA_BROWSER_DRIVER`` picks the driver).
What it asserts, on what reached the page and on the pixels:

- a click lands once, after real ``pointermove``s along the way (>= 5);
- the cursor is in the next screenshot at the point it acted on (its own colour);
- the page never saw it: 0 main-world DOM mutations, the same node count, and
  ``read_page`` text and tree identical before and after it was drawn;
- hover and type_text move it too; after a navigation it is drawn again where it stood;
- headful: it is on the X display a person watches (``import -window root``);
- ``cursor=False`` (``LYRA_BROWSER_CURSOR=off``): no cursor pixels, no extra pointer moves;
- the time it adds to a click, median over N clicks, is printed and bounded.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect  # noqa: E402

PAGE = """<!doctype html><html><head><title>cursor</title>
<style>body{margin:0;background:#fff;font-family:sans-serif}
#go{position:absolute;left:420px;top:260px;width:140px;height:44px}
#menu{position:absolute;left:120px;top:120px;padding:8px}
#menu:hover{background:#ddd}
#name{position:absolute;left:700px;top:420px}
#next{position:absolute;left:200px;top:480px}</style></head><body>
<p id="t">Cursor test page</p>
<button id="go">Go</button>
<span id="menu">Menu</span>
<input id="name" aria-label="Name">
<a id="next" href="/two">Next page</a>
<script>
const s = {muts: 0, clicks: 0, moves: 0, hovers: 0};
window.__s = s;
new MutationObserver((m) => { s.muts += m.length; })
  .observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
document.getElementById('go').addEventListener('click', () => { s.clicks++; });
document.addEventListener('pointermove', () => { s.moves++; });
document.getElementById('menu').addEventListener('pointerenter', () => { s.hovers++; });
</script></body></html>"""

TWO = """<!doctype html><title>two</title><body style="margin:0;background:#fff">
<p>Second page</p><button id="b2" style="position:absolute;left:300px;top:300px">B</button>
</body>"""

STATE_JS = (
    "() => [window.__s.muts, window.__s.clicks, window.__s.moves, window.__s.hovers,"
    " document.querySelectorAll('*').length]"
)
CENTER_JS = (
    "(sel) => { const r = document.querySelector(sel).getBoundingClientRect();"
    " return [r.left + r.width / 2, r.top + r.height / 2]; }"
)

N_LATENCY = 20
MAX_ADDED_MS = 600


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        body = (TWO if self.path.startswith("/two") else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def decode_png(png: bytes) -> tuple[int, int, int, list[bytearray]]:
    """Width, height, bytes per pixel and rows of an 8-bit RGB(A) PNG."""
    pos, idat, w, h, bpp = 8, b"", 0, 0, 3
    while pos < len(png):
        (length,) = struct.unpack(">I", png[pos : pos + 4])
        kind, data = png[pos + 4 : pos + 8], png[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", data[:10])
            assert depth == 8, depth
            bpp = 4 if ctype == 6 else 3
        elif kind == b"IDAT":
            idat += data
        pos += 12 + length
    raw, stride, rows, prev, i = zlib.decompress(idat), w * bpp, [], bytearray(w * bpp), 0
    for _ in range(h):
        f, line = raw[i], bytearray(raw[i + 1 : i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(line)
        prev = line
    return w, h, bpp, rows


def cursor_pixels(png: bytes, box: tuple[int, int, int, int] | None = None) -> int:
    """Pixels of the cursor's fill colour (255, 45, 85), in ``box`` or anywhere."""
    w, h, bpp, rows = decode_png(png)
    x0, y0, x1, y1 = box or (0, 0, w, h)
    n = 0
    for y in range(max(0, y0), min(h, y1)):
        row = rows[y]
        for x in range(max(0, x0), min(w, x1)):
            r, g, b = row[x * bpp : x * bpp + 3]
            if r > 230 and g < 90 and 50 < b < 130:
                n += 1
    return n


def around(point) -> tuple[int, int, int, int]:
    x, y = int(point[0]), int(point[1])
    return (x - 4, y - 4, x + 24, y + 30)


async def ev(page, js: str, arg=None):
    """Evaluate in the page's own world (where its scripts keep their counters).

    The native driver evaluates in an isolated world unless told otherwise."""
    try:
        if arg is None:
            return await page.evaluate(js, world="main")
        return await page.evaluate(js, arg, world="main")
    except TypeError:
        return await (page.evaluate(js) if arg is None else page.evaluate(js, arg))


async def shot(tools) -> bytes:
    res = await tools["screenshot"]()
    assert res.get("status") == "ok", res
    return Path(res["image_path"]).read_bytes()


async def run_mode(base: str, *, headless: bool) -> None:
    from lyra_browser.context import acquire_page

    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-cursor-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless, consent_channel="autonomous")
    print(f"\n[{mode}] cursor on")
    try:
        expect((await tools["open_browser"]()).get("status") == "ok", "browser launches")
        expect((await tools["navigate"](url=base))["status"] == "ok", "page loads")
        page, _ = await acquire_page(ctx)
        text0 = await tools["read_page"]()
        tree0 = await tools["read_page"](mode="tree")
        before = await ev(page, STATE_JS)

        res = await tools["click"](selector="#go")
        expect(res.get("status") == "ok", "click answers ok", res)
        muts, clicks, moves, _, nodes = await ev(page, STATE_JS)
        expect(clicks == 1, "the click landed once", clicks)
        expect(moves - before[2] >= 5, "the pointer travelled (pointermove events)", moves)
        go = await ev(page, CENTER_JS, "#go")
        png = await shot(tools)
        n = cursor_pixels(png, around(go))
        expect(n >= 60, "the cursor is in the screenshot at the click point", n)
        expect(muts == 0, "the page saw no DOM mutation", muts)
        expect(nodes == before[4], "the node count did not change", (nodes, before[4]))
        text1 = await tools["read_page"]()
        tree1 = await tools["read_page"](mode="tree")
        expect(text1.get("text") == text0.get("text"), "read_page text is unchanged")
        expect(
            tree1.get("text") == tree0.get("text") and tree0.get("text"),
            "read_page tree is unchanged",
        )

        if not headless and shutil.which("import") and os.environ.get("DISPLAY"):
            grab = subprocess.run(
                ["import", "-window", "root", "png:-"], capture_output=True, timeout=30
            )
            n = cursor_pixels(grab.stdout)
            expect(n >= 60, "the cursor is on the X display a person watches", n)

        res = await tools["hover"](selector="#menu")
        expect(res.get("status") == "ok", "hover answers ok", res)
        hovers = (await ev(page, STATE_JS))[3]
        expect(hovers >= 1, "hover reached the element", hovers)
        menu = await ev(page, CENTER_JS, "#menu")
        n = cursor_pixels(await shot(tools), around(menu))
        expect(n >= 60, "after hover the cursor is on the hovered element", n)

        res = await tools["type_text"](selector="#name", value="Ada")
        expect(res.get("status") == "ok", "type_text answers ok", res)
        name = await ev(page, CENTER_JS, "#name")
        n = cursor_pixels(await shot(tools), around(name))
        expect(n >= 60, "type_text moved the cursor to the field", n)

        nxt = await ev(page, CENTER_JS, "#next")
        res = await tools["click"](selector="#next")
        expect(res.get("status") == "ok", "a navigating click answers ok", res)
        await tools["wait_for"](text="Second page")
        expect("/two" in page.url, "the click navigated", page.url)
        n = cursor_pixels(await shot(tools), around(nxt))
        expect(n >= 60, "after the navigation the cursor is drawn again where it stood", n)

        await tools["navigate"](url=base)
        on_ms = await latency(tools)
    finally:
        await tools["close_browser"]()
        tmp.cleanup()

    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-cursor-{mode}-off-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(
        Path(tmp.name), headless=headless, consent_channel="autonomous", cursor=False
    )
    print(f"[{mode}] cursor off")
    try:
        await tools["open_browser"]()
        await tools["navigate"](url=base)
        page, _ = await acquire_page(ctx)
        before = await ev(page, STATE_JS)
        res = await tools["click"](selector="#go")
        expect(res.get("status") == "ok", "off: click answers ok", res)
        _, clicks, moves, _, _ = await ev(page, STATE_JS)
        expect(clicks == 1, "off: the click landed once", clicks)
        expect(moves - before[2] <= 2, "off: no travelling pointer", moves - before[2])
        n = cursor_pixels(await shot(tools))
        expect(n == 0, "off: no cursor anywhere in the screenshot", n)
        off_ms = await latency(tools)
    finally:
        await tools["close_browser"]()
        tmp.cleanup()
    added = on_ms - off_ms
    print(
        f"[{mode}] click median over {N_LATENCY}: on {on_ms:.0f} ms, off {off_ms:.0f} ms, "
        f"added {added:.0f} ms"
    )
    expect(added <= MAX_ADDED_MS, f"the cursor adds at most {MAX_ADDED_MS} ms per click", added)


async def latency(tools) -> float:
    times = []
    for _ in range(N_LATENCY):
        started = time.perf_counter()
        res = await tools["click"](selector="#go")
        times.append((time.perf_counter() - started) * 1000)
        assert res.get("status") == "ok", res
    return statistics.median(times)


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
