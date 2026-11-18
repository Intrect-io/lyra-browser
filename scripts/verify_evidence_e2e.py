#!/usr/bin/env python3
"""Exercise action evidence and visual stability against a real browser.

Same contract as the other ``verify_*_e2e.py`` gates (temporary profile, loopback site,
``build_tools``; ``LYRA_BROWSER_DRIVER`` picks the driver). Asserted:

- a click that changes nothing answers ``visual_change.changed: false``; one that shows a
  toast answers ``true`` with a ``region`` over the toast (and ``region_px`` on it in the
  latest screenshot's pixels, at scale 1 and downscaled); typing into a field is a change;
  with a named snapshot held, a change (not a no-op) carries ``compare_hint`` naming it;
- ``capture=true`` returns the after-image (file, scale), with the cursor at the click;
- ``wait_for(visual_stable=true)`` returns once a 700 ms animation has ended, and answers
  ``timeout`` (``stable: false``) within its budget on a page that never stops moving; a
  screenshot of that page says it was not settled;
- what the evidence adds to a click: median over N clicks with it on and off, bounded.
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
from verify_cursor_e2e import around, cursor_pixels  # noqa: E402

PAGE = """<!doctype html><html><head><title>evidence</title>
<style>body{margin:0;font-family:sans-serif;background:#fff}
button{position:absolute;font-size:15px;padding:6px 12px}
#noop{left:40px;top:40px} #toastb{left:160px;top:40px} #anim{left:300px;top:40px}
#spinon{left:420px;top:40px}
#in{position:absolute;left:40px;top:120px}
#t{display:none;position:fixed;right:40px;bottom:40px;width:220px;height:70px;background:#c22}
#box{position:absolute;left:40px;top:220px;width:60px;height:60px;background:#246;
 transition:transform 700ms linear}
#box.moved{transform:translateX(400px)}
#spin{display:none;position:absolute;left:600px;top:220px;width:60px;height:60px;
 background:#393;animation:sp 0.6s linear infinite}
