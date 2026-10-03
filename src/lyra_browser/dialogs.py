"""Native dialogs: every one is answered the moment a page raises it.

``alert()``, ``confirm()``, ``prompt()`` and ``beforeunload`` stop the page until
somebody answers. Playwright answers for a page only while nothing listens; once a
listener exists it owns every dialog, and one it leaves open freezes the tab —
including the ``evaluate`` that would have read the page afterwards. So there is
exactly one listener, on the browser context, and it always answers.

On the context, not on each page: a popup can raise a dialog from its first script,
before the browser has told us the tab exists, and a listener added to the tab
afterwards is too late for it. Measured on Chrome, headless and headful: every such
dialog was answered by the driver's own default and never reached the tab's listener.
The context's listener is there before any tab is.

Unless the agent asked for something else (``arm``) the answer is the one a
browser gives when nothing listens, so a page sees nothing it did not see before:
alerts and ``beforeunload`` are accepted, ``confirm`` is dismissed (``false``) and
``prompt`` is dismissed (``null``). What was raised, and how it was answered, is
kept — bounded — until a tool reply picks it up.

An armed answer is a one-shot grant, not a standing instruction. It belongs to the
next tool call that drives the browser (``begin_call``, counted where a tool claims
it) and to the dialogs that call raises while it acts (``acting``, the span
``scope.released`` already wraps every action in). It ends with that action —
answered or not, and even when the call was refused before it started — so the
``Delete this?`` confirm of a page the agent reached later is never answered by a
"yes" meant for an earlier one. A dialog outside that span, such as a page's own
timer or one raised after the call returned, gets the default.

Nothing here judges an answer. Accepting a ``confirm`` that goes on to post a form
is still stopped where the request leaves (``enforcement.py``), exactly as if the
click alone had sent it.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing Playwright at module import time
    from playwright.async_api import Page

# What a report carries of a dialog's text. The page wrote it, and a page can write
# a great deal.
MESSAGE_CHARS = 500
# Dialogs kept until a reply picks them up. A page in an ``alert`` loop must not
# grow this without bound; the oldest go first.
BUFFER_SIZE = 50
# The most an armed answer waits for the call it is for to start acting: long enough for
# the click to be asked about and, if it has to be approved, approved. Binding the answer
# to that one call is what keeps it from answering a later page; this only bounds a call
# that is slow to come.
ARM_TTL_S = 60.0

# The dialogs a browser with no listener accepts. An alert has nothing to answer,
# and ``beforeunload`` dismissed would cancel the navigation that raised it.
# Everything else is dismissed: ``confirm()`` is false, ``prompt()`` is null.
_ACCEPTED_BY_DEFAULT = frozenset({"alert", "beforeunload"})


@dataclass(frozen=True, slots=True)
class _Armed:
    accept: bool
    text: str
    expires_at: float
    call: int  # the tool call that armed it; the answer is for the call after that one


class _Window:
    """One ``watch``: the page it looks at and what that page said meanwhile."""

    __slots__ = ("messages", "page")

    def __init__(self, page: Page) -> None:
        self.page = page
        self.messages: list[str] = []


class DialogHandler:
    """Answers, records and reports the native dialogs of a browser context."""

    def __init__(
        self,
        *,
        size: int = BUFFER_SIZE,
        ttl_s: float = ARM_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._raised: deque[dict] = deque(maxlen=size)
        self._ttl_s = ttl_s
        self._clock = clock
        self._armed: _Armed | None = None
        self._windows: list[_Window] = []
        # Tool calls that claimed the browser so far, and the call each open acting
        # span belongs to.
        self._calls = 0
        self._acting: list[int] = []
        # Answers still in flight. The loop holds a task only weakly, so one nobody
        # else references can be collected before the dialog is resolved.
        self._answering: set[asyncio.Task] = set()

    def handle(self, dialog) -> None:
        """The context's dialog listener. Whatever else goes wrong, the dialog is answered."""
        accept, text = False, ""
        try:
            kind = str(dialog.type or "")
            armed = self._take_armed()
            if armed is None:
                accept = kind in _ACCEPTED_BY_DEFAULT
            else:
                accept, text = armed.accept, armed.text
                if accept and not text:
                    # Clicking OK sends what the prompt's box holds. The driver's own
                    # accept() with no text sends an empty string instead (measured in
                    # Chrome), which is not what "accept" means to a person.
                    text = str(dialog.default_value or "")
            self._file(dialog, kind, accept, armed is not None)
        except Exception:  # noqa: BLE001 — a report that cannot be written must not hold the page
            pass
        self._answer(dialog, accept, text)

    def begin_call(self) -> None:
        """A tool call that drives the browser has begun. Reads do not count.

        Called once per call, where a tool claims the browser (``acquire_page``). The
        count is what ties an armed answer to one call: it is for the call numbered
        one past the call that armed it, so any later call — a refused or failed one
        included — finds it already out of reach.
        """
        self._calls += 1

    @contextmanager
    def acting(self) -> Iterator[None]:
        """The span in which the current tool call acts on the page.

        Entered by ``scope.released``, which every acting tool already wraps its
        action in, directly or through ``guard_action``. An armed answer is for the
        dialogs raised inside the span of the call it was armed for, and it ends
        with that span — answered or not. A dialog the page raises while the call is
        still checking its target or waiting for approval, or after the call has
        returned, is answered by default.
        """
        call = self._calls
        self._acting.append(call)
        try:
            yield
        finally:
            self._acting.remove(call)
            if self._armed is not None and call > self._armed.call:
                self._armed = None

    def arm(self, accept: bool, text: str = "") -> float:
        """Answer a dialog raised by the NEXT tool call this way, once.

        The arming call is not that call: the answer waits for the next call that
        drives the browser, applies to the dialogs it raises while it acts, and
        ends when its action does, used or not. ``text`` is what a ``prompt()``
        receives; empty accepts the prompt's own default value. It means nothing
        when dismissing. Arming again replaces an answer nobody used. Returns the
        most seconds the answer can wait for that call.
        """
        self._armed = _Armed(
            accept, text if accept else "", self._clock() + self._ttl_s, self._calls
        )
        return self._ttl_s

    def drain(self) -> list[dict]:
        """Every dialog raised since the last drain, oldest first — and forget them.

        Each is ``{type, message, default_value, handled, armed}``: what the page
        asked, and how it was answered (``accepted``/``dismissed``, by an armed
        answer or by default). The answer typed into a prompt is never kept.
        """
        raised = list(self._raised)
        self._raised.clear()
        return raised

    @contextmanager
    def watch(self, page: Page) -> Iterator[list[str]]:
        """Collect what ``page`` says in dialogs while the block runs.

        For a tool that sends something and reads the page's own verdict back: a
        refused form reports itself in an ``alert()``. The messages, whole and
        trimmed, land in the yielded list — live, so it can be read after a wait —
        and those dialogs are not also left for the next reply, which would report
        the same refusal twice. Dialogs of other pages are not touched.
        """
        window = _Window(page)
        self._windows.append(window)
        try:
            yield window.messages
        finally:
            self._windows.remove(window)

    def reset(self) -> None:
        """Forget every dialog and any armed answer: the window they belonged to is gone.

        An answer armed for one session's browser must not be waiting for the next
        one's, any more than a grant may.
        """
        self._raised.clear()
        self._armed = None

    def _take_armed(self) -> _Armed | None:
        """The armed answer, if this dialog is one it was armed for. Then it is spent.

        A dialog raised outside the acting span of the call after the arming one
        leaves the answer waiting for it: that dialog was not that call's doing.
        """
        armed = self._armed
        if armed is None:
            return None
        if self._clock() >= armed.expires_at:
            self._armed = None
            return None
        if armed.call + 1 not in self._acting:
            return None
        self._armed = None
        return armed

    def _file(self, dialog, kind: str, accept: bool, armed: bool) -> None:
        message = str(dialog.message or "").strip()
        page = getattr(dialog, "page", None)
        watching = [window for window in self._windows if window.page is page]
        if watching:
            for window in watching:
                window.messages.append(message)
            return
        self._raised.append(
            {
                "type": kind,
                "message": message[:MESSAGE_CHARS],
                "default_value": str(dialog.default_value or "")[:MESSAGE_CHARS],
                "handled": "accepted" if accept else "dismissed",
                "armed": armed,
            }
        )

    def _answer(self, dialog, accept: bool, text: str) -> None:
        try:
            if not accept:
                settled = dialog.dismiss()
            elif text:
                settled = dialog.accept(text)
            else:
                settled = dialog.accept()
        except Exception:  # noqa: BLE001 — answered by someone else first, or the page is gone
            return
        # The driver's answer is a coroutine; a fake's is already done.
        if inspect.isawaitable(settled):
            self._settle(settled)

    def _settle(self, settled: Awaitable) -> None:
        """Run the driver's answer to completion without ever raising into its loop."""

        async def finish() -> None:
            try:
                await settled
            except Exception:  # noqa: BLE001 — answered by someone else first, or the page closed
                pass

        try:
            task = asyncio.get_running_loop().create_task(finish())
        except RuntimeError:  # no loop to run it on: nothing can await it
            close = getattr(settled, "close", None)
            if close is not None:
                close()
            return
        self._answering.add(task)
        task.add_done_callback(self._answering.discard)
