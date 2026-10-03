"""Waiting: hold until the page is ready, rather than guess with a sleep.

A page that draws itself after ``domcontentloaded`` looks empty to ``read_page``
for a moment: measured on a fixture that renders 300ms late, ``read_page`` saw 0
of 5 items straight after ``navigate``. With no way to wait, the only stand-in
was a click, which has side effects. ``wait_for`` is the wait.

It only observes. Like the other read tools it checks who holds the browser
without becoming the holder (``acquire_page(claim=False)``), asks nobody for
anything and keeps working while the user has taken over: waiting changes
nothing on the page, so there is nothing for a gate to judge.
"""

from __future__ import annotations

import asyncio
import re
import time

from fastmcp import FastMCP

from ..context import ServerContext, acquire_page
from ..origin import loggable_url

# The ceiling on one wait, whatever the caller asks for. It is ours, not the
# driver's: a model that asks for an hour must not park a tool call (and the MCP
# request behind it) that long.
_MAX_TIMEOUT_MS = 30_000

# How often the text condition is re-read. A number, not the driver's default of
# one check per animation frame: frames stop in a background tab, and a wait that
# never wakes is the very failure this tool exists to remove.
_POLL_MS = 100

# How much of the page a timeout shows, and how long it may take to fetch it. The
# excerpt is read after the wait has already failed, so it must not become a
# second wait of its own on a page that will not answer.
_EXCERPT_CHARS = 300
_EXCERPT_BUDGET_S = 1.5

_STATES = ("visible", "hidden", "attached", "detached")
_LOAD_STATES = ("load", "domcontentloaded", "networkidle")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_GLOB_CHARS = "*?{["

# Whether the page's visible text holds ``text``. It reads the place ``read_page``
# reads (``body.innerText``), so "wait_for said yes" and "read_page will show it"
# are one fact. Whitespace is collapsed on both sides: a line break or a
# non-breaking space in the page must not make a phrase copied from ``read_page``
# miss. ``present=false`` inverts it, and still needs a body: a document that has
# not started has neither the text nor its absence.
_TEXT_JS = r"""({ text, present }) => {
  const body = document.body;
  if (!body) return false;
  const norm = (s) => s.replace(/\s+/g, ' ').trim();
  return norm(body.innerText).includes(norm(text)) === present;
}"""

_EXCERPT_JS = "() => (document.body ? document.body.innerText.slice(0, 2000) : '')"


def _error(reason: str, given: object) -> dict:
    return {"status": "error", "reason": reason, "given": given}


def _url_problem(pattern: str) -> str:
    """Why ``pattern`` can never match a URL, or ``""`` when it can.

    The pattern is a glob over the *whole* URL, so a bare ``dashboard`` matches
    nothing that exists. Saying so now beats a timeout that says it in ten
    seconds and leaves the caller to work out why.
    """
    if _SCHEME.match(pattern) or any(ch in pattern for ch in _GLOB_CHARS):
        return ""
    return (
        "url is matched against the whole URL as a glob: give a full URL, or a "
        "pattern such as **/dashboard** ('*' stops at '/', '**' does not)."
    )


def _problem(conditions: dict[str, str], state: str, timeout_ms: int) -> tuple[str, dict | None]:
    """The condition asked for, or the error envelope saying why the arguments
    are not a wait."""
    given = [name for name, value in conditions.items() if value.strip()]
    if len(given) != 1:
        return "", _error("Give exactly one of text, selector, url or load_state.", given)
    kind = given[0]
    value = conditions[kind]
    if state not in _STATES:
        return "", _error(f"state must be one of: {', '.join(_STATES)}.", state)
    if kind == "text" and state not in ("visible", "hidden"):
        return "", _error("A text wait is 'visible' (it appears) or 'hidden' (it goes).", state)
    if kind in ("url", "load_state") and state != "visible":
        return "", _error("state applies to text and selector waits only.", state)
    if kind == "load_state" and value not in _LOAD_STATES:
        return "", _error(f"load_state must be one of: {', '.join(_LOAD_STATES)}.", value)
    if kind == "url" and (why := _url_problem(value)):
        return "", _error(why, value)
    if timeout_ms < 1:
        # Below 1 is not "no wait": the driver reads 0 as "never time out".
        return "", _error(f"timeout_ms must be 1 to {_MAX_TIMEOUT_MS}.", timeout_ms)
    return kind, None


def _timed_out(exc: BaseException) -> bool:
    """Whether the driver gave up waiting.

    By class name rather than ``isinstance``: playwright and its patchright fork
    each define a ``TimeoutError`` of their own, and importing either here would
    pin the driver before the session has chosen one.
    """
    return any(cls.__name__ == "TimeoutError" for cls in type(exc).__mro__)


