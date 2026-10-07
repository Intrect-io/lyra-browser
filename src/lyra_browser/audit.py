"""Append-only audit trail.

Every tool invocation is recorded as one JSON line so a user can review exactly
what the agent did in their browser. This is a hard requirement for a tool that
acts inside a session sharing the user's logins.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path, *, mirror_stderr: bool = False) -> None:
        self._path = path
        # Each record is also written, unprefixed, as one JSON line on stderr —
        # for hosts whose disk is ephemeral but whose stderr is collected.
        self._mirror_stderr = mirror_stderr

    def record(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        status: str = "ok",
        detail: str | None = None,
        origin: str | None = None,
        initiator: str | None = None,
    ) -> None:
        """Append a single audit entry. Best-effort: never raises into a tool call.

        ``origin`` is where the action points and ``initiator`` is where it came
        from. Reconstructing an incident needs both: a request to a bank is
        ordinary from the bank's own page and alarming from anywhere else.
        """
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "tool": tool,
            "args": _redact(args or {}),
            "status": status,
            "detail": detail,
        }
        # Only present when known, so existing entries keep their shape.
        if origin is not None:
            entry["origin"] = origin
        if initiator is not None:
            entry["initiator"] = initiator
        line = json.dumps(entry, ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            # Auditing must not break the agent loop; surfacing happens elsewhere.
            pass
        if self._mirror_stderr:
            try:
                print(line, file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass


# Argument keys whose values should never hit the on-disk log verbatim.
# ``post_data`` is here because the enforcement layer sees raw form bodies, and
# that is exactly where a submitted password lives.
_SENSITIVE_KEYS = {"password", "secret", "token", "value", "post_data"}


def _redact(args: dict[str, Any]) -> dict[str, Any]:
    """Redact values that may carry credentials typed into form fields."""
    out: dict[str, Any] = {}
    for key, val in args.items():
        if key.lower() in _SENSITIVE_KEYS and isinstance(val, str):
            out[key] = f"<redacted:{len(val)} chars>"
        else:
            out[key] = val
    return out
