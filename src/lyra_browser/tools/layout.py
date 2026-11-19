"""``inspect_layout``: what the computed layout says is wrong on screen.

Checking a page by sight misses what is a pixel or a shade off. This reads the computed
styles and geometry of what is visible and names three kinds of fault:

- ``low_contrast``: text whose colour against what is painted behind it is under the WCAG
  AA ratio (4.5:1, or 3:1 for large text: 24 px, or 18.66 px bold). Disabled controls are
  exempt, as in WCAG 1.4.3;
- ``clipped``: text cut off by its own box or by an ancestor that clips (``overflow``
  hidden or clip), or spilling out of the bordered or filled box that holds it;
- ``covered``: text or a control that something painted lies on top of.

What is behind a text is read from the page's own paint order at the text
(``elementsFromPoint``), so a dark photo, video or canvas behind a heading makes its
background unknown (counted, never guessed), and colours in any CSS space (oklch, lab,
color-mix) are converted through a detached canvas.

Not judged, by design: text that is not visible (scrolled out of its scroller, clipped
away, ``opacity: 0``, visually-hidden ``sr-only`` text), text under a fixed or sticky
header (counted as ``under_fixed``), and text that cannot be hit-tested
(``pointer-events: none``, counted). Open shadow roots are scanned.

It only reads: no style, attribute or node is written, so page scripts see nothing (the
gate counts mutations). Typed values are never reported: a control is labelled by its
placeholder, ``aria-label`` or type, never by what is in it.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from .. import marks
from ..context import ServerContext, acquire_page
from ..devtools import clip
from ..origin import loggable_url

_READ_BUDGET_S = 5.0
MAX_FINDINGS = 50
MAX_ELEMENTS = 4000

# The scan, given the element to scan under (``root``) and its options.
_LAYOUT_FN = r"""(root, arg) => {
  const {cap, maxElements} = arg;
  const vw = innerWidth, vh = innerHeight;
  const findings = [];
  const counts = {scanned: 0, unknown_background: 0, under_fixed: 0, not_hit_testable: 0,
    contrast_checked: 0};
  let truncated = false;

  // Any CSS colour to sRGB, through a canvas that is never attached to the page.
  const paint = new OffscreenCanvas(1, 1).getContext('2d', {willReadFrequently: true});
  const colours = new Map();
  const rgba = (c) => {
    if (!c) return null;
    if (colours.has(c)) return colours.get(c);
    let out = null;
    const m = /^rgba?\(([^)]+)\)$/.exec(c);
    if (m) {
      const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
      out = [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    } else {
      paint.clearRect(0, 0, 1, 1);
      paint.fillStyle = '#000';
      paint.fillStyle = c;
      paint.fillRect(0, 0, 1, 1);
      const d = paint.getImageData(0, 0, 1, 1).data;
      out = [d[0], d[1], d[2], d[3] / 255];
    }
    colours.set(c, out);
    return out;
  };
  const lum = ([r, g, b]) => {
    const f = (v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const over = (top, under) => {  // top (with alpha) composited on an opaque colour
    const a = top[3];
    return [0, 1, 2].map((i) => top[i] * a + under[i] * (1 - a)).concat([1]);
  };
  const style = (n, pseudo) => getComputedStyle(n, pseudo);
  const parentOf = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
  const within = (outer, inner) => {  // inner is outer or inside it, across shadow roots
    for (let n = inner; n; n = parentOf(n)) if (n === outer) return true;
    return false;
  };
  const opacity = (el) => {
    let o = 1;
    for (let n = el; n && n.nodeType === 1; n = parentOf(n)) o *= Number(style(n).opacity);
    return o;
  };
  // The page canvas: white, or the dark one Chrome paints for a dark color-scheme.
  const canvas = (() => {
    const top = document.documentElement;
    const bg = rgba(style(top).backgroundColor);
    if (bg && bg[3] >= 1) return bg;
    const scheme = style(top).colorScheme || '';
    const dark = /dark/.test(scheme) &&
      (!/light/.test(scheme) || matchMedia('(prefers-color-scheme: dark)').matches);
    return dark ? [18, 18, 18, 1] : [255, 255, 255, 1];
  })();
  const MEDIA = new Set(['IMG', 'VIDEO', 'CANVAS', 'IFRAME', 'OBJECT', 'EMBED', 'PICTURE']);
  const isMedia = (n) => MEDIA.has(n.tagName) || n instanceof SVGSVGElement;
  // What is painted behind ``el`` at (x, y): the page's own paint order there, top
  // down, from just below el. An image, a video, a canvas or a gradient on the way makes
  // it unknown.
  const background = (el, x, y) => {
    const scope = el.getRootNode().elementsFromPoint ? el.getRootNode() : document;
    const stack = scope.elementsFromPoint(x, y);
    let i = stack.indexOf(el);
    const layers = [];
    const take = (n) => {
      const cs = style(n);
      if (isMedia(n) || (cs.backgroundImage && cs.backgroundImage !== 'none')) return false;
      const c = rgba(cs.backgroundColor);
      if (c && c[3] > 0) layers.push([c[0], c[1], c[2], c[3] * opacity(n)]);
      return true;
    };
    const opaque = () => layers.length && layers[layers.length - 1][3] >= 1;
    if (i < 0) {
      // Not hit-testable at that point: fall back to its own ancestors.
      for (let n = el; n && n.nodeType === 1 && !opaque(); n = parentOf(n)) {
        if (!take(n)) return null;
      }
    } else {
      if (!take(el)) return null;
      for (i += 1; i < stack.length && !opaque(); i++) {
        const n = stack[i];
        if (within(el, n)) continue;
        if (!take(n)) return null;
      }
    }
    let colour = canvas;
    for (let k = layers.length - 1; k >= 0; k--) colour = over(layers[k], colour);
    return colour;
  };
  const ownText = (el) => {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.replace(/\s+/g, ' ').trim();
  };
  const textRect = (el) => {
    let b = null;
    for (const n of el.childNodes) {
      if (n.nodeType !== 3 || !n.nodeValue.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        if (r.width < 1 || r.height < 1) continue;
        b = b ? {l: Math.min(b.l, r.left), t: Math.min(b.t, r.top),
                 r: Math.max(b.r, r.right), b: Math.max(b.b, r.bottom)}
              : {l: r.left, t: r.top, r: r.right, b: r.bottom};
      }
    }
    return b;
  };
  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + el.id;
    const cls = [...el.classList].slice(0, 2).join('.');
    if (cls) s += '.' + cls;
    const p = parentOf(el);
    if (p && p.nodeType === 1) {
      const same = [...p.children].filter((c) => c.tagName === el.tagName);
      if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      return describe(p).split(' > ').slice(-2).join(' > ') + ' > ' + s;
    }
    return s;
  };
  const add = (finding) => {
    if (findings.length >= cap) { truncated = true; return; }
    findings.push(finding);
  };
  const box = (b) =>
    [Math.round(b.l), Math.round(b.t), Math.round(b.r - b.l), Math.round(b.b - b.t)];
  const rectOf = (r) => ({l: r.left, t: r.top, r: r.right, b: r.bottom});
  const meet = (a, b) => ({l: Math.max(a.l, b.l), t: Math.max(a.t, b.t),
    r: Math.min(a.r, b.r), b: Math.min(a.b, b.b)});
  const area = (b) => Math.max(0, b.r - b.l) * Math.max(0, b.b - b.t);
  const VIEW = {l: 0, t: 0, r: vw, b: vh};
  const CUT = ['hidden', 'clip'];
  const scrolls = (cs) => ['auto', 'scroll'].includes(cs.overflowX) ||
    ['auto', 'scroll'].includes(cs.overflowY);
  // The part of ``b`` that ancestors let show, and the nearest box that cut some of it
  // off (a scroller hides on purpose and is not one).
  const showing = (el, b) => {
    let seen = meet(b, VIEW);
    let cutBy = null;
    for (let n = parentOf(el); n && n.nodeType === 1; n = parentOf(n)) {
      const cs = style(n);
      const clips = cs.overflowX !== 'visible' || cs.overflowY !== 'visible' ||
        (cs.clipPath && cs.clipPath !== 'none');
      if (!clips) continue;
      const before = area(seen);
      seen = meet(seen, rectOf(n.getBoundingClientRect()));
      const cuts = CUT.includes(cs.overflowX) || CUT.includes(cs.overflowY);
      if (area(seen) < before - 1 && cuts && !scrolls(cs) && !cutBy) cutBy = n;
    }
    return {seen, cutBy};
  };
  // Visually hidden on purpose: the sr-only pattern, a clip that hides it all, a fade-out.
  const hiddenOnPurpose = (el, cs, rect) => {
    if (rect.width <= 2 && rect.height <= 2) return true;
    if (cs.clip && /rect\(\s*0/.test(cs.clip)) return true;
    if (cs.clipPath && /inset\(\s*50%/.test(cs.clipPath)) return true;
    return opacity(el) < 0.1;
  };
  const isFixed = (n) => {
    for (let m = n; m && m.nodeType === 1; m = parentOf(m)) {
      const p = style(m).position;
      if (p === 'fixed' || p === 'sticky') return true;
    }
    return false;
  };
  // Something painted at (x, y): a background, an image or a control, or its own glyphs.
  const paintsAt = (n, x, y) => {
    const cs = style(n);
    if (isMedia(n) || n.matches('input, select, textarea, button')) return true;
    if (cs.backgroundImage && cs.backgroundImage !== 'none') return true;
    const bg = rgba(cs.backgroundColor);
    if (bg && bg[3] > 0.05) return true;
    const t = textRect(n);
    return !!t && x >= t.l && x <= t.r && y >= t.t && y <= t.b;
  };
  const CONTROLS = 'button, a[href], input, select, textarea, [role=button], [role=link]';
  const LABELLED = new Set(['submit', 'button', 'reset']);
  const labelOf = (el, text) => {
    if (text) return text.slice(0, 60);
    // A typed value is never reported (it may be a password).
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (el.tagName === 'INPUT' && LABELLED.has(t)) return (el.value || t).slice(0, 60);
    return (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
      el.getAttribute('title') || t || el.tagName.toLowerCase()).slice(0, 60);
  };

  const elements = [root];
  const collect = (node) => {
    for (const el of node.querySelectorAll('*')) {
      if (elements.length >= maxElements) { truncated = true; return; }
      elements.push(el);
      if (el.shadowRoot) collect(el.shadowRoot);
    }
  };
  if (root.shadowRoot) collect(root.shadowRoot);
  collect(root);

  const contrast = (el, cs, tr, label, colourOf) => {
    if (el.closest(':disabled, [aria-disabled=true]')) return;
    const fg = rgba(colourOf);
    if (!fg) return;
    const bg = background(el, (tr.l + tr.r) / 2, (tr.t + tr.b) / 2);
    if (!bg) { counts.unknown_background++; return; }
    counts.contrast_checked++;
    const seen = over([fg[0], fg[1], fg[2], fg[3] * opacity(el)], bg);
    const l1 = lum(seen), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700;
    const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
    if (ratio < need) add({kind: 'low_contrast', element: describe(el), text: label,
      box_css: box(tr), ratio: Math.round(ratio * 100) / 100, needs: need, color: colourOf,
      background: 'rgb(' + bg.slice(0, 3).map(Math.round).join(', ') + ')', font_px: size});
  };
  const spills = (el, tr, em) => {
    // Out of the box that draws it (a border or a fill); a scroller on the way stops it.
    for (let n = el; n && n.nodeType === 1 && n !== document.body; n = parentOf(n)) {
      const ns = style(n);
      if (scrolls(ns)) return null;
      const drawn = parseFloat(ns.borderTopWidth) > 0 || parseFloat(ns.borderLeftWidth) > 0 ||
        (rgba(ns.backgroundColor) || [0, 0, 0, 0])[3] > 0;
      if (!drawn || ns.display === 'inline') continue;
      const h = rectOf(n.getBoundingClientRect());
      const side = Math.max(h.l - tr.l, tr.r - h.r);
      // Glyph boxes run taller than a tight line-height: allow a third of an em.
      const vert = Math.max(h.t - tr.t, tr.b - h.b) - em / 3;
      const out = Math.max(side, vert);
      return out > 2 ? {out, holder: n, h} : null;
    }
    return null;
  };

  for (const el of elements) {
    if (el.namespaceURI !== 'http://www.w3.org/1999/xhtml') continue;  // SVG: fill, not color
    const cs = style(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const text = ownText(el);
    const isControl = el.matches(CONTROLS);
    if (!text && !isControl) continue;
    const rect = el.getBoundingClientRect();
    const tr = text ? textRect(el) : null;
    const extent = tr || rectOf(rect);
    if (area(extent) < 1) continue;
    if (cs.display !== 'contents' && hiddenOnPurpose(el, cs, rect)) continue;
    const {seen, cutBy} = showing(el, extent);
    if (area(seen) < 1) continue;  // scrolled away, clipped away, or off screen
    counts.scanned++;
    const label = labelOf(el, text);
    const em = parseFloat(cs.fontSize) || 16;

    if (text && tr) {
      contrast(el, cs, meet(tr, seen), label, cs.color);
      const ownCut = CUT.includes(cs.overflowX) || CUT.includes(cs.overflowY);
      const own = rectOf(rect);
      const pastOwn = Math.max(own.l - tr.l, tr.r - own.r);
      if (ownCut && pastOwn > 2) {
        add({kind: 'clipped', element: describe(el), text: label, box_css: box(own),
          detail: cs.textOverflow === 'ellipsis' ? 'text is truncated with an ellipsis'
            : 'text is cut off by its own box (overflow ' + cs.overflow + ')'});
      } else if (cutBy && area(seen) < area(meet(tr, VIEW)) - 1) {
        add({kind: 'clipped', element: describe(el), text: label, box_css: box(tr),
          detail: 'text is cut off by ' + describe(cutBy)});
      } else {
        const s = spills(el, tr, em);
        if (s) add({kind: 'clipped', element: describe(el), text: label, box_css: box(tr),
          detail: 'text spills ' + Math.round(s.out) + ' px out of ' + describe(s.holder),
          holder_css: box(s.h)});
      }
    } else if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) {
      // A control's own text: its value or its placeholder (the colour, never the value).
      const t = (el.getAttribute('type') || '').toLowerCase();
      const inner = meet(rectOf(rect), seen);
      if ((el.tagName === 'INPUT' && LABELLED.has(t)) || el.value) {
        contrast(el, cs, inner, label, cs.color);
      } else if (el.getAttribute('placeholder')) {
        contrast(el, cs, inner, label, style(el, '::placeholder').color);
      }
    }

    // What is on top: points across the text, or the centre of a control.
    if (cs.pointerEvents === 'none') { counts.not_hit_testable++; continue; }
    const probe = meet(extent, seen);
    const y = (probe.t + probe.b) / 2;
    const xs = tr ? [0.1, 0.3, 0.5, 0.7, 0.9].map((f) => probe.l + (probe.r - probe.l) * f)
      : [(probe.l + probe.r) / 2];
    const scope = el.getRootNode().elementFromPoint ? el.getRootNode() : document;
    let hidden = 0, by = null, fixed = 0;
    for (const x of xs) {
      const hit = scope.elementFromPoint(x, y);
      if (!hit || within(el, hit) || within(hit, el)) continue;
      if (!paintsAt(hit, x, y)) continue;  // a transparent click-catcher, not a cover
      if (isFixed(hit) && !isFixed(el)) { fixed++; continue; }
      hidden++;
      by = by || hit;
    }
    if (fixed) counts.under_fixed++;
    if (hidden) add({kind: 'covered', element: describe(el), text: label, box_css: box(probe),
      covered_by: describe(by), covered_points: hidden + '/' + xs.length});
  }
  return {findings, truncated, ...counts};
}"""

# The whole page: the body (or the root element of a document without one).
_LAYOUT_JS = "(arg) => (" + _LAYOUT_FN + ")(document.body || document.documentElement, arg)"


def _in_latest_px(page, rect, now) -> list[int] | None:
    """``rect`` (viewport CSS px) in pixels of the page's latest viewport screenshot."""
    geometry = marks.last(page)
    if geometry is None or geometry.origin != (0.0, 0.0) or now is None:
        return None
    x, y, w, h = (float(v) for v in rect)
    if geometry.full_page:
        x, y = x + now[3], y + now[4]
    elif abs(now[3] - geometry.scroll[0]) > 1 or abs(now[4] - geometry.scroll[1]) > 1:
        return None
    s = geometry.scale
    return [round(x * s), round(y * s), round(w * s), round(h * s)]


async def _scan(page, selector: str, arg: dict) -> dict:
    """Run the scan on the page, or under the element a selector names (CSS,
    ``aria-ref=...``, ``text=...``, ``role=...``, as the other tools take them)."""
    if not selector:
        return await page.evaluate(_LAYOUT_JS, arg)
    target = page.locator(selector).first
    if await target.count() == 0:
        return {"missing": True}
    return await target.evaluate(_LAYOUT_FN, arg)


_KEEP = ("scanned", "contrast_checked", "unknown_background", "under_fixed", "not_hit_testable")


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def inspect_layout(selector: str = "", max_findings: int = 30) -> dict:
        """Name layout faults on screen from the page's computed styles.

        ``low_contrast`` (text under WCAG AA against what is painted behind it, with the
        ratio), ``clipped`` (text cut off by its box or an ancestor, or spilling out of
        the box that draws it) and ``covered`` (text or a control with something painted
        on top). ``selector`` (CSS or ``aria-ref=...``) limits it to one region. Each
        finding has ``box_css`` and, when there is a screenshot of the page, ``box_px``
        in its pixels for ``screenshot(region=...)``. Read-only: nothing is written to
        the page and no permission is needed. Text over an image, a video, a canvas or a
        gradient is counted (``unknown_background``), not judged; hidden text,
        scrolled-away text and text under a fixed header are not faults.
        """
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        cap = max(1, min(int(max_findings), MAX_FINDINGS))
        arg = {"cap": cap, "maxElements": MAX_ELEMENTS}
        try:
            data = await asyncio.wait_for(_scan(page, selector, arg), _READ_BUDGET_S)
        except Exception as exc:  # noqa: BLE001 — a bad selector, a navigation, a slow page
            text = f"{type(exc).__name__}: {exc}"
            bad = bool(selector) and any(w in text.lower() for w in ("selector", "syntax"))
            reply = {
                "status": "error" if bad else "unavailable",
                "url": loggable_url(page.url),
                "error": clip(text, 300),
            }
            if bad:
                reply["selector"] = selector
            return reply
        if not isinstance(data, dict):
            data = {}
        if data.get("missing"):
            return {"status": "not_found", "selector": selector, "url": loggable_url(page.url)}
        findings = [f for f in data.get("findings") or [] if isinstance(f, dict)]
        now = await marks.page_geometry(page) if findings else None
        for finding in findings:
            px = _in_latest_px(page, finding.get("box_css") or [0, 0, 0, 0], now)
            if px is not None:
                finding["box_px"] = px
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.get("kind", "?")] = counts.get(finding.get("kind", "?"), 0) + 1
        # The audit keeps what was asked and how many of each, never the page's text.
        ctx.audit.record("inspect_layout", {"selector": selector, "counts": counts})
        return {
            "status": "ok",
            "url": loggable_url(page.url),
            "findings": findings,
            "counts": counts,
            **{k: int(data.get(k, 0) or 0) for k in _KEEP},
            "truncated": bool(data.get("truncated")),
        }
