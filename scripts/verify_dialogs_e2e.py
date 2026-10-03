#!/usr/bin/env python3
"""Exercise native-dialog handling against a real installed browser.

Companion to ``verify_browser_e2e.py`` (same explicit-gate contract: temporary
profile, loopback site, not part of the unit suite). It reuses that script's
``build_tools``, so the server is driven exactly as the other gates drive it, with
Config defaults: enforcement on, approval on.

The thing being proved is that no dialog ever leaves a page waiting. A listener
that does not answer freezes the tab — including the ``evaluate`` that would read
it afterwards — so every step that could hang runs under a deadline, and the
deadline failing *is* the finding. Beyond that: an alert's text comes back on the
click that raised it, an armed answer decides ``confirm()`` and ``prompt()`` for the
next action only — and neither follows the agent onto a later page nor outlives the
click it was armed for — ``beforeunload`` does not strand a navigation, a dialog raised
with no tool running is recorded, popups are covered as well as the opener, and a
form refused through an ``alert()`` is reported refused instead of hanging.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

from lyra_browser.origin import parse_origin  # noqa: E402
from lyra_browser.permission import Capability  # noqa: E402

# Each control writes the dialog's answer into the title, so the script reads what
# the *page* saw — the return value of confirm() and prompt() — not what we sent.
HOME = """<!doctype html><title>dialogs</title><h1>Dialogs E2E</h1>
<button id="plain">plain</button>
<button id="alert">alert</button>
<button id="confirm">confirm</button>
<button id="prompt">prompt</button>
<button id="late">late</button>
<button id="popup">popup</button>
<button id="popup-now">popup now</button>
<script>
const say = (name, value) => { document.title = name + ':' + value; };
const on = (id, fn) => { document.getElementById(id).onclick = fn; };
on('alert', () => say('alert', alert('hello from the page')));
on('confirm', () => say('confirm', confirm('Delete the item?')));
on('prompt', () => say('prompt', prompt('Your name?', 'anon')));
on('late', () => setTimeout(() => say('late', confirm('Still there?')), 800));
on('popup', () => window.open('/popup'));
on('popup-now', () => window.open('/popup-now'));
addEventListener('beforeunload', (e) => { e.preventDefault(); e.returnValue = ''; });
</script>"""

# A popup that asks a while after it has loaded, and one that asks while loading. The
# delay is long next to a click (which listens for a download for 100ms after it), so
# the first is raised after the call that opened it has returned.
POPUP = """<!doctype html><title>popup</title><h1>Popup</h1>
<script>setTimeout(() => { document.title = 'popup:' + confirm('Popup asks'); }, 800);</script>"""
POPUP_NOW = """<!doctype html><title>popup-now</title><h1>Popup</h1>
<script>document.title = 'now:' + confirm('At load');</script>"""

NEXT = "<!doctype html><title>next</title><h1>Next</h1>"

# The page an agent reaches after the one an answer was armed on.
LATER = """<!doctype html><title>later</title><h1>Later</h1>
<button id="delete">delete</button>
<script>
document.getElementById('delete').onclick = () => {
  document.title = 'deleted:' + confirm('Delete this?');
};
</script>"""

# One control per tool that acts on a page; each asks a question from its own event.
TOOLS = """<!doctype html><title>tools</title><h1>Tools</h1>
<input id="typed"><button id="hovered">hover me</button>
<select id="picked"><option value="a">A</option><option value="b">B</option></select>
<script>
const say = (name, value) => { document.title = name + ':' + value; };
document.getElementById('typed').oninput = () => say('typed', confirm('Keep typing?'));
document.getElementById('hovered').onmouseover = () => say('hovered', confirm('Hover?'));
document.getElementById('picked').onchange = () => say('picked', confirm('Switch?'));
addEventListener('keydown', (e) => { if (e.key === 'F9') say('key', confirm('F9?')); });
addEventListener('beforeunload', (e) => { e.preventDefault(); e.returnValue = ''; });
</script>"""

# A form that refuses through an alert(), the way the sites this was measured on do.
FORM = """<!doctype html><title>form</title><h1>Form</h1>
<form id="f" action="/save" method="post">
  <input id="title" name="title">
  <label><input type="radio" name="is_draft" value="1" checked> draft</label>
  <label><input type="radio" name="is_draft" value="0"> live</label>
  <button id="send" type="submit">Submit</button>
