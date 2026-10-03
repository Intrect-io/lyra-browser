"""Navigation tools: open the window and move between pages."""

from __future__ import annotations

from fastmcp import FastMCP

from ..approval import TakeoverActive
from ..context import (
    ServerContext,
    acquire_page,
    claim_session,
    current_session_key,
    release_ownership,
)
from ..downloads import EVENT_WAIT_S
from ..enforcement import refused_by_guard, refused_envelope, wait_unless_refused
from ..origin import parse_origin
from ..permission import Capability
from .scope import released, require

# How long a navigation waits, by Playwright's own names. ``domcontentloaded``
# stays the default: the document is parsed and usable, and a site that holds a
# connection open forever (analytics, long-poll) never reaches ``networkidle``.
# A page that draws itself after loading is ``wait_for``'s job, not this one's.
_WAIT_UNTIL = ("domcontentloaded", "load", "networkidle", "commit")


def _bad_wait_until(wait_until: str) -> dict | None:
    """The error envelope for a ``wait_until`` the driver would reject, or None.

    Checked before anything else happens, so a typo costs no approval prompt, no
    grant and no page load.
    """
    if wait_until in _WAIT_UNTIL:
        return None
    return {
        "status": "error",
        "reason": f"wait_until must be one of: {', '.join(_WAIT_UNTIL)}.",
        "given": wait_until,
    }


def _response_facts(response) -> dict:
    """What the server said about the document a navigation landed on.

    ``goto`` used to be awaited and dropped, so a 404, a 500 and a PDF all came
    back as a bare ``ok``. ``content_type`` is the media type alone, lower-cased:
    its parameters (``charset``, ``boundary``) are not what a caller branches on.
    Both are ``None`` when there is no HTTP response to read — ``about:blank``,
    ``data:``, a history step the browser served without asking the server.
    """
    if response is None:
        return {"http_status": None, "content_type": None}
    raw = next((v for k, v in response.headers.items() if k.lower() == "content-type"), "")
    return {
        "http_status": response.status or None,
        "content_type": raw.split(";", 1)[0].strip().lower() or None,
    }


def _download_started(exc: Exception) -> bool:
    """Whether a navigation error is the browser turning the URL into a download.

    The driver reports it as an error rather than a response and its text is the
    only thing to go on. It is not a failure: the tab stays where it was.
    """
    return "Download is starting" in str(exc)


def _scopes(leaving: bool, download: bool) -> list[Capability]:
    """Being on the site is always needed; saving a file only when declared."""
    scopes = [Capability.NAVIGATE if leaving else Capability.INTERACT]
    if download:
        scopes.append(Capability.DOWNLOAD)
    return scopes


_UNSEEN_HINT = (
    "The browser said a download was starting, but none was reported to this server, so "
    "it wrote no file to the download dir. The tab stayed where it was."
)
_CANNOT_DECLARE_HINT = (
    "A file download started and was cancelled: nothing was saved. To download it, "
    "call navigate with the file's URL and download=true."
)
_NOT_A_FILE_HINT = (
    "download=true was declared, but that URL loaded as a page (see content_type), "
    "not a file, so there was nothing to download. The permission was released."
)


async def _download_result(watch, page, requested: str = "", *, can_declare: bool = True) -> dict:
    """The reply for a navigation the browser turned into a file download.

    The driver reports that as an error, and the tab stays where it was. What
    became of the file is the watch's to say: ``download`` when the call declared
    it and was allowed, ``download_blocked`` when it was cancelled for want of a
    permission. ``go_back`` and ``reload_page`` cannot declare one, so their hint
    points at ``navigate``.
    """
    await watch.settle(coming=True)
    result: dict = {"status": "ok", "url": page.url}
    if requested:
        result["requested_url"] = requested
    watch.apply(result)
    if result["status"] == "ok" and "download" not in result:
        # The driver said one was starting and the browser never reported it.
        result["status"] = "download_started"
        result["hint"] = _UNSEEN_HINT
    elif result["status"] == "download_blocked" and not can_declare:
        result["hint"] = _CANNOT_DECLARE_HINT
    return result


