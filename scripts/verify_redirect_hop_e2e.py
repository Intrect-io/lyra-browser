#!/usr/bin/env python3
"""An HTTP redirect chain must be judged hop by hop, not only at its first request.

The question. Playwright's ``context.route`` is offered the FIRST request of a chain and nothing
after it: every request that has a ``redirectedFrom`` is continued before any handler hears of
it. So a site the user approved could answer ``302 Location: http://elsewhere/`` and the tab
would follow, unjudged and unrecorded. ``NavigationGuard.on_request`` (a ``context.on("request")``
listener) closes that: each hop that changes origin is judged like a first request - the same
``classify``, the same grants, the same spending of a single-use one, the same audit row plus
``redirect_from`` - and, in enforce mode, refused by cancelling the load. This gate drives the
real tools (navigate, type_text, get_url, read_page) in a real Chrome against four loopback
origins and checks what that does to the tab, to the audit trail and to the servers:

- SITE (``127.0.0.1``): where the tab starts, approved with ``navigate(confirm=True)``; it
  serves the redirectors (``/hop?status=&to=``) and the form whose POST is redirected;
- PEER (``127.0.0.1``, another port): approved for hops, the way a user's earlier yes would be
  (``perms.grant`` with the initiator the hop starts from);
- AWAY, AWAY2 (``localhost``): never approved - the targets that must be refused.

Two guarantees a hop guard can make, told apart by ``--guarantee``:

``after`` (default) is what this branch promises. The hop request has already been sent when
the listener runs, so a refused hop REACHES its destination once - a GET with that origin's
cookies, or the body of a 307/308 POST - and only the rest of the chain is cut. The gate asserts
what is promised: the tool answered ``blocked_by_policy`` and named the origin the site
redirected to (``redirected_to``), the tab did not move, the trail holds ``denied`` then
``stopped``, the destination saw AT MOST ONE request and nothing further down the chain; and it
prints a ``KNOWN`` line (not a PASS) naming exactly what escaped. Scenario 10 is the contrast: a
first request - a page that sends itself away - is refused by the route before it leaves, so its
destination sees nothing under either guarantee, and the refusal names no redirect.

``before`` is the contract of an interceptor that sees a hop before it leaves: the destination
of a refused hop sees NOTHING. Those assertions - and the tab staying where it was - are
reported as ``FAIL`` lines that do not stop the run, so one run lists them all. They fail on
this branch; that is the point of the flag. ``--relay`` (with ``--guarantee before``) swaps in a
reference interceptor defined in this file - ``route.fetch(max_redirects=0)`` then
``route.fulfill`` - to prove they can be met, and where they cannot: the route layer still never
sees the second hop of a chain.

HOP_DELAY_S. A loopback server answers in well under a millisecond, faster than any stop can
land, so every hop would commit before it could be cancelled and every refusal would read
``not_stopped``. A remote server takes tens of milliseconds at least. Every destination that is
a REFUSED hop target (AWAY, AWAY2) therefore holds its answer back by HOP_DELAY_S, the way a
remote server does (``--hop-delay``; measured here: the stop lands within ~60 ms of the request
reaching the server, at load average ~15). Its request is logged on arrival, before the wait,
so what reached it is still counted. One scenario (8) removes the delay: a hop answered before
the stop lands commits, the audit says ``not_stopped`` and the tab is blanked - KNOWN, not a
guarantee.

Every assertion is a PASS line, what escaped is a KNOWN line, and each scenario is a case: a
failed assertion ends its case, prints FAIL and the run goes on, so the summary table at the end
of each mode lists every case that broke. Exit status 0: every assertion held (KNOWN lines are
not failures); 1: at least one did not (on this branch ``--guarantee before`` always does).

    .venv/bin/python scripts/verify_redirect_hop_e2e.py --headless-only
    xvfb-run -a -s "-screen 0 1920x1080x24" .venv/bin/python \\
        scripts/verify_redirect_hop_e2e.py --headful-only
    <venv with patchright>/bin/python scripts/verify_redirect_hop_e2e.py \\
        --headless-only --driver patchright
    xvfb-run -a -s "-screen 0 1920x1080x24" <venv with patchright>/bin/python \\
        scripts/verify_redirect_hop_e2e.py --headful-only --driver patchright
"""

from __future__ import annotations

import argparse
import asyncio
import html
import inspect
import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import REPO, build_tools  # noqa: E402
from verify_browser_e2e import expect as base_expect  # noqa: E402

from lyra_browser.enforcement import NavigationGuard, classify  # noqa: E402
from lyra_browser.origin import loggable_url, parse_origin  # noqa: E402
from lyra_browser.permission import Capability  # noqa: E402

HOP_DELAY_S = 0.35  # how long the destination of a refused hop takes to answer (see above)
DRIVER_TIMEOUT_MS = 8000  # the driver's own timeout, shortened so a hang shows as a failure
DEADLINE_S = 10.0  # every poll gives up after this
POLL_S = 0.05
QUIET_MARGIN_S = 0.3  # past the moment a held-back answer would have been due
NO_DELAY_ATTEMPTS = 8  # scenario 8 repeats until a hop has committed, at most this often
SECRET = "e2e-secret-value"  # typed into the form: the audit trail must never hold it
AWAY_COOKIE = ("away_c", "1")  # the browser's own cookie for AWAY: what an escaped hop carries
HOP_STATUSES = (301, 302, 303, 307, 308)
REFUSED = frozenset({"denied", "would_deny", "stopped", "not_stopped"})
BLANKED = "the page had already taken it; the tab was blanked"


# -- the four loopback origins --------------------------------------------------------------


@dataclass(frozen=True)
class Arrival:
    """One request as it reached a server: logged on arrival, before any delay."""

    method: str
    target: str  # path and query as received
    body: str
    cookie: str
    at: float

    @property
    def path(self) -> str:
        return urlsplit(self.target).path


def describe(arrival: Arrival) -> str:
    text = f"{arrival.method} {arrival.path}"
    if arrival.body:
        text += f", body {arrival.body!r}"
    if arrival.cookie:
        text += f", cookie {arrival.cookie}"
    return text


