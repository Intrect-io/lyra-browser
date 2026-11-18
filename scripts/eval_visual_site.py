"""The loopback site of ``eval_visual.py``: tasks that are only solvable by looking.

Every task is a page whose answer lives in pixels (a canvas, a CSS colour, an overflow,
a toast that is gone in seconds), never in text the accessibility tree or ``read_page``
would hand over. The page reports what the agent did through ``fetch`` (never a form
navigation, so no consent is involved) and the server keeps the score: the oracle is on
this side of the wire, and nothing the agent says counts.

Control endpoints (reset, solution, result) need a token only the harness knows, so an
agent that wanders to ``/api/...`` learns nothing.
"""

from __future__ import annotations

import base64
import json
import random
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

# What the agent is told, per task. The page itself carries no instruction.
GOALS: dict[str, str] = {
    "canvas_target": "On this page, click the green star.",
    "icon_toolbar": "On this page, click the button whose icon is a trash can (delete).",
    "toast_code": (
        "Press 'Generate code'. A code appears for a moment. Type that code into the "
        "Code box and press Check."
    ),
    "color_state": ("Press 'Next colour' until the big square is blue, then press Done."),
    "canvas_modal": (
        "Close the dialog that covers the page (its close button is an X), then press Continue."
    ),
    "chart_value": (
        "Read the value of the 'Mar' bar in the chart, type it into the Value box and press Check."
    ),
    "layout_fault": (
        "One of the four cards has text spilling out of its box. Press 'Report' on that card."
    ),
    "changed_panel": (
        "Press 'Apply theme'. One of the three panels changes colour: press 'This one' on "
        "that panel."
    ),
}
TASKS: tuple[str, ...] = tuple(GOALS)

SHAPES = ("circle", "square", "triangle", "star")
COLOURS = {"red": "#d62828", "green": "#2a9d3a", "blue": "#1d4ed8", "orange": "#f08c00"}
ICONS = ("trash", "pencil", "star", "gear", "download", "heart")
PALETTE = {
    "red": "#dc2626",
    "blue": "#2563eb",
    "green": "#16a34a",
    "yellow": "#eab308",
    "purple": "#9333ea",
}
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May")


def _obscure(data: Any) -> str:
    """Page data as base64 with a xor: not secret, just not text a reader hands over."""
    raw = json.dumps(data).encode()
    return base64.b64encode(bytes(b ^ 0x5A for b in raw)).decode()


