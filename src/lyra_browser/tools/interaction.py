"""Interaction tools: click, type, key presses.

These used to guess an action's effect from the DOM — a control's ``type``
attribute decided whether a click "was" a submit, and a list of key names decided
whether a keystroke was. Both guesses lost to the page: a bare ``<button>``
inside a form submits, ``onclick="form.submit()"`` submits from a control the
check cleared, an ``input`` listener submits while the agent only typed, the
attribute can change between the check and the click, and Space activates a
focused button exactly like Enter.

So the guessing is gone. A tool now declares the scope it wants and the
enforcement layer judges what actually leaves the browser. Declaring
``submits=true`` buys permission to send a form; submitting without declaring it
is stopped at the request, whichever of those paths produced it.

Failing is the other half of being usable. A target that is absent, hidden or
disabled is named within seconds and *before* permission is asked (see
``actionable.py``), and what goes wrong once the action runs comes back as an
envelope instead of Playwright's 30s timeout. Reading the element to report what
was clicked is a report, never a decision — nothing here gates on it.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from ..approval import TakeoverActive
from ..context import ServerContext, acquire_page
from ..downloads import KEYBOARD_SETTLE_SCALE
from ..permission import Capability
from .actionable import DEFAULT_TIMEOUT_MS, check_target, clamp_timeout, guard_action
from .aftermath import LINGER_S, Aftermath
from .dialogs import report_dialogs
from .scope import page_origin, released, require

# Reads what a click is about to land on, in one round trip: the tag, the role
# (the page's own, else the one HTML gives the element) and a best-effort
# accessible name. Form fields are never read for their content — a password box
# must not turn up in a reply — only buttons expose their ``value`` as a label.
_DESCRIBE_JS = """(el) => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  const implicitRole = () => {
    if (tag === 'a' || tag === 'area') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'select') return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'option') return 'option';
    if (tag === 'img') return el.getAttribute('alt') === '' ? 'presentation' : 'img';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'li') return 'listitem';
    if (tag === 'nav') return 'navigation';
    if (tag === 'input') {
      if (['button', 'submit', 'reset', 'image', 'file'].includes(type)) return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'range') return 'slider';
      if (type === 'number') return 'spinbutton';
      if (type === 'search') return el.hasAttribute('list') ? 'combobox' : 'searchbox';
      if (['text', 'email', 'tel', 'url', 'password'].includes(type)) {
        return el.hasAttribute('list') ? 'combobox' : 'textbox';
      }
    }
    return 'generic';
  };
  const named = (ids) => clean(ids.split(/\\s+/).map((id) => {
    const node = document.getElementById(id);
    return node ? (node.innerText || node.textContent) : '';
  }).join(' '));
  const isField = tag === 'input' || tag === 'textarea' || tag === 'select';
  let name = '';
  if (el.getAttribute('aria-labelledby')) name = named(el.getAttribute('aria-labelledby'));
  if (!name) name = clean(el.getAttribute('aria-label'));
  if (!name && el.labels && el.labels.length) {
    name = clean(Array.from(el.labels).map((l) => l.innerText || l.textContent).join(' '));
  }
  if (!name && tag === 'input' && ['button', 'submit', 'reset'].includes(type)) {
    name = clean(el.value);
  }
  if (!name && (tag === 'img' || tag === 'area' || (tag === 'input' && type === 'image'))) {
    name = clean(el.getAttribute('alt'));
  }
  if (!name && !isField) name = clean(el.innerText || el.textContent);
  if (!name) {
    const img = el.querySelector('img[alt]');
    if (img) name = clean(img.getAttribute('alt'));
  }
  if (!name) name = clean(el.getAttribute('title')) || clean(el.getAttribute('placeholder'));
  const role = clean(el.getAttribute('role')).split(' ')[0] || implicitRole();
  return { tag, role, name: name.slice(0, 200) };
}"""

# The read is a courtesy, so it gets a second, not the driver's 30s: if the
# element vanished between the check and here, waiting for it to come back would
# undo everything the fast failure is for. The click that follows has its own
# (short) budget and gives the real answer.
_DESCRIBE_BUDGET_S = 1.0
_NAME_CHARS = 80

# Where the page is scrolled to, and how far it can go. ``at_bottom`` allows a
# couple of pixels: fractional scroll positions on a scaled display otherwise
# leave a page that is visibly at its end a pixel short of it.
_SCROLL_STATE_JS = """() => {
  const el = document.scrollingElement || document.documentElement;
  const y = Math.round(el.scrollTop);
  const h = el.scrollHeight;
  return { scroll_y: y, scroll_height: h, at_bottom: y + el.clientHeight >= h - 2 };
}"""

# Jump to an end of the document. ``instant`` matters: a page with
# ``scroll-behavior: smooth`` would otherwise animate past the moment it is read.
_SCROLL_TO_JS = """(to) => {
  const el = document.scrollingElement || document.documentElement;
  window.scrollTo({ top: to === 'top' ? 0 : el.scrollHeight, behavior: 'instant' });
}"""

# One scroll moves at most this far, so a stray number cannot spin the page
# through a whole feed in a single call; ``to='bottom'`` is the way to go far.
MAX_SCROLL_PX = 20_000
_SCROLL_ENDS = ("top", "bottom")

# A scroll is not over when the call returns: the browser finishes the movement
# and the page reacts to it (lazy content, sticky headers) a moment later. The
# position is re-read until it has held for ``_SETTLE_HOLD_READS`` reads in a
# row, for at most ``_SETTLE_MAX_S``.
_SETTLE_STEP_S = 0.06
_SETTLE_MAX_S = 0.3
_SETTLE_HOLD_READS = 3


def _scopes(submits: bool, download: bool = False) -> list[Capability]:
    """Interacting is always needed; sending a form or saving a file only when declared."""
    scopes = [Capability.INTERACT]
    if submits:
        scopes.append(Capability.SUBMIT)
    if download:
        scopes.append(Capability.DOWNLOAD)
    return scopes


async def _read_target(everything, first) -> dict:
    """How many elements the selector matched, and what the first one is.

    Read just before the click, while the element is still there — a click that
    navigates or re-renders takes it away. Whatever cannot be read is left out:
    this reports, and a page that will not answer costs the report, not the click.
    """
    seen: dict = {}
    try:
        seen["matches"] = await everything.count()
    except Exception:  # noqa: BLE001 — a report that cannot be read is left out
        pass
    try:
        info = await asyncio.wait_for(first.evaluate(_DESCRIBE_JS), _DESCRIBE_BUDGET_S)
    except Exception:  # noqa: BLE001 — as above
        return seen
    if isinstance(info, dict):
        seen["clicked"] = {
            "tag": str(info.get("tag") or ""),
            "role": str(info.get("role") or ""),
            "name": " ".join(str(info.get("name") or "").split())[:_NAME_CHARS],
        }
    return seen


async def _scroll_state(page) -> dict:
    """Where the document is scrolled, as ``scroll_y`` / ``scroll_height`` / ``at_bottom``."""
    raw = await page.evaluate(_SCROLL_STATE_JS)
    raw = raw if isinstance(raw, dict) else {}
    return {
        "scroll_y": int(raw.get("scroll_y") or 0),
        "scroll_height": int(raw.get("scroll_height") or 0),
        "at_bottom": bool(raw.get("at_bottom")),
    }


async def _settled_scroll(page) -> dict:
    """The scroll position once it has held still, or after ``_SETTLE_MAX_S``.

    A wheel event is dispatched, not finished, and a page that loads more on
    reaching the end grows a moment after it: an answer read straight away, or
    after one quiet frame, is short of where the page ends up. So the position
    must repeat ``_SETTLE_HOLD_READS`` times, not just twice.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SETTLE_MAX_S
    seen = await _scroll_state(page)
    held = 0
    while loop.time() < deadline:
        await asyncio.sleep(_SETTLE_STEP_S)
        now = await _scroll_state(page)
        held = held + 1 if now == seen else 0
        seen = now
        if held >= _SETTLE_HOLD_READS:
            break
    return seen


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def click(
        selector: str,
        reason: str = "",
        submits: bool = False,
        confirm: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        download: bool = False,
    ) -> dict:
        """Click the first element matching ``selector`` (CSS or Playwright text=).

        Set ``submits=true`` when the control sends a form or completes a
        purchase — that asks for the permission it needs. If the page submits
        anyway without you declaring it, the request is stopped, so declare it
        when you mean it. ``reason`` is shown to the user when they are asked.

        Fails fast instead of hanging. A selector that matches nothing answers
        ``not_found``, and a match that cannot be used ``hidden`` or ``disabled``
        — each after at most a few seconds' wait for a page that is still
        building or enabling its controls, and before anyone is asked. Once
        clicking, ``timeout_ms`` (default 10000, max 30000) bounds the wait, and
        a failure answers ``timeout`` (the click may have landed — look at the
        page before clicking again), ``element_not_actionable`` (covered,
        detached) or ``page_closed``.

        The reply says how many elements matched (``matches``; the first is
        clicked) and what was hit (``clicked``: tag, role, name), so a wrong
        pick is visible.

        If the click makes the page navigate and the browser refuses it — no
        approval covers the destination, or a form was sent without
        ``submits=true`` — the reply is ``blocked_by_policy`` (with ``url``, where
        the tab still stands, and ``redirected_to`` when the site redirected the
        browser elsewhere) and the tab has not moved: ``navigate`` to the
        destination to be asked for it. A click that opens a tab answers
        ``new_tab: true`` and ``tab_count``; the new tab is the one every later
        call reads and acts on (``tabs`` goes back).

        Set ``download=true`` when the click is meant to save a file: that asks for
        permission to write one, and the reply carries ``download`` (``filename``,
        ``path``, ``bytes``, ``url_origin``) once it is saved. A click that starts a
        download without it is cancelled and answered ``download_blocked`` — nothing
        is saved, so repeat the click with ``download=true``. The permission pays for
        one file and ends with the call.

        A native dialog the page raised is answered at once and listed under
        ``dialogs`` in the reply; ``handle_dialog`` chooses the answer beforehand.
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        args = {"selector": selector}
        timeout = clamp_timeout(timeout_ms)
        everything = page.locator(selector)
        target = everything.first
        # Usability is checked before permission is asked: a control that cannot
        # be clicked should not prompt, and a one-shot SUBMIT bought for it
        # would outlive this call.
        unusable = await check_target(
            ctx,
            tool="click",
            page=page,
            origin=origin,
            locator=target,
            selector=selector,
            args=args,
        )
        if unusable:
            return unusable
        denied, bought = await require(
            ctx,
            "click",
            target=origin,
            capabilities=_scopes(submits, download),
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            return denied
        watch = ctx.downloads.watch(bought, declared=download, start_budget_s=timeout / 1000)
        after = Aftermath(ctx, page)
        async with guard_action(
            ctx,
            bought,
            tool="click",
            selector=selector,
            page=page,
            origin=origin,
            args=args,
            timeout_ms=timeout,
        ) as done:
            # Watching starts before anything else in the block, so the permission is
            # handed back on every way out of it — and a file that arrives while the
            # target is being read is this call's, not a stray that spends it.
            async with watch:
                seen = await _read_target(everything, target)
                await target.click(timeout=timeout)
                after.acted()
                await watch.settle()
            done.result = {"status": "ok", "url": page.url, **seen}
            matches = seen.get("matches", 1)
            if matches > 1:
                done.result["hint"] = (
                    f"The selector matched {matches} elements and the first was clicked. "
                    "If that was not the one you meant, make the selector more specific "
                    "or pick another with '>> nth=N' (0-based)."
                )
            report_dialogs(ctx, done.result)
            watch.apply(done.result)
            status = await after.fold(
                done.result, caused_by="click", declare="" if submits else "submits=true"
            )
            ctx.audit.record("click", args, status=status, origin=origin.describe())
        return done.result

    @mcp.tool
    async def type_text(
        selector: str,
        value: str,
        submit: bool = False,
        reason: str = "",
        confirm: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict:
        """Fill ``selector`` with ``value``. The value is redacted from the audit log.

        Set ``submit=true`` to press Enter afterwards, which also asks for
        permission to send the form. Note that some pages submit on typing alone;
        that is stopped unless you declared it, and answered ``blocked_by_policy``
        (the tab has not moved; ``navigate`` to the destination to be asked for it).

        Fails fast like ``click``: ``not_found``, ``hidden`` or ``disabled``
        before anyone is asked, then ``timeout``, ``element_not_actionable``
        (read-only, not a text field, detached) or ``page_closed`` within
        ``timeout_ms`` (default 10000, max 30000) for each step.

        A native dialog the page raised is answered at once and listed under
        ``dialogs`` in the reply.
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        args = {"selector": selector, "value": value, "submit": submit}
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        timeout = clamp_timeout(timeout_ms)
        locator = page.locator(selector).first
        unusable = await check_target(
            ctx,
            tool="type_text",
            page=page,
            origin=origin,
            locator=locator,
            selector=selector,
            args=args,
        )
        if unusable:
            return unusable
        denied, bought = await require(
            ctx,
            "type_text",
            target=origin,
            capabilities=_scopes(submit),
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            ctx.audit.record("type_text", args, status="needs_approval", origin=origin.describe())
            return denied
        # Typing Enter starts a navigation on purpose, so it listens as long as a click does.
        after = Aftermath(ctx, page, linger_s=ctx.config.download_settle_s if submit else LINGER_S)
        async with guard_action(
            ctx,
            bought,
            tool="type_text",
            selector=selector,
            page=page,
            origin=origin,
            args=args,
            timeout_ms=timeout,
        ) as done:
            await locator.fill(value, timeout=timeout)
            if submit:
                await locator.press("Enter", timeout=timeout)
            after.acted()
            done.result = report_dialogs(ctx, {"status": "ok", "url": page.url})
            status = await after.fold(
                done.result, caused_by="typing", declare="" if submit else "submit=true"
            )
            ctx.audit.record("type_text", args, status=status, origin=origin.describe())
        return done.result

    @mcp.tool
    async def press_key(
        key: str,
        reason: str = "",
        submits: bool = False,
        confirm: bool = False,
        download: bool = False,
    ) -> dict:
        """Press a keyboard key (e.g. 'Enter', 'Escape', 'Control+a') on the page.

        No key is treated as special: Enter, Space and anything else all send
        whatever the page decides to send, and that is judged when it happens.
        Set ``submits=true`` if you are deliberately sending a form. A key that
        makes the page navigate where no approval reaches is answered
        ``blocked_by_policy`` and the tab has not moved (see ``click``); one that
        opens a tab answers ``new_tab: true`` and ``tab_count``.

        Set ``download=true`` if the key is meant to save a file: that asks for
        permission to write one, and the reply carries ``download`` once it is
        saved. A key that starts a download without it is cancelled and answered
        ``download_blocked``; repeat it with ``download=true``.

        A native dialog the page raised is answered at once and listed under
        ``dialogs`` in the reply.
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        denied, bought = await require(
            ctx,
            "press_key",
            target=origin,
            capabilities=_scopes(submits, download),
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            return denied
        watch = ctx.downloads.watch(
            bought,
            declared=download,
            start_budget_s=DEFAULT_TIMEOUT_MS / 1000,
            settle_scale=KEYBOARD_SETTLE_SCALE,
        )
        after = Aftermath(ctx, page)
        async with released(ctx, bought):
            async with watch:
                await page.keyboard.press(key)
                after.acted()
                await watch.settle()
            reply = watch.apply(report_dialogs(ctx, {"status": "ok"}))
            status = await after.fold(
                reply, caused_by="key press", declare="" if submits else "submits=true"
            )
            ctx.audit.record("press_key", {"key": key}, status=status, origin=origin.describe())
            return reply

    @mcp.tool
    async def hover(
        selector: str,
        reason: str = "",
        confirm: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict:
        """Move the pointer over the first element matching ``selector``.

        For menus and tooltips that only open while the pointer is on their
        trigger: hover, then ``click`` the item that appeared. Nothing is
        pressed, so no form is sent by it.

        Fails fast like ``click``: ``not_found``, ``hidden`` or ``disabled``
        before anyone is asked, then ``timeout``, ``element_not_actionable``
        (covered, detached) or ``page_closed`` within ``timeout_ms`` (default
        10000, max 30000). The reply says how many elements matched (``matches``;
        the first is hovered) and what was hovered (``hovered``: tag, role, name).
        A page that navigates where no approval reaches as the pointer arrives is
        answered ``blocked_by_policy`` (see ``click``).
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        args = {"selector": selector}
        timeout = clamp_timeout(timeout_ms)
        everything = page.locator(selector)
        target = everything.first
        unusable = await check_target(
            ctx,
            tool="hover",
            page=page,
            origin=origin,
            locator=target,
            selector=selector,
            args=args,
        )
        if unusable:
            return unusable
        denied, bought = await require(
            ctx,
            "hover",
            target=origin,
            capabilities=_scopes(False),
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            return denied
        after = Aftermath(ctx, page)
        async with guard_action(
            ctx,
            bought,
            tool="hover",
            selector=selector,
            page=page,
            origin=origin,
            args=args,
            timeout_ms=timeout,
        ) as done:
            seen = await _read_target(everything, target)
            if "clicked" in seen:
                seen["hovered"] = seen.pop("clicked")
            await target.hover(timeout=timeout)
            after.acted()
            done.result = {"status": "ok", "url": page.url, **seen}
            matches = seen.get("matches", 1)
            if matches > 1:
                done.result["hint"] = (
                    f"The selector matched {matches} elements and the first was hovered. "
                    "If that was not the one you meant, make the selector more specific "
                    "or pick another with '>> nth=N' (0-based)."
                )
            status = await after.fold(done.result, caused_by="hover")
            ctx.audit.record("hover", args, status=status, origin=origin.describe())
        return done.result

    @mcp.tool
    async def scroll(
        to: str = "",
        by_y: int = 0,
        selector: str = "",
        reason: str = "",
        confirm: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict:
        """Scroll the page. Give exactly one of ``to``, ``by_y`` or ``selector``.

        - ``to='top'`` / ``to='bottom'``: jump to that end of the document.
        - ``by_y``: scroll by that many pixels, positive down and negative up
          (at most 20000 either way per call), with the mouse wheel.
        - ``selector``: bring the first matching element into view.

        Pages that load more as you reach the end (feeds, lazy lists) grow
        after a scroll, so call ``scroll(to='bottom')`` again until the reply
        says ``at_bottom``. The reply is ``scroll_y``, ``scroll_height`` and
        ``at_bottom`` once the position has settled. A ``scroll`` that moved
        nothing says so: the content may sit in an inner scrolling panel, which
        ``selector`` on an element inside it reaches.

        ``selector`` fails fast like ``click`` (``not_found``, ``hidden``,
        ``disabled``, ``timeout``, ``page_closed``).
        """
        chosen = [
            name for name, given in (("to", to), ("by_y", by_y), ("selector", selector)) if given
        ]
        if len(chosen) != 1:
            return {
                "status": "error",
                "reason": "give exactly one of to, by_y or selector",
                "given": chosen,
            }
        if to and to not in _SCROLL_ENDS:
            return {"status": "error", "reason": "to must be 'top' or 'bottom'", "given": to}
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        distance = max(-MAX_SCROLL_PX, min(by_y, MAX_SCROLL_PX))
        args = {"to": to, "by_y": distance, "selector": selector}
        timeout = clamp_timeout(timeout_ms)
        target = page.locator(selector).first if selector else None
        if target is not None:
            unusable = await check_target(
                ctx,
                tool="scroll",
                page=page,
                origin=origin,
                locator=target,
                selector=selector,
                args=args,
            )
            if unusable:
                return unusable
        denied, bought = await require(
            ctx,
            "scroll",
            target=origin,
            capabilities=_scopes(False),
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            return denied
        async with guard_action(
            ctx,
            bought,
            tool="scroll",
            selector=selector,
            page=page,
            origin=origin,
            args=args,
            timeout_ms=timeout,
        ) as done:
            before = await _scroll_state(page)
            if target is not None:
                await target.scroll_into_view_if_needed(timeout=timeout)
            elif to:
                await page.evaluate(_SCROLL_TO_JS, to)
            else:
                await page.mouse.wheel(0, distance)
            after = await _settled_scroll(page)
            ctx.audit.record("scroll", args, origin=origin.describe())
            done.result = {"status": "ok", "url": page.url, **after}
            downward = to == "bottom" or (not to and distance > 0)
            can_still_move = not after["at_bottom"] if downward else after["scroll_y"] > 0
            if target is None and after == before and can_still_move:
                done.result["hint"] = (
                    "The page did not move. The content may scroll inside a panel of its "
                    "own: pass scroll(selector=...) for an element inside it."
                )
        return done.result