@keyframes sp{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
</style></head><body>
<button id="noop">Nothing</button><button id="toastb">Toast</button>
<button id="anim">Animate</button><button id="spinon">Spin</button>
<input id="in" aria-label="Field"><div id="t"></div><div id="box"></div><div id="spin"></div>
<script>
document.getElementById('toastb').addEventListener('click', () =>
  setTimeout(() => { document.getElementById('t').style.display = 'block'; }, 30));
document.getElementById('anim').addEventListener('click', () =>
  document.getElementById('box').classList.add('moved'));
document.getElementById('spinon').addEventListener('click', () => {
  document.getElementById('spin').style.display = 'block'; });
</script></body></html>"""

N_LATENCY = 20
MAX_ADDED_MS = 150
RECT_JS = (
    "(el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }"
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


def intersects(a, b) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


async def ab_click_ms(ctx, tools) -> tuple[float, float]:
    """Median click time with evidence on and off, measured in alternation.

    Interleaved rather than one block after the other, so a host whose load changes
    during the run (other jobs, a CodeQL scan) weighs on both arms alike.
    """
    on, off = [], []
    for _ in range(N_LATENCY):
        for flag, times in ((False, off), (True, on)):
            ctx.config.evidence = flag
            started = time.perf_counter()
            res = await tools["click"](selector="#noop")
            times.append((time.perf_counter() - started) * 1000)
            assert res.get("status") == "ok", res
    ctx.config.evidence = True
    return statistics.median(on), statistics.median(off)


def iou(a, b) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix = max(0.0, min(ax0 + aw, bx0 + bw) - max(ax0, bx0))
    iy = max(0.0, min(ay0 + ah, by0 + bh) - max(ay0, by0))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union else 0.0


async def toast_in_pixels(tools, page, base: str, *, label: str) -> dict:
    """Reload, screenshot, show the toast: ``region_px`` must sit on it in that image."""
    expect((await tools["navigate"](url=base))["status"] == "ok", f"{label}: page loads again")
    shot = await tools["screenshot"]()
    res = await tools["click"](selector="#toastb")
    vc = res.get("visual_change") or {}
    toast = await page.locator("#t").first.evaluate(RECT_JS)
    want = [v * shot["scale"] for v in toast]
    got = vc.get("region_px")
    score = iou(got, want) if got else 0.0
    expect(
        score >= 0.5,
        f"{label}: region_px is on the toast in the screenshot (IoU >= 0.5)",
        (got, want, score),
    )
    return res


async def next_look(ctx, tools, page, base: str) -> None:
    """region_px at scale 1 and downscaled; compare_hint only with a snapshot and a change."""
    res = await toast_in_pixels(tools, page, base, label="scale 1")
    expect("compare_hint" not in res, "no snapshot held, no compare hint", res)
    edge = ctx.config.capture_max_edge
    ctx.config.capture_max_edge = 1000
    await toast_in_pixels(tools, page, base, label="downscaled to 1000 px")
    ctx.config.capture_max_edge = edge

    expect((await tools["navigate"](url=base))["status"] == "ok", "the page loads again")
    await tools["screenshot"](name="before")
    res = await tools["click"](selector="#noop")
    expect("compare_hint" not in res, "a snapshot held but no change: no hint", res)
    res = await tools["click"](selector="#toastb")
    expect(
        res.get("snapshots") == ["before"] and 'compare_to="before"' in res.get("compare_hint", ""),
        "a change with a snapshot held names it and hints to compare",
        res,
    )


async def run_mode(base: str, *, headless: bool) -> None:
    from lyra_browser.context import acquire_page

    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-evidence-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=headless, consent_channel="autonomous")
    print(f"\n[{mode}]")
    try:
        expect((await tools["open_browser"]()).get("status") == "ok", "browser launches")
        expect((await tools["navigate"](url=base))["status"] == "ok", "page loads")
        page, _ = await acquire_page(ctx)

        res = await tools["click"](selector="#noop")
        vc = res.get("visual_change") or {}
        expect(vc.get("changed") is False, "a click that changes nothing says so", vc)

        res = await tools["click"](selector="#toastb")
        vc = res.get("visual_change") or {}
        toast = await page.locator("#t").first.evaluate(RECT_JS)
        expect(vc.get("changed") is True, "a click that shows a toast is a change", vc)
        expect(
            vc.get("region") is not None and intersects(vc["region"], toast),
            "the changed region covers the toast",
            (vc.get("region"), toast),
        )

        res = await tools["type_text"](selector="#in", value="hello")
        vc = res.get("visual_change") or {}
        expect(vc.get("changed") is True, "typing into a field is a change", vc)

        res = await tools["click"](selector="#noop", capture=True)
        expect(
            Path(res.get("image_path", "/nonexistent")).is_file() and res["capture"]["scale"] > 0,
            "capture=true returns the after-image and its scale",
            res,
        )
        noop = await page.locator("#noop").first.evaluate(RECT_JS)
        point = (noop[0] + noop[2] / 2, noop[1] + noop[3] / 2)
        scale = res["capture"]["scale"]
        box = tuple(round(v * scale) for v in around(point))
        n = cursor_pixels(Path(res["image_path"]).read_bytes(), box)
        expect(n >= 40, "the after-image shows the cursor where it clicked", n)

        await next_look(ctx, tools, page, base)

        await tools["click"](selector="#anim")
        res = await tools["wait_for"](visual_stable=True, timeout_ms=4000)
        expect(
            res.get("status") == "ok" and res.get("stable") is True,
            "wait_for(visual_stable) returns once the animation ended",
            res,
        )
        expect(res["waited_ms"] < 2500, "and within its budget", res["waited_ms"])

        ctx.config.cursor = False
        on_ms, off_ms = await ab_click_ms(ctx, tools)
        added = on_ms - off_ms
        print(
            f"[{mode}] click median over {N_LATENCY}: evidence on {on_ms:.0f} ms, "
            f"off {off_ms:.0f} ms, added {added:.0f} ms"
        )
        expect(added <= MAX_ADDED_MS, f"evidence adds at most {MAX_ADDED_MS} ms per click", added)

        await tools["click"](selector="#spinon")
        res = await tools["wait_for"](visual_stable=True, timeout_ms=1000)
        expect(
            res.get("status") == "timeout" and res.get("stable") is False,
            "a page that never stops moving is not called stable",
            res,
        )
        expect(res["waited_ms"] <= 1600, "and the wait keeps to its budget", res["waited_ms"])
        started = time.perf_counter()
        shot = await tools["screenshot"]()
        took = time.perf_counter() - started
        expect(shot.get("settled") is False, "a screenshot of it says it was not settled", shot)
        expect(took < 2.5, "and does not wait past its own budget", took)
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
