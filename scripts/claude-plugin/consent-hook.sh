#!/usr/bin/env bash
# PreToolUse hook for lyra's tools (see hooks/hooks.json and consent_hook.py).
#
# Runs consent_hook.py with the plugin's own Python, the one the server runs on,
# isolated (-I) so nothing in the working directory is imported. Without a
# Python it fails closed for the one thing it guards: a call that carries
# consent_override is blocked (exit 2), and every other call goes ahead as it
# was, which means lyra asks as it always did.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${CLAUDE_PLUGIN_DATA:-}/venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3 || true)"
fi
if [[ -z "$PY" ]]; then
  if grep -q consent_override; then
    echo "lyra-browser: consent_override is set only by the lyra plugin" >&2
    exit 2
  fi
  exit 0
fi
exec "$PY" -I "$HERE/consent_hook.py"