class Server(ThreadingHTTPServer):
    """One loopback origin: serves the fixture, logs what reaches it, can hold its answers."""

    def __init__(self, name: str, host: str, delay_s: float = 0.0) -> None:
        super().__init__(("127.0.0.1", 0), Handler)  # port 0: no race for a "free" one
        self.name = name
        self.host = host
        self.delay_s = delay_s
        self._lock = threading.Lock()
        self._seen: list[Arrival] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.server_address[1]}"

    def url(self, path: str = "/") -> str:
        return self.base + path

    def record(self, arrival: Arrival) -> None:
        with self._lock:
            self._seen.append(arrival)

    def arrivals(self) -> list[Arrival]:
        with self._lock:
            return list(self._seen)

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    def close(self) -> None:
        self.shutdown()
        self.server_close()  # shutdown() stops serving but leaves the socket bound

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Chrome hangs up on a hop the guard refused while its answer is still being held back.
        # That is the point of the delay, not an error.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def page(title: str, body: str = "") -> str:
    return f"<!doctype html><title>{title}</title><h1>{title}</h1>{body}"


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def _send(self, status: int, body: str, headers: tuple[tuple[str, str], ...] = ()) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")  # a 301/308 must not be answered from cache
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, status: int, to: str, cookie: str = "") -> None:
        headers = (("Location", to),) + ((("Set-Cookie", cookie),) if cookie else ())
        self._send(status, "", headers)

    def _serve(self) -> None:
        site = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        parts = urlsplit(self.path)
        if parts.path == "/favicon.ico":  # asked of every committed page; not what is counted
            self._send(404, "")
            return
        cookie = self.headers.get("Cookie", "")
        site.record(Arrival(self.command, self.path, body, cookie, time.monotonic()))
        if site.delay_s > 0:
            time.sleep(site.delay_s)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        name = site.name
        if parts.path in ("/hop", "/bounce", "/post-hop"):
            # One redirector under three names: /hop on SITE, /bounce on the others, and
            # /post-hop for the form's POST.
            self._redirect(int(query.get("status", 302)), query.get("to", "/landing"))
        elif parts.path == "/cookie-hop":
            cookie_set = f"{name.lower()}_hop={name.lower()}; Path=/"
            self._redirect(302, query.get("to", "/whoami"), cookie_set)
        elif parts.path == "/whoami":
            self._send(200, page(f"{name} WHOAMI", f"<p>cookie: {html.escape(cookie)}</p>"))
        elif parts.path == "/form":
            action = "/post-hop?" + urlencode(
                {"status": query.get("status", 307), "to": query.get("to", "/landing")}
            )
            form = (
                f'<form method="post" action="{html.escape(action)}"><input id="q" name="q"></form>'
            )
            self._send(200, page(f"{name} FORM", form))
        elif parts.path == "/start":
            self._send(200, page(f"{name} START"))
        elif parts.path == "/js-away":
            # A page that sends itself away while it loads: a refusal that is not a redirect.
            script = f"<script>location.href = {json.dumps(query.get('to', '/landing'))};</script>"
            self._send(200, page(f"{name} JS-AWAY", script))
        elif parts.path == "/landing":
            self._send(200, page(f"{name} LANDING {self.command}", f"<p>{html.escape(body)}</p>"))
        else:
            self._send(404, page(f"{name} 404"))


# -- the reference interceptor of --relay (the rejected "V1"; defined here, not in src) -------

_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class _HopRequest:
    """The request Chrome would send for a 3xx answer, shaped like what ``classify`` reads."""

    def __init__(self, source: Any, url: str, method: str, post_data: str | None) -> None:
        self.url = url
        self.method = method
        self.post_data = post_data
        self.frame = source.frame
        self.redirected_from = source

    def is_navigation_request(self) -> bool:
        return True


class RelayGuard(NavigationGuard):
    """Answer every judged document from Node, so the first hop can be refused before it leaves.

    ``route.fetch(max_redirects=0)`` asks the first server, and a 3xx answer is judged as the
    request it would become before the browser is given it. The route still never hears of the
    hop after that one: the browser follows the 3xx itself.
    """

    on_request = None  # the reference must not lean on the real listener (see install_relay)

    async def __call__(self, route: Any, request: Any) -> None:
        try:
            intent = classify(request)
            if intent is None:
                await route.continue_()
            elif not self._judge(intent):
                await route.fulfill(status=204)
            else:
                response = await route.fetch(max_redirects=0)
                hop = self._next_hop(request, response)
                judged = classify(hop) if hop is not None else None
                if judged is not None and not self._judge(judged):
                    await route.fulfill(status=204)
                else:
                    await route.fulfill(response=response)
        except Exception as exc:  # noqa: BLE001 — as the real guard: a broken one refuses
            await self._fail_closed(route, exc)

    @staticmethod
    def _next_hop(request: Any, response: Any) -> _HopRequest | None:
        location = response.headers.get("location")
        if response.status not in _REDIRECTS or not location:
            return None
        method, body = request.method, request.post_data
        if response.status == 303 or (response.status in (301, 302) and method == "POST"):
            method, body = "GET", None  # the Fetch spec: the body does not follow
        return _HopRequest(request, urljoin(request.url, location), method, body)


def install_relay(ctx: Any) -> None:
    """Swap the reference in the way ``ServerContext`` wires the real guard, before launch."""
    guard = RelayGuard(ctx.config, ctx.perms, ctx.audit, collab=ctx.collab)
    ctx.guard = guard
    ctx.session.guard = guard  # no on_request: the session installs the route and no listener


# -- what each case asserted --------------------------------------------------------------------


@dataclass
class Case:
    name: str
    passed: int = 0
    known: int = 0
    failures: list[str] = field(default_factory=list)


class Ledger:
    def __init__(self) -> None:
        self.cases: list[Case] = []
        self.current = self.begin("setup")

    def begin(self, name: str) -> Case:
        self.current = Case(name)
        self.cases.append(self.current)
        return self.current


LEDGER = Ledger()


def expect(condition: bool, message: str, detail: object = "") -> None:
    base_expect(condition, message, detail)
    LEDGER.current.passed += 1


def known(message: str) -> None:
    LEDGER.current.known += 1
    print(f"KNOWN  {message}")


def fail(text: str) -> None:
    LEDGER.current.failures.append(text)
    print(f"FAIL  {text}")


def promise(condition: bool, message: str, detail: object = "") -> None:
    """An assertion of ``--guarantee before``: a failure is listed, and the case goes on."""
    try:
        expect(condition, message, detail)
    except AssertionError as failure:
        fail(str(failure))


