#!/usr/bin/env python3
"""Real-sites gate: the structural-perception path against the live web.

Promoted from the 2026-08-22 soak. The soak counted what the permission layer
refused while an agent browsed real sites; this gate keeps that count and adds the path an
agent takes now: read the page as a tree, then click a link by the ``aria-ref=`` the tree
printed. For every site it does what an agent would:

1. ``navigate`` (``confirm=True``), ``read_page(mode="tree")``, ``read_page`` text;
2. take the first same-origin link on the tree's ``aria-ref=`` lines and click it with
   ``click(selector="aria-ref=eN")`` - no CSS, no text selector;
3. record success, judged navigations (audit rows with ``tool == "navigation"``), blocked
   ones (``denied`` / ``would_deny`` / ``guard_error``), tree elements and characters, text
   characters and elapsed seconds.

A site succeeds when the tree carries refs and elements, the text is not empty and - if the
tree names an internal link - the ref click answers ``ok`` and the page really moves. A
site whose tree names none (example.com's only link leaves the site) succeeds with
``clicked=false``; the baseline pins that as well, so a link picker that silently finds
nothing cannot pass. ``refs: false`` and a failed ref click are reported as findings.

The numbers are compared with ``realsites_baseline.json``. ``success``, ``clicked`` and
``blocked`` must match exactly; ``navigations`` and the sizes may drift by 10% of the
baseline plus a small absolute slack, because the sites are live. A site that cannot be
reached at all (DNS, connection, TLS, timeout, HTTP 5xx/429) is SKIP with its reason, never
FAIL - unless the guard refused a navigation while it waited: then the site did answer and
the hang is ours (a regression, see ``verify_scriptredirect_e2e.py``).
neverssl.com sends the page elsewhere before DOMContentLoaded: ``navigate`` answers
``blocked_by_policy``, so the page is never read and the baseline pins ``success: false``.
The gate fails when fewer than ``--min-sites`` ran. ``local-embed+form`` is a loopback
control (an embed, an internal link, a form that is never submitted) needing no network.
Exit status: 0 when the gate holds, 1 when it does not, 2 for a bad command line.

Nothing here logs in or submits anything: a temporary profile, GET navigations only.

    .venv/bin/python scripts/verify_realsites.py
    .venv/bin/python scripts/verify_realsites.py --sites iana.org,MDN --retries 2
    .venv/bin/python scripts/verify_realsites.py --update-baseline
    xvfb-run -a .venv/bin/python scripts/verify_realsites.py --headful
    <venv with patchright>/bin/python scripts/verify_realsites.py --driver patchright
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urljoin, urlparse, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, free_port  # noqa: E402

BASELINE = Path(__file__).with_name("realsites_baseline.json")


class Site(NamedTuple):
    name: str
    url: str


LOCAL = "local-embed+form"

# The soak's sites, same labels (they are the baseline's keys). Dead ones are dropped
# here and from the baseline, never silently skipped for good.
REAL_SITES = (
    Site("example.com", "https://example.com/"),
    Site("iana.org", "https://www.iana.org/"),
    Site("w3.org", "https://www.w3.org/"),
    Site("python.org", "https://www.python.org/"),
    Site("MDN", "https://developer.mozilla.org/en-US/docs/Web/HTTP"),
    Site("GitHub", "https://github.com/microsoft/playwright"),
    Site("HackerNews", "https://news.ycombinator.com/"),
    Site("Wikipedia", "https://en.wikipedia.org/wiki/HTTP"),
    Site("httpbin", "https://httpbin.org/"),
    Site("neverssl", "http://neverssl.com/"),
)
SITE_NAMES = (LOCAL, *(site.name for site in REAL_SITES))

# What the loopback control serves: an embed from another origin, an internal link, a
# button and a form. The gate never submits the form; it is there for the tree to list.
LOCAL_OUTER = """<!doctype html><title>local-embed</title><h1>outer</h1>
<iframe src="http://localhost:{inner}/" width="220" height="90"></iframe>
<a href="/second">internal</a>
<button type="button" onclick="document.title='toggled'">toggle</button>
<form action="/submitted" method="post"><input name="q" value="v">
<button>send</button></form>"""

IDLE_S = 8.0  # how long to wait for the network to go quiet; a chatty site is read as it stands
STABLE_STEP_S = 0.5
STABLE_MAX_S = 6.0  # how long to wait for the tree to hold still; a live feed is read anyway
RETRY_PAUSE_S = 2.0
GRACE_S = 10.0  # beyond the driver's own deadline, so the driver is the one that reports it
BLANK_TRIES = 4  # resets of the window between sites, see `blank`
MAX_TREE_PAGES = 5  # pages of the tree read while looking for an internal link

BLOCKED = frozenset({"denied", "would_deny", "guard_error"})
EXACT = ("success", "clicked", "blocked")
RELATIVE = 0.10
SLACK = {"navigations": 1, "tree_elements": 3, "tree_chars": 150, "text_chars": 150}

# A link to one of these is a file, not a page: clicking it starts a download or opens a
# viewer, which says nothing about perception.
NOT_PAGES = tuple(
    (
        ".pdf .zip .gz .tgz .bz2 .xz .7z .tar .exe .dmg .pkg .msi .deb .rpm .iso .apk "
        ".xml .rss .atom .json .csv .txt .ico .png .jpg .jpeg .gif .svg .webp .mp3 .mp4 .woff"
        " .woff2"
    ).split()
)

# What a failed navigation says when the site could not be reached at all.
UNREACHABLE = (
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION",
    "ERR_TIMED_OUT",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_ADDRESS_UNREACHABLE",
    "ERR_NETWORK",
    "ERR_PROXY",
    "ERR_TUNNEL",
    "ERR_EMPTY_RESPONSE",
    "ERR_SSL",
    "ERR_CERT",
    "Timeout",
)

# ``aria-ref=f2e16 link "Hacker News" [expanded] -> news``; the name is a JSON string.
LINK_LINE = re.compile(
    r'aria-ref=(?P<ref>\w+) link(?: "(?:[^"\\]|\\.)*")?(?: \[[^\]]*\])* -> (?P<href>.+)'
)


class Handler(BaseHTTPRequestHandler):
    inner_port = 0

    def _send(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(LOCAL_OUTER.format(inner=self.inner_port))
        elif path == "/second":
            self._send("<!doctype html><title>local-second</title><h1>second</h1>")
        else:
            self.send_error(404)

    def log_message(self, *_args: object) -> None:
        pass


class InnerHandler(Handler):
    def do_GET(self) -> None:  # noqa: N802
        self._send("<!doctype html><title>inner</title>inner")


class Unreachable(Exception):
    """The site could not be reached at all: a skip, not a failure."""


class Failed(Exception):
    """The site was reached and something the gate checks did not hold."""

    def __init__(self, reason: str, *, finding: bool = False) -> None:
        super().__init__(reason)
        self.finding = finding


@dataclass
class Result:
    name: str
    url: str
    outcome: str = "fail"  # "ok" | "fail" | "skip"
    reason: str = ""
    finding: bool = False  # a defect, not the network: refs: false, a failed ref click, a hang
    clicked: bool = False
    link: str = ""
    http_status: int | None = None
    navigations: int = 0
    blocked: int = 0
    blocked_detail: list[str] = field(default_factory=list)
    tree_elements: int = 0
    tree_chars: int = 0
    text_chars: int = 0
    elapsed_s: float = 0.0
    attempts: int = 1
    notes: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.outcome == "ok"

    def entry(self) -> dict:
        """What the baseline keeps of this run."""
        return {
            "success": self.success,
            "clicked": self.clicked,
            "navigations": self.navigations,
            "blocked": self.blocked,
            "tree_elements": self.tree_elements,
            "tree_chars": self.tree_chars,
            "text_chars": self.text_chars,
            "elapsed_s": round(self.elapsed_s, 1),
        }


def audit_rows(ctx) -> list[dict]:
    path = ctx.config.audit_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def describe(exc: BaseException) -> str:
    """The first line of an error, which is the part a person reads."""
    lines = str(exc).splitlines()
    return (lines[0] if lines else type(exc).__name__)[:140]


def unreachable(exc: BaseException) -> bool:
    """Whether a failed navigation says the site could not be reached at all."""
    return isinstance(exc, TimeoutError) or any(marker in str(exc) for marker in UNREACHABLE)


def document_key(url: str) -> tuple[str, str, str, str]:
    """Which document a URL names: a fragment is the same document, an empty path is ``/``."""
    parts = urlsplit(url)
    return parts.scheme, parts.netloc.lower(), parts.path or "/", parts.query


def pick_link(tree_text: str, page_url: str) -> tuple[str, str] | None:
    """The first tree line naming a link to another page of the same origin.

    Returns ``(ref, absolute url)``. The tree prints hrefs as the page wrote them
    (``news``, ``/about``, ``vote?id=1``), so they are resolved against the page first.
    """
    here = urlsplit(page_url)
    for line in tree_text.splitlines():
        found = LINK_LINE.fullmatch(line)
        if found is None:
            continue
        target = urljoin(page_url, found["href"])
        parts = urlsplit(target)
        if (parts.scheme, parts.netloc.lower()) != (here.scheme, here.netloc.lower()):
            continue  # another origin, a mailto:, a javascript: ...
        if document_key(target) == document_key(page_url):
            continue  # the page itself, or one of its own anchors
        if parts.path.lower().endswith(NOT_PAGES):
            continue
        return found["ref"], target
    return None


async def moved_from(ctx, before: str, timeout: float) -> str:
    """The URL the session is on once it is a different document, or "" if it never is.

    The page is asked each time: a link that opens a new tab makes that tab current.
    """
    deadline = time.monotonic() + timeout
    key = document_key(before)
    while True:
        url = (await ctx.session.page()).url
        if url not in ("", "about:blank") and document_key(url) != key:
            return url
        if time.monotonic() >= deadline:
            return ""
        await asyncio.sleep(0.1)


async def settled(tools) -> None:
    """Let the page finish drawing itself, as an agent is told to with ``wait_for``.

    A site that renders after it loads is a different page a second later: httpbin has 6
    tree elements at DOMContentLoaded and 40 once Swagger UI is up, Wikipedia drops from
    1325 to 1069. Sizes only compare once the page has stopped changing. Neither wait is a
    check - a site that never goes quiet is simply read as it stands.
    """
    await tools["wait_for"](load_state="networkidle", timeout_ms=int(IDLE_S * 1000))
    seen = None
    deadline = time.monotonic() + STABLE_MAX_S
    while time.monotonic() < deadline:
        count = (await tools["read_page"](mode="tree", max_chars=0)).get("elements")
        if count == seen:
            return
        seen = count
        await asyncio.sleep(STABLE_STEP_S)


async def exercise(ctx, tools, site: Site, res: Result, timeout: float) -> None:
    """The agent's path through one site. Raises Unreachable or Failed; returns on success."""
    try:
        nav = await asyncio.wait_for(
            tools["navigate"](url=site.url, reason="real-sites gate", confirm=True),
            timeout + GRACE_S,
        )
    except TimeoutError:
        raise Unreachable(f"no answer after {timeout + GRACE_S:.0f}s") from None
    except Exception as exc:  # noqa: BLE001 - classified below; anything else is a real failure
        if unreachable(exc):
            raise Unreachable(describe(exc)) from exc
        raise
    res.http_status = nav.get("http_status")
    if nav.get("status") == "blocked_by_policy":
        raise Failed("navigate answered blocked_by_policy: the page sent itself elsewhere")
    if nav.get("status") != "ok":
        raise Failed(f"navigate answered {nav.get('status')}: {nav.get('hint') or nav}")
    if res.http_status and (res.http_status >= 500 or res.http_status == 429):
        raise Unreachable(f"http {res.http_status}")
    await settled(tools)

    async def read(what: str, **args) -> dict:
        try:
            got = await asyncio.wait_for(tools["read_page"](**args), timeout + GRACE_S)
        except TimeoutError:
            raise Failed(f"{what} still running after {timeout + GRACE_S:.0f}s") from None
        if "status" in got:  # a reading carries none; an envelope does
            raise Failed(f"{what} answered {got['status']}: {got.get('hint') or got}")
        return got

    tree = await read("read_page(tree)", mode="tree")
    res.tree_elements = int(tree.get("elements") or 0)
    res.tree_chars = int(tree.get("total_chars") or 0)
    if tree.get("refs") is not True:
        raise Failed(f"tree mode returned refs={tree.get('refs')!r}", finding=True)
    if not res.tree_elements:
        raise Failed("the tree lists no elements")
    res.text_chars = int((await read("read_page(text)")).get("total_chars") or 0)
    if not res.text_chars:
        raise Failed("the text read is empty")

    page_url = tree["url"]
    picked = pick_link(tree["text"], page_url)
    pages = 1
    while picked is None and tree.get("truncated") and pages < MAX_TREE_PAGES:
        tree = await read("read_page(tree, next page)", mode="tree", offset=tree["next_offset"])
        picked = pick_link(tree["text"], page_url)
        pages += 1
    if picked is None:
        res.reason = "no internal link in the tree"
        return

    ref, res.link = picked
    try:
        clicked = await asyncio.wait_for(
            tools["click"](selector=f"aria-ref={ref}"), timeout + GRACE_S
        )
    except TimeoutError:
        raise Failed(f"click via aria-ref={ref} still running", finding=True) from None
    except Exception as exc:  # noqa: BLE001 - a click that raised is a failed click
        raise Failed(f"click via aria-ref={ref} raised: {describe(exc)}", finding=True) from exc
    if clicked.get("status") != "ok":
        raise Failed(
            f"click via aria-ref={ref} answered {clicked.get('status')}: "
            f"{clicked.get('hint') or clicked}",
            finding=True,
        )
    if not await moved_from(ctx, page_url, timeout):
        raise Failed(
            f"click via aria-ref={ref} answered ok but the page stayed on {page_url} "
            f"(wanted {res.link}); matches={clicked.get('matches')!r} "
            f"clicked={clicked.get('clicked')!r}",
            finding=True,
        )
    res.clicked = True
    await settled(tools)


