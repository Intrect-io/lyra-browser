"""Reading tools: extract what the page says and capture what it shows.

Reads are non-mutating, so they are not blocked by a user takeover — the agent
can keep watching the page while the user drives.

``read_page`` answers two different questions. ``mode="text"`` is what a person
reads: the rendered text. ``mode="tree"`` is what a person *does*: one line per
thing on the page that can be clicked, typed into or chosen, each with a handle
(``aria-ref=e12``) that click and type_text accept as a selector. The tree is
built from Playwright's AI aria snapshot, which reaches into open shadow roots
and iframes and names things no CSS selector can (icon-only links, a page full
of identical "Read more"), and is then reduced: the raw snapshot is mostly
structure the model cannot act on, several times the size of the page's text.

Captures are files (see ``capture.py``): the tool result carries a path, and
the client attaches the image to the model's next turn. Base64 is opt-in.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass

from fastmcp import FastMCP

from ..capture import envelope, save_capture, take
from ..context import ServerContext, acquire_page

_MODES = frozenset({"text", "tree"})

# --------------------------------------------------------------------------
# Structural snapshot: the AI aria snapshot, reduced to what can be acted on
# --------------------------------------------------------------------------

# What earns a line. Lists, tables, wrappers and paragraphs are what make the raw
# snapshot large; the model needs the things it can act on, and headings to tell
# regions apart. ``option`` is listed only when it carries a ref (see _wanted).
_ACTIONABLE = frozenset(
    {
        "link",
        "button",
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "spinbutton",
        "tab",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "treeitem",
    }
)
_LISTED = _ACTIONABLE | {"heading"}

# For these the text after the colon is what the user typed or chose, not what
# the control is called. It is never shown — a password field arrives in the
# snapshot with its value — and never mistaken for a name.
_VALUE_ROLES = frozenset({"textbox", "searchbox", "combobox", "listbox", "slider", "spinbutton"})

# The states a model needs to decide what to do: is the box already ticked, is
# the button usable, which tab is open. The rest (focus, cursor, geometry) is
# noise to it.
_STATE = re.compile(r"checked(?:=mixed)?|disabled|expanded|pressed(?:=mixed)?|selected|level=\d+")

_NAME_MAX = 120
_HREF_MAX = 200

# ``- role "name" [attr] [attr]: inline``. A key that YAML cannot leave bare (a
# name with ``: `` or `` #`` in it) arrives wrapped in single quotes; the name
# inside is always a double-quoted literal with backslash escapes.
_LINE = re.compile(r"( *)- (.*)")
_KEY = re.compile(r'(?P<role>[\w-]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]*\])*)')
_ATTR = re.compile(r"\[([^\]]*)\]")
_ESCAPE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|(.))", re.DOTALL)
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_BOX = re.compile(r"box=(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)")


@dataclass(slots=True)
class Element:
    """One line of the tree: something on the page a model can act on."""

    role: str
    name: str = ""
    ref: str = ""
    href: str = ""
    states: tuple[str, ...] = ()
    # Whether any part is on screen right now; None when no geometry was given.
    in_view: bool | None = None

    def line(self) -> str:
        """``aria-ref=e4 link "Home" -> /home``.

        The ref comes first and exactly as a selector, so it can be copied
        verbatim into click/type_text. Names and hrefs are shortened: a card
        that is one big link would otherwise cost more than everything around it.
        """
        parts = []
        if self.ref:
            parts.append(f"aria-ref={self.ref}")
        parts.append(self.role)
        if self.name:
            parts.append(json.dumps(_clip(self.name, _NAME_MAX), ensure_ascii=False))
        parts.extend(f"[{state}]" for state in self.states)
        if self.href:
            parts.append(f"-> {_clip(self.href, _HREF_MAX)}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Tree:
    """What ``compact_snapshot`` makes of a snapshot."""

    elements: list[Element]
    # How many leading elements are on screen; None when they were not reordered.
    in_viewport: int | None = None

    @property
    def text(self) -> str:
        """One element per line, every line newline-terminated."""
        return "".join(f"{element.line()}\n" for element in self.elements)


@dataclass(slots=True)
class _Node:
    role: str
    name: str = ""
    attrs: tuple[str, ...] = ()
    inline: str = ""


# left, top, right, bottom
_Rect = tuple[float, float, float, float]


@dataclass(slots=True)
class _Frame:
    """An iframe whose children are measured from its own top-left corner."""

    depth: int
    x: float
    y: float
    clip: _Rect


def _scrub(text: str) -> str:
    """One line, single spaces, and valid UTF-8.

    Sites cut labels with ``substring`` and leave half an emoji; a lone surrogate
    cannot be encoded, so one such name would fail the whole response.
    """
    return " ".join(text.split()).encode("utf-8", "replace").decode("utf-8")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unescape(text: str) -> str:
    if "\\" not in text:
        return text

    def one(match: re.Match[str]) -> str:
        byte, unit, char = match.groups()
        if byte or unit:
            return chr(int(byte or unit, 16))
        return _SIMPLE_ESCAPES.get(char, char)

    return _ESCAPE.sub(one, text)


def _scalar(text: str) -> str:
    """A YAML value as Playwright writes it: bare, or double-quoted."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return _unescape(text[1:-1])
    return text


