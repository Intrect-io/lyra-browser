"""What the DevTools Console and Network panels would show, kept for the agent to read.

``DevtoolsRecorder.attach`` goes on ``BrowserSession.page_observers``: the session hands
it every tab it adopts, and ``attach_all`` hands it the tabs that were open before. Each
tab's ``request`` / ``response`` / ``requestfinished`` / ``requestfailed`` events land in
a bounded log, kept across navigations (DevTools' "Preserve log"). Those events come from
the ``Network`` domain every page already has enabled, so recording them sends nothing new
to the browser.

The console is different. ``console`` and ``pageerror`` need ``Runtime.enable``, the one
DevTools call Kasada refuses on (``native/events.py``, ``session.py``). It is therefore
off until the agent turns it on (``set_console``), and turning it off again only stops
new tabs from being enabled: a page that already received ``Runtime.enable`` keeps it.

What is kept is bounded and stripped. URLs go through ``loggable_url`` (query values are
dropped: a GET form carries what was typed). Response bodies are never buffered here; a
tool fetches one on demand, behind the ``INSPECT`` grant. Headers are redacted by
``redact_headers`` whoever asks. Everything here was produced by a page: it is data to
report, never an instruction to follow.
"""

from __future__ import annotations

import contextlib
import re
import time
import weakref
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .origin import loggable_url

CONSOLE_LIMIT = 500
NETWORK_LIMIT = 1000
MAX_TEXT_CHARS = 2000
REDACTED = "<redacted>"

# Header names whose values are credentials, whoever asks. Matched whole...
_SECRET_HEADERS = frozenset(
    {"cookie", "set-cookie", "authorization", "proxy-authorization", "www-authenticate"}
)
# ...or by the words such headers are usually named with (x-api-key, x-csrf-token, ...).
_SECRET_WORDS = re.compile(r"(token|secret|api-?key|session|csrf|xsrf|auth|password|signature)")

