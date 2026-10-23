"""What a page reports to observers (``uat/observe.py``), and Playwright's URL globs.

``request`` / ``response`` / ``requestfailed`` come from the ``Network`` domain every page
already has enabled. ``console`` / ``pageerror`` need ``Runtime.enable``, the one call this
driver otherwise never sends (Kasada refuses on it), so a page turns it on only when
something subscribes to one of those two events: the UAT recorder does, an ordinary
session never does.
"""

from __future__ import annotations

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
OBSERVER_EVENTS = frozenset({"request", "response", "requestfailed"})
RUNTIME_EVENTS = frozenset({"console", "pageerror"})


class Request:
    def __init__(self, params: dict) -> None:
        request = params.get("request") or {}
        self.url: str = request.get("url", "") + request.get("urlFragment", "")
        self.method: str = request.get("method", "GET")
        self.resource_type: str = _RESOURCE_TYPES.get(params.get("type", ""), "other")
        self.post_data: str | None = request.get("postData")
        self.headers: dict[str, str] = {
            str(k).lower(): str(v) for k, v in (request.get("headers") or {}).items()
        }
        self.failure: str | None = None


class ObservedResponse:
    def __init__(self, params: dict, request: Request | None) -> None:
        response = params.get("response") or {}
        self.request = request
        self.url: str = response.get("url", "")
        self.status: int = int(response.get("status") or 0)


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