def audit_rows(ctx: Any) -> list[dict]:
    path = ctx.config.audit_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def trail_row(
    status: str, capability: str, method: str, url: str, source: str = ""
) -> tuple[str, str, str, str, str | None]:
    """A navigation row as the trail should hold it: query values gone, names kept."""
    return (status, capability, method, loggable_url(url), loggable_url(source) if source else None)


def brief(entry: dict) -> tuple[Any, ...]:
    args = entry["args"]
    return (
        entry["status"],
        args.get("capability"),
        args.get("method"),
        args.get("url"),
        args.get("redirect_from"),
    )


def holds_values(url: str) -> bool:
    """Whether a logged URL carries a query value rather than only parameter names."""
    return "=" in urlsplit(url).query


class Gate:
    """One browser, the four origins and the real tools."""

    def __init__(
        self,
        ctx: Any,
        tools: dict,
        page: Any,
        servers: list[Server],
        *,
        guarantee: str,
        hop_delay: float,
    ) -> None:
        self.ctx, self.tools, self.page = ctx, tools, page
        self.servers = servers
        self.site, self.peer, self.away, self.away2 = servers
        self.guarantee = guarantee
        self.hop_delay = hop_delay
        self.mark = 0  # audit rows before this one belong to earlier cases
        self.refusals = 0  # the guard's count when the case began

    # -- plumbing ---------------------------------------------------------------------------

    @property
    def start_url(self) -> str:
        return self.site.url("/start")

    def hop(self, status: int, to: str) -> str:
        """A URL on SITE that answers ``status`` with ``Location: to``."""
        return self.site.url("/hop?" + urlencode({"status": status, "to": to}))

    @staticmethod
    def bounce(server: Server, to: str, status: int = 302) -> str:
        """A URL on ``server`` that sends the tab on to ``to``."""
        return server.url("/bounce?" + urlencode({"status": status, "to": to}))

    def rows(self) -> list[dict]:
        """The guard's rows since the case began."""
        return [r for r in audit_rows(self.ctx)[self.mark :] if r["tool"] == "navigation"]

    def trail(self) -> list[tuple[Any, ...]]:
        return [brief(entry) for entry in self.rows()]

    def expect_trail(self, message: str, *acceptable: list[tuple[Any, ...]]) -> None:
        got = self.trail()
        expect(got in acceptable, message, got)

    def refused_trails(
        self, head: list, capability: str, method: str, url: str, source: str
    ) -> list[list]:
        """The trails a refused hop may leave: ``denied`` then ``stopped``.

        Under ``before`` a hop refused before it left has nothing to stop, so ``denied`` alone
        is acceptable too.
        """
        denied = trail_row("denied", capability, method, url, source)
        stopped = trail_row("stopped", capability, method, url, source)
        trails = [[*head, denied, stopped]]
        if self.guarantee == "before":
            trails.append([*head, denied])
        return trails

    def approve(self, *servers: Server) -> None:
        """Approve hops to ``servers`` from the page the tab stands on, as a user's yes would."""
        initiator = parse_origin(self.page.url)
        for server in servers:
            self.ctx.perms.grant(
                self.ctx.guard.session_key,
                parse_origin(server.base),
                Capability.NAVIGATE,
                initiator=initiator,
            )

    @staticmethod
    def origin(server: Server) -> str:
        """How the guard names a server's origin: in ``redirected_to`` and in the audit rows."""
        return parse_origin(server.base).describe()

    def expect_named(self, label: str, reply: dict, server: Server) -> None:
        """A refused redirect names the origin it was refused for: the agent asked for another
        URL, so only this tells it what to ask for. The guard holds the same string."""
        origin = self.origin(server)
        expect(
            reply.get("redirected_to") == origin
            and origin in reply.get("reason", "")
            and self.ctx.guard.refused_hop == origin,
            f"{label}: the refusal names the origin the site redirected to",
            (reply.get("redirected_to"), reply.get("reason"), self.ctx.guard.refused_hop),
        )

    async def go(self, url: str, **kwargs: Any) -> dict:
        return await self.tools["navigate"](url=url, **kwargs)

    @staticmethod
    async def until(predicate: Any, what: str, deadline_s: float = DEADLINE_S) -> None:
        deadline = time.monotonic() + deadline_s
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out after {deadline_s:.0f}s waiting for {what}")
            await asyncio.sleep(POLL_S)

    async def lands_on(self, url: str) -> None:
        await self.until(lambda: self.page.url == url, f"the tab to land on {url}")
        await self.page.wait_for_load_state("domcontentloaded")

    async def refused_soon(self) -> None:
        await self.until(lambda: self.ctx.guard.refusals > self.refusals, "the hop to be refused")

    async def settle(self) -> None:
        """Every refused hop's stop has finished, and a destination holding its answer back has
        had the time to give it: whatever should have been cancelled has been, or has landed."""
        await asyncio.wait_for(self.ctx.guard.settled(), DEADLINE_S)
        held = max(server.delay_s for server in self.servers)
        latest = max((a.at for s in self.servers for a in s.arrivals()), default=time.monotonic())
        wait = latest + held + QUIET_MARGIN_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def reset(self) -> None:
        """Every case starts alike: no grants, no cookies but AWAY's own, the tab on SITE.

        Quiet first: a stop from the last case that lands late would cancel this one's load.
        """
        ctx = self.ctx
        await asyncio.wait_for(ctx.guard.settled(), DEADLINE_S)
        ctx.collab.takeover = False
        ctx.guard._mode = "enforce"  # observe mode is set by its own case, on the live guard
        ctx.perms.revoke_all()
        for server in (self.away, self.away2):
            server.delay_s = self.hop_delay
        await self.page.goto("about:blank")
        await self.page.context.clear_cookies()
        name, value = AWAY_COOKIE
        await self.page.context.add_cookies([{"name": name, "value": value, "url": self.away.base}])
        opened = await self.go(self.start_url, reason="redirect hop e2e", confirm=True)
        if opened.get("status") != "ok" or opened.get("url") != self.start_url:
            raise RuntimeError(f"could not start a case on SITE: {opened}")
        for server in self.servers:
            server.clear()
        self.mark = len(audit_rows(ctx))
        self.refusals = ctx.guard.refusals

    async def where(self) -> dict:
        return await self.tools["get_url"]()

    async def standing(self, label: str, url: str, marker: str) -> None:
        where = await self.where()
        text = (await self.tools["read_page"]()).get("text", "")
        expect(
            where.get("url") == url and marker in text,
            f"{label}: the tab never moved and is readable",
            (where.get("url"), text[:60]),
        )

    async def still_usable(self, label: str) -> None:
        again = await self.go(self.site.url("/landing"))
        expect(
            again.get("status") == "ok" and again.get("title") == "SITE LANDING GET",
            f"{label}: the next navigation works",
            again,
        )

    def reached(self, server: Server, what: str) -> list[Arrival]:
        """What reached the destination of a refused hop, judged under the chosen guarantee."""
        seen = server.arrivals()
        detail = [describe(a) for a in seen]
        if self.guarantee == "before":
            promise(not seen, f"{server.name} was never reached", detail)
            return seen
        expect(len(seen) <= 1, f"{server.name} saw at most one request", detail)
        if seen:
            known(f"{server.name} received {what} once ({detail[0]})")
        return seen

    async def case(self, name: str, scenario: Any, *args: Any) -> None:
        print(f"\n-- {name}")
        LEDGER.begin(name)
        try:
            await scenario(*args)
        except AssertionError as failure:
            fail(str(failure))

    # -- 1. every status: an approved hop passes, an unapproved one is refused ---------------

    async def approved_hop(self, status: int) -> None:
        label = f"{status} to an approved origin"
        await self.reset()
        self.approve(self.peer)
        landing = self.peer.url("/landing")
        via = self.hop(status, landing)
        reply = await self.go(via)  # no confirm: moving within SITE needs no approval
        expect(
            reply.get("status") == "ok" and reply.get("url") == landing,
            f"{label}: navigate lands on it",
            reply,
        )
        where = await self.where()
        expect(where.get("title") == "PEER LANDING GET", f"{label}: the tab stands on PEER", where)
        self.expect_trail(
            f"{label}: judged like a first request, allowed, with its source",
            [
                trail_row("allowed", "interact", "GET", via),
                trail_row("allowed", "navigate", "GET", landing, via),
            ],
        )
        seen = [(a.method, a.path) for a in self.peer.arrivals()]
        expect(seen == [("GET", "/landing")], f"{label}: PEER saw the hop once", seen)

    async def refused_hop(self, status: int) -> None:
        label = f"{status} to an unapproved origin"
        await self.reset()
        landing = self.away.url("/landing")
        via = self.hop(status, landing)
        reply = await self.go(via)
        await self.settle()
        expect(
            reply.get("status") == "blocked_by_policy" and reply.get("url") == self.start_url,
            f"{label}: navigate answers blocked_by_policy from where the tab stands",
            reply,
        )
        await self.standing(label, self.start_url, "SITE START")
        self.expect_named(label, reply, self.away)
        expect(self.ctx.guard.refusals == self.refusals + 1, f"{label}: counted as one refusal")
        head = [trail_row("allowed", "interact", "GET", via)]
        self.expect_trail(
            f"{label}: the trail holds denied, then stopped",
            *self.refused_trails(head, "navigate", "GET", landing, via),
        )
        hop_row = self.rows()[1]
        expect(
            hop_row["origin"] == self.origin(self.away)
            and hop_row["initiator"] == self.origin(self.site),
            f"{label}: aimed at AWAY, started from the page the tab stands on",
            (hop_row["origin"], hop_row["initiator"]),
        )
        self.reached(self.away, f"the {status} hop request")
        await self.still_usable(label)

    # -- 2. a hop that stays on its origin is not judged ---------------------------------------

    async def stays_put(self, status: int, spelling: str) -> None:
        label = f"{status} with a {spelling} Location on the same origin"
        await self.reset()
        landing = self.site.url("/landing")
        via = self.hop(status, "/landing" if spelling == "relative" else landing)
        reply = await self.go(via)  # no confirm: no prompt to answer
        expect(
            reply.get("status") == "ok" and reply.get("url") == landing,
            f"{label}: passes without a prompt",
            reply,
        )
        self.expect_trail(
            f"{label}: only the first request is judged, the hop leaves no row",
            [trail_row("allowed", "interact", "GET", via)],
        )
        expect(self.ctx.guard.refusals == self.refusals, f"{label}: nothing refused")

    # -- 3. chains -------------------------------------------------------------------------------

    async def chain_second_refused(self) -> None:
        label = "SITE -> approved PEER -> unapproved AWAY"
        await self.reset()
        self.approve(self.peer)
        landing = self.away.url("/landing")
        second = self.bounce(self.peer, landing)
        via = self.hop(302, second)
        reply = await self.go(via)
        await self.settle()
        expect(
            reply.get("status") == "blocked_by_policy" and reply.get("url") == self.start_url,
            f"{label}: refused at the second hop",
            reply,
        )
        await self.standing(label, self.start_url, "SITE START")
        self.expect_named(label, reply, self.away)
        head = [
            trail_row("allowed", "interact", "GET", via),
            trail_row("allowed", "navigate", "GET", second, via),
        ]
        self.expect_trail(
            f"{label}: PEER allowed, AWAY denied then stopped",
            *self.refused_trails(head, "navigate", "GET", landing, second),
        )
        seen = [a.path for a in self.peer.arrivals()]
        expect(seen == ["/bounce"], f"{label}: PEER saw its hop once", seen)
        expect(self.ctx.guard.refusals == self.refusals + 1, f"{label}: one refusal")
        self.reached(self.away, "the second hop request")
        await self.still_usable(label)

    async def chain_first_refused(self) -> None:
        label = "SITE -> unapproved AWAY -> approved PEER"
        await self.reset()
        self.approve(self.peer)
        second = self.bounce(self.away, self.peer.url("/landing"))
        via = self.hop(302, second)
        reply = await self.go(via)
        await self.settle()
        expect(
            reply.get("status") == "blocked_by_policy" and reply.get("url") == self.start_url,
            f"{label}: refused at the first hop",
            reply,
        )
        await self.standing(label, self.start_url, "SITE START")
        self.expect_named(label, reply, self.away)
        head = [trail_row("allowed", "interact", "GET", via)]
        self.expect_trail(
            f"{label}: AWAY denied then stopped, and nothing after it",
            *self.refused_trails(head, "navigate", "GET", second, via),
        )
        seen = [describe(a) for a in self.peer.arrivals()]
        expect(not seen, f"{label}: the third hop never happens, PEER saw nothing", seen)
        self.reached(self.away, "the first hop request")
        await self.still_usable(label)

    async def chain_all_approved(self) -> None:
        label = "SITE -> PEER -> AWAY2 -> SITE, all approved"
        await self.reset()
        self.approve(self.peer, self.away2)
        landing = self.site.url("/landing")
        third = self.bounce(self.away2, landing)
        second = self.bounce(self.peer, third)
        via = self.hop(302, second)
        reply = await self.go(via)
        expect(
            reply.get("status") == "ok" and reply.get("url") == landing,
            f"{label}: the chain passes and lands",
            reply,
        )
        seen = {s.name: [a.path for a in s.arrivals()] for s in (self.peer, self.away2)}
        expect(
            seen == {"PEER": ["/bounce"], "AWAY2": ["/bounce"]},
            f"{label}: each hop's server was asked once",
            seen,
        )
        self.expect_trail(
            f"{label}: every hop judged and allowed, each naming the one before",
            [
                trail_row("allowed", "interact", "GET", via),
                trail_row("allowed", "navigate", "GET", second, via),
                trail_row("allowed", "navigate", "GET", third, second),
                trail_row("allowed", "interact", "GET", landing, third),
            ],
        )

    # -- 4. cookies set on a 3xx apply ----------------------------------------------------------

    async def cookie_same_origin(self) -> None:
        label = "a cookie set on a same-origin 3xx"
        await self.reset()
        via = self.site.url("/cookie-hop")
        reply = await self.go(via)
        expect(reply.get("status") == "ok", f"{label}: navigate passes", reply)
        text = (await self.tools["read_page"]())["text"]
        expect("site_hop=site" in text, f"{label}: the page it redirected to shows it", text)
        names = {c["name"] for c in await self.page.context.cookies([self.site.base])}
        expect("site_hop" in names, f"{label}: is in the browser's cookies", sorted(names))
        self.expect_trail(
            f"{label}: judged once, the hop leaves no row",
            [trail_row("allowed", "interact", "GET", via)],
        )

    async def cookie_cross_origin(self) -> None:
        label = "a cookie set by an approved PEER hop"
        await self.reset()
        self.approve(self.peer)
        landing = self.peer.url("/whoami")
        via = self.hop(302, self.peer.url("/cookie-hop"))
        reply = await self.go(via)
        expect(
            reply.get("status") == "ok" and reply.get("url") == landing,
            f"{label}: lands on PEER",
            reply,
        )
        text = (await self.tools["read_page"]())["text"]
        expect("peer_hop=peer" in text, f"{label}: PEER's next page got it back", text)
        names = {c["name"] for c in await self.page.context.cookies([self.peer.base])}
        expect("peer_hop" in names, f"{label}: is in the browser's cookies", sorted(names))
        self.expect_trail(
            f"{label}: only the hop that changed origin is judged",
            [
                trail_row("allowed", "interact", "GET", via),
                trail_row("allowed", "navigate", "GET", self.peer.url("/cookie-hop"), via),
            ],
        )

    async def cookie_refused(self) -> None:
        label = "a cookie set by a refused AWAY hop"
        await self.reset()
        target = self.away.url("/cookie-hop")
        via = self.hop(302, target)
        reply = await self.go(via)
        await self.settle()
        expect(reply.get("status") == "blocked_by_policy", f"{label}: navigate refuses", reply)
        self.expect_named(label, reply, self.away)
        names = {c["name"] for c in await self.page.context.cookies([self.away.base])}
        expect(
            "away_hop" not in names and AWAY_COOKIE[0] in names,
            f"{label}: never reaches the browser's cookies (its own stays)",
            sorted(names),
        )
        self.reached(self.away, "the cookie hop request")

    # -- 5. a POST that is redirected ---------------------------------------------------------

    async def open_form(self, status: int, to: str) -> tuple[str, str]:
        """SITE/form, whose POST goes to SITE/post-hop, which answers ``status`` -> ``to``.

        Returns the form's URL and the URL the POST is sent to. Opening the form is itself a
        judged navigation, so the case starts counting (trail, arrivals) from here.
        """
        query = urlencode({"status": status, "to": to})
        form = self.site.url("/form?" + query)
        opened = await self.go(form)
        if opened.get("status") != "ok" or opened.get("url") != form:
            raise RuntimeError(f"could not open the form: {opened}")
        self.mark = len(audit_rows(self.ctx))
        self.site.clear()
        return form, self.site.url("/post-hop?" + query)

    async def submit(self, label: str, *, refused_to: Server | None = None) -> None:
        """The declared submission: the one-shot SUBMIT it buys pays for the first POST only.

        ``refused_to`` is the server the site's redirect sends the browser on to, when no
        approval covers it: the call then answers the refusal, and names that origin."""
        sent = await self.tools["type_text"](
            selector="#q",
            value=SECRET,
            submit=True,
            reason="redirect hop e2e: a declared submission",
            confirm=True,
        )
        if refused_to is None:
            expect(sent.get("status") == "ok", f"{label}: type_text(submit=True) is accepted", sent)
            return
        # The refused hop comes after the server's own answer, so it can land after the call
        # has stopped listening: then the reply is ok and the refusal is on the trail (checked
        # by the caller). When it did land in time, the call says so and names the origin.
        status = sent.get("status")
        expect(
            status in ("ok", "blocked_by_policy"),
            f"{label}: type_text(submit=True) is accepted or answers the refusal",
            sent,
        )
        if status == "blocked_by_policy":
            self.expect_named(label, sent, refused_to)

    async def post_stays_put(self) -> None:
        label = "POST 307 to the same origin"
        await self.reset()
        _form, posted = await self.open_form(307, "/landing")
        await self.submit(label)
        await self.lands_on(self.site.url("/landing"))
        where = await self.where()
        expect(where.get("title") == "SITE LANDING POST", f"{label}: re-sent there", where)
        sent = [(a.method, a.path, a.body) for a in self.site.arrivals()]
        expect(
            sent == [("POST", "/post-hop", f"q={SECRET}"), ("POST", "/landing", f"q={SECRET}")],
            f"{label}: the body reached both, once each",
            sent,
        )
        self.expect_trail(
            f"{label}: the POST is judged once and the hop needs no second approval",
            [trail_row("allowed", "submit", "POST", posted)],
        )
        expect(self.ctx.guard.refusals == self.refusals, f"{label}: nothing refused")

    async def post_307_refused(self) -> None:
        label = "POST 307 to an unapproved origin"
        await self.reset()
        landing = self.away.url("/landing")
        form, posted = await self.open_form(307, landing)
        await self.submit(label, refused_to=self.away)
        await self.refused_soon()
        await self.settle()
        await self.standing(label, form, "SITE FORM")
        head = [trail_row("allowed", "submit", "POST", posted)]
        self.expect_trail(
            f"{label}: the first POST spent the grant, the hop is denied then stopped",
            *self.refused_trails(head, "submit", "POST", landing, posted),
        )
        sender = self.rows()[1]["args"].get("cross_origin_send")
        expect(
            sender == self.origin(self.away),
            f"{label}: the row says the body was sent across origins",
            sender,
        )
        expect(
            self.ctx.guard.refused_hop == self.origin(self.away),
            f"{label}: the guard remembers the origin it refused",
            self.ctx.guard.refused_hop,
        )
        self.reached(self.away, "the 307 POST body")
        await self.still_usable(label)

    async def post_303_approved(self) -> None:
        label = "POST 303 to an approved origin"
        await self.reset()
        landing = self.peer.url("/landing")
        _form, posted = await self.open_form(303, landing)
        self.approve(self.peer)
        await self.submit(label)
        await self.lands_on(landing)
        where = await self.where()
        expect(where.get("title") == "PEER LANDING GET", f"{label}: became a GET", where)
        seen = [(a.method, a.path, a.body) for a in self.peer.arrivals()]
        expect(seen == [("GET", "/landing", "")], f"{label}: PEER got no body", seen)
        self.expect_trail(
            f"{label}: the POST, then the hop judged as a navigation",
            [
                trail_row("allowed", "submit", "POST", posted),
                trail_row("allowed", "navigate", "GET", landing, posted),
            ],
        )

    async def post_302_refused(self) -> None:
        label = "POST 302 to an unapproved origin"
        await self.reset()
        landing = self.away.url("/landing")
        form, posted = await self.open_form(302, landing)
        await self.submit(label, refused_to=self.away)
        await self.refused_soon()
        await self.settle()
        await self.standing(label, form, "SITE FORM")
        head = [trail_row("allowed", "submit", "POST", posted)]
        self.expect_trail(
            f"{label}: refused as a navigation, denied then stopped",
            *self.refused_trails(head, "navigate", "GET", landing, posted),
        )
        expect(
            self.ctx.guard.refused_hop == self.origin(self.away),
            f"{label}: the guard remembers the origin it refused",
            self.ctx.guard.refused_hop,
        )
        seen = self.reached(self.away, "the hop's GET (the POST body stayed behind)")
        expect(
            all(a.method == "GET" and not a.body for a in seen),
            f"{label}: the typed value did not leave, the 302 made the POST a GET",
            [describe(a) for a in seen],
        )
        await self.still_usable(label)

    # -- 6. observe, 7. takeover ---------------------------------------------------------------

    async def observed(self) -> None:
        label = "observe mode"
        await self.reset()
        self.ctx.guard._mode = "observe"
        landing = self.away.url("/landing")
        via = self.hop(302, landing)
        reply = await self.go(via)
        await asyncio.wait_for(self.ctx.guard.settled(), DEADLINE_S)
        expect(
            reply.get("status") == "ok" and reply.get("url") == landing,
            f"{label}: the hop to an unapproved origin lands",
            reply,
        )
        self.expect_trail(
            f"{label}: recorded as would_deny with its source, and nothing stopped",
            [
                trail_row("allowed", "interact", "GET", via),
                trail_row("would_deny", "navigate", "GET", landing, via),
            ],
        )
        expect(self.ctx.guard.refusals == self.refusals, f"{label}: no refusal counted")
        seen = [describe(a) for a in self.away.arrivals()]
        expect(len(seen) == 1, f"{label}: AWAY was asked once", seen)

    async def takeover(self) -> None:
        label = "takeover"
        await self.reset()
        landing = self.away.url("/landing")
        via = self.hop(302, landing)
        self.ctx.collab.takeover = True
        try:
            standing_down = await self.go(via)
            expect(
                standing_down.get("status") == "takeover_active",
                f"{label}: the agent's navigate stands down",
                standing_down,
            )
            await self.page.goto(via)  # the user's own navigation, through the browser
            await asyncio.wait_for(self.ctx.guard.settled(), DEADLINE_S)
        finally:
            self.ctx.collab.takeover = False
        expect(self.page.url == landing, f"{label}: the user's hop lands", self.page.url)
        self.expect_trail(
            f"{label}: both requests recorded as user_driven",
            [
                trail_row("user_driven", "interact", "GET", via),
                trail_row("user_driven", "navigate", "GET", landing, via),
            ],
        )
        expect(self.ctx.guard.refusals == self.refusals, f"{label}: nothing refused")

    # -- 8. the honest limit: a destination that answers before the stop can land -----------

    def outcome(self, seen: dict, head: list, landing: str) -> str:
        """What one attempt came to, from the trail and the tab - or ``inconsistent``."""
        stopped = [*head, *self.refused_trails([], "navigate", "GET", landing, seen["via"])[0]]
        blanked = [
            *head,
            trail_row("denied", "navigate", "GET", landing, seen["via"]),
            trail_row("not_stopped", "navigate", "GET", landing, seen["via"]),
        ]
        withheld = [*head, trail_row("denied", "navigate", "GET", landing, seen["via"])]
        if seen["trail"] == stopped and seen["tab"] == self.start_url:
            return "stopped"
        if seen["trail"] == blanked and seen["tab"] == "about:blank" and seen["reason"] == BLANKED:
            return "committed"
        if self.guarantee == "before" and seen["trail"] == withheld:
            return "withheld" if seen["tab"] == self.start_url else "inconsistent"
        return "inconsistent"

    async def no_delay(self) -> None:
        landing = self.away.url("/landing")
        via = self.hop(302, landing)
        head = [trail_row("allowed", "interact", "GET", via)]
        attempts: list[dict] = []
        for _ in range(NO_DELAY_ATTEMPTS):
            await self.reset()
            self.away.delay_s = 0.0  # a server that answers at once, like loopback or a LAN
            reply = await self.go(via)
            await asyncio.wait_for(self.ctx.guard.settled(), DEADLINE_S)
            await asyncio.sleep(QUIET_MARGIN_S)
            rows = self.rows()
            attempts.append(
                {
                    "via": via,
                    "reply": reply.get("status"),
                    "tab": self.page.url,
                    "trail": [brief(entry) for entry in rows],
                    "reason": rows[-1]["args"].get("reason") if rows else None,
                    "arrivals": len(self.away.arrivals()),
                    "refusals": self.ctx.guard.refusals - self.refusals,
                    "redirected_to": reply.get("redirected_to"),
                    "refused_hop": self.ctx.guard.refused_hop,
                    "reply_url": reply.get("url"),
                }
            )
            attempts[-1]["outcome"] = self.outcome(attempts[-1], head, landing)
            if attempts[-1]["outcome"] in ("committed", "withheld"):
                break
        outcomes = [a["outcome"] for a in attempts]
        label = "no delay"
        expect(all(a["refusals"] == 1 for a in attempts), f"{label}: each attempt is one refusal")
        away_origin = self.origin(self.away)
        expect(
            all(
                a["reply"] == "blocked_by_policy"
                and a["redirected_to"] == away_origin
                and a["refused_hop"] == away_origin
                and a["reply_url"] == a["tab"]
                for a in attempts
            ),
            "no delay: navigate always answers blocked_by_policy, naming AWAY, from where the "
            "tab ends (SITE when the stop won, about:blank when the page had committed)",
            [(a["reply"], a["redirected_to"], a["reply_url"], a["tab"]) for a in attempts],
        )
        expect(
            all(a["tab"] != landing for a in attempts),
            "no delay: the refused page is never left on screen",
            [a["tab"] for a in attempts],
        )
        expect(
            "inconsistent" not in outcomes,
            "no delay: the tab and the audit always agree "
            "(stopped = tab stayed; not_stopped = tab blanked, with the reason)",
            [(a["tab"], a["trail"][-1:], a["reason"]) for a in attempts],
        )
        if self.guarantee == "before":
            promise(
                all(a["arrivals"] == 0 for a in attempts),
                "no delay: AWAY was never reached",
                [a["arrivals"] for a in attempts],
            )
            promise(
                all(a["tab"] == self.start_url for a in attempts),
                "no delay: the tab was never moved",
                [a["tab"] for a in attempts],
            )
        else:
            expect(
                all(a["arrivals"] <= 1 for a in attempts),
                "no delay: AWAY saw at most one request per attempt",
                [a["arrivals"] for a in attempts],
            )
            committed = [a for a in attempts if a["outcome"] == "committed"]
            reached = sum(a["arrivals"] for a in attempts)
            if committed:
                known(
                    f"no delay: AWAY received the hop request and answered before the stop "
                    f"landed ({len(committed)} of {len(attempts)} attempts): the refused page "
                    f'committed, then the tab was blanked (audit: denied, not_stopped "{BLANKED}")'
                )
            else:
                known(
                    f"no delay: AWAY received the hop request ({reached} of {len(attempts)} "
                    f"attempts); the stop won every race this time - nothing guarantees it"
                )
        await self.still_usable("no delay")

    # -- 9. what the trail keeps ----------------------------------------------------------------

    async def hygiene(self) -> None:
        label = "a hop whose URL carries query values"
        await self.reset()
        self.approve(self.peer)
        landing = self.peer.url("/landing?" + urlencode({"token": "hunter2", "page": "private"}))
        via = self.hop(302, landing)
        reply = await self.go(via)
        expect(reply.get("status") == "ok", f"{label}: passes", reply)
        entries = self.rows()
        expect(
            [brief(entry) for entry in entries]
            == [
                trail_row("allowed", "interact", "GET", via),
                trail_row("allowed", "navigate", "GET", landing, via),
            ],
            f"{label}: the rows keep the parameter names and drop the values",
            [brief(entry) for entry in entries],
        )
        text = json.dumps(entries)
        expect(
            not any(word in text for word in ("hunter2", "private", "status=302", "to=http")),
            f"{label}: no query value in them",
            text,
        )
        every = [r for r in audit_rows(self.ctx) if r["tool"] == "navigation"]
        urls = [r["args"].get(key, "") for r in every for key in ("url", "redirect_from")]
        expect(
            every and not any(holds_values(url) for url in urls),
            f"all {len(every)} navigation rows of this run hold names, never values",
            [url for url in urls if holds_values(url)],
        )
        refusals = [r for r in every if r["status"] in REFUSED]
        expect(
            refusals and all(r["args"].get("redirect_from") for r in refusals),
            f"every one of the {len(refusals)} refusal rows names the hop it came from",
            [brief(r) for r in refusals if not r["args"].get("redirect_from")],
        )
        expect(
            not any(r["status"] == "guard_error" for r in every),
            "the guard never failed on its own (no guard_error row)",
        )
        expect(
            SECRET not in ctx_audit_text(self.ctx),
            "the typed value is nowhere in the audit trail",
        )

    # -- 10. a refusal that is not a redirect ---------------------------------------------------

    async def refused_without_redirect(self) -> None:
        label = "a page that sends itself away"
        await self.reset()
        landing = self.away.url("/landing")
        hop_reply = await self.go(self.hop(302, landing))
        await self.settle()
        expect(
            hop_reply.get("redirected_to") == self.origin(self.away),
            f"{label}: first, a refused hop leaves its origin on the guard",
            hop_reply,
        )
        self.away.clear()
        self.mark = len(audit_rows(self.ctx))
        self.refusals = self.ctx.guard.refusals
        via = self.site.url("/js-away?" + urlencode({"to": landing}))
        reply = await self.go(via)
        expect(
            reply.get("status") == "blocked_by_policy" and reply.get("url") == via,
            f"{label}: navigate answers blocked_by_policy from where the tab stands",
            reply,
        )
        expect(
            "redirected_to" not in reply and self.ctx.guard.refused_hop == "",
            f"{label}: no redirect was refused, so the reply names none and the guard forgot",
            (reply.get("redirected_to"), self.ctx.guard.refused_hop),
        )
        expect(self.ctx.guard.refusals == self.refusals + 1, f"{label}: counted as one refusal")
        self.expect_trail(
            f"{label}: a first request, refused with no source and nothing to stop",
            [
                trail_row("allowed", "interact", "GET", via),
                trail_row("denied", "navigate", "GET", landing),
            ],
        )
        seen = [describe(a) for a in self.away.arrivals()]
        expect(not seen, f"{label}: AWAY saw nothing, the route refused it before it left", seen)
        await self.still_usable(label)

    # -- the run --------------------------------------------------------------------------------

    async def run(self) -> None:
        for status in HOP_STATUSES:
            await self.case(f"1. {status}: approved origin", self.approved_hop, status)
            await self.case(f"1. {status}: unapproved origin", self.refused_hop, status)
        for status in HOP_STATUSES:
            for spelling in ("relative", "absolute"):
                await self.case(
                    f"2. {status}: {spelling} Location", self.stays_put, status, spelling
                )
        await self.case("3a. approved, then unapproved", self.chain_second_refused)
        await self.case("3b. unapproved, then approved", self.chain_first_refused)
        await self.case("3c. three hops, all approved", self.chain_all_approved)
        await self.case("4a. cookie, same-origin hop", self.cookie_same_origin)
        await self.case("4b. cookie, approved PEER hop", self.cookie_cross_origin)
        await self.case("4c. cookie, refused AWAY hop", self.cookie_refused)
        await self.case("5a. POST 307, same origin", self.post_stays_put)
        await self.case("5b. POST 307, unapproved", self.post_307_refused)
        await self.case("5c. POST 303, approved", self.post_303_approved)
        await self.case("5d. POST 302, unapproved", self.post_302_refused)
        await self.case("6. observe mode", self.observed)
        await self.case("7. takeover", self.takeover)
        await self.case("8. no delay (the honest limit)", self.no_delay)
        await self.case("9. audit hygiene", self.hygiene)
        await self.case("10. a refusal that is not a redirect", self.refused_without_redirect)


