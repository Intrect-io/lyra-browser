"""``inspect_layout``: what the computed layout says is wrong on screen.

Checking a page by sight misses what is a pixel or a shade off. This reads the computed
styles and geometry of what is on screen and names three kinds of fault:

- ``low_contrast``: text whose colour against its effective background is under the WCAG
  AA ratio (4.5:1, or 3:1 for large text: 24 px, or 18.66 px bold);
- ``clipped``: text cut off by its own box (``overflow`` hidden or clipped) or spilling out
  of the box that holds it;
- ``covered``: text or a control that another element lies on top of, found by asking the
  page what is at points across it (``elementFromPoint``).

It only reads: no style, attribute or node is written, so page scripts see nothing (the
gate counts mutations). It is a complement to looking, not a replacement: a background
image, a canvas or a gradient has no colour to compute, and those are reported as
``unknown_background`` rather than guessed.
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

# One pass over the elements under ``selector`` (or the body) that are on screen.
_LAYOUT_JS = r"""(arg) => {
  const {selector, cap, maxElements} = arg;
  const root = selector ? document.querySelector(selector) : document.body;
  if (!root) return {missing: true};
  const vw = innerWidth, vh = innerHeight;
  const findings = [];
  let scanned = 0, unknownBg = 0, truncated = false;

  const parse = (c) => {
    const m = /rgba?\(([^)]+)\)/.exec(c || '');
    if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
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
  // The colour behind ``el``: its own and its ancestors' backgrounds, composited down to
  // the canvas. A background image on the way makes it unknown.
  const background = (el) => {
    const layers = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const cs = getComputedStyle(n);
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null;
      const c = parse(cs.backgroundColor);
      if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; }
    }
    let colour = [255, 255, 255, 1];
    for (let i = layers.length - 1; i >= 0; i--) colour = over(layers[i], colour);
    return colour;
  };
  const opacity = (el) => {
    let o = 1;
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      o *= Number(getComputedStyle(n).opacity);
    }
    return o;
  };
  const ownText = (el) => {
    let t = '';
    for (const n of el.childNodes) if (n.nodeType === 3) t += n.nodeValue;
    return t.replace(/\s+/g, ' ').trim();
  };
  const textRect = (el) => {
    let box = null;
    for (const n of el.childNodes) {
      if (n.nodeType !== 3 || !n.nodeValue.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        if (r.width < 1 || r.height < 1) continue;
        box = box ? {l: Math.min(box.l, r.left), t: Math.min(box.t, r.top),
                     r: Math.max(box.r, r.right), b: Math.max(box.b, r.bottom)}
                  : {l: r.left, t: r.top, r: r.right, b: r.bottom};
      }
    }
    return box;
  };
  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + el.id;
    const cls = [...el.classList].slice(0, 2).join('.');
    if (cls) s += '.' + cls;
    const p = el.parentElement;
    if (p) {
      const same = [...p.children].filter((c) => c.tagName === el.tagName);
      if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
      return describe(p).split(' > ').slice(-2).join(' > ') + ' > ' + s;
    }
    return s;
  };
  const onScreen = (r) => r.width >= 1 && r.height >= 1 && r.right > 0 && r.bottom > 0 &&
    r.left < vw && r.top < vh;
  const add = (finding) => {
    if (findings.length >= cap) { truncated = true; return; }
    findings.push(finding);
  };
  const box = (b) =>
    [Math.round(b.l), Math.round(b.t), Math.round(b.r - b.l), Math.round(b.b - b.t)];
  // The box that holds ``el``'s text: the nearest ancestor-or-self that draws a border or
  // a background, or clips, i.e. the edge a reader sees the text spill over.
  const holder = (el) => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      const cs = getComputedStyle(n);
      const drawn = parseFloat(cs.borderTopWidth) > 0 || parseFloat(cs.borderLeftWidth) > 0 ||
        (parse(cs.backgroundColor) || [0, 0, 0, 0])[3] > 0 || cs.overflow !== 'visible';
      if (drawn && cs.display !== 'inline') return n;
    }
    return null;
  };
  const CONTROLS = 'button, a[href], input, select, textarea, [role=button], [role=link]';

  const all = root.querySelectorAll('*');
  for (const el of [root, ...all]) {
    if (scanned >= maxElements) { truncated = true; break; }
    const rect = el.getBoundingClientRect();
    if (!onScreen(rect)) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    const text = ownText(el);
    const isControl = el.matches(CONTROLS);
    if (!text && !isControl) continue;
    scanned++;
    const tr = text ? textRect(el) : null;
    const area = tr || {l: rect.left, t: rect.top, r: rect.right, b: rect.bottom};
    const label = (text || el.getAttribute('aria-label') || el.value || '').slice(0, 60);

    if (text && tr) {
      const fg = parse(cs.color);
      const bg = background(el);
      if (!bg) unknownBg++;
      else if (fg) {
        const o = opacity(el);
        const seen = over([fg[0], fg[1], fg[2], fg[3] * o], bg);
        const l1 = lum(seen), l2 = lum(bg);
        const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        const size = parseFloat(cs.fontSize), bold = Number(cs.fontWeight) >= 700;
        const large = size >= 24 || (size >= 18.66 && bold);
        const need = large ? 3 : 4.5;
        if (ratio < need) add({kind: 'low_contrast', element: describe(el), text: label,
          box_css: box(tr), ratio: Math.round(ratio * 100) / 100, needs: need,
          color: cs.color, background: 'rgb(' + bg.slice(0, 3).map(Math.round).join(', ') + ')',
          font_px: size});
      }
      const cut = ['hidden', 'clip'];
      const clips = cut.includes(cs.overflowX) || cut.includes(cs.overflowY);
      if (clips && (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)) {
        add({kind: 'clipped', element: describe(el), text: label, box_css: box(area),
          detail: 'text is cut off by its own box (overflow ' + cs.overflow + ')',
          content_px: [el.scrollWidth, el.scrollHeight],
          client_px: [el.clientWidth, el.clientHeight]});
      } else {
        const h = holder(el);
        if (h) {
          const hr = h.getBoundingClientRect();
          const out = Math.max(hr.left - tr.l, tr.r - hr.right, hr.top - tr.t, tr.b - hr.bottom);
          if (out > 2) add({kind: 'clipped', element: describe(el), text: label, box_css: box(tr),
            detail: 'text spills ' + Math.round(out) + ' px out of ' + describe(h),
            holder_css: box({l: hr.left, t: hr.top, r: hr.right, b: hr.bottom})});
        }
      }
    }

    // Points across the text (or the control): what the page says is on top there.
    const xs = [0.1, 0.3, 0.5, 0.7, 0.9].map((f) => area.l + (area.r - area.l) * f);
    const y = area.t + (area.b - area.t) / 2;
    let hidden = 0, by = null;
    for (const x of xs) {
      if (x < 0 || y < 0 || x >= vw || y >= vh) continue;
      const hit = document.elementFromPoint(x, y);
      if (!hit || hit === el || el.contains(hit) || hit.contains(el)) continue;
      hidden++;
      by = by || hit;
    }
    if (hidden) add({kind: 'covered', element: describe(el), text: label, box_css: box(area),
      covered_by: describe(by), covered_points: hidden + '/5'});
  }
  return {findings, scanned, unknown_background: unknownBg, truncated};
}"""


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


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def inspect_layout(selector: str = "", max_findings: int = 30) -> dict:
        """Name layout faults on screen from the page's computed styles.

        ``low_contrast`` (text under WCAG AA against its background, with the ratio),
        ``clipped`` (text cut off by its box or spilling out of it) and ``covered``
        (text or a control with another element on top). ``selector`` limits it to one
        region. Each finding has ``box_css`` and, when there is a screenshot of the page,
        ``box_px`` in its pixels for ``screenshot(region=...)``. Read-only: nothing is
        written to the page and no permission is needed. Backgrounds that are images,
        gradients or canvas cannot be computed and are counted, not guessed.
        """
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        cap = max(1, min(int(max_findings), MAX_FINDINGS))
        arg = {"selector": selector, "cap": cap, "maxElements": MAX_ELEMENTS}
        try:
            data = await asyncio.wait_for(page.evaluate(_LAYOUT_JS, arg), _READ_BUDGET_S)
        except Exception as exc:  # noqa: BLE001 — a navigation tore the world down, a slow page
            return {
                "status": "unavailable",
                "url": loggable_url(page.url),
                "error": clip(f"{type(exc).__name__}: {exc}", 300),
            }
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
        ctx.audit.record("inspect_layout", {"selector": selector, "counts": counts})
        return {
            "status": "ok",
            "url": loggable_url(page.url),
            "findings": findings,
            "counts": counts,
            "scanned": data.get("scanned", 0),
            "unknown_background": data.get("unknown_background", 0),
            "truncated": bool(data.get("truncated")),
        }
