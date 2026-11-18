"""Suite v2 of the visual eval: harder tasks, still only solvable by looking.

v1 (``eval_visual_site.py``) asks for things a full screenshot shows plainly. These ask for
what one plain capture barely shows or cannot show at all: 4-character codes drawn 5 px tall
among one-character near misses, two of 60 bars 1 px apart, a target below the fold, a label
that exists only while the pointer is over a button, a tile moved by 1 px or shaded slightly
after a "deploy", text cut off by a box the colour of the card, text of lower contrast than
its neighbours, and one of 36 boxes 1-2 px out of line.

A first cut (7 px codes, 2 px bars, 3-5 px shifts, near-white text) was solved 16/16 by
Sonnet through plain screenshots (``docs/measurements/visual-eval/v2/pilot-first-cut.json``),
so it measured nothing; these are the harder settings.

Same rules as v1: the page reports what the agent did through ``fetch``, the server keeps
the score, page data is obfuscated, and every answer is a click (nothing is typed and sent),
so a task measures sight, not how a consent prompt is answered.
"""

from __future__ import annotations

import random
import string
from typing import Any

GOALS_V2: dict[str, str] = {
    "tiny_find": (
        "The page names a code at the top. Click the small tile that is labelled with that code."
    ),
    "tall_bar": "Click the tallest bar in the chart. Two bars are almost the same height.",
    "far_target": "Click the purple hexagon on this page.",
    "hover_tip": (
        "Each round button shows a label while the pointer is over it. Click the button "
        "labelled 'Archive'."
    ),
    "regression": (
        "Press 'Deploy'. Afterwards one tile looks slightly different than before: press "
        "'This one' on that tile."
    ),
    "overlap": (
        "On one card, part of the text is hidden behind something. Press 'Report' on that card."
    ),
    "faint_text": (
        "On one card the text has much less contrast against its background than on the "
        "others. Press 'Report' on that card."
    ),
    "misaligned": "One box in the grid sits a few pixels out of line with the others. Click it.",
}
TASKS_V2: tuple[str, ...] = tuple(GOALS_V2)

TIPS = ("Archive", "Share", "Print", "Rename", "Move", "Copy")
_CODE = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"

# Geometry the pages draw with and the scripted solver aims at (CSS px).
TINY = {"cols": 8, "rows": 6, "x0": 20, "y0": 80, "dx": 105, "dy": 70, "w": 95, "h": 56}
BARS = {"n": 60, "x0": 30, "dx": 14, "w": 6, "base": 330}
FAR_TOP = 2600


def reset_v2(rng: random.Random, task: str) -> dict[str, Any]:
    """One round of a v2 task: what the page draws, including the answer."""
    if task == "tiny_find":
        n = TINY["cols"] * TINY["rows"]
        target = "".join(rng.choice(_CODE) for _ in range(4))
        codes = {target}
        # Near misses first: one character off, so reading three of four is not enough.
        while len(codes) < 9:
            k = rng.randrange(4)
            codes.add(target[:k] + rng.choice(_CODE) + target[k + 1 :])
        while len(codes) < n:
            codes.add("".join(rng.choice(_CODE) for _ in range(4)))
        order = sorted(codes - {target})
        rng.shuffle(order)
        at = rng.randrange(n)
        order.insert(at, target)
        return {"codes": order, "target": at}
    if task == "tall_bar":
        n = BARS["n"]
        heights = [rng.randint(60, 225) for _ in range(n)]
        first, second = rng.sample(range(n), 2)
        heights[first] = 240
        heights[second] = 239
        return {"heights": heights, "target": first}
    if task == "far_target":
        # Decoys share a shape or a colour with the target; the target is far below the fold.
        top = [
            {"id": 1, "shape": "hexagon", "colour": "orange", "x": 120, "y": 150},
            {"id": 2, "shape": "circle", "colour": "purple", "x": 300, "y": 150},
            {"id": 3, "shape": "square", "colour": "green", "x": 480, "y": 150},
        ]
        rng.shuffle(top)
        for i, s in enumerate(top):
            s["x"] = 120 + i * 180
        far = [
            {"id": 0, "shape": "hexagon", "colour": "purple", "x": 0, "y": 150},
            {"id": 4, "shape": "circle", "colour": "orange", "x": 0, "y": 150},
        ]
        rng.shuffle(far)
        for i, s in enumerate(far):
            s["x"] = 150 + i * 260
        return {"top": top, "far": far, "target": 0}
    if task == "hover_tip":
        tips = list(TIPS)
        rng.shuffle(tips)
        return {"tips": tips, "target": tips.index("Archive")}
    if task == "regression":
        kind = rng.choice(["shift", "shade"])
        return {"changed": rng.randrange(12), "kind": kind}
    if task == "overlap":
        return {"covered": rng.randrange(4)}
    if task == "faint_text":
        return {"faint": rng.randrange(6)}
    if task == "misaligned":
        return {"off": rng.randrange(36), "dy": rng.choice([1, -1, 2, -2])}
    raise KeyError(task)


