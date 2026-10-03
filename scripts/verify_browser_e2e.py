#!/usr/bin/env python3
"""Exercise lyra-browser against a real installed browser.

This is an explicit distribution/runtime gate, not part of the unit suite. It
uses a temporary profile and a loopback test site, so the user's real browser
profile and network data are never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

HOME = """<!doctype html><title>home</title><h1>Browser E2E</h1>
<p id="visits"></p>
<form action="/submitted" method="post"><input id="secret" name="secret">
<button id="submit">Submit</button>
<button id="sneaky" type="button" onclick="document.querySelector('form').submit()">Sneaky</button>
</form>
<button id="plain" type="button" onclick="document.title='clicked'">Plain</button>
<input id="keys" aria-label="keys">
<script>
document.addEventListener('keydown', (e) => { document.body.dataset.lastKey = e.key; });
</script>
<a id="inside" href="/second">Second</a>
<script>
const visits = Number(localStorage.getItem('visits') || 0) + 1;
localStorage.setItem('visits', String(visits));
document.querySelector('#visits').textContent = `visits=${visits}`;
</script>"""


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
        pages = {
            "/": HOME,
            "/second": "<title>second</title><h1>Second</h1>",
            "/submitted": "<title>submitted</title><h1>Submitted</h1>",
        }
        if path not in pages:
            self.send_error(404)
            return
        self._send(pages[path])

    def do_POST(self) -> None:  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._send("<title>submitted</title><h1>Submitted</h1>")

    def log_message(self, *_args: object) -> None:
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def expect(condition: bool, message: str, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{message}: {detail}")
    print(f"PASS  {message}")


async def build_tools(data_dir: Path, *, headless: bool, **overrides):
    """The server's tools over a real session. ``overrides`` are extra ``Config`` fields
    (``enforcement_mode="observe"``, say) for a gate that needs a configuration of its own."""
    from fastmcp import FastMCP

    from lyra_browser.approval import CollaborationState
    from lyra_browser.audit import AuditLog
    from lyra_browser.config import Config
    from lyra_browser.context import ServerContext
    from lyra_browser.session import BrowserSession
    from lyra_browser.tools import register_all

    # LYRA_BROWSER_GUARD=cdp runs this gate, and every gate built on build_tools, against
    # the CDP guard backend instead of the default.
    backend = (
        {"guard_backend": os.environ["LYRA_BROWSER_GUARD"]}
        if "LYRA_BROWSER_GUARD" in os.environ
        else {}
    )
    cfg = Config(data_dir=data_dir, headless=headless, **backend, **overrides)
    # The gate promises to leave the user's data alone; with VEGA_DATA_DIR set
    # the capture dir would otherwise resolve into the real uploads root.
    cfg.capture_dir = data_dir / "captures"
    ctx = ServerContext(
        config=cfg,
        session=BrowserSession(cfg),
        audit=AuditLog(cfg.audit_path),
        collab=CollaborationState(require_approval=cfg.require_approval),
    )
    mcp = FastMCP("browser-e2e")
    register_all(mcp, ctx)
    tools = {tool.name: tool.fn for tool in await mcp.list_tools()}
    return ctx, tools


async def run_mode(base: str, alt_base: str, *, headless: bool) -> None:
    mode = "headless" if headless else "headful"
    # A TemporaryDirectory, not a bare mkdtemp: this gate is meant to be run
    # repeatedly, and mkdtemp leaks a Chrome profile per invocation — with the
    # audit log and the typed fixture value inside it.
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-browser-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    ctx, tools = await build_tools(data_dir, headless=headless)
    print(f"\n[{mode}] data_dir={data_dir}")
    try:
        opened = await tools["open_browser"]()
        expect(opened.get("status") == "ok", "browser launches", opened)
        expect(opened.get("attended") is not headless, "attendance is reported", opened)

        refused = await tools["navigate"](url=base)
        expect(refused.get("status") == "needs_approval", "first site requires approval", refused)
        entered = await tools["navigate"](url=base, reason="local E2E", confirm=True)
        expect(entered.get("title") == "home", "approved navigation reaches the page", entered)

        page_text = await tools["read_page"]()
        expect("Browser E2E" in page_text.get("text", ""), "rendered page text is readable")

        clicked = await tools["click"](selector="#plain")
        expect(clicked.get("status") == "ok", "ordinary interaction succeeds", clicked)
        expect((await tools["get_url"]()).get("title") == "clicked", "click changes the real DOM")

        await tools["click"](selector="#submit")
        await (await ctx.session.page()).wait_for_timeout(250)
        expect(
            (await tools["get_url"]()).get("title") != "submitted",
            "undeclared submission is blocked at the request",
        )

        submitted = await tools["type_text"](
            selector="#secret",
            value="e2e-secret-value",
            submit=True,
            reason="submit the local fixture",
            confirm=True,
        )
        expect(submitted.get("status") == "ok", "declared submission executes", submitted)
        await (await ctx.session.page()).wait_for_timeout(250)
        expect((await tools["get_url"]()).get("title") == "submitted", "form really submitted")

        await tools["navigate"](url=base, confirm=True)
        cross = await tools["navigate"](url=alt_base)
        expect(cross.get("status") == "needs_approval", "cross-origin navigation is gated", cross)

        shot = await tools["screenshot"]()
        shot_path = Path(shot.get("image_path", ""))
        png = shot_path.read_bytes() if shot_path.is_file() else b""
        expect(png.startswith(b"\x89PNG\r\n\x1a\n"), "screenshot is a real PNG file", shot)
        expect(
            shot_path.parent == Path(data_dir) / "captures" and "base64" not in shot,
            "screenshot is handed over as a path, not as text",
            shot,
        )
        inline = await tools["screenshot"](inline=True)
        expect(
            base64.b64decode(inline.get("base64", "")) == Path(inline["image_path"]).read_bytes(),
            "inline base64 is the same file",
        )
        picture = await tools["read_image"](selector="#plain")
        pic_path = Path(picture.get("image_path", ""))
        pic = pic_path.read_bytes() if pic_path.is_file() else b""
        expect(
            pic.startswith(b"\x89PNG\r\n\x1a\n") and pic_path.name.startswith("element-"),
            "read_image captures one element to a file",
            picture,
        )
        expect(
            (await tools["read_image"](selector="#does-not-exist")).get("status") == "not_found",
            "read_image reports a missing element",
        )

        if headless:
            for name, kwargs in (
                ("highlight_element", {"selector": "#plain"}),
                ("ask_user_to_do", {"instruction": "inspect the fixture"}),
                ("request_takeover", {"reason": "E2E"}),
            ):
                result = await tools[name](**kwargs)
                expect(result.get("status") == "unattended", f"{name} refuses unattended use")
            expect(not ctx.collab.takeover, "unattended takeover leaves no lock")
        else:
            expect(
                (await tools["highlight_element"](selector="#plain")).get("status") == "ok",
                "highlight reaches the visible page",
            )
            expect(
                (await tools["ask_user_to_do"](instruction="inspect", selector="#plain")).get(
                    "status"
                )
                == "awaiting_user",
                "attended session can ask the user",
            )
            expect(
                (await tools["request_takeover"](reason="E2E handoff")).get("status")
                == "takeover_active",
                "takeover activates",
            )
            expect(
                (await tools["navigate"](url=base + "second")).get("status") == "takeover_active",
                "agent mutations stop during takeover",
            )
            expect(
                (await tools["resume_after_takeover"](reason="E2E handback")).get("status") == "ok",
                "control returns to the agent",
            )

        # A key press reaches the page's own listener, and is reported as sent.
        pressed = await tools["press_key"](key="Escape", reason="E2E key press")
        expect(pressed.get("status") == "ok", "press_key is accepted", pressed)
        last_key = await (await ctx.session.page()).evaluate("document.body.dataset.lastKey")
        expect(last_key == "Escape", "press_key reaches the page's keydown listener", last_key)

        # History and reload act on the real document: the fixture counts its loads
        # in localStorage, so a reload is visible as one more visit.
        await tools["navigate"](url=base + "second")
        back = await tools["go_back"]()
        expect(
            back.get("status") == "ok" and back.get("url") == base and "http_status" in back,
            "go_back returns to the previous page",
            back,
        )
        before = (await tools["read_page"]()).get("text", "")
        reloaded = await tools["reload_page"]()
        after = (await tools["read_page"]()).get("text", "")
        expect(reloaded.get("status") == "ok", "reload_page is accepted", reloaded)
        counted = [re.search(r"visits=(\d+)", text) for text in (before, after)]
        visits = [int(found.group(1)) for found in counted if found]
        expect(
            len(visits) == 2 and visits[1] == visits[0] + 1,
            "reload_page loads the document again",
            (before, after),
        )

        audit_raw = ctx.config.audit_path.read_text()
        audit_rows = [json.loads(line) for line in audit_raw.splitlines() if line]
        expect(len(audit_rows) >= 10, "audit trail contains the real run", len(audit_rows))
        expect("e2e-secret-value" not in audit_raw, "typed values are absent from the audit")
        expect(
            any(
                row.get("tool") == "navigation" and row.get("status") == "denied"
                for row in audit_rows
            ),
            "policy refusal is auditable",
        )

        # Last: closing ends the session and drops what it earned, so nothing after
        # this point could run against a live window.
        closed = await tools["close_browser"](reason="E2E finished")
        expect(closed.get("status") == "ok", "close_browser is accepted", closed)
        expect(not ctx.session.live, "close_browser really closes the window")
        expect(
            any(
                row.get("tool") == "close_browser"
                for row in (
                    json.loads(line)
                    for line in ctx.config.audit_path.read_text().splitlines()
                    if line
                )
            ),
            "close_browser is audited",
        )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool) -> None:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    alt_base = f"http://localhost:{port}/"
    try:
        if not headful_only:
            await run_mode(base, alt_base, headless=True)
        if not headless_only:
            await run_mode(base, alt_base, headless=False)
    finally:
        server.shutdown()
        server.server_close()  # shutdown() stops serving but leaves the socket bound


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
