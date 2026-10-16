"""What PyPI will publish: the sdist's contents and the metadata its page shows.

The sdist is built from an allowlist. Hatch's default takes the whole working tree,
and an untracked file that is not gitignored (a local plan, a scratch directory) then
ships to a public index; measured 2026-10-16: 67 MB, including a private plan file.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_sdist_is_an_allowlist_of_what_the_package_needs():
    sdist = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert sorted(sdist["include"]) == ["/LICENSE", "/README.md", "/pyproject.toml", "/src"]


def test_pypi_page_speaks_of_lyra_alone():
    project = _pyproject()["project"]
    text = " ".join([project["description"], *project["keywords"]]).lower()
    assert "vega" not in text
    assert "lyra" in text


def test_project_names_what_a_registry_shows():
    project = _pyproject()["project"]
    assert (ROOT / project["readme"]).is_file()
    assert {"Homepage", "Repository", "Issues"} <= set(project["urls"])
    assert all("@" not in str(author.get("email", "")) for author in project["authors"])
