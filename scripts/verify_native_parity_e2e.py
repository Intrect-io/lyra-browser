#!/usr/bin/env python3
"""Compare the native driver's tree view with Playwright's, then act through its refs.

Same fixture page, read through lyra's tools on both drivers (``read_page(mode="tree")``):
the elements and their in-view order have to be the same. Then, on the native driver
only, refs are used the way a model uses them: a button in the top frame, in a same-site
iframe and in a cross-site iframe (an out-of-process frame), Korean typed into a field
of that cross-site frame, and a ref from before a navigation, which must answer
``not_found``.

Needs Google Chrome and ``playwright>=1.60`` (the baseline side only). Not part of the
unit suite: ``python scripts/verify_native_parity_e2e.py [--headful]``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastmcp import Client

from lyra_browser.config import Config
from lyra_browser.server import build_server

MAIN = """<!doctype html><title>Tree</title>
<h1>Tree page</h1>
<nav><a href="/next">Next page</a> <a href="/logo"><img alt="Logo" width="20" height="20"
  src="data:image/gif;base64,R0lGODlhAQABAIAAAP///wAAACwAAAAAAQABAAACAkQBADs="></a></nav>
<label>Email <input type="email"></label>
<select aria-label="Country"><option>Korea</option><option selected>Japan</option></select>
<label><input type="checkbox" checked> Remember me</label>
<button onclick="this.textContent='top hit'">Top button</button>
<button disabled>Disabled button</button>
<div role="tablist"><div role="tab" aria-selected="true">Tab one</div>
  <div role="tab" aria-selected="false">Tab two</div></div>
<h2>한국어 제목</h2>
<shadow-host></shadow-host>
<script>customElements.define('shadow-host', class extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML =
    '<button>Shadow button</button>'; } });</script>
<iframe src="/inner" title="same" width="400" height="80"></iframe>
<iframe src="http://localhost:CROSS/cross" title="cross" width="400" height="80"></iframe>
<div id="list"></div>
<script>for (let i = 0; i < 60; i++) { const b = document.createElement('button');
  b.textContent = 'Item ' + i; document.getElementById('list').appendChild(b); }</script>"""
INNER = """<!doctype html><button onclick="this.textContent='inner hit'">Inner button</button>"""
CROSS = """<!doctype html>
<button id="cb" onclick="this.textContent='cross hit'">Cross button</button>
<input aria-label="Cross field" onkeydown="if(event.key==='Enter')
  document.getElementById('cb').textContent='typed:'+this.value">"""


def serve(pages: dict) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            body = pages.get(self.path.split("?")[0], "<title>other</title>other")
            body = body() if callable(body) else body
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


_REF = re.compile(r"^aria-ref=\S+ ")


async def tree_for(driver: str, url: str, keep_open: bool = False):
    data = Path(tempfile.mkdtemp(prefix=f"lyra-tree-{driver}-"))
    cfg = Config(
        client="generic",
        headless="--headful" not in sys.argv,
        driver=driver,
        guard_backend="cdp",
        consent_channel="off",
    )
    cfg.data_dir = data
    cfg.__post_init__()
    cfg.consent_channel = "off"
    client = Client(build_server(cfg))
    await client.__aenter__()

    async def call(tool: str, **args):
        res = await client.call_tool(tool, args, raise_on_error=False)
        return res.structured_content or {
            "text": [c.text for c in res.content if hasattr(c, "text")]
        }

    await call("open_browser")
    await call("navigate", url=url, reason="parity", wait_until="load")
    await asyncio.sleep(0.8)
    tree = await call("read_page", mode="tree", max_chars=200000)
    if not keep_open:
        await call("close_browser", reason="parity")
        await client.__aexit__(None, None, None)
        return tree, None, None
    return tree, call, client


