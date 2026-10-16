#!/usr/bin/env bash
# The dependency pins the npx package ships (npm/constraints.txt), exported from uv.lock.
#
#   scripts/npm-constraints.sh > npm/constraints.txt     regenerate after `uv lock`
#   scripts/npm-constraints.sh | diff - npm/constraints.txt     what CI runs
#
# The flags live here, once, so the file and the check cannot disagree. Markers
# stay in (`; python_full_version < '3.12'`), so one file serves every platform.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv export --frozen --no-dev --no-emit-project --no-hashes --no-header --no-annotate