async def blank(ctx) -> None:
    """Back to a blank page from wherever the last site left the window.

    A site that failed to load leaves Chrome committing its error page for a moment, and a
    navigation started in that moment is interrupted by it: ask again a little later.
    """
    page = await ctx.session.page()
    for attempt in range(BLANK_TRIES):
        try:
            await page.goto("about:blank")
            return
        except Exception as exc:  # noqa: BLE001 - only that one race is retried
            if "interrupted by another navigation" not in str(exc) or attempt == BLANK_TRIES - 1:
                raise
            await asyncio.sleep(0.5)


async def visit(ctx, tools, site: Site, timeout: float) -> Result:
    """One attempt at one site, from the same starting point every time."""
    res = Result(site.name, site.url)
    started = time.monotonic()
    mark = len(audit_rows(ctx))
    try:
        # Nothing of the previous site is still loading, and no grant carries over:
        # `open_browser` is the boundary an agent marks between tasks.
        await blank(ctx)
        await tools["open_browser"]()
        mark = len(audit_rows(ctx))
        await exercise(ctx, tools, site, res, timeout)
        res.outcome = "ok"
    except Unreachable as exc:
        res.outcome, res.reason = "skip", str(exc)
    except Failed as exc:
        res.outcome, res.reason, res.finding = "fail", str(exc), exc.finding
    except Exception as exc:  # noqa: BLE001 - whatever the tools raised is the failure
        res.outcome, res.reason = "fail", f"{type(exc).__name__}: {describe(exc)}"
    res.elapsed_s = time.monotonic() - started
    judged = [row for row in audit_rows(ctx)[mark:] if row["tool"] == "navigation"]
    refused = [row for row in judged if row["status"] in BLOCKED]
    res.navigations = len(judged)
    res.blocked = len(refused)
    res.blocked_detail = [
        f"{row['status']} {(row.get('args') or {}).get('capability', '?')} "
        f"{str((row.get('args') or {}).get('url', ''))[:90]} (initiator {row.get('initiator')})"
        for row in refused[:3]
    ]
    if res.outcome == "skip" and refused:
        # The network did not stop it: the site answered (a page asked to go elsewhere), our
        # own guard turned that away, and the navigation waited on a load that never came.
        res.outcome, res.finding = "fail", True
        res.reason = f"{res.reason} - reached, but the guard refused: {res.blocked_detail[0]}"
    return res


