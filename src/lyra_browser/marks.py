"""What a capture means in page coordinates: scale, marks, and points to act on.

A screenshot is pixels; acting on what it shows needs the way back to the page. Every
viewport capture records its ``scale`` (image pixels per CSS pixel: the device pixel
ratio times any downscaling to ``capture_max_edge``), so a point read off the image is
``x / scale`` CSS pixels into the viewport. ``click``/``hover`` with ``x``/``y`` use the
last capture of that page to make that conversion.

``annotate`` draws set-of-marks on the PNG, never in the page: a numbered box for each
actionable element on screen, numbered in the order of the tree, each tied to the
``aria-ref`` that ``read_page(mode="tree")`` prints for it. The page's DOM is not touched,
so its scripts see nothing and its layout does not move.
"""

from __future__ import annotations

import io
import weakref
from dataclasses import dataclass
from typing import Any

# Marks colour: an orange no page uses for its own focus rings, readable on white and dark.
MARK_RGB = (255, 120, 0)
LABEL_TEXT_RGB = (255, 255, 255)
MAX_MARKS = 80

_GEOMETRY_JS = (
    "[window.devicePixelRatio || 1, window.innerWidth, window.innerHeight,"
    " window.scrollX, window.scrollY,"
    " Math.max(document.documentElement.scrollWidth, window.innerWidth)]"
)


@dataclass(frozen=True)
class Geometry:
    """How the last capture of a page maps to its viewport."""

    scale: float
    full_page: bool
    scroll: tuple[float, float]
    viewport: tuple[float, float]


_last: weakref.WeakKeyDictionary[Any, Geometry] = weakref.WeakKeyDictionary()


async def page_geometry(page: Any) -> tuple[float, ...] | None:
    """(device pixel ratio, viewport width, height, scrollX, scrollY, document width)."""
    try:
        got = await page.evaluate(_GEOMETRY_JS)
        return tuple(float(v) for v in got)
    except Exception:  # noqa: BLE001 — a page that cannot say keeps scale unknown
        return None


def remember(page: Any, geometry: Geometry) -> None:
    try:
        _last[page] = geometry
    except TypeError:
        pass


def last(page: Any) -> Geometry | None:
    try:
        return _last.get(page)
    except TypeError:
        return None


def process(
    png: bytes,
    *,
    css_width: float | None,
    max_edge: int,
    marks: list[dict] | None = None,
    dpr: float = 1.0,
) -> tuple[bytes, float]:
    """Downscale ``png`` to ``max_edge`` and draw ``marks``; return it and its scale.

    The scale is measured, not assumed: the image's width over the CSS width it shows
    (``css_width``). The page's ``devicePixelRatio`` is not enough: Playwright captures
    at its own emulated ratio whatever the page reports (measured: an image 1280 px
    wide while the page said 2). ``dpr`` is the fallback when no width is known.
    ``marks`` carry ``n`` and ``box_css`` (left, top, width, height in CSS px of the
    captured area); each gets ``box_px`` filled in. A PNG Pillow cannot read is returned
    as it is.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover — pillow is a base dependency
        return png, dpr
    try:
        img = Image.open(io.BytesIO(png))
        img.load()
    except Exception:  # noqa: BLE001
        return png, dpr
    w, h = img.size
    scale = w / css_width if css_width else dpr
    if max_edge and max(w, h) > max_edge:
        ratio = max_edge / max(w, h)
        img = img.resize((max(1, round(w * ratio)), max(1, round(h * ratio))), Image.LANCZOS)
        scale *= ratio
    if marks:
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        size = max(11, round(13 * min(scale, 2.0)))
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # Pillow without FreeType sizes
            font = ImageFont.load_default()
        width = max(2, round(2 * min(scale, 2.0)))
        for mark in marks:
            x, y, bw, bh = mark["box_css"]
            box = [
                round(x * scale),
                round(y * scale),
                round((x + bw) * scale),
                round((y + bh) * scale),
            ]
            mark["box_px"] = [box[0], box[1], box[2] - box[0], box[3] - box[1]]
            draw.rectangle(box, outline=MARK_RGB, width=width)
            label = str(mark["n"])
            tl = draw.textbbox((0, 0), label, font=font)
            tw, th = tl[2] - tl[0], tl[3] - tl[1]
            lx = max(0, box[0])
            ly = box[1] - th - 6 if box[1] - th - 6 >= 0 else box[1]
            draw.rectangle([lx, ly, lx + tw + 6, ly + th + 6], fill=MARK_RGB)
            draw.text((lx + 3 - tl[0], ly + 3 - tl[1]), label, fill=LABEL_TEXT_RGB, font=font)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), scale


def marks_from(elements: list, *, offset: tuple[float, float] = (0.0, 0.0)) -> list[dict]:
    """Marks for the tree's on-screen elements with a box and a ref, in tree order.

    ``offset`` shifts viewport boxes into the captured area (a full-page capture starts
    at the top of the document, so it is the scroll position).
    """
    marks: list[dict] = []
    for element in elements:
        if not element.in_view or not element.ref or element.box is None:
            continue
        left, top, right, bottom = element.box
        if right - left < 1 or bottom - top < 1:
            continue
        marks.append(
            {
                "n": len(marks) + 1,
                "ref": element.ref,
                "role": element.role,
                "name": element.name[:80],
                "box_css": [
                    round(left + offset[0], 1),
                    round(top + offset[1], 1),
                    round(right - left, 1),
                    round(bottom - top, 1),
                ],
            }
        )
        if len(marks) >= MAX_MARKS:
            break
    return marks


def to_viewport(page: Any, x: float, y: float, current: tuple | None) -> tuple[float, float]:
    """A point in the last capture's image pixels as CSS pixels in the viewport now.

    With no capture of this page yet, ``x``/``y`` are taken as CSS pixels.
    ``current`` is ``page_geometry`` now (for the scroll of a full-page capture).
    """
    geometry = last(page)
    if geometry is None:
        return float(x), float(y)
    cx, cy = x / geometry.scale, y / geometry.scale
    if geometry.full_page and current is not None:
        cx -= current[3]
        cy -= current[4]
    return cx, cy
