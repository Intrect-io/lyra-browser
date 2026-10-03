#!/usr/bin/env python3
"""Probe: what else does dropping ``--enable-unsafe-swiftshader`` change?

Playwright adds ``--enable-unsafe-swiftshader`` to every launch. Where Chrome cannot use a GPU
that switch is what gives a page a software WebGL, and DataDome refuses the browser that has
one when plain Chrome has none. ``ignore_default_args`` removes it. Before
that is done for everyone this script measures what else it costs, launching exactly like the
product (``session.launch_kwargs`` + ``launch_persistent_context``, a throwaway profile) with
that one argument as the only difference between the two cells (``with`` / ``without``).

Per launch it records

  stack         the switch really is in / out of the browser's argv (/proc), GPU process
                incarnations and flags, the ``chrome://gpu`` feature status, problems and log,
                CPU time and RSS of the process tree
  captures      ``capture.take`` around ``page.screenshot`` (viewport, full page) and
                ``locator.screenshot`` (element) on loopback fixtures - plain DOM, CSS
                transforms / filters / gradients / fixed + scroll, canvas 2D, WebGL (unchecked
                and with a canvas 2D fallback), 9000px and 33000px pages, a raster-bound 12000px
                page: the cold first capture and warm repeats in ms, retries of the first-frame
                transient, and the PNG
  webgl         what detection snippets, ``getContext`` and ``OffscreenCanvas`` return, and what
                a page that calls ``getContext('webgl')`` without checking does
  page-visible  FINGERPRINT_JS of probe_document_intercept.py, GL / codec / media-query facts,
                and a few raster-bound timings (rAF cadence, canvas 2D, DOM paint)

``report`` pairs the cells: a permutation test on per-launch medians for every timing, and for
every capture whether the PNG bytes are equal or, if not, how many decoded pixels differ (stdlib
PNG decoder; PIL is not needed). Both modes run in one process with the cells interleaved
(which variant goes first alternates), so host load lands on both. The numbers are only
comparable within one run: this host is shared and loaded, quote ranges.

    .venv/bin/python scripts/probe_swiftshader.py run --reps 8 --out /tmp/sw
    .venv/bin/python scripts/probe_swiftshader.py run --reps 5 --route --out /tmp/sw-route
    .venv/bin/python scripts/probe_swiftshader.py run --reps 5 --guard cdp --out /tmp/sw-cdp
    .venv/bin/python scripts/probe_swiftshader.py report --out /tmp/sw

``--route`` adds a pass-through ``context.route("**/*")`` to every launch (Fetch interception and
the HTTP cache off, what the ``route`` guard does). ``--guard cdp`` builds the launch options
with ``Config(guard_backend="cdp")`` (it adds ``--remote-debugging-port=0``; the switch drop it
also asks for is applied here to the ``without`` cell only). Headful needs ``Xvfb`` on PATH:
every headful launch gets a private server on a high display number, so the real ``$DISPLAY``
is never used (``--display :N`` reuses one that exists). Headless launches get an environment
with no display at all, as a Hermes gateway has. Nothing is written into the repo: results,
PNGs and profiles go under ``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import os
import random
import re
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SWITCH = "--enable-unsafe-swiftshader"
MODES = ("headless", "headful")
VARIANTS = ("with", "without")  # the switch kept (Playwright's default) / dropped
SHOT_REPS = {"viewport": 5, "full": 4, "element": 5}  # the first capture of each is the cold one
ONLY_FULL = {"tall-xl": 2, "heavy": 3}  # full page only, this many captures: cost grows with area
NOISE = "GPU stall due to ReadPixels"  # the fixtures' own readPixels, not a finding
LAUNCH_TIMEOUT_S = 300

# --------------------------------------------------------------------------
# Fixtures, served from loopback. Each sets window.__ready once it has painted.
# --------------------------------------------------------------------------

_HEAD = "<!doctype html><meta charset=utf-8><title>{0}</title>"

_GL_DRAW = """
const sh = (t, s) => { const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o);
  return o; };
