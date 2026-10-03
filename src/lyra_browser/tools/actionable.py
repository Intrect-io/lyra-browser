"""Can this control be used right now — and what to say when it cannot.

Playwright answers an unusable control with a 30s wait and an error whose real
reason is buried in a call log. Measured against a page with a full-screen
overlay, a plain ``locator.click()`` sat for 30.0s before giving up; and a click
that had *landed* and started a slow navigation timed out the same way while the
page went on to load. A caller pays half a minute to learn what a three-second
look would have said, and cannot tell "nothing happened" from "it happened".

This module is the shared answer, in the two places that time is lost:

- **Before the action** — ``check_target``. An absent, hidden or disabled control
  is named (``not_found`` / ``hidden`` / ``disabled``) *before* permission is
  asked. The order is the point: asking first prompts a person for an action that
  cannot run, and a one-shot grant bought for it stays live afterwards for a
  later request from the same origin to spend.
- **During the action** — ``guard_action``. What the driver itself raises — a
  timeout, a closed page, an element that went away or is covered — becomes an
  envelope the caller can act on, audited, with whatever the action bought still
  handed back through ``released``.

Playwright is not imported here: the session loads it lazily and may load
patchright instead. Errors are recognised by class name and message, which both
drivers share.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..context import ServerContext
from ..origin import Origin
from .scope import released

# A page that builds its editor or its picker from script attaches them after
# ``domcontentloaded``. Measured on KVR: the CKEditor iframe and the Image Picker
# button are both absent at load and present ~0.5s later. Playwright auto-waits
# for an *action*, but ``count()`` answers immediately — so a tool that asked
# once would call a working page broken. This is the wait, and it is short
# because the alternative is a tool that sits on a genuinely missing control.
_APPEAR_BUDGET_S = 3.0
_APPEAR_STEP_S = 0.15

# What one action may wait, in milliseconds. Playwright's own default is 30s,
# which is where "stuck" came from; and to Playwright a timeout of 0 means *no*
# timeout, so a caller must not be able to ask for it.
DEFAULT_TIMEOUT_MS = 10_000
MAX_TIMEOUT_MS = 30_000


def clamp_timeout(timeout_ms: int) -> int:
    """The budget handed to Playwright: at least 1ms, at most ``MAX_TIMEOUT_MS``."""
    return max(1, min(timeout_ms, MAX_TIMEOUT_MS))


async def _appears(locator, budget_s: float | None = None) -> bool:
    """Whether ``locator`` matches anything within ``budget_s``.

    The budget is read at call time, not bound as a default: a test that pins it
    to zero must not still wait three seconds per candidate selector.
    """
    budget = _APPEAR_BUDGET_S if budget_s is None else budget_s
    deadline = asyncio.get_running_loop().time() + budget
    while True:
        if await locator.count() > 0:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(_APPEAR_STEP_S)


async def _actionable(locator, need_visible: bool = True, budget_s: float | None = None) -> str:
    """Why this control cannot be used, or ``""`` when it can.

    A control can be in the DOM and still be unusable, and the two failures need
    different answers. Measured on KVR: ``select[name=event_country]`` exists on a
    news form but sits in the Deal/Offer section, which is hidden until that type
    is chosen — so Playwright waits out its full 30s actionability timeout and
    raises a locator error the model cannot act on. Saying which of the three it
    is (absent, hidden, disabled) is what lets a caller fix it.

    The wait comes first, so a control the page builds from script — measured at
    ~0.5s on KVR — is not mistaken for one that is missing.

    ``need_visible=False`` skips the visibility check for controls that work
    while hidden — a file input behind a styled button.

    ``budget_s`` overrides how long to wait for the control to appear; left out,
    it is the module's appear budget, read when called.
    """
    if not await _appears(locator, budget_s):
        return "not_found"
    try:
        if need_visible and not await locator.is_visible():
            return "hidden"
        if not await locator.is_enabled():
            return "disabled"
    except Exception:  # noqa: BLE001 — an unreadable control is not a usable one
        return "unreadable"
    return ""


# What to tell a caller about a control that cannot be used. Each says what is
# wrong and the next move, because the envelope is all the model gets back.
_UNUSABLE_HINTS = {
    "not_found": (
        "Nothing on the page matches this selector, even after waiting {budget}s for a "
        "page that builds its controls late. Check it against read_page or read_form, "
        "or wait for the page to finish loading."
    ),
    "hidden": (
        "The element is in the page but was still not visible after {budget}s — a "
        "collapsed menu, an inactive tab, or a section revealed by an earlier choice. "
        "Reveal it first. If the selector also matches a visible copy, narrow it to "
        "that one (Playwright understands ':visible', e.g. 'button.save:visible')."
    ),
    "disabled": (
        "The element was still disabled after {budget}s. Pages usually enable it once "
        "a required field is filled or an option is chosen; do that first."
    ),
    "unreadable": (
        "The element's state could not be read — the page may be navigating. Read the "
        "page again and retry."
    ),
}

# A ``read_page(mode="tree")`` ref (``aria-ref=e12``, ``aria-ref=f1e3`` inside a
# frame) is a handle into the last such read, not a query over the live page: it
# resolves against that one snapshot, so a newer read, a navigation or a removed
# frame ends it, and nothing the page does later brings it back. Measured on
# Playwright 1.63: an unknown ref counts 0 and a click on it waits out its whole
# timeout; a ref into a frame that is gone raises at once (``Invalid frame in
# aria-ref selector``); an element removed after the read still counts 1, and is
# simply not visible.
_STALE_REF = (
    "This ref no longer points at anything. Refs belong to the latest "
    'read_page(mode="tree") and stop working once the page changes or a newer read '
    "replaces them — read it again and use a ref from that read."
)
_HIDDEN_REF = (
    "The element this ref points to is not visible: it may be collapsed, or the page "
    "may have changed since you read it (a removed element keeps its ref but is no "
    'longer shown). Read the page again with read_page(mode="tree") for current refs.'
)
_REF_HINTS = {"not_found": _STALE_REF, "hidden": _HIDDEN_REF}


def _is_ref(selector: str) -> bool:
    """Whether ``selector`` is a ref from ``read_page(mode="tree")``."""
    return selector.strip().startswith("aria-ref=")


_PAGE_CLOSED = (
    "The page this call was acting on has been closed (a popup that closed itself, "
    "or a closed tab or window). Call get_url to see which page is current before "
    "retrying."
)

# A click that landed and started a navigation waits for that navigation, so a
# slow server ends it with the same TimeoutError as a click that never happened
# (measured: timed out at 1.5s, the page loaded the target 2.5s later). Only the
# call log tells them apart, and repeating the first is not harmless.
_DELIVERED = (
    "The click was delivered, but the page had not finished reacting within {ms}ms — "
    "usually a navigation it started is still loading. Do not click again; check "
    "get_url / read_page first."
)

_TIMEOUT = (
    "The action did not finish within {ms}ms. The page may still be loading or busy, "
    "and the action may have partly happened (a click or Enter can start a navigation "
    "that is still in flight) — check get_url / read_page before repeating anything "
    "that is not safe to repeat. Raise timeout_ms (max {max}) only if the page is "
    "genuinely slow."
)

_GONE = (
    "The element was removed from the page while it was being acted on — the page "
    "re-rendered. Read the page again and retry with a fresh selector."
)

# Playwright names the check that keeps failing in its call log, and the last
# line is the reason the last attempt gave up. Matched in lower case, most
# specific first; ``{line}`` is that log line, which is how a covering element
# gets named (``<div id="overlay"></div> intercepts pointer events``).
_NOT_ACTIONABLE: tuple[tuple[str, str], ...] = (
    (
        "intercepts pointer events",
        "The click would land on something else instead of the target — {line}. "
        "Dismiss it (a cookie banner, modal or overlay), scroll it out of the way, or "
        "aim at a different selector.",
    ),
    ("detached from the dom", _GONE),
    ("not attached to the dom", _GONE),
    (
        "execution context was destroyed",
        "The page navigated while the action ran. Check get_url / read_page for where "
        "it landed before retrying.",
    ),
    (
        "is not an <input>",
        "This element cannot be typed into. Aim at the <input>, <textarea> or "
        "[contenteditable] element itself; a dropdown takes select_option and a "
        "rich-text body takes set_editor.",
    ),
    (
        "element is not editable",
        "The field is read-only, so it cannot be typed into.",
    ),
    (
        "element is not visible",
        "The element stayed invisible. Reveal it first (open the menu or section that "
        "contains it).",
    ),
    (
        "element is not enabled",
        "The element stayed disabled. Satisfy whatever enables it (a required field, "
        "an option) first.",
    ),
    (
        "element is not stable",
        "The element keeps moving (an animation or a layout that never settles), so it "
        "cannot be acted on reliably. Wait for the page to settle and retry.",
    ),
    (
        "outside of the viewport",
        "The element is outside the viewport and could not be scrolled into view.",
    ),
)

# Packages whose exception classes count as "the driver said so". Matched by
# name because importing either would defeat the lazy load, and patchright
# defines the same classes in its own namespace.
_DRIVER_PACKAGES = frozenset({"playwright", "patchright"})


def _first_line(exc: BaseException) -> str:
    """The headline of an error — Playwright puts the call log below it."""
    for line in str(exc).splitlines():
        if line.strip():
            return line.strip()[:200]
    return type(exc).__name__


# A call log echoes what it was asked to do — ``- fill("hunter2")`` — so a line that
# looks like a call is the caller's input, never a reason. Skipping those keeps a
# typed value out of the hint and the audit trail even when it happens to contain
# a phrase this module looks for.
_BULLET = re.compile(r"^[\s\-]+")
_ECHOED_CALL = re.compile(r"^[\w.]+\(")


def _reasons(exc: BaseException) -> list[str]:
    """The lines of an error that can say why, without indentation or bullet."""
    bare = (_BULLET.sub("", line).strip() for line in str(exc).splitlines())
    return [line for line in bare if line and not _ECHOED_CALL.match(line)]


def describe_failure(
    exc: BaseException, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> tuple[str, str, str] | None:
    """``(status, hint, detail)`` for a driver failure a caller can act on.

    ``None`` means this is not one of those — a bad selector, a bug — and the
    caller should let it propagate rather than dress it up. Order matters: a
    closed page is checked first because its message carries the same call log a
    covered element does, and a click that already landed is checked before the
    element-state lines that precede it.
    """
    lineage = type(exc).__mro__
    names = {cls.__name__ for cls in lineage}
    if "TargetClosedError" in names:
        return "page_closed", _PAGE_CLOSED, _first_line(exc)

    timed_out = "TimeoutError" in names
    if not timed_out and not any(
        cls.__module__.partition(".")[0] in _DRIVER_PACKAGES for cls in lineage
    ):
        return None

    reasons = _reasons(exc)

    # A stale ref is a missing element, whichever way the driver says so.
    if not timed_out and any("aria-ref selector" in line.lower() for line in reasons):
        return "not_found", _STALE_REF, _first_line(exc)

    if timed_out and any(" action done" in line for line in reasons):
        return "timeout", _DELIVERED.format(ms=timeout_ms), _first_line(exc)
    for line in reversed(reasons):
        lowered = line.lower()
        for needle, hint in _NOT_ACTIONABLE:
            if needle in lowered:
                shown = line[:160]
                return (
                    "element_not_actionable",
                    hint.format(line=shown),
                    f"{_first_line(exc)} — {shown}",
                )
    if timed_out:
        return "timeout", _TIMEOUT.format(ms=timeout_ms, max=MAX_TIMEOUT_MS), _first_line(exc)
    return None


def _failure(
    ctx: ServerContext,
    exc: Exception,
    *,
    tool: str,
    selector: str,
    page,
    origin: Origin,
    args: dict | None,
    timeout_ms: int,
) -> dict | None:
    """The audited envelope for a driver failure, or ``None`` if it is not one."""
    described = describe_failure(exc, timeout_ms)
    if described is None:
        return None
    status, hint, detail = described
    ctx.audit.record(tool, args, status=status, detail=detail, origin=origin.describe())
    return {"status": status, "selector": selector, "url": getattr(page, "url", ""), "hint": hint}


def _unusable_hint(reason: str, selector: str) -> str:
    """What to do about a control that cannot be used, in terms of how it was named."""
    if _is_ref(selector) and reason in _REF_HINTS:
        return _REF_HINTS[reason]
    return _UNUSABLE_HINTS.get(reason, "").format(budget=f"{_APPEAR_BUDGET_S:g}")


# Present but not ready: what a page shows for a moment while it hydrates, fades a
# menu in, or waits on validation before it enables a button. Anything else is final.
_NOT_READY = frozenset({"hidden", "disabled"})


async def check_target(
    ctx: ServerContext,
    *,
    tool: str,
    page,
    origin: Origin,
    locator,
    selector: str,
    args: dict | None = None,
    need_visible: bool = True,
) -> dict | None:
    """The envelope for a control that cannot be used, or ``None`` when it can.

    Call this before ``require()``. It only reads, so it needs no permission, and
    nothing is bought until it passes: an unusable control neither prompts nor
    leaves a one-shot grant behind. A page that closes while it looks answers
    ``page_closed`` like any other action would.

    The look is not one snapshot, because Playwright's own click is not: it waits
    for a control to become ready, and pages routinely show one that is not yet.
    Measured against a button enabled 0.8s after load and another made visible at
    the same moment, the original ``click`` succeeded after ~1s where a one-shot
    check refused both within 50ms. So a control that is present but ``hidden`` or
    ``disabled`` is looked at again until it is ready or the window closes — and
    that window is *shared* with the wait for the control to appear, so every
    answer, whichever way it goes, arrives within one ``_APPEAR_BUDGET_S``.

    A ``read_page(mode="tree")`` ref gets no window: it can neither appear nor
    become ready later, so waiting only delays the "read the page again" it needs.
    A ref the driver rejects outright (its frame is gone) is ``not_found``.
    """
    loop = asyncio.get_running_loop()
    budget = 0.0 if _is_ref(selector) else _APPEAR_BUDGET_S
    deadline = loop.time() + budget
    try:
        reason = await _actionable(locator, need_visible, budget)
        while reason in _NOT_READY and loop.time() < deadline:
            await asyncio.sleep(_APPEAR_STEP_S)
            reason = await _actionable(locator, need_visible, 0.0)
    except Exception as exc:  # noqa: BLE001 — only the driver's own failures are converted
        failure = _failure(
            ctx,
            exc,
            tool=tool,
            selector=selector,
            page=page,
            origin=origin,
            args=args,
            timeout_ms=DEFAULT_TIMEOUT_MS,
        )
        if failure is None:
            raise
        return failure
    if not reason:
        return None
    ctx.audit.record(tool, args, status=reason, origin=origin.describe())
    return {
        "status": reason,
        "selector": selector,
        "url": getattr(page, "url", ""),
        "hint": _unusable_hint(reason, selector),
    }


@dataclass(slots=True)
class Guarded:
    """What a guarded action produced.

    ``result`` is the reply to hand back: the one the block stored, or the
    failure envelope that replaced it when the driver raised.
    """

    result: dict | None = None


@contextlib.asynccontextmanager
async def guard_action(
    ctx: ServerContext,
    bought: list,
    *,
    tool: str,
    selector: str,
    page,
    origin: Origin,
    args: dict | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> AsyncIterator[Guarded]:
    """Run an action so the driver's failures come back as envelopes.

    Wraps ``released`` rather than replacing it: whatever the action bought is
    handed back whether it succeeded, failed in a way reported here, or raised
    something that is not ours to report. The block stores its reply in
    ``guarded.result``; on a driver failure the block is abandoned, the failure
    is audited and ``result`` holds the envelope. Anything that is not a driver
    failure propagates untouched.
    """
    guarded = Guarded()
    async with released(ctx, bought):
        try:
            yield guarded
        except Exception as exc:  # noqa: BLE001 — only the driver's own failures are converted
            failure = _failure(
                ctx,
                exc,
                tool=tool,
                selector=selector,
                page=page,
                origin=origin,
                args=args,
                timeout_ms=timeout_ms,
            )
            if failure is None:
                raise
            guarded.result = failure
