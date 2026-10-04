"""A popup that closes itself must not strand the session, and the agent can pick a tab.

The session used to follow onto every new tab and never look back: an OAuth
popup that finished with ``window.close()`` left it holding a dead page, so the
next tool failed with "Target closed" while ``open_browser`` still said ok. These
tests drive the real ``BrowserSession`` (and the real tools) against a fake
browser that behaves the way the measured one does — a popup keeps its opener
after it closes, a closed opener reads as ``None``, and headful Chrome quits when
its last window closes — so a gate or a fallback that only *looks* right does not
pass. Assertions read what was asked of each tab, not only the envelope.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from types import SimpleNamespace

import pytest

from lyra_browser.approval import CollaborationState
from lyra_browser.audit import AuditLog
from lyra_browser.config import Config
from lyra_browser.context import ServerContext
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability
from lyra_browser.session import BrowserClosed, BrowserSession
from lyra_browser.tools import tabs as tabs_tool

APP = "https://app.example/dashboard"
SIGN_IN = "https://idp.example/oauth?code=SECRET-CODE"
OTHER = "https://other.example/"

GONE = "BrowserContext.new_page: Target page, context or browser has been closed"


class FakeError(Exception):
    """What the driver raises once the thing it was talking to is gone."""


class FakeTab:
    """A Playwright Page, as far as tabs are concerned. Every ask lands in ``calls``."""

    def __init__(self, browser, url, title="", opener=None):
        self._browser = browser
        self.url = url
        self._title = title
        self._opener = opener
        self.closed = False
        self.title_hangs = False
        self.calls: list[tuple] = []

    def is_closed(self) -> bool:
        return self.closed

    async def opener(self):
        # Answered from state the page already holds: a popup still knows who
        # opened it after it has closed, and an opener that has closed is None.
        await asyncio.sleep(0)
        if self._opener is not None and self._opener.closed:
            return None
        return self._opener

    async def title(self) -> str:
        if self.title_hangs:
            await asyncio.Event().wait()
        return self._title

    async def bring_to_front(self) -> None:
        await asyncio.sleep(0)  # every driver call is a round trip
        if self._browser.gone:
            raise FakeError("Page.bring_to_front: " + GONE.split(": ", 1)[1])
        self.calls.append(("bring_to_front",))

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.calls.append(("close",))
        self._browser.page_closed(self)


class FakeBrowser:
    """A persistent context and the browser behind it."""

    def __init__(self, quits_with_last_window: bool) -> None:
        self._pages: list[FakeTab] = []
        self._handlers: dict[str, list] = {}
        self.routes: list[tuple] = []
        self.events: list[tuple] = []
        self.quits_with_last_window = quits_with_last_window
        self.gone = False

    @property
    def pages(self) -> list[FakeTab]:
        return list(self._pages)

    def on(self, event, handler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    async def route(self, pattern, handler) -> None:
        self.routes.append((pattern, handler))

    async def new_page(self) -> FakeTab:
        await asyncio.sleep(0)
        if self.gone:
            raise FakeError(GONE)
        self.events.append(("new_page",))
        return self._add(FakeTab(self, "about:blank"))

    def popup(self, opener: FakeTab, url: str, title: str = "") -> FakeTab:
        """What ``window.open`` and a ``target=_blank`` link do."""
        return self._add(FakeTab(self, url, title, opener=opener))

    def _add(self, tab: FakeTab) -> FakeTab:
        # BrowserContext._on_page: the page joins ``pages``, then "page" is emitted.
        self._pages.append(tab)
        for handler in self._handlers.get("page", []):
            handler(tab)
        return tab

    def page_closed(self, tab: FakeTab) -> None:
        self.events.append(("close", tab.url))
        tab.closed = True
        if tab in self._pages:
            self._pages.remove(tab)
        if not self._pages and self.quits_with_last_window:
            self.gone = True

    def crash(self) -> None:
        """The browser dies under the session: every tab closes, nothing can open."""
        self.gone = True
        for tab in self._pages:
            tab.closed = True
        self._pages.clear()

    async def close(self) -> None:
        self.crash()


class FakePlaywright:
    def __init__(self, quits_with_last_window: bool) -> None:
        self._quits = quits_with_last_window
        self.browsers: list[FakeBrowser] = []
        self.stops = 0
        self.chromium = SimpleNamespace(launch_persistent_context=self._launch)

    async def _launch(self, **kwargs) -> FakeBrowser:
        browser = FakeBrowser(self._quits)
        browser._add(FakeTab(browser, "about:blank"))  # Chrome opens with one blank tab
        self.browsers.append(browser)
        return browser

    async def stop(self) -> None:
        self.stops += 1


def _config(tmp_path) -> Config:
    cfg = Config(headless=False)
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    cfg.capture_dir = tmp_path / "captures"
    return cfg


@pytest.fixture
def make_session(tmp_path, monkeypatch):
    """A real BrowserSession whose driver is the fake above. Not yet started."""

    def _make(*, quits_with_last_window: bool = False):
        session = BrowserSession(_config(tmp_path))
        pw = FakePlaywright(quits_with_last_window)

        async def start_playwright():
            return pw

        monkeypatch.setattr(session, "_start_playwright", start_playwright)
        return session, pw

    return _make


@pytest.fixture
def make_env(tmp_path, monkeypatch, tools_of):
    """The real tools over a real session over the fake browser.

    The first tab shows ``APP``; ``approved`` are the sites the agent already
    holds NAVIGATE (and so INTERACT) on.
    """

    async def _make(*, quits_with_last_window: bool = False, approved=(APP,)):
        cfg = _config(tmp_path)
        session = BrowserSession(cfg)
        pw = FakePlaywright(quits_with_last_window)

        async def start_playwright():
            return pw

        monkeypatch.setattr(session, "_start_playwright", start_playwright)
        ctx = ServerContext(
            config=cfg,
            session=session,
            audit=AuditLog(cfg.audit_path),
            collab=CollaborationState(require_approval=True),
        )
        await session.start()
        browser = pw.browsers[0]
        first = browser.pages[0]
        first.url, first._title = APP, "App"
        for url in approved:
            ctx.perms.grant("default", parse_origin(url), Capability.NAVIGATE)
        return SimpleNamespace(
            ctx=ctx,
            tools=await tools_of(ctx),
            session=session,
            pw=pw,
            browser=browser,
            first=first,
        )

    return _make


async def _started(make_session, **kwargs):
    session, pw = make_session(**kwargs)
    await session.start()
    browser = pw.browsers[0]
    return session, pw, browser, browser.pages[0]


# --------------------------------------------------------------------------
# A tab that ends under the agent
# --------------------------------------------------------------------------


async def test_a_popup_that_closes_itself_hands_the_session_back_to_its_opener(make_session):
    """The regression: the session kept the closed popup as its page.

    Two other tabs are open and the newest is not the opener, so falling back to
    "the last tab" would be wrong here — only the opener is where the user was.
    """
    session, _, browser, app = await _started(make_session)
    other = await browser.new_page()
    await session.activate(app)
    popup = browser.popup(app, SIGN_IN)
    assert await session.page() is popup, "the session follows onto the popup"

    await popup.close()

    page = await session.page()
    assert page is app, "back on the tab that opened it, not on the newest one"
    assert not page.is_closed()
    assert other in browser.pages and not other.closed, "and nothing else was touched"


async def test_with_the_opener_gone_the_newest_open_tab_is_used(make_session):
    session, _, browser, app = await _started(make_session)
    older = await browser.new_page()
    newest = await browser.new_page()
    await session.activate(app)
    popup = browser.popup(app, SIGN_IN)
    await app.close()
    await popup.close()

    assert await session.page() is newest
    assert older in browser.pages


async def test_a_tab_without_an_opener_falls_back_to_the_newest_open_one(make_session):
    session, _, browser, app = await _started(make_session)
    middle = await browser.new_page()
    typed = await browser.new_page()  # a tab the user opened by hand: no opener
    assert await session.page() is typed

    await typed.close()

    assert await session.page() is middle


async def test_when_no_tab_is_left_a_blank_one_is_opened_once(make_session):
    """Two callers finding the same dead tab must not open two blank ones."""
    session, _, browser, app = await _started(make_session)
    await app.close()
    opened_before = browser.events.count(("new_page",))

    pages = await asyncio.gather(*(session.page() for _ in range(6)))

    assert len({id(p) for p in pages}) == 1
    assert pages[0].url == "about:blank" and not pages[0].is_closed()
    assert browser.events.count(("new_page",)) == opened_before + 1
    assert len(browser.pages) == 1


async def test_page_is_never_a_closed_tab_whatever_closes(make_session):
    """The invariant, over many arbitrary opens and closes."""
    session, _, browser, _ = await _started(make_session)
    rng = random.Random(20260930)
    for _ in range(300):
        pages = browser.pages
        choice = rng.choice(["popup", "typed", "close_active", "close_any", "close_all"])
        if choice == "popup" and pages:
            browser.popup(rng.choice(pages), "https://p.example/")
        elif choice == "typed":
            await browser.new_page()
        elif choice == "close_active" and pages:
            await (await session.page()).close()
        elif choice == "close_any" and pages:
            await rng.choice(pages).close()
        elif choice == "close_all":
            for tab in pages:
                await tab.close()

        page = await session.page()

        assert not page.is_closed()
        assert page in browser.pages


@pytest.mark.parametrize("how", ["last window closed", "crash with three tabs open"])
async def test_a_browser_that_went_away_is_started_again(make_session, how):
    """Headful Chrome quits with its last window, and a crash takes every tab at once."""
    session, pw, browser, app = await _started(make_session, quits_with_last_window=True)
    if how == "last window closed":
        await app.close()
    else:
        await browser.new_page()
        browser.popup(app, SIGN_IN)
        browser.crash()
    assert browser.gone

    page = await session.page()

    assert len(pw.browsers) == 2, "a second browser was launched"
    assert page in pw.browsers[1].pages and not page.is_closed()
    assert pw.stops == 1, "and the driver of the dead one was let go, not leaked"
    assert session.started


async def test_get_url_after_a_self_closing_popup_reports_the_opener(make_env):
    """The tool the popup used to break: it read the closed popup's title and raised."""
    env = await make_env()
    popup = env.browser.popup(env.first, SIGN_IN)
    await popup.close()

    assert await env.tools["get_url"]() == {"url": APP, "title": "App", "tab_count": 1}
    assert ("new_page",) not in env.browser.events, "moving to the opener opened nothing"


