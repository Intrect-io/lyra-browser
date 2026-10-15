#!/usr/bin/env bash
# What the lyra-browser plugin would find on this machine, for the doctor skill.
# Prints to stdout on purpose: a person (or Claude, through the Bash tool) reads it.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$HERE/../.." && pwd)}"
DATA="${1:-${CLAUDE_PLUGIN_DATA:-}}"
echo "plugin root:  $ROOT"
echo "plugin data:  ${DATA:-(CLAUDE_PLUGIN_DATA unset)}"
if [[ -n "$DATA" ]]; then
  if [[ -x "$DATA/venv/bin/lyra-browser" ]]; then
    echo "venv:         ready ($("$DATA/venv/bin/python" --version 2>&1))"
    [[ -f "$DATA/venv.stamp" ]] && echo "stamp:        $(cut -c1-12 "$DATA/venv.stamp")"
    "$DATA/venv/bin/python" - <<'PY'
import importlib.metadata as m
for name in ("lyra-browser", "fastmcp", "playwright", "patchright"):
    try:
        print(f"{name + ':':14}{m.version(name)}")
    except m.PackageNotFoundError:
        print(f"{name + ':':14}not installed")
PY
  else
    echo "venv:         not built yet (bootstrap.sh runs at session start; log: $DATA/bootstrap.log)"
  fi
fi
uv_bin="$(command -v uv || echo "$HOME/.local/bin/uv")"
[[ -x "$uv_bin" ]] && echo "uv:           $("$uv_bin" --version 2>/dev/null)" || echo "uv:           not found (python3 -m venv + pip fallback)"
for b in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge msedge; do
  p="$(command -v "$b" 2>/dev/null || true)"; [[ -n "$p" ]] && echo "browser:      $b -> $p"
done
for app in "/Applications/Google Chrome.app" "/Applications/Microsoft Edge.app"; do
  [[ -d "$app" ]] && echo "browser:      $app"
done
cache="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
[[ -d "$cache" ]] && echo "bundled:      $(ls "$cache" 2>/dev/null | tr '\n' ' ')" || echo "bundled:      none ($cache); the install-browser skill adds one"
case "$(uname -s)" in
  Linux) echo "display:      DISPLAY=${DISPLAY:-} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-} (none means headless, nobody can watch)";;
  *)     echo "display:      $(uname -s) opens windows without a display variable";;
esac
data_dir="${LYRA_BROWSER_DATA_DIR:-$HOME/.lyra-browser}"
echo "profile:      $data_dir/profile$([[ -d "$data_dir/profile" ]] || echo ' (not created yet)')"
if [[ -f "$data_dir/profile.lock" ]] && command -v flock >/dev/null; then
  flock -n "$data_dir/profile.lock" true 2>/dev/null && echo "profile lock: free" || echo "profile lock: held by another lyra-browser (a second session gets an instance profile without logins)"
fi