def record_v2(task: str, data: dict, events: list[dict], kind: str, value: Any) -> bool | None:
    """The oracle: True/False once the agent's action decides the round, None otherwise."""
    if task in ("tiny_find", "tall_bar", "far_target", "hover_tip") and kind == "click":
        return value == data["target"]
    if task == "regression" and kind == "pick":
        deployed = any(e["kind"] == "deployed" for e in events)
        return deployed and value == data["changed"]
    if task == "overlap" and kind == "report":
        return value == data["covered"]
    if task == "faint_text" and kind == "report":
        return value == data["faint"]
    if task == "misaligned" and kind == "click":
        return value == data["off"]
    return None


def page_v2(task: str, data: dict) -> tuple[str, str]:
    """(title, body) of a v2 task page; the body reads ``DATA`` and calls ``report``."""
    return globals()[f"_page_{task}"](data)


def _page_tiny_find(d: dict) -> tuple[str, str]:
    t = TINY
    return (
        "Tiles",
        f"""<h1>Tiles</h1>
<canvas id="c" width="880" height="500" style="background:#fff;border:1px solid #ccc"></canvas>
<script>
const T = {{cols:{t["cols"]}, x0:{t["x0"]}, y0:{t["y0"]}, dx:{t["dx"]}, dy:{t["dy"]},
  w:{t["w"]}, h:{t["h"]}}};
const g = document.getElementById('c').getContext('2d');
g.fillStyle = '#111'; g.font = 'bold 24px sans-serif';
g.fillText('Find: ' + DATA.codes[DATA.target], 30, 50);
DATA.codes.forEach((code, i) => {{
  const x = T.x0 + (i % T.cols) * T.dx, y = T.y0 + Math.floor(i / T.cols) * T.dy;
  g.fillStyle = '#eef1f5'; g.fillRect(x, y, T.w, T.h);
  g.fillStyle = '#333'; g.font = '5px sans-serif';
  g.fillText(code, x + T.w / 2 - 7, y + T.h / 2 + 3);
}});
document.getElementById('c').addEventListener('click', (e) => {{
  const b = e.target.getBoundingClientRect(), x = e.clientX - b.left, y = e.clientY - b.top;
  let hit = null;
  DATA.codes.forEach((_, i) => {{
    const tx = T.x0 + (i % T.cols) * T.dx, ty = T.y0 + Math.floor(i / T.cols) * T.dy;
    if (x >= tx && x <= tx + T.w && y >= ty && y <= ty + T.h) hit = i; }});
  report('click', hit); }});
</script>""",
    )


