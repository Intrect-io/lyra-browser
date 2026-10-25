"""The npx package (npm/) is a shim over the PyPI release.

It must name the same version as the Python package, pin the dependency set
uv.lock locked, ship nothing but the shim, and carry no address of a person.
The shim's behaviour is tested in npm/test (``node --test``); these tests check
what only the Python side knows: the version and the lock.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NPM = ROOT / "npm"
PACKAGE_JSON = NPM / "package.json"
SHIM = NPM / "bin" / "lyra-browser.js"
CONSTRAINTS = NPM / "constraints.txt"

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9_.\-]+)==(?P<version>[^\s;]+)")


def _package() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _PIN.match(line)
        assert match, f"constraints.txt line is not an exact pin: {line!r}"
        pins[_normalise(match["name"])] = match["version"]
    return pins


def _locked() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {_normalise(p["name"]): p["version"] for p in lock["package"] if "version" in p}


def test_npm_version_moves_with_the_package_version():
    """The shim installs ``lyra-browser==<its own version>``; a skew would ask PyPI for a
    release that does not exist, or run one the npm package was not tested with."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert _package()["version"] == project["project"]["version"] == plugin["version"]


def test_shim_takes_its_version_from_package_json_not_a_second_copy():
    source = SHIM.read_text(encoding="utf-8")
    assert "require('../package.json')" in source
    assert not re.search(r"lyra-browser==\d", source), "the version is spelled out in the shim"


def test_constraints_pin_exactly_what_uv_lock_locked():
    pins, locked = _pins(), _locked()
    assert pins, "constraints.txt is empty"
    for name, version in pins.items():
        assert locked.get(name) == version, (
            f"{name}: constraints {version}, uv.lock {locked.get(name)}"
        )
    assert "lyra-browser" not in pins, "the project itself is the requirement, not a constraint"
    # The direct dependencies of pyproject.toml (extras excluded: the shim installs the
    # base set, and Playwright became an extra when native took over the default). The
    # full set is diffed in CI against `scripts/npm-constraints.sh`.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    direct = [
        re.split(r"[<>=!~\[]", spec, maxsplit=1)[0].strip()
        for spec in project["project"]["dependencies"]
    ]
    assert direct, "pyproject.toml declares no dependencies"
    for name in direct:
        assert _normalise(name) in pins, f"{name} is not pinned"
    # Playwright is pinned for `install-browser` (it installs the [playwright] extra
    # from the same lock), though the server itself does not depend on it.
    assert "playwright" in pins


def test_package_ships_only_the_shim():
    package = _package()
    assert package["name"] == "@intrect/lyra-browser"
    assert package["publishConfig"] == {"access": "public"}, "a scoped package defaults to private"
    assert not package.get("private"), "a private package cannot be published"
    assert sorted(package["files"]) == [
        "LICENSE",
        "README.md",
        "bin/lyra-browser.js",
        "constraints.txt",
    ]
    assert "dependencies" not in package and "optionalDependencies" not in package
    lifecycle = {"preinstall", "install", "postinstall"}
    assert not lifecycle & set(package.get("scripts", {})), "an install script runs on every npx"
    assert package["repository"]["directory"] == "npm"


def test_bin_points_at_an_executable_with_a_node_shebang():
    package = _package()
    assert package["bin"] == {"lyra-browser": "bin/lyra-browser.js"}
    assert SHIM.stat().st_mode & 0o111, "npm keeps the mode the file has in the tarball"
    assert SHIM.read_text(encoding="utf-8").startswith("#!/usr/bin/env node\n")


def test_npm_files_carry_no_email_address():
    """The repository is public: no one's address goes in a file."""
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    for path in (PACKAGE_JSON, NPM / "README.md", SHIM):
        assert not email.search(path.read_text(encoding="utf-8")), path.name


def test_readmes_that_a_registry_renders_have_only_absolute_links():
    """PyPI and npm render README.md away from the repository, so ``docs/X.md`` is a 404."""
    relative = re.compile(r"\]\((?!https?://|#|mailto:)")
    for path in (ROOT / "README.md", NPM / "README.md"):
        assert not relative.findall(path.read_text(encoding="utf-8")), path


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed")
def test_npm_pack_ships_exactly_the_files_the_package_names():
    """What `npm publish` would upload, as npm itself reports it. ``prepack`` copies the
    repository LICENSE in (the copy is gitignored), so the tarball has all five."""
    done = subprocess.run(
        ["npm", "pack", "--dry-run", "--json"], cwd=NPM, capture_output=True, text=True, check=True
    )
    # A lifecycle banner ("> pkg prepack ...") may come before the JSON.
    (entry,) = json.loads(done.stdout[done.stdout.index("[") :])
    paths = sorted(f["path"] for f in entry["files"])
    assert paths == [
        "LICENSE",
        "README.md",
        "bin/lyra-browser.js",
        "constraints.txt",
        "package.json",
    ]
    modes = {f["path"]: f["mode"] for f in entry["files"]}
    assert modes["bin/lyra-browser.js"] & 0o111, (
        "the launcher lost its executable bit in the tarball"
    )
    assert entry["unpackedSize"] < 50_000, "the package is a launcher; something large got in"