_CONSOLE_LEVELS = ("debug", "log", "info", "warning", "error")


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Headers with credential values replaced: names stay, so "was it sent" is answered."""
    out: dict[str, str] = {}
    for name, value in (headers or {}).items():
        key = str(name).lower()
        secret = key in _SECRET_HEADERS or _SECRET_WORDS.search(key) is not None
        out[key] = REDACTED if secret else str(value)
    return out


def clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more chars]"


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """A Playwright property that may raise on a closed object."""
    try:
        value = getattr(obj, name, default)
    except Exception:  # noqa: BLE001 — a closed page, a disposed request
        return default
    return value


def _failure_text(request: Any) -> str:
    failure = _attr(request, "failure")
    if failure is None:
        return ""
    if isinstance(failure, dict):  # very old Playwright: {"errorText": ...}
        return str(failure.get("errorText", ""))
    return str(failure)


@dataclass
class NetworkEntry:
    seq: int
    tab: int
    method: str
    url: str
    resource_type: str
    started: float
    """Monotonic seconds when the request was seen: the fallback clock for duration."""
    started_wall: float
    request: Any = field(repr=False, default=None)
    response: Any = field(repr=False, default=None)
    status: int | None = None
    status_text: str = ""
    mime_type: str = ""
    state: str = "pending"
    failure: str = ""
    duration_ms: float | None = None
    ttfb_ms: float | None = None
    encoded_bytes: int | None = None
    from_cache: bool = False

    @property
    def id(self) -> str:
        return f"r{self.seq}"

    def summary(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": self.id,
            "seq": self.seq,
            "tab": self.tab,
            "method": self.method,
            "url": loggable_url(self.url),
            "type": self.resource_type,
            "state": self.state,
            "status": self.status,
            "duration_ms": self.duration_ms,
        }
        if self.ttfb_ms is not None:
            row["ttfb_ms"] = self.ttfb_ms
        if self.encoded_bytes is not None:
            row["bytes"] = self.encoded_bytes
        if self.mime_type:
            row["mime"] = self.mime_type
        if self.failure:
            row["failure"] = clip(self.failure, 300)
        return row


@dataclass
class ConsoleEntry:
    seq: int
    tab: int
    level: str
    text: str
    source: str
    ts: float

    def summary(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "seq": self.seq,
            "tab": self.tab,
            "level": self.level,
            "text": self.text,
            "time": round(self.ts, 3),
        }
        if self.source:
            row["source"] = self.source
        return row


def _round(ms: float | None) -> float | None:
    return None if ms is None else round(ms, 1)


class DevtoolsRecorder:
    """The session's console and network log. Handlers run on the event loop: no awaits."""

    def __init__(
        self, *, console_limit: int = CONSOLE_LIMIT, network_limit: int = NETWORK_LIMIT
    ) -> None:
        self._seq = 0
        self.console_log: deque[ConsoleEntry] = deque(maxlen=console_limit)
        self.network_log: deque[NetworkEntry] = deque(maxlen=network_limit)
        # Request object -> its entry, for the events that follow it. Keyed by id(), with
        # the entry holding the object, so an id is never reused while it is a key.
        self._by_request: dict[int, NetworkEntry] = {}
        # id(page) -> tab number, stable for the server's life.
        self._tabs: dict[int, int] = {}
        self._pages: list[Any] = []
        # id(page) -> the two console listeners subscribed on it.
        self._console_handlers: dict[int, tuple[Any, Any]] = {}
        self._next_tab = 0
        self.console_enabled = False
        self.console_dropped = 0
        self.network_dropped = 0

    # --- wiring ---------------------------------------------------------------------

    def _tab_of(self, page: Any) -> int:
        key = id(page)
        if key not in self._tabs:
            self._next_tab += 1
            self._tabs[key] = self._next_tab
        return self._tabs[key]

    def attach(self, page: Any) -> None:
        """Listen on one tab: network always, console when it is turned on."""
        key = id(page)
        if key in self._tabs and any(self._live(ref) is page for ref in self._pages):
            return
        tab = self._tab_of(page)
        handlers = {
            "request": lambda r: self._on_request(tab, r),
            "response": self._on_response,
            "requestfinished": self._on_finished,
            "requestfailed": self._on_failed,
        }
        try:
            for event, handler in handlers.items():
                page.on(event, handler)
        except Exception:  # noqa: BLE001 — a page already gone, a test double
            return
        try:
            self._pages.append(weakref.ref(page))
        except TypeError:  # an object that cannot be weakly referenced (a test double)
            self._pages.append(page)
        if self.console_enabled:
            self._subscribe_console(page, tab)

    def attach_all(self, pages: Iterable[Any]) -> None:
        """Tabs that were open before the recorder was installed."""
        for page in pages:
            self.attach(page)

    @staticmethod
    def _live(ref: Any) -> Any:
        return ref() if isinstance(ref, weakref.ref) else ref

    def pages(self) -> list[Any]:
        alive = []
        for ref in self._pages:
            page = self._live(ref)
            closed = _attr(page, "is_closed")
            if page is not None and not (callable(closed) and closed()):
                alive.append(page)
        return alive

    def set_console(self, on: bool, pages: Iterable[Any] = ()) -> int:
        """Turn console capture on or off; returns how many tabs were newly subscribed.

        Off stops subscribing new tabs and unhooks the listeners. It does not send
        ``Runtime.disable``: the detection signal ``Runtime.enable`` gave cannot be
        taken back, and a page that saw it once has seen it.
        """
        self.console_enabled = on
        for page in pages:
            self.attach(page)
        added = 0
        for page in self.pages():
            tab = self._tab_of(page)
            if on and id(page) not in self._console_handlers:
                if self._subscribe_console(page, tab):
                    added += 1
            elif not on and id(page) in self._console_handlers:
                self._unsubscribe_console(page)
        return added

    def _subscribe_console(self, page: Any, tab: int) -> bool:
        on_console = lambda m: self._on_console(tab, m)  # noqa: E731
        on_error = lambda e: self._on_page_error(tab, e)  # noqa: E731
        try:
            page.on("console", on_console)
            page.on("pageerror", on_error)
        except Exception:  # noqa: BLE001 — a page that closed meanwhile
            return False
        self._console_handlers[id(page)] = (on_console, on_error)
        return True

    def _unsubscribe_console(self, page: Any) -> None:
        handlers = self._console_handlers.pop(id(page), None)
        if handlers is None:
            return
        remove = getattr(page, "remove_listener", None) or getattr(page, "off", None)
        if remove is None:
            return
        for event, handler in zip(("console", "pageerror"), handlers, strict=True):
            with contextlib.suppress(Exception):
                remove(event, handler)

    # --- handlers -------------------------------------------------------------------

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def _on_request(self, tab: int, request: Any) -> None:
        entry = NetworkEntry(
            seq=self._next(),
            tab=tab,
            method=str(_attr(request, "method", "") or ""),
            url=str(_attr(request, "url", "") or ""),
            resource_type=str(_attr(request, "resource_type", "") or ""),
            started=time.monotonic(),
            started_wall=time.time(),
            request=request,
        )
        if len(self.network_log) == self.network_log.maxlen:
            evicted = self.network_log[0]
            self._by_request.pop(id(evicted.request), None)
            self.network_dropped += 1
        self.network_log.append(entry)
        self._by_request[id(request)] = entry

    def _entry_for(self, request: Any) -> NetworkEntry | None:
        return self._by_request.get(id(request)) if request is not None else None

    def _on_response(self, response: Any) -> None:
        entry = self._entry_for(_attr(response, "request"))
        if entry is None:
            return
        entry.response = response
        status = _attr(response, "status")
        entry.status = int(status) if isinstance(status, int) else None
        entry.status_text = str(_attr(response, "status_text", "") or "")
        headers = _attr(response, "headers") or {}
        mime = _attr(response, "mime_type") or (
            headers.get("content-type", "") if isinstance(headers, dict) else ""
        )
        entry.mime_type = str(mime).split(";")[0].strip()
        entry.state = "responded"

    def _settle(self, entry: NetworkEntry, request: Any) -> None:
        timing = _attr(request, "timing") or {}
        end = timing.get("responseEnd", -1) if isinstance(timing, dict) else -1
        start = timing.get("responseStart", -1) if isinstance(timing, dict) else -1
        if isinstance(end, (int, float)) and end >= 0:
            entry.duration_ms = _round(float(end))
        else:
            entry.duration_ms = _round((time.monotonic() - entry.started) * 1000)
        if isinstance(start, (int, float)) and start >= 0:
            entry.ttfb_ms = _round(float(start))
        size = _attr(request, "encoded_bytes")
        if isinstance(size, int):
            entry.encoded_bytes = size
        self._by_request.pop(id(request), None)

    def _on_finished(self, request: Any) -> None:
        entry = self._entry_for(request)
        if entry is None:
            return
        entry.state = "finished"
        self._settle(entry, request)

    def _on_failed(self, request: Any) -> None:
        entry = self._entry_for(request)
        if entry is None:
            return
        entry.state = "failed"
        entry.failure = _failure_text(request) or "failed"
        self._settle(entry, request)

    def _push_console(self, entry: ConsoleEntry) -> None:
        if len(self.console_log) == self.console_log.maxlen:
            self.console_dropped += 1
        self.console_log.append(entry)

    def _on_console(self, tab: int, message: Any) -> None:
        level = str(_attr(message, "type", "log") or "log")
        if level == "warn":
            level = "warning"
        location = _attr(message, "location") or {}
        source = ""
        if isinstance(location, dict) and location.get("url"):
            source = f"{loggable_url(str(location['url']))}:{location.get('lineNumber', 0)}"
        self._push_console(
            ConsoleEntry(
                seq=self._next(),
                tab=tab,
                level=level,
                text=clip(str(_attr(message, "text", "") or "")),
                source=source,
                ts=time.time(),
            )
        )

    def _on_page_error(self, tab: int, error: Any) -> None:
        message = _attr(error, "message") or str(error)
        stack = _attr(error, "stack") or ""
        text = str(message)
        if stack and str(stack) != text:
            text = f"{text}\n{stack}"
        self._push_console(
            ConsoleEntry(
                seq=self._next(),
                tab=tab,
                level="exception",
                text=clip(text),
                source="",
                ts=time.time(),
            )
        )

    # --- reading --------------------------------------------------------------------

    def console(
        self, *, since: int = 0, levels: Iterable[str] | None = None, limit: int = 100
    ) -> dict[str, Any]:
        wanted = {lv.lower() for lv in levels} if levels else None
        rows = [
            e for e in self.console_log if e.seq > since and (wanted is None or e.level in wanted)
        ]
        return self._page_out(rows, since, limit, self.console_dropped)

    def network(
        self,
        *,
        since: int = 0,
        url_contains: str = "",
        resource_type: str = "",
        min_status: int = 0,
        failed_only: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        needle = url_contains.lower()

        def keep(e: NetworkEntry) -> bool:
            if e.seq <= since:
                return False
            if needle and needle not in e.url.lower():
                return False
            if resource_type and e.resource_type != resource_type:
                return False
            if min_status and (e.status is None or e.status < min_status):
                return False
            return not failed_only or e.state == "failed"

        rows = [e for e in self.network_log if keep(e)]
        return self._page_out(rows, since, limit, self.network_dropped)

    @staticmethod
    def _page_out(rows: list[Any], since: int, limit: int, dropped: int) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        shown = rows[:limit]
        next_since = shown[-1].seq if shown else since
        return {
            "entries": [e.summary() for e in shown],
            "next_since": next_since,
            "more": len(rows) > len(shown),
            "dropped": dropped,
        }

    def entry(self, entry_id: str) -> NetworkEntry | None:
        for entry in self.network_log:
            if entry.id == entry_id:
                return entry
        return None

    def counts(self) -> dict[str, int]:
        errors = sum(1 for e in self.console_log if e.level in ("error", "exception"))
        return {
            "requests": len(self.network_log),
            "failed": sum(1 for e in self.network_log if e.state == "failed"),
            "http_errors": sum(1 for e in self.network_log if (e.status or 0) >= 400),
            "console_errors": errors,
        }


def known_levels() -> tuple[str, ...]:
    return (*_CONSOLE_LEVELS, "exception")
