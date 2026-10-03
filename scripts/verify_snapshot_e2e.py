#!/usr/bin/env python3
"""Exercise ``read_page`` — tree mode, paging, regions, links — against a real browser.

Companion to ``verify_browser_e2e.py`` and ``verify_forms_e2e.py`` (same contract:
temporary profile, loopback site, not part of the unit suite, explicit gates). It reuses
that script's ``build_tools`` so the server is driven the same way.

What a *page* recorded is what is asserted, not what the tool said: every click goes
through the ``aria-ref=`` token copied out of the tree, and the fixture page tells the
server (``/hit``) and its own DOM which element received it. Both, because a driver
that evaluates in an isolated world (patchright) cannot read the page's JavaScript
globals but does share the DOM.

Run it under both drivers and both modes::

    .venv/bin/python scripts/verify_snapshot_e2e.py
    xvfb-run -a .venv/bin/python scripts/verify_snapshot_e2e.py --headful-only
    <venv with patchright>/bin/python scripts/verify_snapshot_e2e.py --driver patchright
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

# What the fixture server saw: element ids reported by /hit, and document paths.
HITS: list[str] = []
VISITS: list[str] = []

RECORD_JS = """<script>
function record(id) {
  document.body.dataset.last = String(id);
  fetch('/hit?i=' + encodeURIComponent(id), {keepalive: true});
}
</script>"""

PROSE = (
    "Ships in two to three business days. Returns are accepted within thirty days of "
    "delivery provided the item is unused and in its original packaging, and refunds go "
    "back to the original payment method once the parcel has been inspected. "
)


def _card(i: int) -> str:
    """Sixty links that a selector cannot tell apart: same text, no text, same label."""
    kind = i % 3
    if kind == 0:
        body, label = "Read more", ""
    elif kind == 1:
        body, label = (
            '<svg width="16" height="16" aria-hidden="true"><circle cx="8" cy="8" r="6"/></svg>',
            "",
        )
    else:
        body, label = (
            '<svg width="16" height="16" aria-hidden="true"><rect width="12" height="12"/></svg>',
            ' aria-label="Share"',
        )
    link = (
        f'<a class="card" data-i="{i}" href="/go/{i}"{label} '
        f"onclick=\"event.preventDefault(); record('{i}')\">{body}</a>"
    )
    return f"<article><p>{PROSE}</p>{link}</article>"


NAV = " ".join(
    f'<a href="/{name.lower()}" onclick="event.preventDefault(); record(\'nav-{name.lower()}\')">'
    f"{name}</a>"
    for name in ("Home", "Docs", "Pricing", "Blog", "About")
)

LINKS = f"""<!doctype html><meta charset="utf-8"><title>catalog</title>
<style>body {{ font: 14px sans-serif; margin: 8px }} article {{ margin: 6px 0 }}</style>
<a id="far" href="/far" style="position:absolute; top:7000px; left:0">Far away link</a>
<header>
  <nav aria-label="Main">
    {NAV}
  </nav>
  <input id="search" aria-label="Search" placeholder="Search the catalog">
</header>
<main>
  <h1>Catalog</h1>
  <p>{PROSE * 3}</p>
  <ul>{"".join(f"<li>Feature {n}: {PROSE}</li>" for n in range(10))}</ul>
  <label>Email <input id="email" type="email" value="me@example.com"></label>
  <label>Password <input id="pw" type="password" value="hunter2-e2e"></label>
  <select id="country" aria-label="Country"><option value="kr">Korea</option>
    <option value="us">United States</option></select>
  <section id="cards">{"".join(_card(i) for i in range(60))}</section>
  <p>
    <a href="/same">Same</a> <a href="/same">Same</a> <a href="/rel?x=1&y=2">Relative</a>
    <a href="/hidden-display" style="display:none">Hidden display</a>
    <span hidden><a href="/hidden-attr">Hidden attr</a></span>
    <a href="/hidden-vis" style="visibility:hidden">Hidden visibility</a>
    <a href="/hidden-zero" style="display:inline-block;width:0;height:0"></a>
  </p>
  <a id="real" href="/go/real">Go for real</a>
