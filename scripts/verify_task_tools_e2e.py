#!/usr/bin/env python3
"""Real-browser gate for the task tools: drag, evaluate_readonly, read_download,
save_deliverable and set_emulation.

Runs the server's own tools over a real session (``build_tools``) against a loopback
site, on the driver ``LYRA_BROWSER_DRIVER`` names (native by default):

- drag: a slider that follows ``pointermove`` reaches the end; an HTML5 drag-and-drop
  target is measured (a NOTE, not a failure: raw pointer input does not start an HTML5
  drag on every driver)
- evaluate_readonly: reads answer; ``document.title = ...``, a DOM append and ``fetch``
  are refused by Chrome itself, and the title is unchanged afterwards; storage names are
  refused before Chrome is asked
- read_download: a CSV saved by ``click(download=true)`` is read back
- save_deliverable: the file is where the reply says, and not written twice
- set_emulation: locale, time zone and position reach the page, survive a navigation
  and reach a tab opened afterwards

    python scripts/verify_task_tools_e2e.py
    LYRA_BROWSER_DRIVER=playwright python scripts/verify_task_tools_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(SCRIPTS))

from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

SLIDER = """<!doctype html><title>slider</title>
<style>#track{position:relative;width:400px;height:40px;background:#ddd;margin:40px}
#knob{position:absolute;left:0;top:0;width:40px;height:40px;background:#36c;touch-action:none}
#end{position:absolute;right:0;top:0;width:40px;height:40px;background:#c63}</style>
<div id=track><div id=knob></div><div id=end></div></div><p id=out>0</p>
<script>
const knob = document.getElementById('knob'), track = document.getElementById('track');
let held = false;
knob.addEventListener('pointerdown', e => { held = true; knob.setPointerCapture(e.pointerId); });
knob.addEventListener('pointermove', e => {
  if (!held) return;
  const r = track.getBoundingClientRect();
  const x = Math.max(0, Math.min(360, e.clientX - r.left - 20));
  knob.style.left = x + 'px';
  document.getElementById('out').textContent = Math.round(x / 3.6);
});
knob.addEventListener('pointerup', () => {
  held = false; document.title = 'slider:' + document.getElementById('out').textContent;
});
</script>"""

DND = """<!doctype html><title>dnd</title>
<div id=card draggable=true style="width:80px;height:40px;background:#9c6">card</div>
<div id=bin style="width:200px;height:120px;margin-top:40px;background:#eee">bin</div>
<script>
const bin = document.getElementById('bin');
bin.addEventListener('dragover', e => e.preventDefault());
bin.addEventListener('drop', e => { e.preventDefault(); document.title = 'dropped'; });
</script>"""

READ = """<!doctype html><title>read me</title><ul><li>a</li><li>b</li><li>c</li></ul>
<script>window.appState = {plan: 'Studio', price: 29};</script>
<a id=csv href="/files/prices.csv">prices</a>"""

GEO = """<!doctype html><title>geo</title><p id=out>waiting</p>
<script>
navigator.geolocation.getCurrentPosition(
  p => { document.title = 'geo:' + p.coords.latitude.toFixed(2) + ',' +
                          p.coords.longitude.toFixed(2); },
  e => { document.title = 'geo-error:' + e.code; });