async def test_get_url_and_open_browser_say_how_many_tabs_are_open(make_env):
    """The agent follows onto a popup; this is how it learns there are now two."""
    env = await make_env()
    env.browser.popup(env.first, SIGN_IN)

    read = await env.tools["get_url"]()
    opened = await env.tools["open_browser"]()

    assert read["url"] == SIGN_IN and read["tab_count"] == 2
    assert opened["status"] == "ok" and opened["tab_count"] == 2


async def test_a_read_that_counts_tabs_does_not_bring_a_closed_browser_back(make_env):
    env = await make_env()
    await env.first.close()
    events_before = list(env.browser.events)

    result = await env.tools["get_url"]()

    assert result["status"] == "browser_closed" and "tab_count" not in result
    assert env.browser.events == events_before, "counting the tabs opened nothing"


async def test_open_browser_reports_the_live_page_not_the_dead_popup(make_env):
    """It said ok, with the popup's URL, for a page nothing could use."""
    env = await make_env()
    popup = env.browser.popup(env.first, SIGN_IN)
    await popup.close()

    result = await env.tools["open_browser"]()

    assert result["status"] == "ok" and result["url"] == APP


async def test_open_browser_starts_the_browser_again_when_it_went_away(make_env):
    env = await make_env(quits_with_last_window=True)
    env.browser.crash()

    result = await env.tools["open_browser"]()

    assert result["status"] == "ok" and result["url"] == "about:blank"
    assert len(env.pw.browsers) == 2 and not env.pw.browsers[1].pages[0].closed