_HEAD = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
 body {{ font-family: sans-serif; margin: 24px; background: #fafafa; color: #222; }}
 button {{ font-size: 15px; padding: 6px 14px; margin: 4px; }}
 input {{ font-size: 15px; padding: 5px; }}
</style>
<script>
 const DATA = JSON.parse(atob("{data}").split('').map(c =>
   String.fromCharCode(c.charCodeAt(0) ^ 0x5A)).join(''));
 function report(kind, value) {{
   return fetch('/api/{task}/event', {{method: 'POST', headers: {{'Content-Type':
     'application/json'}}, body: JSON.stringify({{kind, value}})}});
 }}
</script></head><body>
"""


def _page(task: str, title: str, data: Any, body: str) -> str:
    return _HEAD.format(title=title, data=_obscure(data), task=task) + body + "</body></html>"


@dataclass
class TaskState:
    """One task's current round: what was drawn, and what the agent did about it."""

    data: dict = field(default_factory=dict)
    success: bool = False
    events: list[dict] = field(default_factory=list)


class EvalSite:
    """The task pages, their per-round randomness and the score."""

    def __init__(self, seed: int | None = None) -> None:
        self.token = secrets.token_hex(16)
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.state: dict[str, TaskState] = {}
        for task in TASKS:
            self.reset(task)

    # ---- rounds -----------------------------------------------------------------------
    def reset(self, task: str) -> None:
        rng = self.rng
        data: dict[str, Any]
        if task == "canvas_target":
            cells = [(c, r) for c in range(3) for r in range(2)]
            rng.shuffle(cells)
            combos = [("star", "green")]
            others = [(s, c) for s in SHAPES for c in COLOURS if (s, c) != ("star", "green")]
            rng.shuffle(others)
            # Decoys share a shape or a colour with the target, so neither alone decides.
            decoys = [
                ("star", rng.choice(["red", "blue", "orange"])),
                (rng.choice(["circle", "square", "triangle"]), "green"),
            ]
            decoys += [o for o in others if o not in decoys][:3]
            combos += decoys
            shapes = []
            for i, ((shape, colour), (c, r)) in enumerate(zip(combos, cells, strict=False)):
                shapes.append(
                    {
                        "id": i,
                        "shape": shape,
                        "colour": colour,
                        "x": 100 + c * 200 + rng.randint(-30, 30),
                        "y": 100 + r * 200 + rng.randint(-30, 30),
                    }
                )
            data = {"shapes": shapes, "target": 0}
        elif task == "icon_toolbar":
            icons = list(ICONS)
            rng.shuffle(icons)
            data = {"icons": icons, "target": "trash"}
        elif task == "toast_code":
            data = {"issued": None}
        elif task == "color_state":
            order = list(PALETTE)
            rng.shuffle(order)
            if order[0] == "blue":
                order.append(order.pop(0))
            data = {"order": order, "hex": PALETTE}
        elif task == "canvas_modal":
            corner = rng.choice(["tl", "tr", "bl", "br"])
            data = {"corner": corner, "closed": False}
        elif task == "chart_value":
            values = [rng.randint(12, 98) for _ in MONTHS]
            data = {"months": list(MONTHS), "values": values, "target": "Mar"}
        elif task == "layout_fault":
            data = {"faulty": rng.randrange(4)}
        elif task == "changed_panel":
            data = {"changed": rng.randrange(3)}
        else:
            raise KeyError(task)
        with self.lock:
            self.state[task] = TaskState(data=data)

    def solution(self, task: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state[task].data))

    def result(self, task: str) -> dict:
        with self.lock:
            st = self.state[task]
            return {"success": st.success, "events": list(st.events)}

    # ---- the oracle ---------------------------------------------------------------------
    def record(self, task: str, kind: str, value: Any) -> None:
        with self.lock:
            st = self.state[task]
            st.events.append({"kind": kind, "value": value})
            d = st.data
            if task == "canvas_target" and kind == "click":
                st.success = value == d["target"]
            elif task == "icon_toolbar" and kind == "click":
                st.success = value == d["target"]
            elif task == "toast_code":
                if kind == "issued":
                    d["issued"] = value
                elif kind == "answer":
                    st.success = bool(d["issued"]) and str(value).strip().upper() == d["issued"]
            elif task == "color_state" and kind == "done":
                st.success = value == "blue"
            elif task == "canvas_modal":
                if kind == "closed":
                    d["closed"] = True
                elif kind == "continue":
                    st.success = bool(d["closed"])
            elif task == "chart_value" and kind == "answer":
                want = d["values"][d["months"].index(d["target"])]
                st.success = str(value).strip() == str(want)
            elif task == "layout_fault" and kind == "report":
                st.success = value == d["faulty"]
            elif task == "changed_panel" and kind == "pick":
                applied = any(e["kind"] == "applied" for e in st.events)
                st.success = applied and value == d["changed"]

    # ---- pages --------------------------------------------------------------------------
    def page(self, task: str) -> str:
        d = self.solution(task)
        return getattr(self, f"_page_{task}")(task, d)

    def _page_canvas_target(self, task: str, d: dict) -> str:
        body = """<h1>Shapes</h1>
<canvas id="c" width="600" height="400" style="border:1px solid #ccc;background:#fff"></canvas>
<script>
const COL = {red:'#d62828', green:'#2a9d3a', blue:'#1d4ed8', orange:'#f08c00'};
const g = document.getElementById('c').getContext('2d');
function star(x, y, r) { g.beginPath(); for (let i = 0; i < 10; i++) {
  const a = Math.PI / 5 * i - Math.PI / 2, rr = i % 2 ? r * 0.45 : r;
  g.lineTo(x + rr * Math.cos(a), y + rr * Math.sin(a)); } g.closePath(); g.fill(); }
for (const s of DATA.shapes) { g.fillStyle = COL[s.colour];
  if (s.shape === 'circle') { g.beginPath(); g.arc(s.x, s.y, 36, 0, 7); g.fill(); }
  if (s.shape === 'square') g.fillRect(s.x - 34, s.y - 34, 68, 68);
  if (s.shape === 'triangle') { g.beginPath(); g.moveTo(s.x, s.y - 40);
    g.lineTo(s.x + 40, s.y + 32); g.lineTo(s.x - 40, s.y + 32); g.closePath(); g.fill(); }
  if (s.shape === 'star') star(s.x, s.y, 42); }
document.getElementById('c').addEventListener('click', (e) => {
  const b = e.target.getBoundingClientRect(); const x = e.clientX - b.left, y = e.clientY - b.top;
  let hit = null; for (const s of DATA.shapes) if (Math.hypot(s.x - x, s.y - y) < 44) hit = s.id;
  report('click', hit); });
</script>"""
        return _page(task, "Shapes", d, body)

    def _page_icon_toolbar(self, task: str, d: dict) -> str:
        svg = {
            "trash": '<rect x="7" y="9" width="14" height="15" rx="2"/><rect x="5" y="5" width="18" height="3"/><rect x="11" y="2" width="6" height="3"/>',  # noqa: E501
            "pencil": '<path d="M5 23 L7 16 L19 4 L24 9 L12 21 Z"/>',
            "star": '<path d="M14 2 L17.5 10 L26 10.5 L19.5 16 L21.5 25 L14 20 L6.5 25 L8.5 16 L2 10.5 L10.5 10 Z"/>',  # noqa: E501
            "gear": '<circle cx="14" cy="14" r="7"/><rect x="12" y="1" width="4" height="26"/><rect x="1" y="12" width="26" height="4"/>',  # noqa: E501
            "download": '<path d="M11 2 H17 V13 H22 L14 21 L6 13 H11 Z"/><rect x="4" y="23" width="20" height="3"/>',  # noqa: E501
            "heart": '<path d="M14 25 L3 13 A6 6 0 0 1 14 6 A6 6 0 0 1 25 13 Z"/>',
        }
        buttons = "".join(
            f'<button data-i="{i}"><svg width="28" height="28" viewBox="0 0 28 28" '
            f'fill="#333" aria-hidden="true">{svg[name]}</svg></button>'
            for i, name in enumerate(d["icons"])
        )
        body = f"""<h1>Toolbar</h1><div id="bar">{buttons}</div>
<script>
for (const b of document.querySelectorAll('#bar button'))
  b.addEventListener('click', () => report('click', DATA.icons[+b.dataset.i]));
</script>"""
        return _page(task, "Toolbar", d, body)

    def _page_toast_code(self, task: str, d: dict) -> str:
        body = """<h1>Codes</h1>
<button id="gen">Generate code</button>
<canvas id="toast" width="320" height="70"
 style="display:none;position:fixed;right:30px;bottom:30px"></canvas>
<p><label>Code <input id="code" autocomplete="off"></label> <button id="check">Check</button></p>
<p id="msg"></p>
<script>
const A = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
let timer = null;
document.getElementById('gen').addEventListener('click', () => {
  let code = ''; for (let i = 0; i < 6; i++) code += A[Math.floor(Math.random() * A.length)];
  report('issued', code);
  const t = document.getElementById('toast'), g = t.getContext('2d');
  g.fillStyle = '#111'; g.fillRect(0, 0, 320, 70); g.fillStyle = '#fff';
  g.font = 'bold 26px monospace'; g.fillText('CODE: ' + code, 24, 45);
  t.style.display = 'block'; clearTimeout(timer);
  timer = setTimeout(() => { t.style.display = 'none'; g.clearRect(0, 0, 320, 70); }, 2500);
});
document.getElementById('check').addEventListener('click', async () => {
  await report('answer', document.getElementById('code').value);
  document.getElementById('msg').textContent = 'Checked.';
});
</script>"""
        return _page(task, "Codes", d, body)

    def _page_color_state(self, task: str, d: dict) -> str:
        body = """<h1>Colours</h1>
<div id="sq" style="width:220px;height:220px;border:2px solid #333"></div>
<p><button id="next">Next colour</button> <button id="done">Done</button></p>
<p id="msg"></p>
<script>
let i = 0; const sq = document.getElementById('sq');
function paint() { sq.style.background = DATA.hex[DATA.order[i]]; }
paint();
document.getElementById('next').addEventListener('click', () => {
  i = (i + 1) % DATA.order.length; paint(); });
document.getElementById('done').addEventListener('click', async () => {
  await report('done', DATA.order[i]); document.getElementById('msg').textContent = 'Saved.'; });
</script>"""
        return _page(task, "Colours", d, body)

    def _page_canvas_modal(self, task: str, d: dict) -> str:
        body = """<h1>Welcome</h1><p>Read the notice, then continue.</p>
<button id="cont">Continue</button><p id="msg"></p>
<canvas id="modal" style="position:fixed;left:0;top:0;width:100vw;height:100vh"></canvas>
<script>
const m = document.getElementById('modal');
m.width = innerWidth; m.height = innerHeight;
const g = m.getContext('2d');
g.fillStyle = 'rgba(0,0,0,0.55)'; g.fillRect(0, 0, m.width, m.height);
const bw = 460, bh = 260, bx = (m.width - bw) / 2, by = (m.height - bh) / 2;
g.fillStyle = '#fff'; g.fillRect(bx, by, bw, bh);
g.fillStyle = '#222'; g.font = '22px sans-serif'; g.fillText('Notice', bx + 30, by + 120);
g.font = '16px sans-serif'; g.fillText('Close this dialog to continue.', bx + 30, by + 150);
const cx = DATA.corner[1] === 'l' ? bx + 26 : bx + bw - 26;
const cy = DATA.corner[0] === 't' ? by + 26 : by + bh - 26;
g.strokeStyle = '#222'; g.lineWidth = 3; g.beginPath();
g.moveTo(cx - 9, cy - 9); g.lineTo(cx + 9, cy + 9); g.moveTo(cx + 9, cy - 9);
g.lineTo(cx - 9, cy + 9); g.stroke();
m.addEventListener('click', (e) => {
  if (Math.hypot(e.clientX - cx, e.clientY - cy) < 16) { m.remove(); report('closed', true); }
});
document.getElementById('cont').addEventListener('click', async () => {
  await report('continue', true); document.getElementById('msg').textContent = 'Continued.'; });
</script>"""
        return _page(task, "Welcome", d, body)

    def _page_chart_value(self, task: str, d: dict) -> str:
        body = """<h1>Sales</h1>
<canvas id="chart" width="560" height="320" style="background:#fff;border:1px solid #ccc"></canvas>
<p><label>Value <input id="val" autocomplete="off"></label> <button id="check">Check</button></p>
<p id="msg"></p>
<script>
const g = document.getElementById('chart').getContext('2d');
g.font = '15px sans-serif';
DATA.months.forEach((mo, k) => { const v = DATA.values[k], x = 50 + k * 100, h = v * 2.4;
  g.fillStyle = '#4f7cac'; g.fillRect(x, 280 - h, 60, h);
  g.fillStyle = '#222'; g.fillText(mo, x + 16, 302); g.fillText(String(v), x + 18, 272 - h); });
document.getElementById('check').addEventListener('click', async () => {
  await report('answer', document.getElementById('val').value);
  document.getElementById('msg').textContent = 'Checked.'; });
</script>"""
        return _page(task, "Sales", d, body)

    def _page_layout_fault(self, task: str, d: dict) -> str:
        cards = []
        for i in range(4):
            spill = (
                "font-size:30px;letter-spacing:9px;white-space:nowrap"
                if i == d["faulty"]
                else "font-size:15px"
            )
            cards.append(
                f'<div class="card"><p style="{spill}">Quarterly summary</p>'
                f'<button data-i="{i}">Report</button></div>'
            )
        body = (
            """<style>.card{display:inline-block;vertical-align:top;width:170px;height:130px;
 margin:14px 40px 14px 0;border:2px solid #444;background:#fff;padding:8px;overflow:visible}</style>
<h1>Cards</h1><div id="cards">"""
            + "".join(cards)
            + """</div><p id="msg"></p>
<script>
for (const b of document.querySelectorAll('#cards button'))
  b.addEventListener('click', async () => { await report('report', +b.dataset.i);
    document.getElementById('msg').textContent = 'Reported.'; });
</script>"""
        )
        return _page(task, "Cards", d, body)

    def _page_changed_panel(self, task: str, d: dict) -> str:
        panels = "".join(
            f'<div class="panel" id="p{i}"><p>Panel</p><button data-i="{i}">This one</button></div>'
            for i in range(3)
        )
        body = (
            """<style>.panel{display:inline-block;width:160px;height:120px;margin:10px;
 border:1px solid #888;background:#f4f4f4;padding:8px}</style>
<h1>Theme</h1><button id="apply">Apply theme</button><div>"""
            + panels
            + """</div>
<p id="msg"></p>
<script>
document.getElementById('apply').addEventListener('click', () => {
  document.getElementById('p' + DATA.changed).style.background = '#f59e0b';
  report('applied', true); });
for (const b of document.querySelectorAll('.panel button'))
  b.addEventListener('click', async () => { await report('pick', +b.dataset.i);
    document.getElementById('msg').textContent = 'Picked.'; });
</script>"""
        )
        return _page(task, "Theme", d, body)


def make_handler(site: EvalSite) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            raw = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data), "application/json")

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            url = urlparse(self.path)
            parts = url.path.strip("/").split("/")
            if url.path == "/":
                links = "".join(f'<li><a href="/task/{t}">{t}</a></li>' for t in TASKS)
                return self._send(200, f"<!doctype html><title>eval</title><ul>{links}</ul>")
            if len(parts) == 2 and parts[0] == "task" and parts[1] in GOALS:
                return self._send(200, site.page(parts[1]))
            if len(parts) == 3 and parts[0] == "api" and parts[1] in GOALS:
                token = parse_qs(url.query).get("token", [""])[0]
                if not secrets.compare_digest(token, site.token):
                    return self._json(403, {"error": "forbidden"})
                task, what = parts[1], parts[2]
                if what == "reset":
                    site.reset(task)
                    return self._json(200, {"ok": True})
                if what == "solution":
                    return self._json(200, site.solution(task))
                if what == "result":
                    return self._json(200, site.result(task))
            if url.path == "/favicon.ico":
                return self._send(404, "")
            return self._send(404, "not found")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "api" and parts[1] in GOALS and parts[2] == "event":
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "bad json"})
                site.record(parts[1], str(body.get("kind")), body.get("value"))
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "not found"})

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


def serve(site: EvalSite, port: int = 0) -> ThreadingHTTPServer:
    """Start the site on loopback in a daemon thread; the caller shuts it down."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(site))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