def ctx_audit_text(ctx: Any) -> str:
    return ctx.config.audit_path.read_text()


def report(mode: str, cases: list[Case]) -> None:
    width = max(len(case.name) for case in cases)
    print(f"\n[{mode}] cases")
    for case in cases:
        verdict = "FAIL" if case.failures else "ok"
        print(
            f"  {verdict:<4} {case.name:<{width}}  {case.passed:>3} PASS  {case.known:>2} KNOWN"
            f"  {len(case.failures):>2} FAIL"
        )
    for case in cases:
        for failure in case.failures:
            print(f"  FAIL {case.name}: {failure}")
    print(
        f"[{mode}] {sum(c.passed for c in cases)} PASS, {sum(c.known for c in cases)} KNOWN, "
        f"{sum(len(c.failures) for c in cases)} FAIL"
    )


async def run_mode(
    servers: list[Server],
    *,
    headless: bool,
    driver: str,
    guarantee: str,
    relay: bool,
    hop_delay: float,
) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-hop-{mode}-", ignore_cleanup_errors=True)
    first = len(LEDGER.cases)
    LEDGER.begin(f"{mode}: setup")
    ctx, tools = await build_tools(Path(tmp.name), headless=headless)
    ctx.config.driver = driver
    if relay:
        install_relay(ctx)
    print(
        f"\n[{mode}] driver={driver} guarantee={guarantee} relay={relay} "
        f"hop_delay={hop_delay}s data_dir={tmp.name}"
    )
    try:
        guard_file = Path(inspect.getfile(NavigationGuard)).resolve()
        expect(
            guard_file.is_relative_to(REPO),
            "the gate runs this checkout's guard, not another tree's",
            guard_file,
        )
        opened = await tools["open_browser"]()
        expect(opened.get("status") == "ok", "browser launches", opened)
        expect(
            driver == "auto" or opened.get("driver") == driver,
            f"the driver is {opened.get('driver')}",
            opened,
        )
        page_ = await ctx.session.page()
        page_.context.set_default_timeout(DRIVER_TIMEOUT_MS)
        gate = Gate(ctx, tools, page_, servers, guarantee=guarantee, hop_delay=hop_delay)
        await gate.run()
    finally:
        await ctx.session.stop()
        tmp.cleanup()
        report(mode, LEDGER.cases[first:])