# --------------------------------------------------------------------------
# Reading never brings a closed browser back
# --------------------------------------------------------------------------

# Every tool that reads through ``acquire_page(claim=False)``, with arguments that get
# past its own validation. A new read tool belongs here.
READ_TOOLS = {
    "get_url": {},
    "read_page": {},
    "screenshot": {},
    "read_image": {"selector": "img"},
    "read_form": {},
    "read_draft": {},
    "wait_for": {"text": "ready"},
    "tabs": {},
}

# How a browser ends up closed, and whether closing its last tab takes the browser with
# it: headful Chrome quits with its last window, headless keeps running.
CLOSED_WAYS = [
    pytest.param("last tab closed", False, id="last-tab-closed"),
    pytest.param("last window closed", True, id="last-window-closed-browser-quits"),
    pytest.param("crash", True, id="crash"),
    pytest.param("close_browser", False, id="close_browser"),
]


async def _close(env, how: str) -> None:
    if how == "crash":
        env.browser.crash()
    elif how == "close_browser":
        await env.tools["close_browser"]()
    else:
        await env.first.close()


async def test_a_reader_moves_to_the_opener_without_opening_anything(make_session):
    session, _, browser, app = await _started(make_session)
    await browser.new_page()
    await session.activate(app)
    popup = browser.popup(app, SIGN_IN)
    await popup.close()
    opened = browser.events.count(("new_page",))

    page = await session.page(revive=False)

    assert page is app
    assert browser.events.count(("new_page",)) == opened