async def run_site(ctx, tools, site: Site, retries: int, timeout: float) -> Result:
    notes: list[str] = []
    for attempt in range(1, retries + 2):
        res = await visit(ctx, tools, site, timeout)
        res.attempts = attempt
        # A refusal by the guard is a verdict, not network noise: asking again gets it again.
        # A finding is a verdict too: retrying a wrong-target or no-op click until it
        # happens to work would hide exactly what the gate exists to show.
        if res.outcome == "ok" or res.blocked or res.finding or attempt == retries + 1:
            break
        notes.append(f"attempt {attempt}: {res.outcome} - {res.reason}")
        await asyncio.sleep(RETRY_PAUSE_S)
    res.notes = notes
    return res


def problems(res: Result, base: dict | None) -> list[str]:
    """How a run differs from its baseline entry; empty when it is within tolerance."""
    if base is None:
        return ["no baseline entry for this site (run with --update-baseline)"]
    found = []
    for key in EXACT:
        got, want = getattr(res, key), base.get(key)
        if got != want:
            found.append(f"{key} = {got} (baseline {want})")
            if key == "blocked" and res.blocked_detail:
                found.append("  refused: " + "; ".join(res.blocked_detail))
    if res.success != base.get("success"):
        return found  # a run that stopped early has sizes that mean nothing
    for key, slack in SLACK.items():
        got, want = getattr(res, key), base.get(key)
        if not isinstance(want, int | float):
            found.append(f"baseline has no {key}")
            continue
        allowed = RELATIVE * want + slack
        if abs(got - want) > allowed:
            found.append(f"{key} = {got} (baseline {want}, allowed +-{allowed:.0f})")
    return found


