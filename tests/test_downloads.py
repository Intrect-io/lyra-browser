"""Downloads are saved only when the agent asked, and nothing else is left on disk.

The browser accepts a file the moment a page offers one, and the navigation guard
never sees it — it judges requests, a download is a response. These tests drive
the real tools and the real handler against a fake page whose downloads behave the
way the measured ones do (see ``FakeDownload``). What they read is what is on disk,
what is left of the browser's own copy, which grants are still live and what the
audit trail says — not only the envelope a tool returned.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as DriverTimeout

from conftest import FakeLocator, FakePage, FakeSession
from lyra_browser import downloads
from lyra_browser.approval import CollaborationState
from lyra_browser.audit import AuditLog
from lyra_browser.config import Config
from lyra_browser.context import ServerContext
from lyra_browser.downloads import (
    remember_and_prune,
    sanitize_filename,
    source_origin,
    unique_name,
)
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability
from lyra_browser.session import BrowserSession

START = "https://start.example/"
SITE = parse_origin(START)
FILE_URL = "https://start.example/files/report.csv"
CSV = b"a,b\n1,2\n"  # 8 bytes


# --------------------------------------------------------------------------
# What the tests read
# --------------------------------------------------------------------------


def _audit(ctx) -> list[dict]:
    text = ctx.config.audit_path.read_text()
    return [json.loads(line) for line in text.splitlines() if line]


def _download_rows(ctx) -> list[dict]:
    return [row for row in _audit(ctx) if row["tool"] == "download"]


def _live_download_grants(ctx) -> list:
    return [g for g in ctx.perms.live_grants("default") if g.capability is Capability.DOWNLOAD]


def _on_disk(ctx) -> list[str]:
    """Everything under the download dir, hidden files and temp files included."""
    directory = ctx.config.download_dir
    return sorted(p.name for p in directory.rglob("*")) if directory.exists() else []


def _files(ctx) -> list[str]:
    """What was saved: the ledger the server keeps beside it is not a download."""
    return [name for name in _on_disk(ctx) if not name.startswith(".")]


@pytest.fixture
async def env(make_ctx, tools_of, page, make_download):
    ctx = make_ctx()
    return SimpleNamespace(ctx=ctx, tools=await tools_of(ctx), page=page, download=make_download)


# --------------------------------------------------------------------------
# A download nobody asked for is cancelled, deleted and reported
# --------------------------------------------------------------------------


async def test_a_download_nobody_asked_for_is_cancelled_and_reported(env):
    dl = env.download()
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl")

    assert result["status"] == "download_blocked"
    assert "download=true" in result["hint"], "the reply says how to get what was meant"
    assert result["blocked_download"] == {
        "filename": "report.csv",
        "url_origin": "https://start.example",
    }
    assert ("click", "#dl") in env.page.calls, "the click itself still happened"
    assert not dl.artifact.exists(), "the browser's own copy is deleted"
    assert _on_disk(env.ctx) == [], "and nothing reached the download dir"
    assert _download_rows(env.ctx)[-1]["status"] == "blocked"
    assert _live_download_grants(env.ctx) == []


async def test_a_download_still_in_flight_is_cancelled_before_it_finishes(env):
    dl = env.download(hangs=True)  # never completes: only cancelling removes it
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl")

    assert result["status"] == "download_blocked"
    assert not dl.artifact.exists()
    assert _on_disk(env.ctx) == []


async def test_a_download_arriving_with_no_call_in_flight_is_cancelled(env):
    """A page that downloads on its own, long after any tool call returned."""
    dl = env.download()

    env.ctx.downloads.on_download(dl)
    await env.ctx.downloads.idle()

    assert not dl.artifact.exists()
    assert _on_disk(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "blocked"


async def test_idle_returns_when_the_last_handler_ends_with_the_call(env, monkeypatch):
    """What a real browser leaves behind: a call returns the moment its handler ends,
    before the loop has run that task's done-callback. ``idle`` waited on the set,
    ``gather`` of finished tasks never yields, and so it spun the loop — and with it
    the callback that would have emptied the set — until the end-to-end gate hung."""
    env.page.downloads_on[("click", "#dl")] = env.download()
    calls: list = []
    real_gather = asyncio.gather

    def counting_gather(*args, **kwargs):
        calls.append(args)
        assert len(calls) < 10, "idle() is spinning"
        return real_gather(*args, **kwargs)

    monkeypatch.setattr(downloads.asyncio, "gather", counting_gather)

    await env.tools["click"](selector="#dl")
    await env.ctx.downloads.idle()


async def test_a_download_just_after_the_click_returns_is_still_reported(env):
    env.ctx.config.download_settle_s = 0.5
    dl = env.download(arrives_after=0.05)
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl")

    assert result["status"] == "download_blocked"
    assert not dl.artifact.exists()


async def test_a_download_after_the_settle_window_is_cancelled_all_the_same(env):
    env.ctx.config.download_settle_s = 0.0
    dl = env.download(arrives_after=0.05)
    env.page.downloads_on[("click", "#dl")] = dl

    await env.tools["click"](selector="#dl")
    await asyncio.sleep(0.1)
    await env.ctx.downloads.idle()

    assert not dl.artifact.exists(), "arriving late buys it nothing"
    assert _on_disk(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "blocked"


async def test_press_key_cancels_an_undeclared_download(env):
    dl = env.download()
    env.page.downloads_on[("key", "Enter")] = dl

    result = await env.tools["press_key"](key="Enter")

    assert result["status"] == "download_blocked"
    assert "download=true" in result["hint"]
    assert not dl.artifact.exists()
    assert _on_disk(env.ctx) == []


async def test_press_key_listens_longer_than_a_click_for_the_download_it_started(env):
    """A key press returns before the browser has sent the request — measured, the
    download lands up to ~100ms later — where a click waits for the navigation."""
    env.ctx.config.download_settle_s = 0.2
    dl = env.download(arrives_after=0.3)  # past a click's window, inside a key's
    env.page.downloads_on[("key", "Enter")] = dl

    result = await env.tools["press_key"](key="Enter")

    assert result["status"] == "download_blocked"
    assert not dl.artifact.exists()


async def test_navigate_cancels_an_undeclared_download_and_the_tab_stays(env):
    dl = env.download(arrives_after=0.01)  # the driver's error lands before the event
    env.page.downloads_on[("goto", FILE_URL)] = dl

    result = await env.tools["navigate"](url=FILE_URL)

    assert result["status"] == "download_blocked"
    assert "download=true" in result["hint"]
    assert result["requested_url"] == FILE_URL
    assert result["url"] == START, "the tab did not move"
    assert not dl.artifact.exists()
    assert _on_disk(env.ctx) == []


@pytest.mark.parametrize(
    ("name", "trigger"), [("go_back", ("go_back",)), ("reload_page", ("reload",))]
)
async def test_history_steps_cannot_declare_a_download_and_point_at_navigate(env, name, trigger):
    dl = env.download(arrives_after=0.01)
    env.page.downloads_on[trigger] = dl

    result = await env.tools[name]()

    assert result["status"] == "download_blocked"
    assert "navigate" in result["hint"] and "download=true" in result["hint"]
    assert "requested_url" not in result
    assert not dl.artifact.exists()
    assert _on_disk(env.ctx) == []


async def test_a_navigation_error_the_browser_never_backed_with_a_download_is_not_claimed(
    env, monkeypatch
):
    monkeypatch.setattr(downloads, "EVENT_WAIT_S", 0.05)

    async def starts_a_download_nobody_reports(url, wait_until=None):
        raise RuntimeError(f'Page.goto: Download is starting\nCall log:\n  - navigating to "{url}"')

    env.page.goto = starts_a_download_nobody_reports

    result = await env.tools["navigate"](url=FILE_URL)

    assert result["status"] == "download_started"
    assert "download" not in result, "nothing was saved, so nothing is claimed"
    assert result["url"] == START
    assert _on_disk(env.ctx) == []


# --------------------------------------------------------------------------
# A declared download is saved, once
# --------------------------------------------------------------------------


async def test_a_declared_download_is_saved_and_reported(env):
    dl = env.download()
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["status"] == "ok"
    saved = result["download"]
    assert saved == {
        "filename": "report.csv",
        "path": str(env.ctx.config.download_dir / "report.csv"),
        "bytes": 8,
        "url_origin": "https://start.example",
    }
    assert Path(saved["path"]).read_bytes() == CSV, "the exact bytes"
    assert not dl.artifact.exists(), "the browser's own copy does not linger"
    assert _files(env.ctx) == ["report.csv"]
    assert not [name for name in _on_disk(env.ctx) if name.endswith(".part")]
    row = _download_rows(env.ctx)[-1]
    assert row["status"] == "saved"
    assert row["args"]["path"] == saved["path"]
    assert env.ctx.downloads.saved() == [saved], "and the session keeps the list"
    assert _live_download_grants(env.ctx) == [], "the permission went with the file"


async def test_declaring_a_download_asks_for_the_download_permission_first(env):
    dl = env.download()
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl", download=True)  # not approved

    assert result["status"] == "needs_approval"
    assert result["reason"].startswith("download on "), "it is DOWNLOAD that was asked for"
    assert env.page.calls == [], "nothing happened before the answer"
    assert env.page.downloads_on, "and the page was never given the chance"
    assert _live_download_grants(env.ctx) == []


async def test_an_approval_covers_one_click_and_the_next_is_asked_again(env):
    env.page.downloads_on[("click", "#dl")] = env.download()
    first = await env.tools["click"](selector="#dl", download=True, confirm=True)
    assert "download" in first

    env.page.downloads_on[("click", "#dl")] = env.download()
    second = await env.tools["click"](selector="#dl", download=True)  # no confirm this time

    assert second["status"] == "needs_approval"
    assert env.page.calls.count(("click", "#dl")) == 1
    assert _files(env.ctx) == ["report.csv"]


async def test_the_permission_pays_for_one_file_in_a_call(env):
    one = env.download(name="one.csv", data=b"1")
    two = env.download(name="two.csv", data=b"2")
    env.page.downloads_on[("click", "#dl")] = [one, two]

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["status"] == "ok"
    assert result["download"]["filename"] == "one.csv"
    assert "1 more" in result["hint"]
    assert _files(env.ctx) == ["one.csv"]
    assert not two.artifact.exists(), "the second one was cancelled"
    assert sorted(row["status"] for row in _download_rows(env.ctx)) == ["blocked", "saved"]


async def test_a_click_after_a_declared_one_gets_no_second_file(env):
    env.page.downloads_on[("click", "#dl")] = env.download()
    await env.tools["click"](selector="#dl", download=True, confirm=True)

    second = env.download(name="again.csv")
    env.page.downloads_on[("click", "#dl")] = second
    result = await env.tools["click"](selector="#dl")

    assert result["status"] == "download_blocked", "the grant was spent by the first"
    assert not second.artifact.exists()
    assert _files(env.ctx) == ["report.csv"]


async def test_a_declared_call_that_starts_no_download_leaves_no_permission_behind(env):
    result = await env.tools["click"](selector="#dl", download=True, confirm=True, timeout_ms=50)

    assert result["status"] == "download_not_started"
    assert "read_page" in result["hint"]
    assert _live_download_grants(env.ctx) == [], "the bought grant was handed back"

    stray = env.download()
    env.page.on_download(stray)  # the page decides to download a moment later
    await env.ctx.downloads.idle()

    assert not stray.artifact.exists(), "and it cannot pay for whatever comes next"
    assert _files(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "blocked"


async def test_a_click_the_driver_fails_does_not_leave_its_download_permission_live(
    env, monkeypatch
):
    async def times_out(self, *args, **kwargs):
        raise DriverTimeout("Locator.click: Timeout 1000ms exceeded.")

    monkeypatch.setattr(FakeLocator, "click", times_out)

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["status"] == "timeout"
    assert _live_download_grants(env.ctx) == []


async def test_a_declared_press_key_saves_the_file(env):
    env.page.downloads_on[("key", "Enter")] = env.download()

    result = await env.tools["press_key"](key="Enter", download=True, confirm=True)

    assert result["status"] == "ok"
    assert Path(result["download"]["path"]).read_bytes() == CSV
    assert _live_download_grants(env.ctx) == []


async def test_a_declared_navigation_saves_the_file_and_the_tab_stays(env):
    env.page.downloads_on[("goto", FILE_URL)] = env.download(arrives_after=0.01)

    result = await env.tools["navigate"](url=FILE_URL, download=True, confirm=True)

    assert result["status"] == "ok"
    assert Path(result["download"]["path"]).read_bytes() == CSV
    assert result["url"] == START and env.page.url == START, "the tab did not move"
    assert result["requested_url"] == FILE_URL
    assert _live_download_grants(env.ctx) == []


async def test_a_declared_navigation_asks_for_the_download_permission(env):
    env.page.downloads_on[("goto", FILE_URL)] = env.download(arrives_after=0.01)

    result = await env.tools["navigate"](url=FILE_URL, download=True)

    assert result["status"] == "needs_approval"
    assert result["reason"].startswith("download on ")
    assert env.page.calls == []


async def test_a_declared_navigation_to_a_page_is_not_reported_as_a_download(env):
    result = await env.tools["navigate"](
        url="https://start.example/article", download=True, confirm=True
    )

    assert result["status"] == "download_not_started"
    assert "download" not in result
    assert result["title"] == "Start", "the page it did load is still described"
    assert _live_download_grants(env.ctx) == [], "and the permission is handed back"


# --------------------------------------------------------------------------
# What is kept, and where
# --------------------------------------------------------------------------


async def test_a_hostile_file_name_cannot_leave_the_download_dir(env, tmp_path):
    env.page.downloads_on[("click", "#dl")] = env.download(name="../../evil.txt", data=b"evil")

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    path = Path(result["download"]["path"])
    assert path.parent == env.ctx.config.download_dir
    assert path.name == "evil.txt"
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_a_taken_name_gets_a_suffix_and_neither_file_is_lost(env):
    env.page.downloads_on[("click", "#dl")] = env.download(data=b"first")
    first = await env.tools["click"](selector="#dl", download=True, confirm=True)
    env.page.downloads_on[("click", "#dl")] = env.download(data=b"second")
    second = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert first["download"]["filename"] == "report.csv"
    assert second["download"]["filename"] == "report (1).csv"
    assert Path(first["download"]["path"]).read_bytes() == b"first"
    assert Path(second["download"]["path"]).read_bytes() == b"second"


async def test_only_files_the_server_saved_are_ever_pruned(env):
    directory = env.ctx.config.download_dir
    directory.mkdir(parents=True)
    (directory / "notes.txt").write_text("not a download")
    env.ctx.config.download_keep = 2

    for name in ("a.csv", "b.csv", "c.csv"):
        env.page.downloads_on[("click", "#dl")] = env.download(name=name)
        await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert _files(env.ctx) == ["b.csv", "c.csv", "notes.txt"]


@pytest.mark.parametrize(("limit", "kept"), [(8, True), (7, False)])
async def test_the_size_limit_is_inclusive_and_a_bigger_file_is_deleted(env, limit, kept):
    env.ctx.config.download_max_bytes = limit
    dl = env.download()
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert not dl.artifact.exists()
    if kept:
        assert result["download"]["bytes"] == 8
        return
    assert result["status"] == "download_failed"
    assert "7 byte limit" in result["reason"]
    assert "download" not in result
    assert _files(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "too_large"
    assert _live_download_grants(env.ctx) == []


async def test_a_limit_of_zero_lifts_the_cap(env):
    env.ctx.config.download_max_bytes = 0
    env.page.downloads_on[("click", "#dl")] = env.download(data=b"x" * 300_000)

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["download"]["bytes"] == 300_000


async def test_a_download_that_never_finishes_is_cancelled_and_reported(env):
    env.ctx.config.download_timeout_s = 0.05
    dl = env.download(hangs=True)
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["status"] == "download_failed"
    assert "still arriving" in result["reason"]
    assert not dl.artifact.exists()
    assert _files(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "timeout"


async def test_a_broken_transfer_is_reported_and_leaves_nothing(env):
    dl = env.download(error="net::ERR_CONNECTION_RESET")
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["status"] == "download_failed"
    assert "ERR_CONNECTION_RESET" in result["reason"]
    assert "Download.path" not in result["reason"], "the driver's API name is noise"
    assert _files(env.ctx) == []
    assert not dl.artifact.exists()


async def test_a_transfer_the_driver_calls_canceled_does_not_read_as_our_doing(env):
    """Measured with a server that closed the connection early: ``Download.path:
    canceled``. A bare "canceled" next to ``download_blocked`` (which this server did
    cancel) would send the model looking for a permission."""
    env.page.downloads_on[("click", "#dl")] = env.download(error="canceled")

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["status"] == "download_failed"
    assert "interrupted" in result["reason"]
    assert "canceled" not in result["reason"] and "Download.path" not in result["reason"]
    assert _files(env.ctx) == []


async def test_the_audit_names_where_a_file_came_from_and_never_the_url(env):
    url = "https://cdn.example/files/report.csv?token=SECRET123&sig=abc"
    env.page.downloads_on[("click", "#dl")] = env.download(url=url)

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["download"]["url_origin"] == "https://cdn.example"
    assert _download_rows(env.ctx)[-1]["args"]["url_origin"] == "https://cdn.example"
    raw = env.ctx.config.audit_path.read_text()
    assert "SECRET123" not in raw and "sig=abc" not in raw


# --------------------------------------------------------------------------
# Who may pay for a file
# --------------------------------------------------------------------------


async def test_a_grant_pays_for_exactly_one_arrival(env):
    env.ctx.perms.grant("default", SITE, Capability.DOWNLOAD, SITE)
    first, second = env.download(name="one.csv"), env.download(name="two.csv")

    env.ctx.downloads.on_download(first)
    env.ctx.downloads.on_download(second)
    await env.ctx.downloads.idle()

    assert _files(env.ctx) == ["one.csv"], "judged in the order they came"
    assert not second.artifact.exists()


async def test_a_grant_held_by_another_session_pays_for_nothing(env):
    env.ctx.perms.grant("someone-else", SITE, Capability.DOWNLOAD, SITE)
    dl = env.download()

    env.ctx.downloads.on_download(dl)
    await env.ctx.downloads.idle()

    assert not dl.artifact.exists()
    assert _files(env.ctx) == []
    other = [
        g for g in env.ctx.perms.live_grants("someone-else") if g.capability.value == "download"
    ]
    assert len(other) == 1, "and their grant was not spent by ours"


async def test_a_grant_is_read_under_the_session_the_tools_last_served(env):
    """The handler runs from a browser callback and cannot ask who is calling; the
    tools leave the session on the guard, and that is the book it reads."""
    env.ctx.guard.session_key = "client-b"
    env.ctx.perms.grant("client-b", SITE, Capability.DOWNLOAD, SITE)
    dl = env.download()

    env.ctx.downloads.on_download(dl)
    await env.ctx.downloads.idle()

    assert _files(env.ctx) == ["report.csv"]
    assert len(env.ctx.downloads.saved("client-b")) == 1, "and the list is kept per session"
    assert env.ctx.downloads.saved("default") == []


async def test_a_download_delivered_twice_is_judged_once(env):
    env.ctx.perms.grant("default", SITE, Capability.DOWNLOAD, SITE)
    dl = env.download()

    env.ctx.downloads.on_download(dl)
    env.ctx.downloads.on_download(dl)
    await env.ctx.downloads.idle()

    assert _files(env.ctx) == ["report.csv"]
    assert len(_download_rows(env.ctx)) == 1


async def test_a_takeover_leaves_the_users_own_downloads_alone(env):
    env.ctx.collab.takeover = True
    theirs = env.download()

    env.ctx.downloads.on_download(theirs)
    await env.ctx.downloads.idle()

    assert _files(env.ctx) == ["report.csv"], "the user's download is the user's to make"
    assert _download_rows(env.ctx)[-1]["status"] == "user_driven"

    env.ctx.collab.takeover = False
    mine = env.download(name="agent.csv")
    env.ctx.downloads.on_download(mine)
    await env.ctx.downloads.idle()

    assert not mine.artifact.exists(), "and it stops the moment the agent has the wheel again"
    assert _files(env.ctx) == ["report.csv"]


# --------------------------------------------------------------------------
# Enforcement mode: enforce cancels what nothing covers, observe records it and keeps it
# --------------------------------------------------------------------------
#
# `observe` is the operator saying "record, do not block". The navigation guard then
# only logs `would_deny`; cancelling a download there would block after all, and a
# POST-backed "Export" cancelled once is re-sent when the model repeats it.


@pytest.fixture
def observing(env):
    """``env`` with enforcement switched to ``observe`` before anything happens."""
    env.ctx.config.enforcement_mode = "observe"
    return env


async def test_observe_saves_an_undeclared_download_and_says_so(observing):
    env = observing
    dl = env.download()
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl")

    assert result["status"] == "ok", "not download_blocked: nothing is blocked in this mode"
    saved = result["download"]
    assert saved == {
        "filename": "report.csv",
        "path": str(env.ctx.config.download_dir / "report.csv"),
        "bytes": 8,
        "url_origin": "https://start.example",
    }
    assert result["observed"] is True, "so the model can tell it was not covered"
    assert "observe" in result["hint"] and "download=true" in result["hint"]
    assert Path(saved["path"]).read_bytes() == CSV, "the exact bytes"
    assert not dl.artifact.exists(), "the browser's own copy is deleted, as for a declared one"
    assert _files(env.ctx) == ["report.csv"]
    row = _download_rows(env.ctx)[-1]
    assert row["status"] == "would_block"
    assert row["args"] == saved
    assert env.ctx.downloads.saved() == [saved]
    assert _live_download_grants(env.ctx) == [], "nothing was bought, and so nothing was spent"


async def test_enforce_cancels_the_same_download_and_keeps_no_file(env):
    """The twin of the test above, in the mode every server starts in."""
    assert env.ctx.config.enforcement_mode == "enforce"
    dl = env.download()
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl")

    assert result["status"] == "download_blocked"
    assert "download" not in result and "observed" not in result
    assert not dl.artifact.exists()
    assert _on_disk(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "blocked"


async def test_observe_saves_an_undeclared_key_press_download(observing):
    env = observing
    env.page.downloads_on[("key", "Enter")] = env.download()

    result = await env.tools["press_key"](key="Enter")

    assert result["status"] == "ok" and result["observed"] is True
    assert Path(result["download"]["path"]).read_bytes() == CSV
    assert _download_rows(env.ctx)[-1]["status"] == "would_block"


async def test_observe_saves_an_undeclared_navigation_download_and_the_tab_stays(observing):
    env = observing
    env.page.downloads_on[("goto", FILE_URL)] = env.download(arrives_after=0.01)

    result = await env.tools["navigate"](url=FILE_URL)

    assert result["status"] == "ok" and result["observed"] is True
    assert Path(result["download"]["path"]).read_bytes() == CSV
    assert result["url"] == START and env.page.url == START, "the tab did not move"
    assert result["requested_url"] == FILE_URL
    assert _download_rows(env.ctx)[-1]["status"] == "would_block"


@pytest.mark.parametrize(
    ("name", "trigger"), [("go_back", ("go_back",)), ("reload_page", ("reload",))]
)
async def test_observe_saves_a_download_a_history_step_started(observing, name, trigger):
    """In enforce mode these answer download_blocked and point at navigate(download=true),
    which would send the request a second time."""
    env = observing
    env.page.downloads_on[trigger] = env.download(arrives_after=0.01)

    result = await env.tools[name]()

    assert result["status"] == "ok" and result["observed"] is True
    assert Path(result["download"]["path"]).read_bytes() == CSV
    assert "navigate" not in result["hint"]
    assert _download_rows(env.ctx)[-1]["status"] == "would_block"
    assert _files(env.ctx) == ["report.csv"]


# Chrome 154 reports a reload or a history step that becomes a download as an abort —
# `net::ERR_ABORTED; maybe frame was detached?` — where `goto` gets "Download is
# starting", and offers the file 0.1-22ms after the error (measured, n=12 per case,
# headless and headful). Those tools used to raise, with the download judged behind
# their back.

HISTORY_STEPS = [("go_back", "go_back", ("go_back",)), ("reload_page", "reload", ("reload",))]


def _abort_like_chrome(page, method: str, trigger: tuple) -> None:
    """Make ``page.<method>`` fail the way Chrome does when the navigation turns into a
    download: the file (if one is registered for ``trigger``) and the abort come together."""

    async def aborted(wait_until=None):
        page.calls.append((method,))
        page.start_download(trigger)
        raise RuntimeError(f"Page.{method}: net::ERR_ABORTED; maybe frame was detached?")

    setattr(page, method, aborted)


@pytest.mark.parametrize(("tool", "method", "trigger"), HISTORY_STEPS)
@pytest.mark.parametrize("lag", [None, 0.01], ids=["file-then-error", "error-then-file"])
async def test_an_abort_a_download_follows_is_answered_as_that_download(
    env, tool, method, trigger, lag
):
    dl = env.download(arrives_after=lag)
    env.page.downloads_on[trigger] = dl
    _abort_like_chrome(env.page, method, trigger)

    result = await env.tools[tool]()

    assert result["status"] == "download_blocked"
    assert "navigate" in result["hint"] and "download=true" in result["hint"]
    assert result["url"] == START, "the tab did not move"
    assert not dl.artifact.exists()
    assert _on_disk(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "blocked"
    assert [r["status"] for r in _audit(env.ctx) if r["tool"] == tool][-1] == "download_started"


@pytest.mark.parametrize(("tool", "method", "trigger"), HISTORY_STEPS)
async def test_observe_saves_the_download_an_aborted_history_step_started(
    observing, tool, method, trigger
):
    env = observing
    env.page.downloads_on[trigger] = env.download(arrives_after=0.01)
    _abort_like_chrome(env.page, method, trigger)

    result = await env.tools[tool]()

    assert result["status"] == "ok" and result["observed"] is True
    assert Path(result["download"]["path"]).read_bytes() == CSV
    assert _download_rows(env.ctx)[-1]["status"] == "would_block"
    assert _files(env.ctx) == ["report.csv"]


@pytest.mark.parametrize(("tool", "method", "trigger"), HISTORY_STEPS)
async def test_an_abort_no_download_follows_is_still_an_error(
    env, monkeypatch, tool, method, trigger
):
    monkeypatch.setattr(downloads, "EVENT_WAIT_S", 0.05)
    _abort_like_chrome(env.page, method, trigger)  # nothing registered: no file comes

    with pytest.raises(RuntimeError, match="ERR_ABORTED"):
        await env.tools[tool]()

    assert _on_disk(env.ctx) == []


@pytest.mark.parametrize(("tool", "method", "trigger"), HISTORY_STEPS)
async def test_only_an_abort_waits_for_a_download(env, monkeypatch, tool, method, trigger):
    """Any other navigation error is the caller's at once, not after a wait for a file."""
    monkeypatch.setattr(downloads, "EVENT_WAIT_S", 5.0)

    async def refused(wait_until=None):
        raise RuntimeError(f"Page.{method}: net::ERR_CONNECTION_REFUSED at {START}")

    setattr(env.page, method, refused)

    began = time.monotonic()
    with pytest.raises(RuntimeError, match="ERR_CONNECTION_REFUSED"):
        await env.tools[tool]()

    assert time.monotonic() - began < 2.0


