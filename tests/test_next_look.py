"""What an action reply adds for the next look (evidence.point_the_next_look).

Real pixels are scripts/verify_evidence_e2e.py; these pin the mapping and when the hint shows.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lyra_browser import evidence, marks
from lyra_browser.compare import Snapshot, Snapshots


class _Page:
    def __init__(self, scroll=(0.0, 0.0)) -> None:
        self.scroll = scroll

    async def evaluate(self, _js):
        return [1, 1280, 800, self.scroll[0], self.scroll[1], 1280]


def _geo(scale=1.0, full_page=False, scroll=(0.0, 0.0), origin=(0.0, 0.0)):
    return marks.Geometry(
        scale=scale, full_page=full_page, scroll=scroll, viewport=(1280, 800), origin=origin
    )


def _ctx(*names):
    snaps = Snapshots()
    for n in names:
        snaps.put(Snapshot(n, b"x", 1.0, (1280, 800), (0, 0), "u", 0))
    return SimpleNamespace(snapshots=snaps)


@pytest.mark.parametrize(
    ("geo", "scroll", "want"),
    [
        (_geo(scale=0.5), (0, 0), [50, 25, 20, 10]),
        (_geo(scale=2.0), (0, 0), [200, 100, 80, 40]),
        (_geo(full_page=True), (0, 300), [100, 350, 40, 20]),
        (_geo(scroll=(0, 0)), (0, 300), None),  # scrolled since the capture
        (_geo(scale=4.0, origin=(90.0, 40.0)), (0, 0), None),  # the latest is a zoom
    ],
)
async def test_region_in_the_latest_screenshot(geo, scroll, want):
    page = _Page(scroll)
    marks.remember(page, geo)
    assert await evidence.region_in_latest(page, [100, 50, 40, 20]) == want


async def test_no_screenshot_no_region_px():
    assert await evidence.region_in_latest(_Page(), [1, 2, 3, 4]) is None


async def test_the_hint_needs_a_snapshot_and_a_change():
    page = _Page()
    marks.remember(page, _geo())
    reply = {"visual_change": {"changed": True, "ratio": 0.1, "region": [10, 10, 5, 5]}}
    await evidence.point_the_next_look(_ctx("a", "b"), page, reply)
    assert reply["visual_change"]["region_px"] == [10, 10, 5, 5]
    assert reply["snapshots"] == ["a", "b"] and 'compare_to="b"' in reply["compare_hint"]

    quiet = {"visual_change": {"changed": False, "ratio": 0.0, "region": None}}
    await evidence.point_the_next_look(_ctx("a"), page, quiet)
    assert "compare_hint" not in quiet and "region_px" not in quiet["visual_change"]

    none_held = {"visual_change": {"changed": True, "ratio": 0.1, "region": None}}
    await evidence.point_the_next_look(_ctx(), page, none_held)
    assert "compare_hint" not in none_held


async def test_a_reply_without_evidence_is_left_alone():
    reply = {"status": "ok"}
    await evidence.point_the_next_look(SimpleNamespace(), _Page(), reply)
    assert reply == {"status": "ok"}
