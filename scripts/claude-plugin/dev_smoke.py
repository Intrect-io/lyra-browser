"""Headless smoke through whatever ``lyra_browser`` the running interpreter imports.

Run with ``python -I`` so the import is the installed package, not a checkout. A loopback
page, a throwaway data dir: open_browser, navigate, read_page, screenshot, close_browser.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = b"<!doctype html><title>lyra smoke</title><h1>lyra smoke page</h1>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server API
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


async def main() -> int:
    from fastmcp import FastMCP

    from lyra_browser.approval import CollaborationState
    from lyra_browser.audit import AuditLog
    from lyra_browser.config import Config
    from lyra_browser.context import ServerContext
    from lyra_browser.session import BrowserSession
    from lyra_browser.tools import register_all

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    with tempfile.TemporaryDirectory(prefix="lyra-smoke-", ignore_cleanup_errors=True) as tmp:
        cfg = Config(data_dir=Path(tmp), headless=True, consent_channel="autonomous")
        cfg.capture_dir = Path(tmp) / "captures"
        ctx = ServerContext(
            config=cfg,
            session=BrowserSession(cfg),
            audit=AuditLog(cfg.audit_path),
            collab=CollaborationState(require_approval=cfg.require_approval),
        )
        mcp = FastMCP("lyra-smoke")
        register_all(mcp, ctx)
        tools = {t.name: t.fn for t in await mcp.list_tools()}
        try:
            steps = [
                ("open_browser", await tools["open_browser"]()),
                ("navigate", await tools["navigate"](url=url)),
            ]
            text = await tools["read_page"]()
            steps.append(("read_page", text))
            shot = await tools["screenshot"]()
            steps.append(("screenshot", shot))
        finally:
            closed = await tools["close_browser"]()
            server.shutdown()
            server.server_close()
        steps.append(("close_browser", closed))
        for name, result in steps:
            # read_page answers its fields without a status; the others say "ok".
            if not isinstance(result, dict) or result.get("status", "ok") != "ok":
                print(f"FAIL  {name}: {result}", file=sys.stderr)
                return 1
        if "lyra smoke page" not in str(text.get("text", text)):
            print(f"FAIL  read_page did not read the page: {text}", file=sys.stderr)
            return 1
        if not Path(shot["image_path"]).is_file() or shot.get("bytes", 0) <= 0:
            print(f"FAIL  screenshot wrote no image: {shot}", file=sys.stderr)
            return 1
    print("PASS  open, navigate, read, screenshot, close", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