def _unquote_key(content: str) -> tuple[str, str]:
    """Split ``'a key with '' in it': rest`` into the key and what follows it."""
    chars: list[str] = []
    i = 1
    while i < len(content):
        if content[i] == "'":
            if content[i + 1 : i + 2] == "'":
                chars.append("'")
                i += 2
                continue
            return "".join(chars), content[i + 1 :]
        chars.append(content[i])
        i += 1
    return "".join(chars), ""


def _parse_content(content: str) -> _Node | None:
    """Read the part of a snapshot line after ``- ``; None when it is not a node."""
    if content.startswith("/"):  # a property of the node above: ``/url: /home``
        prop, _, value = content.partition(":")
        return _Node(role=prop, inline=_scalar(value))
    if content.startswith("'"):
        key, rest = _unquote_key(content)
        match = _KEY.match(key)
    else:
        match = _KEY.match(content)
        rest = content[match.end() :] if match else ""
    if match is None:
        return None
    return _Node(
        role=match["role"],
        name=_unescape(match["name"] or ""),
        attrs=tuple(_ATTR.findall(match["attrs"] or "")),
        inline=_scalar(rest[1:]) if rest.startswith(":") else "",
    )


def _ref_of(attrs: tuple[str, ...]) -> str:
    return next((a[4:] for a in attrs if a.startswith("ref=")), "")


def _box_of(attrs: tuple[str, ...]) -> _Rect | None:
    """``[box=x,y,w,h]`` as left, top, right, bottom."""
    for attr in attrs:
        match = _BOX.fullmatch(attr)
        if match:
            x, y, w, h = (float(v) for v in match.groups())
            return x, y, x + w, y + h
    return None


def _wanted(node: _Node, ref: str, refs: bool) -> bool:
    if node.role not in _LISTED:
        return False
    if refs:
        # The snapshot hands a ref only to what is rendered with a non-empty box
        # (measured: a zero-size button and a closed <select>'s options have
        # none). Without one the element is there but nobody can see or click it.
        return bool(ref)
    # No refs, so nothing tells a visible option from the two hundred in a
    # closed <select>; the select itself is listed and select_option handles it.
    return node.role != "option"


def _on_screen(rect: _Rect, clip: _Rect) -> bool:
    """Whether any part of a non-empty ``rect`` lies inside ``clip``."""
    left, top, right, bottom = rect
    return (
        right > left
        and bottom > top
        and left < clip[2]
        and right > clip[0]
        and top < clip[3]
        and bottom > clip[1]
    )


def _clipped(clip: _Rect, rect: _Rect) -> _Rect:
    """What of ``clip`` is left once it is narrowed to ``rect``."""
    return (
        max(clip[0], rect[0]),
        max(clip[1], rect[1]),
        min(clip[2], rect[2]),
        min(clip[3], rect[3]),
    )


