#!/usr/bin/env bash
# Run the installed lyra plugin on this checkout's code.
#
#   dev-install.sh [--data DIR] [--installed-root DIR]           install
#   dev-install.sh [--data DIR] [--installed-root DIR] --verify  check what is installed
#
# The installed plugin keeps its Python environment in its data directory
# (default ~/.claude/plugins/data/lyra-browser-lyra-browser/venv); Claude Code's launcher
# and any other client pointed at that venv's `lyra-browser` (Hermes, say) run it. This
# installs the checkout this script lives in into that venv, then writes the stamp
# bootstrap.sh expects for the *installed* plugin root, so the next launch keeps it instead
# of rebuilding from the installed version. Only the Python package changes: hooks, skills,
# the launcher and plugin.json stay those of the installed version. A real plugin update
# (a new version, a new install path) rebuilds the venv from that version as usual.
#
# --verify exits 0 only when the installed package equals this checkout's src/lyra_browser,
# bootstrap.sh would not rebuild, `lyra-browser doctor --json` says ok, and a headless smoke
# through the installed package (open, navigate, read, screenshot, close) passes.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKOUT="$(cd "$HERE/../.." && pwd)"
DATA="${HOME}/.claude/plugins/data/lyra-browser-lyra-browser"
INSTALLED_ROOT=""
VERIFY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data) DATA="$2"; shift 2 ;;
    --installed-root) INSTALLED_ROOT="$2"; shift 2 ;;
    --verify) VERIFY=1; shift ;;
    -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "dev-install: unknown argument $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$INSTALLED_ROOT" ]]; then
  REGISTRY="${HOME}/.claude/plugins/installed_plugins.json"
  if [[ ! -f "$REGISTRY" ]]; then
    echo "dev-install: $REGISTRY not found; install the plugin first or pass --installed-root" >&2
    exit 1
  fi
  INSTALLED_ROOT="$(python3 - "$REGISTRY" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
entries = data.get("plugins", data).get("lyra-browser@lyra-browser") or []
user = [e for e in entries if e.get("scope") == "user"] or entries
print(user[0]["installPath"] if user else "")
PY
)"
  if [[ -z "$INSTALLED_ROOT" ]]; then
    echo "dev-install: lyra-browser@lyra-browser is not installed; pass --installed-root" >&2
    exit 1
  fi
fi
INSTALLED_ROOT="$(cd "$INSTALLED_ROOT" && pwd)"
VENV="$DATA/venv"

# shellcheck source=stamp.sh
. "$HERE/stamp.sh"
WANT="$(want_stamp "$INSTALLED_ROOT")"

if [[ "$VERIFY" == 0 ]]; then
  UV="$(command -v uv || true)"
  [[ -z "$UV" && -x "$HOME/.local/bin/uv" ]] && UV="$HOME/.local/bin/uv"
  if [[ -z "$UV" ]]; then
    echo "dev-install: uv is required (https://docs.astral.sh/uv/)" >&2
    exit 1
  fi
  mkdir -p "$DATA"
  echo "dev-install: installing $CHECKOUT into $VENV" >&2
  UV_PROJECT_ENVIRONMENT="$VENV" "$UV" sync --frozen --no-dev --no-editable \
    --reinstall-package lyra-browser --project "$CHECKOUT" >&2
  echo "$WANT" >"$DATA/venv.stamp"
  echo "dev-install: done; stamped for $INSTALLED_ROOT" >&2
  exit 0
fi

fail() { echo "dev-install --verify: FAIL $*" >&2; exit 1; }
[[ -x "$VENV/bin/python" ]] || fail "no environment at $VENV"
SITE="$("$VENV/bin/python" -I -c 'import lyra_browser, os; print(os.path.dirname(lyra_browser.__file__))')"
if ! diff -rq -x __pycache__ "$CHECKOUT/src/lyra_browser" "$SITE" >&2; then
  fail "installed package $SITE differs from $CHECKOUT/src/lyra_browser"
fi
echo "PASS  installed package equals the checkout" >&2
[[ -f "$DATA/venv.stamp" && "$(cat "$DATA/venv.stamp")" == "$WANT" ]] \
  || fail "stamp does not match $INSTALLED_ROOT: the next launch would rebuild"
echo "PASS  bootstrap would keep this install" >&2
"$VENV/bin/lyra-browser" doctor --json >"$DATA/dev-install-doctor.json" 2>/dev/null || true
"$VENV/bin/python" -I -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get("ok") is True else 1)' \
  "$DATA/dev-install-doctor.json" || fail "doctor is not ok: $DATA/dev-install-doctor.json"
echo "PASS  doctor ok" >&2
"$VENV/bin/python" -I "$HERE/dev_smoke.py" || fail "headless smoke"
echo "PASS  headless smoke through the installed package" >&2
