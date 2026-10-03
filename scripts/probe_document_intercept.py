#!/usr/bin/env python3
"""Probe: can the navigation guard intercept *documents only*?

The guard judges navigations and nothing else (``enforcement.classify`` returns
None for anything that is not a document), yet it is installed with
``context.route("**/*")``, which Playwright turns into ``Fetch.enable`` on every
request *and* ``Network.setCacheDisabled(true)``. DataDome (etsy.com,
tripadvisor.com) refuses a browser launched that way. This script measures whether
intercepting only ``Document`` requests is enough to be let in, what else has to
change with it, and whether the guard still sees what it sees today.

Results and the reasoning are in the project history. Subcommands (each
prints what it measured; nothing is asserted about the verdict):

  measure      Real internet. A matrix of launch variants ("cells") x sites x
               repetitions, one fresh profile per visit, visits shuffled and spaced
               out. Headful needs ``xvfb-run -a -s "-screen 0 1920x1080x24"``. Cells
               that use patchright need a venv that has it. Cell names accept
               modifiers: ``c-doc+rm:swift`` drops a switch group, ``keep:ft``
               restores one (see GROUPS), ``sandbox`` / ``nosandbox``.
  summary      Aggregate one or more ``measure`` result files into a table.
  wire         Loopback. What a server sees per interception mode: request headers
               and whether the HTTP cache still works.
  tls          What an edge sees before any JavaScript: JA4, HTTP/2 fingerprint and
               header order per cell, from an echo service.
  fingerprint  Loopback. What a page can read that differs between launch variants.
  fixture      Loopback. The real ``NavigationGuard`` under each interception design
               (``route`` today, ``doc-page`` per-tab raw CDP, ``doc-side`` a second
               CDP connection with browser-level auto-attach): forms, links,
               redirects, popups, iframes (same-site, cross-origin, cross-site).
  selftest     No browser. The adapters from ``Fetch.requestPaused`` to the guard,
               against the Playwright-shaped fakes the unit tests use.

Cells
-----
  a-*   plain system Chrome over raw CDP, no Playwright (the control)
  b-*   today's launch: BrowserSession + ``context.route`` with the real guard
  n-*   BrowserSession with no interception at all (lower bound for any variant)
  c-*   BrowserSession, no route; ``doc`` per-tab raw ``Fetch.enable`` on Document
        only, ``side`` the same from a second CDP connection (the sidecar)
  d-*   c-* (or the route) with some of Playwright's default switches removed
  e-*   the same through patchright

Results are JSONL under ``testing/doc_intercept_out/`` (gitignored scratch).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_browser_e2e import free_port  # noqa: E402

OUT = REPO / "testing" / "doc_intercept_out"
CHROME = os.environ.get("CHROME", shutil.which("google-chrome") or "/usr/bin/google-chrome")

SITES = {
    "etsy": "https://www.etsy.com/signin",
    "tripadvisor": "https://www.tripadvisor.com/",
    "hyatt": "https://www.hyatt.com/",
}

# Playwright's chromiumSwitches() as shipped in 1.63, verbatim. ``ignore_default_args``
# matches whole argument strings, so the ``--disable-features`` entry has to be the
# exact joined string; ``chrome_switches`` below checks the removal really happened.
_PW_DISABLED_FEATURES = [
    "AvoidUnnecessaryBeforeUnloadCheckSync",
    "DestroyProfileOnBrowserClose",
    "DialMediaRouteProvider",
    "GlobalMediaControls",
    "HttpsUpgrades",
    "LensOverlay",
    "MediaRouter",
    "PaintHolding",
    "ThirdPartyStoragePartitioning",
    "BlockOriginHeaderModificationOnRedirect",
    "Translate",
    "AutoDeElevate",
    "OptimizationHints",
    "msForceBrowserSignIn",
    "msEdgeUpdateLaunchServicesPreferredVersion",
]
PW_SWITCHES = [
    "--disable-field-trial-config",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-back-forward-cache",
    "--disable-breakpad",
    "--disable-client-side-phishing-detection",
    "--disable-component-extensions-with-background-pages",
    "--disable-component-update",
    "--no-default-browser-check",
    "--disable-default-apps",
    "--disable-dev-shm-usage",
    "--disable-edgeupdater",
    "--disable-extensions",
    "--disable-features=" + ",".join(_PW_DISABLED_FEATURES),
    "--enable-features=CDPScreenshotNewSurface",
    "--allow-pre-commit-input",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--disable-updater-scheduler",
    "--force-color-profile=srgb",
    "--metrics-recording-only",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    "--no-service-autorun",
    "--export-tagged-pdf",
    "--disable-search-engine-choice-screen",
    "--unsafely-disable-devtools-self-xss-warnings",
    "--edge-skip-compat-layer-relaunch",
    "--disable-infobars",
    "--disable-sync",
]
# Switches no trimmed cell removes, because removing them measures the host, not the
# cell. None is visible to a page or to a server:
#   --no-first-run, --disable-search-engine-choice-screen
#       browser UI only; without them the launch hangs behind a dialog.
#   --password-store=basic
#       where cookies are encrypted at rest. On this host the OS keyring is locked and
#       a Chrome without the switch never sends its first request (measured: plain
#       Chrome's Page.navigate does not return and the server sees nothing).
_KEEP = ("--no-first-run", "--disable-search-engine-choice-screen", "--password-store=basic")
_AUTOMATION = "--enable-automation"
_SWIFTSHADER = "--enable-unsafe-swiftshader"

GROUPS: dict[str, list[str]] = {
    "ft": ["--disable-field-trial-config"],
    "feat": [a for a in PW_SWITCHES if a.startswith("--disable-features=")],
    "net": [
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-client-side-phishing-detection",
        "--no-default-browser-check",
    ],
    "sched": [
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-ipc-flooding-protection",
        "--disable-hang-monitor",
        "--allow-pre-commit-input",
    ],
    "ux": ["--disable-popup-blocking", "--disable-prompt-on-repost", "--force-color-profile=srgb"],
    "bfcache": ["--disable-back-forward-cache"],
    "ext": [
        "--disable-extensions",
        "--disable-component-extensions-with-background-pages",
        "--disable-default-apps",
    ],
    "misc": [
        "--disable-breakpad",
        "--disable-dev-shm-usage",
        "--disable-edgeupdater",
        "--enable-features=CDPScreenshotNewSurface",
        "--disable-updater-scheduler",
        "--metrics-recording-only",
        "--use-mock-keychain",
        "--no-service-autorun",
        "--export-tagged-pdf",
        "--unsafely-disable-devtools-self-xss-warnings",
        "--edge-skip-compat-layer-relaunch",
        "--disable-infobars",
        "--disable-sync",
    ],
    "swift": [_SWIFTSHADER],
    "auto": [_AUTOMATION],
}
_BARE = [a for a in PW_SWITCHES if a not in _KEEP] + [_SWIFTSHADER]

CLASSIFY_JS = r"""
(() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const html = document.documentElement ? document.documentElement.outerHTML : '';
  const text = document.body ? document.body.innerText : '';
  const t = document.title || '';
  return {
    url: location.href,
    status: nav ? nav.responseStatus : null,
    title: t.slice(0, 120),
    text: text.replace(/\s+/g, ' ').slice(0, 240),
    htmlLen: html.length,
    m: {
      cfChallenge: /Just a moment|Attention Required|Checking your browser|cf-chl-|Verify you are human|cdn-cgi\/challenge-platform|_cf_chl_opt/i.test(t + ' ' + html.slice(0, 20000)) && !document.querySelector('input[type=password]'),
      akamaiDenied: /Access Denied/i.test(t) || /Reference\s*#\d/i.test(text),
      datadome: /captcha-delivery\.com|geo\.captcha-delivery/i.test(html),
      px: /px-captcha|Press (&amp;|&) Hold|perimeterx/i.test(html + text),
      recaptcha: /recaptcha/i.test(html),
      blockedText: /unusual traffic|are you a robot|not a robot|bot detected|automated|blocked|forbidden|denied/i.test(text.slice(0, 2000)),
      passwordField: !!document.querySelector('input[type=password]'),
    },
  };
})()
"""  # noqa: E501


def verdict(state: dict) -> str:
    """Coarse outcome of one visit; the raw state is kept next to it for review.

    Same heuristics as the compat probe, so numbers stay comparable.
    """
    if not state or state.get("error"):
        return "error"
    m = state.get("m", {})
    status = state.get("status") or 0
    if m.get("cfChallenge"):
        return "challenge:cloudflare"
    if m.get("px") and not m.get("passwordField"):
        return "challenge:perimeterx"
    if m.get("datadome") and not m.get("passwordField"):
        return "challenge:datadome"
    if m.get("akamaiDenied"):
        return "blocked:akamai"
    if status in (401, 403, 429, 503):
        return f"blocked:{status}"
    if m.get("blockedText") and not m.get("passwordField") and state.get("htmlLen", 0) < 20000:
        return "blocked:text"
    return "ok"


def short(v: str) -> str:
    """ok / challenge / blocked / error, without the vendor."""
    return v.split(":")[0]


# --------------------------------------------------------------------------
# What a launch looks like from the outside
# --------------------------------------------------------------------------


def chrome_switches(user_data_dir: str) -> list[str]:
    """The command line of the browser process that owns ``user_data_dir``.

    The only way to know that ``ignore_default_args`` removed what it was told to:
    the arguments Playwright passes are not logged, and a cell that silently kept
    a switch it claims to have dropped would measure nothing.
    """
    needle = f"--user-data-dir={user_data_dir}"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in argv if a]
        if needle in args and not any(a.startswith("--type=") for a in args):
            return args[1:]
    return []


def kill_group(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def devtools_endpoint(profile: Path, timeout: float = 10.0) -> str:
    """The browser websocket of a Chrome started with ``--remote-debugging-port=0``.

    Chrome writes the port it picked and the browser's path into
    ``<profile>/DevToolsActivePort`` (two lines).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            port, path = (profile / "DevToolsActivePort").read_text().split("\n")[:2]
            return f"ws://127.0.0.1:{port}{path}"
        except (OSError, ValueError):
            await asyncio.sleep(0.1)
    raise RuntimeError(f"no DevToolsActivePort in {profile}")


