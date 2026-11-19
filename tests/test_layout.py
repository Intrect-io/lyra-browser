"""inspect_layout on fakes: the envelope, the cap, the pixel mapping and what is audited.

What the page script finds is scripts/verify_layout_e2e.py (a real page, both drivers).
"""

from __future__ import annotations

from lyra_browser import marks
from lyra_browser.tools.layout import _LAYOUT_JS, MAX_FINDINGS
from lyra_browser.uat.spec import DEFAULT_TOOLS

FINDINGS = [
    {"kind": "low_contrast", "element": "p#a", "text": "x", "box_css": [10, 20, 30, 10]},
    {"kind": "covered", "element": "button#b", "text": "Save", "box_css": [40, 50, 20, 10]},
    {"kind": "low_contrast", "element": "p#c", "text": "y", "box_css": [0, 0, 5, 5]},
]


async def test_findings_are_counted_and_audited(make_ctx, tools_of, page):
    page.eval_answers[_LAYOUT_JS] = {"findings": FINDINGS, "scanned": 9, "unknown_background": 1}
    ctx = make_ctx()
    out = await (await tools_of(ctx))["inspect_layout"]()
    assert out["status"] == "ok" and out["counts"] == {"low_contrast": 2, "covered": 1}
    assert out["scanned"] == 9 and out["unknown_background"] == 1 and out["truncated"] is False
    rows = [r for r in ctx.config.audit_path.read_text().splitlines() if "inspect_layout" in r]
    assert rows and "low_contrast" in rows[-1]


async def test_the_cap_is_clamped_and_passed_to_the_page(make_ctx, tools_of, page):
    page.eval_answers[_LAYOUT_JS] = {"findings": [], "scanned": 0}
    tools = await tools_of(make_ctx())
    await tools["inspect_layout"](max_findings=10_000)
    await tools["inspect_layout"](max_findings=0, selector="#x")
    args = [c[2] for c in page.calls if c[0] == "evaluate" and c[1] == _LAYOUT_JS]
    assert args[0]["cap"] == MAX_FINDINGS and args[1]["cap"] == 1 and args[1]["selector"] == "#x"


async def test_a_missing_region_and_a_failed_read_say_so(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())
    page.eval_answers[_LAYOUT_JS] = {"missing": True}
    assert (await tools["inspect_layout"](selector="#gone"))["status"] == "not_found"
    page.eval_answers[_LAYOUT_JS] = RuntimeError("Execution context was destroyed")
    out = await tools["inspect_layout"]()
    assert out["status"] == "unavailable" and "destroyed" in out["error"]


async def test_box_px_follows_the_latest_screenshot(make_ctx, tools_of, page):
    page.eval_answers[_LAYOUT_JS] = {"findings": [dict(FINDINGS[0])], "scanned": 1}
    page.eval_answers[marks._GEOMETRY_JS] = [1, 1280, 800, 0, 0, 1280]
    tools = await tools_of(make_ctx())
    assert "box_px" not in (await tools["inspect_layout"]())["findings"][0]  # no screenshot yet
    marks.remember(
        page, marks.Geometry(scale=0.5, full_page=False, scroll=(0, 0), viewport=(1280, 800))
    )
    assert (await tools["inspect_layout"]())["findings"][0]["box_px"] == [5, 10, 15, 5]


def test_the_visual_eval_does_not_get_it():
    # The eval measures what a model sees; a DOM reading of contrast and overlap would
    # answer its tasks without looking (README, inspect_layout).
    assert "inspect_layout" not in DEFAULT_TOOLS
