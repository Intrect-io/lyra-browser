"""Named snapshots and before/after comparison of captures.

``screenshot(name="before")`` keeps the capture; ``screenshot(compare_to="before")`` takes
a new one and says what changed: the share of pixels, and boxes around the changed regions,
drawn on the image it attaches. That is the "look, fix, reload, look again" loop of checking
a change by sight.

Both captures are taken without lyra's cursor (it would be a change of its own), and two
captures of a different scroll position, viewport or scale are not compared pixel by pixel:
that answers ``mismatch``, since every pixel would move and the boxes would say nothing.

Snapshots live in memory only, per server, capped in count and bytes (oldest dropped), and
are dropped when the browser closes or changes hands.
"""

from __future__ import annotations

import io
import time
from collections import OrderedDict
from dataclasses import dataclass

MAX_SNAPSHOTS = 8
MAX_BYTES = 48 * 1024 * 1024
# A pixel counts as changed when a channel moved by more than this (0..255): above PNG
# and compositor noise (0 on a reload, measured), below a slight recolour (~16-20).
PIXEL_THRESHOLD = 10
# Changes are gathered in cells of this many image px; changed cells closer than one cell
# apart become one box.
CELL = 8
MAX_BOXES = 20
BOX_RGB = (230, 0, 120)


@dataclass(frozen=True)
class Snapshot:
    name: str
    png: bytes
    scale: float
    viewport: tuple[float, float]
    scroll: tuple[float, float]
    url: str
    taken_at: float


class Snapshots:
    """The named snapshots of one server, oldest first."""

    def __init__(self, max_count: int = MAX_SNAPSHOTS, max_bytes: int = MAX_BYTES) -> None:
        self.max_count = max_count
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, Snapshot] = OrderedDict()

    def put(self, snap: Snapshot) -> list[str]:
        """Keep ``snap`` under its name; return the names dropped to stay in the caps."""
        self._items.pop(snap.name, None)
        self._items[snap.name] = snap
        dropped = []
        while len(self._items) > self.max_count or (
            len(self._items) > 1 and self.bytes() > self.max_bytes
        ):
            name, _ = self._items.popitem(last=False)
            dropped.append(name)
        return dropped

    def get(self, name: str) -> Snapshot | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return list(self._items)

    def bytes(self) -> int:
        return sum(len(s.png) for s in self._items.values())

    def clear(self) -> None:
        self._items.clear()


def mismatch(before: Snapshot, scale: float, viewport, scroll) -> str | None:
    """Why two captures cannot be compared pixel by pixel, or None."""
    if abs(before.viewport[0] - viewport[0]) > 0.5 or abs(before.viewport[1] - viewport[1]) > 0.5:
        return "viewport"
    if abs(before.scale - scale) > 0.005:
        return "scale"
    if abs(before.scroll[0] - scroll[0]) > 1 or abs(before.scroll[1] - scroll[1]) > 1:
        return "scroll"
    return None


def diff(before_png: bytes, after_png: bytes) -> tuple[float, list[list[int]]]:
    """(share of changed pixels, boxes [left, top, width, height] in image px)."""
    from PIL import Image, ImageChops

    a = Image.open(io.BytesIO(before_png)).convert("RGB")
    b = Image.open(io.BytesIO(after_png)).convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    r, g, bl = ImageChops.difference(a, b).split()
    delta = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    mask = delta.point(lambda v: 255 if v > PIXEL_THRESHOLD else 0)
    hist = mask.histogram()
    changed = hist[255]
    total = a.size[0] * a.size[1]
    if not changed:
        return 0.0, []
    cols = -(-a.size[0] // CELL)
    rows = -(-a.size[1] // CELL)
    # Any changed pixel marks its cell (BOX average > 0).
    cells = mask.resize((cols, rows), Image.BOX)
    on = {(x, y) for y in range(rows) for x in range(cols) if cells.getpixel((x, y)) > 0}
    boxes: list[list[int]] = []
    for comp in _components(on):
        x0 = min(c[0] for c in comp) * CELL
        y0 = min(c[1] for c in comp) * CELL
        x1 = min(a.size[0], (max(c[0] for c in comp) + 1) * CELL)
        y1 = min(a.size[1], (max(c[1] for c in comp) + 1) * CELL)
        exact = mask.crop((x0, y0, x1, y1)).getbbox()
        if exact:
            boxes.append([x0 + exact[0], y0 + exact[1], exact[2] - exact[0], exact[3] - exact[1]])
    boxes = _merge_aligned(boxes)
    boxes.sort(key=lambda bx: bx[2] * bx[3], reverse=True)
    return changed / total, boxes[:MAX_BOXES]


# Two boxes whose tops and bottoms (or lefts and rights) agree within this many px,
ALIGN = 3
# one of them a sliver at most this thin across (the edge an element left or reached),
SLIVER = 8
# and at most this many times their shared extent apart, are one element that moved.
SPAN_GAP = 4.0


def _aligned(a: list[int], b: list[int]) -> bool:
    """The two edges of one element that moved.

    An element shifted by 3 px changes in a 3 px sliver at its leading and trailing edges
    (and wherever its content moved): separate regions with the same top and bottom, at
    least one of them thin. Two whole tiles recoloured side by side are neither thin, so
    they stay two boxes.
    """
    for lo, size in ((1, 3), (0, 2)):
        far, fsize = 1 - lo, 5 - size
        if abs(a[lo] - b[lo]) > ALIGN or abs(a[lo] + a[size] - b[lo] - b[size]) > ALIGN:
            continue
        if min(a[fsize], b[fsize]) > SLIVER:
            continue
        gap = max(a[far], b[far]) - min(a[far] + a[fsize], b[far] + b[fsize])
        if gap <= SPAN_GAP * max(a[size], b[size]):
            return True
    return False


def _merge_aligned(boxes: list[list[int]]) -> list[list[int]]:
    boxes = [list(b) for b in boxes]
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if _aligned(boxes[i], boxes[j]):
                    a, b = boxes[i], boxes.pop(j)
                    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
                    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
                    boxes[i] = [x0, y0, x1 - x0, y1 - y0]
                    merged = True
                    break
            if merged:
                break
    return boxes


def _components(on: set[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """Groups of cells, a cell joining a group when within two cells of one in it."""
    near = [(dx, dy) for dx in range(-2, 3) for dy in range(-2, 3)]
    seen: set[tuple[int, int]] = set()
    groups = []
    for start in sorted(on, key=lambda c: (c[1], c[0])):
        if start in seen:
            continue
        stack, comp = [start], []
        seen.add(start)
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy))
            fresh = [(cx + dx, cy + dy) for dx, dy in near]
            fresh = [n for n in fresh if n in on and n not in seen]
            seen.update(fresh)
            stack.extend(fresh)
        groups.append(comp)
    return groups


def outline(png: bytes, boxes: list[list[int]], scale: float) -> bytes:
    """``png`` with each box outlined (a little outside it, so the change stays visible)."""
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    width = max(2, round(2 * min(scale, 2.0)))
    for x, y, w, h in boxes:
        pad = width + 1
        draw.rectangle([x - pad, y - pad, x + w + pad, y + h + pad], outline=BOX_RGB, width=width)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def describe(boxes: list[list[int]], scale: float) -> list[dict]:
    return [
        {
            "box_px": box,
            "box_css": [round(v / scale, 1) for v in box],
        }
        for box in boxes
    ]


def now() -> float:
    return time.monotonic()
