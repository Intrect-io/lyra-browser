"""inspect_layout on fakes: the envelope, the cap, the pixel mapping and what is audited.

What the page script finds is scripts/verify_layout_e2e.py (a real page, both drivers).
"""

from __future__ import annotations

from lyra_browser import marks
from lyra_browser.tools.layout import _LAYOUT_FN, _LAYOUT_JS, MAX_FINDINGS
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
    await tools["inspect_layout"](max_findings=0)
    args = [c[2] for c in page.calls if c[0] == "evaluate" and c[1] == _LAYOUT_JS]
    assert args[0]["cap"] == MAX_FINDINGS and args[1]["cap"] == 1


async def test_a_selector_scans_under_the_element_it_names(make_ctx, tools_of, page):
    page.present = {"aria-ref=e7"}
    tools = await tools_of(make_ctx())
    out = await tools["inspect_layout"](selector="aria-ref=e7")
    assert out["status"] == "ok"
    asked = [c for c in page.calls if c[0] == "evaluate" and c[1] == "aria-ref=e7"]
    assert asked and asked[0][2] == _LAYOUT_FN


async def test_a_missing_region_and_a_failed_read_say_so(make_ctx, tools_of, page):
    tools = await tools_of(make_ctx())
    page.present = set()
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


async def test_a_typed_value_never_comes_back(make_ctx, tools_of, page):
    # The page script labels controls by placeholder, aria-label or type; the Python side
    # passes findings through, so the script's contract is pinned here by its source.
    assert "el.value" in _LAYOUT_FN and "it may be a password" in _LAYOUT_FN
    assert "LABELLED.has(t)) return clipText(el.value" in _LAYOUT_FN  # submit/button/reset only
    assert "TEXTAREA') return ''" in _LAYOUT_FN  # a textarea's text node is its value


async def test_findings_from_a_patched_page_are_checked_not_trusted(make_ctx, tools_of, page):
    # A page that replaced Math.round gives back strings for numbers: those are dropped.
    page.eval_answers[_LAYOUT_JS] = {
        "findings": [
            {"kind": "covered", "element": "p", "box_css": ["x", "x", "x", "x"]},
            {"kind": "evil", "element": "p", "box_css": [0, 0, 1, 1]},
            {"kind": "clipped", "element": "e" * 5000, "box_css": [1, 2, 3, 4], "extra": "z"},
        ],
        "scanned": "many",
        "truncated": "yes please",
    }
    out = await (await tools_of(make_ctx()))["inspect_layout"]()
    assert [f["kind"] for f in out["findings"]] == ["clipped"]
    assert len(out["findings"][0]["element"]) <= 230 and "extra" not in out["findings"][0]
    assert out["scanned"] == 0 and out["truncated"] is False
