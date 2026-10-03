#!/usr/bin/env python3
"""An action the guard refused must say so, and one that opened a tab must say that.

Found by ``verify_realsites.py`` on MDN: a ``click`` on a link inside an ad frame answered
``{"status": "ok"}`` although the NavigationGuard had refused the navigation it caused (the
audit read ``denied navigate <url> (initiator (unknown))``). The 204 leaves the tab where it
was, so the reply looked like a click that did nothing, with no word of why. And a click that
opened a tab reported the opener's URL as if nothing had happened.

This gate serves pages from two loopback origins (``127.0.0.1`` and ``localhost``) and checks,
in a real browser, that:

- a link to an origin no approval covers, a form sent without ``submits=true``, a redirect, a
  key press, a typed Enter, a hover and a page's own timer are each answered ``blocked_by_policy``
  with ``url`` unchanged and a hint that names the way out (``navigate``, or ``submits=true``),
  that the destination never saw a request, and that the audit row of the action says so too;
- the same holds for a link in a sandboxed iframe and in a cross-origin iframe, where the
  initiator of a popup is unknown (the MDN case);
- the way out the hint names works: the declared form goes through, and so does the link once the
  destination was asked for;
- a click to an approved origin, a click that only runs script and a same-site link stay ``ok``,
  the script-only click without any wait added after the driver returned;
- ``observe`` mode records ``would_deny`` and stays ``ok``, and a takeover is untouched;
- an action that opened a tab says ``new_tab: true`` and ``tab_count`` (``click`` and
  ``press_key``), ``get_url`` and ``open_browser`` carry ``tab_count``, and a popup the guard
  refused opens no tab and answers ``blocked_by_policy``.

It also measures, per scenario, how late the guard's verdict landed after the driver's call
returned (``INFO`` lines). That is the number the design rests on: the check is made once the
action and the settle window every action already listens for are over, with no wait of its own,
so a verdict must land inside that window.

    .venv/bin/python scripts/verify_click_refusal_e2e.py
    xvfb-run -a .venv/bin/python scripts/verify_click_refusal_e2e.py --headful-only
    LYRA_BROWSER_GUARD=cdp .venv/bin/python scripts/verify_click_refusal_e2e.py
    <venv with patchright>/bin/python scripts/verify_click_refusal_e2e.py --driver patchright
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

from lyra_browser.origin import parse_origin  # noqa: E402
from lyra_browser.permission import Capability  # noqa: E402

ANSWER_WITHIN_S = 4.0  # a tool call that has to say something answers well inside this
EXTRA_WAIT_S = 0.15  # what a script-only click may take beyond the settle window every action has
FOLD_WITHIN_S = 0.03  # the check itself, when there is nothing to report
FRAME_CLICK_TRIES = 8  # clicks Chrome may drop on a frame it has only just made

# @FAR@ and @NEAR@ are the two ports. FAR is localhost, NEAR is 127.0.0.1: other origins.
FAR_FINAL = "http://localhost:@FAR@/final"
# Counted in the DOM, not on ``window``: patchright evaluates in an isolated world and would
# not see a page global.
LISTENER = (
    "<script>addEventListener('message', (e) => { if (e.data === 'clicked') {"
    " const root = document.documentElement;"
    " root.dataset.clicked = String(Number(root.dataset.clicked || 0) + 1); } });</script>"
)
CLICKS_JS = "Number(document.documentElement.dataset.clicked || 0)"
REPORT = "onclick=\"parent.postMessage('clicked', '*')\""


def _srcdoc(markup: str) -> str:
    return markup.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


NEAR_PAGES = {
    "/second": "<!doctype html><title>SECOND</title><h1>second</h1>",
    # a link to another origin: leaving
    "/link": f'<!doctype html><title>link</title><h1>link page</h1><a id="go" href="{FAR_FINAL}">'
    "to the other site</a>",
    # a link to a page of the same site
    "/same": '<!doctype html><title>same</title><a id="go" href="/second">second</a>',
    # a click that only runs script
    "/js": '<!doctype html><title>js</title><button id="go" onclick="document.title=\'clicked\'">'
    "b</button>",
    # same-site link whose server sends the browser to the other origin
    "/hoplink": '<!doctype html><title>hop</title><a id="go" href="/redir">hop</a>',
    # a form sent to another origin
    "/form": f'<!doctype html><title>form</title><form method="post" action="{FAR_FINAL}">'
    '<input name="a" value="1"><button id="go">send</button></form>',
    # a GET form to another origin, submitted by typing
    "/search": f'<!doctype html><title>search</title><form action="{FAR_FINAL}">'
    '<input id="q" name="q"></form>',
    # a link already focused, for a key press
    "/keylink": f'<!doctype html><title>key</title><a id="go" href="{FAR_FINAL}">k</a>'
    '<script>document.getElementById("go").focus()</script>',
    # a menu that sends the page away when the pointer arrives
    "/hover": '<!doctype html><title>hover</title><div id="go" style="width:200px;height:50px" '
    f"onmouseover=\"location.href='{FAR_FINAL}'\">menu</div>",
    # a page's own timers: one inside the settle window, one long after it
    "/timer": '<!doctype html><title>timer</title><button id="go" '
    f"onclick=\"setTimeout(function () {{ location.href = '{FAR_FINAL}'; }}, 0)\">b</button>",
    "/slowtimer": '<!doctype html><title>slow</title><button id="go" '
    f"onclick=\"setTimeout(function () {{ location.href = '{FAR_FINAL}'; }}, 600)\">b</button>",
    # an ad frame: sandboxed, so it has no origin of its own
    "/sandbox": "<!doctype html><title>sandbox</title>"
    + LISTENER
    + '<iframe id="ad" sandbox="allow-scripts allow-popups allow-top-navigation" srcdoc="'
    + _srcdoc(f'<a id="go" target="_blank" href="{FAR_FINAL}" {REPORT}>ad</a>')
    + '"></iframe>',
    "/sandbox-top": "<!doctype html><title>sandbox-top</title>"
    + LISTENER
    + '<iframe id="ad" sandbox="allow-scripts allow-popups allow-top-navigation" srcdoc="'
    + _srcdoc(f'<a id="go" target="_top" href="{FAR_FINAL}" {REPORT}>ad</a>')
    + '"></iframe>',
    # frames from the other origin
    "/cross-popup": "<!doctype html><title>cross-popup</title>"
    + LISTENER
    + '<iframe id="ad" src="http://localhost:@FAR@/framed-popup"></iframe>',
    "/cross-top": "<!doctype html><title>cross-top</title>"
    + LISTENER
    + '<iframe id="ad" src="http://localhost:@FAR@/framed-top"></iframe>',
    # popups
    "/popup-button": "<!doctype html><title>popup-button</title>"
    '<button id="go" onclick="window.open(\'/second\')">open</button>',
    "/popup-link": "<!doctype html><title>popup-link</title>"
    '<a id="go" target="_blank" href="/second">open</a>',
    "/popup-key": "<!doctype html><title>popup-key</title>"
    '<a id="go" target="_blank" href="/second">open</a>'
    '<script>document.getElementById("go").focus()</script>',
}

FAR_PAGES = {
    "/final": "<!doctype html><title>FINAL</title><h1>final</h1>",
    "/framed-popup": "<!doctype html><title>framed</title>"
    f'<a id="go" target="_blank" href="http://127.0.0.1:@NEAR@/second" {REPORT}>ad</a>',
    "/framed-top": "<!doctype html><title>framed</title>"
    f'<a id="go" target="_top" href="{FAR_FINAL}" {REPORT}>ad</a>',
}


class Near(BaseHTTPRequestHandler):
    far_port = 0
    near_port = 0
    pages = NEAR_PAGES

    def log_message(self, *_args) -> None:
        pass

    def _send(self, body: str) -> None:
        raw = body.replace("@FAR@", str(Near.far_port)).replace("@NEAR@", str(Near.near_port))
        raw = raw.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/redir":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{Near.far_port}/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = self.pages.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.do_GET()


class Far(Near):
    """The destination the pages try to reach. Whatever arrives at /final is recorded."""

    pages = FAR_PAGES
    seen: list[str] = []

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/final":
            Far.seen.append(f"{self.command} {self.path}")
        super().do_GET()


class Probe:
    """When the guard's verdicts landed, against the moment the driver's action returned.

    The driver's ``click`` / ``hover`` / ``press`` / ``fill`` and the keyboard's ``press`` are
    wrapped at the class (playwright and patchright alike), and the guard's one judgement is
    wrapped at the instance, so the same numbers come out of either backend.
    """

    def __init__(self, ctx, page) -> None:
        self.returned = 0.0
        self.verdicts: list[tuple[float, bool]] = []
        self.tab_events: list[float] = []
        self.fold_s: list[float] = []
        judge = ctx.guard._judge

        def judged(intent):
            allowed = judge(intent)
            self.verdicts.append((time.monotonic(), allowed))
            return allowed

        ctx.guard._judge = judged
        for cls, names in (
            (type(page.locator("body")), ("click", "hover", "press", "fill")),
            (type(page.keyboard), ("press",)),
        ):
            for name in names:
                self._wrap(cls, name)
        page.context.on("page", lambda _tab: self.tab_events.append(time.monotonic()))
        from lyra_browser.tools.aftermath import Aftermath

        fold = Aftermath.fold
        probe = self

        @functools.wraps(fold)
        async def timed_fold(this, *args, **kwargs):
            started = time.monotonic()
            try:
                return await fold(this, *args, **kwargs)
            finally:
                probe.fold_s.append(time.monotonic() - started)

        Aftermath.fold = timed_fold

    def _wrap(self, cls, name: str) -> None:
        original = getattr(cls, name)
        probe = self

        @functools.wraps(original)
        async def wrapper(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            finally:
                probe.returned = time.monotonic()

        setattr(cls, name, wrapper)

    def reset(self) -> None:
        self.returned = 0.0
        self.verdicts.clear()
        self.tab_events.clear()
        self.fold_s.clear()

    def refusal_lateness_ms(self) -> float | None:
        """First refusal, in ms after the driver's call returned (negative: before it did)."""
        refused = [at for at, allowed in self.verdicts if not allowed]
        return (refused[0] - self.returned) * 1000 if refused and self.returned else None

    def tab_lateness_ms(self) -> float | None:
        return (self.tab_events[0] - self.returned) * 1000 if self.tab_events else None


