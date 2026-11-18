"""Named snapshots and the pixel diff (compare.py); the real browser is verify_compare_e2e."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from lyra_browser import compare
from lyra_browser.compare import Snapshot, Snapshots


def _png(draw=None, size=(200, 120)) -> bytes:
    img = Image.new("RGB", size, "white")
    if draw:
        draw(ImageDraw.Draw(img))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _snap(name: str, png: bytes = b"x" * 10, **kw) -> Snapshot:
    base = {"scale": 1.0, "viewport": (1280, 800), "scroll": (0, 0), "url": "u", "taken_at": 0}
    return Snapshot(name=name, png=png, **(base | kw))


def test_the_same_image_has_no_change():
    png = _png(lambda d: d.rectangle([20, 20, 60, 60], fill="red"))
    assert compare.diff(png, png) == (0.0, [])


def test_one_recoloured_rectangle_is_one_box_around_it():
    a = _png(lambda d: d.rectangle([40, 30, 99, 69], fill=(229, 231, 235)))
    b = _png(lambda d: d.rectangle([40, 30, 99, 69], fill=(212, 216, 222)))
    ratio, boxes = compare.diff(a, b)
    assert boxes == [[40, 30, 60, 40]]
    assert abs(ratio - 60 * 40 / (200 * 120)) < 1e-6


def test_a_moved_rectangle_is_one_box_not_two_edges():
    a = _png(lambda d: d.rectangle([20, 30, 139, 79], outline="black", fill=(229, 231, 235)))
    b = _png(lambda d: d.rectangle([23, 30, 142, 79], outline="black", fill=(229, 231, 235)))
    _, boxes = compare.diff(a, b)
    assert len(boxes) == 1
    x, y, w, h = boxes[0]
    assert x <= 21 and x + w >= 142 and y == 30 and h == 50


def test_two_tiles_recoloured_side_by_side_stay_two_boxes():
    a = _png(
        lambda d: (
            d.rectangle([10, 20, 79, 69], fill="red"),
            d.rectangle([100, 20, 169, 69], fill="red"),
        )
    )
    b = _png(
        lambda d: (
            d.rectangle([10, 20, 79, 69], fill="blue"),
            d.rectangle([100, 20, 169, 69], fill="blue"),
        )
    )
    assert len(compare.diff(a, b)[1]) == 2


def test_two_unrelated_changes_stay_two_boxes():
    a = _png()
    b = _png(
        lambda d: (
            d.rectangle([5, 5, 15, 15], fill="red"),
            d.rectangle([150, 90, 170, 110], fill="blue"),
        )
    )
    assert len(compare.diff(a, b)[1]) == 2


def test_noise_below_the_threshold_is_not_a_change():
    a = _png(lambda d: d.rectangle([0, 0, 199, 119], fill=(100, 100, 100)))
    b = _png(lambda d: d.rectangle([0, 0, 199, 119], fill=(100 + compare.PIXEL_THRESHOLD,) * 3))
    assert compare.diff(a, b) == (0.0, [])


def test_mismatch_names_what_differs():
    before = _snap("b")
    assert compare.mismatch(before, 1.0, (1280, 800), (0, 0)) is None
    assert compare.mismatch(before, 1.0, (1000, 800), (0, 0)) == "viewport"
    assert compare.mismatch(before, 2.0, (1280, 800), (0, 0)) == "scale"
    assert compare.mismatch(before, 1.0, (1280, 800), (0, 300)) == "scroll"


def test_snapshots_keep_the_newest_within_count_and_bytes():
    snaps = Snapshots(max_count=3, max_bytes=25)
    assert snaps.put(_snap("a")) == []
    assert snaps.put(_snap("b")) == []
    assert snaps.put(_snap("c")) == ["a"]  # 30 bytes > 25
    assert snaps.names() == ["b", "c"]
    snaps.put(_snap("b"))  # kept again: newest now
    assert snaps.names() == ["c", "b"]
    big = Snapshots(max_count=2, max_bytes=10**6)
    for n in "xyz":
        big.put(_snap(n))
    assert big.names() == ["y", "z"]
    big.clear()
    assert big.names() == [] and big.get("z") is None


def test_one_snapshot_larger_than_the_cap_is_still_kept():
    snaps = Snapshots(max_count=3, max_bytes=5)
    snaps.put(_snap("only"))
    assert snaps.names() == ["only"]


def test_boxes_are_described_in_both_pixel_kinds():
    assert compare.describe([[20, 10, 40, 30]], 2.0) == [
        {"box_px": [20, 10, 40, 30], "box_css": [10.0, 5.0, 20.0, 15.0]}
    ]


def test_outline_draws_in_the_box_colour():
    png = compare.outline(_png(), [[50, 40, 30, 20]], 1.0)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    assert sum(1 for p in img.getdata() if p == compare.BOX_RGB) > 100
