"""A screenshot reaches a remote model as pixels, not as a path on another machine."""

from __future__ import annotations

import base64

from fastmcp import Client, FastMCP
from mcp.types import ImageContent

from lyra_browser.tools import register_all
from lyra_browser.vision import VisionMiddleware


async def _screenshot(ctx, *, vision: bool):
    mcp = FastMCP("test")
    register_all(mcp, ctx)
    if vision:
        mcp.add_middleware(VisionMiddleware())
    async with Client(mcp) as client:
        return await client.call_tool("screenshot", {}, raise_on_error=False)


async def test_screenshot_carries_the_png_it_wrote(make_ctx, page):
    result = await _screenshot(make_ctx(), vision=True)
    images = [c for c in result.content if isinstance(c, ImageContent)]
    assert len(images) == 1
    assert images[0].mimeType == "image/png"
    assert base64.b64decode(images[0].data) == page.png
    assert result.structured_content["image_path"]


async def test_without_the_middleware_the_result_is_a_path_only(make_ctx):
    result = await _screenshot(make_ctx(), vision=False)
    assert not any(isinstance(c, ImageContent) for c in result.content)
    assert result.structured_content["image_path"]


async def test_an_unreadable_capture_leaves_the_result_alone(tmp_path):
    """The file is gone by the time it is read: the path envelope stands, no error."""
    mcp = FastMCP("test")

    @mcp.tool(name="screenshot")
    def screenshot() -> dict:
        return {"status": "ok", "image_path": str(tmp_path / "gone.png")}

    mcp.add_middleware(VisionMiddleware())
    async with Client(mcp) as client:
        result = await client.call_tool("screenshot", {})
    assert not any(isinstance(c, ImageContent) for c in result.content)
    assert result.structured_content["status"] == "ok"
