"""Two servers on one data dir must not fight over the persistent profile.

Chrome serves one process per profile directory; a second ``launch_persistent_context``
on it dies with "Opening in existing browser session". The harness spawns several
servers per profile (a gateway and its cron worker), so which of them gets the
profile is decided when each first opens a browser — not when it is spawned. These
tests hold the claim from a second descriptor or a second process, the way a rival
server would, and drive the real ``BrowserSession`` over a fake driver.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lyra_browser.config import Config
from lyra_browser.session import (
    BrowserSession,
    ProfileLock,
    _is_profile_busy,
    sweep_dead_instances,
)

BUSY = (
    "BrowserType.launch_persistent_context: Opening in existing browser session. "
    "This usually means that the profile is already in use by another instance of Chromium."
)


def make_config(tmp_path) -> Config:
    cfg = Config(headless=False, channel="chrome")
    cfg.data_dir = tmp_path / "lyra-browser"
    cfg.__post_init__()
    cfg.capture_dir = cfg.data_dir / "captures"
    return cfg


class FakeContext:
    def __init__(self) -> None:
        self.pages = [object()]
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def route(self, pattern, handler) -> None:
        pass

    def on(self, event, handler) -> None:
        pass


class FakePlaywright:
    """Records each launch; ``busy_on`` makes the listed profiles refuse like Chrome does."""

    def __init__(self, busy_on=()) -> None:
        self.launches: list[dict] = []
        self.busy_on = {str(p) for p in busy_on}
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    async def _launch(self, **kwargs) -> FakeContext:
        self.launches.append(kwargs)
        if kwargs["user_data_dir"] in self.busy_on:
            raise RuntimeError(BUSY)
        return FakeContext()

    async def stop(self) -> None:
        pass


def make_session(cfg, monkeypatch, pw=None):
    session = BrowserSession(cfg)
    pw = pw or FakePlaywright()

    async def start_playwright():
        return pw

    monkeypatch.setattr(session, "_start_playwright", start_playwright)
    return session, pw


def hold_from_another_process(lock_path):
    """A live process that holds the claim, as a rival server would. Caller kills it."""
    code = (
        "import fcntl, os, sys\n"
        f"fd = os.open({str(lock_path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "sys.stdin.read()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


# --------------------------------------------------------------------------
# The claim itself
# --------------------------------------------------------------------------


def test_second_claim_on_one_lock_file_is_refused_until_the_first_releases(tmp_path):
    path = tmp_path / "profile.lock"
    first, second = ProfileLock(path), ProfileLock(path)

    assert first.try_acquire()
    assert not second.try_acquire()
    first.release()
    assert second.try_acquire()
    second.release()


def test_a_claim_dies_with_its_holder(tmp_path):
    """SIGKILL must not leave a stale claim: that was the whole point of flock."""
    path = tmp_path / "profile.lock"
    holder = hold_from_another_process(path)
    try:
        assert not ProfileLock(path).try_acquire()
    finally:
        holder.kill()
        holder.wait()
    mine = ProfileLock(path)
    assert mine.try_acquire()
    mine.release()


def test_a_busy_launch_error_is_recognised():
    assert _is_profile_busy(RuntimeError(BUSY))
    assert not _is_profile_busy(RuntimeError("Executable doesn't exist at /opt/chrome"))


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------


async def test_a_free_profile_is_the_shared_one(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    session, pw = make_session(cfg, monkeypatch)

    await session.start()

    assert session.profile_mode == "shared"
    assert pw.launches[0]["user_data_dir"] == str(cfg.profile_dir)
    assert not cfg.instances_dir.exists()


async def test_a_held_profile_sends_the_second_server_to_a_private_one(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    rival = os.open(cfg.data_dir / "profile.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(rival, fcntl.LOCK_EX)
    try:
        session, pw = make_session(cfg, monkeypatch)
        await session.start()
    finally:
        os.close(rival)

    assert session.profile_mode == "instance"
    private = cfg.instances_dir / str(os.getpid()) / "profile"
    assert pw.launches[0]["user_data_dir"] == str(private)
    assert private.is_dir()
    assert str(cfg.profile_dir) not in {launch["user_data_dir"] for launch in pw.launches}


async def test_exactly_one_of_two_sessions_gets_the_shared_profile(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    first, first_pw = make_session(cfg, monkeypatch)
    second, second_pw = make_session(cfg, monkeypatch)

    await first.start()
    await second.start()

    assert (first.profile_mode, second.profile_mode) == ("shared", "instance")
    assert first_pw.launches[0]["user_data_dir"] == str(cfg.profile_dir)
    assert second_pw.launches[0]["user_data_dir"] != str(cfg.profile_dir)


async def test_closing_the_browser_hands_the_profile_to_the_next_server(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    first, _ = make_session(cfg, monkeypatch)
    second, second_pw = make_session(cfg, monkeypatch)
    await first.start()
    await first.stop()

    await second.start()

    assert second.profile_mode == "shared"
    assert second_pw.launches[0]["user_data_dir"] == str(cfg.profile_dir)


async def test_a_session_that_never_launches_holds_nothing(tmp_path, monkeypatch):
    """The idle gateway child must not keep the cron worker out of the logins."""
    cfg = make_config(tmp_path)
    idle, _ = make_session(cfg, monkeypatch)  # constructed, never started
    worker, worker_pw = make_session(cfg, monkeypatch)

    await worker.start()

    assert idle.profile_mode == "shared"
    assert worker.profile_mode == "shared"
    assert worker_pw.launches[0]["user_data_dir"] == str(cfg.profile_dir)


async def test_a_failed_launch_releases_the_claim(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    broken, _ = make_session(cfg, monkeypatch)

    async def boom():
        raise RuntimeError("driver exploded")

    monkeypatch.setattr(broken, "_start_playwright", boom)
    with pytest.raises(RuntimeError):
        await broken.start()

    probe = ProfileLock(cfg.data_dir / "profile.lock")
    assert probe.try_acquire()
    probe.release()


async def test_chrome_already_on_the_profile_without_a_claim_also_falls_back(tmp_path, monkeypatch):
    """A server from before the claim existed holds Chrome but never took the lock."""
    cfg = make_config(tmp_path)
    pw = FakePlaywright(busy_on=[cfg.profile_dir])
    session, _ = make_session(cfg, monkeypatch, pw)

    await session.start()

    assert session.profile_mode == "instance"
    assert [launch["user_data_dir"] for launch in pw.launches] == [
        str(cfg.profile_dir),
        str(cfg.instances_dir / str(os.getpid()) / "profile"),
    ]
    # The claim was given back: the shared profile is not ours.
    probe = ProfileLock(cfg.data_dir / "profile.lock")
    assert probe.try_acquire()
    probe.release()


async def test_the_fallback_sweeps_instance_dirs_of_dead_servers(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    stale = cfg.instances_dir / str(dead.pid) / "profile"
    stale.mkdir(parents=True)
    alive = cfg.instances_dir / str(os.getppid()) / "profile"
    alive.mkdir(parents=True)
    (cfg.instances_dir / "notes").mkdir()
    rival = os.open(cfg.data_dir / "profile.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(rival, fcntl.LOCK_EX)
    try:
        session, _ = make_session(cfg, monkeypatch)
        await session.start()
    finally:
        os.close(rival)

    assert not stale.parent.exists()
    assert alive.is_dir(), "a running server's directory is never touched"
    assert (cfg.instances_dir / "notes").is_dir(), "only pid-named directories are swept"


def test_sweep_never_removes_its_own_directory(tmp_path):
    mine = tmp_path / str(os.getpid())
    mine.mkdir()
    sweep_dead_instances(tmp_path, keep_pid=os.getpid())
    assert mine.is_dir()


# --------------------------------------------------------------------------
# What the agent is told
# --------------------------------------------------------------------------


async def test_open_browser_tells_the_agent_when_logins_are_missing(make_ctx, tools_of):
    ctx = make_ctx()
    ctx.session.profile_mode = "instance"
    tools = await tools_of(ctx)

    result = await tools["open_browser"]()

    assert result["profile"] == "instance"
    assert "no saved logins in this instance" in result["note"]


async def test_open_browser_on_the_shared_profile_has_nothing_to_warn_about(make_ctx, tools_of):
    ctx = make_ctx()
    ctx.session.profile_mode = "shared"
    tools = await tools_of(ctx)

    result = await tools["open_browser"]()

    assert result["profile"] == "shared"
    assert "note" not in result


async def test_closing_a_private_instance_removes_its_profile(tmp_path, monkeypatch):
    cfg = make_config(tmp_path)
    cfg.data_dir.mkdir(parents=True)
    rival = os.open(cfg.data_dir / "profile.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(rival, fcntl.LOCK_EX)
    try:
        session, _ = make_session(cfg, monkeypatch)
        await session.start()
        private = cfg.instances_dir / str(os.getpid())
        assert private.is_dir()
        await session.stop()
    finally:
        os.close(rival)

    assert not private.exists()
    assert cfg.profile_dir.is_dir(), "the shared profile is never touched"
