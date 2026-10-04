"""An HTTP redirect hop is a navigation like any other.

Playwright continues every request that has a ``redirectedFrom`` before any route
handler is asked, so the route judges the first request of a chain and nothing after
it. A site the user approved could therefore send the tab anywhere with no judgement
and no audit row. The guard now also listens to ``context.on("request")``, where each
hop is announced — after it has been sent — and judges it like a first request: same
``classify``, same grants, same spending of single-use ones, same audit row (plus
``redirect_from``), and in enforce mode the load is cancelled.

The doubles are shaped like the objects Playwright hands over: a hop has a
``redirected_from`` request, and its frame belongs to a page whose context can open a
CDP session.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lyra_browser.audit import AuditLog
from lyra_browser.config import Config
from lyra_browser.enforcement import NavigationGuard, classify
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability, PermissionStore

PAGE = "https://page.example/start"  # where the tab stands when the chain begins
A = "https://a.example/go"  # the approved server that answers with a redirect
B = "https://b.example/landing"  # where it sends the tab
PAGE_ORIGIN = parse_origin(PAGE)
B_ORIGIN = parse_origin(B)


class FakeCdp:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.detached = False

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append(method)
        return {}

    async def detach(self) -> None:
        self.detached = True


class FakeContext:
    def __init__(self, error: Exception | None = None) -> None:
        self.cdp = FakeCdp()
        self.error = error
        self.opened = 0

    async def new_cdp_session(self, page: object) -> FakeCdp:
        self.opened += 1
        if self.error is not None:
            raise self.error
        return self.cdp


class FakePage:
    def __init__(
        self,
        url: str = PAGE,
        error: Exception | None = None,
        goto_error: Exception | None = None,
    ) -> None:
        self.url = url
        self.context = FakeContext(error)
        self.visited: list[str] = []
        self._goto_error = goto_error

    async def goto(self, url: str, **kwargs: object) -> None:
        self.visited.append(url)
        if self._goto_error is not None:
            raise self._goto_error
        self.url = url


class FakeFrame:
    def __init__(self, page: FakePage, url: str, parent: FakeFrame | None = None) -> None:
        self.page = page
        self.url = url
        self.parent_frame = parent


class FakeHop:
    """A request that something redirected here, as Playwright reports it."""

    def __init__(
        self,
        url: str = B,
        source: str | None = A,
        *,
        method: str = "GET",
        post_data: str | None = None,
        navigation: bool = True,
        page: FakePage | None = None,
        frame: FakeFrame | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.post_data = post_data
        self.redirected_from = SimpleNamespace(url=source) if source is not None else None
        self._navigation = navigation
        self.page = page or FakePage()
        self.frame = frame or FakeFrame(self.page, self.page.url)

    def is_navigation_request(self) -> bool:
        return self._navigation


@pytest.fixture
def make_guard(tmp_path):
    def _make(mode="enforce", collab=None):
        cfg = Config()
        cfg.data_dir = tmp_path
        cfg.__post_init__()
        perms = PermissionStore(ttl_s=600.0)
        guard = NavigationGuard(cfg, perms, AuditLog(cfg.audit_path), mode=mode, collab=collab)
        return guard, perms, cfg

    return _make


def rows(cfg) -> list[dict]:
    if not cfg.audit_path.exists():
        return []
    return [json.loads(x) for x in cfg.audit_path.read_text().splitlines() if x.strip()]


def navigation_rows(cfg) -> list[dict]:
    return [r for r in rows(cfg) if r["tool"] == "navigation"]


# --------------------------------------------------------------------------
# classify: a hop is judged like a first request to where it leads
# --------------------------------------------------------------------------


def test_a_hop_to_another_origin_is_judged_like_a_first_request_to_it():
    hop = FakeHop()

    intent = classify(hop)

    assert intent is not None
    assert intent.capability is Capability.NAVIGATE
    assert intent.target == B_ORIGIN
    assert intent.initiator == PAGE_ORIGIN, (
        "the page that was on screen, not the redirecting server"
    )
    assert intent.redirect_from == A


def test_a_first_request_has_no_redirect_source():
    assert classify(FakeHop(source=None)).redirect_from == ""


def test_a_hop_that_stays_on_the_origin_that_issued_it_is_not_judged():
    """Approving the first request covered the origin; ``/old`` to ``/new`` is not an exit."""
    assert classify(FakeHop(url="https://a.example/elsewhere")) is None


def test_the_same_host_upgraded_to_https_is_not_judged():
    hop = FakeHop(url="https://a.example/x", source="http://a.example/x")

    assert classify(hop) is None


@pytest.mark.parametrize(
    ("source", "url"),
    [
        ("https://a.example/", "http://a.example/"),  # a downgrade is leaving the secure site
        ("http://a.example:8080/", "https://a.example/"),  # another port is another server
        ("http://a.example/", "https://a.example:8443/"),
        ("http://a.example/", "https://www.a.example/"),  # another host
    ],
)
def test_anything_but_a_plain_upgrade_is_somewhere_new(source, url):
    assert classify(FakeHop(url=url, source=source)) is not None


def test_a_hop_to_a_document_with_no_site_is_judged():
    """``data:`` and friends are never "staying put", whoever sent the tab there."""
    intent = classify(FakeHop(url="data:text/html,<p>x</p>"))

    assert intent is not None and intent.target.is_opaque


@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_a_hop_that_repeats_a_submission_is_a_submission(method):
    """A 307/308 re-sends the body; the sender, not the new destination, is judged."""
    intent = classify(FakeHop(method=method, post_data="a=1"))

    assert intent.capability is Capability.SUBMIT
    assert intent.subject == PAGE_ORIGIN
    assert intent.redirect_from == A


def test_a_302_that_turned_a_post_into_a_get_is_a_plain_navigation():
    assert classify(FakeHop(method="GET", post_data=None)).capability is Capability.NAVIGATE


def test_a_hop_inside_an_embedded_frame_is_the_page_building_itself():
    page = FakePage()
    hop = FakeHop(page=page, frame=FakeFrame(page, "about:blank", parent=FakeFrame(page, PAGE)))

    assert classify(hop) is None


def test_a_hop_of_a_subresource_is_not_a_navigation():
    assert classify(FakeHop(navigation=False)) is None


# --------------------------------------------------------------------------
# the listener: judged after it left, refused by stopping the load
# --------------------------------------------------------------------------


async def test_enforce_refuses_an_unapproved_hop_and_stops_the_load(make_guard):
    guard, _, cfg = make_guard("enforce")
    hop = FakeHop()

    guard.on_request(hop)
    await guard.settled()

    assert guard.refusals == 1, "the tool that started the load reads this to say blocked_by_policy"
    assert hop.page.context.cdp.sent == ["Page.stopLoading"]
    assert hop.page.context.cdp.detached
    assert hop.page.visited == [], "a stop that landed leaves the document standing"
    judged, stopped = navigation_rows(cfg)
    assert judged["status"] == "denied"
    assert judged["args"]["redirect_from"] == A
    assert judged["args"]["capability"] == "navigate"
    assert judged["origin"] == B_ORIGIN.describe()
    assert judged["initiator"] == PAGE_ORIGIN.describe()
    assert stopped["status"] == "stopped"
    assert stopped["args"]["redirect_from"] == A


async def test_enforce_lets_an_approved_hop_pass_and_leaves_the_load_alone(make_guard):
    guard, perms, cfg = make_guard("enforce")
    perms.grant("default", B_ORIGIN, Capability.NAVIGATE, initiator=PAGE_ORIGIN)
    hop = FakeHop()

    guard.on_request(hop)
    await guard.settled()

    assert guard.refusals == 0
    assert hop.page.context.opened == 0, "nothing to stop, so no session was opened"
    [row] = navigation_rows(cfg)
    assert row["status"] == "allowed" and row["args"]["redirect_from"] == A


async def test_a_grant_earned_from_another_page_does_not_cover_a_hop(make_guard):
    """The same initiator rule as for a first request: a hop does not widen it."""
    guard, perms, _ = make_guard("enforce")
    perms.grant(
        "default", B_ORIGIN, Capability.NAVIGATE, initiator=parse_origin("https://x.example")
    )

    guard.on_request(FakeHop())
    await guard.settled()

    assert guard.refusals == 1


async def test_observe_records_what_it_would_have_refused_and_stops_nothing(make_guard):
    guard, _, cfg = make_guard("observe")
    hop = FakeHop()

    guard.on_request(hop)
    await guard.settled()

    assert guard.refusals == 0
    assert hop.page.context.opened == 0
    [row] = navigation_rows(cfg)
    assert row["status"] == "would_deny" and row["args"]["redirect_from"] == A


async def test_a_takeover_drives_its_own_redirects(make_guard):
    from lyra_browser.approval import CollaborationState

    collab = CollaborationState(require_approval=True)
    collab.takeover = True
    guard, _, cfg = make_guard("enforce", collab=collab)
    hop = FakeHop()

    guard.on_request(hop)
    await guard.settled()

    assert guard.refusals == 0 and hop.page.context.opened == 0
    [row] = navigation_rows(cfg)
    assert row["status"] == "user_driven" and row["args"]["redirect_from"] == A


async def test_a_hop_spends_the_single_use_grant_it_rides_in_enforce_not_in_observe(make_guard):
    for mode, spent in (("enforce", True), ("observe", False)):
        guard, perms, _ = make_guard(mode)
        perms.grant("default", PAGE_ORIGIN, Capability.SUBMIT, initiator=PAGE_ORIGIN)

        guard.on_request(FakeHop(method="POST", post_data="a=1"))
        await guard.settled()

        covered = perms.check("default", PAGE_ORIGIN, Capability.SUBMIT, PAGE_ORIGIN)
        assert covered is not spent, mode


async def test_a_second_send_of_one_approved_submission_needs_its_own_approval(make_guard):
    """One approval, one body. The 307 that repeats it elsewhere is a second send."""
    guard, perms, _ = make_guard("enforce")
    perms.grant("default", PAGE_ORIGIN, Capability.SUBMIT, initiator=PAGE_ORIGIN)
    first, hop = (
        FakeHop(source=None, method="POST", post_data="a=1"),
        FakeHop(method="POST", post_data="a=1"),
    )

    guard.on_request(first)  # the route's job, not the listener's: ignored here
    guard.on_request(hop)
    guard.on_request(FakeHop(method="POST", post_data="a=1"))
    await guard.settled()

    assert guard.refusals == 1, "the first hop spent the grant, the second had none"


async def test_a_hop_that_stays_on_its_origin_is_neither_recorded_nor_stopped(make_guard):
    guard, _, cfg = make_guard("enforce")
    hop = FakeHop(url="https://a.example/elsewhere")

    guard.on_request(hop)
    await guard.settled()

    assert guard.refusals == 0 and hop.page.context.opened == 0
    assert navigation_rows(cfg) == []


async def test_a_first_request_is_the_routes_to_judge_not_the_listeners(make_guard):
    """Otherwise every navigation would be judged twice, and a one-shot grant spent twice."""
    guard, _, cfg = make_guard("enforce")

    guard.on_request(FakeHop(source=None))
    await guard.settled()

    assert guard.refusals == 0 and navigation_rows(cfg) == []


async def test_a_subresource_redirect_is_ignored(make_guard):
    guard, _, cfg = make_guard("enforce")

    guard.on_request(FakeHop(navigation=False))
    await guard.settled()

    assert guard.refusals == 0 and navigation_rows(cfg) == []


# --------------------------------------------------------------------------
# stopping can fail, and the audit says so instead of claiming a refusal
# --------------------------------------------------------------------------


async def test_a_stop_that_cannot_be_sent_blanks_the_tab_instead(make_guard):
    """No stop means the load may still commit; a new navigation supersedes it."""
    guard, _, cfg = make_guard("enforce")
    hop = FakeHop(page=FakePage(error=RuntimeError("Not attached to an active page")))

    guard.on_request(hop)
    await guard.settled()

    assert guard.refusals == 1, "it is still a refusal; it just did not land in time"
    assert hop.page.visited == ["about:blank"]
    _, outcome = navigation_rows(cfg)
    assert outcome["status"] == "not_stopped"
    assert outcome["args"]["reason"] == "the stop failed (RuntimeError); the tab was blanked"


async def test_a_page_that_took_the_hop_before_the_stop_is_blanked(make_guard):
    """A server that answers before the stop lands (loopback) commits the page first, and
    the agent must not be left reading an origin that was refused."""
    guard, _, cfg = make_guard("enforce")
    page = FakePage()
    hop = FakeHop(page=page)
    page.url = B  # the commit happened while the stop was in flight

    guard.on_request(hop)
    await guard.settled()

    assert page.visited == ["about:blank"]
    _, outcome = navigation_rows(cfg)
    assert outcome["status"] == "not_stopped"
    assert outcome["args"]["reason"] == "the page had already taken it; the tab was blanked"


async def test_a_fragment_does_not_hide_that_the_page_took_the_hop(make_guard):
    guard, _, cfg = make_guard("enforce")
    page = FakePage()
    hop = FakeHop(page=page)
    page.url = B + "#section"

    guard.on_request(hop)
    await guard.settled()

    assert page.visited == ["about:blank"]


async def test_a_tab_that_will_not_blank_is_recorded_not_raised(make_guard):
    guard, _, cfg = make_guard("enforce")
    page = FakePage(goto_error=RuntimeError("Target closed"))
    page.url = B
    hop = FakeHop(page=page)

    guard.on_request(hop)
    await guard.settled()

    _, outcome = navigation_rows(cfg)
    assert outcome["status"] == "not_stopped"
    assert outcome["args"]["reason"].endswith("blanking it failed (RuntimeError)")


async def test_a_subframe_hop_is_stopped_and_the_page_is_not_blanked(make_guard):
    """A frame's commit cannot be read off ``page.url``, and the page is not the frame's to lose."""
    guard, _, cfg = make_guard("enforce")
    page = FakePage()
    hop = FakeHop(
        method="POST",
        post_data="a=1",
        page=page,
        frame=FakeFrame(page, "about:blank", parent=FakeFrame(page, PAGE)),
    )
    page.url = B

    guard.on_request(hop)
    await guard.settled()

    _, outcome = navigation_rows(cfg)
    assert outcome["status"] == "stopped"
    assert page.visited == []


async def test_a_subframe_stop_that_fails_leaves_the_page_alone_and_says_so(make_guard):
    guard, _, cfg = make_guard("enforce")
    page = FakePage(error=RuntimeError("gone"))
    hop = FakeHop(
        method="POST",
        post_data="a=1",
        page=page,
        frame=FakeFrame(page, "about:blank", parent=FakeFrame(page, PAGE)),
    )

    guard.on_request(hop)
    await guard.settled()

    _, outcome = navigation_rows(cfg)
    assert outcome["status"] == "not_stopped"
    assert outcome["args"]["reason"] == "the stop failed (RuntimeError)"
    assert page.visited == []


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        ("Page.goto: net::ERR_ABORTED at http://x/", True),
        ('Navigation to "http://b/" is interrupted by another navigation to "about:blank"', True),
        ("net::ERR_NAME_NOT_RESOLVED at http://nope.invalid/", False),
    ],
)
def test_a_navigation_the_guard_interrupted_reads_as_refused(text, refused):
    from lyra_browser.enforcement import refused_by_guard

    assert refused_by_guard(RuntimeError(text)) is refused


async def test_a_broken_guard_stops_the_load_in_enforce_and_leaves_it_in_observe(
    make_guard, monkeypatch
):
    def boom(request):
        raise RuntimeError("guard bug")

    monkeypatch.setattr("lyra_browser.enforcement.classify", boom)
    for mode, stops in (("enforce", True), ("observe", False)):
        guard, _, cfg = make_guard(mode)
        hop = FakeHop()

        guard.on_request(hop)  # must not raise: an escaping exception surfaces elsewhere
        await guard.settled()

        assert (hop.page.context.opened == 1) is stops, mode
        assert navigation_rows(cfg)[0]["status"] == "guard_error"
        assert guard.refusals == 0, "not a refusal: no approval would have helped"


def test_a_refused_hop_heard_with_no_event_loop_is_audited_not_raised(make_guard):
    """The listener runs inside the driver's dispatch loop; raising there takes it down."""
    guard, _, cfg = make_guard("enforce")

    guard.on_request(FakeHop())  # refused, and nothing can run the stop

    assert guard.refusals == 1
    assert [r["status"] for r in navigation_rows(cfg)] == ["denied", "guard_error"]


