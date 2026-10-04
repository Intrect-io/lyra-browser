import asyncio

import pytest

from lyra_browser.config import Config
from lyra_browser.context import ServerContext, acquire_page
from lyra_browser.session import BrowserSession, BrowserUnavailable


def test_default_candidates_prefer_installed_then_bundled():
    cfg = Config()
    # No explicit channel: try the user's installed browsers, bundled Chromium last.
    assert cfg.browser_candidates() == ["chrome", "msedge", None]


def test_explicit_channel_is_tried_first():
    cfg = Config(channel="msedge")
    assert cfg.browser_candidates() == ["msedge", None]


def test_no_bundled_fallback_when_disabled():
    cfg = Config(allow_bundled_fallback=False)
    assert cfg.browser_candidates() == ["chrome", "msedge"]


def test_browser_unavailable_envelope():
    env = BrowserUnavailable(["chrome", "msedge", None], cause=None).envelope()
    assert env["status"] == "browser_unavailable"
    assert env["tried"] == ["chrome", "msedge", "bundled-chromium"]
    assert "Chrome" in env["user_action"]


class _RaisingSession(BrowserSession):
    async def page(self):  # type: ignore[override]
        raise BrowserUnavailable(["chrome", None], cause=None)


@pytest.mark.asyncio
async def test_acquire_page_returns_envelope_when_unavailable(tmp_path):
    from lyra_browser.approval import CollaborationState
    from lyra_browser.audit import AuditLog

    cfg = Config()
    object.__setattr__(cfg, "data_dir", tmp_path)
    cfg.__post_init__()
    ctx = ServerContext(
        config=cfg,
        session=_RaisingSession(cfg),
        audit=AuditLog(cfg.audit_path),
        collab=CollaborationState(),
    )
    page, err = await acquire_page(ctx)
    assert page is None
    assert err["status"] == "browser_unavailable"


@pytest.mark.asyncio
async def test_concurrent_page_calls_launch_once_and_share_the_page(tmp_path, monkeypatch):
    """Concurrent MCP calls must not open the persistent profile more than once.

    ``_launch`` is replaced so nothing here touches a disk, but the session is
    still built on ``tmp_path`` rather than ``Config()``'s default — which is the
    developer's real profile directory (``~/.lyra-browser``, real logins).
    """
    cfg = Config()
    object.__setattr__(cfg, "data_dir", tmp_path)
    cfg.__post_init__()
    session = BrowserSession(cfg)
    expected_page = object()
    launch_started = asyncio.Event()
    release_launch = asyncio.Event()
    launch_count = 0

    async def fake_launch():
        nonlocal launch_count
        launch_count += 1
        launch_started.set()
        await release_launch.wait()
        session._context = object()
        session._page = expected_page

    monkeypatch.setattr(session, "_launch", fake_launch)

    calls = [asyncio.create_task(session.page()) for _ in range(10)]
    await launch_started.wait()
    await asyncio.sleep(0)
    release_launch.set()
    pages = await asyncio.gather(*calls)

    assert launch_count == 1
    assert pages == [expected_page] * 10


@pytest.mark.asyncio
async def test_concurrent_acquire_page_calls_report_browser_unavailable(tmp_path, monkeypatch):
    """A launch failure stays a structured result for every concurrent caller.

    The failure comes from ``_launch``, not from ``page()``: raising above
    ``start()`` would skip the lock, the ``_starting`` flag and the ``finally``
    that clears it, which is the whole path this is meant to cover.
    """
    from lyra_browser.approval import CollaborationState
    from lyra_browser.audit import AuditLog

    cfg = Config()
    object.__setattr__(cfg, "data_dir", tmp_path)
    cfg.__post_init__()
    session = BrowserSession(cfg)
    attempts = 0

    async def failing_launch():
        nonlocal attempts
        attempts += 1
        raise BrowserUnavailable(["chrome", None], cause=None)

    monkeypatch.setattr(session, "_launch", failing_launch)
    ctx = ServerContext(
        config=cfg,
        session=session,
        audit=AuditLog(cfg.audit_path),
        collab=CollaborationState(),
    )

    results = await asyncio.gather(*(acquire_page(ctx) for _ in range(10)))

    assert all(page is None for page, _ in results)
    assert all(err["status"] == "browser_unavailable" for _, err in results)
    assert session._starting is False, "a failed launch must clear the in-flight flag"
    # Each serialised caller retries rather than sharing one failure. That is
    # current behaviour, not a promise: caching the failure would change this
    # number deliberately, and this assertion is how that change gets noticed.
    assert attempts == 10