async def _refused_redirect(ctx: ServerContext, since: int) -> str:
    """Where a redirect refused while this call ran was sending the browser, or ``""``.

    A server that answers before the guard's cancellation lands (loopback, a LAN) lets
    the load finish, so the driver reports success for a navigation the guard refused.
    Asked once the cancellation has had its say, this is how the call finds out.
    """
    return await ctx.guard.refused_redirect_since(since) if ctx.guard else ""


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def open_browser() -> dict:
        """Launch (or focus) the visible browser window the user shares with you.

        If no browser is installed this returns a ``browser_unavailable`` envelope
        (relay it so the user can install Chrome) rather than failing.
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        browser = ctx.session.active_channel or "chromium"
        # Opening the browser starts over. Grants outlive a tool call by design
        # — that is what stops every click being re-asked — but they must not
        # outlive the task that earned them, and this is the boundary the agent
        # itself marks. Clearing only ever asks the user more, never less.
        ctx.perms.revoke_all(current_session_key(ctx))
        ctx.audit.record("open_browser", detail=browser)
        result = {
            "status": "ok",
            "url": page.url,
            "tab_count": ctx.session.tab_info()["tab_count"],
            "browser": browser,
            "driver": getattr(ctx.session, "active_driver", None),
            "attended": ctx.config.attended,
            "profile": getattr(ctx.session, "profile_mode", "shared"),
        }
        notes = []
        if result["profile"] == "instance":
            notes.append(
                "Another lyra-browser server holds the saved profile, so this window "
                "is a private, empty one: no saved logins in this instance. Log in "
                "again here, or have the other session release the browser."
            )
        if not ctx.config.attended:
            notes.append(
                "Headless: no window is shown, so the user cannot see this session "
                "or take it over. Collaboration tools are unavailable — do not wait "
                "for a human."
            )
        if notes:
            result["note"] = " ".join(notes)
        return result

    @mcp.tool
    async def close_browser(reason: str = "") -> dict:
        """Close the shared window, discarding every approval it earned.

        This is how a session finishes: the window goes away, the grants go with
        it, and the next session may claim the browser. Without it the refusal
        another session receives would name a wait that never ends.
        """
        # Claim first, exactly as acquire_page does. Closing is a way to end a
        # turn, not a way around whose turn it is: without this a non-owner
        # could shut the holder's window and drop the grants it was using.
        conflict = await claim_session(ctx)
        if conflict is not None:
            return conflict
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        if not ctx.session.live:
            # `live`, not `started`: mid-launch the window is on its way and
            # reporting "was not open" would skip the revocation and leave one
            # standing that nobody believes in.
            #
            # Still release. Claiming happens above, so an already-closed browser
            # would otherwise leave this caller owning a window that does not
            # exist, told the close had succeeded.
            await release_ownership(ctx, close=False)
            return {"status": "ok", "note": "The browser was not open."}
        await release_ownership(ctx, close=True)
        ctx.audit.record("close_browser", detail=reason or None)
        return {"status": "ok"}

    @mcp.tool
    async def navigate(
        url: str,
        reason: str = "",
        confirm: bool = False,
        wait_until: str = "domcontentloaded",
        download: bool = False,
    ) -> dict:
        """Navigate the shared window to a URL.

        Going to a different site asks for permission to be on that site.
        Moving within the current one does not. A URL with no host — ``file:``,
        ``data:``, ``javascript:`` — is never "the same site" and is always
        asked, every time, because one approval there would cover every local
        file or every document the agent can author.

        ``ok`` means the navigation happened, not that the page is good: read
        ``http_status`` (404, 500 ...) and ``content_type`` (``application/pdf``
        ...) of the page that answered. Both are null when no HTTP response was
        involved (``about:blank``, ``data:``). ``wait_until`` is how much of the
        load to wait for: ``domcontentloaded`` (default), ``load``, ``commit`` or
        ``networkidle`` — which never settles on a page that keeps polling. A page
        that draws itself after it has loaded needs ``wait_for``. A URL that turns
        out to be a file download does not move the tab: with ``download=true`` —
        which asks for permission to write a file — it is saved and the reply
        carries ``download`` (``filename``, ``path``, ``bytes``, ``url_origin``);
        without it the download is cancelled and answered ``download_blocked``, so
        repeat the call with ``download=true``. The permission pays for one file and
        ends with the call.
        """
        if (invalid := _bad_wait_until(wait_until)) is not None:
            return invalid
        page, err = await acquire_page(ctx)
        if err:
            return err
        target = parse_origin(url)
        current = parse_origin(page.url)
        audit_kw = {"origin": target.describe(), "initiator": current.describe()}
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        leaving = not target.same_site_as(current)
        denied, bought = await require(
            ctx,
            "navigate",
            target=target,
            capabilities=_scopes(leaving, download),
            initiator=current,
            reason=reason,
            confirm=confirm,
            subject=url,
        )
        if denied:
            ctx.audit.record("navigate", {"url": url}, status="needs_approval", **audit_kw)
            return denied
        async with released(ctx, bought):
            refused_before = ctx.guard.refusals if ctx.guard else 0
            watch = ctx.downloads.watch(bought, declared=download, start_budget_s=EVENT_WAIT_S)
            async with watch:
                try:
                    response = await wait_unless_refused(
                        ctx.guard, page.goto(url, wait_until=wait_until), refused_before
                    )
                except Exception as exc:  # noqa: BLE001
                    turned_away = ctx.guard is not None and ctx.guard.refusals > refused_before
                    if not (turned_away and refused_by_guard(exc)):
                        if _download_started(exc):
                            ctx.audit.record(
                                "navigate", {"url": url}, status="download_started", **audit_kw
                            )
                            return await _download_result(watch, page, url)
                        # Not ours: a site that went away, a real error.
                        raise
                    ctx.audit.record("navigate", status="blocked_by_policy")
                    return refused_envelope(page.url, redirected_to=ctx.guard.refused_hop)
            if hop := await _refused_redirect(ctx, refused_before):
                ctx.audit.record("navigate", status="blocked_by_policy")
                return refused_envelope(page.url, redirected_to=hop)
            ctx.audit.record("navigate", {"url": url}, **audit_kw)
            result = {
                "status": "ok",
                "url": page.url,
                "title": await page.title(),
                **_response_facts(response),
            }
            if download:
                # Declared, and the URL was a page: there is no file to wait for.
                result["status"] = "download_not_started"
                result["hint"] = _NOT_A_FILE_HINT
            return result

    @mcp.tool
    async def go_back(
        reason: str = "", confirm: bool = False, wait_until: str = "domcontentloaded"
    ) -> dict:
        """Go back one entry in history.

        Where "back" leads is not known until it happens, so this asks only to
        interact with the current site; landing somewhere else is judged then.
        Reports ``http_status`` and ``content_type`` like ``navigate`` — both
        null when the browser restored the page without asking the server.
        """
        if (invalid := _bad_wait_until(wait_until)) is not None:
            return invalid
        page, err = await acquire_page(ctx)
        if err:
            return err
        current = parse_origin(page.url)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        denied, bought = await require(
            ctx,
            "go_back",
            target=current,
            capabilities=[Capability.INTERACT],
            initiator=current,
            reason=reason,
            confirm=confirm,
        )
        if denied:
            return denied
        async with released(ctx, bought):
            refused_before = ctx.guard.refusals if ctx.guard else 0
            async with ctx.downloads.watch() as watch:
                try:
                    response = await wait_unless_refused(
                        ctx.guard, page.go_back(wait_until=wait_until), refused_before
                    )
                except Exception as exc:  # noqa: BLE001
                    turned_away = ctx.guard is not None and ctx.guard.refusals > refused_before
                    if not (turned_away and refused_by_guard(exc)):
                        if _download_started(exc) or await watch.began_after_abort(exc):
                            ctx.audit.record(
                                "go_back", status="download_started", origin=current.describe()
                            )
                            return await _download_result(watch, page, can_declare=False)
                        # Not ours: a site that went away, a real error.
                        raise
                    ctx.audit.record("go_back", status="blocked_by_policy")
                    return refused_envelope(page.url, redirected_to=ctx.guard.refused_hop)
            if hop := await _refused_redirect(ctx, refused_before):
                ctx.audit.record("go_back", status="blocked_by_policy")
                return refused_envelope(page.url, redirected_to=hop)
            ctx.audit.record("go_back", origin=current.describe())
            return {"status": "ok", "url": page.url, **_response_facts(response)}

    @mcp.tool
    async def reload_page(
        reason: str = "", confirm: bool = False, wait_until: str = "domcontentloaded"
    ) -> dict:
        """Reload the current page. Reports ``http_status`` and ``content_type``
        like ``navigate``."""
        if (invalid := _bad_wait_until(wait_until)) is not None:
            return invalid
        page, err = await acquire_page(ctx)
        if err:
            return err
        current = parse_origin(page.url)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        denied, bought = await require(
            ctx,
            "reload_page",
            target=current,
            capabilities=[Capability.INTERACT],
            initiator=current,
            reason=reason,
            confirm=confirm,
        )
        if denied:
            return denied
        async with released(ctx, bought):
            refused_before = ctx.guard.refusals if ctx.guard else 0
            async with ctx.downloads.watch() as watch:
                try:
                    response = await wait_unless_refused(
                        ctx.guard, page.reload(wait_until=wait_until), refused_before
                    )
                except Exception as exc:  # noqa: BLE001
                    turned_away = ctx.guard is not None and ctx.guard.refusals > refused_before
                    if not (turned_away and refused_by_guard(exc)):
                        if _download_started(exc) or await watch.began_after_abort(exc):
                            ctx.audit.record(
                                "reload_page", status="download_started", origin=current.describe()
                            )
                            return await _download_result(watch, page, can_declare=False)
                        # Not ours: a site that went away, a real error.
                        raise
                    ctx.audit.record("reload_page", status="blocked_by_policy")
                    return refused_envelope(page.url, redirected_to=ctx.guard.refused_hop)
            if hop := await _refused_redirect(ctx, refused_before):
                ctx.audit.record("reload_page", status="blocked_by_policy")
                return refused_envelope(page.url, redirected_to=hop)
            ctx.audit.record("reload_page", origin=current.describe())
            return {"status": "ok", "url": page.url, **_response_facts(response)}
