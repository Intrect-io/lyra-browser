"""Grants have to be narrow, or an attacker who gets one gets the browser.

The properties worth pinning: opaque origins never earn a lease, effects that
cannot be undone are spent on first use, one session's approval never authorises
another's, and a grant earned from one page does not license a different page to
drive the same destination.
"""

from __future__ import annotations

import pytest

from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability, PermissionStore

SITE = parse_origin("https://example.com/")
OTHER = parse_origin("https://other.example/")
LOCAL = parse_origin("file:///etc/passwd")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(clock: FakeClock) -> PermissionStore:
    return PermissionStore(ttl_s=600.0, clock=clock)


def test_granted_scope_is_covered(store):
    store.grant("s1", SITE, Capability.NAVIGATE)
    assert store.check("s1", SITE, Capability.NAVIGATE)


def test_grant_does_not_leak_across_capabilities(store):
    store.grant("s1", SITE, Capability.NAVIGATE)
    assert not store.check("s1", SITE, Capability.SUBMIT)


def test_grant_does_not_leak_across_origins(store):
    store.grant("s1", SITE, Capability.NAVIGATE)
    assert not store.check("s1", OTHER, Capability.NAVIGATE)


def test_sessions_are_isolated(store):
    """Over HTTP one server serves several clients; approvals must not cross."""
    store.grant("s1", SITE, Capability.NAVIGATE)
    assert not store.check("s2", SITE, Capability.NAVIGATE)


# --------------------------------------------------------------------------
# Opaque origins are never leased — they are approved one request at a time
# --------------------------------------------------------------------------


def test_an_approved_opaque_origin_authorises_the_request_it_approved(store):
    """Approving a local file must actually open it.

    This once asserted the opposite: ``grant`` returned ``None`` for an opaque
    origin, so approval produced nothing and — once enforcement became the
    default — the guard found nothing and refused. The user said yes and the
    browser said no. The rule that matters is "no reusable lease", not "no
    authorisation at all".
    """
    assert store.grant("s1", LOCAL, Capability.NAVIGATE) is not None
    assert store.consume("s1", LOCAL, Capability.NAVIGATE)


def test_opaque_origin_is_asked_again_for_the_next_request(store):
    """One file:// lease would cover every local file, so it is spent on use."""
    store.grant("s1", LOCAL, Capability.NAVIGATE)
    assert store.consume("s1", LOCAL, Capability.NAVIGATE)
    assert not store.consume("s1", LOCAL, Capability.NAVIGATE), "no second ride"
    # Arriving still carries permission to use what you arrived at, opaque or
    # not — but that too is spent on first use rather than leased for ten minutes.
    assert store.consume("s1", LOCAL, Capability.INTERACT)
    assert not store.consume("s1", LOCAL, Capability.INTERACT)


def test_an_unused_opaque_approval_does_not_linger(store):
    """It is released like any other one-shot when the action does not use it."""
    bought = store.grant("s1", LOCAL, Capability.NAVIGATE)
    store.release_unspent([bought])
    assert not store.check("s1", LOCAL, Capability.NAVIGATE)


# --------------------------------------------------------------------------
# Single use vs lease
# --------------------------------------------------------------------------


@pytest.mark.parametrize("capability", [Capability.SUBMIT, Capability.DOWNLOAD])
def test_irreversible_capabilities_are_single_use(store, capability):
    store.grant("s1", SITE, capability)
    assert store.consume("s1", SITE, capability)
    assert not store.consume("s1", SITE, capability)


@pytest.mark.parametrize("capability", [Capability.NAVIGATE, Capability.INTERACT])
def test_reversible_capabilities_are_leased(store, capability):
    store.grant("s1", SITE, capability)
    for _ in range(5):
        assert store.consume("s1", SITE, capability)


