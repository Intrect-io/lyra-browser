import re
from pathlib import Path

import pytest

from lyra_browser.config import Config
from lyra_browser.server import build_server, instructions_for

README = Path(__file__).resolve().parent.parent / "README.md"


def _readme_tools() -> set[str]:
    """Tool names in the README's Tools table: the backticked names in column two."""
    section = re.search(r"^## Tools\n(.*?)(?=^#{2,3} )", README.read_text(), re.S | re.M)
    assert section, "README has no '## Tools' section"
    names: set[str] = set()
    for line in section.group(1).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and not set(cells[0]) <= set("-: "):
            names.update(re.findall(r"`([a-z_]+)`", cells[1]))
    return names


@pytest.mark.asyncio
async def test_registered_tools_are_exactly_the_readme_tools(tmp_path):
    """A tool nobody documented, or a documented tool that is gone, fails here."""
    cfg = Config()
    object.__setattr__(cfg, "data_dir", tmp_path)
    cfg.__post_init__()
    mcp = build_server(cfg)
    registered = {tool.name for tool in await mcp.list_tools()}
    documented = _readme_tools()
    assert documented, "the README Tools table was not parsed"
    assert registered - documented == set(), "registered but missing from the README table"
    assert documented - registered == set(), "in the README table but not registered"


def _words(text: str) -> set[str]:
    """Identifiers as written in the instructions (``aria-ref`` stays one word)."""
    return set(re.findall(r"[A-Za-z_][\w-]*", text))


@pytest.mark.parametrize("client", ["vega", "hermes", "generic", "remote"])
@pytest.mark.parametrize("headless", [True, False])
def test_instructions_name_the_envelopes_tools_return(client, headless):
    """A status no instruction explains is one the model meets cold, mid-task."""
    words = _words(instructions_for(Config(client=client, headless=headless)))
    assert {
        "needs_approval",
        "takeover_active",
        "browser_unavailable",
        "unattended",
        "session_conflict",
        "blocked_by_policy",
    }.issubset(words)


@pytest.mark.parametrize("client", ["vega", "hermes", "generic", "remote"])
def test_instructions_say_how_to_address_an_element(client):
    words = _words(instructions_for(Config(client=client)))
    assert {
        "aria-ref",
        "tree",
        "click",
        "type_text",
        "refs",
        "not_found",
        "total_chars",
        "next_offset",
    }.issubset(words)


def test_healthz_answers_over_http_without_a_browser(tmp_path):
    """A platform's liveness ping must not need a session or a token."""
    from starlette.testclient import TestClient

    cfg = Config(client="remote")
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    with TestClient(build_server(cfg).http_app()) as http:
        reply = http.get("/healthz")
    assert reply.status_code == 200
    assert reply.text == "ok"
