"""The virtual cursor: drawn with the overlay, travelled with the mouse, never in the way.

These run on doubles; ``scripts/verify_cursor_e2e.py`` checks the pixels and the page.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from lyra_browser import cursor
from lyra_browser.config import Config


class _Mouse:
    def __init__(self, log):
        self.log = log

    async def move(self, x, y, **_):
        self.log.append(("move", round(x, 1), round(y, 1)))


class _Page:
    """A native-like page: its own ``_send`` and a mouse."""

    def __init__(self, fail: str = ""):
        self.log: list = []
        self.mouse = _Mouse(self.log)
        self.fail = fail

    async def _send(self, method, params):
        if method == self.fail:
            raise RuntimeError("boom")
        self.log.append((method, params))
        return {}


class _Locator:
    def __init__(self, point):
        self.point = point

    async def scroll_into_view_if_needed(self, timeout=None):
        pass

    async def evaluate(self, js, timeout=None):
        assert "getBoundingClientRect" in js
        return self.point


def _ctx(**overrides):
    cfg = Config(**{"cursor_ms": 0, **overrides})
    return SimpleNamespace(config=cfg, session=SimpleNamespace(_page=None))


def test_arrow_quad_has_its_tip_on_the_point():
    quad = cursor.arrow_quad(100, 50)
    assert len(quad) == 8
    assert quad[:2] == [100, 50]


def test_path_ends_exactly_on_the_target_and_is_eased():
    pts = cursor.path((0, 0), (300, 0))
    assert pts[-1] == (300, 0)
    assert 6 <= len(pts) <= 24
    steps = [math.dist(a, b) for a, b in zip([(0, 0), *pts], pts, strict=False)]
    assert steps[0] < max(steps) and steps[-1] < max(steps)


async def test_travel_moves_the_mouse_and_draws_along_the_path():
    page = _Page()
    assert await cursor.travel(_ctx(), page, 400, 300)
    moves = [e for e in page.log if e[0] == "move"]
    quads = [e for e in page.log if e[0] == "Overlay.highlightQuad"]
    assert len(moves) >= 6 and len(quads) == len(moves)
    assert moves[-1] == ("move", 400, 300)
    assert quads[-1][1]["quad"][:2] == [400, 300]
    assert ("Overlay.enable", {}) in page.log and ("DOM.enable", {}) in page.log
    assert cursor.position(page) == (400, 300)


async def test_the_overlay_is_enabled_once_per_page():
    page = _Page()
    await cursor.travel(_ctx(), page, 10, 10)
    await cursor.travel(_ctx(), page, 200, 200)
    assert sum(1 for e in page.log if e[0] == "Overlay.enable") == 1


async def test_off_does_nothing_at_all():
    page = _Page()
    ctx = _ctx(cursor=False)
    assert not await cursor.travel(ctx, page, 10, 10)
    assert not await cursor.travel_to(ctx, page, _Locator([5, 5]))
    assert not await cursor.redraw(ctx, page)
    assert page.log == []


async def test_a_failing_overlay_never_fails_the_action():
    page = _Page(fail="Overlay.highlightQuad")
    assert await cursor.travel(_ctx(), page, 10, 10) is False


async def test_travel_to_skips_an_element_in_a_frame():
    page = _Page()
    assert not await cursor.travel_to(_ctx(), page, _Locator(None))
    assert page.log == []


async def test_redraw_puts_it_back_where_it_stood_and_hide_takes_it_away():
    page = _Page()
    ctx = _ctx()
    await cursor.travel(ctx, page, 120, 80)
    page.log.clear()
    assert await cursor.redraw(ctx, page)
    assert page.log[-1][1]["quad"][:2] == [120, 80]
    ctx.session._page = page
    await cursor.hide_current(ctx)
    assert page.log[-1] == ("Overlay.hideHighlight", {})
    assert cursor.position(page) is None
    assert not await cursor.redraw(ctx, page)


async def test_playwright_pages_get_one_cdp_session():
    sent = []

    class Session:
        async def send(self, method, params):
            sent.append(method)

    opened = []

    class Context:
        async def new_cdp_session(self, page):
            opened.append(page)
            return Session()

    class Page:  # weak-referenceable, as Playwright's pages are
        def __init__(self):
            self.context = Context()
            self.mouse = _Mouse([])

    page = Page()
    await cursor.travel(_ctx(), page, 50, 50)
    await cursor.travel(_ctx(), page, 60, 60)
    assert len(opened) == 1
    assert "Overlay.highlightQuad" in sent


def test_config_reads_the_switch():
    import os

    old = os.environ.get("LYRA_BROWSER_CURSOR")
    try:
        os.environ["LYRA_BROWSER_CURSOR"] = "off"
        assert Config.from_env().cursor is False
        os.environ["LYRA_BROWSER_CURSOR"] = ""
        assert Config.from_env().cursor is True
    finally:
        if old is None:
            os.environ.pop("LYRA_BROWSER_CURSOR", None)
        else:
            os.environ["LYRA_BROWSER_CURSOR"] = old