def windowed_user_agent() -> str:
    """The UA this Chrome sends with a window, read from a headless launch.

    Headless says ``HeadlessChrome`` and a person's Chrome does not; every
    headless cell (including the control) sends the windowed one, as the real
    session does (``session._fixed_user_agent``).
    """
    port = free_port()
    profile = tempfile.mkdtemp(prefix="probe-ua-")
    proc = subprocess.Popen(
        [CHROME, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        for _ in range(100):
            try:
                raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
                return json.load(raw)["User-Agent"].replace("HeadlessChrome", "Chrome")
            except Exception:  # noqa: BLE001 - still starting
                time.sleep(0.2)
        raise RuntimeError("headless Chrome did not come up")
    finally:
        kill_group(proc)
        shutil.rmtree(profile, ignore_errors=True)


# --------------------------------------------------------------------------
# The control: plain Chrome over raw CDP, no Runtime.enable
# --------------------------------------------------------------------------


class PlainChrome:
    """System Chrome started the way a shortcut starts it, read over raw CDP.

    No Playwright, no ``Runtime.enable``, no extra switches. ``fetch`` turns on
    Fetch interception the way the cells under comparison do ("doc": documents
    only, "all": every request) and continues each paused request untouched.
    """

    def __init__(self, headless: bool, fetch: str = "", user_agent: str | None = None) -> None:
        self.headless = headless
        self.fetch = fetch
        self.user_agent = user_agent
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="probe-plain-")
        self.proc: subprocess.Popen | None = None
        self.documents: list[str] = []
        self._id = 0
        self._pending: dict = {}

    async def __aenter__(self) -> PlainChrome:
        import websockets

        args = [
            CHROME,
            f"--user-data-dir={self.profile}",
            f"--remote-debugging-port={self.port}",
            "--no-first-run",
            "--password-store=basic",  # see _KEEP: without it this host never sends a request
            "--no-default-browser-check",
            "--window-size=1280,800",
        ]
        if self.headless:
            args.append("--headless=new")
        # (the windowed UA, when headless, is set below through CDP: the same
        # Emulation.setUserAgentOverride, with the same UA-CH metadata, Playwright sends)
        args.append("about:blank")
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        for _ in range(100):
            try:
                raw = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=1)
                page = next(p for p in json.load(raw) if p.get("type") == "page")
                self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=2**27)
                break
            except Exception:  # noqa: BLE001 - still starting
                await asyncio.sleep(0.2)
        else:
            raise RuntimeError("plain chrome did not come up")
        self._reader = asyncio.create_task(self._pump())
        if self.user_agent:
            await self.send(
                "Emulation.setUserAgentOverride",
                userAgent=self.user_agent,
                userAgentMetadata={
                    "mobile": False,
                    "model": "",
                    "architecture": "x86",
                    "platform": "Linux",
                    "platformVersion": "",
                },
            )
        if self.fetch == "cache":
            # What Playwright's route adds to Fetch: the HTTP cache is switched off, which
            # puts Cache-Control/Pragma: no-cache on every request, documents included.
            await self.send("Network.enable")
            await self.send("Network.setCacheDisabled", cacheDisabled=True)
        elif self.fetch:
            pattern = {"urlPattern": "*", "requestStage": "Request"}
            if self.fetch == "doc":
                pattern["resourceType"] = "Document"
            await self.send("Fetch.enable", patterns=[pattern])
        return self

    async def _pump(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif msg.get("method") == "Fetch.requestPaused":
                    params = msg["params"]
                    if params.get("resourceType") == "Document":
                        self.documents.append(
                            f"{params['request']['method']} {params['request']['url']}"
                        )
                    asyncio.create_task(
                        self.send("Fetch.continueRequest", requestId=params["requestId"])
                    )
        except Exception:  # noqa: BLE001 - socket closed
            pass

    async def __aexit__(self, *_exc: object) -> None:
        try:
            self._reader.cancel()
            await self.ws.close()
        finally:
            kill_group(self.proc)
            shutil.rmtree(self.profile, ignore_errors=True)

    async def send(self, method: str, **params: object) -> dict:
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        msg = await fut
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg["result"]

    async def goto(self, url: str) -> None:
        # Page.navigate is what the omnibox does: no Page.enable, no Runtime.enable.
        await self.send("Page.navigate", url=url)

    async def evaluate(self, js: str) -> dict | None:
        res = await self.send(
            "Runtime.evaluate", expression=js, awaitPromise=True, returnByValue=True
        )
        return res.get("result", {}).get("value")

    def command_line(self) -> list[str]:
        return chrome_switches(self.profile)


# --------------------------------------------------------------------------
# Document-only interception, the way the guard would use it
# --------------------------------------------------------------------------


class PausedFrame:
    """The slice of a Playwright Frame that ``enforcement`` reads."""

    __slots__ = ("parent_frame", "url")

    def __init__(self, url: str, parent_frame: PausedFrame | None) -> None:
        self.url = url
        self.parent_frame = parent_frame


class PausedRequest:
    """The slice of a Playwright Request that ``enforcement.classify`` reads.

    Built from a ``Fetch.requestPaused`` event. ``frame`` raises for a frame the
    tab's frame tree does not know, exactly as Playwright does for some navigation
    requests, so the guard's fail-closed branch is the one that runs.
    """

    def __init__(self, event: dict, frames: dict[str, PausedFrame]) -> None:
        req = event["request"]
        self.url = req["url"]
        self.method = req["method"]
        # classify() only asks whether a body exists; a large or binary one comes
        # without ``postData`` and is flagged by ``hasPostData``.
        self.post_data = req.get("postData") or ("<body>" if req.get("hasPostData") else None)
        self._document = event.get("resourceType") == "Document"
        self._frame = frames.get(event.get("frameId", ""))

    def is_navigation_request(self) -> bool:
        return self._document

    @property
    def frame(self) -> PausedFrame:
        if self._frame is None:
            raise RuntimeError("Frame not available for this request")
        return self._frame


class PausedRoute:
    """The slice of a Playwright Route that ``NavigationGuard`` calls.

    ``fulfill(status=204)`` is the guard's refusal: the page stays where it is.
    """

    def __init__(self, cdp: object, request_id: str) -> None:
        self._cdp = cdp
        self._id = request_id
        self.resolution = ""

    async def continue_(self) -> None:
        await self._cdp.send("Fetch.continueRequest", {"requestId": self._id})
        self.resolution = "continue"

    async def fulfill(self, *, status: int = 200, **_ignored: object) -> None:
        await self._cdp.send(
            "Fetch.fulfillRequest", {"requestId": self._id, "responseCode": status}
        )
        self.resolution = f"fulfill:{status}"

    async def abort(self, *_a: object, **_k: object) -> None:
        raise AssertionError("the guard answers 204 and never aborts")


class FrameCache:
    """Which frames a session has, and where each points, kept from ``Page.*`` events.

    Renderer-bound commands (``Page.getFrameTree``, ``Runtime.evaluate``) do not
    answer while a *main-frame* navigation request is paused: measured, they sit
    for as long as the request is held. A handler that is holding a paused request
    must therefore not ask the page anything, and reads this instead.
    """

    def __init__(self, *, root_is_subframe: bool = False) -> None:
        self.frames: dict[str, PausedFrame] = {}
        # The root frame of an iframe target (an out-of-process iframe) is a subframe
        # whose parent lives in another session.
        self._root_is_subframe = root_is_subframe

    def _node(self, frame_id: str, parent_id: str | None) -> PausedFrame:
        node = self.frames.get(frame_id)
        if node is not None:
            return node
        parent = None
        if parent_id is not None:
            parent = self.frames.get(parent_id)
            if parent is None:  # the parent is in another process: a stand-in, no origin
                parent = self.frames[parent_id] = PausedFrame("", None)
        elif self._root_is_subframe:
            parent = PausedFrame("", None)
        node = self.frames[frame_id] = PausedFrame("", parent)
        return node

    def load(self, tree: dict) -> None:
        frame = tree["frame"]
        self._node(frame["id"], frame.get("parentId")).url = frame.get("url", "")
        for child in tree.get("childFrames", []):
            self.load(child)

    def attached(self, params: dict) -> None:
        self._node(params["frameId"], params.get("parentFrameId"))

    def navigated(self, params: dict) -> None:
        frame = params["frame"]
        self._node(frame["id"], frame.get("parentId")).url = frame.get("url", "")

    def detached(self, params: dict) -> None:
        self.frames.pop(params["frameId"], None)


class _Interceptor:
    """What the per-tab and the sidecar interceptors share: running the handler."""

    def __init__(self, handler: object) -> None:
        self._handler = handler
        self._tasks: set[asyncio.Task] = set()
        self.errors: list[str] = []
        self.seen: list[dict] = []

    def _spawn(self, coro: object) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _judge(self, cdp: object, cache: FrameCache, event: dict, label: str) -> None:
        route = PausedRoute(cdp, event["requestId"])
        try:
            await self._handler(route, PausedRequest(event, cache.frames))
        except Exception as exc:  # noqa: BLE001 - never leave a request paused
            self.errors.append(f"judge: {type(exc).__name__}: {str(exc)[:80]}")
        finally:
            if not route.resolution:
                # The guard answers every request it is given; reaching here means the
                # adapter or the transport failed first. Same rule as the guard's own
                # _fail_closed: refuse in enforce mode, wave through when only observing.
                try:
                    if getattr(self._handler, "mode", "enforce") == "observe":
                        await route.continue_()
                    else:
                        await route.fulfill(status=204)
                except Exception:  # noqa: BLE001 - target already gone
                    pass
            self.seen.append(
                {
                    "method": event["request"]["method"],
                    "url": event["request"]["url"],
                    "frame": event.get("frameId", "")[:6],
                    "via": label,
                    "resolution": route.resolution,
                }
            )


class DocumentInterceptor(_Interceptor):
    """``Fetch.enable`` on Document requests of each tab, raw CDP, no ``context.route``.

    One CDP session per tab (``context.new_cdp_session``), made for the tabs that
    exist and for each ``page`` event after that. Every paused document request is
    handed to ``handler(route, request)`` -- the same signature, and the very same
    ``NavigationGuard`` instance, as ``context.route`` uses -- through the adapters
    above. The only primitive Playwright's public API offers for this is a session
    made *after* a tab exists, which is the limit measured in ``fixture``.
    """

    def __init__(self, context: object, handler: object) -> None:
        super().__init__(handler)
        self._context = context

    async def start(self) -> None:
        for page in list(self._context.pages):
            await self._attach(page)
        self._context.on("page", lambda page: self._spawn(self._attach_quietly(page)))

    async def _attach_quietly(self, page: object) -> None:
        try:
            await self._attach(page)
        except Exception as exc:  # noqa: BLE001 - a tab that closed before we got there
            self.errors.append(f"attach: {type(exc).__name__}: {str(exc)[:80]}")

    async def _attach(self, page: object) -> None:
        cdp = await self._context.new_cdp_session(page)
        cache = FrameCache()
        cdp.on("Page.frameAttached", cache.attached)
        cdp.on("Page.frameNavigated", cache.navigated)
        cdp.on("Page.frameDetached", cache.detached)
        cdp.on("Fetch.requestPaused", lambda ev: self._spawn(self._judge(cdp, cache, ev, "page")))
        await cdp.send("Page.enable")
        cache.load((await cdp.send("Page.getFrameTree"))["frameTree"])
        await cdp.send(
            "Fetch.enable",
            {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]},
        )