async def test_observe_does_not_flag_a_download_a_permission_covered(observing):
    """``would_block`` and ``observed`` mean "enforcement would have cancelled this"."""
    env = observing
    env.page.downloads_on[("click", "#dl")] = env.download()

    result = await env.tools["click"](selector="#dl", download=True, confirm=True)

    assert result["status"] == "ok" and result["download"]["bytes"] == 8
    assert "observed" not in result
    assert _download_rows(env.ctx)[-1]["status"] == "saved"


async def test_observe_never_spends_a_grant(observing):
    """As the guard does in this mode: a live grant marks a file as covered and is left
    as it was. (The call that bought it still retires it when it stops waiting.)"""
    env = observing
    env.ctx.perms.grant("default", SITE, Capability.DOWNLOAD, SITE)
    first, second = env.download(name="one.csv"), env.download(name="two.csv")

    env.ctx.downloads.on_download(first)
    env.ctx.downloads.on_download(second)
    await env.ctx.downloads.idle()

    assert _files(env.ctx) == ["one.csv", "two.csv"]
    assert [row["status"] for row in _download_rows(env.ctx)] == ["saved", "saved"]
    (grant,) = _live_download_grants(env.ctx)
    assert grant.uses_left == 1


async def test_observe_keeps_every_file_of_a_call_and_says_so(observing):
    env = observing
    one, two = env.download(name="one.csv", data=b"1"), env.download(name="two.csv", data=b"2")
    env.page.downloads_on[("click", "#dl")] = [one, two]

    result = await env.tools["click"](selector="#dl")

    assert result["status"] == "ok" and result["download"]["filename"] == "one.csv"
    assert _files(env.ctx) == ["one.csv", "two.csv"]
    assert "1 more" in result["hint"] and "saved too" in result["hint"]
    assert "pays for one file" not in result["hint"], "nothing was cancelled, so nothing says so"


