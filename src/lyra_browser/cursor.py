"""The virtual cursor: what a person watching the window sees the agent point at.

Before a pointer action (click, hover, a field about to be typed into, an option about
to be picked) a cursor travels from where it last stood to the target, and the real
mouse travels the same path, so hover styles and ``pointermove`` listeners see what
they would see from a person. Then the action runs, and the cursor stays where it acted.

It is drawn with the DevTools overlay (``Overlay.highlightQuad``), not with the DOM.
Measured 2026-11-18 (Chrome 157, ``testing/cursor_overlay_spike_261118_v1.py``): the
quad is in ``Page.captureScreenshot`` headless and headful and on the X display, the
page's ``MutationObserver`` records nothing, the node count does not change and the
click underneath still lands. A DOM cursor would be seen by page scripts and could take
the click itself.

The cursor is a courtesy: nothing here may fail an action. Every CDP or input error is
swallowed and the action goes on without it. ``Config.cursor`` (``LYRA_BROWSER_CURSOR``)
turns all of it off: no overlay, no extra pointer movement.
"""

from __future__ import annotations

import asyncio
import math
import weakref
from dataclasses import dataclass
from typing import Any

# A classic arrow, tip at (0, 0), as one concave quad: tip, left wing, notch, right wing.
_ARROW = ((0.0, 0.0), (0.0, 22.0), (6.0, 16.5), (16.0, 16.0))
# A colour no page uses for its own pointer: the gates look for it in captures.
FILL = {"r": 255, "g": 45, "b": 85, "a": 1.0}
OUTLINE = {"r": 255, "g": 255, "b": 255, "a": 1.0}

# Where the box's centre is, in the main frame's viewport, after scrolling it into view.
# Elements inside a frame report frame-local coordinates, so they are left alone
# (``null``): a cursor drawn in the wrong place is worse than none.
_POINT_JS = """(el) => {
  if (el.ownerDocument.defaultView !== window.top) return null;
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  return [r.left + r.width / 2, r.top + r.height / 2];
}"""

_STEP_MIN, _STEP_MAX = 6, 24
_PX_PER_STEP = 30.0


@dataclass
class _State:
    x: float | None = None
    y: float | None = None
    shown: bool = False
    cdp: Any = None
    enabled_for: Any = None


_states: weakref.WeakKeyDictionary[Any, _State] = weakref.WeakKeyDictionary()


def _state(page: Any) -> _State:
    try:
        st = _states.get(page)
        if st is None:
            st = _State()
            _states[page] = st
        return st
    except TypeError:  # not weak-referenceable (a test double): keep no state
        return _State()


def arrow_quad(x: float, y: float) -> list[float]:
    """The arrow with its tip at (x, y), as CDP's flat quad."""
    return [c for dx, dy in _ARROW for c in (x + dx, y + dy)]


def path(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[float, float]]:
    """Points from ``start`` (exclusive) to ``end`` (inclusive), eased in and out, with a
    slight bow so the motion reads as a hand rather than a ruler."""
    (x0, y0), (x1, y1) = start, end
    dist = math.hypot(x1 - x0, y1 - y0)
    steps = max(_STEP_MIN, min(_STEP_MAX, int(dist / _PX_PER_STEP)))
    bow = min(40.0, dist * 0.08)
    nx, ny = ((y0 - y1) / dist, (x1 - x0) / dist) if dist else (0.0, 0.0)
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        b = math.sin(math.pi * t) * bow
        out.append((x0 + (x1 - x0) * e + nx * b, y0 + (y1 - y0) * e + ny * b))
    out[-1] = (x1, y1)
    return out


async def _send(page: Any, method: str, params: dict | None = None) -> Any:
    """One DevTools command on ``page``: native pages have their own session, Playwright
    (and patchright) pages get a CDP session of their own, opened once."""
    native = getattr(page, "_send", None)
    if native is not None and callable(native):
        return await native(method, params or {})
    st = _state(page)
    if st.cdp is None:
        st.cdp = await page.context.new_cdp_session(page)
    return await st.cdp.send(method, params or {})


async def _ensure_overlay(page: Any) -> None:
    st = _state(page)
    # The overlay agent needs the DOM agent. Enabling is per session, so once per page;
    # it survives navigations of that page.
    if st.enabled_for is page:
        return
    await _send(page, "DOM.enable")
    await _send(page, "Overlay.enable")
    st.enabled_for = page


async def _draw(page: Any, x: float, y: float) -> None:
    await _send(
        page,
        "Overlay.highlightQuad",
        {"quad": arrow_quad(x, y), "color": FILL, "outlineColor": OUTLINE},
    )


def enabled(ctx: Any) -> bool:
    return bool(getattr(ctx.config, "cursor", False))


async def travel(ctx: Any, page: Any, x: float, y: float) -> bool:
    """Move the cursor, and the mouse, from where they stood to (x, y). True if drawn."""
    if not enabled(ctx):
        return False
    st = _state(page)
    try:
        await _ensure_overlay(page)
        if st.x is None:
            # First appearance: from a little up and left of the target, so even the
            # first action shows a movement rather than a cursor popping into place.
            st.x, st.y = max(0.0, x - 120.0), max(0.0, y - 90.0)
        points = path((st.x, st.y), (x, y))
        pause = max(0.0, ctx.config.cursor_ms / 1000.0 / len(points))
        for px, py in points:
            await page.mouse.move(px, py)
            await _draw(page, px, py)
            if pause:
                await asyncio.sleep(pause)
        st.x, st.y, st.shown = x, y, True
        return True
    except Exception:  # noqa: BLE001 — a courtesy must never fail the action
        return False


async def travel_to(ctx: Any, page: Any, locator: Any, *, timeout: float = 2000) -> bool:
    """Bring ``locator``'s element into view and travel to its centre. True if drawn."""
    if not enabled(ctx):
        return False
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout)
        point = await locator.evaluate(_POINT_JS, timeout=timeout)
    except Exception:  # noqa: BLE001 — the action reports its own failure
        return False
    if not point:
        return False
    return await travel(ctx, page, float(point[0]), float(point[1]))


async def redraw(ctx: Any, page: Any) -> bool:
    """Draw the cursor again where it last stood (a navigation clears the overlay)."""
    if not enabled(ctx):
        return False
    st = _state(page)
    if not st.shown or st.x is None:
        return False
    try:
        await _ensure_overlay(page)
        await _draw(page, st.x, st.y)
        return True
    except Exception:  # noqa: BLE001
        return False


async def hide(ctx: Any, page: Any) -> None:
    """Take the cursor off the screen (the user holds the window during a takeover)."""
    st = _state(page)
    if not st.shown:
        return
    st.shown = False
    try:
        await _send(page, "Overlay.hideHighlight")
    except Exception:  # noqa: BLE001
        pass


async def hide_current(ctx: Any) -> None:
    """Hide the cursor on the session's current page, without claiming the session."""
    page = getattr(ctx.session, "_page", None)
    if page is not None:
        await hide(ctx, page)


def position(page: Any) -> tuple[float, float] | None:
    st = _states.get(page) if _weakable(page) else None
    if st is None or st.x is None or not st.shown:
        return None
    return st.x, st.y


def _weakable(obj: Any) -> bool:
    try:
        weakref.ref(obj)
        return True
    except TypeError:
        return False