def test_a_request_that_cannot_say_whether_it_was_redirected_is_ignored(make_guard):
    """An event handler that raises takes the driver's dispatch loop with it."""
    guard, _, cfg = make_guard("enforce")

    class Unreadable:
        @property
        def redirected_from(self):
            raise RuntimeError("disposed")

    guard.on_request(Unreadable())  # must not raise

    assert guard.refusals == 0 and navigation_rows(cfg) == []


# --------------------------------------------------------------------------
# the agent asked for one URL and was turned away from another: say which
# --------------------------------------------------------------------------


def test_the_envelope_names_where_a_refused_redirect_led():
    from lyra_browser.enforcement import refused_envelope

    plain = refused_envelope("https://page.example/start")
    hop = refused_envelope("https://page.example/start", redirected_to="https://b.example")

    assert "redirected_to" not in plain and "redirected" not in plain["reason"]
    assert hop["redirected_to"] == "https://b.example"
    assert "https://b.example" in hop["reason"]
    assert hop["status"] == plain["status"] == "blocked_by_policy"
    assert hop["url"] == "https://page.example/start"


async def test_a_refusal_remembers_whether_it_was_a_redirect(make_guard):
    guard, _, _ = make_guard("enforce")
    assert guard.refused_hop == ""

    guard.on_request(FakeHop())
    await guard.settled()
    assert guard.refused_hop == B_ORIGIN.describe()

    class FirstRequest:  # the route's kind of refusal: nothing redirected it
        url = B
        method = "GET"
        post_data = None
        redirected_from = None

        def is_navigation_request(self) -> bool:
            return True

        frame = FakeFrame(FakePage(), PAGE)

    class Route:
        async def fulfill(self, **kwargs: object) -> None:
            pass

    await guard(Route(), FirstRequest())
    assert guard.refusals == 2
    assert guard.refused_hop == "", "an earlier redirect must not be blamed for this refusal"


