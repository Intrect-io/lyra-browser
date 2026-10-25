#!/usr/bin/env bash
# The dependency pins the npx package ships (npm/constraints.txt), exported from uv.lock.
#
#   scripts/npm-constraints.sh > npm/constraints.txt     regenerate after `uv lock`
#   scripts/npm-constraints.sh | diff - npm/constraints.txt     what CI runs
#
# The flags live here, once, so the file and the check cannot disagree. Markers
# stay in (`; python_full_version < '3.12'`), so one file serves every platform.
#
# --extra playwright: Playwright is not a dependency of the server, but the
# launcher's `install-browser` installs it, and it must be the release this lock
# was tested with (another one fetches a different Chromium revision). A constraint
# never installs anything by itself, so the server's own start is unchanged.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv export --frozen --no-dev --extra playwright --no-emit-project --no-hashes --no-header --no-annotate