async def main() -> int:
    cross = serve({"/cross": CROSS})
    site = serve(
        {"/": lambda: MAIN.replace("CROSS", str(cross.server_address[1])), "/inner": INNER}
    )
    url = f"http://127.0.0.1:{site.server_address[1]}/"
    failures: list[str] = []

    def check(name: str, ok: bool, detail) -> None:
        if not ok:
            failures.append(name)
        print(
            ("PASS " if ok else "FAIL ")
            + name
            + " :: "
            + json.dumps(detail, ensure_ascii=False, default=str)[:400]
        )

    pw_tree, _, _ = await tree_for("playwright", url)
    nat_tree, call, client = await tree_for("native", url, keep_open=True)
    pw_lines = [_REF.sub("", ln) for ln in pw_tree.get("text", "").splitlines()]
    nat_lines = [_REF.sub("", ln) for ln in nat_tree.get("text", "").splitlines()]
    only_pw = Counter(pw_lines) - Counter(nat_lines)
    only_nat = Counter(nat_lines) - Counter(pw_lines)
    check(
        "tree_elements_match",
        not only_pw and not only_nat,
        {
            "playwright": len(pw_lines),
            "native": len(nat_lines),
            "only_playwright": list(only_pw.elements()),
            "only_native": list(only_nat.elements()),
        },
    )
    check(
        "tree_refs_on",
        nat_tree.get("refs") is True and pw_tree.get("refs") is True,
        {"pw": pw_tree.get("refs"), "native": nat_tree.get("refs")},
    )
    check(
        "in_viewport_count_matches",
        pw_tree.get("in_viewport") == nat_tree.get("in_viewport"),
        {"pw": pw_tree.get("in_viewport"), "native": nat_tree.get("in_viewport")},
    )
    first_pw = [ln for ln in pw_lines[: pw_tree.get("in_viewport") or 0]]
    first_nat = [ln for ln in nat_lines[: nat_tree.get("in_viewport") or 0]]
    check(
        "in_viewport_order_matches",
        first_pw == first_nat,
        {"pw": first_pw[:6], "native": first_nat[:6]},
    )

    def ref_of(text: str, needle: str) -> str:
        for ln in text.splitlines():
            if needle in ln:
                return ln.split(" ", 1)[0].removeprefix("aria-ref=")
        return ""

    text = nat_tree.get("text", "")
    for label, needle, after in (
        ("ref_click_top", 'button "Top button"', 'button "top hit"'),
        ("ref_click_same_site_iframe", 'button "Inner button"', 'button "inner hit"'),
        ("ref_click_cross_site_oopif", 'button "Cross button"', 'button "cross hit"'),
    ):
        ref = ref_of(text, needle)
        r = await call("click", selector=f"aria-ref={ref}", reason="parity")
        tree = await call("read_page", mode="tree", max_chars=200000)
        check(
            label,
            r.get("status") == "ok" and after in tree.get("text", ""),
            {"ref": ref, "click": r},
        )
        text = tree.get("text", "")

    ref = ref_of(text, 'textbox "Cross field"')
    r = await call(
        "type_text", selector=f"aria-ref={ref}", value="안녕 OOPIF", submit=True, reason="parity"
    )
    tree = await call("read_page", mode="tree", max_chars=200000)
    check(
        "ref_type_korean_into_oopif",
        r.get("status") == "ok" and "typed:안녕 OOPIF" in tree.get("text", ""),
        {"ref": ref, "type": r},
    )

    stale = ref_of(tree.get("text", ""), "Item 5")
    await call("navigate", url=url + "other", reason="parity", wait_until="load")
    r = await call("click", selector=f"aria-ref={stale}", reason="parity", timeout_ms=1500)
    check("stale_ref_not_found", r.get("status") == "not_found", r)

    await call("close_browser", reason="parity")
    await client.__aexit__(None, None, None)
    site.shutdown()
    cross.shutdown()
    print("\nFAILED:" if failures else "\nALL PASSED", failures or "")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
