"""Collaboration tools: the human-in-the-loop primitives.

These are what make the window *shared* rather than just *visible*: the agent
can spotlight an element, ask the user to perform a step it should not do
itself (passwords, CAPTCHAs, payment), and hand the wheel back and forth.

Every one of them assumes somebody is looking at the screen. In headless mode
nobody is, so they refuse with an ``unattended`` envelope instead of reporting
success for something no human will ever see. Refusing also keeps
``request_takeover`` from deadlocking a headless run: it would otherwise block
every mutation while waiting for a user who cannot exist.
"""

from __future__ import annotations

from fastmcp import FastMCP

from ..approval import TakeoverActive
from ..context import ServerContext, acquire_page, claim_session
from ..origin import parse_origin
from ..permission import Capability
from .scope import require

# Injected once per call: draws a temporary outline + scrolls the element in view.
_HIGHLIGHT_JS = """
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  const prev = el.style.outline;
  el.style.outline = '3px solid #ff2d55';
  el.style.outlineOffset = '2px';
  setTimeout(() => { el.style.outline = prev; }, 4000);
  return true;
}
"""


def _unattended(reason: str, hint: str) -> dict:
    """Envelope for a collaboration tool called when no human is watching."""
    return {"status": "unattended", "reason": reason, "hint": hint}


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def highlight_element(selector: str) -> dict:
        """Outline an element in the shared window so the user can see what you mean.

        Requires an attended (headful) session — in headless mode there is no one
        to show it to, so this returns ``unattended``.
        """
        if not ctx.config.attended:
            ctx.audit.record("highlight_element", {"selector": selector}, status="unattended")
            return _unattended(
                "Headless session: nobody can see a highlight.",
                "Describe the element in text instead, or read the page and act directly.",
            )
        page, err = await acquire_page(ctx)
        if err:
            return err
        # This injects a style and scrolls the viewport — it writes to the page,
        # so it waits like every other write while the user holds the session.
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        found = await page.evaluate(_HIGHLIGHT_JS, selector)
        status = "ok" if found else "not_found"
        ctx.audit.record("highlight_element", {"selector": selector}, status=status)
        return {"status": status, "selector": selector}

    @mcp.tool
    async def ask_user_to_do(instruction: str, selector: str | None = None) -> dict:
        """Ask the user to perform a step you must not do yourself.

        Use for credentials, CAPTCHAs, 2FA, or payment confirmation. Highlights
        the relevant element when ``selector`` is given, then returns an
        ``awaiting_user`` envelope. Relay the instruction, then poll the page
        (read_page / get_url) to detect completion.

        Requires an attended (headful) session — in headless mode there is no
        user to ask, so this returns ``unattended`` rather than making you wait.
        """
        if not ctx.config.attended:
            ctx.audit.record(
                "ask_user_to_do",
                {"instruction": instruction, "selector": selector},
                status="unattended",
            )
            return _unattended(
                "Headless session: there is no user at this window to ask.",
                "Do not wait for a human. Either complete the task yourself or stop "
                "and report that this step needs an attended session.",
            )
        page, err = await acquire_page(ctx)
        if err:
            return err
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        if selector:
            await page.evaluate(_HIGHLIGHT_JS, selector)
        ctx.audit.record("ask_user_to_do", {"instruction": instruction, "selector": selector})
        return {
            "status": "awaiting_user",
            "instruction": instruction,
            "selector": selector,
            "hint": "Relay this to the user; when they finish, re-read the page to continue.",
        }

    @mcp.tool
    async def request_takeover(reason: str) -> dict:
        """Hand control to the user. Agent mutations are blocked until they hand back.

        Requires an attended (headful) session. In headless mode the takeover is
        refused and no state changes: blocking every mutation while waiting for a
        user who cannot see the window would deadlock the run.
        """
        if not ctx.config.attended:
            ctx.audit.record("request_takeover", {"reason": reason}, status="unattended")
            return _unattended(
                "Headless session: there is no visible window for the user to take over.",
                "Control was NOT handed over and you are not blocked. Continue, or stop "
                "and report that this step needs an attended session.",
            )
        # Claiming the session first: taking over freezes every mutating tool,
        # so a session that does not hold the browser must not be able to do it
        # to the one that does.
        conflict = await claim_session(ctx)
        if conflict is not None:
            return conflict
        # This tool never touches the browser, so it would not meet a lost guard the way
        # every page-taking tool does. A window that was closed has nothing to hand over.
        lost = getattr(ctx.session, "guard_lost", None)
        if lost is not None:
            return lost.envelope()
        ctx.collab.takeover = True
        ctx.collab.takeover_reason = reason
        ctx.audit.record("request_takeover", {"reason": reason})
        return {
            "status": "takeover_active",
            "reason": reason,
            "hint": "Wait for the user. Call resume_after_takeover when they return control.",
        }

    @mcp.tool
    async def resume_after_takeover(reason: str = "") -> dict:
        """Resume agent control after the user has handed the session back.

        Releasing a takeover goes through the consent channel: an agent that can
        clear its own lock can ignore the lock. On a client without elicitation
        this still succeeds — the release is recorded as the model's own
        assertion, which is what ``consent_channel=legacy`` in the audit means.
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = parse_origin(page.url)
        if ctx.collab.takeover:
            denied, bought = await require(
                ctx,
                "resume_after_takeover",
                target=origin,
                capabilities=[Capability.INTERACT],
                initiator=origin,
                reason=reason or "The user is handing control back.",
                confirm=True,
                subject=getattr(page, "url", ""),
            )
            if denied:
                return TakeoverActive(
                    ctx.collab.takeover_reason or "The user still holds the session."
                ).envelope()
            # Clearing a flag issues no request, so nothing can spend this later.
            ctx.perms.release_unspent(bought)
        ctx.collab.takeover = False
        ctx.collab.takeover_reason = ""
        ctx.audit.record("resume_after_takeover", origin=origin.describe())
        return {"status": "ok", "url": page.url, "title": await page.title()}