async def test_a_reader_moves_to_the_newest_open_tab_without_opening_anything(make_session):
    session, _, browser, _ = await _started(make_session)
    newest = await browser.new_page()
    typed = await browser.new_page()  # opened by hand: no opener to go back to
    await typed.close()
    opened = browser.events.count(("new_page",))

    assert await session.page(revive=False) is newest
    assert browser.events.count(("new_page",)) == opened


@pytest.mark.parametrize("how", ["last tab closed", "last window closed", "crash", "stopped"])
async def test_a_reader_is_refused_once_the_browser_is_closed_and_opens_nothing(make_session, how):
    session, pw, browser, app = await _started(
        make_session, quits_with_last_window=how == "last window closed"
    )
    if how == "crash":
        browser.crash()
    elif how == "stopped":
        await session.stop()
    else:
        await app.close()
    events_before = list(browser.events)

    with pytest.raises(BrowserClosed):
        await session.page(revive=False)

    assert browser.events == events_before, "no tab was opened"
    assert len(pw.browsers) == 1, "and no browser was started"


async def test_a_browser_nobody_has_started_still_starts_for_a_reader(make_session):
    session, pw = make_session()

    page = await session.page(revive=False)

    assert len(pw.browsers) == 1 and page in pw.browsers[0].pages


@pytest.mark.parametrize("quits", [False, True], ids=["browser-stays", "browser-quits"])
async def test_a_claiming_call_still_revives_what_a_reader_was_refused(make_session, quits):
    session, pw, browser, app = await _started(make_session, quits_with_last_window=quits)
    await app.close()
    with pytest.raises(BrowserClosed):
        await session.page(revive=False)

    page = await session.page()

    assert page.url == "about:blank" and not page.is_closed()
    assert len(pw.browsers) == (2 if quits else 1)
    assert await session.page(revive=False) is page, "and a reader can use what it opened"


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
@pytest.mark.parametrize(("how", "quits"), CLOSED_WAYS)
async def test_a_read_tool_reports_a_closed_browser_and_reopens_nothing(make_env, how, quits, name):
    env = await make_env(quits_with_last_window=quits)
    await _close(env, how)
    events_before = list(env.browser.events)

    result = await env.tools[name](**READ_TOOLS[name])

    assert result["status"] == "browser_closed"
    assert "open_browser" in result["hint"]
    assert env.browser.events == events_before, "no tab was opened"
    assert len(env.pw.browsers) == 1, "and no browser was started"