class _SessionHandle:
    """``send`` on one flat session of the sidecar's connection."""

    def __init__(self, side: SidecarInterceptor, session_id: str) -> None:
        self._side = side
        self._sid = session_id

    async def send(self, method: str, params: dict | None = None) -> dict:
        return await self._side.call(method, params, self._sid)


class SidecarInterceptor(_Interceptor):
    """The same interception, from a CDP connection of its own.

    Chrome is started with ``--remote-debugging-port=0`` next to the pipe Playwright
    uses, and this connects to that port. It auto-attaches at browser level with
    ``waitForDebuggerOnStart``, so a popup or an out-of-process iframe is *held*
    until its Fetch interception is in place -- a session made through Playwright
    after the tab exists cannot promise that. Flat sessions (``sessionId`` on every
    message) are what the browser-level auto-attach supports, and what Playwright's
    ``CDPSession.send`` cannot address, hence the second connection.
    """

    def __init__(self, ws_url: str, handler: object) -> None:
        super().__init__(handler)
        self._url = ws_url
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._targets: dict[str, tuple[dict, FrameCache]] = {}
        self.attached: list[tuple] = []
        self._setups: set[asyncio.Task] = set()
        self.closed = False

    async def start(self) -> None:
        import websockets

        self._ws = await websockets.connect(self._url, max_size=2**27)
        self._reader = asyncio.create_task(self._pump())
        await self.call(
            "Target.setAutoAttach",
            {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
                "filter": [{"type": "page"}, {"exclude": True}],
            },
        )
        # The tabs that already exist are attached by events that follow that reply; the
        # first navigation must not start before they are guarded.
        await asyncio.sleep(0.3)
        while self._setups:
            await asyncio.gather(*list(self._setups), return_exceptions=True)

    async def stop(self) -> None:
        self._reader.cancel()
        await self._ws.close()

    async def call(self, method: str, params: dict | None = None, session: str = "") -> dict:
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        message: dict = {"id": mid, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        await self._ws.send(json.dumps(message))
        reply = await fut
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply.get("result", {})

    async def _pump(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                    continue
                method, params, sid = msg.get("method"), msg.get("params", {}), msg.get("sessionId")
                if method == "Target.attachedToTarget":
                    # Registered here, not in the task: events for the session may
                    # follow before the task first runs.
                    info = params["targetInfo"]
                    cache = FrameCache(root_is_subframe=info["type"] == "iframe")
                    self._targets[params["sessionId"]] = (info, cache)
                    setup = asyncio.ensure_future(self._on_attached(params, cache))
                    self._setups.add(setup)
                    setup.add_done_callback(self._setups.discard)
                elif sid in self._targets:
                    cache = self._targets[sid][1]
                    if method == "Page.frameAttached":
                        cache.attached(params)
                    elif method == "Page.frameNavigated":
                        cache.navigated(params)
                    elif method == "Page.frameDetached":
                        cache.detached(params)
                    elif method == "Fetch.requestPaused":
                        self._spawn(self._judge(_SessionHandle(self, sid), cache, params, "side"))
                elif method == "Target.detachedFromTarget":
                    self._targets.pop(params.get("sessionId", ""), None)
        except Exception:  # noqa: BLE001 - socket closed
            pass
        finally:
            self.closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("sidecar connection closed"))

    async def _on_attached(self, params: dict, cache: FrameCache) -> None:
        info, sid = params["targetInfo"], params["sessionId"]
        self.attached.append((info["type"], params["waitingForDebugger"]))
        try:
            # Everything before the resume is answered by the browser process. The page
            # itself is not running yet: a command that needs its renderer (Page.enable,
            # Page.getFrameTree) waits for the resume we are about to give -- measured as
            # popups that never load -- so those come after it.
            await self.call(
                "Fetch.enable",
                {"patterns": [{"resourceType": "Document", "requestStage": "Request"}]},
                sid,
            )
            await self.call(
                "Target.setAutoAttach",
                {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                    "filter": [{"type": "iframe"}, {"exclude": True}],
                },
                sid,
            )
        except Exception as exc:  # noqa: BLE001 - the target went away, or could not be guarded
            self.errors.append(f"attach {info['type']}: {type(exc).__name__}: {str(exc)[:80]}")
            # A target that could not be guarded is not released: it is closed. Resuming it
            # would be the one way to let an unjudged document out.
            try:
                await self.call("Target.closeTarget", {"targetId": info["targetId"]})
            except Exception:  # noqa: BLE001 - gone already
                pass
            return
        if params["waitingForDebugger"]:
            try:
                await self.call("Runtime.runIfWaitingForDebugger", session=sid)
            except Exception:  # noqa: BLE001 - gone already
                return
        # Now the page runs and its frames can be read. A request paused before this lands
        # is judged with an empty cache, i.e. an unknown frame, which the guard treats as an
        # opaque initiator -- the same answer Playwright gives for a popup's first request.
        try:
            await self.call("Page.enable", session=sid)
            cache.load((await self.call("Page.getFrameTree", session=sid))["frameTree"])
        except Exception as exc:  # noqa: BLE001 - closed while loading
            self.errors.append(f"frames {info['type']}: {type(exc).__name__}: {str(exc)[:80]}")


# --------------------------------------------------------------------------
# Cells
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    name: str
    kind: str = "pw"  # "plain" or "pw"
    route: bool = False  # context.route("**/*") with the real guard (observe mode)
    doc: bool = False  # per-tab raw Fetch on Document only, real guard through adapters
    side: bool = False  # browser-level auto-attach from a second CDP connection (sidecar)
    ignore: tuple[str, ...] = ()  # Playwright default switches removed
    sandbox: bool = False  # keep Chrome's sandbox (drop --no-sandbox)
    add_args: tuple[str, ...] = ()
    driver: str = "playwright"
    plain_fetch: str = ""  # kind == "plain": "", "doc" or "all"
    note: str = ""


def _ignore(*names: str) -> tuple[str, ...]:
    out: list[str] = []
    for name in names:
        out.extend(GROUPS[name])
    return tuple(dict.fromkeys(out))


_BARE_KEEP_AUTO = tuple(_BARE)  # automation infobar kept: it is part of the product
_BARE_ALL = (*_BARE, _AUTOMATION)


def build_cells() -> dict[str, Cell]:
    cells = [
        Cell("a-plain", kind="plain", note="control: plain Chrome, raw CDP"),
        Cell("a-plain-doc", kind="plain", plain_fetch="doc", note="plain + Fetch on documents"),
        Cell("a-plain-all", kind="plain", plain_fetch="all", note="plain + Fetch on everything"),
        Cell(
            "a-plain-nocache",
            kind="plain",
            plain_fetch="cache",
            note="plain + Network.setCacheDisabled only (what Playwright's route adds)",
        ),
        Cell("b-route", route=True, note="today: BrowserSession + context.route + guard"),
        Cell("n-noroute", note="BrowserSession, nothing intercepted"),
        Cell("c-doc", doc=True, note="no route; per-tab raw Fetch, documents only"),
        Cell("c-side", side=True, note="no route; sidecar auto-attach, documents only"),
        Cell(
            "d-doc-bare",
            doc=True,
            ignore=_BARE_ALL,
            sandbox=True,
            add_args=_KEEP,
            note="c-doc, every removable default switch dropped (upper bound)",
        ),
        Cell(
            "d-doc-keepauto",
            doc=True,
            ignore=_BARE_KEEP_AUTO,
            sandbox=True,
            add_args=_KEEP,
            note="d-doc-bare but --enable-automation kept (product constraint)",
        ),
        Cell(
            "n-bare",
            ignore=_BARE_KEEP_AUTO,
            sandbox=True,
            add_args=_KEEP,
            note="no interception, every removable switch but --enable-automation dropped",
        ),
        Cell(
            "d-route-bare",
            route=True,
            ignore=_BARE_KEEP_AUTO,
            sandbox=True,
            add_args=_KEEP,
            note="context.route + guard, switches trimmed: is the route alone enough to be refused",
        ),
        Cell("e-patch-route", route=True, driver="patchright", note="patchright + context.route"),
        Cell("e-patch-doc", doc=True, driver="patchright", note="patchright + c-doc"),
        Cell("e-patch-side", side=True, driver="patchright", note="patchright + c-side"),
        Cell(
            "e-patch-doc-bare",
            doc=True,
            driver="patchright",
            ignore=_BARE_KEEP_AUTO,
            sandbox=True,
            add_args=_KEEP,
            note="patchright + d-doc-keepauto",
        ),
    ]
    # Bisection cells: c-doc with ONE group of default switches removed. The route
    # is off, so a pass or a refusal is about the switches alone.
    for group in ("ft", "feat", "net", "sched", "ux", "bfcache", "ext", "misc", "swift"):
        cells.append(
            Cell(
                f"d-doc-rm-{group}",
                doc=True,
                ignore=_ignore(group),
                note=f"c-doc minus group '{group}'",
            )
        )
    cells.append(Cell("d-doc-sandbox", doc=True, sandbox=True, note="c-doc with the sandbox on"))
    return {c.name: c for c in cells}


CELLS = build_cells()


def parse_cell(spec: str) -> Cell:
    """A named cell, or ``base+rm:group+rm:group+sandbox`` derived on the spot.

    ``rm:<group>`` drops a group of Playwright's default switches (see GROUPS),
    ``sandbox`` keeps Chrome's sandbox. This is how the bisection is driven from the
    command line without a new cell per combination.
    """
    base, *mods = spec.split("+")
    if base not in CELLS:
        raise SystemExit(f"unknown cell {base!r}; --list-cells shows them")
    cell = CELLS[base]
    if not mods:
        return cell
    ignore = list(cell.ignore)
    sandbox = cell.sandbox
    for mod in mods:
        if mod == "sandbox":
            sandbox = True
        elif mod == "nosandbox":
            sandbox = False
        elif mod.startswith("rm:") and mod[3:] in GROUPS:
            ignore.extend(GROUPS[mod[3:]])
        elif mod.startswith("keep:") and mod[5:] in GROUPS:
            restored = set(GROUPS[mod[5:]])
            ignore = [a for a in ignore if a not in restored]
        else:
            raise SystemExit(f"unknown cell modifier {mod!r} in {spec!r}")
    return replace(
        cell,
        name=spec,
        ignore=tuple(dict.fromkeys(ignore)),
        sandbox=sandbox,
        note=f"{base} with {', '.join(mods)}",
    )


# --------------------------------------------------------------------------
# Running a cell
# --------------------------------------------------------------------------


class PlaywrightCell:
    """The production session (BrowserSession + ServerContext) with one variation.

    The variation is applied to ``session.launch_kwargs`` (switches removed, sandbox
    kept) and to the interception (route or per-tab Fetch), nothing else, so a
    difference between two cells is a difference in exactly that.
    """

    def __init__(self, cell: Cell, headless: bool, user_agent: str | None) -> None:
        self.cell = cell
        self.headless = headless
        self.user_agent = user_agent
        self.data_dir = Path(tempfile.mkdtemp(prefix="probe-pw-"))
        self.interceptor: DocumentInterceptor | SidecarInterceptor | None = None
        self._restore = None

    async def __aenter__(self) -> PlaywrightCell:
        from lyra_browser import session as sess
        from lyra_browser.approval import CollaborationState
        from lyra_browser.audit import AuditLog
        from lyra_browser.config import Config
        from lyra_browser.context import ServerContext

        cell = self.cell
        cfg = Config(
            data_dir=self.data_dir,
            headless=self.headless,
            channel="chrome",
            driver=cell.driver,
            enforcement_mode="observe",
        )
        cfg.capture_dir = self.data_dir / "captures"
        if self.headless and self.user_agent:
            # Same UA the session would have settled on, without the relaunch.
            (self.data_dir / "browser-ua.json").write_text(json.dumps({"chrome": self.user_agent}))
        self.session = sess.BrowserSession(cfg)
        self.ctx = ServerContext(
            config=cfg,
            session=self.session,
            audit=AuditLog(cfg.audit_path),
            collab=CollaborationState(require_approval=cfg.require_approval),
        )
        if not cell.route:
            self.session.route_handler = None  # ServerContext installs the guard as a route
        original = sess.launch_kwargs

        def patched(*args: object, **kwargs: object) -> dict:
            kw = original(*args, **kwargs)
            if cell.ignore:
                kw["ignore_default_args"] = list(cell.ignore)
            if cell.sandbox:
                kw["chromium_sandbox"] = True
            if cell.add_args:
                kw["args"] = [*kw.get("args", []), *cell.add_args]
            if cell.side:
                # A second transport next to Playwright's pipe; the port is picked by
                # Chrome and written to <profile>/DevToolsActivePort.
                kw["args"] = [*kw.get("args", []), "--remote-debugging-port=0"]
            kw["timeout"] = 45000  # a switch that hangs the launch should fail fast
            return kw

        sess.launch_kwargs = patched
        self._restore = lambda: setattr(sess, "launch_kwargs", original)
        try:
            await self.session.start()
            self.page = await self.session.page()
            if cell.doc:
                self.interceptor = DocumentInterceptor(self.session._context, self.ctx.guard)
                await self.interceptor.start()
            elif cell.side:
                self.interceptor = SidecarInterceptor(
                    await devtools_endpoint(self._profile()), self.ctx.guard
                )
                await self.interceptor.start()
        except BaseException:
            await self._close()
            raise
        return self

    def _profile(self) -> Path:
        return Path(self.session._profile_dir or self.ctx.config.profile_dir)

    def command_line(self) -> list[str]:
        return chrome_switches(str(self._profile()))

    async def goto(self, url: str) -> None:
        try:
            await self.page.goto(url, wait_until="commit", timeout=30000)
        except Exception as exc:  # noqa: BLE001 - recorded by the caller through the page state
            print(f"    goto: {type(exc).__name__}: {str(exc)[:100]}", flush=True)

    async def evaluate(self, js: str) -> dict | None:
        return await self.page.evaluate(js)

    @property
    def documents(self) -> list[str]:
        if self.interceptor is None:
            return []
        return [f"{s['method']} {s['url']} -> {s['resolution']}" for s in self.interceptor.seen]

    async def _close(self) -> None:
        try:
            if isinstance(self.interceptor, SidecarInterceptor):
                await self.interceptor.stop()
        except Exception:  # noqa: BLE001 - already closed
            pass
        try:
            await self.session.stop()
        finally:
            if self._restore:
                self._restore()
            shutil.rmtree(self.data_dir, ignore_errors=True)

    async def __aexit__(self, *_exc: object) -> None:
        await self._close()


def make_browser(cell: Cell, headless: bool, user_agent: str | None):
    if cell.kind == "plain":
        return PlainChrome(headless, cell.plain_fetch, user_agent if headless else None)
    return PlaywrightCell(cell, headless, user_agent)


async def settle(browser: object, seconds: float = 15.0) -> dict:
    """Poll until a challenge resolves or time runs out; return the last state."""
    deadline = time.monotonic() + seconds
    state: dict = {}
    await asyncio.sleep(4)
    while True:
        try:
            state = await browser.evaluate(CLASSIFY_JS) or {}
        except Exception as exc:  # noqa: BLE001 - mid-navigation
            state = {"error": str(exc)[:120]}
        if time.monotonic() >= deadline or (verdict(state) == "ok"):
            return state
        await asyncio.sleep(2.5)


async def visit(cell: Cell, site: str, headless: bool, user_agent: str | None) -> dict:
    started = time.monotonic()
    row: dict = {"cell": cell.name, "site": site, "mode": "headless" if headless else "headful"}
    try:
        async with make_browser(cell, headless, user_agent) as browser:
            await browser.goto(SITES[site])
            state = await settle(browser)
            row.update(
                verdict=verdict(state),
                status=state.get("status"),
                title=state.get("title"),
                final=state.get("url"),
                flags=[k for k, v in state.get("m", {}).items() if v],
                text=(state.get("text") or "")[:100],
                documents=browser.documents,
                error=state.get("error"),
            )
            if cell.ignore or cell.sandbox:
                row["switches_ok"] = switches_applied(cell, browser.command_line())
    except Exception as exc:  # noqa: BLE001 - a failed launch is a result, not a crash
        row.update(verdict="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
    row["seconds"] = round(time.monotonic() - started, 1)
    return row


def switches_applied(cell: Cell, cmdline: list[str]) -> bool:
    """Whether the browser really ran without what the cell says it dropped."""
    if not cmdline:
        return False
    still = [a for a in cell.ignore if a in cmdline]
    sandbox_ok = (not cell.sandbox) or "--no-sandbox" not in cmdline
    return not still and sandbox_ok


# --------------------------------------------------------------------------
# measure / summary
# --------------------------------------------------------------------------


def resolve_cells(spec: str) -> list[Cell]:
    return [parse_cell(s.strip()) for s in spec.split(",") if s.strip()]


async def cmd_measure(args: argparse.Namespace) -> None:
    headless = args.mode == "headless"
    cells = resolve_cells(args.cells)
    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    for site in sites:
        if site not in SITES:
            raise SystemExit(f"unknown site {site!r}; known: {', '.join(SITES)}")
    ua = windowed_user_agent() if headless else None
    out = Path(args.out) if args.out else OUT / f"measure-{args.mode}-{int(time.time())}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    print(f"# {len(cells)} cells x {len(sites)} sites x {args.reps} reps -> {out}", flush=True)
    first = True
    for rep in range(1, args.reps + 1):
        order = [(site, cell) for site in sites for cell in cells]
        rng.shuffle(order)  # no cell is always first, so none is always the one that is warm
        for site, cell in order:
            if not first:
                await asyncio.sleep(args.gap)
            first = False
            try:
                row = await asyncio.wait_for(visit(cell, site, headless, ua), timeout=args.timeout)
            except TimeoutError:
                row = {"cell": cell.name, "site": site, "verdict": "error", "error": "timeout"}
            row.update(rep=rep, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
            with out.open("a") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"{rep} {site:12} {cell.name:18} {row.get('verdict'):22} "
                f"{row.get('status')} {row.get('title') or row.get('error') or ''}"[:150],
                flush=True,
            )
    summarize([out])


def load_rows(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        for line in path.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def summarize(paths: list[Path]) -> None:
    rows = load_rows(paths)
    cells: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        key = (row.get("mode", "?"), row["site"], row["cell"])
        cells[key][short(row.get("verdict", "error"))] += 1
        cells[key]["n"] += 1
    print(f"\n{'mode':9} {'site':12} {'cell':20} ok/n  detail")
    for (mode, site, cell), tally in sorted(cells.items()):
        detail = ", ".join(f"{k}={v}" for k, v in sorted(tally.items()) if k not in ("n", "ok"))
        print(f"{mode:9} {site:12} {cell:20} {tally['ok']}/{tally['n']}  {detail}")


# --------------------------------------------------------------------------
# fixture: the real NavigationGuard, fed by each interception design
# --------------------------------------------------------------------------


class World:
    """Three loopback origins and a log of what reached them.

    SITE and PEER share a host (one site to Chrome, two origins to the guard); AWAY is
    another host, so Chrome puts its frames in another process (an OOPIF). A request
    that reaches a server is the ground truth for "the guard did not stop it".
    """

    def __init__(self) -> None:
        ports = [free_port() for _ in range(3)]
        self.site = f"http://127.0.0.1:{ports[0]}"
        self.peer = f"http://127.0.0.1:{ports[1]}"
        self.away = f"http://localhost:{ports[2]}"
        self.hits: list[str] = []
        self.pages: dict[str, str] = {}
        self._servers = [
            self._serve("SITE", ports[0]),
            self._serve("PEER", ports[1]),
            self._serve("AWAY", ports[2]),
        ]

    def _expand(self, html: str) -> str:
        return (
            html.replace("{SITE}", self.site)
            .replace("{PEER}", self.peer)
            .replace("{AWAY}", self.away)
        )

    def _serve(self, label: str, port: int) -> ThreadingHTTPServer:
        world = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, body: str, code: int = 200, headers: dict | None = None) -> None:
                raw = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path != "/favicon.ico":
                    world.hits.append(f"{label} GET {path}")
                if path.startswith("/s/") and label == "SITE":
                    self._send(world._expand(world.pages.get(path[3:], "<title>?</title>")))
                elif path in ("/framepost", "/framepost-late"):
                    delay = (
                        "setTimeout(()=>f.submit(),700)" if path.endswith("late") else "f.submit()"
                    )
                    self._send(
                        "<!doctype html><title>frame</title><form id=g method=post "
                        'action="/collect"><input name=leak value=x></form>'
                        f"<script>const f=document.getElementById('g');{delay}</script>"
                    )
                elif path == "/redir":
                    self._send("", 302, {"Location": f"{world.away}/dest"})
                else:
                    self._send(f"<!doctype html><title>{path}</title>{label} {path}")

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                world.hits.append(f"{label} POST {urlparse(self.path).path}")
                self._send(f"<!doctype html><title>posted</title>{label} posted")

            def log_message(self, *_args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def close(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()


@dataclass
class Scenario:
    name: str
    html: str
    click: str = ""  # selector clicked after load; empty: the page acts on its own
    clicks: int = 1
    wait: float = 1.0  # seconds for the effects to land
    forbidden: tuple[str, ...] = ()  # hits that mean the guard let something through
    required: tuple[str, ...] = ()  # hits that mean the guard blocked something it should not
    grant_submit: bool = False  # one single-use SUBMIT grant on SITE before the clicks
    exactly: tuple[tuple[str, int], ...] = ()  # (hit, how many times it must have arrived)
    race: bool = False  # popups: repeated, because the interesting thing is a rate


def _form(action: str, target: str = "") -> str:
    extra = f" target={target}" if target else ""
    return (
        f'<form method=post action="{action}"{extra}><input name=x value=1>'
        "<button id=go>go</button></form>"
    )


def build_scenarios() -> list[Scenario]:
    popup = "window.open('{AWAY}/dest')"
    return [
        Scenario("link-same", '<a id=go href="/dest">go</a>', "#go", required=("SITE GET /dest",)),
        Scenario(
            "link-away", '<a id=go href="{AWAY}/dest">go</a>', "#go", forbidden=("AWAY GET /dest",)
        ),
        Scenario(
            "script-nav-away",
            "<button id=go onclick=\"location.href='{AWAY}/dest'\">go</button>",
            "#go",
            forbidden=("AWAY GET /dest",),
        ),
        Scenario("form-post-same", _form("/collect"), "#go", forbidden=("SITE POST /collect",)),
        Scenario(
            "form-post-away", _form("{AWAY}/collect"), "#go", forbidden=("AWAY POST /collect",)
        ),
        Scenario(
            "form-submit-js",
            '<form id=f method=post action="{AWAY}/collect"><input name=x value=1></form>'
            "<button id=go onclick=\"document.getElementById('f').submit()\">go</button>",
            "#go",
            forbidden=("AWAY POST /collect",),
        ),
        Scenario(
            "form-get-away",
            '<form method=get action="{AWAY}/dest"><input name=q value=1>'
            "<button id=go>go</button></form>",
            "#go",
            forbidden=("AWAY GET /dest",),
        ),
        Scenario(
            "redirect-away",
            '<a id=go href="/redir">go</a>',
            "#go",
            required=("SITE GET /redir",),
            forbidden=("AWAY GET /dest",),
        ),
        Scenario(
            "meta-refresh-away",
            '<meta http-equiv=refresh content="0;url={AWAY}/dest">hi',
            forbidden=("AWAY GET /dest",),
        ),
        Scenario(
            "popup-open-away",
            f'<button id=go onclick="{popup}">go</button>',
            "#go",
            forbidden=("AWAY GET /dest",),
            race=True,
        ),
        Scenario(
            "popup-anchor-away",
            '<a id=go target=_blank href="{AWAY}/dest">go</a>',
            "#go",
            forbidden=("AWAY GET /dest",),
            race=True,
        ),
        Scenario(
            "popup-post-away",
            _form("{AWAY}/collect", "_blank"),
            "#go",
            forbidden=("AWAY POST /collect",),
            race=True,
        ),
        Scenario(
            "popup-post-same",
            _form("/collect", "_blank"),
            "#go",
            forbidden=("SITE POST /collect",),
            race=True,
        ),
        Scenario(
            "popup-two-step",
            "<button id=go onclick=\"var w=window.open('about:blank');"
            "w.location='{AWAY}/dest'\">go</button>",
            "#go",
            forbidden=("AWAY GET /dest",),
            race=True,
        ),
        # Positive controls: a popup the grants cover must still open. Without them a popup
        # that never loads would read as "refused".
        Scenario(
            "popup-anchor-ok",
            '<a id=go target=_blank href="/dest">go</a>',
            "#go",
            required=("SITE GET /dest",),
            race=True,
        ),
        Scenario(
            "popup-open-ok",
            "<button id=go onclick=\"window.open('/dest')\">go</button>",
            "#go",
            required=("SITE GET /dest",),
            race=True,
        ),
        Scenario(
            "popup-post-granted",
            _form("/collect", "_blank"),
            "#go",
            grant_submit=True,
            required=("SITE POST /collect",),
            race=True,
        ),
        Scenario(
            "iframe-get-away",
            '<iframe src="{AWAY}/frame"></iframe>',
            required=("AWAY GET /frame",),
        ),
        Scenario(
            "iframe-get-same", '<iframe src="/frame"></iframe>', required=("SITE GET /frame",)
        ),
        Scenario(
            "iframe-post-same",
            '<iframe src="/framepost"></iframe>',
            forbidden=("SITE POST /collect",),
        ),
        Scenario(
            "iframe-post-peer",
            '<iframe src="{PEER}/framepost"></iframe>',
            forbidden=("PEER POST /collect",),
        ),
        Scenario(
            "iframe-post-away",
            '<iframe src="{AWAY}/framepost"></iframe>',
            forbidden=("AWAY POST /collect",),
        ),
        Scenario(
            "iframe-post-away-late",
            '<iframe src="{AWAY}/framepost-late"></iframe>',
            wait=1.6,
            forbidden=("AWAY POST /collect",),
        ),
        Scenario(
            "xhr-away",
            "<button id=go onclick=\"fetch('{AWAY}/api',"
            "{method:'POST',mode:'no-cors',body:'x'})\">go</button>",
            "#go",
            required=("AWAY POST /api",),  # the blind spot: reaches the server under every design
        ),
        Scenario(
            "submit-grant-once",
            '<iframe name=sink></iframe><form method=post action="/collect" target=sink>'
            "<input name=x value=1><button id=go>go</button></form>",
            "#go",
            clicks=2,
            grant_submit=True,
            exactly=(("SITE POST /collect", 1),),
        ),
    ]


class FixtureBrowser:
    """A persistent Chrome with the production launch options and one interception design.

    ``none``      nothing intercepted (checks that every scenario really does escape)
    ``route``     ``context.route("**/*")`` with the guard -- today
    ``doc-page``  per-tab raw Fetch on Document, the guard through the adapters
    ``doc-side``  the same from a second CDP connection with browser-level auto-attach
    """

    def __init__(self, design: str, headless: bool, driver: str, user_agent: str | None) -> None:
        self.design = design
        self.headless = headless
        self.driver = driver
        self.user_agent = user_agent
        self.tmp = Path(tempfile.mkdtemp(prefix="probe-fx-"))
        self.interceptor: DocumentInterceptor | SidecarInterceptor | None = None

    async def __aenter__(self) -> FixtureBrowser:
        from lyra_browser.audit import AuditLog
        from lyra_browser.config import Config
        from lyra_browser.enforcement import NavigationGuard
        from lyra_browser.permission import PermissionStore
        from lyra_browser.session import launch_kwargs, load_driver

        cfg = Config(
            data_dir=self.tmp, headless=self.headless, channel="chrome", enforcement_mode="enforce"
        )
        cfg.ensure_dirs()
        self.cfg = cfg
        self.perms = PermissionStore()
        self.guard = NavigationGuard(cfg, self.perms, AuditLog(cfg.audit_path), mode="enforce")
        _name, async_playwright = load_driver(self.driver)
        self.pw = await async_playwright().start()
        kw = launch_kwargs(cfg, "chrome", self.user_agent)
        if self.design == "doc-side":
            kw["args"] = [*kw["args"], "--remote-debugging-port=0"]
        self.ctx = await self.pw.chromium.launch_persistent_context(**kw)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        if self.design == "route":
            await self.ctx.route("**/*", self.guard)
        elif self.design == "doc-page":
            self.interceptor = DocumentInterceptor(self.ctx, self.guard)
            await self.interceptor.start()
        elif self.design == "doc-side":
            self.interceptor = SidecarInterceptor(
                await devtools_endpoint(cfg.profile_dir), self.guard
            )
            await self.interceptor.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        try:
            if isinstance(self.interceptor, SidecarInterceptor):
                await self.interceptor.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.ctx.close()
            await self.pw.stop()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def grant(self, origin_url: str, capability: str) -> None:
        from lyra_browser.origin import parse_origin
        from lyra_browser.permission import Capability

        self.perms.grant("default", parse_origin(origin_url), Capability(capability))

    def audit_rows(self, start: int) -> list[dict]:
        try:
            lines = self.cfg.audit_path.read_text().splitlines()
        except OSError:
            return []
        rows = [json.loads(line) for line in lines[start:] if line.strip()]
        return [r for r in rows if r.get("tool") == "navigation"]

    def audit_len(self) -> int:
        try:
            return len(self.cfg.audit_path.read_text().splitlines())
        except OSError:
            return 0

    async def reset(self) -> None:
        for page in list(self.ctx.pages):
            if page is not self.page:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            await self.page.goto("about:blank")
        except Exception:  # noqa: BLE001
            pass

    async def run(self, world: World, sc: Scenario) -> dict:
        mark, audit_mark, refusals = len(world.hits), self.audit_len(), self.guard.refusals
        # Every scenario starts from the same grants. A single-use grant that an earlier
        # scenario bought and a design never spent (a popup's POST it never saw) would
        # otherwise pay for a later, unrelated submission.
        self.perms.revoke_all("default")
        self.grant(world.site, "navigate")
        if sc.grant_submit:
            self.grant(world.site, "submit")
        error = ""
        try:
            await self.page.goto(f"{world.site}/s/{sc.name}", wait_until="commit", timeout=15000)
            await asyncio.sleep(0.5)
            if sc.click:
                for _ in range(sc.clicks):
                    await self.page.click(sc.click, no_wait_after=True, timeout=5000)
                    await asyncio.sleep(0.4)
        except Exception as exc:  # noqa: BLE001 - a refused navigation may surface here
            error = f"{type(exc).__name__}: {str(exc)[:80]}"
        await asyncio.sleep(sc.wait)
        hits = world.hits[mark:]
        rows = self.audit_rows(audit_mark)
        signature = sorted(
            (
                r["args"].get("capability"),
                r["args"].get("method"),
                r["status"],
                r.get("origin"),
                r.get("initiator"),
            )
            for r in rows
        )
        try:
            alive = bool(
                await asyncio.wait_for(self.page.evaluate("document.title !== undefined"), 5)
            )
        except Exception:  # noqa: BLE001
            alive = False
        seen = len(self.interceptor.seen) if self.interceptor else 0
        result = {
            "hits": hits,
            "escaped": [h for h in sc.forbidden if h in hits],
            "missing": [h for h in sc.required if h not in hits],
            "miscount": [h for h, n in sc.exactly if hits.count(h) != n],
            "refusals": self.guard.refusals - refusals,
            "audit": signature,
            "alive": alive,
            "error": error,
            "paused": seen,
        }
        await self.reset()
        return result


def judge(runs: list[dict], sc: Scenario, design: str) -> str:
    """One table cell: did the design stop what must be stopped, and pass what must pass."""
    n = len(runs)
    escaped = sum(1 for r in runs if r["escaped"])
    bad = sum(1 for r in runs if r["missing"] or r["miscount"])
    dead = sum(1 for r in runs if not r["alive"])
    if design == "none":
        if sc.forbidden:
            reached = sum(1 for r in runs if len(r["escaped"]) == len(sc.forbidden))
            return "reached" if reached == n else f"NOT-REACHED {n - reached}/{n} (fixture)"
        # Without a guard both clicks of submit-grant-once land; only a missing hit is broken.
        return "passes" if not any(r["missing"] for r in runs) else "FIXTURE-BROKEN"
    text = "ok"
    if escaped:
        text = f"ESCAPE {escaped}/{n}"
    elif bad:
        text = f"OVER-BLOCK {bad}/{n}"
    elif n > 1:
        text = f"ok 0/{n} escaped"
    return text + (f" DEAD-PAGE {dead}" if dead else "")


async def cmd_fixture(args: argparse.Namespace) -> None:
    pick = (args.headless_only, args.headful_only)
    modes = [
        m for m, only in zip(("headless", "headful"), pick, strict=True) if only or not any(pick)
    ]
    designs = [d.strip() for d in args.designs.split(",") if d.strip()]
    all_scenarios = build_scenarios()
    wanted = {s.strip() for s in args.scenarios.split(",") if s.strip()}
    scenarios = [s for s in all_scenarios if not wanted or s.name in wanted]
    for mode in modes:
        headless = mode == "headless"
        ua = windowed_user_agent() if headless else None
        world = World()
        for sc in all_scenarios:
            world.pages[sc.name] = sc.html
        table: dict[str, dict[str, list[dict]]] = {sc.name: {} for sc in scenarios}
        extra: dict[str, dict] = {}
        try:
            for design in designs:
                print(f"\n[{mode}] design={design}", flush=True)
                async with FixtureBrowser(design, headless, args.driver, ua) as fx:
                    fx.grant(world.site, "navigate")
                    for sc in scenarios:
                        runs = []
                        for _ in range(args.races if sc.race else 1):
                            runs.append(await fx.run(world, sc))
                        table[sc.name][design] = runs
                        print(f"  {sc.name:24} {judge(runs, sc, design)}", flush=True)
                    extra[design] = await fixture_extras(fx, world, design)
        finally:
            world.close()
        print_fixture_report(mode, designs, scenarios, table, extra)


async def fixture_extras(fx: FixtureBrowser, world: World, design: str) -> dict:
    """Checks that are not a click and a count: what a tool sees, and what happens on loss."""
    out: dict = {}
    # A tool's own goto to somewhere ungranted: the error text refused_by_guard() keys on.
    from lyra_browser.enforcement import refused_by_guard

    before = fx.guard.refusals
    start_url = fx.page.url
    try:
        await fx.page.goto(f"{world.away}/dest", wait_until="commit", timeout=8000)
        out["goto_refused"] = "NOT REFUSED"
    except Exception as exc:  # noqa: BLE001
        out["goto_refused"] = (
            f"refused_by_guard={refused_by_guard(exc)} refusals+={fx.guard.refusals - before} "
            f"url_unchanged={fx.page.url == start_url}"
        )
    await fx.reset()
    # The same goto once the destination is granted: the allowed path must still work.
    fx.grant(world.away, "navigate")
    mark = len(world.hits)
    try:
        await fx.page.goto(f"{world.away}/dest", wait_until="commit", timeout=8000)
        out["goto_granted"] = f"url={fx.page.url.split('/')[-1]} hits={world.hits[mark:]}"
    except Exception as exc:  # noqa: BLE001
        out["goto_granted"] = f"FAILED {type(exc).__name__}: {str(exc)[:60]}"
    await fx.reset()
    # A granted popup POST, then what is left of the single-use grant it bought.
    from lyra_browser.permission import Capability

    granted = next(s for s in build_scenarios() if s.name == "popup-post-granted")
    world.pages[granted.name] = granted.html
    await fx.run(world, granted)
    out["unspent_submit_grants_after_a_granted_popup_post"] = sum(
        1
        for g in fx.perms.live_grants("default")
        if g.capability is Capability.SUBMIT and (g.uses_left or 0) > 0
    )
    fx.perms.revoke_all("default")
    fx.grant(world.site, "navigate")
    if design == "doc-side" and isinstance(fx.interceptor, SidecarInterceptor):
        # What losing the sidecar does: the browser keeps running, unguarded.
        await fx.interceptor.stop()
        await asyncio.sleep(0.3)
        sc = Scenario("loss", _form("{AWAY}/collect"), "#go", forbidden=("AWAY POST /collect",))
        world.pages["loss"] = sc.html
        res = await fx.run(world, sc)
        out["after_sidecar_loss"] = (
            "FAIL-OPEN (POST reached the server)" if res["escaped"] else "still refused"
        )
    if fx.interceptor is not None:
        out["errors"] = fx.interceptor.errors[:3]
    return out


def print_fixture_report(
    mode: str, designs: list[str], scenarios: list[Scenario], table: dict, extra: dict
) -> None:
    width = 24
    print(f"\n=== fixture, {mode}: guard in enforce mode, NAVIGATE granted on SITE only ===")
    print(f"{'scenario':{width}}" + "".join(f"{d:26}" for d in designs))
    for sc in scenarios:
        cells = [judge(table[sc.name][d], sc, d) for d in designs]
        print(f"{sc.name:{width}}" + "".join(f"{c:26}" for c in cells))
    if "route" in designs:
        print("\naudit parity with route (same capability/method/decision/origin/initiator):")
        for design in designs:
            if design in ("route", "none"):
                continue
            diffs = []
            for sc in scenarios:
                ref = [r["audit"] for r in table[sc.name]["route"]]
                got = [r["audit"] for r in table[sc.name][design]]
                if ref != got:
                    diffs.append(sc.name)
            print(f"  {design:10} differs on: {', '.join(diffs) if diffs else '(nothing)'}")
    print("\nextras:")
    for design, data in extra.items():
        for key, value in data.items():
            print(f"  {design:10} {key}: {value}")


# --------------------------------------------------------------------------
# wire: what a server sees, per interception mode
# --------------------------------------------------------------------------


class WireServer:
    """One origin that logs every request's headers, in the order they arrived."""

    PAGE = (
        '<!doctype html><title>wire</title><link rel=stylesheet href="/a.css">'
        '<script src="/a.js"></script><img src="/a.png"><script>fetch("/api")</script>'
    )

    def __init__(self) -> None:
        self.requests: list[tuple[str, list[tuple[str, str]]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                outer.requests.append((path, list(self.headers.items())))
                cacheable = {"Cache-Control": "max-age=3600"}
                kinds = {
                    "/": ("text/html", self_page, {}),
                    "/a.css": ("text/css", "body{}", cacheable),
                    "/a.js": ("application/javascript", "1", cacheable),
                    "/a.png": ("image/png", "\x89PNG", cacheable),
                }
                ctype, body, extra = kinds.get(path, ("text/plain", "ok", {}))
                raw = body.encode("latin-1", "replace")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args: object) -> None:
                pass

        self_page = self.PAGE
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


async def wire_visit(mode: str, headless: bool, ua: str | None, driver: str) -> dict:
    from lyra_browser.audit import AuditLog
    from lyra_browser.config import Config
    from lyra_browser.enforcement import NavigationGuard
    from lyra_browser.permission import PermissionStore
    from lyra_browser.session import launch_kwargs, load_driver

    server = WireServer()
    tmp = Path(tempfile.mkdtemp(prefix="probe-wire-"))
    cfg = Config(data_dir=tmp, headless=headless, channel="chrome", enforcement_mode="observe")
    cfg.ensure_dirs()
    guard = NavigationGuard(cfg, PermissionStore(), AuditLog(cfg.audit_path), mode="observe")
    _name, async_playwright = load_driver(driver)
    pw = await async_playwright().start()
    kw = launch_kwargs(cfg, "chrome", ua)
    if mode == "doc-side":
        kw["args"] = [*kw["args"], "--remote-debugging-port=0"]
    ctx = await pw.chromium.launch_persistent_context(**kw)
    side = None
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        if mode == "route":
            await ctx.route("**/*", guard)
        elif mode == "doc-page":
            await DocumentInterceptor(ctx, guard).start()
        elif mode == "doc-side":
            side = SidecarInterceptor(await devtools_endpoint(cfg.profile_dir), guard)
            await side.start()
        elif mode in ("fetch-all", "cache-off"):
            cdp = await ctx.new_cdp_session(page)
            if mode == "fetch-all":
                cdp.on(
                    "Fetch.requestPaused",
                    lambda ev: asyncio.ensure_future(
                        cdp.send("Fetch.continueRequest", {"requestId": ev["requestId"]})
                    ),
                )
                await cdp.send(
                    "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
                )
            else:
                await cdp.send("Network.enable")
                await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
        for _ in range(2):  # the second visit shows whether the HTTP cache still works
            await page.goto(server.url + "/", wait_until="load")
            await asyncio.sleep(0.6)
        return {"requests": list(server.requests)}
    finally:
        if side is not None:
            await side.stop()
        await ctx.close()
        await pw.stop()
        server.close()
        shutil.rmtree(tmp, ignore_errors=True)


async def cmd_wire(args: argparse.Namespace) -> None:
    headless = args.mode == "headless"
    ua = windowed_user_agent() if headless else None
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    results = {mode: await wire_visit(mode, headless, ua, args.driver) for mode in modes}
    base = results[modes[0]]["requests"]

    def first(reqs: list, path: str) -> list[str]:
        return next(([k.lower() for k, _ in h] for p, h in reqs if p == path), [])

    def count(reqs: list, path: str) -> int:
        return sum(1 for p, _ in reqs if p == path)

    print(f"\n=== wire, {args.mode}: what the server saw (page loaded twice) ===")
    print(
        f"{'mode':11} {'doc headers == ' + modes[0]:22} {'cache-control/pragma on doc':28} "
        f"{'a.js fetched (cacheable)':26} {'a.png fetched':14}"
    )
    for mode in modes:
        reqs = results[mode]["requests"]
        doc = first(reqs, "/")
        same = (
            "yes"
            if doc == first(base, "/")
            else "NO " + ",".join(sorted(set(doc) ^ set(first(base, "/"))))
        )
        extra = [k for k in ("cache-control", "pragma") if k in doc]
        print(
            f"{mode:11} {same:22} {','.join(extra) or '-':28} "
            f"{count(reqs, '/a.js')}x{'':23} {count(reqs, '/a.png')}x"
        )


# --------------------------------------------------------------------------
# selftest: the adapters alone, no browser
# --------------------------------------------------------------------------


class _PwFrame:
    def __init__(self, url: str, parent: _PwFrame | None = None) -> None:
        self.url = url
        self.parent_frame = parent


class _PwRequest:
    """What Playwright hands the route handler, as the unit tests already fake it."""

    def __init__(self, url, method, post_data, navigation, frame) -> None:
        self.url, self.method, self.post_data = url, method, post_data
        self._navigation, self._frame = navigation, frame

    def is_navigation_request(self) -> bool:
        return self._navigation

    @property
    def frame(self) -> _PwFrame:
        if self._frame is None:
            raise RuntimeError("Frame not available for this request")
        return self._frame


class _FakeCdp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        return {}


# (name, url, method, body, navigation, frame url, parent url or None, kind)
# kind: "page" frame in a page target, "oopif" root of an iframe target, "unknown" no frame
_PARITY_CASES = [
    (
        "same-origin link",
        "https://a.example/x",
        "GET",
        None,
        True,
        "https://a.example/",
        None,
        "page",
    ),
    (
        "cross-origin link",
        "https://b.example/x",
        "GET",
        None,
        True,
        "https://a.example/",
        None,
        "page",
    ),
    ("from about:blank", "https://a.example/x", "GET", None, True, "about:blank", None, "page"),
    ("frame unknown", "https://a.example/x", "GET", None, True, "", None, "unknown"),
    (
        "form post same origin",
        "https://a.example/s",
        "POST",
        "a=1",
        True,
        "https://a.example/",
        None,
        "page",
    ),
    (
        "form post cross origin",
        "https://b.example/s",
        "POST",
        "a=1",
        True,
        "https://a.example/",
        None,
        "page",
    ),
    (
        "get with a body",
        "https://a.example/s",
        "GET",
        "a=1",
        True,
        "https://a.example/",
        None,
        "page",
    ),
    ("put", "https://a.example/s", "PUT", None, True, "https://a.example/", None, "page"),
    (
        "subresource",
        "https://a.example/app.js",
        "GET",
        None,
        False,
        "https://a.example/",
        None,
        "page",
    ),
    (
        "iframe get",
        "https://c.example/f",
        "GET",
        None,
        True,
        "https://a.example/",
        "https://a.example/",
        "page",
    ),
    (
        "iframe post",
        "https://a.example/s",
        "POST",
        "a=1",
        True,
        "https://c.example/f",
        "https://a.example/",
        "page",
    ),
    (
        "blank iframe post",
        "https://a.example/s",
        "POST",
        "a=1",
        True,
        "about:blank",
        "https://a.example/",
        "page",
    ),
    ("popup first post", "https://b.example/s", "POST", "a=1", True, "about:blank", None, "page"),
    (
        "oopif get",
        "https://c.example/f2",
        "GET",
        None,
        True,
        "https://c.example/f",
        "https://a.example/",
        "oopif",
    ),
    (
        "oopif post",
        "https://c.example/s",
        "POST",
        "a=1",
        True,
        "https://c.example/f",
        "https://a.example/",
        "oopif",
    ),
]


def _paused_equivalent(case: tuple) -> tuple[dict, FrameCache]:
    """The Fetch.requestPaused event, and the frame cache, that case would produce."""
    _name, url, method, body, navigation, frame_url, parent_url, kind = case
    cache = FrameCache(root_is_subframe=(kind == "oopif"))
    request: dict = {"url": url, "method": method}
    if body:
        request["postData"] = body
        request["hasPostData"] = True
    event = {
        "requestId": "r1",
        "request": request,
        "frameId": "F2",
        "resourceType": "Document" if navigation else "Script",
    }
    if kind == "unknown":
        return event, cache
    if kind == "oopif":  # the iframe target's own tree: one root, no parent in it
        cache.load({"frame": {"id": "F2", "url": frame_url}})
    elif parent_url is not None:
        cache.load(
            {
                "frame": {"id": "F1", "url": parent_url},
                "childFrames": [{"frame": {"id": "F2", "parentId": "F1", "url": frame_url}}],
            }
        )
    else:
        cache.load({"frame": {"id": "F2", "url": frame_url}})
    return event, cache


def _playwright_equivalent(case: tuple) -> _PwRequest:
    _name, url, method, body, navigation, frame_url, parent_url, kind = case
    if kind == "unknown":
        return _PwRequest(url, method, body, navigation, None)
    parent = _PwFrame(parent_url) if parent_url else None
    return _PwRequest(url, method, body, navigation, _PwFrame(frame_url, parent))


async def cmd_selftest(_args: argparse.Namespace) -> None:
    from lyra_browser.audit import AuditLog
    from lyra_browser.config import Config
    from lyra_browser.enforcement import NavigationGuard, classify
    from lyra_browser.origin import parse_origin
    from lyra_browser.permission import Capability, PermissionStore

    bad = 0
    print("classify(): Playwright-shaped request vs the adapter over Fetch.requestPaused")
    for case in _PARITY_CASES:
        event, cache = _paused_equivalent(case)
        want = classify(_playwright_equivalent(case))
        got = classify(PausedRequest(event, cache.frames))
        same = want == got
        bad += not same
        label = "None" if want is None else f"{want.capability.value} subject={want.subject}"
        print(f"  {'ok  ' if same else 'DIFF'} {case[0]:24} {label}")
    print("\nNavigationGuard end to end (enforce; NAVIGATE+INTERACT granted on a.example):")
    tmp = Path(tempfile.mkdtemp(prefix="probe-self-"))
    try:
        for case in _PARITY_CASES:
            outcomes = []
            for make in ("playwright", "paused"):
                cfg = Config(data_dir=tmp / make, enforcement_mode="enforce", headless=True)
                perms = PermissionStore()
                perms.grant("default", parse_origin("https://a.example/"), Capability.NAVIGATE)
                guard = NavigationGuard(cfg, perms, AuditLog(cfg.audit_path), mode="enforce")
                cdp = _FakeCdp()
                route = PausedRoute(cdp, "r1")
                if make == "playwright":
                    request = _playwright_equivalent(case)
                else:
                    event, cache = _paused_equivalent(case)
                    request = PausedRequest(event, cache.frames)
                await guard(route, request)
                outcomes.append((route.resolution, guard.refusals))
            same = outcomes[0] == outcomes[1]
            bad += not same
            print(f"  {'ok  ' if same else 'DIFF'} {case[0]:24} {outcomes[1][0]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    cdp = _FakeCdp()
    await PausedRoute(cdp, "r9").fulfill(status=204)
    await PausedRoute(cdp, "r9").continue_()
    print(f"\nroute adapter sent: {[(m, p) for m, p in cdp.sent]}")
    print(f"\n{'ALL PARITY CASES MATCH' if not bad else f'{bad} MISMATCHES'}")


# --------------------------------------------------------------------------
# fingerprint: what the page itself can read, per cell
# --------------------------------------------------------------------------

FINGERPRINT_JS = r"""
(async () => {
  const r = {};
  r.webdriver = navigator.webdriver;
  r.ua = navigator.userAgent;
  const uad = navigator.userAgentData;
  r.brands = uad ? uad.brands.map(b => b.brand + '/' + b.version).join(',') : null;
  try {
    const he = await uad.getHighEntropyValues(['platform', 'platformVersion', 'architecture', 'bitness', 'fullVersionList']);
    r.uaHigh = [he.platform, he.platformVersion, he.architecture, he.bitness, (he.fullVersionList || []).map(x => x.brand).join('|')].join(';');
  } catch (e) { r.uaHigh = 'err'; }
  r.platform = navigator.platform;
  r.languages = (navigator.languages || []).join(',');
  r.plugins = navigator.plugins.length;
  r.mimeTypes = navigator.mimeTypes.length;
  r.chromeKeys = window.chrome ? Object.keys(window.chrome).sort().join(',') : 'none';
  r.outer = outerWidth + 'x' + outerHeight;
  r.inner = innerWidth + 'x' + innerHeight;
  r.screen = screen.width + 'x' + screen.height + 'x' + screen.colorDepth;
  r.dpr = devicePixelRatio;
  r.hwc = navigator.hardwareConcurrency;
  r.deviceMemory = navigator.deviceMemory;
  r.gamut = ['srgb', 'p3', 'rec2020'].filter(g => matchMedia('(color-gamut: ' + g + ')').matches).join(',');
  r.hover = matchMedia('(hover: hover)').matches;
  r.pointer = matchMedia('(pointer: fine)').matches;
  r.notification = Notification.permission;
  try { r.permNotifications = (await navigator.permissions.query({name: 'notifications'})).state; } catch (e) { r.permNotifications = 'err'; }
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const d = gl.getExtension('WEBGL_debug_renderer_info');
    r.webgl = gl.getParameter(d.UNMASKED_VENDOR_WEBGL) + ' | ' + gl.getParameter(d.UNMASKED_RENDERER_WEBGL);
  } catch (e) { r.webgl = 'none'; }
  try {
    const c = document.createElement('canvas'); c.width = 120; c.height = 40;
    const x = c.getContext('2d'); x.font = '16px Arial'; x.fillText('probe 4549 \u00e9', 4, 24); x.arc(60, 20, 12, 0, 6.28); x.stroke();
    let h = 0; for (const ch of c.toDataURL()) h = (h * 31 + ch.charCodeAt(0)) | 0;
    r.canvas = h;
  } catch (e) { r.canvas = 'err'; }
  r.pdfViewer = navigator.pdfViewerEnabled;
  r.connection = navigator.connection ? [navigator.connection.effectiveType, navigator.connection.rtt, navigator.connection.downlink].join('/') : null;
  r.automationGlobals = Object.keys(window).filter(k => /cdc_|__playwright|__pw|webdriver/i.test(k)).join(',');
  r.speechVoices = speechSynthesis.getVoices().length;
  r.perfMemory = performance.memory ? Math.round(performance.memory.jsHeapSizeLimit / 1e6) : null;
  r.popupBlockedWithoutGesture = (() => { const w = window.open('about:blank'); const blocked = !w; if (w) w.close(); return blocked; })();
  r.tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  r.visibility = document.visibilityState;
  r.hasFocus = document.hasFocus();
  return r;
})()
"""  # noqa: E501


async def cmd_fingerprint(args: argparse.Namespace) -> None:
    headless = args.mode == "headless"
    ua = windowed_user_agent() if headless else None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FpHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    rows: dict[str, dict] = {}
    try:
        for cell in resolve_cells(args.cells):
            try:
                async with make_browser(cell, headless, ua) as browser:
                    await browser.goto(url)
                    await asyncio.sleep(1.5)
                    rows[cell.name] = await browser.evaluate(FINGERPRINT_JS)
            except Exception as exc:  # noqa: BLE001 - a failed launch is a result
                rows[cell.name] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
    finally:
        server.shutdown()
        server.server_close()
    names = list(rows)
    base = rows[names[0]]
    print(f"\n=== page-visible differences from {names[0]} ({args.mode}) ===")
    for name in names[1:]:
        row = rows[name]
        diff = {
            k: (base.get(k), row.get(k))
            for k in sorted(set(base) | set(row))
            if base.get(k) != row.get(k)
        }
        print(f"{name}: " + ("(identical)" if not diff else ""))
        for key, (a, b) in diff.items():
            print(f"    {key}: {a!r} -> {b!r}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"fingerprint-{args.mode}.json").write_text(json.dumps(rows, indent=1, default=str))


class _FpHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"<!doctype html><title>fp</title>fp"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


# --------------------------------------------------------------------------
# tls: what an edge sees before any JavaScript runs
# --------------------------------------------------------------------------

TLS_ECHO = "https://tls.peet.ws/api/all"


def tls_summary(echo: dict) -> dict:
    """The parts of the echo that a bot defence keys on, without the noise (IP, randoms)."""
    frames = (echo.get("http2") or {}).get("sent_frames") or []
    headers = next((f.get("headers") for f in frames if f.get("headers")), []) or []
    names = [h.split(":", 2)[1] if h.startswith(":") else h.split(":", 1)[0] for h in headers]
    tls = echo.get("tls") or {}
    return {
        "http": echo.get("http_version"),
        "ja4": tls.get("ja4"),
        "peetprint": (tls.get("peetprint_hash") or "")[:12],
        "akamai": ((echo.get("http2") or {}).get("akamai_fingerprint_hash") or "")[:12],
        "header_order": ",".join(n.lower() for n in names),
    }


async def cmd_tls(args: argparse.Namespace) -> None:
    headless = args.mode == "headless"
    ua = windowed_user_agent() if headless else None
    rows = []
    for cell in resolve_cells(args.cells):
        for _rep in range(args.reps):
            try:
                async with make_browser(cell, headless, ua) as browser:
                    await browser.goto(TLS_ECHO)
                    await asyncio.sleep(2.5)
                    text = await browser.evaluate("document.body.innerText")
                row = {"cell": cell.name, **tls_summary(json.loads(text))}
            except Exception as exc:  # noqa: BLE001 - a failed launch is a result
                row = {"cell": cell.name, "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            rows.append(row)
            print(json.dumps(row), flush=True)
    print(f"\n=== tls/h2/header fingerprint per cell ({args.mode}) ===")
    base = next((r for r in rows if "ja4" in r), {})
    for row in rows:
        if "error" in row:
            print(f"{row['cell']:22} {row['error']}")
            continue
        diff = [k for k in ("ja4", "peetprint", "akamai", "header_order") if row[k] != base.get(k)]
        print(
            f"{row['cell']:22} ja4={row['ja4']} peet={row['peetprint']} h2={row['akamai']} "
            f"{'== first row' if not diff else 'DIFFERS: ' + ','.join(diff)}"
        )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure", help="real-internet matrix")
    m.add_argument("--mode", choices=["headless", "headful"], default="headless")
    m.add_argument("--cells", default="a-plain,b-route,n-noroute,c-doc,d-doc-bare")
    m.add_argument("--sites", default="etsy,tripadvisor")
    m.add_argument("--reps", type=int, default=3)
    m.add_argument("--gap", type=float, default=20.0, help="seconds between visits")
    m.add_argument("--timeout", type=float, default=150.0, help="seconds per visit")
    m.add_argument("--seed", type=int, default=4549)
    m.add_argument("--out", default="")
    m.add_argument("--list-cells", action="store_true")
    s = sub.add_parser("summary", help="aggregate measure files")
    s.add_argument("files", nargs="+", type=Path)
    f = sub.add_parser("fixture", help="loopback parity of the guard under each design")
    f.add_argument("--headless-only", action="store_true")
    f.add_argument("--headful-only", action="store_true")
    f.add_argument("--designs", default="none,route,doc-page,doc-side")
    f.add_argument("--scenarios", default="", help="comma-separated names; default all")
    f.add_argument("--races", type=int, default=8, help="repetitions of each popup scenario")
    f.add_argument("--driver", choices=["playwright", "patchright"], default="playwright")
    w = sub.add_parser("wire", help="what a server sees per interception mode")
    w.add_argument("--mode", choices=["headless", "headful"], default="headless")
    w.add_argument("--modes", default="none,route,doc-page,doc-side,fetch-all,cache-off")
    w.add_argument("--driver", choices=["playwright", "patchright"], default="playwright")
    t = sub.add_parser("tls", help="TLS / HTTP2 / header fingerprint per cell, via an echo service")
    t.add_argument("--mode", choices=["headless", "headful"], default="headful")
    t.add_argument("--cells", default="a-plain,b-route,n-noroute,c-doc,d-doc-bare")
    t.add_argument("--reps", type=int, default=1)
    sub.add_parser("selftest", help="adapter parity with Playwright's request shape; no browser")
    p = sub.add_parser("fingerprint", help="page-visible differences per cell (loopback)")
    p.add_argument("--mode", choices=["headless", "headful"], default="headful")
    p.add_argument("--cells", default="a-plain,n-noroute,d-doc-bare")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "measure":
        if args.list_cells:
            for cell in CELLS.values():
                print(f"{cell.name:20} {cell.note}")
            return 0
        asyncio.run(cmd_measure(args))
    elif args.cmd == "summary":
        summarize(args.files)
    elif args.cmd == "fixture":
        asyncio.run(cmd_fixture(args))
    elif args.cmd == "wire":
        asyncio.run(cmd_wire(args))
    elif args.cmd == "tls":
        asyncio.run(cmd_tls(args))
    elif args.cmd == "fingerprint":
        asyncio.run(cmd_fingerprint(args))
    elif args.cmd == "selftest":
        asyncio.run(cmd_selftest(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
