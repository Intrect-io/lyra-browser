"""``aria_snapshot``: Chrome's accessibility tree, written the way Playwright writes its own.

``read_page(mode="tree")`` reads Playwright's ``aria_snapshot(mode="ai", boxes=True)``
through ``tools.reading.compact_snapshot``. This produces the same text from
``Accessibility.getFullAXTree``, frame by frame (an out-of-process iframe through its own
session), so the reader does not change:

    - button "Save" [disabled] [ref=e12] [box=964,128,46,22]
    - link "Home" [ref=e3] [box=8,8,40,20]
      - /url: /home
    - iframe [ref=e31] [box=269,337,404,124]
      - button "Inner" [ref=f1e2] [box=8,8,120,22]

- Roles are Chrome's computed ARIA roles; the few internal ones it adds are mapped back
  (``MenuListOption`` is an ``option``, ``StaticText`` a ``text``) or skipped.
- ``box`` is the element's content box in its own frame's viewport (CSS pixels), as
  Playwright gives it; ``compact_snapshot`` adds the frame offsets itself.
- A ref goes to every rendered node with a non-empty box, ``e<n>`` in the top frame and
  ``f<k>e<n>`` in the k-th frame met. Refs belong to the latest snapshot: taking one
  replaces the page's ref table, which ``aria-ref=`` selectors resolve against.

Measured in the phase-0 spike: 324 of 324 listed elements matched Playwright's (role,
name, states, href) on a page with forms, a same-site and a cross-site iframe, an open
shadow root, Korean text and a 300-button list.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from .errors import ProtocolError

if TYPE_CHECKING:
    from .frames import FrameRef
    from .page import NativePage

# Chrome's internal roles, mapped to what Playwright prints, or None to print nothing
# (the children are kept, one level up).
_ROLE_MAP: dict[str, str | None] = {
    "MenuListOption": "option",
    "MenuListPopup": None,
    "RootWebArea": None,
    "WebArea": None,
    "StaticText": "text",
    "InlineTextBox": None,
    "LineBreak": None,
    "LabelText": None,
    "DisclosureTriangle": "group",
    "Iframe": "iframe",
    "IframePresentational": "iframe",
    "image": "img",
    "generic": "generic",
    "none": None,
    "ListMarker": None,
}
# Nodes that get neither a ref nor a box: text runs, and wrappers nobody acts on.
_NO_REF = frozenset({"text"})
_STATES = ("checked", "disabled", "expanded", "pressed", "selected", "level")


def _prop(node: dict, name: str):
    for prop in node.get("properties", ()):
        if prop.get("name") == name:
            return (prop.get("value") or {}).get("value")
    return None


def _states(node: dict) -> list[str]:
    out = []
    for state in _STATES:
        value = _prop(node, state)
        if value is None or value is False or value == "false":
            continue
        out.append(state if value is True or value == "true" else f"{state}={value}")
    return out


class _Builder:
    def __init__(self, page: NativePage, *, refs: bool, boxes: bool, depth: int | None) -> None:
        self._page = page
        self._refs = refs
        self._boxes = boxes
        self._depth = depth
        self._frames_seen = 0
        self.table: dict[str, tuple[str, int]] = {}

    async def frame(
        self, frame: FrameRef, root_backend: int | None, prefix: str, indent: int
    ) -> list[str]:
        page = self._page
        params = {} if page._frame_map.is_out_of_process(frame.id) else {"frameId": frame.id}
        try:
            nodes = (await page._send_on(frame.session, "Accessibility.getFullAXTree", params)).get(
                "nodes", []
            )
        except ProtocolError:
            return []
        if not nodes:
            return []
        by_id = {n["nodeId"]: n for n in nodes}
        root = None
        if root_backend is not None:
            root = next((n for n in nodes if n.get("backendDOMNodeId") == root_backend), None)
        if root is None:
            root = next((n for n in nodes if "parentId" not in n), nodes[0])

        # Pass 1: which nodes print, at which depth.
        printed: list[tuple[dict, str, int]] = []

        def walk(node: dict, depth: int) -> None:
            role = node.get("role", {}).get("value", "")
            mapped = _ROLE_MAP.get(role, role)
            below = depth
            if not node.get("ignored") and mapped:
                printed.append((node, mapped, depth))
                below = depth + 1
            if mapped == "iframe" or (self._depth is not None and below - indent > self._depth):
                return
            for child_id in node.get("childIds", ()):
                child = by_id.get(child_id)
                if child is not None:
                    walk(child, below)

        walk(root, indent)

        # Pass 2: boxes (one round, concurrently) and the frames iframes own.
        origin = await page._frame_map.frame_origin(frame)
        boxed = [n for n, role, _d in printed if role not in _NO_REF and n.get("backendDOMNodeId")]
        links = [n for n, role, _d in printed if role == "link" and n.get("backendDOMNodeId")]
        quads, hrefs = await asyncio.gather(
            asyncio.gather(*(self._box(frame, n["backendDOMNodeId"], origin) for n in boxed)),
            asyncio.gather(*(self._href(frame, n["backendDOMNodeId"]) for n in links)),
        )
        box_of = {id(n): q for n, q in zip(boxed, quads, strict=True)}
        href_of = {id(n): h for n, h in zip(links, hrefs, strict=True)}

        lines: list[str] = []
        counter = 0
        for node, role, depth in printed:
            name = (node.get("name") or {}).get("value", "") or ""
            if role == "iframe":
                name = ""  # Playwright names a frame by its content, not its title
            attrs = _states(node)
            box = box_of.get(id(node))
            backend = node.get("backendDOMNodeId")
            if box is not None:
                if self._refs:
                    counter += 1
                    ref = f"{prefix}e{counter}"
                    attrs.append(f"ref={ref}")
                    self.table[ref] = (frame.id, backend)
                if self._boxes:
                    attrs.append("box=" + ",".join(str(round(v)) for v in box))
            key = role + (f" {json.dumps(str(name), ensure_ascii=False)}" if name else "")
            key += "".join(f" [{a}]" for a in attrs)
            lines.append(f"{'  ' * depth}- {key}")
            url = href_of.get(id(node)) or _prop(node, "url")
            if role == "link" and url:
                lines.append(f"{'  ' * (depth + 1)}- /url: {url}")
            if role == "iframe" and backend:
                lines.extend(await self._child_frame(frame, backend, depth + 1))
        return lines

    async def _child_frame(self, frame: FrameRef, backend: int, indent: int) -> list[str]:
        page = self._page
        try:
            node = (
                await page._send_on(frame.session, "DOM.describeNode", {"backendNodeId": backend})
            )["node"]
        except ProtocolError:
            return []
        child_id = node.get("frameId")
        if not child_id:
            return []
        self._frames_seen += 1
        return await self.frame(page._frame(child_id), None, f"f{self._frames_seen}", indent)

    async def _href(self, frame: FrameRef, backend: int) -> str | None:
        """A link's ``href`` as written, which is what Playwright prints (Chrome's tree has
        the resolved URL)."""
        try:
            node = (
                await self._page._send_on(
                    frame.session, "DOM.describeNode", {"backendNodeId": backend}
                )
            )["node"]
        except ProtocolError:
            return None
        attrs = node.get("attributes") or []
        for name, value in zip(attrs[0::2], attrs[1::2], strict=False):
            if name == "href":
                return value
        return None

    async def _box(
        self, frame: FrameRef, backend: int, origin: tuple[float, float]
    ) -> tuple[float, float, float, float] | None:
        try:
            quads = (
                await self._page._send_on(
                    frame.session, "DOM.getContentQuads", {"backendNodeId": backend}
                )
            ).get("quads", [])
        except ProtocolError:
            return None
        if not quads:
            return None
        xs = quads[0][0::2]
        ys = quads[0][1::2]
        x, y = min(xs), min(ys)
        width, height = max(xs) - x, max(ys) - y
        if width <= 0 or height <= 0:
            return None
        return x - origin[0], y - origin[1], width, height


async def aria_snapshot(
    page: NativePage,
    frame: FrameRef,
    root_backend: int | None,
    *,
    refs: bool,
    boxes: bool,
    depth: int | None = None,
) -> str:
    """The snapshot of ``root_backend``'s subtree; with ``refs``, the page's new ref table."""
    builder = _Builder(page, refs=refs, boxes=boxes, depth=depth)
    lines = await builder.frame(frame, root_backend, "", 0)
    if refs:
        page._refs = builder.table
    return "\n".join(lines)
