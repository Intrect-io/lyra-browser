"""Action evidence: the before/after comparison, the cursor mask and the wiring.

Real pages, pixels and the measured overhead are ``scripts/verify_evidence_e2e.py``'s.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from lyra_browser import evidence
from lyra_browser.evidence import Thumb, compare


def _thumb(draw=None, size=(320, 200)) -> Thumb:
    img = Image.new("L", size, 255)
    if draw:
        draw(ImageDraw.Draw(img))
    return Thumb(image=img, scale=0.25)


def test_identical_frames_are_no_change():
    assert compare(_thumb(), _thumb()) == {"changed": False, "ratio": 0.0, "region": None}


def test_a_new_box_is_a_change_with_its_region_in_css_pixels():
    after = _thumb(lambda d: d.rectangle([200, 150, 254, 167], fill=0))
    got = compare(_thumb(), after)
    assert got["changed"] is True
    assert got["region"] == [800, 600, 220, 72]


def test_a_few_stray_pixels_are_not_a_change():
    after = _thumb(lambda d: d.point([(5, 5), (6, 6)], fill=0))
    assert compare(_thumb(), after)["changed"] is False


def test_the_cursor_is_masked_where_it_stood_and_where_the_overlay_draws_it():
    # The overlay is not scaled in a clipped capture: an arrow at CSS (80, 56) shows at
    # thumbnail pixel (80, 56), not (20, 14).
    after = _thumb(lambda d: d.polygon([(80, 56), (80, 78), (96, 72)], fill=0))
    assert compare(_thumb(), after)["changed"] is True
    assert compare(_thumb(), after, ignore=[(80, 56)])["changed"] is False


def test_a_resized_viewport_is_a_change():
    got = compare(_thumb(), _thumb(size=(300, 200)))
    assert got["changed"] is True and got["region"] is None


async def test_capture_true_returns_the_after_image(make_ctx, tools_of, page, monkeypatch):
    ctx = make_ctx()
    ctx.config.cursor = False
    ctx.config.capture_settle_ms = 0

    async def shot(ctx_, page_, **kwargs):
        assert kwargs["kind"] == "action"
        return {"image_path": "/tmp/after.png", "width": 640, "height": 400, "scale": 0.5}

    from lyra_browser.tools import reading

    monkeypatch.setattr(reading, "capture_page", shot)
    result = await (await tools_of(ctx))["click"](selector="#go", capture=True)
    assert result["status"] == "ok", result
    assert result["image_path"] == "/tmp/after.png"
    assert result["capture"] == {"width": 640, "height": 400, "scale": 0.5}


async def test_a_refused_action_gets_no_evidence_and_no_capture(make_ctx, tools_of):
    ctx = make_ctx(on_site=False)
    result = await (await tools_of(ctx))["click"](selector="#go", capture=True)
    assert result["status"] != "ok"
    assert "image_path" not in result and "visual_change" not in result


async def test_evidence_off_adds_nothing(make_ctx, tools_of, monkeypatch):
    ctx = make_ctx()
    ctx.config.cursor = False
    ctx.config.evidence = False
    called = []

    async def thumb(*a, **k):
        called.append(1)

    monkeypatch.setattr(evidence, "thumb", thumb)
    result = await (await tools_of(ctx))["click"](selector="#go")
    assert result["status"] == "ok"
    assert "visual_change" not in result and called == []


async def test_visual_stable_waits_alone(make_ctx, tools_of):
    tools = await tools_of(make_ctx())
    got = await tools["wait_for"](visual_stable=True, text="x")
    assert got["status"] == "error"


async def test_visual_stable_reports_stable_and_unstable(make_ctx, tools_of, monkeypatch):
    tools = await tools_of(make_ctx())

    async def stable(page, *, timeout_s, interval_s=0.15):
        return {"stable": True, "waited_ms": 150}

    monkeypatch.setattr(evidence, "wait_stable", stable)
    assert await tools["wait_for"](visual_stable=True) == {
        "status": "ok",
        "stable": True,
        "waited_ms": 150,
    }

    async def moving(page, *, timeout_s, interval_s=0.15):
        return {"stable": False, "waited_ms": 1000}

    monkeypatch.setattr(evidence, "wait_stable", moving)
    assert (await tools["wait_for"](visual_stable=True, timeout_ms=1000))["status"] == "timeout"