async def test_repeated_reads_over_a_closed_browser_open_nothing(make_env):
    env = await make_env()
    await env.first.close()

    for _ in range(2):
        for name, kwargs in READ_TOOLS.items():
            assert (await env.tools[name](**kwargs))["status"] == "browser_closed"

    assert env.browser.events == [("close", APP)]
    assert len(env.pw.browsers) == 1


@pytest.mark.parametrize(("how", "quits"), CLOSED_WAYS)
async def test_open_browser_brings_a_closed_browser_back_for_the_reads_that_follow(
    make_env, how, quits
):
    env = await make_env(quits_with_last_window=quits)
    await _close(env, how)
    assert (await env.tools["get_url"]())["status"] == "browser_closed"

    reopened = await env.tools["open_browser"]()

    assert reopened["status"] == "ok" and reopened["url"] == "about:blank"
    assert await env.tools["get_url"]() == {"url": "about:blank", "title": "", "tab_count": 1}
    assert len(env.pw.browsers) == (1 if how == "last tab closed" else 2)


async def test_listing_downloads_with_the_browser_closed_opens_nothing(make_env):
    """It needs no page, so there is nothing to refuse — and nothing it may open."""
    env = await make_env(quits_with_last_window=True)
    await env.first.close()
    events_before = list(env.browser.events)

    result = await env.tools["list_downloads"]()

    assert result["status"] == "ok"
    assert env.browser.events == events_before and len(env.pw.browsers) == 1


# --------------------------------------------------------------------------
# Choosing a tab (the session side of the tool)
# --------------------------------------------------------------------------


async def test_closing_the_last_tab_opens_a_blank_one_first(make_session):
    """Closing headful Chrome's last window quits it, taking the context along.

    The fake reproduces that, so a close-then-open order would leave a dead
    browser here, as it did against the real one.
    """
    session, pw, browser, app = await _started(make_session, quits_with_last_window=True)

    page = await session.close_page(app)

    assert page.url == "about:blank" and not page.is_closed()
    assert not browser.gone and len(pw.browsers) == 1, "the browser never went away"
    assert browser.events == [("new_page",), ("close", "about:blank")]
    assert app.calls == [("close",)]


async def test_closing_the_active_tab_returns_to_its_opener_and_shows_it(make_session):
    session, _, browser, app = await _started(make_session)
    await browser.new_page()
    await session.activate(app)
    popup = browser.popup(app, SIGN_IN)

    page = await session.close_page(popup)

    assert page is app
    assert popup.calls == [("close",)]
    assert ("bring_to_front",) in app.calls, "the user is shown the tab the agent is on"


async def test_closing_a_background_tab_leaves_the_active_one_alone(make_session):
    session, _, browser, app = await _started(make_session)
    await browser.new_page()
    active = await browser.new_page()

    page = await session.close_page(app)

    assert page is active
    assert app.closed and active.calls == [], "the active tab was not even raised"


async def test_activating_a_tab_raises_it_and_makes_it_the_page(make_session):
    session, _, browser, app = await _started(make_session)
    later = await browser.new_page()

    assert await session.activate(app) is True

    assert await session.page() is app
    assert app.calls == [("bring_to_front",)] and later.calls == []


async def test_a_tab_that_closed_first_cannot_be_activated(make_session):
    session, _, browser, app = await _started(make_session)
    later = await browser.new_page()
    await app.close()

    assert await session.activate(app) is False

    assert await session.page() is later, "the session stays where it was"


async def test_tab_info_counts_the_open_tabs_and_finds_the_active_one(make_session):
    session, _, browser, app = await _started(make_session)
    assert session.tab_info() == {"tab_count": 1, "active_index": 0}
    await browser.new_page()
    popup = browser.popup(app, SIGN_IN)
    assert session.tab_info() == {"tab_count": 3, "active_index": 2}

    await popup.close()
    await session.page()

    assert session.tab_info() == {"tab_count": 2, "active_index": 0}