def load_baseline(path: Path) -> dict:
    if not path.exists():
        return {"_meta": {}, "sites": {}}
    data = json.loads(path.read_text("utf-8"))
    data.setdefault("_meta", {})
    data.setdefault("sites", {})
    return data


def write_baseline(path: Path, old: dict, results: list[Result], meta: dict, *, pruned: bool):
    """Merge this run into the baseline: skipped sites keep their old entry."""
    sites = dict(old["sites"])
    for res in results:
        if res.outcome != "skip":
            sites[res.name] = res.entry()
    if pruned:  # a whole-list run: sites no longer in the list go
        sites = {name: sites[name] for name in SITE_NAMES if name in sites}
    else:
        sites = {name: sites[name] for name in (*SITE_NAMES, *sites) if name in sites}
    data = {"_meta": meta, "sites": sites}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def short(url: str, limit: int = 46) -> str:
    parts = urlsplit(url)
    local = parts._replace(scheme="", netloc="").geturl() if parts.path else url
    return local if len(local) <= limit else local[: limit - 1] + "…"


def table(results: list[Result], verdicts: dict[str, str]) -> None:
    head = (
        f"{'site':<17}{'verdict':<8}{'ok':<4}{'click':<6}{'nav':>4}{'blk':>4}"
        f"{'elems':>7}{'tree_ch':>9}{'text_ch':>9}{'secs':>7}  detail"
    )
    print("\n" + head)
    for res in results:
        verdict = verdicts[res.name]
        if res.outcome == "skip":
            cells = f"{'-':<4}{'-':<6}{'-':>4}{'-':>4}{'-':>7}{'-':>9}{'-':>9}"
        else:
            cells = (
                f"{'yes' if res.success else 'NO':<4}{'yes' if res.clicked else '-':<6}"
                f"{res.navigations:>4}{res.blocked:>4}{res.tree_elements:>7}"
                f"{res.tree_chars:>9}{res.text_chars:>9}"
            )
        detail = res.reason or (short(res.link) if res.link else "")
        tries = f" [{res.attempts} tries]" if res.attempts > 1 else ""
        print(f"{res.name:<17}{verdict:<8}{cells}{res.elapsed_s:>7.1f}  {detail}{tries}")


