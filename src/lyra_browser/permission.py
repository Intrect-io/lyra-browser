"""Grants — what the user allowed, scoped to an origin and a capability.

The old gate asked "is this call allowed?" one call at a time, which meant it
had to guess an action's effect from its arguments. Pages decide effects, so the
guess lost. This module holds the other half of the answer instead: a record of
what the user agreed to, narrow enough that an attacker who obtains one gets a
single site and a single kind of action rather than the whole browser.

Two rules do most of the work:

* **Opaque origins never receive a lease.** One reusable ``file://`` grant would
  cover every local file, and one ``data:`` grant every document the agent can
  author, because all of them share a single authority-less origin. So an
  approval there buys exactly one request and is asked again for the next.
* **Effects that cannot be undone are single-use.** Approving one payment must
  not mean approving every payment on that site for the next ten minutes.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from .origin import Origin


class Capability(StrEnum):
    """What a grant permits. Deliberately about effects, not about tools."""

    NAVIGATE = "navigate"
    """Load a document from this origin."""

    INTERACT = "interact"
    """Click and type while staying on this origin."""

    SUBMIT = "submit"
    """Send a non-idempotent request (POST/PUT/DELETE, or a navigation with a body)."""

    DOWNLOAD = "download"
    """Write a file to disk."""

    UPLOAD = "upload"
    """Give a file to a page — a file picker, or a drag-and-drop target."""

    PUBLISH = "publish"
    """Make content publicly visible — an item an audience can see, not a draft.

    Separate from SUBMIT on purpose: a submission is one request, while publishing
    is the irreversible, public half of it. Nothing infers this capability, and no
    reusable lease ever covers it, so every switch from draft to public is its own
    decision with its own approval.
    """


# Capabilities whose effects cannot be taken back, so they are never leased.
# UPLOAD belongs here: a file handed to a page has left this machine, and no
# later click takes it back. PUBLISH too: an audience that saw something cannot
# unsee it.
_SINGLE_USE = frozenset(
    {Capability.SUBMIT, Capability.DOWNLOAD, Capability.UPLOAD, Capability.PUBLISH}
)

# Being allowed onto a site carries permission to use it. Asking again for every
# click would train the user to approve without reading, which costs more than it
# buys — the parts that actually matter (sending a form, leaving for somewhere
# else) stay separate capabilities and are still asked.
_IMPLIED: dict[Capability, tuple[Capability, ...]] = {
    Capability.NAVIGATE: (Capability.INTERACT,),
}


@dataclass(slots=True)
class Grant:
    origin: Origin
    capability: Capability
    expires_at: float
    uses_left: int | None
    """``None`` means unlimited until expiry."""
    initiator: Origin | None
    """Where the approved action was to be started from. ``None`` = unconstrained."""
    subject_url: str = ""
    """The one URL this grant covers, for origins that cannot tell URLs apart.

    Every ``file:`` URL is the same origin, so an origin-wide grant there is a
    grant over the whole disk. The consent prompt names a specific file; this is
    what makes the grant mean what the prompt said.
    """

    def is_live(self, now: float) -> bool:
        if now >= self.expires_at:
            return False
        return self.uses_left is None or self.uses_left > 0

    def covers(
        self,
        origin: Origin,
        capability: Capability,
        initiator: Origin | None,
        url: str = "",
    ) -> bool:
        if self.capability is not capability or self.origin != origin:
            return False
        if self.subject_url and self.subject_url != url:
            return False
        if self.initiator is None:
            return True
        # A grant earned from one page does not license another page to drive the
        # same destination — that is how one approved bank visit would otherwise
        # become an open door for every hostile page afterwards.
        return initiator is not None and self.initiator == initiator


class PermissionStore:
    """Per-session book of live grants.

    Sessions are kept apart because the server can serve several clients over
    HTTP; one client's approval must not authorise another's.
    """

    def __init__(self, ttl_s: float = 600.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._clock = clock  # monotonic by default: wall-clock jumps must not extend a lease
        self._grants: dict[str, list[Grant]] = {}

    def grant(
        self,
        session: str,
        origin: Origin,
        capability: Capability,
        initiator: Origin | None = None,
        subject: str = "",
    ) -> Grant | None:
        """Record an approval, scoped to one request when the origin is opaque.

        Refusing outright was wrong once enforcement became the default: the
        approval produced nothing, the guard then found nothing, and an approved
        ``file://`` navigation was refused — the user said yes and the browser
        said no. One use satisfies both halves, since it authorises the request
        that was approved and expires on it.
        """
        self._prune_all()
        now = self._clock()
        book = self._grants.setdefault(session, [])
        # A one-shot approval is never merged into an existing one. Sharing the
        # object shares the single use and the release timer with it, so one
        # call's cleanup retired a later call's fresh approval — the user said
        # yes and the browser said no, seconds apart.
        existing = (
            None
            if self.is_one_shot(origin, capability)
            else next(
                (
                    g
                    for g in book
                    if g.covers(origin, capability, initiator) and g.initiator == initiator
                ),
                None,
            )
        )
        if existing is not None:
            # Re-approving the same scope renews it rather than stacking another
            # object; otherwise a long session grows a grant list it scans linearly.
            existing.expires_at = now + self._ttl_s
            if existing.uses_left is not None:
                existing.uses_left = max(existing.uses_left, 1)
            return existing
        entry = self._make(origin, capability, now, initiator, subject)
        book.append(entry)
        for implied in _IMPLIED.get(capability, ()):
            # Implied scopes are unconstrained by initiator: the point is that
            # being on a site lets you use it, whoever sent you there.
            book.append(self._make(origin, implied, now, None))
        return entry

    @staticmethod
    def is_one_shot(origin: Origin, capability: Capability) -> bool:
        """One use only — an effect that cannot be undone, or an opaque origin.

        Public because the consent broker must know it: a one-shot grant belongs
        to the single action that bought it, so it can never stand in as "already
        approved" for a later one.
        """
        return capability in _SINGLE_USE or origin.is_opaque

    def _make(
        self,
        origin: Origin,
        capability: Capability,
        now: float,
        initiator: Origin | None,
        subject: str = "",
    ) -> Grant:
        return Grant(
            origin=origin,
            capability=capability,
            expires_at=now + self._ttl_s,
            uses_left=1 if self.is_one_shot(origin, capability) else None,
            initiator=initiator,
            # Only an origin that cannot distinguish its own URLs needs pinning.
            subject_url=subject if (origin.is_opaque and subject) else "",
        )

    def check(
        self,
        session: str,
        origin: Origin,
        capability: Capability,
        initiator: Origin | None = None,
        url: str = "",
    ) -> bool:
        """Is this scope already covered? Does not spend a single-use grant."""
        return self._find(session, origin, capability, initiator, url) is not None

    def consume(
        self,
        session: str,
        origin: Origin,
        capability: Capability,
        initiator: Origin | None = None,
        url: str = "",
    ) -> bool:
        """Check and spend. Use this at the point the action actually happens."""
        found = self._find(session, origin, capability, initiator, url)
        if found is None:
            return False
        if found.uses_left is not None:
            found.uses_left -= 1
        return True

    def release_unspent(self, grants: Iterable[Grant]) -> None:
        """Retire single-use grants an action bought but did not use.

        A SUBMIT bought for one click must not outlive that click. Declaring a
        GET form buys SUBMIT, the request that follows classifies as a plain
        navigation, and nothing spends it — leaving a ten-minute bearer ticket
        that any later POST could ride.
        """
        for entry in grants:
            if self.is_one_shot(entry.origin, entry.capability) and entry.uses_left:
                entry.uses_left = 0

    def revoke_all(self, session: str | None = None) -> None:
        """Drop grants — for a session, or for every session when given none."""
        if session is None:
            self._grants.clear()
        else:
            self._grants.pop(session, None)

    def find_live(
        self,
        session: str,
        origin: Origin,
        capability: Capability,
        initiator: Origin | None = None,
        url: str = "",
    ) -> Grant | None:
        """The live grant covering this scope, without renewing or spending it.

        ``grant()`` would extend the expiry, which quietly turns a ten-minute
        bound into a sliding window that any busy session keeps alive forever.
        """
        return self._find(session, origin, capability, initiator, url)

    def live_grants(self, session: str) -> list[Grant]:
        """Live grants for a session, oldest first. For audit and inspection."""
        self._prune_all()
        return list(self._grants.get(session, []))

    def _find(
        self,
        session: str,
        origin: Origin,
        capability: Capability,
        initiator: Origin | None,
        url: str = "",
    ) -> Grant | None:
        self._prune_all()
        for entry in self._grants.get(session, []):
            if entry.covers(origin, capability, initiator, url):
                return entry
        return None

    def _prune_all(self) -> None:
        """Drop dead grants everywhere, not just where someone happened to look.

        Pruning only the session being queried leaves finished sessions in the
        book forever, and a long-lived server accumulates them silently.
        """
        now = self._clock()
        for session in list(self._grants):
            live = [g for g in self._grants[session] if g.is_live(now)]
            if live:
                self._grants[session] = live
            else:
                del self._grants[session]
