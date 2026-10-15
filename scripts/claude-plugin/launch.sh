#!/usr/bin/env bash
# The command Claude Code runs for the plugin's MCP server (see .mcp.json).
#
# Makes sure the environment exists (bootstrap.sh is idempotent and usually
# returns at once, the SessionStart hook having built it), then replaces itself
# with the server. stdout is the MCP transport from here on: nothing in this
# file may write to it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CLAUDE_PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$HERE/../.." && pwd)}"

if [[ -z "${CLAUDE_PLUGIN_DATA:-}" ]]; then
  echo "lyra-browser: CLAUDE_PLUGIN_DATA is not set; this launcher is for Claude Code" >&2
  exit 2
fi
"$HERE/bootstrap.sh"
exec "$CLAUDE_PLUGIN_DATA/venv/bin/lyra-browser" --client claude-code "$@"
