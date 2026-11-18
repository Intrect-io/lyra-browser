"""Captures in page coordinates: scale, set-of-marks and coordinate actions.

Pixels and the page are checked by ``scripts/verify_visual_e2e.py``; these pin the
arithmetic and the gates on doubles.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from lyra_browser import marks
from lyra_browser.context import current_session_key
from lyra_browser.tools.reading import compact_snapshot


def _png(w: int, h: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


def test_scale_is_measured_from_the_image_not_the_ratio_the_page_reports():
    png, scale = marks.process(_png(1280, 800), css_width=1280, dpr=2.0, max_edge=0)
    assert scale == 1.0
    assert Image.open(io.BytesIO(png)).size == (1280, 800)


def test_a_capture_over_the_max_edge_is_downscaled_and_says_by_how_much():
    png, scale = marks.process(_png(2560, 1600), css_width=1280, max_edge=1568)
    assert Image.open(io.BytesIO(png)).size == (1568, 980)
    assert scale == pytest.approx(1568 / 1280)


def test_marks_are_drawn_on_the_image_and_get_pixel_boxes():
    found = [{"n": 1, "ref": "e1", "box_css": [100, 100, 80, 30]}]
    png, scale = marks.process(_png(800, 600), css_width=400, max_edge=0, marks=found)
    assert scale == 2.0
    assert found[0]["box_px"] == [200, 200, 160, 60]
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert img.getpixel((200, 230)) == marks.MARK_RGB


def test_an_unreadable_capture_is_returned_as_it_is():
    assert marks.process(b"not a png", css_width=100, max_edge=10) == (b"not a png", 1.0)


SNAPSHOT = """- generic [ref=e1] [box=0,0,1280,800]:
  - button "Alpha" [ref=e2] [box=40,34,66,33]
  - button [ref=e3] [box=120,34,40,33]
  - link "Far" [ref=e4] [box=40,2000,60,20]:
    - /url: /far
"""


def test_marks_cover_what_is_on_screen_in_tree_order_with_their_refs():
    tree = compact_snapshot(SNAPSHOT, refs=True, viewport=(1280, 800))
    found = marks.marks_from(tree.elements)
    assert [(m["n"], m["ref"], m["name"]) for m in found] == [(1, "e2", "Alpha"), (2, "e3", "")]
    assert found[0]["box_css"] == [40, 34, 66, 33]


def test_full_page_marks_are_shifted_by_the_scroll():
    tree = compact_snapshot(SNAPSHOT, refs=True, viewport=(1280, 800))
    found = marks.marks_from(tree.elements, offset=(0, 500))
    assert found[0]["box_css"][1] == 534


class _Page:
    pass


def test_points_go_back_through_the_last_capture():
    page = _Page()
    assert marks.to_viewport(page, 50, 60, None) == (50, 60)
    marks.remember(
        page, marks.Geometry(scale=2.0, full_page=False, scroll=(0, 0), viewport=(640, 400))
    )
    assert marks.to_viewport(page, 50, 60, None) == (25, 30)
    marks.remember(
        page, marks.Geometry(scale=1.0, full_page=True, scroll=(0, 0), viewport=(640, 400))
    )
    assert marks.to_viewport(page, 50, 900, (1, 640, 400, 0, 700, 640)) == (50, 200)


async def test_click_at_a_point_goes_through_the_mouse_and_the_gates(make_ctx, tools_of, page):
    ctx = make_ctx()
    ctx.config.cursor = False
    marks.remember(
        page, marks.Geometry(scale=2.0, full_page=False, scroll=(0, 0), viewport=(640, 400))
    )
    result = await (await tools_of(ctx))["click"](x=200, y=100)
    assert result["status"] == "ok", result
    assert result["point_css"] == [100, 50]
    assert ("mouse_click", 100, 50) in page.calls
    audit = [json.loads(line) for line in Path(ctx.config.audit_path).read_text().splitlines()]
    assert any(r.get("tool") == "click" and r.get("args", {}).get("x") == 200 for r in audit)


async def test_a_point_off_screen_is_refused_before_anyone_is_asked(
    make_ctx, tools_of, page, monkeypatch
):
    ctx = make_ctx()

    async def geometry(_page):
        return (1.0, 640.0, 400.0, 0.0, 0.0, 640.0)

    monkeypatch.setattr(marks, "page_geometry", geometry)
    result = await (await tools_of(ctx))["click"](x=5000, y=10)
    assert result["status"] == "outside_viewport"
    assert not any(c[0] == "mouse_click" for c in page.calls)
    assert ctx.perms.live_grants(current_session_key(ctx)) == [] or all(
        g.capability.value != "submit" for g in ctx.perms.live_grants(current_session_key(ctx))
    )


async def test_hover_at_a_point_moves_and_never_presses(make_ctx, tools_of, page):
    ctx = make_ctx()
    ctx.config.cursor = False
    result = await (await tools_of(ctx))["hover"](x=30, y=40)
    assert result["status"] == "ok", result
    assert ("mouse_move", 30, 40) in page.calls
    assert not any(c[0] == "mouse_click" for c in page.calls)


async def test_neither_a_selector_nor_a_point_is_an_error(make_ctx, tools_of):
    tools = await tools_of(make_ctx())
    assert (await tools["click"]())["status"] == "error"
    assert (await tools["click"](x=3))["status"] == "error"
    assert (await tools["hover"]())["status"] == "error"


async def test_annotated_screenshot_returns_marks_and_scale(make_ctx, tools_of, page, monkeypatch):
    ctx = make_ctx()
    page.png = _png(1280, 800)

    async def geometry(_page):
        return (1.0, 1280.0, 800.0, 0.0, 0.0, 1280.0)

    async def found(_page, _geo, *, full_page):
        return [
            {"n": 1, "ref": "e2", "role": "button", "name": "Alpha", "box_css": [40, 34, 66, 33]}
        ]

    from lyra_browser.tools import reading

    monkeypatch.setattr(marks, "page_geometry", geometry)
    monkeypatch.setattr(reading, "_marks_of", found)
    result = await (await tools_of(ctx))["screenshot"](annotate=True)
    assert result["scale"] == 1.0
    assert result["marks"][0]["box_px"] == [40, 34, 66, 33]
    assert marks.last(page).scale == 1.0
