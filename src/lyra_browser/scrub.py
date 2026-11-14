"""Keep known secret values out of what a run writes and what a model reads.

The audit already redacts the arguments that carry typed values. A secret can still come
back another way: a page that echoes a field, a captured POST body, a tool result that
quotes the form. A ``Scrubber`` holds the values that must never appear (a password, a
TOTP seed) and replaces them, as they are and as a form or URL encodes them, wherever
text is written or returned. Values shorter than four characters are not held: replacing
them would mangle ordinary text and they protect nothing.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, quote_plus

MARK = "<secret>"
MIN_LEN = 4


class Scrubber:
    def __init__(self, secrets: list[str] | tuple[str, ...] = ()) -> None:
        self._needles: list[str] = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str) -> None:
        if not secret or len(secret) < MIN_LEN:
            return
        for form in {secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]}:
            if form not in self._needles:
                self._needles.append(form)
        self._needles.sort(key=len, reverse=True)

    def __bool__(self) -> bool:
        return bool(self._needles)

    def text(self, value: str) -> str:
        for needle in self._needles:
            if needle in value:
                value = value.replace(needle, MARK)
        return value

    def data(self, value: Any) -> Any:
        """A JSON-shaped value with every string scrubbed."""
        if not self._needles:
            return value
        return json.loads(self.text(json.dumps(value, ensure_ascii=False, default=str)))