def _page_tall_bar(d: dict) -> tuple[str, str]:
    b = BARS
    return (
        "Load",
        f"""<h1>Load per minute</h1>
<canvas id="c" width="900" height="360" style="background:#fff;border:1px solid #ccc"></canvas>
<script>
const B = {{x0:{b["x0"]}, dx:{b["dx"]}, w:{b["w"]}, base:{b["base"]}}};
const g = document.getElementById('c').getContext('2d');
g.strokeStyle = '#999'; g.beginPath(); g.moveTo(30, B.base); g.lineTo(880, B.base); g.stroke();
DATA.heights.forEach((h, i) => {{ g.fillStyle = '#4f7cac';
  g.fillRect(B.x0 + i * B.dx, B.base - h, B.w, h); }});
document.getElementById('c').addEventListener('click', (e) => {{
  const r = e.target.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  let hit = null;
  DATA.heights.forEach((h, i) => {{ const c = B.x0 + i * B.dx + B.w / 2;
    if (Math.abs(x - c) <= B.w / 2 + 3 && y <= B.base && y >= B.base - h - 6) hit = i; }});
  report('click', hit); }});
</script>""",
    )


def _page_far_target(d: dict) -> tuple[str, str]:
    return (
        "Shapes",
        f"""<h1>Shapes</h1>
<canvas id="c1" width="640" height="300" style="background:#fff;border:1px solid #ccc"></canvas>
<p>More below.</p>
<div style="height:{FAR_TOP - 420}px"></div>
<canvas id="c2" width="640" height="300" style="background:#fff;border:1px solid #ccc"></canvas>
<div style="height:600px"></div>
<script>
const COL = {{purple:'#7e22ce', orange:'#ea580c', green:'#16a34a'}};
function draw(id, shapes) {{
  const c = document.getElementById(id), g = c.getContext('2d');
  for (const s of shapes) {{ g.fillStyle = COL[s.colour]; g.beginPath();
    if (s.shape === 'circle') g.arc(s.x, s.y, 44, 0, 7);
    if (s.shape === 'square') g.rect(s.x - 40, s.y - 40, 80, 80);
    if (s.shape === 'hexagon') for (let k = 0; k < 6; k++) {{
      const a = Math.PI / 3 * k; g.lineTo(s.x + 46 * Math.cos(a), s.y + 46 * Math.sin(a)); }}
    g.closePath(); g.fill(); }}
  c.addEventListener('click', (e) => {{
    const b = c.getBoundingClientRect(), x = e.clientX - b.left, y = e.clientY - b.top;
    let hit = null; for (const s of shapes) if (Math.hypot(s.x - x, s.y - y) < 48) hit = s.id;
    report('click', hit); }});
}}
draw('c1', DATA.top); draw('c2', DATA.far);
</script>""",
    )


def _page_hover_tip(d: dict) -> tuple[str, str]:
    buttons = "".join(
        f'<button class="rb" data-i="{i}"><svg width="26" height="26" viewBox="0 0 26 26" '
        'aria-hidden="true"><circle cx="13" cy="13" r="9" fill="#555"/></svg></button>'
        for i in range(len(d["tips"]))
    )
    return (
        "Actions",
        f"""<style>.rb{{border-radius:50%;width:48px;height:48px;margin:10px}}</style>
<h1>Actions</h1><div id="bar">{buttons}</div>
<canvas id="tip" width="120" height="32"
 style="display:none;position:absolute;pointer-events:none"></canvas>
<script>
const tip = document.getElementById('tip'), g = tip.getContext('2d');
for (const b of document.querySelectorAll('#bar .rb')) {{
  b.addEventListener('mouseenter', () => {{
    const r = b.getBoundingClientRect();
    g.clearRect(0, 0, 120, 32); g.fillStyle = '#111'; g.fillRect(0, 0, 120, 32);
    g.fillStyle = '#fff'; g.font = '15px sans-serif'; g.fillText(DATA.tips[+b.dataset.i], 10, 21);
    tip.style.left = (r.left + scrollX) + 'px'; tip.style.top = (r.bottom + scrollY + 6) + 'px';
    tip.style.display = 'block'; }});
  b.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; }});
  b.addEventListener('click', () => report('click', +b.dataset.i));
}}
</script>""",
    )


