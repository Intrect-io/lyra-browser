"""Did an action visibly change the page, and where? Cheap before/after thumbnails.

An action reply says what the driver did; it cannot say whether anything happened on
screen. A click that missed, a toast that came and went, a form that redrew one field:
the agent would otherwise need a screenshot (an image in its context) to find out.
Here every pointer and keyboard action takes a small thumbnail of the viewport before
and after (DevTools ``Page.captureScreenshot`` with a clip at a quarter scale: no page
script runs), compares them, and adds ``visual_change: {changed, ratio, region}`` to
the reply, ``region`` being the bounding box of what changed in CSS pixels.

The virtual cursor moves between the two thumbnails, so the boxes around where it
stood before and after are left out of the comparison.

``wait_stable`` is the same comparison in a loop: the page is settled when two
thumbnails a short interval apart are the same (animations, late images, web fonts).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import io
import time
import weakref
from dataclasses import dataclass
from typing import Any

from . import cursor

THUMB_SCALE = 0.25
# A pixel counts as changed when its grey level moved by more than this (scaling
# noise stays well under it), and the page as changed when this share of pixels did.
PIXEL_DELTA = 24
CHANGED_RATIO = 0.0003
# Settle after the action before the after-thumbnail: a click's handler and the frame
# it paints (three frames at 60 Hz). Short on purpose: it is paid by every action
# (measured overhead in verify_evidence_e2e.py); what arrives later (a fetch, a slow
# animation) is wait_for(visual_stable=true)'s to see, or a screenshot's.
AFTER_SETTLE_S = 0.05
# The cursor's box, from its tip (cursor.py draws a 16 x 22 arrow), with a margin.
_CURSOR_BOX = (-4, -4, 24, 30)


@dataclass
class Thumb:
    image: Any  # PIL.Image, grey
    scale: float  # thumbnail px per CSS px
    viewport: dict | None = None  # the layout viewport it was taken of


# The layout viewport, kept per page for a moment: actions come in runs, and asking for
# the metrics again is one more DevTools round trip on every thumbnail.
_VIEWPORT_TTL_S = 2.0
_viewports: weakref.WeakKeyDictionary[Any, tuple[float, dict]] = weakref.WeakKeyDictionary()


def _recent_viewport(page: Any) -> dict | None:
    try:
        got = _viewports.get(page)
    except TypeError:
        return None
    if got and time.monotonic() - got[0] < _VIEWPORT_TTL_S:
        return got[1]
    return None


def _remember_viewport(page: Any, vp: dict) -> None:
    try:
        _viewports[page] = (time.monotonic(), vp)
    except TypeError:
        pass


async def thumb(page: Any, viewport: dict | None = None) -> Thumb | None:
    """A grey quarter-scale thumbnail of the viewport, or None if it cannot be had.

    ``viewport`` (a previous thumbnail's) saves asking for the layout metrics again; a
    viewport that changed since then shows as a different size, which reads as changed.
    """
    try:
        from PIL import Image

        vp = viewport or _recent_viewport(page)
        if vp is None:
            metrics = await cursor._send(page, "Page.getLayoutMetrics")
            vp = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
            _remember_viewport(page, vp)
        width = float(vp.get("clientWidth") or 0)
        height = float(vp.get("clientHeight") or 0)
        if width <= 0 or height <= 0:
            return None
        shot = await cursor._send(
            page,
            "Page.captureScreenshot",
            {
                # PNG, not JPEG: compression noise at edges reads as change.
                "format": "png",
                # Faster, larger encoding: a thumbnail is compared and thrown away.
                "optimizeForSpeed": True,
                "fromSurface": True,
                "clip": {
                    "x": float(vp.get("pageX", 0)),
                    "y": float(vp.get("pageY", 0)),
                    "width": width,
                    "height": height,
                    "scale": THUMB_SCALE,
                },
            },
        )
        img = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("L")
        return Thumb(image=img, scale=img.size[0] / width, viewport=vp)
    except Exception:  # noqa: BLE001 — evidence is a report, never a failure
        return None


def _masked(img: Any, boxes: list[tuple[float, float]], scale: float) -> Any:
    """Black out the cursor wherever it may show in a thumbnail.

    The DevTools overlay is not scaled with a clipped capture: measured on Chrome 157,
    an arrow at CSS (80, 56) shows full size at thumbnail pixel (80, 56) of a quarter-scale
    capture, not at (20, 14). Both places are masked, so a Chrome that does scale it is
    covered too.
    """
    if not boxes:
        return img
    from PIL import ImageDraw

    img = img.copy()
    draw = ImageDraw.Draw(img)
    x0, y0, x1, y1 = _CURSOR_BOX
    for x, y in boxes:
        draw.rectangle(
            [(x + x0) * scale, (y + y0) * scale, (x + x1) * scale, (y + y1) * scale], fill=0
        )
        draw.rectangle([x + x0, y + y0, x + x1, y + y1], fill=0)
    return img


def compare(before: Thumb, after: Thumb, *, ignore: list[tuple[float, float]] = ()) -> dict:
    """``{changed, ratio, region}`` between two thumbnails, ``region`` in CSS px."""
    from PIL import ImageChops

    a, b = before.image, after.image
    if a.size != b.size:
        return {"changed": True, "ratio": 1.0, "region": None, "note": "the viewport resized"}
    a = _masked(a, list(ignore), before.scale)
    b = _masked(b, list(ignore), after.scale)
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > PIXEL_DELTA else 0)
    hist = diff.histogram()
    changed_px = hist[255]
    total = a.size[0] * a.size[1]
    ratio = changed_px / total if total else 0.0
    box = diff.getbbox()
    region = None
    if box and ratio > CHANGED_RATIO:
        s = after.scale
        region = [
            round(box[0] / s),
            round(box[1] / s),
            round((box[2] - box[0]) / s),
            round((box[3] - box[1]) / s),
        ]
    return {"changed": ratio > CHANGED_RATIO, "ratio": round(ratio, 4), "region": region}


class Evidence:
    """Before/after around one action. ``before()`` first, then ``apply(reply)``."""

    def __init__(self, ctx: Any, page: Any) -> None:
        self.ctx = ctx
        self.page = page
        self.on = bool(getattr(ctx.config, "evidence", False))
        self._before: Thumb | None = None
        self._url = ""
        self._cursor_from: tuple[float, float] | None = None

    async def before(self) -> None:
        if not self.on:
            return
        self._url = getattr(self.page, "url", "")
        self._cursor_from = cursor.position(self.page)
        self._before = await thumb(self.page)

    async def apply(self, reply: dict) -> None:
        if not self.on or self._before is None or reply.get("status") not in ("ok", None):
            return
        await asyncio.sleep(AFTER_SETTLE_S)
        if getattr(self.page, "url", "") != self._url:
            reply["visual_change"] = {
                "changed": True,
                "ratio": 1.0,
                "region": None,
                "note": "the page navigated",
            }
            return
        after = await thumb(self.page, self._before.viewport)
        if after is None:
            return
        ignore = [p for p in (self._cursor_from, cursor.position(self.page)) if p]
        reply["visual_change"] = compare(self._before, after, ignore=ignore)


async def wait_stable(page: Any, *, timeout_s: float, interval_s: float = 0.15) -> dict:
    """Wait until two thumbnails ``interval_s`` apart are the same, for at most
    ``timeout_s``. Web fonts are waited for first (bounded by the same clock)."""
    started = time.monotonic()
    deadline = started + timeout_s
    try:
        await asyncio.wait_for(
            page.evaluate("() => document.fonts ? document.fonts.ready.then(() => true) : true"),
            max(0.05, deadline - time.monotonic()),
        )
    except Exception:  # noqa: BLE001 — fonts are a nicety; pixels decide
        pass
    ignore = [p for p in (cursor.position(page),) if p]
    prev = await thumb(page)
    if prev is None:
        return {"stable": None, "waited_ms": round((time.monotonic() - started) * 1000)}
    last_ratio = None
    while True:
        await asyncio.sleep(interval_s)
        cur = await thumb(page)
        if cur is None:
            return {"stable": None, "waited_ms": round((time.monotonic() - started) * 1000)}
        result = compare(prev, cur, ignore=ignore)
        last_ratio = result["ratio"]
        if not result["changed"]:
            return {"stable": True, "waited_ms": round((time.monotonic() - started) * 1000)}
        if time.monotonic() >= deadline:
            return {
                "stable": False,
                "waited_ms": round((time.monotonic() - started) * 1000),
                "last_change": result,
                "last_ratio": last_ratio,
            }
        prev = cur


ACTION_TOOLS = frozenset({"click", "type_text", "press_key", "hover", "scroll", "select_option"})


def around(ctx: Any):
    """Wrap an action tool: thumbnail before, ``visual_change`` after, and with
    ``capture=true`` the after-image (cursor included) as ``image_path``.

    The page is the session's current one, read without claiming it: a refused or
    failed action costs one thumbnail and gets no ``visual_change``.
    """

    def wrap(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            page = getattr(ctx.session, "_page", None)
            ev = Evidence(ctx, page) if page is not None else None
            if ev is not None:
                await ev.before()
            result = await fn(*args, **kwargs)
            if not isinstance(result, dict) or result.get("status") != "ok":
                return result
            now = getattr(ctx.session, "_page", None)
            if ev is not None and now is page:
                await ev.apply(result)
            if kwargs.get("capture") and now is not None:
                from .tools.reading import capture_page

                shot = await capture_page(ctx, now, tool=f"{fn.__name__}.capture", kind="action")
                result["image_path"] = shot["image_path"]
                result["capture"] = {k: shot[k] for k in ("width", "height", "scale") if k in shot}
            if now is page and now is not None:
                await point_the_next_look(ctx, now, result)
            return result

        return wrapper

    return wrap


async def point_the_next_look(ctx: Any, page: Any, reply: dict) -> None:
    """Make the next look cheap: where the change is in the latest screenshot's pixels
    (``region_px``, ready for ``screenshot(region=...)``), and, when named snapshots are
    held, which one to compare with."""
    change = reply.get("visual_change")
    if not isinstance(change, dict):
        return
    region = change.get("region")
    if region:
        px = await region_in_latest(page, region)
        if px is not None:
            change["region_px"] = px
    snapshots = getattr(ctx, "snapshots", None)
    names = snapshots.names() if snapshots is not None else []
    if change.get("changed") and names:
        reply["snapshots"] = names
        reply["compare_hint"] = (
            f'screenshot(compare_to="{names[-1]}") shows what changed since that snapshot.'
        )


async def region_in_latest(page: Any, region: list) -> list[int] | None:
    """A viewport CSS rect in pixels of the page's latest screenshot, or None.

    None when there is no screenshot, when the latest is a zoomed region (its own small
    frame), or when the page has scrolled since a viewport capture (its pixels moved).
    """
    from . import marks

    geometry = marks.last(page)
    if geometry is None or geometry.origin != (0.0, 0.0):
        return None
    now = await marks.page_geometry(page)
    if now is None:
        return None
    x, y, w, h = (float(v) for v in region)
    if geometry.full_page:
        x, y = x + now[3], y + now[4]
    elif abs(now[3] - geometry.scroll[0]) > 1 or abs(now[4] - geometry.scroll[1]) > 1:
        return None
    s = geometry.scale
    return [round(x * s), round(y * s), round(w * s), round(h * s)]
