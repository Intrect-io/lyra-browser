#!/usr/bin/env bash
# Build (or confirm) the lyra-browser plugin's Python environment.
#
# Claude Code installs Node dependencies for a plugin but not Python ones; the
# documented pattern is to install them from a hook into ${CLAUDE_PLUGIN_DATA},
# the one directory that survives plugin updates. This script is that hook,
# and launch.sh runs it again before starting the MCP server, so whichever of
# the two runs first does the work and the other finds the stamp.
#
# Contract:
#   - Never writes to stdout. launch.sh shares stdout with the MCP transport,
#     and a SessionStart hook's stdout goes into the model's context.
#   - Everything it does is logged to $CLAUDE_PLUGIN_DATA/bootstrap.log and
#     summarised on stderr.
#   - Never runs `playwright install`: that takes minutes, and the server
#     drives a system Chrome or Edge when one exists. The install-browser skill
#     does it on request.
#   - Serialised by a lock directory: two sessions starting at once build
#     once; the second waits, then finds the stamp.
set -euo pipefail

if [[ -z "${CLAUDE_PLUGIN_DATA:-}" ]]; then
  echo "lyra-browser: CLAUDE_PLUGIN_DATA is not set; run this through Claude Code" >&2
  exit 2
fi
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DATA="$CLAUDE_PLUGIN_DATA"
VENV="$DATA/venv"
LOG="$DATA/bootstrap.log"
STAMP="$DATA/venv.stamp"
LOCK="$DATA/bootstrap.lock"
mkdir -p "$DATA"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$LOG"; echo "lyra-browser: $*" >&2; }

# What the environment was built from. Rebuilt when the lock, the project
# metadata or the plugin's install path changes (the path changes with every
# plugin version, and the venv was installed from that path).
# sha256sum is GNU coreutils; macOS ships shasum instead (and no sha256sum unless
# coreutils is installed). Both print the same digest.
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum
  else
    shasum -a 256
  fi
}

want_stamp() {
  {
    cat "$ROOT/pyproject.toml"
    [[ -f "$ROOT/uv.lock" ]] && cat "$ROOT/uv.lock"
    echo "root=$ROOT"
  } | sha256 | cut -d' ' -f1
}

WANT="$(want_stamp)"
if [[ -f "$STAMP" && "$(cat "$STAMP")" == "$WANT" && -x "$VENV/bin/lyra-browser" ]]; then
  exit 0
fi

# Serialise. A lock left by a process that died is reclaimed once its pid is gone.
waited=0
until mkdir "$LOCK" 2>/dev/null; do
  holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [[ -n "$holder" ]] && ! kill -0 "$holder" 2>/dev/null; then
    rm -rf "$LOCK"
    continue
  fi
  if (( waited >= 900 )); then
    log "gave up waiting for another bootstrap (lock: $LOCK)"
    exit 1
  fi
  sleep 1
  waited=$((waited + 1))
done
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# Another process may have finished while we waited.
if [[ -f "$STAMP" && "$(cat "$STAMP")" == "$WANT" && -x "$VENV/bin/lyra-browser" ]]; then
  exit 0
fi

log "building the Python environment in $VENV (first run or plugin update; log: $LOG)"
started=$(date +%s)

UV="$(command -v uv || true)"
[[ -z "$UV" && -x "$HOME/.local/bin/uv" ]] && UV="$HOME/.local/bin/uv"

if [[ -n "$UV" && -f "$ROOT/uv.lock" ]]; then
  log "using uv ($("$UV" --version 2>/dev/null))"
  # --reinstall-package: uv caches the built project wheel by name and version,
  # so a changed tree at the same version would otherwise reinstall stale code.
  if ! UV_PROJECT_ENVIRONMENT="$VENV" "$UV" sync --frozen --no-dev --no-editable \
      --reinstall-package lyra-browser --project "$ROOT" >>"$LOG" 2>&1; then
    log "uv sync failed; last lines of $LOG:"
    tail -n 20 "$LOG" >&2
    exit 1
  fi
else
  PY="$(command -v python3 || true)"
  if [[ -z "$PY" ]]; then
    log "neither uv nor python3 found; install uv (https://docs.astral.sh/uv/) or Python 3.11+"
    exit 1
  fi
  if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    log "$PY is older than 3.11; install uv or a newer Python"
    exit 1
  fi
  log "uv not found; using $PY -m venv + pip"
  if ! { "$PY" -m venv --clear "$VENV" && "$VENV/bin/pip" install --quiet "$ROOT"; } >>"$LOG" 2>&1; then
    log "pip install failed; last lines of $LOG:"
    tail -n 20 "$LOG" >&2
    exit 1
  fi
fi

if [[ ! -x "$VENV/bin/lyra-browser" ]]; then
  log "environment built but $VENV/bin/lyra-browser is missing"
  exit 1
fi
echo "$WANT" >"$STAMP"
log "ready in $(( $(date +%s) - started ))s"
