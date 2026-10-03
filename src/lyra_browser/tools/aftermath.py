"""What an action set off that its own reply would not say.

``click`` answers when the driver reports the click done. Two things can still be
true of that moment, and the agent's next move depends on them:

- **The guard refused a navigation the action caused.** The 204 leaves the tab where
  it was, so a reply that says ``ok`` with the same ``url`` reads as a click that did
  nothing — and the audit trail, which the agent does not read, is the only place that
  says why. The reply is ``blocked_by_policy`` instead, with the fields it already
  had (``matches``, ``clicked``, ``dialogs``) kept.
- **The action opened a tab.** The session follows onto it (``BrowserSession._adopt_page``)
  while the reply's ``url`` is still the page the action was made on, so without a word
  the agent keeps reasoning about a page it is no longer reading.

When is the check made? Measured (Chrome 154, playwright and patchright 1.63, route and cdp
guard, headless and headful, ``scripts/verify_click_refusal_e2e.py`` and the lateness probe
behind it): the driver holds a ``click`` and a ``press`` until the navigation they started
has been judged, so that verdict is in 0.5 to 48 ms *before* the call returns. What it does
not hold for lands after: the tab of a popup 3 to 12 ms later, a form posted from a frame up
to 20 ms, a page's own ``setTimeout(0)`` about 20 ms — and for ``hover`` and ``fill``, which
are not held at all, the navigation reaches the guard 1 to 8 ms after they return. Every
action that can start a download already listens for ``download_settle_s`` (100 ms, 250 ms
for a key) after it acts, which covers all of the above with room to spare — so a click pays
nothing for this check.

An action with no such window (``hover``, plain ``type_text``) lingers for ``LINGER_S`` after
the driver returns (a typed Enter, which starts a navigation on purpose, for the whole settle
window like a click), and only while nothing has shown: the moment a refusal, a new tab or a new
URL appears it is over, so an action whose effect is visible does not wait at all, and the
wait is counted from when the driver returned, so a call that has already listened longer
adds nothing. What a page does later still than that (a timer of its own) is on the audit
trail and nowhere else.
"""

from __future__ import annotations

import asyncio
import time

from ..context import ServerContext
from ..enforcement import refused_envelope

# How long a verdict or a tab that has not shown yet is waited for, counted from when the
# driver's call returned: five times the slowest a hover or a fill was seen to bring its
# navigation to the guard (7.8 ms in 240 runs over every backend, driver and mode), and no
# more than ``download_settle_s`` — an operator who turned listening after an action off
# (0) has this off too.
LINGER_S = 0.04
_POLL_S = 0.004

# A reply in one of these states did not achieve what the agent asked for, so a refusal
# is the better explanation of it. A saved file (``download``), and a download the browser
# started and the call cancelled, are results of their own and keep their status.
_REPLACEABLE = frozenset({"ok", "download_not_started"})

_NEW_TAB_HINT = (
    "A new tab opened and it is now the one you read and act on, not the page this action "
    "was made on. tabs(action='list') shows both, tabs(action='switch', index=N) goes back."
)


class Aftermath:
    """The guard's refusals, the open tabs and the URL before an action; its reply is folded after.

    Create it before the action, call ``acted()`` the moment the driver's call returns, and
    ``fold()`` the reply once the tool has listened for whatever else it listens for.
    """

    __slots__ = ("_acted", "_ctx", "_linger_s", "_page", "_refusals", "_tabs", "_url")

    def __init__(self, ctx: ServerContext, page, *, linger_s: float = LINGER_S) -> None:
        """``linger_s``: how long to wait for a verdict that has not shown, counted from when the
        driver returns. An action that deliberately starts a navigation (typing Enter) passes
        the whole settle window, as a click listens for it."""
        self._ctx = ctx
        self._linger_s = linger_s
        self._page = page
        self._refusals = ctx.guard.refusals if ctx.guard else 0
        self._tabs = ctx.session.open_pages()
        self._url = page.url
        self._acted = 0.0

    def acted(self) -> None:
        """The driver's call has returned; the linger is counted from here."""
        self._acted = time.monotonic()

    async def fold(self, reply: dict, *, caused_by: str, declare: str = "") -> str:
        """Say in ``reply`` what the action set off; returns the status to audit the action under.

        ``blocked_by_policy`` when the reply became that, ``ok`` otherwise. ``caused_by`` and
        ``declare`` word the hint (see ``refused_envelope``).
        """
        ctx = self._ctx
        await self._linger()
        tabs = ctx.session.open_pages()
        if any(tab not in self._tabs for tab in tabs):
            reply["new_tab"] = True
            reply["tab_count"] = len(tabs)
            _add_hint(reply, _NEW_TAB_HINT)
        guard = ctx.guard
        if guard is None or guard.refusals <= self._refusals:
            return "ok"
        if reply.get("status") not in _REPLACEABLE or "download" in reply:
            return "ok"
        # A refused redirect hop may still be being cancelled: the tab is read once it is
        # where it will stay.
        hop = await guard.refused_redirect_since(self._refusals)
        earlier = reply.get("hint")
        reply.update(
            refused_envelope(
                self._page.url, redirected_to=hop, caused_by=caused_by, declare=declare
            )
        )
        if earlier:
            _add_hint(reply, earlier)
        return "blocked_by_policy"

    async def _linger(self) -> None:
        """Wait out a verdict or a tab that is still on its way, while nothing has shown."""
        deadline = self._acted + min(self._ctx.config.download_settle_s, self._linger_s)
        while (left := deadline - time.monotonic()) > 0 and self._quiet():
            await asyncio.sleep(min(_POLL_S, left))

    def _quiet(self) -> bool:
        """Nothing refused, no tab opened, the URL where it was: the action has shown no effect."""
        guard = self._ctx.guard
        return (
            (guard is None or guard.refusals <= self._refusals)
            and self._page.url == self._url
            and not any(tab not in self._tabs for tab in self._ctx.session.open_pages())
        )


def _add_hint(reply: dict, text: str) -> None:
    """Add to whatever the tool already said, without overwriting it."""
    reply["hint"] = f"{reply['hint']} {text}" if reply.get("hint") else text
