"""One server, two agent harnesses.

VEGA and Hermes both spawn lyra-browser as a stdio MCP server, and differ in
what they can do with the result: where the model is allowed to look at an
image, whether anyone is at the window, and what environment the child gets.
The client profile picks defaults for each; explicit settings always win.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lyra_browser.__main__ import build_parser, config_from_args
from lyra_browser.config import Config


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in (
        "VEGA_DATA_DIR",
        "LYRA_BROWSER_DATA_DIR",
        "LYRA_BROWSER_CAPTURE_DIR",
        "LYRA_BROWSER_HEADLESS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    return tmp_path


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def test_vega_data_dir_means_vega_and_keeps_its_paths(clean_env, monkeypatch):
    """The VEGA install must not move: same data dir, same uploads root."""
    monkeypatch.setenv("VEGA_DATA_DIR", str(clean_env / "vega"))
    cfg = Config.from_env()
    assert cfg.client == "vega"
    assert cfg.data_dir == clean_env / "vega" / "browser"
    assert cfg.capture_dir == clean_env / "vega" / "uploads" / "browser"
    assert cfg.headless is False


def test_hermes_home_means_hermes(clean_env, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(clean_env / "hh"))
    cfg = Config.from_env()
    assert cfg.client == "hermes"
    assert cfg.data_dir == clean_env / "hh" / "browser"
    # Under $HERMES_HOME/cache: the one host root Hermes' vision will read
    # from even under a sandboxed terminal backend.
    assert cfg.capture_dir == clean_env / "hh" / "cache" / "browser"


def test_hermes_without_hermes_home_uses_its_default_home(clean_env):
    """Hermes strips HERMES_HOME from its stdio servers' env, so an explicit
    --client hermes has to find ~/.hermes on its own."""
    cfg = Config.from_env(client="hermes")
    assert cfg.data_dir == clean_env / "home" / ".hermes" / "browser"
    assert cfg.capture_dir == clean_env / "home" / ".hermes" / "cache" / "browser"


def test_nothing_set_is_generic(clean_env):
    cfg = Config.from_env()
    assert cfg.client == "generic"
    assert cfg.data_dir == clean_env / "home" / ".lyra-browser"
    assert cfg.capture_dir == cfg.data_dir / "captures"


def test_explicit_client_env_beats_detection(clean_env, monkeypatch):
    monkeypatch.setenv("VEGA_DATA_DIR", str(clean_env / "vega"))
    monkeypatch.setenv("LYRA_BROWSER_CLIENT", "hermes")
    assert Config.from_env().client == "hermes"


def test_an_unknown_client_is_not_a_fourth_behaviour(clean_env, monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_CLIENT", "hermez")
    assert Config.from_env().client == "generic"
    assert Config(client="hermez").client == "generic"


def test_explicit_paths_beat_every_client(clean_env, monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_DATA_DIR", str(clean_env / "d"))
    monkeypatch.setenv("LYRA_BROWSER_CAPTURE_DIR", str(clean_env / "c"))
    for client in ("vega", "hermes", "generic"):
        cfg = Config.from_env(client=client)
        assert cfg.data_dir == clean_env / "d"
        assert cfg.capture_dir == clean_env / "c"


# --------------------------------------------------------------------------
# Headless default
# --------------------------------------------------------------------------


def test_hermes_defaults_to_headless(clean_env):
    """Hermes answers over chat, not at the window, and passes no display."""
    cfg = Config.from_env(client="hermes")
    assert cfg.headless is True
    assert cfg.attended is False


def test_linux_without_a_display_runs_headless(clean_env, monkeypatch):
    """A headful launch with no display is a crash; headless is a session."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert Config.from_env(client="vega").headless is True


def test_wayland_alone_counts_as_a_display(clean_env, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert Config.from_env(client="generic").headless is False


def test_macos_needs_no_display_variable(clean_env, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("sys.platform", "darwin")
    assert Config.from_env(client="vega").headless is False


@pytest.mark.parametrize(("env", "expected"), [("false", False), ("true", True)])
def test_explicit_headless_env_beats_the_client_default(clean_env, monkeypatch, env, expected):
    monkeypatch.setenv("LYRA_BROWSER_HEADLESS", env)
    assert Config.from_env(client="hermes").headless is expected


def test_blank_headless_env_means_undecided(clean_env, monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_HEADLESS", " ")
    assert Config.from_env(client="hermes").headless is True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_client_flag_picks_paths_and_mode(clean_env, monkeypatch):
    monkeypatch.setenv("VEGA_DATA_DIR", str(clean_env / "vega"))
    cfg = config_from_args(build_parser().parse_args(["--client", "hermes"]))
    assert cfg.client == "hermes"
    assert cfg.capture_dir == clean_env / "home" / ".hermes" / "cache" / "browser"
    assert cfg.headless is True


def test_no_headless_flag_beats_the_hermes_default(clean_env):
    cfg = config_from_args(build_parser().parse_args(["--client", "hermes", "--no-headless"]))
    assert cfg.headless is False


def test_unknown_client_flag_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--client", "hermez"])


# --------------------------------------------------------------------------
# Server instructions tell the truth for the client and the mode
# --------------------------------------------------------------------------


def _instructions(client: str, headless: bool) -> str:
    from lyra_browser.server import instructions_for

    return instructions_for(Config(client=client, headless=headless))


def test_headless_instructions_promise_no_window():
    text = _instructions("hermes", headless=True)
    assert "visible Chromium window" not in text
    assert "nobody is watching" in text
    # Handing work to a person who is not there is not an instruction to give.
    assert "call ask_user_to_do" not in text


def test_attended_instructions_keep_the_shared_window():
    text = _instructions("vega", headless=False)
    assert "visible Chromium window" in text
    assert "call ask_user_to_do" in text


def test_each_client_is_told_how_it_sees_a_capture():
    assert "attached to your next turn" in _instructions("vega", headless=False)
    hermes = _instructions("hermes", headless=True)
    assert "vision_analyze" in hermes
    assert "attached to your next turn" not in hermes
    assert "inline=true" in _instructions("generic", headless=True)


async def test_build_server_uses_the_config_it_was_given(tmp_path):
    from lyra_browser.server import build_server

    cfg = Config(client="hermes", headless=True)
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    mcp = build_server(cfg)
    assert "vision_analyze" in (mcp.instructions or "")