</form>
<script>
document.getElementById('f').onsubmit = () => {
  if (document.getElementById('title').value) return true;
  alert('Title is required');
  return false;
};
</script>"""

PAGES = {
    "/": HOME,
    "/popup": POPUP,
    "/popup-now": POPUP_NOW,
    "/next": NEXT,
    "/later": LATER,
    "/tools": TOOLS,
    "/form": FORM,
}
POSTS: list[str] = []


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path not in PAGES:
            self.send_error(404)
            return
        self._send(PAGES[path])

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        POSTS.append(raw.decode("utf-8", "replace"))
        self._send("<title>saved</title><h1>Saved</h1>")

    def log_message(self, *_args: object) -> None:
        pass


async def bounded(awaitable, what: str, seconds: float = 15.0):
    """Await ``awaitable``; a hang is the bug under test, so it fails loudly."""
    try:
        return await asyncio.wait_for(awaitable, seconds)
    except TimeoutError:
        raise AssertionError(f"{what} hung for {seconds:.0f}s: the page is frozen") from None


async def until(check, what: str, timeout: float = 8.0):
    """Poll ``check`` (plain or async) until it returns something truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if inspect.isawaitable(found):
            found = await found
        if found:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def dialog(kind: str, message: str, *, handled: str, armed: bool, default: str = "") -> dict:
    """One entry of a reply's ``dialogs``."""
    return {
        "type": kind,
        "message": message,
        "default_value": default,
        "handled": handled,
        "armed": armed,
    }


