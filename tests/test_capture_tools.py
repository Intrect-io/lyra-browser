"""``screenshot`` and ``read_image`` hand the model a file, not a base64 string.

Asserting on ``page.calls`` and on the file system, not only on the envelope:
a path in the result proves nothing if nothing was written there.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from conftest import FAKE_PNG


def _audit(ctx) -> list[dict]:
    return [json.loads(line) for line in Path(ctx.config.audit_path).read_text().splitlines()]


@pytest.mark.parametrize("full_page", [False, True])
async def test_screenshot_writes_a_file_and_returns_its_path(make_ctx, tools_of, page, full_page):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    result = await tools["screenshot"](full_page=full_page)

    assert result["status"] == "ok"
    assert ("screenshot", full_page) in page.calls
    path = Path(result["image_path"])
    assert path.parent == ctx.config.capture_dir
    assert path.read_bytes() == FAKE_PNG
    assert result["mime_type"] == "image/png"
    assert (result["width"], result["height"]) == (2, 3)
    assert "base64" not in result, "the default response must not carry the image as text"


async def test_screenshot_inline_is_opt_in(make_ctx, tools_of):
    tools = await tools_of(make_ctx())
    result = await tools["screenshot"](inline=True)
    assert base64.b64decode(result["base64"]) == FAKE_PNG
    assert Path(result["image_path"]).exists(), "inline adds to the file, it does not replace it"


async def test_screenshot_audit_records_the_path_not_the_pixels(make_ctx, tools_of):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    result = await tools["screenshot"]()
    entries = [e for e in _audit(ctx) if e["tool"] == "screenshot"]
    assert len(entries) == 1
    assert entries[0]["args"]["image_path"] == result["image_path"]
    assert entries[0]["args"]["bytes"] == len(FAKE_PNG)
    assert "base64" not in json.dumps(entries[0])


async def test_read_image_captures_the_element(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.present = {"#chart"}
    tools = await tools_of(ctx)
    result = await tools["read_image"](selector="#chart")

    assert result["status"] == "ok"
    assert ("element_screenshot", "#chart") in page.calls
    path = Path(result["image_path"])
    assert path.name.startswith("element-")
    assert path.read_bytes() == FAKE_PNG
    assert "base64" not in result


async def test_read_image_reports_a_missing_element_without_capturing(make_ctx, tools_of, page):
    ctx = make_ctx()
    page.present = {"#chart"}
    tools = await tools_of(ctx)
    result = await tools["read_image"](selector="#nope")

    assert result["status"] == "not_found"
    assert result["selector"] == "#nope"
    assert not any(call[0] == "element_screenshot" for call in page.calls)
    assert not list(ctx.config.capture_dir.glob("*.png"))
    entry = next(e for e in _audit(ctx) if e["tool"] == "read_image")
    assert entry["status"] == "not_found"


async def test_captures_are_reads_and_survive_takeover(make_ctx, tools_of, page):
    """Looking at the page is not acting on it: a user driving does not block it."""
    ctx = make_ctx()
    ctx.collab.takeover = True
    tools = await tools_of(ctx)
    assert (await tools["screenshot"]())["status"] == "ok"
    assert (await tools["read_image"](selector="img"))["status"] == "ok"


async def test_capture_dir_is_created_on_first_use(make_ctx, tools_of):
    ctx = make_ctx()
    assert not ctx.config.capture_dir.exists()
    tools = await tools_of(ctx)
    await tools["screenshot"]()
    assert ctx.config.capture_dir.is_dir()


async def test_captures_are_pruned_to_keep(make_ctx, tools_of):
    ctx = make_ctx()
    ctx.config.capture_keep = 2
    tools = await tools_of(ctx)
    for _ in range(4):
        await tools["screenshot"]()
    assert len(list(ctx.config.capture_dir.glob("*.png"))) == 2


async def test_screenshot_retries_until_chrome_has_a_frame(make_ctx, tools_of, page, monkeypatch):
    """Chrome 152 headless refuses the first capture after a navigation; measured, not
    imagined. The tool waits it out instead of handing the model an error."""
    from lyra_browser import capture

    monkeypatch.setattr(capture, "_RETRY_DELAY_S", 0.0)
    page.frames_missing = 2
    tools = await tools_of(make_ctx())
    result = await tools["screenshot"]()
    assert result["status"] == "ok"
    assert page.calls.count(("screenshot", False)) == 3


async def test_screenshot_gives_up_when_nothing_ever_paints(make_ctx, tools_of, page, monkeypatch):
    from lyra_browser import capture

    monkeypatch.setattr(capture, "_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(capture, "_RETRY_BUDGET_S", 0.0)
    page.frames_missing = 10
    tools = await tools_of(make_ctx())
    with pytest.raises(RuntimeError, match="Unable to capture screenshot"):
        await tools["screenshot"]()


async def test_other_capture_errors_are_not_retried(make_ctx, tools_of, page, monkeypatch):
    async def closed(full_page: bool = False) -> bytes:
        page.calls.append(("screenshot", full_page))
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(page, "screenshot", closed)
    tools = await tools_of(make_ctx())
    with pytest.raises(RuntimeError, match="has been closed"):
        await tools["screenshot"]()
    assert page.calls.count(("screenshot", False)) == 1