def _first_line(exc: BaseException) -> str:
    """The driver's own explanation without its call log."""
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else type(exc).__name__


async def _last_seen(page, kind: str) -> str:
    """What the page showed when the wait gave up: a text excerpt, or the URL.

    A ``url`` wait is about where the tab is, so it reports that. Everything else
    is about what the page says, so it reports the head of that; the URL stands
    in when there is no text yet (a blank page) or the page will not answer.
    """
    if kind != "url":
        try:
            text = await asyncio.wait_for(page.evaluate(_EXCERPT_JS), _EXCERPT_BUDGET_S)
        except Exception:  # noqa: BLE001 — a page that will not answer still gets a reply
            text = ""
        seen = " ".join(str(text or "").split())
        if seen:
            return seen[:_EXCERPT_CHARS] + ("…" if len(seen) > _EXCERPT_CHARS else "")
    return page.url


async def _wait(page, kind: str, value: str, state: str, timeout_ms: int) -> None:
    """Hand the condition to the driver, which owns the waiting and its clock."""
    if kind == "text":
        await page.wait_for_function(
            _TEXT_JS,
            arg={"text": value, "present": state == "visible"},
            timeout=timeout_ms,
            polling=_POLL_MS,
        )
    elif kind == "selector":
        await page.locator(value).first.wait_for(state=state, timeout=timeout_ms)
    elif kind == "url":
        # ``domcontentloaded``, not the driver's ``load``: a URL that matched is
        # enough, and one slow third-party resource must not turn it into a timeout.
        await page.wait_for_url(value, timeout=timeout_ms, wait_until="domcontentloaded")
    else:
        await page.wait_for_load_state(value, timeout=timeout_ms)


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def wait_for(
        text: str = "",
        selector: str = "",
        url: str = "",
        load_state: str = "",
        state: str = "visible",
        timeout_ms: int = 10000,
    ) -> dict:
        """Wait until the page is ready, instead of sleeping or clicking to pass time.

        Give exactly one condition:

        - ``text``: the phrase is in the page's visible text — what ``read_page``
          returns. Whitespace does not matter, case does. ``state="hidden"``
          waits for it to be gone instead (a "Loading..." message).
        - ``selector``: an element matching it (CSS or Playwright ``text=``)
          reaches ``state``: ``visible`` (default), ``hidden``, ``attached`` or
          ``detached``.
        - ``url``: the tab's URL matches. A glob over the whole URL, so use
          ``**/checkout**`` or ``https://site.example/cart*``, not ``checkout``.
        - ``load_state``: ``load``, ``domcontentloaded`` or ``networkidle``.

        Waits up to ``timeout_ms`` (default 10000, never more than 30000) and
        returns ``{"status": "ok", "waited_ms"}``. Running out of time is an
        answer, not a failure: ``{"status": "timeout", "waited_ms", "last_seen"}``,
        where ``last_seen`` is a short excerpt of the page text (the URL for a
        ``url`` wait) so you can see why. Bad arguments return ``error``.

        Use it after ``navigate`` or an action when the page draws content late: a
        page that renders 300ms after load looks empty to ``read_page`` until this
        returns. It only reads, so it needs no approval and works while the user
        has taken over.
        """
        conditions = {"text": text, "selector": selector, "url": url, "load_state": load_state}
        kind, invalid = _problem(conditions, state, timeout_ms)
        if invalid is not None:
            return invalid
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        value = conditions[kind]
        limit = min(timeout_ms, _MAX_TIMEOUT_MS)
        # What the audit keeps is the condition, not the page. A URL loses its
        # query values, as everywhere else the trail writes one.
        logged: dict = {kind: loggable_url(value) if kind == "url" else value[:200]}
        if kind in ("text", "selector"):
            logged["state"] = state
        logged["timeout_ms"] = limit
        started = time.monotonic()
        try:
            await _wait(page, kind, value, state, limit)
        except Exception as exc:  # noqa: BLE001 — the driver's verdict, reported not raised
            if _timed_out(exc):
                waited_ms = round((time.monotonic() - started) * 1000)
                ctx.audit.record("wait_for", logged, status="timeout")
                return {
                    "status": "timeout",
                    "waited_ms": waited_ms,
                    "last_seen": await _last_seen(page, kind),
                }
            # Not a timeout: a selector the engine cannot parse, a window the user
            # closed. The caller can act on the reason, so it is an answer too.
            ctx.audit.record("wait_for", logged, status="error", detail=type(exc).__name__)
            return {"status": "error", "reason": _first_line(exc)}
        ctx.audit.record("wait_for", logged)
        return {"status": "ok", "waited_ms": round((time.monotonic() - started) * 1000)}