async def async_main(args: argparse.Namespace) -> bool:
    servers = [
        Server("SITE", "127.0.0.1"),
        Server("PEER", "127.0.0.1"),
        Server("AWAY", "localhost", args.hop_delay),
        Server("AWAY2", "localhost", args.hop_delay),
    ]
    try:
        for headless in (True, False):
            if (headless and args.headful_only) or (not headless and args.headless_only):
                continue
            await run_mode(
                servers,
                headless=headless,
                driver=args.driver,
                guarantee=args.guarantee,
                relay=args.relay,
                hop_delay=args.hop_delay,
            )
    finally:
        for server in servers:
            server.close()
    return any(case.failures for case in LEDGER.cases)


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    parser.add_argument("--driver", choices=["auto", "playwright", "patchright"], default="auto")
    parser.add_argument(
        "--guarantee",
        choices=["after", "before"],
        default=None,
        help="after: a refused hop reaches its destination at most once (what the route "
        "backend promises); before: it reaches nothing (what an interceptor that sees hops "
        "before they leave must meet, the cdp backend). Default: follows LYRA_BROWSER_GUARD",
    )
    parser.add_argument(
        "--hop-delay",
        type=float,
        default=HOP_DELAY_S,
        help="seconds AWAY and AWAY2 hold back every answer; keep it well above the stop's "
        f"latency (default {HOP_DELAY_S})",
    )
    parser.add_argument(
        "--relay",
        action="store_true",
        help="swap in the reference interceptor (route.fetch + route.fulfill) to show the "
        "'before' assertions can be met; needs --guarantee before",
    )
    args = parser.parse_args()
    if args.guarantee is None:
        args.guarantee = "before" if os.environ.get("LYRA_BROWSER_GUARD") == "cdp" else "after"
    if args.relay and args.guarantee != "before":
        parser.error("--relay refuses before it sends, so it only fits --guarantee before")
    failed = asyncio.run(async_main(args))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
