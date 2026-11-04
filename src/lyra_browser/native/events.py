"""What a page reports to observers (``uat/observe.py``), and Playwright's URL globs.

``request`` / ``response`` / ``requestfinished`` / ``requestfailed`` come from the
``Network`` domain every page already has enabled. Each object carries Playwright's
``timing`` shape (milliseconds; ``startTime`` is wall-clock, the rest are relative to it,
``-1`` until known), computed from the browser's own timestamps rather than from when the
event reached Python. ``console`` / ``pageerror`` need ``Runtime.enable``, the one call this
driver otherwise never sends (Kasada refuses on it), so a page turns it on only when
something subscribes to one of those two events: the UAT recorder does, an ordinary
session never does.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import Any

# Network.ResourceType -> Playwright's resource_type.
_RESOURCE_TYPES = {
    "Document": "document",
    "Stylesheet": "stylesheet",
    "Image": "image",
    "Media": "media",
    "Font": "font",
    "Script": "script",
    "TextTrack": "texttrack",
    "XHR": "xhr",
    "Fetch": "fetch",
    "EventSource": "eventsource",
    "WebSocket": "websocket",
    "Manifest": "manifest",
    "Ping": "ping",
    "Prefetch": "prefetch",
    "Preflight": "preflight",
}
OBSERVER_EVENTS = frozenset({"request", "response", "requestfinished", "requestfailed"})
RUNTIME_EVENTS = frozenset({"console", "pageerror"})


class Request:
    def __init__(self, params: dict, page: Any = None) -> None:
        request = params.get("request") or {}
        self.url: str = request.get("url", "") + request.get("urlFragment", "")
        self.method: str = request.get("method", "GET")
        self.resource_type: str = _RESOURCE_TYPES.get(params.get("type", ""), "other")
        self.post_data: str | None = request.get("postData")
        self.headers: dict[str, str] = {
            str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()
        }
        self.failure: str | None = None
        self.request_id: str = params.get("requestId", "")
        # Monotonic seconds, the clock every later Network event is stamped with.
        self._started: float = float(params.get("timestamp") or 0.0)
        wall = params.get("wallTime")
        self.timing: dict[str, float] = {
            "startTime": float(wall) * 1000 if wall else -1,
            "responseStart": -1,
            "responseEnd": -1,
        }
        self.encoded_bytes: int | None = None
        self._response: ObservedResponse | None = None
        self._page = page

    def _since_start_ms(self, timestamp: float) -> float:
        if not self._started or not timestamp:
            return -1
        return max(0.0, (timestamp - self._started) * 1000)

    def _responded(self, response: ObservedResponse, timing: dict | None) -> None:
        self._response = response
        if timing and timing.get("requestTime"):
            headers_end = float(timing.get("receiveHeadersEnd") or 0.0)
            at = float(timing["requestTime"]) + headers_end / 1000
            self.timing["responseStart"] = self._since_start_ms(at)

    def _finished(self, params: dict) -> None:
        self.timing["responseEnd"] = self._since_start_ms(float(params.get("timestamp") or 0.0))
        length = params.get("encodedDataLength")
        if isinstance(length, (int, float)):
            self.encoded_bytes = int(length)

    async def all_headers(self) -> dict[str, str]:
        return dict(self.headers)

    async def response(self) -> ObservedResponse | None:
        return self._response

    async def sizes(self) -> dict[str, int]:
        return {"responseBodySize": self.encoded_bytes or 0}


class ObservedResponse:
    def __init__(self, params: dict, request: Request | None, page: Any = None) -> None:
        response = params.get("response") or {}
        self.request = request
        self.url: str = response.get("url", "")
        self.status: int = int(response.get("status") or 0)
        self.status_text: str = response.get("statusText", "")
        self.headers: dict[str, str] = {
            str(k).lower(): str(v) for k, v in (response.get("headers") or {}).items()
        }
        self.mime_type: str = response.get("mimeType", "")
        self.request_id: str = params.get("requestId", "")
        self._page = page
        if request is not None:
            request._responded(self, response.get("timing"))

    async def all_headers(self) -> dict[str, str]:
        return dict(self.headers)

    async def body(self) -> bytes:
        """``Network.getResponseBody``: what Chrome still holds for this request."""
        if self._page is None:
            raise RuntimeError("response body is not available")
        reply = await self._page._send("Network.getResponseBody", {"requestId": self.request_id})
        data = reply.get("body") or ""
        if reply.get("base64Encoded"):
            return base64.b64decode(data)
        return data.encode("utf-8")


class ConsoleMessage:
    def __init__(self, params: dict) -> None:
        kind = params.get("type", "log")
        self.type: str = "warning" if kind == "warning" else kind
        self.text: str = " ".join(_render(arg) for arg in params.get("args") or ())
        frames = (params.get("stackTrace") or {}).get("callFrames") or []
        top = frames[0] if frames else {}
        self.location: dict = {
            "url": top.get("url", ""),
            "lineNumber": top.get("lineNumber", 0),
            "columnNumber": top.get("columnNumber", 0),
        }


class PageError:
    def __init__(self, params: dict) -> None:
        details = params.get("exceptionDetails") or {}
        exception = details.get("exception") or {}
        self.message: str = exception.get("description") or details.get("text") or ""

    def __str__(self) -> str:
        return self.message


def _render(arg: dict) -> str:
    if "value" in arg:
        value = arg["value"]
        return value if isinstance(value, str) else str(value)
    return str(arg.get("unserializableValue") or arg.get("description") or arg.get("type", ""))


def glob_to_regex(glob: str) -> re.Pattern:
    """Playwright's URL glob: ``*`` stops at ``/``, ``**`` does not, ``{a,b}`` alternates,
    ``?`` is a literal question mark, ``\\`` escapes."""
    out = ["^"]
    in_group = False
    i = 0
    while i < len(glob):
        ch = glob[i]
        if ch == "\\" and i + 1 < len(glob):
            out.append(re.escape(glob[i + 1]))
            i += 2
            continue
        if ch == "*":
            if glob[i + 1 : i + 2] == "*":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "{":
            in_group = True
            out.append("(?:")
        elif ch == "}" and in_group:
            in_group = False
            out.append(")")
        elif ch == "," and in_group:
            out.append("|")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def url_matcher(url: Any) -> Callable[[str], bool]:
    """``wait_for_url``'s argument as a test: a glob, a compiled regex, or a predicate."""
    if callable(url):
        return lambda value: bool(url(value))
    if isinstance(url, re.Pattern):
        return lambda value: url.search(value) is not None
    pattern = glob_to_regex(str(url))
    return lambda value: pattern.match(value) is not None