def compact_snapshot(raw: str, *, refs: bool, viewport: tuple[float, float] | None = None) -> Tree:
    """Reduce an aria snapshot to the elements a model can act on.

    ``raw`` is ``aria_snapshot(mode="ai")`` text, or the plain kind when
    ``refs`` is False. With a ``viewport`` and ``[box=x,y,w,h]`` geometry in the
    snapshot, what is on screen right now is listed first, each group in
    document order. Values of inputs are dropped on purpose.
    """
    elements: list[Element] = []
    # Kept elements with no name yet, waiting for a named descendant. A link that
    # wraps one image arrives without a name and with ``img "Logo"`` under it.
    nameless: list[tuple[int, Element]] = []
    owners: list[Element | None] = []  # the listed element at each depth above
    frames: list[_Frame] = []
    bounds = (0.0, 0.0, viewport[0], viewport[1]) if viewport else None

    for line in raw.split("\n"):
        found = _LINE.fullmatch(line)
        node = _parse_content(found[2]) if found else None
        if found is None or node is None:
            continue
        depth = len(found[1]) // 2
        if node.role.startswith("/"):
            owner = owners[depth - 1] if 0 < depth <= len(owners) else None
            if node.role == "/url" and owner is not None:
                owner.href = _scrub(node.inline)
            continue

        while nameless and nameless[-1][0] >= depth:
            nameless.pop()
        while frames and frames[-1].depth >= depth:
            frames.pop()
        del owners[depth:]
        owners.extend([None] * (depth - len(owners)))

        label = node.name or ("" if node.role in _VALUE_ROLES else node.inline)
        if label and nameless:
            for _, waiting in nameless:
                waiting.name = _scrub(label)
            nameless.clear()

        ref = _ref_of(node.attrs)
        box = _box_of(node.attrs)
        element = None
        if _wanted(node, ref, refs):
            element = Element(
                role=node.role,
                name=_scrub(label),
                ref=ref,
                states=tuple(a for a in node.attrs if _STATE.fullmatch(a)),
            )
            elements.append(element)
            if not element.name:
                nameless.append((depth, element))
        owners.append(element)

        if box is not None and bounds is not None:
            ox, oy, clip = (
                (frames[-1].x, frames[-1].y, frames[-1].clip) if frames else (0.0, 0.0, bounds)
            )
            rect = (ox + box[0], oy + box[1], ox + box[2], oy + box[3])
            if element is not None:
                element.in_view = _on_screen(rect, clip)
            if node.role == "iframe":
                frames.append(_Frame(depth, rect[0], rect[1], _clipped(clip, rect)))

    if viewport is None or all(element.in_view is None for element in elements):
        return Tree(elements)
    near = [element for element in elements if element.in_view]
    far = [element for element in elements if not element.in_view]
    return Tree(near + far, in_viewport=len(near))


def _window(text: str, offset: int, max_chars: int, *, whole_lines: bool) -> tuple[str, int]:
    """The slice of ``text`` a page of output holds, and where it ended.

    Plain characters for text. For the tree a page is made of whole lines, both
    ends: a cut through ``aria-ref=e45`` leaves ``aria-ref=e4``, a valid ref to a
    different element. The end is snapped from the *requested* offset, not from
    the snapped start, so pages tile the text exactly whether a caller continues
    from ``next_offset`` or simply adds ``max_chars``.
    """
    total = len(text)
    start = min(offset, total)
    end = min(start + max_chars, total)
    if whole_lines:
        start = text.rfind("\n", 0, start) + 1
        end = text.rfind("\n", 0, end) + 1 if end < total else total
        if max_chars and end <= start < total:
            # One line longer than the whole budget: better over it than empty.
            end = text.find("\n", start) + 1
    return text[start:end], end


def _snapshot_options(root) -> tuple[dict, bool]:
    """``aria_snapshot`` arguments this Playwright understands, and whether it gives refs.

    ``mode="ai"`` (refs, iframes) arrived in Playwright 1.59 and ``boxes`` in
    1.60, while the declared floor is 1.58 — which is what a VEGA runtime ships.
    Asking it for ``mode`` would be a TypeError, so the signature decides.
    """
    params = inspect.signature(root.aria_snapshot).parameters
    if "mode" not in params:
        return {}, False
    options: dict = {"mode": "ai"}
    if "boxes" in params:
        options["boxes"] = True
    return options, True