</main>
<footer>{"".join(f'<a href="/footer/{n}">Footer {n}</a> ' for n in range(20))}</footer>
<a id="near" href="/near" style="position:absolute; top:0; right:0">Near link</a>
{RECORD_JS}"""

MANY = (
    "<!doctype html><title>many</title><main>"
    + "".join(f'<a href="/m/{n}">Link {n}</a> ' for n in range(130))
    + "</main>"
)

DEEP = f"""<!doctype html><meta charset="utf-8"><title>deep</title>
<h1>Deep</h1>
<my-card id="host"></my-card>
<iframe id="fr" src="/frame" width="400" height="120"></iframe>
{RECORD_JS}
<script>
const root = document.getElementById('host').attachShadow({{mode: 'open'}});
root.innerHTML = '<button id="sb" type="button">Shadow buy</button> ' +
                 '<input id="sn" aria-label="Shadow name">';
root.getElementById('sb').addEventListener('click', () => record('shadow'));
</script>"""

FRAME = """<!doctype html><meta charset="utf-8"><title>frame</title>
<a id="fl" href="/frame-target" onclick="event.preventDefault(); record('frame')">Frame go</a>
<input id="fn" aria-label="Frame name">
<script>
function record(id) {
  document.body.dataset.last = id;
  fetch('/hit?i=' + id, {keepalive: true});
}
</script>"""

LONG_LINES = [
    f"Line {n:04d}: the quick brown fox jumps over the lazy dog — 한글 {n} 🎉 end"
    for n in range(400)
]
LONG = (
    '<!doctype html><meta charset="utf-8"><title>long</title><main>'
    + "".join(f"<p>{line}</p>" for line in LONG_LINES[:200])
    + '<section id="region"><h2>Region</h2><p>REGION-MARKER alpha beta gamma</p>'
    "<p>second region paragraph</p></section>"
    + "".join(f"<p>{line}</p>" for line in LONG_LINES[200:])
    + "<p>OUTSIDE-MARKER</p></main>"
)

PAGES = {"/links": LINKS, "/many": MANY, "/deep": DEEP, "/frame": FRAME, "/long": LONG}


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/hit":
            HITS.append(parse_qs(url.query).get("i", [""])[0])
            self.send_response(204)
            self.end_headers()
            return
        if url.path.startswith("/go/"):
            VISITS.append(url.path)
            self._send(f"<title>went {url.path[4:]}</title><h1>Went</h1>")
            return
        if url.path == "/":
            self._send("<title>home</title><h1>Snapshot E2E</h1>")
            return
        if url.path not in PAGES:
            self.send_error(404)
            return
        self._send(PAGES[url.path])

    def log_message(self, *_args: object) -> None:
        pass


async def _hit_after(before: int, timeout: float = 3.0) -> str | None:
    """The id the server heard about after ``before`` hits, or None if none came."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(HITS) > before:
            return HITS[-1]
        await asyncio.sleep(0.02)
    return None


def _ref_of(lines: list[str], pattern: str) -> str:
    """The ``aria-ref=...`` token of the first tree line matching ``pattern``."""
    for line in lines:
        if re.search(pattern, line):
            return line.split(" ", 1)[0]
    raise AssertionError(f"no tree line matches {pattern!r}: {lines[:12]}")


def _space(ref: str) -> str:
    """The part of ``aria-ref=f2e4`` before the element: which document the ref is from.

    Measured: the first document of a page hands out bare ``e4``; every document after
    it, and every iframe, gets its own ``f<n>`` prefix.
    """
    found = re.fullmatch(r"aria-ref=((?:f\d+)?)e\d+", ref)
    assert found, ref
    return found[1]


async def _resolves(page, ref: str) -> bool:
    """Whether ``ref`` still names an element.

    A ref from an earlier document is not a selector that matches nothing: the driver
    refuses it outright, which is the same answer to a caller.
    """
    try:
        return await page.locator(ref).count() > 0
    except Exception as exc:  # noqa: BLE001 — only the driver's refusal of a stale ref
        assert "aria-ref" in str(exc), exc
        return False


