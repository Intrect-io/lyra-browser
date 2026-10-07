"""Drive a deployed lyra-browser over Streamable HTTP, the way a remote model would.

    # local container (docker run -p 8765:8765 lyra-browser:cf)
    .venv/bin/python scripts/verify_remote_e2e.py

    # through the Worker: wrangler dev, or the deployed *.workers.dev URL
    .venv/bin/python scripts/verify_remote_e2e.py \
        --url https://lyra-browser.<subdomain>.workers.dev/mcp \
        --token "$LYRA_MCP_TOKEN" --expect-auth

The client advertises no elicitation, so consent falls back to the model's
``confirm=true``, exactly as it does for any remote client that cannot be
asked. Exit status is the verdict: 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.types import ImageContent

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: Any = "") -> bool:
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if not ok and detail else "")
        )
        if not ok:
            self.failed += 1
        return ok


async def call(client: Client, tool: str, **args: Any) -> dict:
    result = await client.call_tool(tool, args, raise_on_error=False)
    data = result.structured_content
    return data if isinstance(data, dict) else {"status": "no_envelope", "raw": result.content}


async def run(args: argparse.Namespace) -> int:
    checks = Checks()

    if args.expect_auth:
        async with httpx.AsyncClient(timeout=30) as http:
            reply = await http.post(args.url, json={})
        checks.check(
            "unauthenticated request is refused",
            reply.status_code == 401 and "Bearer" in reply.headers.get("www-authenticate", ""),
            f"status={reply.status_code}",
        )

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else None
    transport = StreamableHttpTransport(args.url, headers=headers)
    async with Client(transport, timeout=args.timeout) as client:
        tools = {tool.name for tool in await client.list_tools()}
        checks.check("tools listed", {"open_browser", "navigate", "screenshot"} <= tools, tools)

        opened = await call(client, "open_browser")
        checks.check(
            "open_browser: headless session",
            opened.get("status") == "ok" and opened.get("attended") is False,
            opened,
        )

        nav = await call(client, "navigate", url=args.site, reason="remote smoke")
        if nav.get("status") == "needs_approval":
            nav = await call(client, "navigate", url=args.site, reason="remote smoke", confirm=True)
        checks.check("navigate", nav.get("status") == "ok", nav)

        page = await call(client, "read_page")
        checks.check(
            "read_page returns the page",
            args.expect_text in f"{page.get('title', '')} {page.get('text', '')}",
            page,
        )

        shot = await client.call_tool("screenshot", {}, raise_on_error=False)
        images = [c for c in shot.content if isinstance(c, ImageContent)]
        png = base64.b64decode(images[0].data) if images else b""
        checks.check(
            "screenshot arrives as a PNG image block",
            len(images) == 1
            and images[0].mimeType == "image/png"
            and png.startswith(PNG_SIGNATURE),
            shot.structured_content,
        )

        closed = await call(client, "close_browser")
        checks.check("close_browser", closed.get("status") == "ok", closed)

    print("ALL PASS" if not checks.failed else f"{checks.failed} FAILED")
    return 1 if checks.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--token", default=os.environ.get("LYRA_MCP_TOKEN"))
    parser.add_argument("--site", default="https://example.com")
    parser.add_argument("--expect-text", default="Example Domain")
    parser.add_argument("--expect-auth", action="store_true")
    parser.add_argument("--timeout", type=float, default=120.0)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