_VIEWPORT_JS = "[window.innerWidth, window.innerHeight]"


async def _viewport(page) -> tuple[float, float] | None:
    """The size the snapshot's boxes are measured against.

    Asked of the page, not ``page.viewport_size``: a headful window has no
    emulated viewport, so that is None there.
    """
    try:
        width, height = await page.evaluate(_VIEWPORT_JS)
        return float(width), float(height)
    except Exception:  # noqa: BLE001 — ordering is a nicety; fall back to document order
        return None


# --------------------------------------------------------------------------
# Links of the text view
# --------------------------------------------------------------------------

_LINK_CAP = 100

# Runs in the page: DOM only, so it behaves the same under patchright's isolated
# world. Text is cut on code points, never mid-surrogate.
_LINKS_JS = r"""(root, {cap, nameMax}) => {
  const words = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const cut = (s) => {
    const chars = Array.from(s);
    return chars.length <= nameMax ? s : chars.slice(0, nameMax - 1).join('') + '…';
  };
  const shown = (a) => {
    if (!Array.from(a.getClientRects()).some((r) => r.width > 0 && r.height > 0)) return false;
    const v = getComputedStyle(a).visibility;
    return v !== 'hidden' && v !== 'collapse';
  };
  const label = (a) => {
    const text = words(a.innerText);
    if (text) return text;
    const named = words(a.getAttribute('aria-label')) || words(a.getAttribute('title'));
    if (named) return named;
    const img = a.querySelector('img[alt]');
    return img ? words(img.getAttribute('alt')) : '';
  };
  const anchors = Array.from(root.querySelectorAll('a[href]'));
  if (root.matches && root.matches('a[href]')) anchors.unshift(root);
  const seen = new Set();
  const links = [];
  let truncated = false;
  for (const a of anchors) {
    if (typeof a.href !== 'string' || !shown(a)) continue;
    const text = cut(label(a));
    const key = text + '\n' + a.href;
    if (seen.has(key)) continue;
    if (links.length >= cap) { truncated = true; break; }
    seen.add(key);
    links.push({text, href: a.href});
  }
  return {links, truncated};
}"""


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def get_url() -> dict:
        """Return the current URL and page title, and how many tabs are open (``tab_count``)."""
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        return {
            "url": page.url,
            "title": await page.title(),
            "tab_count": ctx.session.tab_info()["tab_count"],
        }

    @mcp.tool
    async def read_page(
        max_chars: int = 8000,
        mode: str = "text",
        selector: str = "",
        offset: int = 0,
        links: bool = False,
    ) -> dict:
        """Read the page as text, or as a map of what can be acted on.

        ``mode="text"`` (default) returns the rendered text (``innerText``), not
        HTML. ``links=true`` adds the page's visible links as ``{text, href}``
        — absolute, de-duplicated, at most 100 (tree lines carry their own href).

        ``mode="tree"`` returns one line per visible thing you can act on —
        links, buttons, inputs, selects, checkboxes, tabs, menu items — plus
        headings::

            aria-ref=e12 link "Pricing" -> /pricing
            aria-ref=e15 button "Save"
            aria-ref=e16 checkbox "Remember me" [checked]

        Pass the ``aria-ref=...`` token exactly as written as the ``selector`` of
        ``click`` or ``type_text``. It reaches what a text selector cannot name:
        icon-only links, several links with the same text, elements inside open
        shadow roots and iframes. A ref is good for the page it was read from and
        only while its element stays; a region read (``selector``) replaces the
        refs of the read before it. When a ref is rejected or matches nothing,
        read the tree again. Elements on screen come first (``in_viewport``
        counts them). Input values are never shown. ``refs: false`` means this
        Playwright predates aria refs: lines then carry none, so address elements
        with ``role=link[name="Pricing"]`` or ``text=`` selectors.

        ``selector`` limits either mode to one region (first match); it returns
        ``not_found`` at once when nothing matches. ``offset`` and ``max_chars``
        page through long output: ``total_chars`` is its full length,
        ``truncated`` says more follows, ``next_offset`` is where to continue.
        Tree pages hold whole lines, so a ref is never cut in half. Every call
        reads the page as it is now, so pages read while it changes or scrolls
        may not line up.
        """
        if mode not in _MODES:
            return {"status": "error", "reason": "mode must be 'text' or 'tree'.", "given": mode}
        if offset < 0 or max_chars < 0:
            return {"status": "error", "reason": "offset and max_chars must not be negative."}
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        selector = selector.strip()
        root = page.locator(selector or "body").first
        asked = {"mode": mode, "selector": selector, "offset": offset, "max_chars": max_chars}
        # Counted first: a missing element must not cost the 30s a locator waits
        # for it to appear.
        try:
            missing = await root.count() == 0
        except Exception:  # noqa: BLE001 — re-raised unless it is a ref
            # A ref from an earlier document is not a selector that matches
            # nothing: the driver refuses it ("Invalid frame in aria-ref
            # selector"). To a caller it is the same answer — it is not there.
            if not selector.startswith("aria-ref="):
                raise
            missing = True
        if missing:
            ctx.audit.record("read_page", asked, status="not_found")
            absent = {"status": "not_found", "selector": selector or "body", "url": page.url}
            if selector.startswith("aria-ref="):
                absent["hint"] = (
                    'Refs belong to the latest read_page(mode="tree") and stop working once '
                    "the page changes or a newer region read replaces them — read it again."
                )
            return absent

        extra: dict = {}
        if mode == "tree":
            options, refs = _snapshot_options(root)
            raw = await root.aria_snapshot(**options)
            viewport = await _viewport(page) if "boxes" in options else None
            tree = compact_snapshot(raw, refs=refs, viewport=viewport)
            text = tree.text
            extra = {"mode": "tree", "elements": len(tree.elements), "refs": refs}
            if tree.in_viewport is not None:
                extra["in_viewport"] = tree.in_viewport
        else:
            text = await root.inner_text()
            if links:
                found = await root.evaluate(_LINKS_JS, {"cap": _LINK_CAP, "nameMax": _NAME_MAX})
                extra = {"links": found["links"], "links_truncated": found["truncated"]}

        window, end = _window(text, offset, max_chars, whole_lines=mode == "tree")
        truncated = end < len(text)
        ctx.audit.record("read_page", {**asked, "links": links and mode == "text"})
        result = {
            "url": page.url,
            "title": await page.title(),
            "text": window,
            "truncated": truncated,
            "total_chars": len(text),
            **extra,
        }
        if truncated:
            result["next_offset"] = end
        return result

    @mcp.tool
    async def screenshot(full_page: bool = False, inline: bool = False) -> dict:
        """Capture the page as a PNG file and return its path.

        Use it to *see* the page — layout, images, charts, anything
        ``read_page`` cannot put into words. The file is what you look at;
        ``inline=true`` adds base64 only for a client without access to this
        machine's filesystem.
        """
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        png = await take(lambda: page.screenshot(full_page=full_page))
        path = save_capture(ctx.config, png, kind="page")
        ctx.audit.record(
            "screenshot", {"full_page": full_page, "image_path": str(path), "bytes": len(png)}
        )
        return envelope(path, png, url=page.url, inline=inline)

    @mcp.tool
    async def read_image(selector: str, inline: bool = False) -> dict:
        """Capture one element — an ``img``, ``canvas``, ``svg``, a chart, a
        map — as a PNG file and return its path.

        This is how you look at a picture on the page: it captures the pixels
        the element renders, so it works for anything drawn, not only ``img``.
        Nothing is fetched — no new request leaves the browser. Returns
        ``not_found`` when ``selector`` matches nothing.
        """
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        target = page.locator(selector).first
        if await target.count() == 0:
            ctx.audit.record("read_image", {"selector": selector}, status="not_found")
            return {"status": "not_found", "selector": selector, "url": page.url}
        png = await take(target.screenshot)
        path = save_capture(ctx.config, png, kind="element")
        ctx.audit.record(
            "read_image", {"selector": selector, "image_path": str(path), "bytes": len(png)}
        )
        return envelope(path, png, url=page.url, inline=inline)