async def run_mode(base: str, *, headless: bool) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-dialogs-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    ctx, tools = await build_tools(data_dir, headless=headless)
    print(f"\n[{mode}] data_dir={data_dir}")
    POSTS.clear()
    site = parse_origin(base)

    async def title_of(page=None) -> str:
        page = page or await ctx.session.page()
        return await bounded(page.title(), "reading a title")

    async def titled(expected: str, page=None) -> bool:
        return await title_of(page) == expected

    async def click(selector: str, what: str) -> dict:
        return await bounded(tools["click"](selector=selector), what)

    async def reported_with(clicked: dict) -> list[dict]:
        """Dialogs reported by ``clicked`` and by the action after it.

        A popup's load-time dialog races the click that opened it: it lands on the
        click's reply or on the next one, and either is right.
        """
        follow_up = await tools["press_key"](key="Shift")
        return (clicked.get("dialogs") or []) + (follow_up.get("dialogs") or [])

    async def armed_yes_reaches(name: str, expected_title: str, call) -> None:
        """Arm a yes, run one tool, and see the page's own confirm() come back true."""
        await tools["handle_dialog"](accept=True)
        reply = await bounded(call(), name)
        raised = reply.get("dialogs") or ctx.session.dialogs.drain()
        expect(await titled(expected_title), f"{name}: the page's confirm() was answered yes")
        expect(
            len(raised) == 1 and raised[0]["armed"] is True and raised[0]["handled"] == "accepted",
            f"{name}: and it was the armed answer that did it",
            raised,
        )

    try:
        await tools["open_browser"]()
        entered = await tools["navigate"](url=base, reason="dialogs E2E", confirm=True)
        expect(entered.get("title") == "dialogs", "fixture page loads", entered)

        # --- an alert: the click returns, with the alert's text ---------------
        alerted = await click("#alert", "click on an alert")
        expect(alerted.get("status") == "ok", "a click that raises an alert returns", alerted)
        expect(
            alerted.get("dialogs")
            == [dialog("alert", "hello from the page", handled="accepted", armed=False)],
            "the alert's text comes back on the click reply",
            alerted,
        )
        expect(await titled("alert:undefined"), "and the page's script carried on")

        # --- confirm: no by default, yes or no when armed, once ---------------
        plain = await click("#confirm", "unarmed confirm")
        expect(await titled("confirm:false"), "an unarmed confirm() is false")
        expect(
            plain.get("dialogs")
            == [dialog("confirm", "Delete the item?", handled="dismissed", armed=False)],
            "and the reply says it was dismissed by default",
            plain,
        )

        armed = await tools["handle_dialog"](accept=True)
        expect(armed.get("status") == "ok", "handle_dialog arms an answer", armed)
        yes = await click("#confirm", "armed accept")
        expect(await titled("confirm:true"), "an armed accept makes confirm() true")
        expect(
            yes.get("dialogs")
            == [dialog("confirm", "Delete the item?", handled="accepted", armed=True)],
            "and the reply says it was the armed answer",
            yes,
        )

        await click("#confirm", "second confirm")
        expect(await titled("confirm:false"), "the armed answer was spent by one dialog")

        await tools["handle_dialog"](accept=False)
        no = await click("#confirm", "armed dismiss")
        expect(await titled("confirm:false"), "an armed dismiss makes confirm() false")
        expect(
            no["dialogs"][0]["armed"] is True and no["dialogs"][0]["handled"] == "dismissed",
            "and is reported as armed",
            no,
        )

        # --- prompt: null by default, a typed answer when armed ---------------
        await click("#prompt", "unarmed prompt")
        expect(await titled("prompt:null"), "an unarmed prompt() is null")

        await tools["handle_dialog"](accept=True, text="Ada Lovelace")
        typed = await click("#prompt", "armed prompt")
        expect(await titled("prompt:Ada Lovelace"), "an armed prompt receives the text")
        expect(
            typed["dialogs"]
            == [dialog("prompt", "Your name?", handled="accepted", armed=True, default="anon")]
            and "Ada Lovelace" not in json.dumps(typed),
            "the reply carries the prompt, not what it was told",
            typed,
        )

        await tools["handle_dialog"](accept=True)
        await click("#prompt", "prompt, default accepted")
        expect(await titled("prompt:anon"), "accepting without text takes the prompt's default")

        audit_text = ctx.config.audit_path.read_text()
        entries = [json.loads(line) for line in audit_text.splitlines() if line]
        expect("Ada Lovelace" not in audit_text, "the typed answer is nowhere in the audit log")
        expect(
            any(
                e["tool"] == "handle_dialog" and e["args"] == {"accept": True, "text_chars": 12}
                for e in entries
            ),
            "the audit log records that an answer was armed, and how long",
            [e for e in entries if e["tool"] == "handle_dialog"],
        )

        # --- an armed answer is for the next action only ----------------------
        # A yes armed for one click must not answer the "Delete this?" of whatever
        # page the agent reaches afterwards: like a one-shot grant it ends with the
        # next call that drives the browser, whether or not a dialog opened.
        await tools["handle_dialog"](accept=True)
        await click("#plain", "the click an answer was armed for, which asks nothing")
        two_later = await click("#confirm", "a confirm two calls after the arming")
        expect(
            await titled("confirm:false"),
            "an armed yes does not reach a confirm raised two calls later",
        )
        expect(
            two_later.get("dialogs")
            == [dialog("confirm", "Delete the item?", handled="dismissed", armed=False)],
            "and the reply says that one got the default",
            two_later,
        )

        await tools["handle_dialog"](accept=True)
        await tools["get_url"]()
        await tools["read_page"]()
        await click("#confirm", "a confirm after some reading")
        expect(
            await titled("confirm:true"),
            "reading between the arming and the click does not use the answer up",
        )

        await tools["handle_dialog"](accept=True)
        held = await tools["click"](selector="#confirm", submits=True)
        expect(held.get("status") == "needs_approval", "a click that asks first is refused", held)
        await tools["click"](selector="#confirm", submits=True, confirm=True)
        expect(
            await titled("confirm:false"),
            "a refused click was the next call: its retry has to arm again",
        )

        # --- a dialog with no tool running ------------------------------------
        # Armed as well: the click it was armed for has returned by the time the
        # page's own timer asks, so the question gets the default and not the yes.
        await tools["handle_dialog"](accept=True)
        quiet = await click("#late", "click that schedules a dialog")
        expect("dialogs" not in quiet, "the click returned before the dialog", quiet)
        expect(
            await until(lambda: titled("late:false"), "the late confirm to be answered"),
            "a dialog raised while no tool is running does not freeze the page",
        )
        seen = await tools["press_key"](key="Shift")
        expect(
            seen.get("dialogs")
            == [dialog("confirm", "Still there?", handled="dismissed", armed=False)],
            "and the next action reports it, unarmed: the armed click had already returned",
            seen,
        )
        quiet_again = await tools["press_key"](key="Shift")
        expect("dialogs" not in quiet_again, "a dialog is reported once", quiet_again)

        # --- beforeunload does not strand a navigation -------------------------
        await click("#alert", "click that gives the page a user gesture")
        left = await bounded(tools["navigate"](url=base + "next"), "leaving a beforeunload page")
        expect(
            left.get("status") == "ok" and left.get("title") == "next",
            "navigating away from a page with a beforeunload handler completes",
            left,
        )
        after_leaving = await tools["press_key"](key="Shift")
        expect(
            any(
                d["type"] == "beforeunload" and d["handled"] == "accepted"
                for d in after_leaving.get("dialogs", [])
            ),
            "and the beforeunload it raised is reported as accepted",
            after_leaving,
        )

        # --- an armed answer does not follow the agent onto a later page -------
        await tools["handle_dialog"](accept=True)
        await bounded(tools["navigate"](url=base + "later"), "moving on to the next page")
        later = await click("#delete", "the confirm of a page reached after the arming")
        expect(
            await titled("deleted:false"),
            "an armed yes does not answer the Delete? of a page reached afterwards",
        )
        expect(
            later.get("dialogs")
            == [dialog("confirm", "Delete this?", handled="dismissed", armed=False)],
            "and the reply says it got the default",
            later,
        )

        # --- every tool that acts on a page carries the answer into its action ---
        await tools["navigate"](url=base + "tools")
        await armed_yes_reaches(
            "type_text", "typed:true", lambda: tools["type_text"](selector="#typed", value="x")
        )
        await armed_yes_reaches(
            "hover", "hovered:true", lambda: tools["hover"](selector="#hovered")
        )
        await armed_yes_reaches(
            "select_option",
            "picked:true",
            lambda: tools["select_option"](selector="#picked", value="b"),
        )
        await armed_yes_reaches("press_key", "key:true", lambda: tools["press_key"](key="F9"))
        await tools["handle_dialog"](accept=True)
        left_tools = await bounded(tools["navigate"](url=base + "next"), "leaving with a yes armed")
        raised = ctx.session.dialogs.drain()
        expect(
            left_tools.get("status") == "ok"
            and [(d["type"], d["handled"], d["armed"]) for d in raised]
            == [("beforeunload", "accepted", True)],
            "navigate: the armed answer is what the beforeunload it raised got",
            [left_tools, raised],
        )
        await tools["navigate"](url=base)

        # --- popups get the handler too ---------------------------------------
        ctx.perms.grant("default", site, Capability.NAVIGATE)  # the operator's approval
        await click("#popup", "click that opens a popup")
        await until(lambda: ctx.session.tab_info()["tab_count"] == 2, "the popup's tab")
        popup = ctx.session.open_pages()[-1]
        expect(
            await until(lambda: titled("popup:false", popup), "the popup's confirm to be answered"),
            "a dialog on a popup is answered, not left open",
        )
        told = await tools["press_key"](key="Shift")
        expect(
            told.get("dialogs")
            == [dialog("confirm", "Popup asks", handled="dismissed", armed=False)],
            "and reported like any other",
            told,
        )
        closed = await tools["tabs"](action="close", index=1)
        expect(closed.get("status") == "ok", "the popup is closed", closed)

        opened = await click("#popup-now", "click that opens a popup that asks while loading")
        await until(lambda: ctx.session.tab_info()["tab_count"] == 2, "the second popup's tab")
        loading = ctx.session.open_pages()[-1]
        expect(
            await until(
                lambda: titled("now:false", loading), "the load-time confirm to be answered"
            ),
            "a popup that asks while loading is not left hanging either",
        )
        at_load = await reported_with(opened)
        expect(
            at_load == [dialog("confirm", "At load", handled="dismissed", armed=False)],
            "and what it asked while loading is reported: the listener is the window's",
            at_load,
        )
        await tools["tabs"](action="close", index=1)

        # An answer armed for the click that opens a popup does not follow the agent
        # onto it: the popup is a later page, and it asks after the click has returned.
        await tools["handle_dialog"](accept=True)
        await click("#popup", "click that opens a popup, with a yes armed")
        await until(lambda: ctx.session.tab_info()["tab_count"] == 2, "the third popup's tab")
        asked = ctx.session.open_pages()[-1]
        expect(
            await until(lambda: titled("popup:false", asked), "the popup's confirm to be answered"),
            "an armed yes does not answer the popup that click opened",
        )
        told = await tools["press_key"](key="Shift")
        expect(
            told.get("dialogs")
            == [dialog("confirm", "Popup asks", handled="dismissed", armed=False)],
            "and the popup's question is reported as unarmed",
            told,
        )
        await tools["tabs"](action="close", index=1)

        # Switching tabs drives the browser as well, so it is the call an armed answer
        # is used up by, and the confirm after it gets the default.
        await tools["handle_dialog"](accept=True)
        switched = await tools["tabs"](action="switch", index=0)
        after_switch = await click("#confirm", "a confirm after a tab switch")
        expect(switched.get("status") == "ok", "the tab switch succeeds", switched)
        expect(
            after_switch.get("dialogs")
            == [dialog("confirm", "Delete the item?", handled="dismissed", armed=False)],
            "tabs: switching tabs was the next call, so it used the armed answer up",
            after_switch,
        )

        # --- a form refused through an alert() --------------------------------
        await tools["navigate"](url=base + "form")
        # Leaving the first page raised its beforeunload; this reply reports it, so
        # that what follows the refusal can be told apart from it.
        await tools["press_key"](key="Shift")
        started = time.monotonic()
        refused = await bounded(
            tools["save_draft"](submit="#send", reason="dialogs E2E", confirm=True),
            "save_draft on a form that alerts",
        )
        expect(
            refused.get("status") == "refused"
            and refused.get("refused") is True
            and "Title is required" in refused.get("errors", []),
            "save_draft reports the alert as the site's refusal",
            refused,
        )
        expect(time.monotonic() - started < 10, "and returns rather than hanging on the alert")
        expect(not POSTS, "the refused form sent nothing", POSTS)
        after_refusal = await tools["press_key"](key="Shift")
        expect("dialogs" not in after_refusal, "the refusal is not reported twice", after_refusal)

        await tools["type_text"](selector="#title", value="A headline")
        saved = await bounded(
            tools["save_draft"](submit="#send", reason="dialogs E2E", confirm=True),
            "save_draft on a valid form",
        )
        expect(
            saved.get("status") == "ok" and saved.get("refused") is not True,
            "a valid form saves",
            saved,
        )
        await until(lambda: len(POSTS) == 1, "the form's POST")
        expect(len(POSTS) == 1, "exactly one request reached the site", POSTS)
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool) -> None:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    try:
        if not headful_only:
            await run_mode(base, headless=True)
        if not headless_only:
            await run_mode(base, headless=False)
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(async_main(args.headless_only, args.headful_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