def audit_rows(ctx) -> list[dict]:
    path = ctx.config.audit_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def own_rows(ctx, tool: str, *, after: int = 0) -> list[dict]:
    """The tool's own audit rows (the consent broker writes some under the same name)."""
    return [r for r in audit_rows(ctx)[after:] if r["tool"] == tool and "selector" in r["args"]]


def refused_rows(ctx, *, after: int = 0) -> list[dict]:
    return [
        r for r in audit_rows(ctx)[after:] if r["tool"] == "navigation" and r["status"] == "denied"
    ]


async def timed(awaitable) -> tuple[dict, float]:
    started = time.monotonic()
    result = await awaitable
    return result, time.monotonic() - started


def make_servers(near: int, far: int) -> list[ThreadingHTTPServer]:
    Near.near_port = Far.near_port = near
    Near.far_port = Far.far_port = far
    servers = []
    for port, handler in ((near, Near), (far, Far)):
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    return servers


async def run_mode(near: int, far: int, *, headless: bool, driver: str) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(
        prefix=f"lyra-clickrefusal-{mode}-", ignore_cleanup_errors=True
    )
    ctx, tools = await build_tools(Path(tmp.name), headless=headless)
    ctx.config.driver = driver
    base = f"http://127.0.0.1:{near}"
    far_final = f"http://localhost:{far}/final"
    backend = ctx.config.guard_backend
    settle = ctx.config.download_settle_s
    print(f"\n[{mode}] driver={driver} guard={backend} settle={settle * 1000:.0f}ms")
    print(f"data_dir={tmp.name}")
    try:
        opened = await tools["open_browser"]()
        expect(opened.get("status") == "ok", "browser launches", opened)
        expect(opened.get("tab_count") == 1, "open_browser says one tab is open", opened)
        page = await ctx.session.page()
        probe = Probe(ctx, page)

        async def enter(path: str) -> None:
            """Stand on a fixture page with nothing left over from the one before."""
            nonlocal page
            for extra in ctx.session.open_pages()[1:]:
                await extra.close()
            page = await ctx.session.page()
            entered = await tools["navigate"](url=base + path, confirm=True)
            expect(entered.get("status") == "ok", f"enter {path}", entered)
            Far.seen.clear()
            probe.reset()

        def lateness(what: str, value: float | None, window_ms: float) -> None:
            shown = (
                "no refusal seen" if value is None else f"{value:+.1f} ms after the driver returned"
            )
            print(f"INFO  {what}: {shown} (settle window {window_ms:.0f} ms)")

        async def blocked_click(path: str, what: str, *, tool: str = "click", **kwargs):
            """Do the action on ``path``; assert it was refused, said so, and changed nothing."""
            await enter(path)
            before = len(audit_rows(ctx))
            reply, took = await timed(tools[tool](**kwargs))
            expect(
                reply.get("status") == "blocked_by_policy",
                f"{what}: answered blocked_by_policy",
                (
                    reply,
                    f"refusal landed {probe.refusal_lateness_ms()} ms after the driver returned",
                ),
            )
            expect(took < ANSWER_WITHIN_S, f"{what}: and promptly ({took:.2f}s)")
            expect(reply.get("url") == page.url, f"{what}: with url where the tab stands", reply)
            expect(bool(reply.get("hint")), f"{what}: with a hint", reply)
            expect(Far.seen == [], f"{what}: the destination never saw a request", Far.seen)
            expect(
                len(refused_rows(ctx, after=before)) == 1,
                f"{what}: the guard's trail holds the refusal",
                audit_rows(ctx)[before:],
            )
            acted = [r for r in audit_rows(ctx)[before:] if r["tool"] == tool]
            expect(
                acted and acted[-1]["status"] == "blocked_by_policy",
                f"{what}: and so does the action's own audit row",
                acted,
            )
            lateness(what, probe.refusal_lateness_ms(), settle * 1000)
            return reply

        # -- a link to an origin no approval covers -----------------------------------
        reply = await blocked_click("/link", "a link to an unapproved origin", selector="#go")
        expect(reply["url"] == base + "/link", "the tab is still on the link page", reply)
        expect(
            reply.get("matches") == 1
            and reply.get("clicked", {}).get("role") == "link"
            and "navigate" in reply["hint"],
            "and keeps what it reported (matches, clicked); the hint says to navigate",
            reply,
        )
        again = await tools["get_url"]()
        text = await tools["read_page"]()
        expect(
            again["url"] == base + "/link" and "link page" in text["text"],
            "the page is still standing and readable",
            (again, text),
        )

        # -- a redirect the site made -------------------------------------------------
        await enter("/hoplink")
        before = len(audit_rows(ctx))
        hop = await tools["click"](selector="#go")
        expect(hop.get("status") == "blocked_by_policy", "a link whose server redirects away", hop)
        expect(
            hop.get("redirected_to") == f"http://localhost:{far}",
            "names the origin it was sent to",
            hop,
        )
        expect(hop.get("url") == page.url, "and where the tab stands", (hop, page.url))
        expect(
            [r["status"] for r in audit_rows(ctx)[before:] if r["tool"] == "click"][-1:]
            == ["blocked_by_policy"],
            "and the action's audit row says so",
        )
        if backend == "cdp":
            expect(Far.seen == [], "the refused hop never reached its destination", Far.seen)

        # -- a form sent without declaring it, then declared -------------------------
        reply = await blocked_click("/form", "a form sent without submits=true", selector="#go")
        expect("submits=true" in reply["hint"], "the hint says to declare the send", reply)
        sent, took = await timed(tools["click"](selector="#go", submits=True, confirm=True))
        expect(sent.get("status") == "ok", "and the declared send goes through", sent)
        expect(
            Far.seen == ["POST /final"] and sent.get("url") == far_final,
            "the destination received it, and the tab moved there",
            (Far.seen, sent),
        )

        # -- the keyboard ---------------------------------------------------------------
        await blocked_click("/keylink", "Enter on a focused link", tool="press_key", key="Enter")

        # -- typing Enter into a GET form to another origin ----------------------------
        typed = await blocked_click(
            "/search",
            "typing into a form that leaves the site",
            tool="type_text",
            selector="#q",
            value="needle",
            submit=True,
            confirm=True,
        )
        expect(
            "submit=true" not in typed["hint"],
            "no advice to declare what was already declared",
            typed,
        )

        # -- a hover that sends the page away ------------------------------------------
        await blocked_click(
            "/hover", "a hover that sends the page away", tool="hover", selector="#go"
        )

        # -- a page's own timer ---------------------------------------------------------
        await blocked_click("/timer", "a timer the click set off", selector="#go")

        await enter("/slowtimer")
        slow = await tools["click"](selector="#go")
        expect(
            slow.get("status") == "ok",
            "a timer that fires long after the window is not waited for",
            slow,
        )
        late_by = time.monotonic()
        while time.monotonic() - late_by < 3 and not refused_rows(ctx):
            await asyncio.sleep(0.05)
        expect(
            bool(refused_rows(ctx)) and page.url == base + "/slowtimer",
            "its refusal is on the trail afterwards, and the tab stayed (the known limit)",
            audit_rows(ctx)[-3:],
        )

        # -- a link in an ad frame (the MDN case) ----------------------------------------
        async def landed(registered: int) -> bool:
            """Whether the frame has reported a click since ``registered`` (it posts a message)."""
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if await page.evaluate(CLICKS_JS) > registered:
                    return True
                await asyncio.sleep(0.05)
            return False

        async def blocked_in_frame(path: str, what: str) -> None:
            await enter(path)
            before = len(audit_rows(ctx))
            selector = "#ad >> internal:control=enter-frame >> #go"
            # Chrome sometimes drops a click on a frame it has only just made (a process of its
            # own for a sandboxed or cross-origin frame); the frame reports the clicks that
            # landed, and only those are held to the answer.
            for attempt in range(1, FRAME_CLICK_TRIES + 1):
                registered = await page.evaluate(CLICKS_JS)
                reply = await tools["click"](selector=selector)
                if await landed(registered):
                    break
                expect(
                    reply.get("status") == "ok" and not refused_rows(ctx, after=before),
                    f"{what}: attempt {attempt} did not land in the frame and changed nothing",
                    reply,
                )
            else:
                raise AssertionError(f"{what}: no click landed in the frame in {FRAME_CLICK_TRIES}")
            expect(
                reply.get("status") == "blocked_by_policy",
                f"{what}: answered blocked_by_policy (landed on attempt {attempt})",
                (reply, refused_rows(ctx, after=before)),
            )
            expect(
                reply.get("url") == page.url and bool(reply.get("hint")),
                f"{what}: with url and hint",
                reply,
            )
            expect(Far.seen == [], f"{what}: the destination never saw a request", Far.seen)
            lateness(what, probe.refusal_lateness_ms(), settle * 1000)

        await blocked_in_frame("/sandbox", "a sandboxed ad frame's popup link")
        await blocked_in_frame("/sandbox-top", "a sandboxed ad frame's link to the top page")
        await blocked_in_frame("/cross-popup", "a cross-origin frame's popup link")
        await blocked_in_frame("/cross-top", "a cross-origin frame's link to the top page")

        # -- ok: a script-only click adds no wait ------------------------------------------
        await enter("/js")
        after_driver: list[float] = []
        for _ in range(5):
            probe.reset()
            reply, took = await timed(tools["click"](selector="#go"))
            after_driver.append(time.monotonic() - probe.returned)
            expect(reply.get("status") == "ok", "a script-only click is ok", reply)
            expect(
                not {"hint", "new_tab", "tab_count", "redirected_to"} & reply.keys(),
                "and says nothing more",
                reply,
            )
        expect(
            (await tools["get_url"]())["title"] == "clicked", "and it ran", await tools["get_url"]()
        )
        worst = max(after_driver)
        expect(
            worst < settle + EXTRA_WAIT_S,
            f"no wait beyond the settle window after the driver returned "
            f"(worst of 5: {worst * 1000:.0f} ms, window {settle * 1000:.0f} ms)",
            after_driver,
        )
        expect(
            max(probe.fold_s) < FOLD_WITHIN_S,
            f"and the check itself took {max(probe.fold_s) * 1000:.2f} ms",
            probe.fold_s,
        )

        # -- ok: a same-site link, which visibly navigates ---------------------------------
        await enter("/same")
        reply, took = await timed(tools["click"](selector="#go"))
        expect(
            reply.get("status") == "ok" and reply.get("url") == base + "/second",
            "a same-site link is ok and has moved",
            reply,
        )
        expect(
            probe.fold_s and max(probe.fold_s) < FOLD_WITHIN_S,
            f"with the check taking {max(probe.fold_s) * 1000:.2f} ms",
            probe.fold_s,
        )

        # -- ok: the destination was asked for, as the hint says ----------------------------
        await enter("/link")
        went = await tools["navigate"](url=far_final, reason="the hint's way out", confirm=True)
        expect(
            went.get("status") == "ok", "navigate to the destination asks, and is approved", went
        )
        await enter("/link")
        before = len(audit_rows(ctx))
        approved = await tools["click"](selector="#go")
        expect(
            approved.get("status") == "ok" and approved.get("url") == far_final,
            "and then the same link is ok and arrives",
            approved,
        )
        expect(
            Far.seen.count("GET /final") >= 1 and not refused_rows(ctx, after=before), "unrefused"
        )
        ctx.perms.revoke_all()

        # -- popups: a tab the action opened ------------------------------------------------
        site = parse_origin(base)
        await enter("/popup-button")
        before = len(audit_rows(ctx))
        refused = await tools["click"](selector="#go")
        expect(
            refused.get("status") == "blocked_by_policy" and "new_tab" not in refused,
            "a popup the guard refused is blocked_by_policy",
            refused,
        )
        expect(
            ctx.session.tab_info()["tab_count"] == 1
            and (await tools["get_url"]())["tab_count"] == 1,
            "and opened no tab",
        )
        lateness("a refused popup", probe.refusal_lateness_ms(), settle * 1000)

        # The operator's approval for the site (no tool buys an unconstrained NAVIGATE: see
        # verify_tabs_e2e.py).
        ctx.perms.grant("default", site, Capability.NAVIGATE)
        for path, what, tool, kwargs in (
            ("/popup-button", "window.open", "click", {"selector": "#go"}),
            ("/popup-link", "a target=_blank link", "click", {"selector": "#go"}),
            ("/popup-key", "Enter on a target=_blank link", "press_key", {"key": "Enter"}),
        ):
            await enter(path)
            probe.reset()
            reply, took = await timed(tools[tool](**kwargs))
            expect(
                reply.get("status") == "ok"
                and reply.get("new_tab") is True
                and reply.get("tab_count") == 2,
                f"{what}: ok, new_tab, tab_count 2",
                reply,
            )
            expect("tabs(" in reply.get("hint", ""), f"{what}: and a hint to use tabs", reply)
            expect(
                "url" not in reply or reply["url"] == base + path,
                f"{what}: url, when given, is the page the action was made on",
                reply,
            )
            there = await tools["get_url"]()
            expect(
                there["url"] == base + "/second" and there["tab_count"] == 2,
                f"{what}: get_url reads the new tab and counts both",
                there,
            )
            lateness(f"{what} (tab appeared)", probe.tab_lateness_ms(), settle * 1000)
        await enter("/js")
        reopened = await tools["open_browser"]()
        expect(
            reopened.get("tab_count") == 1, "open_browser counts the tabs that are left", reopened
        )

        # -- a takeover is untouched -----------------------------------------------------------
        await enter("/link")
        ctx.perms.revoke_all()
        if headless:
            ctx.collab.takeover = True
        else:
            taken = await tools["request_takeover"](reason="click refusal E2E")
            expect(taken.get("status") == "takeover_active", "takeover starts", taken)
        before = len(audit_rows(ctx))
        held = await tools["click"](selector="#go")
        expect(
            held.get("status") == "takeover_active" and set(held) <= {"status", "reason", "hint"},
            "an action during a takeover is turned away as before",
            held,
        )
        expect(
            page.url == base + "/link" and not refused_rows(ctx, after=before),
            "and nothing happened",
        )
        await page.goto(far_final)  # the user drives their own browser
        driven = [r for r in audit_rows(ctx)[before:] if r["tool"] == "navigation"]
        expect(
            page.url == far_final and [r["status"] for r in driven] == ["user_driven"],
            "the user's own navigation is let through and recorded as theirs",
            driven,
        )
        if headless:
            ctx.collab.takeover = False
        else:
            back = await tools["resume_after_takeover"](reason="click refusal E2E")
            expect(back.get("status") == "ok", "takeover ends", back)
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def run_observe(near: int, far: int, *, headless: bool, driver: str) -> None:
    """``observe``: the guard records what it would refuse and refuses nothing."""
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(
        prefix=f"lyra-clickobserve-{mode}-", ignore_cleanup_errors=True
    )
    ctx, tools = await build_tools(Path(tmp.name), headless=headless, enforcement_mode="observe")
    ctx.config.driver = driver
    base = f"http://127.0.0.1:{near}"
    print(f"\n[{mode}, observe] driver={driver} data_dir={tmp.name}")
    try:
        expect((await tools["open_browser"]()).get("status") == "ok", "browser launches")
        expect(ctx.guard.mode == "observe", "the guard is on observe")
        entered = await tools["navigate"](url=base + "/link", confirm=True)
        expect(entered.get("status") == "ok", "enter the link page", entered)
        Far.seen.clear()
        reply = await tools["click"](selector="#go")
        expect(reply.get("status") == "ok", "(observe) the click is ok", reply)
        expect(
            reply.get("url") == f"http://localhost:{far}/final" and Far.seen == ["GET /final"],
            "(observe) and the navigation really happened",
            (reply, Far.seen),
        )
        rows = [r for r in audit_rows(ctx) if r["tool"] == "navigation"]
        expect(
            "would_deny" in [r["status"] for r in rows]
            and "denied" not in [r["status"] for r in rows],
            "(observe) the trail records would_deny and no denial",
            rows,
        )
        expect(
            [
                r["status"]
                for r in audit_rows(ctx)
                if r["tool"] == "click" and "selector" in r["args"]
            ]
            == ["ok"],
            "(observe) and the click's own row says ok",
        )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool, driver: str) -> None:
    near, far = free_port(), free_port()
    servers = make_servers(near, far)
    try:
        if not headful_only:
            await run_mode(near, far, headless=True, driver=driver)
            await run_observe(near, far, headless=True, driver=driver)
        if not headless_only:
            await run_mode(near, far, headless=False, driver=driver)
            await run_observe(near, far, headless=False, driver=driver)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    parser.add_argument("--driver", choices=["auto", "playwright", "patchright"], default="auto")
    args = parser.parse_args()
    asyncio.run(async_main(args.headless_only, args.headful_only, args.driver))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
