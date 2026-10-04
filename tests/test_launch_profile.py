"""The session launches a Chrome that looks like the one a person opens.

Measurement showed what gave the agent away: ``navigator.webdriver`` left true
by Playwright's defaults, the ``HeadlessChrome`` token in the headless UA, and
an emulated viewport that reports itself as the screen. These tests pin the
launch options that remove those, and the one-time relaunch that learns the
real Chrome version for the headless UA.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lyra_browser.config import Config
from lyra_browser.session import (
    BrowserSession,
    UserAgentCache,
    _fixed_user_agent,
    launch_kwargs,
)

HEADLESS_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "HeadlessChrome/154.0.8037.57 Safari/537.36"
)
WINDOWED_UA = HEADLESS_UA.replace("HeadlessChrome", "Chrome")


def make_config(tmp_path, **kwargs) -> Config:
    cfg = Config(**kwargs)
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    return cfg


# --------------------------------------------------------------------------
# Launch options
# --------------------------------------------------------------------------


def test_webdriver_flag_is_off_in_both_modes(tmp_path):
    for headless in (False, True):
        kwargs = launch_kwargs(make_config(tmp_path, headless=headless), "chrome")
        assert "--disable-blink-features=AutomationControlled" in kwargs["args"]
        assert kwargs["channel"] == "chrome"
        assert kwargs["service_workers"] == "block"


def test_headful_sizes_the_window_instead_of_emulating_a_viewport(tmp_path):
    kwargs = launch_kwargs(make_config(tmp_path, headless=False), "chrome")
    assert kwargs["no_viewport"] is True
    assert "viewport" not in kwargs
    assert "screen" not in kwargs
    assert "--window-size=1280,800" in kwargs["args"]
    # The real UA is fine with a window; nothing to override.
    assert "user_agent" not in kwargs


def test_headless_keeps_the_viewport_on_a_desktop_sized_screen(tmp_path):
    kwargs = launch_kwargs(make_config(tmp_path, headless=True), None, WINDOWED_UA)
    assert kwargs["viewport"] == {"width": 1280, "height": 800}
    assert kwargs["screen"] == {"width": 1920, "height": 1080}
    assert kwargs["user_agent"] == WINDOWED_UA
    assert "no_viewport" not in kwargs
    assert "channel" not in kwargs


def test_headless_screen_is_never_smaller_than_the_viewport(tmp_path):
    cfg = make_config(tmp_path, headless=True)
    cfg.viewport_width, cfg.viewport_height = 2560, 1440
    kwargs = launch_kwargs(cfg, "chrome")
    assert kwargs["screen"] == {"width": 2560, "height": 1440}


def test_headless_without_a_known_ua_sets_no_override(tmp_path):
    kwargs = launch_kwargs(make_config(tmp_path, headless=True), "chrome", None)
    assert "user_agent" not in kwargs


# --------------------------------------------------------------------------
# UA fix and cache
# --------------------------------------------------------------------------


def test_fixed_user_agent_drops_only_the_headless_token():
    assert _fixed_user_agent(HEADLESS_UA) == WINDOWED_UA
    assert _fixed_user_agent(WINDOWED_UA) is None


def test_ua_cache_roundtrip_and_forget(tmp_path):
    cache = UserAgentCache(tmp_path / "ua.json")
    assert cache.get("chrome") is None
    cache.put("chrome", WINDOWED_UA)
    cache.put("msedge", "Edge UA")
    assert UserAgentCache(tmp_path / "ua.json").get("chrome") == WINDOWED_UA
    cache.put("chrome", None)
    assert cache.get("chrome") is None
    assert cache.get("msedge") == "Edge UA"
    assert not (tmp_path / "ua.json.part").exists(), "the temp file is replaced, not left"


@pytest.mark.parametrize("content", ["not json", "[1,2]", '{"chrome": 3}'])
def test_ua_cache_tolerates_a_bad_file(tmp_path, content):
    path = tmp_path / "ua.json"
    path.write_text(content)
    cache = UserAgentCache(path)
    assert cache.get("chrome") is None
    cache.put("chrome", WINDOWED_UA)
    assert json.loads(path.read_text()) == {"chrome": WINDOWED_UA}


# --------------------------------------------------------------------------
# Launch flow: learn the UA once, then start with it
# --------------------------------------------------------------------------


class FakeContext:
    def __init__(self) -> None:
        self.pages = [object()]
        self.closed = False
        self.routes: list = []
        self.handlers: list = []

    async def close(self) -> None:
        self.closed = True

    async def route(self, pattern, handler) -> None:
        self.routes.append(pattern)

    def on(self, event, handler) -> None:
        self.handlers.append(event)


class FakePlaywright:
    """Records every launch; each returns a fresh context."""

    def __init__(self) -> None:
        self.launches: list[dict] = []
        self.contexts: list[FakeContext] = []
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)
        self.stopped = False

    async def _launch(self, **kwargs) -> FakeContext:
        self.launches.append(kwargs)
        ctx = FakeContext()
        self.contexts.append(ctx)
        return ctx

    async def stop(self) -> None:
        self.stopped = True


def make_session(tmp_path, monkeypatch, *, headless: bool, real_ua: str | None):
    cfg = make_config(tmp_path, headless=headless, channel="chrome")
    session = BrowserSession(cfg)
    pw = FakePlaywright()
    asked = []

    async def start_playwright():
        return pw

    async def real_user_agent():
        asked.append(True)
        return real_ua

    monkeypatch.setattr(session, "_start_playwright", start_playwright)
    monkeypatch.setattr(session, "_real_user_agent", real_user_agent)
    return session, pw, asked


async def test_first_headless_launch_learns_the_ua_and_relaunches_once(tmp_path, monkeypatch):
    session, pw, asked = make_session(tmp_path, monkeypatch, headless=True, real_ua=HEADLESS_UA)

    await session.start()

    assert len(pw.launches) == 2
    assert "user_agent" not in pw.launches[0]
    assert pw.launches[1]["user_agent"] == WINDOWED_UA
    assert pw.contexts[0].closed and not pw.contexts[1].closed
    assert session.started and session.active_channel == "chrome"
    assert UserAgentCache(tmp_path / "browser-ua.json").get("chrome") == WINDOWED_UA


async def test_later_headless_launches_start_with_the_cached_ua(tmp_path, monkeypatch):
    UserAgentCache(tmp_path / "browser-ua.json").put("chrome", WINDOWED_UA)
    session, pw, asked = make_session(tmp_path, monkeypatch, headless=True, real_ua=HEADLESS_UA)

    await session.start()

    assert len(pw.launches) == 1, "steady state: one launch, no relaunch"
    assert pw.launches[0]["user_agent"] == WINDOWED_UA
    assert asked == [True], "the cache is confirmed against the binary, not trusted"


async def test_a_chrome_update_refreshes_the_ua_with_one_relaunch(tmp_path, monkeypatch):
    stale = WINDOWED_UA.replace("154.0.8037.57", "153.0.7000.1")
    UserAgentCache(tmp_path / "browser-ua.json").put("chrome", stale)
    session, pw, _ = make_session(tmp_path, monkeypatch, headless=True, real_ua=HEADLESS_UA)

    await session.start()

    assert [kw.get("user_agent") for kw in pw.launches] == [stale, WINDOWED_UA]
    assert UserAgentCache(tmp_path / "browser-ua.json").get("chrome") == WINDOWED_UA


async def test_a_chrome_that_no_longer_says_headless_drops_the_override(tmp_path, monkeypatch):
    UserAgentCache(tmp_path / "browser-ua.json").put("chrome", WINDOWED_UA)
    session, pw, _ = make_session(tmp_path, monkeypatch, headless=True, real_ua=WINDOWED_UA)

    await session.start()

    assert [kw.get("user_agent") for kw in pw.launches] == [WINDOWED_UA, None]
    assert UserAgentCache(tmp_path / "browser-ua.json").get("chrome") is None


async def test_an_unanswerable_binary_keeps_the_session(tmp_path, monkeypatch):
    """No UA answer means no relaunch — a slightly odd UA beats no browser."""
    session, pw, _ = make_session(tmp_path, monkeypatch, headless=True, real_ua=None)

    await session.start()

    assert len(pw.launches) == 1
    assert session.started


async def test_headful_never_asks_and_never_relaunches(tmp_path, monkeypatch):
    session, pw, asked = make_session(tmp_path, monkeypatch, headless=False, real_ua=HEADLESS_UA)

    await session.start()

    assert len(pw.launches) == 1
    assert asked == []
    assert "user_agent" not in pw.launches[0]


async def test_route_guard_lands_on_the_relaunched_context(tmp_path, monkeypatch):
    """The guard must watch the window that is actually open, not the one that
    was closed to fix the UA."""
    session, pw, _ = make_session(tmp_path, monkeypatch, headless=True, real_ua=HEADLESS_UA)
    session.guard = object()

    await session.start()

    assert pw.contexts[0].routes == []
    assert pw.contexts[1].routes == ["**/*"]
    assert {"page", "dialog"} <= set(pw.contexts[1].handlers), "tab adoption and dialogs too"


async def test_request_observer_lands_on_the_relaunched_context(tmp_path, monkeypatch):
    """Redirect hops are heard as events, so the listener belongs on the context that is
    actually open — the one closed to fix the UA would never hear a hop again."""
    session, pw, _ = make_session(tmp_path, monkeypatch, headless=True, real_ua=HEADLESS_UA)
    session.guard = SimpleNamespace(on_request=lambda request: None)

    await session.start()

    assert "request" not in pw.contexts[0].handlers
    assert "request" in pw.contexts[1].handlers


# --------------------------------------------------------------------------
# Driver choice: patchright when present, playwright otherwise, pinnable
# --------------------------------------------------------------------------


def _fake_import(available: set[str]):
    def _import(name: str):
        pkg = name.split(".")[0]
        if pkg not in available:
            raise ImportError(name)
        return SimpleNamespace(async_playwright=lambda: pkg)

    return _import


def test_auto_prefers_patchright_when_installed(monkeypatch):
    from lyra_browser import session

    monkeypatch.setattr(
        session.importlib, "import_module", _fake_import({"patchright", "playwright"})
    )
    name, factory = session.load_driver("auto")
    assert name == "patchright" and factory() == "patchright"


def test_auto_falls_back_to_playwright(monkeypatch):
    """A VEGA runtime that bundles only playwright keeps working unchanged."""
    from lyra_browser import session

    monkeypatch.setattr(session.importlib, "import_module", _fake_import({"playwright"}))
    assert session.load_driver("auto")[0] == "playwright"


def test_a_pinned_driver_is_not_substituted(monkeypatch):
    from lyra_browser import session

    monkeypatch.setattr(session.importlib, "import_module", _fake_import({"playwright"}))
    assert session.load_driver("playwright")[0] == "playwright"
    with pytest.raises(ImportError, match="patchright"):
        session.load_driver("patchright")


def test_no_driver_at_all_is_an_import_error(monkeypatch):
    from lyra_browser import session

    monkeypatch.setattr(session.importlib, "import_module", _fake_import(set()))
    with pytest.raises(ImportError, match="patchright, playwright"):
        session.load_driver("auto")


def test_driver_env_and_unknown_value(monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_DRIVER", "playwright")
    assert Config.from_env().driver == "playwright"
    monkeypatch.setenv("LYRA_BROWSER_DRIVER", "selenium")
    assert Config.from_env().driver == "auto"


async def test_session_records_which_driver_launched(tmp_path, monkeypatch):
    session, pw, _ = make_session(tmp_path, monkeypatch, headless=False, real_ua=None)
    from lyra_browser import session as mod

    async def start():
        return pw

    monkeypatch.setattr(
        mod, "load_driver", lambda pref: ("patchright", lambda: SimpleNamespace(start=start))
    )
    # make_session stood in for _start_playwright; let the real one run here
    monkeypatch.delattr(session, "_start_playwright", raising=False)
    await session.start()
    assert session.active_driver == "patchright"
    await session.stop()
    assert session.active_driver is None


def test_no_proxy_by_default(tmp_path):
    assert "proxy" not in launch_kwargs(make_config(tmp_path, headless=True), "chrome")


def test_a_configured_proxy_reaches_the_launch(tmp_path):
    cfg = make_config(tmp_path, headless=True, proxy="socks5://10.0.0.2:1080")
    kwargs = launch_kwargs(cfg, "chrome")
    assert kwargs["proxy"] == {"server": "socks5://10.0.0.2:1080"}


def test_the_proxy_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("LYRA_BROWSER_PROXY", "socks5://10.0.0.2:1080")
    assert Config.from_env().proxy == "socks5://10.0.0.2:1080"
    monkeypatch.setenv("LYRA_BROWSER_PROXY", "")
    assert Config.from_env().proxy is None
