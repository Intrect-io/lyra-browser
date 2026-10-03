"""Shared server context passed to every tool registration module."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .approval import CollaborationState
from .audit import AuditLog
from .config import Config
from .consent import ConsentBroker
from .downloads import Downloads
from .enforcement import NavigationGuard
from .permission import PermissionStore
from .session import BrowserClosed, BrowserSession, BrowserUnavailable, GuardLost

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass(slots=True)
class ServerContext:
    config: Config
    session: BrowserSession
    audit: AuditLog
    collab: CollaborationState
    # Assembled from the rest when not supplied, so every existing construction
    # site keeps working unchanged.
    perms: PermissionStore | None = None
    consent: ConsentBroker | None = None
    guard: NavigationGuard | None = None
    owner_session: str | None = None
    """The one session allowed to drive this browser. See claim_session."""
    owner_seen_at: float = 0.0
    """When the owner last used the browser, for handing it on if they vanish."""
    claim_lock: asyncio.Lock | None = None
    """Serialises claiming and releasing. See claim_session."""
    downloads: Downloads | None = None
    """Judges every file a tab is offered, and saves the ones asked for. See downloads.py."""

    def __post_init__(self) -> None:
        if self.perms is None:
            self.perms = PermissionStore(ttl_s=self.config.grant_ttl_s)
        if self.consent is None:
            self.consent = ConsentBroker(self.config, self.perms, self.audit, collab=self.collab)
        if self.guard is None:
            self.guard = NavigationGuard(self.config, self.perms, self.audit, collab=self.collab)
        if self.claim_lock is None:
            self.claim_lock = asyncio.Lock()
        if self.downloads is None:
            self.downloads = Downloads(
                self.config,
                self.perms,
                self.audit,
                collab=self.collab,
                # Read when a file arrives, from a Playwright callback that cannot
                # ask the live request; the tools leave the session key on the guard.
                session_key=lambda: self.guard.session_key,
            )
        # The session puts this on every tab it adopts.
        self.session.download_handler = self.downloads.on_download
        # The session puts this in front of the browser when it starts: as a Playwright
        # route handler (and, for redirect hops, the guard's request listener), or behind the
        # CDP sidecar, whichever config.guard_backend says.
        self.session.guard = self.guard


def _live_session_id() -> str:
    try:
        from fastmcp.server.dependencies import get_context

        return get_context().session_id or "default"
    except Exception:  # noqa: BLE001 — no live request (tests, direct use)
        return "default"


def _conflict(ctx: ServerContext) -> dict:
    """The envelope a session that does not hold the browser gets back."""
    ctx.audit.record("session", {"holder": ctx.owner_session}, status="session_conflict")
    return {
        "status": "session_conflict",
        "reason": "Another session already holds this browser.",
        "hint": (
            "One browser is driven by one session at a time — approvals cannot be "
            "kept apart otherwise. The browser is handed on when the holder closes "
            f"it, or after {ctx.config.owner_idle_timeout_s:.0f}s without a call — and "
            "the window closes with it. Or run a second server with its own data dir."
        ),
    }


async def release_ownership(ctx: ServerContext, *, close: bool) -> None:
    """Locked wrapper. See :func:`_release_ownership` for what it does."""
    async with ctx.claim_lock:
        await _release_ownership(ctx, close=close)


async def _release_ownership(ctx: ServerContext, *, close: bool) -> None:
    """Let go of the browser: grants go with the window, always.

    One function because the two ways this happens kept drifting apart. The
    grants must not survive either — an incoming session inheriting what the
    last one was approved for is the whole thing this boundary exists to stop.

    ``close`` is for handing a *running* browser to someone else: the previous
    holder's document is still loaded and can still start navigations, which the
    guard would judge, and charge, against whoever now holds the session.
    """
    ctx.perms.revoke_all()
    if close:
        await ctx.session.stop()
    ctx.owner_session = None


async def claim_session(ctx: ServerContext, *, claim: bool = True) -> dict | None:
    """Bind this server to one session, or refuse. Returns an envelope on refusal.

    ``claim=False`` only checks. Reading is not driving: a passive client that
    claimed by reading could lock the browser it never uses, and — once an idle
    holder can be displaced — close the window of a session that was still
    working, on the strength of a screenshot.

    Storing grants per session is only half a defence. The other half would be
    the enforcement layer knowing whose request it is judging — and it cannot:
    every session drives the *same* browser, so their traffic is one stream, and
    the route handler has no way back to a request context. Whichever session
    called a tool most recently would decide how the next request is judged, and
    one client's single-use approval would be spent by another's traffic.

    Keying the guard harder does not fix that; sharing one browser is what makes
    it unfixable. So the server serves one session at a time and says so, rather
    than offering an isolation it cannot deliver.
    """
    async with ctx.claim_lock:
        return await _claim_locked(ctx, claim=claim)


async def _claim_locked(ctx: ServerContext, *, claim: bool) -> dict | None:
    """The claim itself. Ownership is read, awaited across, and written here, so
    two sessions arriving together must not interleave — one would be admitted
    while the other's handoff erased it."""
    key = _live_session_id()
    if not claim:
        if ctx.owner_session is not None and ctx.owner_session != key and ctx.session.live:
            return _conflict(ctx)
        if ctx.owner_session == key:
            # Reading is using. It does not make anyone the driver, but a holder
            # part-way through a read-only pass is not idle — without this the
            # handoff would close the window they are reading from.
            ctx.owner_seen_at = time.monotonic()
        return None
    idle_for = time.monotonic() - ctx.owner_seen_at
    if (
        ctx.owner_session is not None
        and ctx.owner_session != key
        and idle_for > ctx.config.owner_idle_timeout_s
    ):
        # A holder that stops calling never gets to release: an HTTP client can
        # crash or drop with its window open, and closing is owner-only. Without
        # this the envelope tells the next session to wait for someone who is
        # already gone. Only an *idle* holder is displaced, never a working one.
        ctx.audit.record(
            "session",
            {"holder": ctx.owner_session, "idle_s": round(idle_for)},
            status="owner_timed_out",
        )
        await _release_ownership(ctx, close=True)
    elif ctx.owner_session is not None and not ctx.session.live:
        # A closed window has no holder. Ownership had no release path at all
        # before this, so "one session at a time" was really "the first session,
        # forever" — over HTTP the second client could never be served.
        await _release_ownership(ctx, close=False)
    if ctx.owner_session is None:
        ctx.owner_session = key
    elif ctx.owner_session != key:
        return _conflict(ctx)
    if ctx.guard is not None:
        ctx.guard.session_key = key
    ctx.owner_seen_at = time.monotonic()
    return None