def _page_regression(d: dict) -> tuple[str, str]:
    tiles = "".join(
        f'<div class="tile" id="t{i}"><button data-i="{i}">This one</button></div>'
        for i in range(12)
    )
    return (
        "Release",
        f"""<style>.tile{{display:inline-block;width:150px;height:84px;margin:8px;
 background:#e5e7eb;border:1px solid #9ca3af;vertical-align:top;padding:6px;box-sizing:border-box}}
 #grid{{width:720px}}</style>
<h1>Release</h1><button id="deploy">Deploy</button><div id="grid">{tiles}</div><p id="msg"></p>
<script>
document.getElementById('deploy').addEventListener('click', () => {{
  const t = document.getElementById('t' + DATA.changed);
  if (DATA.kind === 'shift') t.style.transform = 'translateX(1px)';
  else t.style.background = '#d9dce2';
  report('deployed', true); document.getElementById('msg').textContent = 'Deployed.'; }});
for (const b of document.querySelectorAll('#grid button'))
  b.addEventListener('click', async () => {{ await report('pick', +b.dataset.i);
    document.getElementById('msg').textContent = 'Picked.'; }});
</script>""",
    )


def _cards(n: int, style_of) -> str:
    return "".join(
        f'<div class="card" id="k{i}"><p style="{style_of(i)}">Status: all systems ready</p>'
        f'<button data-i="{i}">Report</button></div>'
        for i in range(n)
    )


_CARD_CSS = """<style>.card{display:inline-block;vertical-align:top;width:190px;height:120px;
 margin:12px;border:1px solid #888;background:#fafafa;padding:10px;box-sizing:border-box}
 #cards{position:relative;width:900px}</style>"""


def _page_overlap(d: dict) -> tuple[str, str]:
    cards = _cards(4, lambda i: "font-size:15px")
    return (
        "Cards",
        f"""{_CARD_CSS}<h1>Cards</h1><div id="cards">{cards}</div>
<div id="cover" style="position:absolute;width:110px;height:26px;background:#fafafa"></div>
<p id="msg"></p>
<script>
const k = document.getElementById('k' + DATA.covered).querySelector('p').getBoundingClientRect();
const cv = document.getElementById('cover');
cv.style.left = (k.left + scrollX + 60) + 'px'; cv.style.top = (k.top + scrollY - 4) + 'px';
for (const b of document.querySelectorAll('#cards button'))
  b.addEventListener('click', async () => {{ await report('report', +b.dataset.i);
    document.getElementById('msg').textContent = 'Reported.'; }});
</script>""",
    )


def _page_faint_text(d: dict) -> tuple[str, str]:
    cards = _cards(6, lambda i: "font-size:15px;color:var(--ink)")
    return (
        "Notices",
        f"""{_CARD_CSS}<h1>Notices</h1><div id="cards">{cards}</div><p id="msg"></p>
<script>
document.querySelectorAll('#cards .card p').forEach((p, i) =>
  p.style.setProperty('--ink', i === DATA.faint ? '#c2c2c2' : '#8a8a8a'));
for (const b of document.querySelectorAll('#cards button'))
  b.addEventListener('click', async () => {{ await report('report', +b.dataset.i);
    document.getElementById('msg').textContent = 'Reported.'; }});
</script>""",
    )


def _page_misaligned(d: dict) -> tuple[str, str]:
    boxes = "".join(f'<button class="bx" data-i="{i}"></button>' for i in range(36))
    return (
        "Grid",
        f"""<style>#grid{{display:grid;grid-template-columns:repeat(6,46px);gap:10px}}
 .bx{{width:46px;height:30px;background:#fff;border:2px solid #334155;margin:0;padding:0}}</style>
<h1>Grid</h1><div id="grid">{boxes}</div>
<script>
document.querySelectorAll('#grid .bx').forEach((b, i) => {{
  if (i === DATA.off) b.style.transform = 'translateY(' + DATA.dy + 'px)';
  b.addEventListener('click', () => report('click', i)); }});
</script>""",
    )