def choose(names: str | None) -> list[str]:
    if not names:
        return list(SITE_NAMES)
    wanted = [name.strip() for name in names.split(",") if name.strip()]
    by_lower = {name.lower(): name for name in SITE_NAMES}
    unknown = [name for name in wanted if name.lower() not in by_lower]
    if unknown:
        raise ValueError(f"unknown site(s) {unknown}; choose from {list(SITE_NAMES)}")
    picked = {by_lower[name.lower()] for name in wanted}
    return [name for name in SITE_NAMES if name in picked]  # the list's order, once each


async def async_main(args: argparse.Namespace, names: list[str], baseline: dict) -> int:
    need = min(args.min_sites, len(names))

    servers: list[ThreadingHTTPServer] = []
    by_name = {site.name: site for site in REAL_SITES}
    if LOCAL in names:
        inner_port, outer_port = free_port(), free_port()
        Handler.inner_port = inner_port
        for port, handler in ((inner_port, InnerHandler), (outer_port, Handler)):
            server = ThreadingHTTPServer(("127.0.0.1", port), handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
        by_name[LOCAL] = Site(LOCAL, f"http://127.0.0.1:{outer_port}/")
    sites = [by_name[name] for name in names]

    mode = "headful" if args.headful else "headless"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-realsites-{mode}-", ignore_cleanup_errors=True)
    ctx, tools = await build_tools(Path(tmp.name), headless=not args.headful)
    ctx.config.driver = args.driver
    results: list[Result] = []
    try:
        try:
            opened = await tools["open_browser"]()
        except ImportError as exc:  # --driver names a driver this environment does not have
            print(f"--driver {args.driver} is not usable here: {exc}")
            return 2
        if opened.get("status") != "ok":
            print(f"the browser did not open: {opened}")
            return 1
        meta = {
            "generated": datetime.now(UTC).strftime("%Y-%m-%d"),
            "mode": mode,
            "driver": opened.get("driver"),
            "browser": opened.get("browser"),
        }
        print(f"[{mode}] browser={meta['browser']} driver={meta['driver']} sites={len(sites)}")
        (await ctx.session.page()).context.set_default_timeout(int(args.timeout * 1000))
        for site in sites:
            res = await run_site(ctx, tools, site, args.retries, args.timeout)
            results.append(res)
            line = f"  {site.name:<17}{res.outcome:<5}{res.elapsed_s:>6.1f}s  {res.reason}"
            print(line, flush=True)
    finally:
        await ctx.session.stop()
        tmp.cleanup()
        for server in servers:
            server.shutdown()
            server.server_close()

    ran = [res for res in results if res.outcome != "skip"]
    skipped = [res for res in results if res.outcome == "skip"]
    verdicts: dict[str, str] = {}
    mismatches: dict[str, list[str]] = {}
    for res in results:
        if res.outcome == "skip":
            verdicts[res.name] = "SKIP"
        elif args.update_baseline:
            verdicts[res.name] = "SAVED"
        else:
            found = problems(res, baseline["sites"].get(res.name))
            verdicts[res.name] = "FAIL" if found else "PASS"
            if found:
                mismatches[res.name] = found
    table(results, verdicts)

    for res in results:
        for note in res.notes:
            print(f"note  {res.name}: {note}")
        for line in res.blocked_detail:
            print(f"blocked  {res.name}: {line}")
    for name, found in mismatches.items():
        for line in found:
            print(f"FAIL  {name}: {line}")
    findings = [res for res in results if res.finding and res.outcome == "fail"]
    if findings:
        print("\nFINDINGS (real, not network):")
        for res in findings:
            print(f"  {res.name}: {res.reason}")

    enough = len(ran) >= need
    print(
        f"\nran {len(ran)}/{len(results)} sites (need {need}); "
        f"{len(skipped)} skipped"
        + (": " + ", ".join(f"{r.name} ({r.reason})" for r in skipped) if skipped else "")
    )
    if args.update_baseline:
        write_baseline(args.baseline, baseline, results, meta, pruned=args.sites is None)
        print(f"baseline written: {args.baseline} ({len(ran)} sites updated)")
        for res in ran:
            if not res.success:
                print(f"WARNING  {res.name}: the baseline now records a FAILURE ({res.reason})")
        if not enough:
            print(f"FAIL  only {len(ran)} sites ran, need {need}")
        return 0 if enough else 1
    if not enough:
        print(f"FAIL  only {len(ran)} sites ran, need {need}: the network, not the code?")
    verdict = "PASS" if enough and not mismatches else "FAIL"
    print(f"gate: {verdict}")
    return 0 if verdict == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sites", help=f"comma-separated subset of: {', '.join(SITE_NAMES)}")
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="extra attempts for a site that skipped or failed (live sites vary between runs)",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="record this run instead of comparing it (sites that were skipped keep their entry)",
    )
    parser.add_argument("--min-sites", type=int, default=8, help="sites that must have run")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds per step")
    parser.add_argument("--driver", choices=["auto", "playwright", "patchright"], default="auto")
    parser.add_argument("--headful", action="store_true", help="needs a display (xvfb-run -a)")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args()
    if args.retries < 0:
        parser.error("--retries must not be negative")
    try:
        names = choose(args.sites)
    except ValueError as exc:
        parser.error(str(exc))
    baseline = load_baseline(args.baseline)
    if not args.update_baseline and not baseline["sites"]:
        parser.error(f"no baseline at {args.baseline}: run with --update-baseline")
    return asyncio.run(async_main(args, names, baseline))


if __name__ == "__main__":
    raise SystemExit(main())