# --------------------------------------------------------------------------
# Popups are still judged where they always were
# --------------------------------------------------------------------------


async def test_popups_are_covered_by_the_one_context_level_route(make_session):
    """``page.route`` never sees a popup's first request, which is why the guard
    sits on the context. Following onto a tab must not add anything per tab —
    the fake tab has no ``route`` at all, so an attempt would raise."""
    session, pw = make_session()
    guard = object()
    session.guard = guard
    await session.start()
    browser = pw.browsers[0]

    popup = browser.popup(browser.pages[0], SIGN_IN)
    await popup.close()
    await session.page()

    assert browser.routes == [("**/*", guard)]


async def test_redirect_hops_are_heard_once_on_the_context_not_once_per_tab(make_session):
    """A hop never reaches a route handler, so the guard listens for it. One listener on
    the context hears every tab, popups included; a tab that adds its own would judge each
    hop twice and spend a single-use grant twice."""
    session, pw = make_session()
    observer = object()
    session.guard = SimpleNamespace(on_request=observer)
    await session.start()
    browser = pw.browsers[0]

    popup = browser.popup(browser.pages[0], SIGN_IN)
    await popup.close()
    await session.page()

    assert browser._handlers["request"] == [observer]


async def test_no_observer_means_no_request_listener(make_session):
    session, pw = make_session()
    await session.start()

    assert "request" not in pw.browsers[0]._handlers


# --------------------------------------------------------------------------
# The tool: listing
# --------------------------------------------------------------------------


async def test_list_shows_every_tab_in_opening_order_and_the_active_one(make_env):
    env = await make_env()
    env.browser.popup(env.first, OTHER, title="Other")

    result = await env.tools["tabs"]()

    assert result["status"] == "ok"
    assert result["tabs"] == [
        {"index": 0, "url": APP, "title": "App", "active": False},
        {"index": 1, "url": OTHER, "title": "Other", "active": True},
    ]
    assert result["tab_count"] == 2 and result["active_index"] == 1


async def test_list_after_a_popup_closed_reports_the_tab_it_returned_to(make_env):
    env = await make_env()
    popup = env.browser.popup(env.first, SIGN_IN)
    await popup.close()

    result = await env.tools["tabs"]()

    assert [t["url"] for t in result["tabs"]] == [APP]
    assert result["tabs"][0]["active"] and result["active_index"] == 0


async def test_list_is_a_read_it_neither_claims_the_browser_nor_waits_for_a_takeover(make_env):
    env = await make_env()
    env.ctx.collab.takeover = True

    result = await env.tools["tabs"]()

    assert result["status"] == "ok"
    assert env.ctx.owner_session is None, "reading left the browser unclaimed"
    assert all(tab.calls == [] for tab in env.browser.pages)


async def test_list_does_not_read_another_sessions_browser(make_env):
    env = await make_env()
    env.ctx.owner_session = "someone-else"
    env.ctx.owner_seen_at = time.monotonic()

    result = await env.tools["tabs"]()

    assert result["status"] == "session_conflict"


async def test_a_tab_that_will_not_answer_is_listed_without_a_title(make_env, monkeypatch):
    """A tab stuck in a script never returns its title. The list that would let
    the agent find that tab must not hang on it."""
    monkeypatch.setattr(tabs_tool, "_TITLE_BUDGET_S", 0.05)
    env = await make_env()
    stuck = env.browser.popup(env.first, OTHER, title="Other")
    stuck.title_hangs = True

    result = await asyncio.wait_for(env.tools["tabs"](), timeout=3)

    assert [t["title"] for t in result["tabs"]] == ["App", ""]


# --------------------------------------------------------------------------
# The tool: switching
# --------------------------------------------------------------------------


async def test_switch_makes_the_tab_the_page_and_raises_it(make_env):
    env = await make_env(approved=(APP, OTHER))
    other = env.browser.popup(env.first, OTHER, title="Other")
    assert await env.session.page() is other

    result = await env.tools["tabs"](action="switch", index=0)

    assert result["status"] == "ok" and result["url"] == APP and result["title"] == "App"
    assert result["tab_count"] == 2 and result["active_index"] == 0
    assert await env.session.page() is env.first
    assert env.first.calls == [("bring_to_front",)] and other.calls == []