const p = gl.createProgram();
gl.attachShader(p, sh(gl.VERTEX_SHADER, 'attribute vec2 a;void main(){gl_Position=vec4(a,0,1);}'));
gl.attachShader(p, sh(gl.FRAGMENT_SHADER, 'void main(){gl_FragColor=vec4(1,.2,.1,1);}'));
gl.linkProgram(p); gl.useProgram(p);
gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-.8, -.8, .8, -.8, 0, .8]), gl.STATIC_DRAW);
const a = gl.getAttribLocation(p, 'a'); gl.enableVertexAttribArray(a);
gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);
gl.clearColor(.1, .1, .4, 1); gl.clear(gl.COLOR_BUFFER_BIT); gl.drawArrays(gl.TRIANGLES, 0, 3);
const px = new Uint8Array(4); gl.readPixels(200, 150, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
window.__px = Array.from(px); window.__drawn = true;
"""

PAGES: dict[str, str] = {
    "dom": _HEAD.format("dom")
    + """
<style>body{font:16px/1.5 system-ui,sans-serif;margin:24px;color:#222}
#card{border:1px solid #888;padding:12px 16px;width:520px}
td,th{border:1px solid #bbb;padding:4px 10px}table{border-collapse:collapse}</style>
<h1>Plain DOM</h1>
<section id=card><h2>Card</h2><p>The quick brown fox jumps over the lazy dog 0123456789 &eacute;
&#xD55C;&#xAE00; &#x65E5;&#x672C;&#x8A9E;</p><ul><li>alpha<li>beta<li>gamma</ul>
<table><tr><th>k<th>v<tr><td>one<td>1<tr><td>two<td>2</table>
<p><input value="text input"> <select><option>one</select> <button>button</button>
<a href="#x">link</a></p></section>
<svg id=svg width=220 height=90><defs><linearGradient id=g><stop offset=0 stop-color="#36f"/>
<stop offset=1 stop-color="#f63"/></linearGradient></defs><rect x=4 y=4 width=212 height=82
rx=14 fill="url(#g)"/><circle cx=60 cy=45 r=26 fill="#fff" fill-opacity=.7 /><path
d="M110 70L150 20L190 70Z" fill=none stroke="#000" stroke-width=3 /></svg>
<img id=img width=120 height=60 alt=x src="data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg'
width='120' height='60'><rect width='120' height='60' fill='%23393'/><text x='8' y='38'
font-size='24' fill='white'>img</text></svg>">
<script>window.__ready = true</script>""",
    "css": _HEAD.format("css")
    + """
<style>body{margin:0;height:2400px;background:linear-gradient(#fafafa,#cde 60%,#fed);
font:16px system-ui,sans-serif}
#bar{position:fixed;top:0;left:0;right:0;height:44px;background:linear-gradient(90deg,#246,#68a);
color:#fff;line-height:44px;padding-left:16px;z-index:5}
#badge{position:fixed;right:12px;bottom:12px;padding:6px 10px;background:#c33;color:#fff;
border-radius:12px;box-shadow:0 2px 8px #0008;z-index:5}
#stage{position:relative;margin:70px 24px 0;height:640px;perspective:700px}
.t{position:absolute;width:150px;height:110px;border-radius:14px;color:#fff;padding:10px;
box-sizing:border-box}
.rot{left:10px;top:10px;background:linear-gradient(135deg,#f80,#e04);transform:rotate(12deg)}
.skew{left:200px;top:20px;background:radial-gradient(circle at 30% 30%,#8f8,#063);
transform:scale(1.2) skewX(-10deg)}
.d3{left:420px;top:10px;background:conic-gradient(#f33,#ff3,#3f3,#3ff,#33f,#f3f,#f33);
transform:rotateY(35deg) rotateX(10deg)}
.blur{left:10px;top:170px;background:#36c;filter:blur(3px)}
.shadow{left:200px;top:170px;background:#3a6;filter:drop-shadow(8px 8px 5px rgba(0,0,0,.55))}
.hue{left:420px;top:170px;background:repeating-linear-gradient(45deg,#f44 0 10px,#fa4 10px 20px);
filter:hue-rotate(90deg) saturate(1.6) contrast(1.1)}
.glass{left:10px;top:340px;width:300px;height:130px;background:rgba(255,255,255,.25);
backdrop-filter:blur(8px);border:1px solid #fff8;color:#000}
.clip{left:340px;top:340px;background:#639;
clip-path:polygon(50% 0,100% 38%,82% 100%,18% 100%,0 38%)}
.mix{left:10px;top:500px;background:#f90;mix-blend-mode:multiply;opacity:.85}
.mask{left:200px;top:500px;background:#09c;-webkit-mask-image:linear-gradient(90deg,#000,transparent)}
.lay{left:420px;top:500px;background:#333;will-change:transform;
transform:translateZ(0) translate(8px,4px);text-shadow:2px 2px 3px #000}
#inner{position:absolute;left:600px;top:10px;width:180px;height:160px;overflow:auto;
background:#fff;border:1px solid #444}
#inner div{height:600px;
background:repeating-linear-gradient(0deg,#eef 0 20px,#dde 20px 40px)}</style>
<div id=bar>fixed header</div><div id=badge>fixed badge</div>
<div id=stage><div class="t rot">rotate</div><div class="t skew">scale+skew</div>
<div class="t d3">3d</div><div class="t blur">blur</div><div class="t shadow">drop-shadow</div>
<div class="t hue">hue</div><div class="t glass">backdrop-filter over the gradient</div>
<div class="t clip">clip-path</div><div class="t mix">multiply</div><div class="t mask">mask</div>
<div class="t lay">layer</div><div id=inner><div></div></div></div>
<script>document.getElementById('inner').scrollTop = 130; window.scrollTo(0, 300);
requestAnimationFrame(() => requestAnimationFrame(() => { window.__ready = true; }));</script>""",
    "canvas2d": _HEAD.format("canvas2d")
    + """
<body style="margin:16px"><canvas id=c width=720 height=420 style="border:1px solid #999"></canvas>
<script>
const x = document.getElementById('c').getContext('2d');
const g = x.createLinearGradient(0, 0, 720, 0);
g.addColorStop(0, '#08f'); g.addColorStop(1, '#f80');
x.fillStyle = g; x.fillRect(0, 0, 720, 420);
x.fillStyle = '#fff8'; for (let i = 0; i < 12; i++) x.fillRect(20 + i * 55, 20 + i * 12, 40, 300);
x.beginPath(); x.arc(160, 200, 90, 0, 6.283); x.fillStyle = '#103'; x.fill();
x.lineWidth = 6; x.strokeStyle = '#fe0'; x.stroke();
x.beginPath(); x.moveTo(300, 360); x.bezierCurveTo(380, 40, 520, 420, 690, 60);
x.lineWidth = 5; x.strokeStyle = '#fff'; x.stroke();
x.font = 'bold 40px sans-serif'; x.fillStyle = '#000'; x.shadowColor = '#0008';
x.shadowBlur = 8; x.shadowOffsetX = 3; x.fillText('Canvas 2D \\u00e9\\ud55c', 280, 120);
x.shadowBlur = 0; x.strokeStyle = '#fff'; x.lineWidth = 1; x.strokeText('stroke text', 300, 170);
x.globalCompositeOperation = 'multiply'; x.fillStyle = '#f0f'; x.fillRect(480, 200, 150, 120);
x.globalCompositeOperation = 'source-over';
x.filter = 'blur(3px)'; x.fillStyle = '#0a4'; x.fillRect(60, 320, 120, 70); x.filter = 'none';
const o = document.createElement('canvas'); o.width = o.height = 64; const ox = o.getContext('2d');
ox.fillStyle = '#fc0'; ox.fillRect(0, 0, 64, 64);
ox.fillStyle = '#000'; ox.fillRect(16, 16, 32, 32);
x.drawImage(o, 520, 300, 150, 100);
x.save(); x.beginPath(); x.rect(600, 330, 80, 60); x.clip();
x.fillStyle = x.createPattern(o, 'repeat');
x.fillRect(560, 320, 200, 100); x.restore();
window.__ready = true;
</script>""",
    # No capability check on purpose: the page that breaks when getContext returns null.
    "webgl": _HEAD.format("webgl")
    + """
<body style="margin:16px"><canvas id=gl width=400 height=300 style="background:#eee"></canvas>
<script>
window.addEventListener('webglcontextlost', () => { window.__lost = true; }, true);
const t0 = performance.now();
const gl = document.getElementById('gl').getContext('webgl');
window.__glMs = Math.round(performance.now() - t0);
"""
    + _GL_DRAW
    + """</script><script>window.__ready = true</script>""",
    "webgl-fallback": _HEAD.format("webgl-fallback")
    + """
<body style="margin:16px"><canvas id=gl width=400 height=300 style="background:#eee"></canvas>
<p id=mode>pending</p>
<script>
const c = document.getElementById('gl'); const t0 = performance.now();
const gl = c.getContext('webgl'); window.__glMs = Math.round(performance.now() - t0);
if (gl) {"""
    + _GL_DRAW
    + """window.__mode = 'webgl'; } else {
  const x = c.getContext('2d'); x.fillStyle = '#1a1a66'; x.fillRect(0, 0, 400, 300);
  x.fillStyle = '#ff3319'; x.beginPath(); x.moveTo(200, 30); x.lineTo(360, 270); x.lineTo(40, 270);
  x.closePath(); x.fill(); window.__mode = 'canvas2d'; }
document.getElementById('mode').textContent = 'renderer: ' + window.__mode;
window.__ready = true;
</script>""",
    "tall": _HEAD.format("tall")
    + """
<style>body{margin:0;height:9000px;font:20px sans-serif;
background:repeating-linear-gradient(#fff 0 250px,#dfe 250px 500px)}
#bar{position:fixed;top:0;left:0;right:0;height:36px;background:#246;color:#fff;line-height:36px;
padding-left:12px}#band{position:absolute;left:40px;top:1000px;width:600px;height:3000px;
background:linear-gradient(#f66,#66f)}.m{position:absolute;left:700px}</style>
<div id=bar>fixed header</div><div id=band></div>
<script>for (let y = 0; y < 9000; y += 500) { const d = document.createElement('div');
d.className = 'm'; d.style.top = y + 'px'; d.textContent = 'y=' + y; document.body.append(d); }
window.__ready = true;</script>""",
    # Raster-bound: 240 cards, each with a 36px shadow, a gradient and a blur; a third also with
    # backdrop-filter.
    "heavy": _HEAD.format("heavy")
    + """
<style>body{margin:0;height:12000px;background:linear-gradient(#fff,#cde 50%,#fed);color:#fff}
.c{position:absolute;width:290px;height:170px;border-radius:18px;
background:linear-gradient(135deg,#f80,#36f);box-shadow:0 12px 36px rgba(0,0,0,.55);
filter:blur(.5px)}
.g{backdrop-filter:blur(8px);background:rgba(255,255,255,.25)}</style>
<body><script>for (let i = 0; i < 240; i++) { const d = document.createElement('div');
d.className = 'c' + (i % 3 ? '' : ' g'); d.textContent = i;
d.style.left = 20 + (i % 4) * 315 + 'px'; d.style.top = 20 + Math.floor(i / 4) * 195 + 'px';
document.body.append(d); }
window.__ready = true;</script>""",
    "probe": _HEAD.format("probe") + "probe<script>window.__ready = true</script>",
}
PAGES["tall-xl"] = PAGES["tall"].replace("9000", "33000")
FIXTURE_ORDER = ["dom", "css", "canvas2d", "webgl", "webgl-fallback", "tall", "tall-xl", "heavy"]
ELEMENTS = {
    "dom": ["#card", "#svg", "#img"],
    "css": ["#stage"],
    "canvas2d": ["#c"],
    "webgl": ["#gl"],
    "webgl-fallback": ["#gl"],
    "tall": ["#band"],
    "tall-xl": [],
    "heavy": [],
}


class Fixtures(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = PAGES.get(self.path.strip("/").removesuffix(".html"))
        if body is None:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args: object) -> None:
        pass


# --------------------------------------------------------------------------
# What the page can read: GL / codec / media facts on top of FINGERPRINT_JS
# --------------------------------------------------------------------------

PROBE_JS = r"""
async () => {
  const r = {};
  const guard = (f) => { try { return f(); } catch (e) { return 'ex:' + e.name; } };
  const fnv = (bytes) => { let h = 2166136261; for (const b of bytes) h = Math.imul(h ^ b, 16777619);
    return (h >>> 0).toString(16); };
  const info = (type, attrs) => {
    const c = document.createElement('canvas'); let why = null;
    c.addEventListener('webglcontextcreationerror', (e) => { why = e.statusMessage; });
    const t0 = performance.now(); let g;
    try { g = c.getContext(type, attrs); } catch (e) { return {ok: false, ex: e.name}; }
    const ms = Math.round(performance.now() - t0);
    if (!g) return {ok: false, why: why && why.slice(0, 90), ms};
    const d = g.getExtension('WEBGL_debug_renderer_info'); const p = (k) => g.getParameter(k);
    return {ok: true, ms, version: p(g.VERSION), maxTexture: p(g.MAX_TEXTURE_SIZE),
      maxViewport: Array.from(p(g.MAX_VIEWPORT_DIMS)).join('x'),
      extensions: (g.getSupportedExtensions() || []).length,
      unmasked: d ? p(d.UNMASKED_VENDOR_WEBGL) + ' | ' + p(d.UNMASKED_RENDERER_WEBGL) : null};
  };
  r.iface = typeof WebGLRenderingContext + '/' + typeof WebGL2RenderingContext;
  r.webgl = info('webgl'); r.webgl2 = info('webgl2'); r.experimental = info('experimental-webgl');
  r.noCaveat = info('webgl', {failIfMajorPerformanceCaveat: true});
  r.detect = {
    modernizr: guard(() => { const c = document.createElement('canvas');
      return !!(window.WebGLRenderingContext && (c.getContext('webgl') || c.getContext('experimental-webgl'))); }),
    webgl2: guard(() => !!(window.WebGL2RenderingContext && document.createElement('canvas').getContext('webgl2'))),
    hardwareOnly: guard(() => !!document.createElement('canvas').getContext('webgl', {failIfMajorPerformanceCaveat: true})),
  };
  r.offscreen = guard(() => (new OffscreenCanvas(8, 8).getContext('webgl') ? 'ok' : 'null'));
  r.offscreenWorker = await new Promise((res) => {
    try {
      const src = "postMessage((()=>{try{return new OffscreenCanvas(8,8).getContext('webgl')?'ok':'null'}catch(e){return 'ex:'+e.name}})())";
      const w = new Worker(URL.createObjectURL(new Blob([src])));
      w.onmessage = (e) => res(e.data); w.onerror = () => res('worker-error');
      setTimeout(() => res('timeout'), 3000);
    } catch (e) { res('ex:' + e.name); }
  });
  r.navigatorGpu = 'gpu' in navigator;
  try { const a = await navigator.gpu.requestAdapter(); r.webgpuAdapter = a ? 'adapter' : null; }
  catch (e) { r.webgpuAdapter = 'ex:' + e.name; }
  const c2 = document.createElement('canvas'); c2.width = 240; c2.height = 120;
  const x = c2.getContext('2d'); x.fillStyle = '#123456'; x.fillRect(0, 0, 4, 4);
  const d4 = x.getImageData(1, 1, 1, 1).data; r.canvas2d = d4[0] === 0x12 && d4[1] === 0x34 && d4[2] === 0x56;
  const gr = x.createRadialGradient(60, 60, 5, 60, 60, 80); gr.addColorStop(0, '#fff'); gr.addColorStop(1, '#306');
  x.fillStyle = gr; x.fillRect(0, 0, 240, 120); x.shadowBlur = 6; x.shadowColor = '#000';
  x.font = '22px serif'; x.fillText('Cwm fjord bank glyphs vext quiz', 6, 70);
  x.filter = 'blur(1.5px)'; x.beginPath(); x.arc(180, 60, 30, 0, 7); x.stroke();
  r.canvas2dHash = fnv(x.getImageData(0, 0, 240, 120).data);
  const gl = document.createElement('canvas').getContext('webgl');
  if (gl) { gl.clearColor(.2, .4, .6, 1); gl.clear(gl.COLOR_BUFFER_BIT); const px = new Uint8Array(4);
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); r.webglClear = Array.from(px).join(','); }
  r.mq = ['(dynamic-range: high)', '(video-dynamic-range: high)', '(prefers-reduced-motion: reduce)',
    '(prefers-color-scheme: dark)', '(any-hover: hover)', '(update: fast)', '(scripting: enabled)']
    .map((q) => (matchMedia(q).matches ? 1 : 0)).join('');
  r.css = ['backdrop-filter: blur(1px)', 'mask-image: none', 'aspect-ratio: 1', 'color: color-mix(in srgb, red, blue)',
    'contain: paint', 'content-visibility: auto'].map((q) => (CSS.supports(q) ? 1 : 0)).join('');
  const cfgs = {h264: 'avc1.42E01E', vp9: 'vp09.00.10.08', av1: 'av01.0.04M.08'};
  r.videoDecoder = {};
  for (const [k, codec] of Object.entries(cfgs)) {
    try { r.videoDecoder[k] = (await VideoDecoder.isConfigSupported({codec})).supported; }
    catch (e) { r.videoDecoder[k] = 'ex:' + e.name; }
  }
  r.canPlay = guard(() => document.createElement('video').canPlayType('video/mp4; codecs="avc1.42E01E"') || 'no');
  try {
    const m = await navigator.mediaCapabilities.decodingInfo({type: 'file', video: {
      contentType: 'video/mp4; codecs="avc1.42E01E"', width: 1280, height: 720, bitrate: 2e6, framerate: 30}});
    r.mediaCaps = [m.supported, m.smooth, m.powerEfficient].join('/');
  } catch (e) { r.mediaCaps = 'ex:' + e.name; }
  return r;
}
"""  # noqa: E501

BENCH_JS = r"""
async () => {
  const frame = () => new Promise((r) => requestAnimationFrame(r));
  const q = (a, p) => { const s = [...a].sort((x, y) => x - y);
    return +s[Math.min(s.length - 1, Math.floor(p * s.length))].toFixed(2); };
  const cadence = async (n, each) => { const d = []; let t = await frame();
    for (let i = 0; i < n; i++) { if (each) each(i); const u = await frame(); d.push(u - t); t = u; }
    return d; };
  const o = {};
  let d = await cadence(60);
  o.rafIdleMed = q(d, .5); o.rafIdleP95 = q(d, .95);
  const host = document.createElement('div');
  host.style.cssText = 'position:fixed;inset:0;overflow:hidden;background:#fff;z-index:9';
  document.body.append(host);
  for (let i = 0; i < 150; i++) { const e = document.createElement('div');
    e.style.cssText = `position:absolute;left:${(i * 37) % 1100}px;top:${(i * 53) % 650}px;width:96px;` +
      `height:64px;border-radius:10px;background:linear-gradient(${i * 7}deg,#f60,#06f);` +
      'box-shadow:0 4px 12px #0006;filter:blur(1px)';
    host.append(e); }
  const kids = [...host.children];
  d = await cadence(60, (i) => kids.forEach((e, k) => { e.style.transform =
    `translate(${Math.sin((i + k) / 9) * 30}px,${Math.cos((i + k) / 7) * 20}px)`; }));
  o.rafPaintMed = q(d, .5); o.rafPaintP95 = q(d, .95); o.rafPaintMax = q(d, 1);
  host.remove();
  const c = document.createElement('canvas'); c.width = c.height = 1024; const x = c.getContext('2d');
  let t0 = performance.now();
  for (let i = 0; i < 400; i++) { x.fillStyle = `hsl(${i % 360},80%,50%)`;
    x.fillRect((i * 13) % 900, (i * 7) % 900, 120, 120); }
  x.getImageData(0, 0, 1, 1); o.c2dFillMs = Math.round(performance.now() - t0);
  t0 = performance.now(); x.getImageData(0, 0, 1024, 1024); o.c2dReadMs = Math.round(performance.now() - t0);
  t0 = performance.now(); x.filter = 'blur(4px)'; for (let i = 0; i < 8; i++) x.drawImage(c, 0, 0);
  x.getImageData(0, 0, 1, 1); o.c2dBlurMs = Math.round(performance.now() - t0);
  t0 = performance.now();
  const big = document.createElement('div'); big.style.cssText = 'display:grid;grid-template-columns:repeat(40,30px)';
  for (let i = 0; i < 2500; i++) { const e = document.createElement('div');
    e.style.cssText = `height:20px;background:hsl(${i % 360},60%,60%);box-shadow:inset 0 0 4px #0004`;
    big.append(e); }
  document.body.append(big); await frame(); await frame();
  o.domPaintMs = Math.round(performance.now() - t0); big.remove();
  return o;
}
"""  # noqa: E501

# Text of chrome://gpu, which is built from shadow roots all the way down.
DEEP_TEXT_JS = r"""
() => {
  const out = [];
  const walk = (n) => {
    if (n.nodeType === 3) { const t = n.textContent.trim(); if (t) out.push(t); return; }
    if (n.nodeType !== 1 && n.nodeType !== 11) return;
    if (n.tagName === 'STYLE' || n.tagName === 'SCRIPT') return;
    if (n.shadowRoot) walk(n.shadowRoot);
    for (const c of n.childNodes) walk(c);
    if (/^(DIV|TR|LI|P|H1|H2|H3|H4|TABLE|SECTION|BR)$/.test(n.tagName || '')) out.push('\n');
  };
  walk(document.documentElement);
  return out.join(' ').replace(/ ?\n ?/g, '\n').replace(/\n+/g, '\n');
}
"""


def fingerprint_js() -> str:
    """The page-visible fingerprint probe of the compat harness, reused verbatim."""
    from probe_document_intercept import FINGERPRINT_JS

    return FINGERPRINT_JS


# --------------------------------------------------------------------------
# The browser from the outside: /proc
# --------------------------------------------------------------------------

_GPU_FLAG = re.compile(
    r"^--(use-gl|use-angle|ozone-platform|enable-unsafe-swiftshader|headless|disable-gpu\S*|"
    r"in-process-gpu|enable-gpu\S*)(=|$)"
)


def _argv(pid: int) -> list[str]:
    """argv of ``pid``; forked children rewrite it into one string with spaces."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return []
    args = [a.decode("utf-8", "replace") for a in raw if a]
    return args[0].split(" ") if len(args) == 1 else args


def proc_tree(profile: Path) -> dict:
    """One pass over /proc: the browser started on ``profile`` and everything under it."""
    ticks, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
    stats: dict[int, tuple[str, int, float, float]] = {}
    kids: dict[int, list[int]] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            raw = Path(entry.path, "stat").read_text()
        except OSError:
            continue
        head, _, tail = raw.rpartition(")")
        fields = tail.split()
        pid = int(entry.name)
        stats[pid] = (
            head.partition("(")[2],
            int(fields[1]),
            (int(fields[11]) + int(fields[12])) / ticks,
            int(fields[21]) * page / 2**20,
        )
        kids.setdefault(int(fields[1]), []).append(pid)
    needle = f"--user-data-dir={profile}"
    root = next(
        (
            pid
            for pid, (comm, *_rest) in stats.items()
            if comm.startswith("chrom")
            and needle in (argv := _argv(pid))
            and not any(a.startswith("--type=") for a in argv)
        ),
        None,
    )
    tree: dict = {"root": root, "procs": [], "gpu": []}
    if root is None:
        return tree
    seen, stack = {root}, [root]
    while stack:
        for child in kids.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    for pid in sorted(seen):
        argv = _argv(pid)
        kind = next((a[7:] for a in argv if a.startswith("--type=")), "browser")
        _comm, _ppid, cpu, rss = stats[pid]
        tree["procs"].append({"pid": pid, "type": kind, "cpu_s": cpu, "rss_mb": rss})
        if pid == root:
            tree["has_switch"] = SWITCH in argv
        if kind == "gpu-process":
            crash = next((a.split("=")[1] for a in argv if a.startswith("--gpu-recent-crash")), "?")
            tree["gpu"].append(
                {
                    "pid": pid,
                    "crash_count": crash,
                    "flags": [a for a in argv if _GPU_FLAG.match(a)],
                }
            )
    return tree


def kill_profile(profile: Path) -> None:
    """SIGKILL whatever is left of the browser on ``profile`` (never anything else)."""
    needle = f"--user-data-dir={profile}"
    for entry in os.scandir("/proc"):
        if entry.name.isdigit() and needle in _argv(int(entry.name)):
            try:
                os.kill(int(entry.name), signal.SIGKILL)
            except OSError:
                pass


def _mask(text: str) -> str:
    """Heap addresses in GL / console messages differ per launch; the message does not."""
    return re.sub(r"0x[0-9a-f]{6,}", "0x*", text)


def parse_gpu(text: str) -> dict:
    """The parts of the chrome://gpu dump that say which GL stack the browser ended up with."""
    sections: dict[str, list[str]] = {}
    current: list[str] = []
    for line in text.splitlines():
        head = re.match(r"^(.+?) ={3,}$", line.strip())
        if head:
            current = sections.setdefault(head.group(1), [])
        else:
            current.append(line.strip())
    # "* Canvas: Software only..." lines under the Graphics Feature Status heading.
    features = {}
    for line in sections.get("Graphics Feature Status", []):
        name, _, status = line.lstrip("* ").partition(": ")
        if status:
            features[name] = status
    driver = dict(
        (k.strip(), v.strip())
        for k, _, v in (ln.partition(" : ") for ln in sections.get("Driver Information", []))
        if v
    )
    log = [ln for ln in sections.get("Log Messages", []) if NOISE not in ln]
    pids = {m.group(1) for ln in log if (m := re.match(r"^\[(\d+):\d+:", ln))}
    return {
        "features": features,
        "problems": [
            ln.lstrip("* ")[:110]
            for ln in sections.get("Problems Detected", [])
            if ln.startswith("* ")
        ],
        "driver": {
            k: driver.get(k, "")[:150]
            for k in ("GPU0", "GL implementation parts", "GL_RENDERER", "Display type")
        },
        "log": [_mask(re.sub(r"^\[[\d:./]+(\w+):", r"[\1:", ln))[:170] for ln in log],
        "log_pids": len(pids),
        "exits": sum("exited" in ln for ln in log),
    }


# --------------------------------------------------------------------------
# PNG: decode with the stdlib, compare pixels
# --------------------------------------------------------------------------


def decode_png(data: bytes) -> tuple[int, int, int, list[bytearray]]:
    """``(width, height, channels, rows)`` of an 8-bit, non-interlaced grey / RGB / RGBA PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, header = 8, [], b""
    while pos < len(data):
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + size]
        if kind == b"IHDR":
            header = body
        elif kind == b"IDAT":
            idat.append(body)
        pos += 12 + size
    width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", header)
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
    if depth != 8 or interlace or channels is None:
        raise ValueError(f"unsupported PNG (depth {depth}, type {ctype}, interlace {interlace})")
    raw, stride = zlib.decompress(b"".join(idat)), width * channels
    rows: list[bytearray] = []
    prev = bytearray(stride)
    for y in range(height):
        base = y * (stride + 1)
        kind, cur = raw[base], bytearray(raw[base + 1 : base + 1 + stride])
        if kind == 1:  # Sub
            for c in range(channels):
                cur[c::channels] = bytes(v & 255 for v in itertools.accumulate(cur[c::channels]))
        elif kind == 2:  # Up
            cur = bytearray((a + b) & 255 for a, b in zip(cur, prev, strict=True))
        elif kind == 3:  # Average
            for i in range(stride):
                left = cur[i - channels] if i >= channels else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        elif kind == 4:  # Paeth
            for i in range(stride):
                a = cur[i - channels] if i >= channels else 0
                b, c = prev[i], (prev[i - channels] if i >= channels else 0)
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                cur[i] = (cur[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif kind != 0:
            raise ValueError(f"bad PNG filter {kind}")
        rows.append(cur)
        prev = cur
    return width, height, channels, rows


MAX_DECODE_MB = 64  # pure Python unfilters ~3 MB of pixels per second: skip what it cannot afford


def _pixel_mb(png: bytes) -> float:
    width, height = struct.unpack(">II", png[16:24])  # IHDR is always the first chunk
    return width * height * 3 / 2**20


def png_diff(a: bytes, b: bytes) -> dict:
    """How two PNGs differ after decoding: differing pixels, largest channel delta, bbox."""
    if (size := max(_pixel_mb(a), _pixel_mb(b))) > MAX_DECODE_MB:
        return {"skipped": f"{size:.0f} MB of pixels"}
    wa, ha, ca, ra = decode_png(a)
    wb, hb, cb, rb = decode_png(b)
    if (wa, ha, ca) != (wb, hb, cb):
        return {"size": f"{wa}x{ha}x{ca} vs {wb}x{hb}x{cb}"}
    count = worst = 0
    box = [wa, ha, -1, -1]
    for y, (p, q) in enumerate(zip(ra, rb, strict=True)):
        if p == q:
            continue
        for x in range(wa):
            u, v = p[x * ca : (x + 1) * ca], q[x * ca : (x + 1) * ca]
            if u != v:
                count += 1
                worst = max(worst, max(abs(s - t) for s, t in zip(u, v, strict=True)))
                box = [min(box[0], x), min(box[1], y), max(box[2], x), max(box[3], y)]
    return {"w": wa, "h": ha, "px": count, "max": worst, "box": box if count else None}


def colors(png: bytes) -> int:
    """Distinct pixel values in ``png`` - 1 means a flat, blank capture."""
    _w, _h, ch, rows = decode_png(png)
    return len({bytes(r[i : i + ch]) for r in rows for i in range(0, len(r), ch)})


# --------------------------------------------------------------------------
# One launch
# --------------------------------------------------------------------------


def ms_since(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


async def shoot(call, take) -> tuple[bytes | None, float, int, str | None]:
    """``capture.take`` around ``call``: (png, ms, attempts, error)."""
    attempts = 0

    async def once() -> bytes:
        nonlocal attempts
        attempts += 1
        return await call()

    t0 = time.perf_counter()
    try:
        return await take(once), ms_since(t0), attempts, None
    except Exception as exc:  # noqa: BLE001 - a failed capture is a result
        return None, ms_since(t0), attempts, f"{type(exc).__name__}: {str(exc)[:100]}"


async def capture_fixture(page, name: str, rec: dict, png_dir: Path, tag: str, take) -> None:
    from lyra_browser.capture import png_size

    plan = [
        ("viewport", lambda: page.screenshot()),
        ("full", lambda: page.screenshot(full_page=True)),
        *((s, page.locator(s).first.screenshot) for s in ELEMENTS[name]),
    ]
    if name in ONLY_FULL:
        plan = plan[1:2]
    for kind, call in plan:
        reps = ONLY_FULL.get(name) or SHOT_REPS["element" if kind.startswith("#") else kind]
        shot: dict = {"ms": [], "sha": [], "error": None}
        png = None
        for i in range(reps):
            png, ms, attempts, error = await shoot(call, take)
            if error:
                shot["error"] = error
                break
            shot["ms"].append(ms)
            if i == 0:
                shot["attempts"], shot["dims_first"] = attempts, png_size(png)
            digest = hashlib.sha256(png).hexdigest()[:12]
            if digest not in shot["sha"]:
                shot["sha"].append(digest)
        if png is not None and not shot["error"]:
            file = png_dir / f"{tag}-{name}-{kind.lstrip('#')}.png"
            file.write_bytes(png)
            shot.update(png=file.name, bytes=len(png), dims=png_size(png))
        rec["shots"][f"{name}:{kind}"] = shot


class PageLog:
    """Console warnings / errors, uncaught exceptions and crashes of one tab."""

    def __init__(self, page) -> None:
        self.errors: list[str] = []
        self.console: list[str] = []
        self.crashes = 0
        page.on("pageerror", lambda e: self.errors.append(str(e)[:140]))
        page.on("crash", lambda *_: setattr(self, "crashes", self.crashes + 1))

        def on_console(msg) -> None:
            if msg.type in ("warning", "error") and NOISE not in msg.text:
                self.console.append(f"{msg.type}: {_mask(msg.text)[:140]}")

        page.on("console", on_console)


async def settle_gpu_page(page) -> str:
    """Open chrome://gpu and wait until its text stops growing."""
    await page.goto("chrome://gpu")
    text, size = "", -1
    for _ in range(40):
        text = await page.evaluate(DEEP_TEXT_JS)
        if "Log Messages" in text and len(text) == size:
            break
        size = len(text)
        await asyncio.sleep(0.4)
    return text


def product_config(args: argparse.Namespace, data_dir: Path, headless: bool):
    """The product's Config; ``--guard cdp`` asks for the launch the CDP guard backend uses."""
    from lyra_browser.config import Config

    extra = {} if args.guard == "route" else {"guard_backend": args.guard}
    return Config(data_dir=data_dir, headless=headless, channel="chrome", **extra)


async def one_launch(pw, mode, variant, rep, base, out, args):
    from lyra_browser.capture import take
    from lyra_browser.session import launch_kwargs

    work = Path(tempfile.mkdtemp(prefix=f"{mode}-{variant}-", dir=out / "profiles"))
    profile = work / "profile"
    config = product_config(args, work, mode == "headless")
    kwargs = launch_kwargs(config, "chrome", None, profile)
    # The product's own ignores stay; the switch is the one thing that differs between cells.
    ignore = [a for a in kwargs.pop("ignore_default_args", None) or [] if a != SWITCH]
    if variant == "without":
        ignore.append(SWITCH)
    if ignore:
        kwargs["ignore_default_args"] = ignore
    kwargs["env"] = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    tag = f"{mode}-{variant}-{rep}"
    rec: dict = {
        "mode": mode,
        "variant": variant,
        "rep": rep,
        "error": None,
        "load": os.getloadavg()[0],
        "shots": {},
        "pages": {},
    }
    t_all = time.perf_counter()
    ctx = xvfb = None
    try:
        if mode == "headful":
            display = args.display
            if not display:
                xvfb, display = start_xvfb(args.screen)
            kwargs["env"]["DISPLAY"] = display
        t0 = time.perf_counter()
        ctx = await pw.chromium.launch_persistent_context(**kwargs)
        rec["launch_ms"] = ms_since(t0)
        first = proc_tree(profile)
        rec["has_switch"] = first.get("has_switch")
        if rec["has_switch"] != (variant == "with"):
            raise RuntimeError(f"argv does not match the cell: has_switch={rec['has_switch']}")
        if args.route:
            # What the guard does to every request: Fetch interception, HTTP cache off.
            await ctx.route("**/*", lambda r: r.continue_())
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        log = PageLog(page)
        png_dir = out / "png"
        settled = first
        for name in FIXTURE_ORDER:
            log.errors, log.console = [], []
            entry = rec["pages"][name] = {}
            try:
                t0 = time.perf_counter()
                await page.goto(f"{base}/{name}.html", wait_until="load")
                await page.wait_for_function("window.__ready === true", timeout=15_000)
                entry["load_ms"] = ms_since(t0)
                entry["state"] = await page.evaluate(
                    "() => ({drawn: window.__drawn ?? null, px: window.__px ?? null,"
                    " glMs: window.__glMs ?? null, mode: window.__mode ?? null,"
                    " lost: window.__lost ?? null})"
                )
                await capture_fixture(page, name, rec, png_dir, tag, take)
            except Exception as exc:  # noqa: BLE001 - recorded, the next fixture still runs
                entry["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
            entry["pageerrors"], entry["console"] = list(log.errors), list(log.console)
            if name == FIXTURE_ORDER[0]:
                settled = proc_tree(profile)  # the GPU process once the first page has painted
        await page.goto(f"{base}/probe.html", wait_until="load")
        rec["facts"] = await page.evaluate(PROBE_JS)
        rec["fp"] = await page.evaluate(fingerprint_js())
        rec["bench"] = await page.evaluate(BENCH_JS)
        rec["crashes"] = log.crashes
        text = await settle_gpu_page(page)
        rec["gpu"] = parse_gpu(text)
        last = proc_tree(profile)
        procs = last["procs"]
        gpu_procs = [p for p in procs if p["type"] == "gpu-process"]
        rec["tree"] = {
            "gpu_pids_stable": [g["pid"] for g in settled["gpu"]]
            == [g["pid"] for g in last["gpu"]],
            "gpu": last["gpu"],
            "procs": len(procs),
            "rss_mb": round(sum(p["rss_mb"] for p in procs), 1),
            "cpu_s": round(sum(p["cpu_s"] for p in procs), 2),
            "gpu_rss_mb": round(sum(p["rss_mb"] for p in gpu_procs), 1),
            "gpu_cpu_s": round(sum(p["cpu_s"] for p in gpu_procs), 2),
        }
    except BaseException as exc:
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:1500]}"
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        if ctx is not None:
            try:
                await asyncio.wait_for(ctx.close(), 20)
            except Exception:  # noqa: BLE001 - killed below
                pass
        kill_profile(profile)
        if xvfb is not None:
            rec["xvfb_exit"] = xvfb.poll()  # None: still up; negative: the signal that ended it
            xvfb.terminate()
            try:
                xvfb.wait(5)
            except subprocess.TimeoutExpired:
                xvfb.kill()
        shutil.rmtree(work, ignore_errors=True)
        rec["wall_s"] = round(time.perf_counter() - t_all, 1)
    return rec


def _x_ready(number: int) -> bool:
    with socket.socket(socket.AF_UNIX) as sock:
        try:
            sock.connect(f"/tmp/.X11-unix/X{number}")
        except OSError:
            return False
    return True


def start_xvfb(screen: str) -> tuple[subprocess.Popen, str]:
    """A private Xvfb on a display number nobody uses: ``(process, ":N")``.

    High random numbers: the low ones belong to real sessions, and to scripts that clean up by
    number. One server per headful launch, so a launch never inherits another's windows.
    """
    for number in random.sample(range(600, 900), 30):
        if any(Path(p).exists() for p in (f"/tmp/.X11-unix/X{number}", f"/tmp/.X{number}-lock")):
            continue
        proc = subprocess.Popen(
            ["Xvfb", f":{number}", "-screen", "0", screen, "-nolisten", "tcp"],
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if _x_ready(number):
                return proc, f":{number}"
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        proc.kill()
        proc.wait()
    raise RuntimeError("no Xvfb could be started")


def provenance() -> dict:
    def git(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
        return done.stdout.strip()

    return {
        "head": git("rev-parse", "--short", "HEAD"),
        "session_py_dirty": bool(git("status", "--porcelain", "--", "src/lyra_browser/session.py")),
    }


async def cmd_run(args: argparse.Namespace) -> None:
    from lyra_browser.session import launch_kwargs, load_driver

    out = Path(args.out)
    for sub in ("png", "profiles"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixtures)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    driver, async_playwright = load_driver("auto")
    sample = launch_kwargs(product_config(args, out, True), "chrome")
    header = {
        "header": True,
        "driver": driver,
        "display": args.display or "a private Xvfb per headful launch",
        "guard": args.guard,
        "route": args.route,
        "chrome": subprocess.run(
            ["google-chrome", "--version"], capture_output=True, text=True
        ).stdout.strip(),
        "launch_kwargs_headless": {k: v for k, v in sample.items() if k != "user_data_dir"},
        **provenance(),
    }
    results = out / "results.jsonl"
    results.write_text(json.dumps(header) + "\n")
    print(json.dumps(header))
    pw = await async_playwright().start()
    try:
        for rep in range(args.reps):
            for index, mode in enumerate(args.modes):
                order = VARIANTS if (rep + index) % 2 == 0 else VARIANTS[::-1]
                for variant in order:
                    for _attempt in range(3):
                        try:
                            rec = await asyncio.wait_for(
                                one_launch(pw, mode, variant, rep, base, out, args),
                                LAUNCH_TIMEOUT_S,
                            )
                        except TimeoutError:
                            rec = {"mode": mode, "variant": variant, "rep": rep, "error": "timeout"}
                        if not (rec["error"] and rec.get("xvfb_exit") is not None):
                            break
                        print(
                            f"  Xvfb ended (exit {rec['xvfb_exit']}) mid-launch: retrying",
                            flush=True,
                        )
                    with results.open("a") as fh:
                        fh.write(json.dumps(rec) + "\n")
                    state = rec["error"] or "ok"
                    print(
                        f"rep {rep} {mode:8} {variant:7} {state} "
                        f"{rec.get('wall_s', '-')}s load {rec.get('load', 0):.1f}",
                        flush=True,
                    )
    finally:
        await pw.stop()
        server.shutdown()
        server.server_close()
        shutil.rmtree(out / "profiles", ignore_errors=True)
    report(out)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def pick(rec: dict, path):
    """``rec`` followed along a dotted path (or a callable's result); None when it is absent."""
    if callable(path):
        try:
            return path(rec)
        except (KeyError, IndexError, TypeError, OSError):
            return None
    for part in path.split("."):
        if not isinstance(rec, dict) or part not in rec:
            return None
        rec = rec[part]
    return rec


def med_range(values: list, digits: int = 0) -> str:
    nums = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if not nums:
        return "-"
    fmt = f"{{:.{digits}f}}"
    low, mid, high = (fmt.format(x) for x in (min(nums), statistics.median(nums), max(nums)))
    return f"{mid} [{low}-{high}]"


def tally(values: list) -> str:
    """``a (4/6), b (2/6)``: the values seen and how often."""
    counts = Counter(v if isinstance(v, str) else json.dumps(v) for v in values)
    if len(counts) == 1:
        return next(iter(counts))
    total = sum(counts.values())
    return ", ".join(f"{k} ({n}/{total})" for k, n in counts.most_common())


def perm_p(a: list[float], b: list[float], limit: int = 20000) -> float | None:
    """Two-sided permutation test on the difference of medians (exact when small)."""
    if len(a) < 2 or len(b) < 2:
        return None
    pool, n = a + b, len(a)
    seen = abs(statistics.median(a) - statistics.median(b))
    if math.comb(len(pool), n) <= limit:
        splits = itertools.combinations(range(len(pool)), n)
    else:
        rng = random.Random(0)
        splits = (rng.sample(range(len(pool)), n) for _ in range(limit))
    hits = count = 0
    for idx in splits:
        chosen = set(idx)
        x = [pool[i] for i in idx]
        y = [pool[i] for i in range(len(pool)) if i not in chosen]
        hits += abs(statistics.median(x) - statistics.median(y)) >= seen - 1e-9
        count += 1
    return hits / count


def compare(a: list[float], b: list[float], digits: int = 0) -> tuple[str, float | None]:
    """``with -> without xRATIO p=P`` over per-launch values; ``*``: p <= .02 and >= 15% shift."""
    if not a or not b:
        return "no data", None
    base = statistics.median(a)
    ratio = statistics.median(b) / base if base else math.nan
    p = perm_p(a, b)
    star = "*" if p is not None and p <= 0.02 and abs(ratio - 1) >= 0.15 else ""
    shown = "-" if p is None else f"{p:.2f}"
    return f"{med_range(a, digits)} -> {med_range(b, digits)} x{ratio:.2f} p={shown}{star}", ratio


def grid(
    title: str, columns: list[str], rows: list[tuple[str, list[str]]], width: int = 34
) -> None:
    """A table; cells wider than ``width`` are cut and listed in full underneath."""
    print(f"\n=== {title} ===")
    label = max(len(name) for name, _ in rows)
    sizes = [
        min(width, max(len(c), *(len(cells[i]) for _, cells in rows)))
        for i, c in enumerate(columns)
    ]

    def line(name: str, cells: list[str]) -> str:
        cut = [c if len(c) <= w else c[: w - 1] + "~" for c, w in zip(cells, sizes, strict=True)]
        return f"{name:<{label}}  " + "  ".join(
            f"{c:<{w}}" for c, w in zip(cut, sizes, strict=True)
        )

    print(line("", columns))
    for name, cells in rows:
        print(line(name, cells))
    for name, cells in rows:
        for column, cell in zip(columns, cells, strict=True):
            if len(cell) > width:
                print(f"  [{name} / {column}] {cell}")


def rows_for(spec: list, cells: dict, keys: list) -> list[tuple[str, list[str]]]:
    """One table row per ``(label, getter, format)``; one cell per ``(mode, variant)`` key."""
    rows = []
    for label, getter, fmt in spec:
        row = []
        for key in keys:
            values = [v for v in (pick(r, getter) for r in cells[key]) if v is not None]
            row.append(fmt(values) if values else "-")
        rows.append((label, row))
    return rows


def one_decimal(values: list) -> str:
    return med_range(values, 1)


GPU_FEATURES = (
    "WebGL",
    "WebGPU",
    "Canvas",
    "Compositing",
    "Rasterization",
    "Multiple Raster Threads",
)
STACK = [
    ("switch in browser argv", "has_switch", tally),
    ("launch ms", "launch_ms", med_range),
    ("first page loaded ms", "pages.dom.load_ms", med_range),
    ("GPU process incarnations", "gpu.log_pids", tally),
    ("GPU exits logged", "gpu.exits", tally),
    ("GPU pid stable after 1st page", "tree.gpu_pids_stable", tally),
    ("GPU procs alive at end", lambda r: len(r["tree"]["gpu"]), tally),
    ("GPU final flags", lambda r: " ".join(r["tree"]["gpu"][-1]["flags"]), tally),
    ("gpu-recent-crash-count", lambda r: r["tree"]["gpu"][-1]["crash_count"], tally),
    ("renderer crashes", "crashes", lambda v: str(sum(v))),
    ("GL implementation", "gpu.driver.GL implementation parts", tally),
    ("GL_RENDERER", "gpu.driver.GL_RENDERER", tally),
    *((f"gpu: {n}", f"gpu.features.{n}", tally) for n in GPU_FEATURES),
    ("gpu: problems", lambda r: "; ".join(p[:30] for p in r["gpu"]["problems"]), tally),
    ("tree RSS MB", "tree.rss_mb", med_range),
    ("tree CPU s", "tree.cpu_s", one_decimal),
    ("GPU proc RSS MB", "tree.gpu_rss_mb", med_range),
    ("GPU proc CPU s", "tree.gpu_cpu_s", one_decimal),
    ("launch wall s", "wall_s", one_decimal),
]


def context_state(which: str):
    def state(r: dict) -> str:
        c = r["facts"][which]
        if c["ok"]:
            return "ok: " + (c.get("unmasked") or "?")[:70]
        return "null: " + (c.get("why") or c.get("ex") or "?")[:70]

    return state


def webgl_spec(out: Path) -> list:
    def unchecked(r: dict) -> str:
        page = r["pages"]["webgl"]
        return f"{page['state']['drawn']} / {page['state']['px']}"

    def colours(r: dict) -> int:
        return colors((out / "png" / r["shots"]["webgl:#gl"]["png"]).read_bytes())

    def joined(page: str, key: str):
        return lambda r: "; ".join(r["pages"][page][key]) or "none"

    return [
        ("typeof WebGL(2)RenderingContext", "facts.iface", tally),
        ("getContext('webgl')", context_state("webgl"), tally),
        ("getContext('webgl2')", context_state("webgl2"), tally),
        ("webgl + failIfMajorPerformanceCaveat", context_state("noCaveat"), tally),
        ("detect: modernizr-style", "facts.detect.modernizr", tally),
        ("detect: webgl2", "facts.detect.webgl2", tally),
        ("detect: hardware only (caveat)", "facts.detect.hardwareOnly", tally),
        ("OffscreenCanvas webgl: main thread", "facts.offscreen", tally),
        ("OffscreenCanvas webgl: worker", "facts.offscreenWorker", tally),
        ("navigator.gpu in window", "facts.navigatorGpu", tally),
        ("navigator.gpu.requestAdapter()", lambda r: str(r["facts"]["webgpuAdapter"]), tally),
        ("canvas 2D works", "facts.canvas2d", tally),
        ("unchecked page: getContext ms", "pages.webgl.state.glMs", med_range),
        ("unchecked page: uncaught", joined("webgl", "pageerrors"), tally),
        ("unchecked page: drawn / centre px", unchecked, tally),
        ("unchecked page: console", joined("webgl", "console"), tally),
        ("unchecked page: canvas capture colours", colours, tally),
        ("fallback page: path taken", "pages.webgl-fallback.state.mode", tally),
        ("fallback page: console", joined("webgl-fallback", "console"), tally),
    ]


def series(rs: list[dict], name: str, cold: bool) -> list[float]:
    """Per-launch capture time of ``name``: the first call (cold) or the median of the repeats."""
    found = []
    for r in rs:
        ms = r["shots"].get(name, {}).get("ms") or []
        if len(ms) > 1:
            found.append(ms[0] if cold else statistics.median(ms[1:]))
    return found


def fmt_diff(d: dict) -> str:
    if "size" in d:
        return f"size {d['size']}"
    if "skipped" in d:
        return f"not decoded ({d['skipped']})"
    return f"{d['px']}px max{d['max']} box{d['box']}"


def pixel_verdict(out: Path, name: str, w: list[dict], wo: list[dict]) -> str:
    """Equal PNG bytes in every launch, or the decoded difference of the unequal pairs."""
    shots = {
        (v, r["rep"]): s
        for v, rs in (("with", w), ("without", wo))
        for r in rs
        if (s := r["shots"].get(name, {})).get("sha")
    }
    if not shots:
        return "no PNG"
    last = {k: s["sha"][-1] for k, s in shots.items()}
    odd = [s for s in shots.values() if len(s["sha"]) > 1]
    note = ""
    if odd:  # the same call, twice in one launch, gave different bytes: say how
        first = Counter(str(s.get("dims_first")) for s in odd)
        note = (
            f" (first call differs from its repeats in {len(odd)}/{len(shots)} launches;"
            f" first: {dict(first)})"
        )
    if len(set(last.values())) == 1:
        return f"identical x{len(last)} {next(iter(shots.values())).get('dims')}{note}"

    def png(key: tuple[str, int]) -> bytes:
        return (out / "png" / shots[key]["png"]).read_bytes()

    reps = sorted({rep for _, rep in last})
    pairs = [
        i
        for i in reps
        if {("with", i), ("without", i)} <= last.keys()
        and last[("with", i)] != last[("without", i)]
    ]
    diffs = [fmt_diff(png_diff(png(("with", i)), png(("without", i)))) for i in pairs[:2]]
    floor = []  # two captures of the same variant that differ: the noise the pairs sit on
    for v in VARIANTS:
        distinct = {sha: key for key, sha in last.items() if key[0] == v}
        if len(distinct) > 1:
            a, b = list(distinct.values())[:2]
            floor.append(f"{v}: {fmt_diff(png_diff(png(a), png(b)))}")
    text = f"DIFFERENT in {len(pairs)}/{len(reps)} pairs: {'; '.join(diffs)}"
    return text + (f" | same-variant noise {floor}" if floor else "") + note


def captures(out: Path, mode: str, w: list[dict], wo: list[dict]) -> None:
    print(f"\n[{mode}] scenario | cold ms with -> without | warm ms with -> without | PNG")
    warm_ratios, stars = [], 0
    for name in w[0]["shots"] if w else []:
        cold, _ = compare(series(w, name, True), series(wo, name, True))
        warm, ratio = compare(series(w, name, False), series(wo, name, False))
        if ratio is not None:
            warm_ratios.append(ratio)
        stars += warm.endswith("*")
        retried = sum(r["shots"].get(name, {}).get("attempts", 1) > 1 for r in w + wo)
        errors = [
            r["shots"][name]["error"] for r in w + wo if r["shots"].get(name, {}).get("error")
        ]
        extra = (f" | first-frame retry x{retried}" if retried else "") + (
            f" | ERRORS {errors[:2]}" if errors else ""
        )
        print(f"  {name:24} | {cold:42} | {warm:42} | {pixel_verdict(out, name, w, wo)}{extra}")
    if warm_ratios:
        print(
            f"  warm without/with over {len(warm_ratios)} scenarios: median "
            f"x{statistics.median(warm_ratios):.2f}, range x{min(warm_ratios):.2f}"
            f"-x{max(warm_ratios):.2f}, flagged * {stars}"
        )


VOLATILE = re.compile(r"(^|\.)(ms|connection|perfMemory)$")  # differs run to run in any cell


def flat(value: object, prefix: str = "", into: dict | None = None) -> dict[str, str]:
    into = {} if into is None else into
    if isinstance(value, dict):
        for k, v in value.items():
            flat(v, f"{prefix}.{k}" if prefix else k, into)
    else:
        into[prefix] = json.dumps(value)
    return into


def page_diffs(mode: str, cells: dict) -> None:
    """Page-readable values (FINGERPRINT_JS + GL facts) whose value sets do not overlap."""
    seen: dict[str, dict[str, set]] = {v: {} for v in VARIANTS}
    for v in VARIANTS:
        for r in cells[(mode, v)]:
            for key, value in flat({"fp": r["fp"], "facts": r["facts"]}).items():
                if not VOLATILE.search(key):
                    seen[v].setdefault(key, set()).add(value)
    keys = sorted(set(seen["with"]) | set(seen["without"]))
    differ = [
        k for k in keys if seen["with"].get(k, set()).isdisjoint(seen["without"].get(k, {"-"}))
    ]
    vary = [k for k in keys if seen["with"].get(k) != seen["without"].get(k) and k not in differ]
    print(f"\n[{mode}] {len(differ)} values differ between with / without; {len(keys)} compared")
    for key in differ:
        print(
            f"  {key}: with={sorted(seen['with'].get(key, ['-']))}"
            f" without={sorted(seen['without'].get(key, ['-']))}"
        )
    if vary:
        print(f"  (vary run to run, overlapping, in at least one cell: {', '.join(vary)})")


def log_lines(cells: dict, keys: list) -> None:
    print(
        "\nchrome://gpu log, by launches that logged the line (fixture readPixels noise removed):"
    )
    for key in keys:
        seen: Counter = Counter()
        for r in cells[key]:
            seen.update(set(r["gpu"]["log"]))
        for line, n in seen.most_common(6):
            print(f"  {key[0]}/{key[1]} x{n}/{len(cells[key])}: {line}")


def load_results(out: Path) -> tuple[dict, list[dict]]:
    lines = [json.loads(ln) for ln in (out / "results.jsonl").read_text().splitlines() if ln]
    return lines[0], lines[1:]


def report(out: Path) -> None:
    header, recs = load_results(out)
    good = [r for r in recs if not r.get("error")]
    modes = [m for m in MODES if any(r["mode"] == m for r in recs)]
    keys = [(m, v) for m in modes for v in VARIANTS]
    cells = {k: [r for r in good if (r["mode"], r["variant"]) == k] for k in keys}
    names = [f"{m}/{v}" for m, v in keys]
    print(
        f"\nchrome {header['chrome']} | driver {header['driver']} | HEAD {header['head']}"
        f" (session.py modified: {header['session_py_dirty']})"
        f" | guard {header.get('guard', 'route')} | pass-through route {header.get('route', False)}"
        f" | {out}"
    )
    print(f"launches per cell: {dict(zip(names, (len(cells[k]) for k in keys), strict=True))}")
    for r in recs:
        if r.get("error"):
            print(f"FAILED {r['mode']}/{r['variant']} rep {r['rep']}: {r['error'][:300]}")
    grid("A. launch and GPU stack", names, rows_for(STACK, cells, keys))
    log_lines(cells, keys)
    print("\n=== B. captures (ms over launches: median [min-max]; p: permutation test) ===")
    for mode in modes:
        captures(out, mode, cells[(mode, "with")], cells[(mode, "without")])
    grid("C. WebGL and the pages that need it", names, rows_for(webgl_spec(out), cells, keys), 38)
    print("\n=== D. page-readable values that differ ===")
    for mode in modes:
        page_diffs(mode, cells)
    rows = []
    for metric in sorted({k for r in good for k in r["bench"]}):
        row = []
        for mode in modes:
            a = [r["bench"][metric] for r in cells[(mode, "with")]]
            b = [r["bench"][metric] for r in cells[(mode, "without")]]
            row.append(compare(a, b, 1)[0])
        rows.append((metric, row))
    grid("E. raster-bound timings in the page (ms, with -> without)", modes, rows, 60)
    print(
        "\nOne launch is one sample. * = p <= .02 and >= 15% shift; with ~100 comparisons a few "
        "stars are chance - look for the same shift in both modes and in cold and warm."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="launch, measure, then print the report")
    run.add_argument("--reps", type=int, default=6, help="launches per cell (default 6)")
    run.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    run.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "swiftshader-probe"))
    run.add_argument("--display", help="use this X display for headful instead of a private Xvfb")
    run.add_argument("--screen", default="1920x1080x24", help="private Xvfb screen")
    run.add_argument(
        "--route",
        action="store_true",
        help="install a pass-through context.route, as the guard does",
    )
    run.add_argument(
        "--guard",
        choices=("route", "cdp"),
        default="route",
        help="Config.guard_backend: cdp also launches with --remote-debugging-port=0",
    )
    rep = sub.add_parser("report", help="print the report of an earlier run")
    rep.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.cmd == "run":
        asyncio.run(cmd_run(args))
    else:
        report(Path(args.out))


if __name__ == "__main__":
    main()
