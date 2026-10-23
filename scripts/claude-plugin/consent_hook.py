"""PreToolUse hook for lyra's tools: carry Claude Code's bypassPermissions mode to the server.

Claude Code tells only hooks which permission mode it is in (``permission_mode`` on
the hook's stdin); the MCP server and the elicitation hook are not told. Run before
every ``mcp__plugin_lyra-browser_lyra__*`` call (hooks/hooks.json), this

- in bypassPermissions mode, rewrites the call's input to carry
  ``consent_override="bypassPermissions"``, so lyra answers its own consent requests
  instead of putting a dialog in front of a person who asked not to be asked;
- in any other mode, denies a call whose input already carries the field: only this
  hook may set it, so the model (or a page talking to it) put it there.

Prints nothing at all for an ordinary call in another mode, which leaves the call
exactly as it was. See src/lyra_browser/bypass.py for the server side.

Standard library only, and no input it is given makes it allow anything: unreadable
input is a deny when it mentions the field and silence otherwise.
"""

from __future__ import annotations

import json
import sys

OVERRIDE_ARG = "consent_override"
BYPASS_MODE = "bypassPermissions"


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


_FORGED = (
    f"{OVERRIDE_ARG} is set only by the lyra plugin, from Claude Code's own permission "
    "mode. Call the tool again without it."
)


def decide(raw: str) -> dict | None:
    """The hook's stdout for ``raw`` stdin, or None to print nothing."""
    try:
        event = json.loads(raw)
        tool_input = event.get("tool_input")
        mode = event.get("permission_mode")
    except (ValueError, AttributeError):
        return _deny(_FORGED) if OVERRIDE_ARG in raw else None
    if not isinstance(tool_input, dict):
        return _deny(_FORGED) if OVERRIDE_ARG in raw else None
    if mode == BYPASS_MODE:
        # Whole-object replace (that is what updatedInput does), so every argument
        # the model chose is carried over unchanged.
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "lyra: Claude Code is in bypassPermissions mode",
                "updatedInput": {**tool_input, OVERRIDE_ARG: BYPASS_MODE},
            }
        }
    if OVERRIDE_ARG in tool_input:
        return _deny(_FORGED)
    return None


def main() -> int:
    out = decide(sys.stdin.read())
    if out is not None:
        sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
