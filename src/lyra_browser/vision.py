"""Attach capture PNGs to tool results as MCP image blocks."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent

IMAGE_TOOLS: frozenset[str] = frozenset({"screenshot", "read_image"})


def payload_of(result: ToolResult) -> Any:
    """The envelope a tool returned: structured content, else its first text block."""
    if isinstance(result.structured_content, dict):
        return result.structured_content
    for block in result.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


def image_block(path: str) -> ImageContent | None:
    """The PNG at ``path`` as an image block, or None if it cannot be read."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return ImageContent(
        type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/png"
    )


class VisionMiddleware(Middleware):
    """Attaches the PNG of ``screenshot``/``read_image`` results as an image block.

    A client that renders MCP images then shows the model the page, rather than
    a path on a machine it cannot read.
    """

    async def on_call_tool(self, context: MiddlewareContext, call_next) -> ToolResult:
        result = await call_next(context)
        if context.message.name in IMAGE_TOOLS:
            payload = payload_of(result)
            if isinstance(payload, dict):
                path = payload.get("image_path")
                block = image_block(str(path)) if path else None
                if block is not None:
                    result.content = [*(result.content or []), block]
        return result