async def run_mode(base: str, *, headless: bool, driver: str) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-snapshot-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    ctx, tools = await build_tools(data_dir, headless=headless)
    ctx.config.driver = driver
    read = tools["read_page"]
    print(f"\n[{mode}] driver={driver} data_dir={data_dir}")
    try:
        opened = await tools["open_browser"]()
        expect(opened.get("status") == "ok", "browser launches", opened)
        print(f"      launched {opened.get('browser')} through {opened.get('driver')}")
        if driver != "auto":
            expect(opened.get("driver") == driver, f"the {driver} driver is the one in use", opened)

        # Reading asks nobody: no site has been approved yet.
        blank = await read(mode="tree")
        expect(
            blank.get("refs") is True and blank.get("elements") == 0 and blank.get("text") == "",
            "a tree read needs no permission and an empty page has an empty tree",
            blank,
        )

        entered = await tools["navigate"](url=base + "links", reason="snapshot E2E", confirm=True)
        expect(entered.get("title") == "catalog", "catalog page loads", entered)
        page = await ctx.session.page()

        # ---- tree: 60 links a text selector cannot tell apart ----------------------
        whole = await read(mode="tree", max_chars=10**7)
        lines = whole["text"].splitlines()
        expect(whole["mode"] == "tree" and whole["refs"] is True, "tree mode reports refs", whole)
        expect(
            whole["elements"] == len(lines) == whole["text"].count("\n"),
            "elements counts the lines of the whole tree",
            (whole["elements"], len(lines)),
        )
        expect(not whole["truncated"] and "next_offset" not in whole, "one page held it all")
        expect(all(line.startswith("aria-ref=") for line in lines), "every line leads with a ref")

        cards: dict[int, tuple[str, str]] = {}
        for line in lines:
            found = re.fullmatch(r'(aria-ref=\S+) link(?: "(.*)")? -> /go/(\d+)', line)
            if found:
                cards[int(found[3])] = (found[1], found[2] or "")
        expect(
            sorted(cards) == list(range(60)),
            "all 60 links listed with role and href",
            sorted(cards),
        )
        names = {name for _, name in cards.values()}
        expect(
            names == {"Read more", "", "Share"},
            "they really are duplicate, empty and identically labelled",
            names,
        )

        wrong: list[tuple[int, str | None, str | None]] = []
        for i, (ref, _) in sorted(cards.items()):
            before = len(HITS)
            clicked = await tools["click"](selector=ref)
            heard = await _hit_after(before)
            seen = await page.evaluate("document.body.dataset.last")
            if clicked.get("status") != "ok" or heard != str(i) or seen != str(i):
                wrong.append((i, heard, seen))
        expect(
            not wrong, "clicking each aria-ref hit exactly the link it was listed for", wrong[:5]
        )

        # ---- tree: values, states, other controls ----------------------------------
        for secret in ("hunter2-e2e", "me@example.com"):
            expect(secret not in whole["text"], f"input value {secret!r} is not in the tree")
        expect(
            re.search(r'textbox "Password"', whole["text"]) is not None,
            "the password field is listed, by label only",
        )
        email_ref = _ref_of(lines, r'textbox "Email"')
        typed = await tools["type_text"](selector=email_ref, value="typed@example.com")
        expect(typed.get("status") == "ok", "type_text accepts an aria-ref", typed)
        expect(
            await page.evaluate("document.getElementById('email').value") == "typed@example.com",
            "the value reached the field the ref named",
        )
        country_ref = _ref_of(lines, r'combobox "Country"')
        picked = await tools["select_option"](selector=country_ref, label="United States")
        expect(picked.get("status") == "ok", "select_option accepts an aria-ref", picked)
        expect(
            await page.evaluate("document.getElementById('country').value") == "us",
            "the option was chosen on the select the ref named",
        )

        # ---- order: what is on screen first ----------------------------------------
        near = next(i for i, line in enumerate(lines) if '"Near link"' in line)
        far = next(i for i, line in enumerate(lines) if '"Far away link"' in line)
        doc_far_first = await page.evaluate(
            "document.getElementById('far').compareDocumentPosition("
            "document.getElementById('near')) & Node.DOCUMENT_POSITION_FOLLOWING"
        )
        expect(doc_far_first, "fixture: the far link precedes the near one in the document")
        expect(
            near < whole["in_viewport"] <= far,
            "the on-screen link is listed before the one below the fold",
            (near, whole["in_viewport"], far),
        )

        # ---- sizes -----------------------------------------------------------------
        raw = await page.locator("body").aria_snapshot(mode="ai")
        html = await page.content()
        text = await page.locator("body").inner_text()
        print(
            f"      sizes (chars): html={len(html)} innerText={len(text)} "
            f"aria-snapshot={len(raw)} tree={whole['total_chars']} ({whole['elements']} elements)"
        )
        expect(
            whole["total_chars"] * 2 <= len(raw),
            "the tree is at most half the raw snapshot",
            (whole["total_chars"], len(raw)),
        )
        expect(whole["total_chars"] < len(html) / 4, "and a fraction of the page's HTML")

        # ---- paging the tree: whole lines, exact reassembly ------------------------
        # Read afresh: the clicks above scrolled the page, and what is on screen
        # decides the order, so the earlier read is no longer the page's tree.
        whole = await read(mode="tree", max_chars=10**7)
        parts, offset = [], 0
        while True:
            chunk = await read(mode="tree", max_chars=700, offset=offset)
            parts.append(chunk["text"])
            if not chunk["truncated"]:
                break
            offset = chunk["next_offset"]
        expect(len(parts) > 3, "the tree needed several pages", len(parts))
        expect("".join(parts) == whole["text"], "tree pages reassemble to the whole tree")
        expect(
            all(
                p.endswith("\n") and all(x.startswith("aria-ref=") for x in p.splitlines())
                for p in parts
            ),
            "no tree page cuts a line, so no ref is ever cut",
        )
        # Every page above was its own snapshot of the whole page. A ref handed out on
        # the very first read must still be the same element after all of them.
        early_ref = cards[42][0]
        before = len(HITS)
        paged = await tools["click"](selector=early_ref)
        expect(paged.get("status") == "ok", "a ref from the first read still clicks", paged)
        expect(await _hit_after(before) == "42", "after paging, it still names the same link")

        # ---- a region: fewer elements, the same refs ---------------------------------
        nav_ref = _ref_of(lines, r'link "Docs" -> /docs')
        region = await read(mode="tree", selector="nav")
        region_lines = region["text"].splitlines()
        expect(
            [line.split(" ", 1)[1] for line in region_lines]
            == [
                'link "Home" -> /home',
                'link "Docs" -> /docs',
                'link "Pricing" -> /pricing',
                'link "Blog" -> /blog',
                'link "About" -> /about',
            ],
            "a selector limits the tree to that region",
            region_lines,
        )
        expect(
            region_lines[1].split(" ", 1)[0] == nav_ref,
            "a region hands out the same refs the whole page did",
            (region_lines[1], nav_ref),
        )
        before = len(HITS)
        clicked = await tools["click"](selector=nav_ref)
        expect(clicked.get("status") == "ok", "a ref from a region clicks", clicked)
        expect(await _hit_after(before) == "nav-docs", "and hits the link it named")

        missing = {}
        for read_mode in ("text", "tree"):
            started = time.monotonic()
            missing[read_mode] = (
                await read(mode=read_mode, selector="#does-not-exist"),
                time.monotonic() - started,
            )
            answer, seconds = missing[read_mode]
            expect(
                answer.get("status") == "not_found" and seconds < 2,
                f"a missing region is not_found at once in {read_mode} mode",
                (answer, round(seconds, 2)),
            )

        # ---- links of the text view ------------------------------------------------
        plain = await read(max_chars=50)
        expect("links" not in plain, "links are opt-in", sorted(plain))
        with_links = await read(max_chars=50, links=True)
        found_links = with_links["links"]
        hrefs = [link["href"] for link in found_links]
        expect(
            all(href.startswith(base) for href in hrefs),
            "every href is absolute",
            [h for h in hrefs if not h.startswith(base)][:3],
        )
        expect(f"{base}rel?x=1&y=2" in hrefs, "a relative href was resolved", hrefs[:5])
        expect(
            not [h for h in hrefs if "/hidden-" in h],
            "hidden anchors (display, hidden attribute, visibility, zero size) are left out",
        )
        expect(
            [link for link in found_links if link["href"] == f"{base}same"]
            == [{"text": "Same", "href": f"{base}same"}],
            "a repeated text+href pair is listed once",
        )
        expect(
            len([h for h in hrefs if "/go/" in h]) == 61 and not with_links["links_truncated"],
            "every distinct visible link is there (60 cards and the real one)",
            len(hrefs),
        )
        expect(
            {"text": "", "href": f"{base}go/1"} in found_links,
            "an icon-only link is kept, with empty text",
        )
        expect(
            with_links["text"] == plain["text"]
            and with_links["total_chars"] == plain["total_chars"],
            "links do not change the text",
        )

        await tools["navigate"](url=base + "many", confirm=True)
        many = await read(max_chars=10, links=True)
        expect(
            len(many["links"]) == 100 and many["links_truncated"] is True,
            "links are capped at 100 and say so",
            (len(many["links"]), many["links_truncated"]),
        )
        expect(
            [link["text"] for link in many["links"]] == [f"Link {n}" for n in range(100)],
            "the cap keeps document order",
        )

        # ---- shadow root and iframe ------------------------------------------------
        await tools["navigate"](url=base + "deep", confirm=True)
        # navigate returns at domcontentloaded of the top document; the iframe may
        # still be loading, and a tree read is a picture of what is there now.
        for _ in range(200):
            if any(f.url.endswith("/frame") for f in page.frames):
                break
            await asyncio.sleep(0.05)
        await next(f for f in page.frames if f.url.endswith("/frame")).wait_for_load_state()
        deep = await read(mode="tree")
        deep_lines = deep["text"].splitlines()
        shadow_ref = _ref_of(deep_lines, r'button "Shadow buy"')
        frame_ref = _ref_of(deep_lines, r'link "Frame go"')
        expect(
            _space(frame_ref) != _space(shadow_ref),
            "the iframe's element has its own ref space; the shadow's shares the page's",
            (shadow_ref, frame_ref),
        )
        before = len(HITS)
        expect((await tools["click"](selector=shadow_ref)).get("status") == "ok", "shadow click")
        expect(await _hit_after(before) == "shadow", "the button in the open shadow root was hit")
        before = len(HITS)
        expect((await tools["click"](selector=frame_ref)).get("status") == "ok", "frame click")
        expect(await _hit_after(before) == "frame", "the link inside the iframe was hit")

        shadow_in = _ref_of(deep_lines, r'textbox "Shadow name"')
        frame_in = _ref_of(deep_lines, r'textbox "Frame name"')
        await tools["type_text"](selector=shadow_in, value="into shadow")
        await tools["type_text"](selector=frame_in, value="into frame")
        expect(
            await page.evaluate(
                "document.getElementById('host').shadowRoot.getElementById('sn').value"
            )
            == "into shadow",
            "type_text filled the input inside the shadow root",
        )
        frame = next(f for f in page.frames if f.url.endswith("/frame"))
        expect(
            await frame.evaluate("document.getElementById('fn').value") == "into frame",
            "type_text filled the input inside the iframe",
        )

        # ---- a ref is for the page it came from ------------------------------------
        await tools["navigate"](url=base + "links", confirm=True)
        fresh = await read(mode="tree", max_chars=10**7)
        fresh_lines = fresh["text"].splitlines()
        old_ref = _ref_of(fresh_lines, r'link "Docs" -> /docs')
        outside = _ref_of(fresh_lines, r'link "Pricing" -> /pricing')
        expect(await _resolves(page, old_ref), "a ref resolves right after its read")
        await read(mode="tree", selector="#cards")
        expect(
            not await _resolves(page, outside),
            "reading a region replaced the refs: one outside it no longer resolves",
        )
        started = time.monotonic()
        unknown = await tools["click"](selector=outside)
        expect(
            unknown.get("status") == "not_found" and time.monotonic() - started < 2,
            "click answers a replaced ref with not_found, at once, not after a 30s wait",
            unknown,
        )
        await read(mode="tree", max_chars=10**7)
        expect(await _resolves(page, outside), "reading the page again restores them")
        await tools["navigate"](url=base + "many", confirm=True)
        expect(not await _resolves(page, outside), "navigating leaves old refs naming nothing")
        started = time.monotonic()
        stale = await read(mode="tree", selector=outside)
        expect(
            stale.get("status") == "not_found" and time.monotonic() - started < 2,
            "read_page answers a stale ref with not_found, at once",
            stale,
        )
        for name, args in (
            ("click", {}),
            ("type_text", {"value": "x"}),
        ):
            started = time.monotonic()
            refused = await tools[name](selector=outside, **args)
            expect(
                refused.get("status") == "not_found" and time.monotonic() - started < 2,
                f"{name} answers a stale ref with not_found, at once",
                refused,
            )

        # ---- a ref that navigates --------------------------------------------------
        await tools["navigate"](url=base + "links", confirm=True)
        again = await read(mode="tree", max_chars=10**7)
        real_ref = _ref_of(again["text"].splitlines(), r'link "Go for real" -> /go/real')
        VISITS.clear()
        went = await tools["click"](selector=real_ref)
        expect(went.get("status") == "ok", "clicking a navigating link by ref succeeds", went)
        await page.wait_for_timeout(400)
        expect(VISITS == ["/go/real"], "the site received the visit the ref pointed at", VISITS)
        expect((await tools["get_url"]()).get("title") == "went real", "and the browser arrived")

        # ---- text paging and a region ----------------------------------------------
        await tools["navigate"](url=base + "long", confirm=True)
        truth = await page.evaluate("document.body.innerText")
        default = await read()
        expect(
            default["truncated"]
            and len(default["text"]) == 8000
            and default["total_chars"] == len(truth),
            "the default page is 8000 of a longer text, and total_chars says how long",
            (len(default["text"]), default["total_chars"], len(truth)),
        )
        for step in (1000, 2500, 8000):
            got, totals, offset = [], set(), 0
            while True:
                chunk = await read(max_chars=step, offset=offset)
                got.append(chunk["text"])
                totals.add(chunk["total_chars"])
                if not chunk["truncated"]:
                    break
                offset = chunk["next_offset"]
            expect(
                totals == {len(truth)}, f"total_chars is the same on every page of {step}", totals
            )
            expect("".join(got) == truth, f"{len(got)} pages of {step} reassemble the page exactly")
        arithmetic = "".join(
            [(await read(max_chars=3000, offset=o))["text"] for o in range(0, len(truth), 3000)]
        )
        expect(arithmetic == truth, "so does simply adding max_chars to offset")
        beyond = await read(offset=len(truth) + 10)
        expect(
            (beyond["text"], beyond["truncated"]) == ("", False),
            "an offset past the end is an empty last page",
        )

        region = await read(selector="#region")
        region_truth = await page.evaluate("document.querySelector('#region').innerText")
        expect(
            region["text"] == region_truth and "REGION-MARKER" in region["text"],
            "selector reads just that region's text",
            region["text"][:80],
        )
        expect("OUTSIDE-MARKER" not in region["text"], "and nothing from outside it")
        expect(region["total_chars"] == len(region_truth), "with the region's own total_chars")

        audit = ctx.config.audit_path.read_text()
        expect(
            "hunter2-e2e" not in audit and "typed@example.com" not in audit,
            "no input value reached the audit trail",
        )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool, driver: str) -> None:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    try:
        if not headful_only:
            await run_mode(base, headless=True, driver=driver)
        if not headless_only:
            await run_mode(base, headless=False, driver=driver)
    finally:
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