async def test_observe_still_caps_the_size(observing):
    env = observing
    env.ctx.config.download_max_bytes = 7
    dl = env.download()
    env.page.downloads_on[("click", "#dl")] = dl

    result = await env.tools["click"](selector="#dl")

    assert result["status"] == "download_failed" and "7 byte limit" in result["reason"]
    assert "download" not in result
    assert not dl.artifact.exists()
    assert _files(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "too_large"


async def test_observe_names_and_prunes_files_like_any_other(observing):
    env = observing
    directory = env.ctx.config.download_dir
    directory.mkdir(parents=True)
    (directory / "notes.txt").write_text("not a download")
    env.ctx.config.download_keep = 1

    for name in ("../../a.csv", "b.csv"):
        env.page.downloads_on[("click", "#dl")] = env.download(name=name)
        result = await env.tools["click"](selector="#dl")
        assert Path(result["download"]["path"]).parent == directory

    assert _files(env.ctx) == ["b.csv", "notes.txt"], "the ledger prunes ours and only ours"


async def test_observe_leaves_a_takeover_download_marked_as_the_users(observing):
    env = observing
    env.ctx.collab.takeover = True

    env.ctx.downloads.on_download(env.download())
    await env.ctx.downloads.idle()

    assert _files(env.ctx) == ["report.csv"]
    assert _download_rows(env.ctx)[-1]["status"] == "user_driven"


@pytest.mark.parametrize("mode", ["enforce", "Observe", "observe ", "OBSERVE", "off", ""])
async def test_only_the_exact_word_observe_stops_the_cancelling(env, mode):
    """The guard's own rule: a value nobody can read never turns the gate off."""
    env.ctx.config.enforcement_mode = mode
    dl = env.download()

    env.ctx.downloads.on_download(dl)
    await env.ctx.downloads.idle()

    assert not dl.artifact.exists()
    assert _on_disk(env.ctx) == []
    assert _download_rows(env.ctx)[-1]["status"] == "blocked"


def test_the_operators_setting_reaches_the_guard_and_the_download_judge_alike(
    tmp_path, monkeypatch
):
    """One switch, two layers: LYRA_BROWSER_ENFORCEMENT moves both or neither."""
    monkeypatch.setenv("LYRA_BROWSER_DATA_DIR", str(tmp_path))
    for value in ("observe", "enforce"):
        monkeypatch.setenv("LYRA_BROWSER_ENFORCEMENT", value)
        cfg = Config.from_env()
        ctx = ServerContext(
            config=cfg,
            session=FakeSession(FakePage()),
            audit=AuditLog(cfg.audit_path),
            collab=CollaborationState(require_approval=True),
        )
        assert ctx.guard.mode == value
        assert ctx.downloads.observing is (value == "observe")


# --------------------------------------------------------------------------
# list_downloads: the session's record of what it saved
# --------------------------------------------------------------------------


async def test_list_downloads_returns_what_was_saved_and_only_that(env):
    for name in ("one.csv", "two.csv"):
        env.page.downloads_on[("click", "#dl")] = env.download(name=name)
        await env.tools["click"](selector="#dl", download=True, confirm=True)
    env.page.downloads_on[("click", "#dl")] = env.download(name="refused.csv")
    await env.tools["click"](selector="#dl")  # blocked: no file, so no entry
    calls_before = list(env.page.calls)

    listed = await env.tools["list_downloads"]()

    assert listed["status"] == "ok" and listed["count"] == 2
    assert [d["filename"] for d in listed["downloads"]] == ["one.csv", "two.csv"]
    assert listed["download_dir"] == str(env.ctx.config.download_dir)
    assert all(Path(d["path"]).is_file() for d in listed["downloads"])
    assert env.page.calls == calls_before, "a read: it touched nothing"


async def test_list_downloads_is_empty_before_anything_was_saved(env):
    listed = await env.tools["list_downloads"]()

    assert listed["count"] == 0 and listed["downloads"] == []


async def test_list_downloads_works_during_a_takeover(env):
    env.ctx.collab.takeover = True

    assert (await env.tools["list_downloads"]())["status"] == "ok"


async def test_list_downloads_does_not_show_another_sessions_files(env):
    env.ctx.perms.grant("default", SITE, Capability.DOWNLOAD, SITE)
    env.ctx.downloads.on_download(env.download())
    await env.ctx.downloads.idle()
    env.ctx.owner_session = "someone-else"  # a live holder, window open

    listed = await env.tools["list_downloads"]()

    assert listed["status"] == "session_conflict"
    assert "downloads" not in listed


# --------------------------------------------------------------------------
# The listener reaches every tab, popups included
# --------------------------------------------------------------------------


class _Tab:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.listeners: dict[str, list] = {}

    def on(self, event, handler) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def emit(self, event, *args) -> None:
        for handler in list(self.listeners.get(event, [])):
            handler(*args)

    def is_closed(self) -> bool:
        return False


class _Browser:
    """A persistent context with the one blank tab Chrome opens."""

    def __init__(self) -> None:
        self.pages = [_Tab()]
        self._handlers: dict[str, list] = {}
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    async def _launch(self, **_kwargs) -> _Browser:
        return self

    def on(self, event, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def route(self, *_args) -> None:
        pass

    def popup(self) -> _Tab:
        """What ``window.open`` does: the tab joins ``pages``, then "page" is emitted."""
        tab = _Tab()
        self.pages.append(tab)
        for handler in self._handlers.get("page", []):
            handler(tab)
        return tab

    async def stop(self) -> None:
        pass


async def test_every_tab_the_session_adopts_has_its_downloads_judged(
    tmp_path, monkeypatch, make_download
):
    cfg = Config(headless=False)
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    cfg.download_dir = tmp_path / "downloads"
    session = BrowserSession(cfg)
    browser = _Browser()

    async def start_playwright():
        return browser

    monkeypatch.setattr(session, "_start_playwright", start_playwright)
    ctx = ServerContext(
        config=cfg,
        session=session,
        audit=AuditLog(cfg.audit_path),
        collab=CollaborationState(require_approval=True),
    )
    await session.start()
    first = browser.pages[0]
    popup = browser.popup()
    on_first, on_popup = make_download(), make_download(name="popup.csv")

    first.emit("download", on_first)
    popup.emit("download", on_popup)
    await ctx.downloads.idle()

    assert not on_first.artifact.exists(), "the tab the browser opened with"
    assert not on_popup.artifact.exists(), "and a popup opened later"
    assert [row["status"] for row in _download_rows(ctx)] == ["blocked", "blocked"]

    ctx.perms.grant("default", SITE, Capability.DOWNLOAD, SITE)
    paid = make_download(name="paid.csv")
    popup.emit("download", paid)
    await ctx.downloads.idle()

    assert [p.name for p in cfg.download_dir.iterdir() if not p.name.startswith(".")] == [
        "paid.csv"
    ], "a popup's download is paid for like any other"


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.csv", "report.csv"),
        ("보고서 2026.csv", "보고서 2026.csv"),
        ("../../evil.txt", "evil.txt"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("C:\\Windows\\win.ini", "win.ini"),
        ("dir/sub/", "sub"),
        (".bashrc", "bashrc"),
        ("...hidden", "hidden"),
        ("..", "download"),
        ("", "download"),
        (None, "download"),
        ("   ", "download"),
        ("/", "download"),
        ("a\x00b\nc.txt", "abc.txt"),
        ("invoice\u202egpj.exe", "invoicegpj.exe"),
        ('re:port*?"<>|.csv', "report.csv"),
        ("name. . ", "name"),
        ("CON", "_CON"),
        ("aux.txt", "_aux.txt"),
        ("LPT3.log", "_LPT3.log"),
        ("console.log", "console.log"),
    ],
)
def test_a_file_name_is_reduced_to_something_safe_to_create(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("name", ["a" * 500 + ".csv", "é" * 300 + ".tar.gz", "가" * 200 + ".txt"])
def test_a_long_name_is_cut_by_bytes_and_keeps_its_extension(name):
    result = sanitize_filename(name)

    assert len(result.encode()) <= 180
    assert result.endswith(name[name.index(".") :]) or result.endswith((".csv", ".tar.gz", ".txt"))
    assert result[0] == name[0]


def test_a_name_that_is_taken_gets_the_first_free_suffix(tmp_path):
    (tmp_path / "report.csv").write_text("x")
    (tmp_path / "report (1).csv").write_text("x")
    (tmp_path / "archive.tar.gz").write_text("x")
    (tmp_path / "dangling.txt").symlink_to(tmp_path / "nowhere")

    assert unique_name(tmp_path, "report.csv") == "report (2).csv"
    assert unique_name(tmp_path, "archive.tar.gz") == "archive (1).tar.gz"
    assert unique_name(tmp_path, "free.csv") == "free.csv"
    assert unique_name(tmp_path, "dangling.txt") == "dangling (1).txt", (
        "a dangling link holds the name: writing through it would land elsewhere"
    )


def test_pruning_never_deletes_a_file_the_server_did_not_save(tmp_path):
    (tmp_path / "foreign.txt").write_text("someone else's")
    for name in ("one", "two", "three", "four"):
        (tmp_path / name).write_text(name)
        remember_and_prune(tmp_path, name, keep=2)

    assert sorted(p.name for p in tmp_path.iterdir() if not p.name.startswith(".")) == [
        "foreign.txt",
        "four",
        "three",
    ]


def test_a_file_removed_by_hand_does_not_cost_the_oldest_one_its_place(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name)
        remember_and_prune(tmp_path, name, keep=3)
    (tmp_path / "c").unlink()  # the newest goes; the ledger still lists it

    (tmp_path / "d").write_text("d")
    remember_and_prune(tmp_path, "d", keep=3)

    assert (tmp_path / "a").exists(), "a ghost in the list is not a reason to delete a real file"


def test_a_ledger_entry_that_is_a_path_deletes_nothing_outside_the_dir(tmp_path):
    directory = tmp_path / "downloads"
    directory.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (directory / ".lyra-browser-downloads").write_text("../victim.txt\n")
    (directory / "new").write_text("n")

    remember_and_prune(directory, "new", keep=1)

    assert victim.read_text() == "keep me"


def test_keep_zero_keeps_everything_and_writes_no_ledger(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text(name)
        remember_and_prune(tmp_path, name, keep=0)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("https://cdn.example/f.csv?token=secret", "https://cdn.example"),
        ("http://127.0.0.1:8080/files/x", "http://127.0.0.1:8080"),
        ("blob:https://app.example/8c1f-uuid", "https://app.example"),
        ("data:text/csv,a,b", "data: (no site)"),
        ("", "(unknown)"),
    ],
)
def test_a_download_is_attributed_to_a_site_and_never_a_path(url, origin):
    assert source_origin(url) == origin


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_download_dir_defaults_under_the_data_dir_and_can_be_moved(monkeypatch, tmp_path):
    monkeypatch.delenv("LYRA_BROWSER_DOWNLOAD_DIR", raising=False)
    monkeypatch.setenv("LYRA_BROWSER_DATA_DIR", str(tmp_path))
    assert Config.from_env().download_dir == tmp_path / "downloads"

    monkeypatch.setenv("LYRA_BROWSER_DOWNLOAD_DIR", str(tmp_path / "elsewhere"))
    assert Config.from_env().download_dir == tmp_path / "elsewhere"


def test_download_limits_come_from_the_environment_and_fall_back(monkeypatch, tmp_path):
    monkeypatch.setenv("LYRA_BROWSER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LYRA_BROWSER_DOWNLOAD_KEEP", "7")
    monkeypatch.setenv("LYRA_BROWSER_DOWNLOAD_MAX_BYTES", "1234")
    monkeypatch.setenv("LYRA_BROWSER_DOWNLOAD_TIMEOUT", "9.5")
    cfg = Config.from_env()
    assert (cfg.download_keep, cfg.download_max_bytes, cfg.download_timeout_s) == (7, 1234, 9.5)

    for name in ("KEEP", "MAX_BYTES", "TIMEOUT"):
        monkeypatch.setenv(f"LYRA_BROWSER_DOWNLOAD_{name}", "lots")
    cfg = Config.from_env()
    assert (cfg.download_keep, cfg.download_max_bytes, cfg.download_timeout_s) == (
        200,
        200 * 1024 * 1024,
        120.0,
    )