async def test_switch_asks_for_the_site_of_the_tab_it_goes_to(make_env):
    """An approval for the page the agent is on is no licence over another site's tab."""
    env = await make_env(approved=(APP,))
    other = env.browser.popup(env.first, OTHER, title="Other")
    await env.session.activate(env.first)
    env.first.calls.clear()

    refused = await env.tools["tabs"](action="switch", index=1)

    assert refused["status"] == "needs_approval"
    assert await env.session.page() is env.first, "the agent stayed where it was"
    assert other.calls == [] and env.first.calls == []
    assert any(
        r["status"] == "needs_approval" and r["args"] == {"action": "switch", "index": 1}
        for r in _audit(env)
        if r["tool"] == "tabs"
    ), "and the refusal is in the trail"

    allowed = await env.tools["tabs"](action="switch", index=1, reason="check it", confirm=True)

    assert allowed["status"] == "ok"
    assert await env.session.page() is other
    assert other.calls == [("bring_to_front",)]


async def test_switch_is_judged_by_the_target_not_by_the_page_it_leaves(make_env):
    """Standing on a site nobody approved must not stop the agent going back to one they did."""
    env = await make_env(approved=(APP,))
    env.browser.popup(env.first, OTHER)  # active, and not approved

    result = await env.tools["tabs"](action="switch", index=0)

    assert result["status"] == "ok", "no approval was needed to return to an approved site"
    assert env.first.calls == [("bring_to_front",)]


async def test_switch_reports_a_tab_that_closed_while_the_user_was_asked(make_env, monkeypatch):
    env = await make_env(approved=(APP,))
    other = env.browser.popup(env.first, OTHER)
    await env.session.activate(env.first)
    original_require = tabs_tool.require

    async def require_then_close(*args, **kwargs):
        result = await original_require(*args, **kwargs)
        await other.close()  # the tab goes away during the wait for an answer
        return result

    monkeypatch.setattr(tabs_tool, "require", require_then_close)

    result = await env.tools["tabs"](action="switch", index=1, confirm=True)

    assert result["status"] == "not_found"
    assert await env.session.page() is env.first, "the session did not move onto a dead tab"


# --------------------------------------------------------------------------
# The tool: closing
# --------------------------------------------------------------------------


async def test_close_shuts_the_tab_and_says_where_the_agent_is_now(make_env):
    env = await make_env(approved=(APP, SIGN_IN))
    popup = env.browser.popup(env.first, SIGN_IN, title="Sign in")

    result = await env.tools["tabs"](action="close", index=1)

    assert result["status"] == "ok"
    assert result["closed"] == {"index": 1, "url": SIGN_IN}
    assert result["url"] == APP and result["title"] == "App"
    assert result["tab_count"] == 1 and result["active_index"] == 0
    assert popup.calls == [("close",)] and popup.closed
    assert ("bring_to_front",) in env.first.calls


async def test_close_asks_first_and_a_refusal_touches_no_tab(make_env):
    env = await make_env(approved=(APP,))
    other = env.browser.popup(env.first, OTHER)

    refused = await env.tools["tabs"](action="close", index=1)

    assert refused["status"] == "needs_approval"
    assert other.calls == [] and not other.closed
    assert len(env.browser.pages) == 2


async def test_close_leaves_a_blank_page_when_it_was_the_last_tab(make_env):
    env = await make_env(quits_with_last_window=True)

    result = await env.tools["tabs"](action="close", index=0)

    assert result["status"] == "ok"
    assert result["url"] == "about:blank" and result["tab_count"] == 1
    assert not env.browser.gone and len(env.pw.browsers) == 1
    assert env.browser.events == [("new_page",), ("close", APP)]