def test_check_does_not_spend_a_single_use_grant(store):
    store.grant("s1", SITE, Capability.SUBMIT)
    assert store.check("s1", SITE, Capability.SUBMIT)
    assert store.check("s1", SITE, Capability.SUBMIT)
    assert store.consume("s1", SITE, Capability.SUBMIT)


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


def test_grant_expires(store, clock):
    store.grant("s1", SITE, Capability.NAVIGATE)
    clock.advance(599)
    assert store.check("s1", SITE, Capability.NAVIGATE)
    clock.advance(2)
    assert not store.check("s1", SITE, Capability.NAVIGATE)


def test_revoke_all_clears_a_session(store):
    store.grant("s1", SITE, Capability.NAVIGATE)
    store.grant("s2", SITE, Capability.NAVIGATE)
    store.revoke_all("s1")
    assert not store.check("s1", SITE, Capability.NAVIGATE)
    assert store.check("s2", SITE, Capability.NAVIGATE)


# --------------------------------------------------------------------------
# The initiator constraint — this is what stops one approved visit from
# becoming an open door for every hostile page afterwards
# --------------------------------------------------------------------------


def test_grant_bound_to_an_initiator_rejects_another_one(store):
    store.grant("s1", SITE, Capability.NAVIGATE, initiator=OTHER)
    evil = parse_origin("https://evil.example/")
    assert not store.check("s1", SITE, Capability.NAVIGATE, initiator=evil)
    assert store.check("s1", SITE, Capability.NAVIGATE, initiator=OTHER)


def test_unconstrained_grant_accepts_any_initiator(store):
    store.grant("s1", SITE, Capability.NAVIGATE, initiator=None)
    assert store.check("s1", SITE, Capability.NAVIGATE, initiator=OTHER)
    assert store.check("s1", SITE, Capability.NAVIGATE, initiator=None)


def test_constrained_grant_rejects_a_missing_initiator(store):
    store.grant("s1", SITE, Capability.NAVIGATE, initiator=OTHER)
    assert not store.check("s1", SITE, Capability.NAVIGATE, initiator=None)


def test_navigate_implies_interact_on_the_same_origin(store):
    """Arriving somewhere carries permission to use it — asking for every click
    would train the user to approve without reading."""
    store.grant("s1", SITE, Capability.NAVIGATE)
    assert store.check("s1", SITE, Capability.INTERACT)
    # But not the parts that matter.
    assert not store.check("s1", SITE, Capability.SUBMIT)


def test_implied_interact_is_not_bound_to_the_initiator(store):
    """The point is that being on a site lets you use it, whoever sent you."""
    store.grant("s1", SITE, Capability.NAVIGATE, initiator=OTHER)
    assert store.check("s1", SITE, Capability.INTERACT, initiator=parse_origin("https://x.test/"))


def test_live_grants_reports_what_is_held(store, clock):
    store.grant("s1", SITE, Capability.NAVIGATE)
    assert len(store.live_grants("s1")) == 2  # NAVIGATE + the INTERACT it implies
    clock.advance(601)
    assert store.live_grants("s1") == []


def test_interacting_with_a_local_file_is_asked_again_each_time(store):
    """On an opaque origin the implied INTERACT is one-shot too, deliberately.

    Every ``file:`` URL is the same origin, so a reusable INTERACT there would
    cover clicking on any local document the agent can reach. On an ordinary
    site the same implication is a lease and clicking is never re-asked — the
    cost of asking falls only where the origin cannot be told apart.
    """
    store.grant("s1", LOCAL, Capability.NAVIGATE)
    implied = store.find_live("s1", LOCAL, Capability.INTERACT)
    assert implied is not None and implied.uses_left == 1

    store.release_unspent([implied])
    assert not store.check("s1", LOCAL, Capability.INTERACT)

    store.grant("s1", SITE, Capability.NAVIGATE)
    lease = store.find_live("s1", SITE, Capability.INTERACT)
    assert lease.uses_left is None, "an ordinary site keeps its lease"
    store.release_unspent([lease])
    assert store.check("s1", SITE, Capability.INTERACT)