</script>"""

PAGES = {"/slider": SLIDER, "/dnd": DND, "/read": READ, "/geo": GEO, "/other": "<title>o</title>"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/files/prices.csv":
            body = b"plan,price\nStudio,29\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", 'attachment; filename="prices.csv"')
        else:
            body = PAGES.get(self.path, "<title>404</title>").encode()
            self.send_response(200 if self.path in PAGES else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


async def title_of(tools, expected_prefix: str, seconds: float = 5.0) -> str:
    deadline = asyncio.get_running_loop().time() + seconds
    title = ""
    while asyncio.get_running_loop().time() < deadline:
        reply = await tools["evaluate_readonly"](expression="document.title")
        title = json.loads(reply.get("result", '""')) if reply["status"] == "ok" else ""
        if title.startswith(expected_prefix):
            break
        await asyncio.sleep(0.2)
    return title


async def main() -> None:
    from lyra_browser.origin import parse_origin
    from lyra_browser.permission import Capability

    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    driver = os.environ.get("LYRA_BROWSER_DRIVER", "auto")
    with tempfile.TemporaryDirectory(prefix="lyra-task-tools-") as tmp:
        data_dir = Path(tmp)
        ctx, tools = await build_tools(data_dir, headless=True)
        site = parse_origin(base)
        try:
            opened = await tools["open_browser"]()
            expect(opened.get("status") == "ok", f"browser opens ({driver})", opened)
            ctx.perms.grant("default", site, Capability.NAVIGATE)

            # -- drag
            await tools["navigate"](url=f"{base}/slider")
            dragged = await tools["drag"](source="#knob", target="#end")
            expect(dragged["status"] == "ok", "drag answers ok", dragged)
            title = await title_of(tools, "slider:")
            expect(title in ("slider:100", "slider:99"), "the slider followed to the end", title)
            await tools["navigate"](url=f"{base}/dnd")
            await tools["drag"](source="#card", target="#bin")
            title = await title_of(tools, "dropped", seconds=2.0)
            print(f"NOTE  HTML5 drag-and-drop on {driver}: {'dropped' if title else 'no drop'}")

            # -- evaluate_readonly
            await tools["navigate"](url=f"{base}/read")
            ev = tools["evaluate_readonly"]
            count = await ev(expression="document.querySelectorAll('li').length")
            expect(count["status"] == "ok" and count["result"] == "3", "a read answers", count)
            state = await ev(expression="window.appState")
            expect(json.loads(state["result"]) == {"plan": "Studio", "price": 29}, "a global")
            for write in (
                "document.title = 'changed'",
                "document.body.appendChild(document.createElement('div'))",
                "fetch('/other')",
            ):
                refused = await ev(expression=write)
                expect(
                    refused["status"] == "refused_side_effect",
                    f"Chrome refuses a write: {write[:30]}",
                    refused,
                )
            title = await ev(expression="document.title")
            expect(json.loads(title["result"]) == "read me", "the title is unchanged", title)
            refused = await ev(expression="document.cookie")
            expect(refused["status"] == "refused_storage", "storage is left to `storage`")

            # -- read_download
            ctx.perms.grant("default", site, Capability.DOWNLOAD)
            saved = await tools["click"](selector="#csv", download=True, confirm=True)
            expect(saved["status"] == "ok" and saved.get("download"), "the CSV is saved", saved)
            read = await tools["read_download"](filename="prices.csv")
            expect(read.get("text") == "plan,price\nStudio,29\n", "and read back", read)

            # -- save_deliverable
            out = await tools["save_deliverable"](name="answer.txt", content="Studio costs 29")
            expect(Path(out["path"]).read_text() == "Studio costs 29", "deliverable written", out)
            again = await tools["save_deliverable"](name="answer.txt", content="x")
            expect(again["status"] == "exists", "not written twice", again)

            # -- set_emulation
            emulated = await tools["set_emulation"](
                latitude=52.52, longitude=13.40, locale="de-DE", timezone="Europe/Berlin"
            )
            expect(emulated["status"] == "ok", "set_emulation answers ok", emulated)
            await tools["navigate"](url=f"{base}/geo")
            title = await title_of(tools, "geo")
            expect(title == "geo:52.52,13.40", "the page gets the position", title)
            loc = await ev(expression="Intl.DateTimeFormat().resolvedOptions().locale")
            expect(json.loads(loc["result"]) == "de-DE", "locale survives a navigation", loc)
            tz = await ev(expression="Intl.DateTimeFormat().resolvedOptions().timeZone")
            expect(json.loads(tz["result"]) == "Europe/Berlin", "time zone too", tz)
            page = await ctx.session.page()
            before = list(ctx.session.open_pages())
            await page.evaluate("void window.open('/other', '_blank')")
            popup = None
            for _ in range(50):
                fresh = [p for p in ctx.session.open_pages() if p not in before]
                if fresh:
                    popup = fresh[0]
                    break
                await asyncio.sleep(0.1)
            expect(popup is not None, "the new tab opened")
            await popup.wait_for_load_state()
            await asyncio.sleep(0.5)
            seen = await popup.evaluate("Intl.DateTimeFormat().resolvedOptions().locale")
            expect(seen == "de-DE", "a tab opened afterwards gets the locale", seen)
        finally:
            await ctx.session.stop()
            server.shutdown()
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
