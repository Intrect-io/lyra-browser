"""Tabs: see what is open, move between tabs, close one.

The session follows onto any tab a page opens — a ``target=_blank`` link, a
``window.open`` popup — because the work moves there (see
``BrowserSession._adopt_page``). That leaves two questions no other tool
answers: which tabs exist, and how to get back to the one that was left or shut
the one that is no longer needed. A popup that closes itself needs no help; the
session returns to the tab that opened it.

Listing is a read and, like every read, does not claim the browser. Switching
and closing change what the agent is working on, so they are mutations: they
stand down for a takeover, are audited, and ask for INTERACT on the site of the
tab they act on. The tab's site, not the active page's: a tab belongs to the
site it shows, and being approved for one site is no licence to read or shut
another's. Reads are not gated by site, so once a second tab exists, choosing
which tab the reads come from is where that boundary has to hold.
"""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from ..approval import TakeoverActive
from ..context import ServerContext, acquire_page
from ..permission import Capability
from .scope import page_origin, released, require

_ACTIONS = ("list", "switch", "close")

# How long one tab gets to give its title. A tab stuck in a script loop never
# answers, and the list that lets the agent find and close it must not be the
# thing that hangs on it.
_TITLE_BUDGET_S = 1.0


async def _title_of(tab) -> str:
    """A tab's title, or ``""`` for one that will not answer in time."""
    try:
        return await asyncio.wait_for(tab.title(), _TITLE_BUDGET_S)
    except Exception:  # noqa: BLE001 — listed without a title rather than not at all
        return ""


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    async def _list() -> dict:
        page, err = await acquire_page(ctx, claim=False)
        if err:
            return err
        pages = ctx.session.open_pages()
        titles = await asyncio.gather(*(_title_of(tab) for tab in pages))
        return {
            "status": "ok",
            "tabs": [
                {"index": i, "url": tab.url, "title": title, "active": tab is page}
                for i, (tab, title) in enumerate(zip(pages, titles, strict=True))
            ],
            **ctx.session.tab_info(),
        }

    async def _change(action: str, index: int | None, reason: str, confirm: bool) -> dict:
        _, err = await acquire_page(ctx)
        if err:
            return err
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        pages = ctx.session.open_pages()
        # Arguments are judged before anyone is asked: a request that cannot be
        # carried out should neither prompt the user nor buy a scope.
        if index is None:
            return {
                "status": "error",
                "reason": f"{action} needs the index of a tab; tabs(action='list') shows them.",
                "tab_count": len(pages),
            }
        if not 0 <= index < len(pages):
            return {
                "status": "not_found",
                "reason": f"There is no tab at index {index}; tabs(action='list') shows them.",
                "tab_count": len(pages),
            }
        # Resolved once, before the wait for permission: that wait can be as long
        # as a person takes, and by then an index may mean a different tab.
        target = pages[index]
        origin = page_origin(target)
        args = {"action": action, "index": index}
        denied, bought = await require(
            ctx,
            "tabs",
            target=origin,
            capabilities=[Capability.INTERACT],
            initiator=origin,
            reason=reason or f"{action} tab {index}",
            confirm=confirm,
            subject=getattr(target, "url", ""),
        )
        if denied:
            ctx.audit.record("tabs", args, status="needs_approval", origin=origin.describe())
            return denied
        async with released(ctx, bought):
            if action == "switch":
                if not await ctx.session.activate(target):
                    ctx.audit.record("tabs", args, status="not_found", origin=origin.describe())
                    return {
                        "status": "not_found",
                        "reason": "That tab closed before it could be used.",
                        **ctx.session.tab_info(),
                    }
                ctx.audit.record("tabs", args, origin=origin.describe())
                return {
                    "status": "ok",
                    "action": "switch",
                    "index": index,
                    "url": target.url,
                    "title": await _title_of(target),
                    **ctx.session.tab_info(),
                }
            closed_url = target.url
            now = await ctx.session.close_page(target)
            ctx.audit.record("tabs", args, origin=origin.describe())
            return {
                "status": "ok",
                "action": "close",
                "closed": {"index": index, "url": closed_url},
                "url": now.url,
                "title": await _title_of(now),
                **ctx.session.tab_info(),
            }

    @mcp.tool
    async def tabs(
        action: str = "list",
        index: int | None = None,
        reason: str = "",
        confirm: bool = False,
    ) -> dict:
        """List the browser's tabs, switch to one, or close one.

        * ``action="list"`` — every open tab with its ``index``, URL, title and
          whether it is the ``active`` one (the one you read and act on).
        * ``action="switch"`` — make tab ``index`` the active one, and bring it to
          the front so the user sees what you see.
        * ``action="close"`` — close tab ``index``. Closing the active tab returns
          you to the tab that opened it, else the newest one; closing the last
          tab leaves a blank one.

        Indexes shift whenever a tab opens or closes, so list again before using
        one. Tabs a page opens are followed automatically, and a popup that
        closes itself (a sign-in window) returns you to where you were — this is
        for when you have to choose. Switching to or closing a tab asks for
        permission to use that tab's site, like any other action there;
        ``reason`` is shown to the user when they are asked.
        """
        if action not in _ACTIONS:
            return {
                "status": "error",
                "reason": f"action must be one of: {', '.join(_ACTIONS)}.",
                "given": action,
            }
        if action == "list":
            return await _list()
        return await _change(action, index, reason, confirm)
