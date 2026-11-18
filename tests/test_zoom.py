"""Region zoom geometry (tools/reading.py region_to_css, zoom_for; marks.to_viewport).

Pixels and real clicks are scripts/verify_zoom_e2e.py.
"""

from __future__ import annotations

import pytest

from lyra_browser import marks
from lyra_browser.tools.reading import ZOOM_MAX, region_to_css, zoom_for

NOW = (1.0, 1280.0, 800.0, 0.0, 0.0, 1280.0)  # dpr, viewport w, h, scrollX, scrollY, doc w


def _geo(scale=1.0, origin=(0.0, 0.0), full_page=False):
    return marks.Geometry(
        scale=scale, full_page=full_page, scroll=(0, 0), viewport=(1280, 800), origin=origin
    )


def test_a_region_of_a_downscaled_capture_is_measured_in_css_px():
    assert region_to_css(_geo(scale=0.5), [100, 50, 40, 20], NOW) == (200.0, 100.0, 80.0, 40.0)


def test_a_region_of_a_zoomed_capture_starts_at_its_origin():
    got = region_to_css(_geo(scale=4.0, origin=(300.0, 200.0)), [40, 80, 40, 40], NOW)
    assert got == (310.0, 220.0, 10.0, 10.0)


def test_a_region_is_clipped_to_the_viewport_and_refused_outside_it():
    assert region_to_css(_geo(), [1200, 700, 200, 200], NOW) == (1200.0, 700.0, 80.0, 100.0)
    assert region_to_css(_geo(), [5000, 10, 50, 50], NOW) == "outside_viewport"
    assert region_to_css(_geo(), [10, 10, 0, 50], NOW) == "empty"
    assert region_to_css(_geo(), [10, 10, 50], NOW) == "malformed"
    assert region_to_css(_geo(), None, NOW) == "malformed"


def test_a_full_page_region_is_taken_back_to_the_viewport():
    now = (1.0, 1280.0, 800.0, 0.0, 1000.0, 1280.0)
    got = region_to_css(_geo(full_page=True), [100, 1100, 50, 50], now)
    assert got == (100.0, 100.0, 50.0, 50.0)


@pytest.mark.parametrize(
    ("w", "h", "edge", "want"),
    [(100, 50, 1568, ZOOM_MAX), (800, 400, 1568, 1.96), (2000, 100, 1000, 0.5), (10, 10, 0, 4.0)],
)
def test_the_zoom_fits_the_max_edge_up_to_the_cap(w, h, edge, want):
    assert zoom_for(w, h, edge) == pytest.approx(want)


def test_a_point_of_a_zoomed_image_maps_to_the_viewport():
    class Page:  # weakly referenceable, as real pages are
        pass

    page = Page()
    marks.remember(page, _geo(scale=4.0, origin=(300.0, 200.0)))
    assert marks.to_viewport(page, 40, 80, NOW) == (310.0, 220.0)