async def test_closing_a_background_tab_keeps_the_agent_on_its_own(make_env):
    env = await make_env(approved=(APP, OTHER))
    other = env.browser.popup(env.first, OTHER)

    result = await env.tools["tabs"](action="close", index=0)

    assert result["status"] == "ok" and result["url"] == OTHER
    assert result["tab_count"] == 1 and result["active_index"] == 0
    assert other.calls == [], "the tab the agent is on was not touched"


async def test_the_audit_trail_records_the_tab_and_never_its_url(make_env):
    env = await make_env(approved=(APP, SIGN_IN))
    env.browser.popup(env.first, SIGN_IN)

    await env.tools["tabs"](action="close", index=1)

    rows = [r for r in _audit(env) if r["tool"] == "tabs" and "action" in r["args"]]
    assert [(r["args"], r["status"], r["origin"]) for r in rows] == [
        ({"action": "close", "index": 1}, "ok", "https://idp.example")
    ]
    assert "SECRET-CODE" not in env.ctx.config.audit_path.read_text()


# --------------------------------------------------------------------------
# The tool: gates that apply to every mutation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["switch", "close"])
async def test_a_takeover_stops_switching_and_closing(make_env, action):
    env = await make_env(approved=(APP, OTHER))
    env.browser.popup(env.first, OTHER)
    env.ctx.collab.takeover = True
    env.ctx.collab.takeover_reason = "user is driving"

    result = await env.tools["tabs"](action=action, index=0)

    assert result["status"] == "takeover_active"
    assert all(tab.calls == [] and not tab.closed for tab in env.browser.pages)


@pytest.mark.parametrize("action", ["switch", "close"])
async def test_switching_and_closing_claim_the_browser_and_respect_its_holder(make_env, action):
    env = await make_env(approved=(APP, OTHER))
    env.browser.popup(env.first, OTHER)

    await env.tools["tabs"](action=action, index=0)
    assert env.ctx.owner_session == "default"

    env.ctx.owner_session = "someone-else"
    env.ctx.owner_seen_at = time.monotonic()
    refused = await env.tools["tabs"](action=action, index=0)
    assert refused["status"] == "session_conflict"


@pytest.mark.parametrize(
    ("action", "index", "status"),
    [
        ("frob", 0, "error"),
        ("switch", None, "error"),
        ("close", None, "error"),
        ("switch", 9, "not_found"),
        ("close", 9, "not_found"),
        ("switch", -1, "not_found"),
        ("close", -1, "not_found"),
    ],
)
async def test_a_request_that_cannot_be_carried_out_asks_nothing_and_touches_nothing(
    make_env, action, index, status
):
    """Refused before anyone is asked: no prompt, no scope bought, no tab moved."""
    env = await make_env(approved=())
    other = env.browser.popup(env.first, OTHER)
    grants_before = env.ctx.perms.live_grants("default")

    result = await env.tools["tabs"](action=action, index=index)

    assert result["status"] == status
    assert env.ctx.perms.live_grants("default") == grants_before
    assert not any(r["args"].get("capability") for r in _audit(env)), "nobody was asked"
    assert all(tab.calls == [] and not tab.closed for tab in (env.first, other))


@pytest.mark.parametrize("action", ["switch", "close"])
async def test_a_one_shot_approval_for_a_blank_tab_is_not_left_behind(make_env, action):
    """A blank tab has no site, so the approval to use it is single-use; the tool
    bought it, so the tool gives back what the action did not spend."""
    env = await make_env(approved=(APP,))
    env.ctx.config.scope_release_grace_s = 0
    blank = await env.browser.new_page()
    await env.session.activate(env.first)

    result = await env.tools["tabs"](action=action, index=1, confirm=True, reason="tidy up")

    assert result["status"] == "ok"
    assert (blank.closed) is (action == "close")
    assert not [
        g
        for g in env.ctx.perms.live_grants("default")
        if g.uses_left and g.capability is Capability.INTERACT and g.origin.is_opaque
    ], "no live single-use scope outlives the call that bought it"


def _audit(env) -> list[dict]:
    """Every audit row so far; a log nothing has written to yet is simply empty."""
    path = env.ctx.config.audit_path
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