@pytest.mark.parametrize(
    ("tool", "method", "args"),
    [
        ("navigate", "goto", {"url": "https://start.example/next", "confirm": True}),
        ("go_back", "go_back", {"confirm": True}),
        ("reload_page", "reload", {"confirm": True}),
    ],
)
async def test_a_navigation_turned_away_at_a_redirect_says_where_it_was_sent(
    make_ctx, tools_of, page, tool, method, args
):
    ctx = make_ctx()
    tools = await tools_of(ctx)

    async def redirected(*_args, **_kwargs):
        ctx.guard.on_request(FakeHop())  # the site answered with a redirect nobody approved
        raise RuntimeError("Page.goto: net::ERR_ABORTED")

    setattr(page, method, redirected)

    result = await tools[tool](**args)
    await ctx.guard.settled()

    assert result["status"] == "blocked_by_policy"
    assert result["redirected_to"] == B_ORIGIN.describe()
    assert result["url"] == page.url, "and where the tab still stands"


@pytest.mark.parametrize(
    ("tool", "method", "args"),
    [
        ("navigate", "goto", {"url": "https://start.example/next", "confirm": True}),
        ("go_back", "go_back", {"confirm": True}),
        ("reload_page", "reload", {"confirm": True}),
    ],
)
async def test_a_load_that_finished_before_the_stop_is_still_answered_as_refused(
    make_ctx, tools_of, page, tool, method, args
):
    """Loopback answers before any stop can land: the driver then reports success for a
    navigation the guard refused. The tool must not repeat that, and must wait for the tab
    to be blanked so that what it reports is where the tab will stay."""
    ctx = make_ctx()
    tools = await tools_of(ctx)
    tab = FakePage()
    tab.url = B  # the refused page committed before the stop landed

    async def finishes(*_args, **_kwargs):
        ctx.guard.on_request(FakeHop(page=tab))
        return None  # the driver's answer: the load completed

    setattr(page, method, finishes)

    result = await tools[tool](**args)

    assert result["status"] == "blocked_by_policy"
    assert result["redirected_to"] == B_ORIGIN.describe()
    assert tab.visited == ["about:blank"], "answered only once the refused page was gone"
    assert [r for r in rows(ctx.config) if r["tool"] == tool][-1]["status"] == "blocked_by_policy"


async def test_a_refusal_that_was_not_a_redirect_does_not_turn_a_finished_load_into_a_block(
    make_ctx, tools_of, page
):
    """The guard counts every refusal; only a redirect refused during the call blames it."""
    ctx = make_ctx()
    tools = await tools_of(ctx)

    async def finishes(url, wait_until=None):
        ctx.guard.refusals += (
            1  # a popup the page opened, say: refused, and nothing to do with this load
        )
        page.url = url

    page.goto = finishes

    result = await tools["navigate"](url="https://start.example/next", confirm=True)

    assert result["status"] == "ok"