def current_session_key(ctx: ServerContext) -> str:
    """The session this server is bound to."""
    return ctx.owner_session or _live_session_id()


async def acquire_page(
    ctx: ServerContext, *, claim: bool = True
) -> tuple[Page | None, dict | None]:
    """Get the active page, or an envelope to return as-is.

    Centralises the "no browser installed" degradation so every tool stays usable
    for an end user who only has VEGA.app and may not have Chrome yet.

    Only a claiming caller may bring a closed browser back. A read gets the
    ``browser_closed`` envelope instead: it can use a tab that is still open, but
    putting a window back on the screen the user closed is not something reading does.
    """
    conflict = await claim_session(ctx, claim=claim)
    if conflict is not None:
        return None, conflict
    if claim:
        # A call that drives the browser: what a handle_dialog answer is bound to. A read
        # does not count, so it neither spends the answer nor outlives it.
        ctx.session.dialogs.begin_call()
    try:
        if claim:
            return await ctx.session.page(), None
        return await ctx.session.page(revive=False), None
    except BrowserUnavailable as exc:
        ctx.audit.record("open_browser", status="browser_unavailable")
        return None, exc.envelope()
    except BrowserClosed as exc:
        return None, exc.envelope()
    except GuardLost as exc:
        return None, exc.envelope()
