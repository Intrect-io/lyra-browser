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
# The page script stops itself before the tool gives up waiting, and says so.
_SCAN_BUDGET_MS = 3500

# The scan, given the element to scan under (``root``) and its options.
_LAYOUT_FN = r"""(root, arg) => {
  const {cap, maxElements, budgetMs} = arg;
  const started = performance.now();
  const vw = innerWidth, vh = innerHeight;
  const findings = [];
  const counts = {scanned: 0, unknown_background: 0, under_fixed: 0, not_hit_testable: 0,
    contrast_checked: 0};
  let truncated = null;

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
  const mix = (a, b, t) => [0, 1, 2].map((i) => a[i] * t + b[i] * (1 - t)).concat([1]);
  const styles = new Map();
  const style = (n, pseudo) => {
    if (pseudo) return getComputedStyle(n, pseudo);
    let cs = styles.get(n);
    if (!cs) { cs = getComputedStyle(n); styles.set(n, cs); }
    return cs;
  };
  const parentOf = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
  const within = (outer, inner) => {  // inner is outer or inside it, across shadow roots
    for (let n = inner; n; n = parentOf(n)) if (n === outer) return true;
    return false;
  };
  const clipText = (t, n) => (t.length > n ? t.slice(0, n) + '…' : t);
  // The outermost ancestor-or-self that fades: its opacity applies to the text and to
  // the backgrounds inside it alike, once.
  const groupOf = (el) => {
    let g = null, o = 1;
    for (let n = el; n && n.nodeType === 1; n = parentOf(n)) {
      const v = Number(style(n).opacity);
      if (v < 1) { g = n; o *= v; }
    }
    return {g, o};
  };
  const opaqueBg = (n) => {
    const c = rgba(style(n).backgroundColor);
    return c && c[3] >= 1 ? c : null;
  };
  // The page canvas: html's background, else body's (it propagates), else white or the
  // dark canvas Chrome paints for a dark color-scheme.
  const canvas = (() => {
    const top = document.documentElement;
    const own = opaqueBg(top) || (document.body && opaqueBg(document.body));
    if (own) return own;
    const scheme = style(top).colorScheme || '';
    const dark = /dark/.test(scheme) &&
      (!/light/.test(scheme) || matchMedia('(prefers-color-scheme: dark)').matches);
    return dark ? [18, 18, 18, 1] : [255, 255, 255, 1];
  })();
  const MEDIA = new Set(['IMG', 'VIDEO', 'CANVAS', 'IFRAME', 'OBJECT', 'EMBED', 'PICTURE']);
  const isMedia = (n) => MEDIA.has(n.tagName) || n instanceof SVGSVGElement;
  const imaged = (cs) => cs.backgroundImage && cs.backgroundImage !== 'none';
  // A ::before/::after that paints a fill (a badge, a highlight): its box is not
  // knowable from here, so what it paints behind the text is unknown.
  const pseudoFill = (n) => {
    for (const pseudo of ['::before', '::after']) {
      const ps = getComputedStyle(n, pseudo);
      if (!ps.content || ps.content === 'none' || ps.content === 'normal') continue;
      const pb = rgba(ps.backgroundColor);
      if ((pb && pb[3] > 0.05) || imaged(ps)) return true;
    }
    return false;
  };
  const contains = (r, x, y) => x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;

  const all = [];
  const late = () => performance.now() - started > budgetMs;
  const gather = (node) => {
    for (const el of node.querySelectorAll('*')) {
      if (late()) { truncated = 'time'; return; }
      all.push(el);
      if (el.shadowRoot) gather(el.shadowRoot);
    }
  };
  all.push(root);
  if (root.shadowRoot) gather(root.shadowRoot);
  gather(root);
  // Layers that hit-testing cannot see (pointer-events: none) but that paint: a
  // decorative image or scrim behind a heading. elementsFromPoint leaves them out.
  const inert = [];
  for (const n of all) {
    if (late()) { truncated = 'time'; break; }
    const r = n.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || r.right < 0 || r.bottom < 0 || r.left > vw || r.top > vh) {
      continue;
    }
    const cs = style(n);
    if (cs.pointerEvents !== 'none' || cs.visibility === 'hidden') continue;
    const bg = rgba(cs.backgroundColor);
    if (isMedia(n) || imaged(cs) || (bg && bg[3] > 0.05)) inert.push({n, r});
  }

  // What is painted behind ``el`` at (x, y), as the page orders it: ``inside`` the
  // fading group (if any) and ``below`` it. null when an image, video, canvas or
  // gradient is in the way: then it cannot be computed.
  const background = (el, x, y, g) => {
    // A layer hit-testing skips cannot be placed in the paint order: unknown.
    for (const {n, r} of inert) if (!within(el, n) && contains(r, x, y)) return null;
    const scope = el.getRootNode().elementsFromPoint ? el.getRootNode() : document;
    const stack = scope.elementsFromPoint(x, y);
    let i = stack.indexOf(el);
    const inside = [], below = [];
    const take = (n) => {
      const cs = style(n);
      if (isMedia(n) || imaged(cs) || pseudoFill(n)) return false;
      const c = rgba(cs.backgroundColor);
      if (c && c[3] > 0) {
        if (g && within(g, n)) inside.push(c);
        else below.push([c[0], c[1], c[2], c[3] * groupOf(n).o]);
      }
      return true;
    };
    const opaque = () => {
      const last = below.length ? below[below.length - 1] : inside[inside.length - 1];
      return last && last[3] >= 1 && (!g || below.length);
    };
    const chain = i < 0 ? [] : stack.slice(i + 1).filter((n) => !within(el, n));
    if (i < 0) for (let n = el; n && n.nodeType === 1; n = parentOf(n)) chain.push(n);
    if (!take(el)) return null;
    for (const n of chain) {
      if (n === el) continue;
      if (opaque()) break;
      if (!take(n)) return null;
    }
    let low = canvas;
    for (let k = below.length - 1; k >= 0; k--) low = over(below[k], low);
    let high = low;
    for (let k = inside.length - 1; k >= 0; k--) high = over(inside[k], high);
    return {inside: high, below: low};
  };
  const ownText = (el) => {
    if (el.tagName === 'TEXTAREA') return '';  // its text node is its value
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
  const textLines = (el) => {
    const out = [];
    for (const n of el.childNodes) {
      if (n.nodeType !== 3 || !n.nodeValue.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        if (r.width >= 1 && r.height >= 1) out.push(rectOf(r));
      }
    }
    return out.length ? out : [{l: 0, t: 0, r: 0, b: 0}];
  };
  const describe = (el, depth = 0) => {
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + clipText(el.id, 40);
    const cls = [...el.classList].slice(0, 2).map((c) => clipText(c, 30)).join('.');
    if (cls) s += '.' + cls;
    const p = parentOf(el);
    if (p && p.nodeType === 1 && depth < 2) {
      const siblings = el.parentNode && el.parentNode.children ? [...el.parentNode.children] : [];
      const same = siblings.filter((c) => c.tagName === el.tagName);
      if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      return describe(p, depth + 1) + ' > ' + s;
    }
    return s;
  };
  const add = (finding) => {
    if (findings.length >= cap) { truncated = truncated || 'findings'; return; }
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
  // A scroller: an axis that scrolls and has something to scroll. (overflow-x: hidden
  // alone computes overflow-y to auto; with nothing to scroll that is not a scroller.)
  const scrolls = (n, cs) =>
    (['auto', 'scroll'].includes(cs.overflowY) && n.scrollHeight > n.clientHeight + 1) ||
    (['auto', 'scroll'].includes(cs.overflowX) && n.scrollWidth > n.clientWidth + 1);
  // The part of ``b`` that ancestors let show, and the nearest box that cut some of it
  // off (a scroller hides on purpose and is not one). An ancestor clips an absolutely
  // positioned element only from its containing block up; a fixed one only the viewport.
  const showing = (el, b) => {
    let seen = meet(b, VIEW);
    let cutBy = null;
    let pos = style(el).position;
    for (let n = parentOf(el); n && n.nodeType === 1; n = parentOf(n)) {
      if (pos === 'fixed') break;
      // html's and body's overflow apply to the viewport, already accounted for.
      if (n === document.documentElement || n === document.body) continue;
      const cs = style(n);
      const block = cs.position !== 'static' || cs.transform !== 'none';
      const skip = pos === 'absolute' && !block;
      if (pos !== 'absolute' || block) pos = cs.position;
      if (skip) continue;
      const clips = cs.overflowX !== 'visible' || cs.overflowY !== 'visible' ||
        (cs.clipPath && cs.clipPath !== 'none');
      if (!clips) continue;
      const before = area(seen);
      seen = meet(seen, rectOf(n.getBoundingClientRect()));
      const cuts = CUT.includes(cs.overflowX) || CUT.includes(cs.overflowY);
      if (area(seen) < before - 1 && cuts && !scrolls(n, cs) && !cutBy) cutBy = n;
    }
    return {seen, cutBy};
  };
  // Visually hidden on purpose: the sr-only pattern (a clip of no area, a tiny box), a
  // clip-path that hides it all, an element faded out.
  const zeroClip = (c) => {
    const m = /rect\(([^)]*)\)/.exec(c || '');
    if (!m) return false;
    const v = m[1].split(/[ ,]+/).filter(Boolean).map(parseFloat);
    return v.length === 4 && (v[2] - v[0] <= 1 || v[1] - v[3] <= 1);
  };
  const hiddenOnPurpose = (el, cs, rect) => {
    if (rect.width <= 2 && rect.height <= 2) return true;
    if (zeroClip(cs.clip)) return true;
    if (cs.clipPath && /inset\(\s*50%/.test(cs.clipPath)) return true;
    return groupOf(el).o < 0.1;
  };
  // What scrolls under a sticky element (a table header, a section title) or under a
  // bar fixed along an edge of the viewport (a header, a footer, a sidebar) is not a
  // layout fault. Any other fixed thing on top (a chat bubble, a banner over a button)
  // still is.
  const edgeBar = (n) => {
    for (let m = n; m && m.nodeType === 1; m = parentOf(m)) {
      const p = style(m).position;
      if (p === 'sticky') return true;
      if (p !== 'fixed') continue;
      const r = m.getBoundingClientRect();
      const across = r.width >= vw * 0.5 && (r.top <= 1 || r.bottom >= vh - 1);
      const down = r.height >= vh * 0.5 && (r.left <= 1 || r.right >= vw - 1);
      if (across || down) return true;
    }
    return false;
  };
  const paints = (n, x, y) => {
    const cs = style(n);
    if (groupOf(n).o < 0.1 || cs.visibility === 'hidden') return false;  // invisible overlay
    if (isMedia(n) || n.matches('input, select, textarea, button')) return true;
    if (imaged(cs)) return true;
    const bg = rgba(cs.backgroundColor);
    if (bg && bg[3] > 0.05) return true;
    for (const pseudo of ['::before', '::after']) {
      const ps = style(n, pseudo);
      if (ps.content && ps.content !== 'none' && ps.content !== 'normal') {
        const pb = rgba(ps.backgroundColor);
        if ((pb && pb[3] > 0.05) || imaged(ps)) return true;
      }
    }
    const t = textRect(n);
    return !!t && x >= t.l && x <= t.r && y >= t.t && y <= t.b;
  };
  // Whether what was hit at (x, y) paints there: the hit or any ancestor of it up to
  // the one it shares with ``el`` (an opaque modal whose transparent child was hit),
  // looking into an open shadow root the hit-test stopped at.
  const covers = (el, hit, x, y) => {
    let top = hit;
    for (let k = 0; k < 8 && top.shadowRoot; k++) {
      const inner = top.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === top) break;
      top = inner;
    }
    for (let n = top; n && n.nodeType === 1; n = parentOf(n)) {
      if (within(n, el)) return false;
      if (paints(n, x, y)) return true;
    }
    return false;
  };
  const CONTROLS = 'button, a[href], input, select, textarea, [role=button], [role=link]';
  const LABELLED = new Set(['submit', 'button', 'reset']);
  // Inputs whose value or placeholder is drawn as text in their colour.
  const TEXTUAL = new Set(['', 'text', 'search', 'email', 'url', 'tel', 'number', 'password',
    'date', 'time', 'datetime-local', 'month', 'week']);
  const labelOf = (el, text) => {
    if (text) return clipText(text, 60);
    // A typed value is never reported (it may be a password).
    const t = (el.getAttribute('type') || '').toLowerCase();
    if (el.tagName === 'INPUT' && LABELLED.has(t)) return clipText(el.value || t, 60);
    return clipText(el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
      el.getAttribute('title') || t || el.tagName.toLowerCase(), 60);
  };

  const contrast = (el, cs, tr, label, colourOf) => {
    if (el.closest(':disabled, [aria-disabled=true]')) return;
    const fg = rgba(colourOf);
    if (!fg) return;
    const {g, o} = groupOf(el);
    const bg = background(el, (tr.l + tr.r) / 2, (tr.t + tr.b) / 2, g);
    if (!bg) { counts.unknown_background++; return; }
    counts.contrast_checked++;
    // The text over its background inside the group, then the group faded once over
    // what is below it.
    const text = mix(over(fg, bg.inside), bg.below, o);
    const back = mix(bg.inside, bg.below, o);
    const l1 = lum(text), l2 = lum(back);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    const size = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700;
    const need = size >= 24 || (size >= 18.66 && bold) ? 3 : 4.5;
    if (ratio < need) add({kind: 'low_contrast', element: describe(el), text: label,
      box_css: box(tr), ratio: Math.round(ratio * 100) / 100, needs: need,
      color: clipText(colourOf, 60),
      background: 'rgb(' + back.slice(0, 3).map(Math.round).join(', ') + ')', font_px: size});
  };
  const spills = (el, tr, em) => {
    // Out of the box that draws it (a border or a fill); a scroller on the way stops it.
    for (let n = el; n && n.nodeType === 1 && n !== document.body; n = parentOf(n)) {
      const ns = style(n);
      if (scrolls(n, ns)) return null;
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

  for (const el of all) {
    if (performance.now() - started > budgetMs) { truncated = 'time'; break; }
    if (counts.scanned >= maxElements) { truncated = 'elements'; break; }
    if (el.namespaceURI !== 'http://www.w3.org/1999/xhtml') continue;  // SVG: fill, not color
    const rect = el.getBoundingClientRect();
    const cs = style(el);
    if (cs.display !== 'contents' &&
        (rect.right < 0 || rect.bottom < 0 || rect.left > vw || rect.top > vh)) continue;
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const text = ownText(el);
    const isControl = el.matches(CONTROLS);
    if (!text && !isControl) continue;
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
      const own = rectOf(rect);
      const pastX = CUT.includes(cs.overflowX) ? Math.max(own.l - tr.l, tr.r - own.r) : 0;
      const pastY = CUT.includes(cs.overflowY)
        ? Math.max(own.t - tr.t, tr.b - own.b) - em / 3 : 0;
      if (Math.max(pastX, pastY) > 2) {
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
      const drawsText = el.tagName !== 'INPUT' || TEXTUAL.has(t) || LABELLED.has(t);
      if (!drawsText) {
        // A checkbox, radio, range or colour input draws no text.
      } else if ((el.tagName === 'INPUT' && LABELLED.has(t)) || el.value) {
        contrast(el, cs, inner, label, cs.color);
      } else if (el.getAttribute('placeholder')) {
        contrast(el, cs, inner, label, style(el, '::placeholder').color);
      }
    }

    // What is on top: points across the text, or the centre of a control.
    if (cs.pointerEvents === 'none') { counts.not_hit_testable++; continue; }
    // Points on the text's own line boxes (not the union, which can span a float or the
    // space beside a short last line), inside what its own box and its ancestors show.
    let shown = seen;
    if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') shown = meet(shown, rectOf(rect));
    const probe = meet(extent, shown);
    const lines = tr ? textLines(el) : [probe];
    const points = [];
    const FRAC = [0.1, 0.3, 0.5, 0.7, 0.9];
    for (let k = 0; k < (tr ? 5 : 1); k++) {
      const line = lines[Math.floor((k * lines.length) / (tr ? 5 : 1))];
      const r = meet(line, shown);
      if (area(r) < 1) continue;
      points.push([tr ? r.l + (r.r - r.l) * FRAC[k] : (r.l + r.r) / 2, (r.t + r.b) / 2]);
    }
    if (!points.length) continue;
    const scope = el.getRootNode().elementFromPoint ? el.getRootNode() : document;
    let hidden = 0, by = null, fixed = 0;
    for (const [x, y] of points) {
      const hit = scope.elementFromPoint(x, y);
      if (!hit || within(el, hit) || within(hit, el)) continue;
      if (!covers(el, hit, x, y)) continue;  // a transparent click-catcher, not a cover
      if (edgeBar(hit) && !edgeBar(el)) { fixed++; continue; }
      hidden++;
      by = by || hit;
    }
    if (fixed) counts.under_fixed++;
    if (hidden) add({kind: 'covered', element: describe(el), text: label, box_css: box(probe),
      covered_by: describe(by), covered_points: hidden + '/' + points.length});
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


_KINDS = ("low_contrast", "clipped", "covered")
_TEXT_KEYS = ("element", "text", "detail", "covered_by", "covered_points", "color", "background")
_NUMBER_KEYS = ("ratio", "needs", "font_px")


def _box(value) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return None
    return [float(v) for v in value]


def _clean(raw) -> dict | None:
    """One finding as the page script made it, checked: the scan runs where the page's own
    globals apply, so its values are data to validate, not trust."""
    if not isinstance(raw, dict) or raw.get("kind") not in _KINDS:
        return None
    box = _box(raw.get("box_css"))
    if box is None:
        return None
    out: dict = {"kind": raw["kind"], "box_css": [round(v) for v in box]}
    for key in _TEXT_KEYS:
        if isinstance(raw.get(key), str):
            out[key] = clip(raw[key], 200)
    for key in _NUMBER_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    holder = _box(raw.get("holder_css"))
    if holder is not None:
        out["holder_css"] = [round(v) for v in holder]
    return out


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


def _count(value) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


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
        arg = {"cap": cap, "maxElements": MAX_ELEMENTS, "budgetMs": _SCAN_BUDGET_MS}
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
        findings = [f for f in (_clean(f) for f in data.get("findings") or []) if f]
        now = await marks.page_geometry(page) if findings else None
        for finding in findings:
            px = _in_latest_px(page, finding["box_css"], now)
            if px is not None:
                finding["box_px"] = px
        truncated = data.get("truncated")
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
            **{k: _count(data.get(k)) for k in _KEEP},
            "truncated": truncated if truncated in ("findings", "elements", "time") else False,
        }
