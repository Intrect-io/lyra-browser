"""The guard judges requests, not intentions — and it never asks anyone.

Route handlers run on a task created when the browser first opened, so they
carry that moment's contextvars forever. Anything here that tried to reach the
user would be addressing a request that finished long ago. This layer only
compares what is leaving against what was granted.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lyra_browser.audit import AuditLog
from lyra_browser.config import Config
from lyra_browser.enforcement import NavigationGuard, classify, frame_origin
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability, PermissionStore

SITE = parse_origin("https://example.com/")


class FakeFrame:
    def __init__(self, url: str = "https://example.com/") -> None:
        self.url = url


class FakeRequest:
    def __init__(
        self,
        url: str = "https://example.com/next",
        method: str = "GET",
        navigation: bool = True,
        post_data: str | None = None,
        frame_url: str | None = "https://example.com/",
        frame_raises: bool = False,
    ) -> None:
        self.url = url
        self.method = method
        self.post_data = post_data
        self._navigation = navigation
        self._frame_url = frame_url
        self._frame_raises = frame_raises

    def is_navigation_request(self) -> bool:
        return self._navigation

    @property
    def frame(self) -> FakeFrame:
        if self._frame_raises:
            # Playwright really does raise here for some navigation requests.
            raise RuntimeError("Frame not available for this request")
        return FakeFrame(self._frame_url or "")


class FakeRoute:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def continue_(self) -> None:
        self.calls.append(("continue",))

    async def fulfill(self, **kwargs) -> None:
        self.calls.append(("fulfill", kwargs.get("status")))

    async def abort(self, *a, **k) -> None:  # must never be used
        self.calls.append(("abort",))


@pytest.fixture
def make_guard(tmp_path):
    def _make(mode="observe", ttl_s=600.0):
        cfg = Config()
        cfg.data_dir = tmp_path
        cfg.__post_init__()
        perms = PermissionStore(ttl_s=ttl_s)
        audit = AuditLog(cfg.audit_path)
        return NavigationGuard(cfg, perms, audit, mode=mode), perms, cfg

    return _make


def _entries(cfg):
    return [json.loads(x) for x in cfg.audit_path.read_text().splitlines() if x.strip()]


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------


def test_subresources_are_not_judged():
    """XHR and images cannot move the user; gating them would break every site."""
    assert classify(FakeRequest(navigation=False)) is None


def test_moving_within_a_site_is_interact():
    """Being on a site includes moving around it; that is not a fresh decision."""
    intent = classify(FakeRequest(url="https://example.com/x", frame_url="https://example.com/"))
    assert intent.capability is Capability.INTERACT
    assert intent.target == parse_origin("https://example.com/x")


def test_leaving_a_site_is_navigate():
    intent = classify(FakeRequest(url="https://other.example/x", frame_url="https://example.com/"))
    assert intent.capability is Capability.NAVIGATE


def test_an_unknown_initiator_counts_as_leaving():
    """Opaque on either side is never 'staying put', so it is judged as a move."""
    intent = classify(FakeRequest(url="https://example.com/x", frame_raises=True))
    assert intent.capability is Capability.NAVIGATE


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_mutating_methods_are_submit(method):
    assert classify(FakeRequest(method=method)).capability is Capability.SUBMIT


def test_navigation_carrying_a_body_is_submit():
    """A body makes it a submission whatever the method claims."""
    assert classify(FakeRequest(method="GET", post_data="q=x")).capability is Capability.SUBMIT


def test_initiator_is_taken_from_the_frame():
    intent = classify(FakeRequest(frame_url="https://origin.example/page"))
    assert intent.initiator == parse_origin("https://origin.example/")


def test_unreadable_frame_yields_an_opaque_initiator():
    """Playwright raises on .frame for some navigations; that must not escape."""
    assert frame_origin(FakeRequest(frame_raises=True)).is_opaque
    assert classify(FakeRequest(frame_raises=True)).initiator.is_opaque


# --------------------------------------------------------------------------
# observe mode — sees everything, blocks nothing
# --------------------------------------------------------------------------


async def test_observe_lets_an_ungranted_navigation_through(make_guard):
    guard, _, cfg = make_guard("observe")
    route = FakeRoute()

    await guard(route, FakeRequest(url="https://other.example/"))

    assert route.calls == [("continue",)]
    assert _entries(cfg)[-1]["status"] == "would_deny"


async def test_observe_does_not_spend_a_single_use_grant(make_guard):
    """Observing must not quietly consume an approval the user paid for."""
    guard, perms, _ = make_guard("observe")
    perms.grant("default", SITE, Capability.SUBMIT, initiator=SITE)

    await guard(FakeRoute(), FakeRequest(method="POST"))

    assert perms.check("default", SITE, Capability.SUBMIT, initiator=SITE)


# --------------------------------------------------------------------------
# enforce mode
# --------------------------------------------------------------------------


async def test_enforce_refuses_with_204_not_abort(make_guard):
    """abort() sends the page to chrome-error:// and the DOM disappears."""
    guard, _, cfg = make_guard("enforce")
    route = FakeRoute()

    await guard(route, FakeRequest(url="https://other.example/"))

    assert route.calls == [("fulfill", 204)]
    assert _entries(cfg)[-1]["status"] == "denied"


async def test_enforce_allows_a_granted_navigation(make_guard):
    guard, perms, _ = make_guard("enforce")
    # NAVIGATE implies INTERACT on the same origin, which is what same-site
    # traffic is classified as.
    perms.grant("default", SITE, Capability.NAVIGATE, initiator=SITE)
    route = FakeRoute()

    await guard(route, FakeRequest(url="https://example.com/next"))

    assert route.calls == [("continue",)]


async def test_enforce_spends_a_single_use_grant(make_guard):
    guard, perms, _ = make_guard("enforce")
    perms.grant("default", SITE, Capability.SUBMIT, initiator=SITE)

    await guard(FakeRoute(), FakeRequest(method="POST"))

    assert not perms.check("default", SITE, Capability.SUBMIT, initiator=SITE)


async def test_enforce_lets_subresources_through_ungranted(make_guard):
    guard, _, _ = make_guard("enforce")
    route = FakeRoute()

    await guard(route, FakeRequest(navigation=False))

    assert route.calls == [("continue",)]


async def test_grant_from_one_page_does_not_license_another(make_guard):
    """The initiator constraint, seen from the enforcement side."""
    guard, perms, _ = make_guard("enforce")
    perms.grant("default", SITE, Capability.NAVIGATE, initiator=parse_origin("https://a.example/"))
    route = FakeRoute()

    await guard(route, FakeRequest(url="https://example.com/x", frame_url="https://evil.example/"))

    assert route.calls == [("fulfill", 204)]


# --------------------------------------------------------------------------
# A broken guard must not open the gate
# --------------------------------------------------------------------------


class ExplodingRequest(FakeRequest):
    @property
    def frame(self):
        raise RuntimeError("boom")

    def is_navigation_request(self):
        return True


async def test_guard_failure_fails_closed_in_enforce(make_guard, monkeypatch):
    guard, _, cfg = make_guard("enforce")
    monkeypatch.setattr(
        "lyra_browser.enforcement.classify",
        lambda request: (_ for _ in ()).throw(RuntimeError("guard bug")),
    )
    route = FakeRoute()

    await guard(route, FakeRequest())

    assert route.calls == [("fulfill", 204)]
    assert _entries(cfg)[-1]["status"] == "guard_error"


async def test_guard_never_raises_out_of_the_handler(make_guard, monkeypatch):
    """An escaping exception surfaces on an unrelated Playwright call later."""
    guard, _, _ = make_guard("enforce")
    monkeypatch.setattr(
        "lyra_browser.enforcement.classify",
        lambda request: (_ for _ in ()).throw(RuntimeError("guard bug")),
    )

    class BrokenRoute(FakeRoute):
        async def fulfill(self, **kwargs):
            raise RuntimeError("route already handled")

    await guard(BrokenRoute(), FakeRequest())  # must not raise


# --------------------------------------------------------------------------
# The audit trail must not become the leak
# --------------------------------------------------------------------------


def test_query_values_are_stripped_from_logged_urls():
    from lyra_browser.enforcement import loggable_url

    got = loggable_url("https://example.com/submitted?q=hunter2&user=bob")
    assert "hunter2" not in got
    assert "bob" not in got
    assert "q" in got and "user" in got  # names stay: they make the entry readable
    assert got.startswith("https://example.com/submitted")


def test_url_without_query_is_unchanged():
    from lyra_browser.enforcement import loggable_url

    assert loggable_url("https://example.com/page") == "https://example.com/page"


async def test_guard_does_not_log_submitted_form_values(make_guard):
    guard, _, cfg = make_guard("observe")

    await guard(FakeRoute(), FakeRequest(url="https://example.com/s?password=hunter2"))

    assert "hunter2" not in cfg.audit_path.read_text()


# --------------------------------------------------------------------------
# Embedding is not moving. A real-site run caught this: a cross-origin iframe
# was judged as an escape, which would have refused every embedded map, video
# and ad on a site the user had already approved.
# --------------------------------------------------------------------------


class SubframeRequest(FakeRequest):
    """A request belonging to an iframe, whose own url is still about:blank."""

    def __init__(self, parent_url="https://example.com/", **kw):
        super().__init__(**kw)
        self._parent_url = parent_url

    @property
    def frame(self):
        outer = self

        class _Parent:
            url = outer._parent_url
            parent_frame = None

        class _Frame:
            url = "about:blank"
            parent_frame = _Parent()

        return _Frame()


def test_embedded_document_is_not_judged():
    assert classify(SubframeRequest(url="https://cdn.other/widget")) is None


def test_form_posted_from_an_iframe_is_still_a_submission():
    intent = classify(SubframeRequest(url="https://other.example/x", method="POST"))
    assert intent is not None
    assert intent.capability is Capability.SUBMIT


def test_iframe_initiator_falls_back_to_the_embedding_page():
    """A frame that has not navigated reports about:blank; attribute it to the parent."""
    got = frame_origin(SubframeRequest(parent_url="https://host.example/page"))
    assert got == parse_origin("https://host.example/")


def test_main_frame_navigation_is_still_judged():
    assert classify(FakeRequest(url="https://other.example/")) is not None


async def test_enforce_lets_an_embedded_document_through(make_guard):
    guard, _, _ = make_guard("enforce")
    route = FakeRoute()

    await guard(route, SubframeRequest(url="https://cdn.other/widget"))

    assert route.calls == [("continue",)]


# --------------------------------------------------------------------------
# One judgement, two adapters
#
# ``decide`` is the judgement; the Playwright route (``__call__``) and the CDP sidecar
# both carry its verdict out. These pin that nothing but the verdict differs.
# --------------------------------------------------------------------------


def _audit_rows(cfg):
    path = cfg.audit_path
    return (
        [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    )


@pytest.mark.parametrize("mode", ["enforce", "observe"])
@pytest.mark.parametrize("granted", [True, False])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_the_route_and_decide_reach_the_same_verdict_and_leave_the_same_trail(
    make_guard, mode, granted, method
):
    outcomes = []
    for via in ("route", "decide"):
        guard, perms, cfg = make_guard(mode)
        cfg.audit_path.unlink(missing_ok=True)
        if granted:
            perms.grant(
                "default", SITE, Capability.SUBMIT if method == "POST" else Capability.NAVIGATE
            )
            perms.grant("default", parse_origin("https://other.example/"), Capability.NAVIGATE)
        request = FakeRequest(
            url="https://other.example/x",
            method=method,
            post_data="a=1" if method == "POST" else None,
        )
        if via == "route":
            route = FakeRoute()
            await guard(route, request)
            allowed = route.calls == [("continue",)]
            assert route.calls in ([("continue",)], [("fulfill", 204)])
        else:
            allowed = guard.decide(request)
        rows = [
            (r["status"], r["args"].get("capability"), r["origin"], r["initiator"])
            for r in _audit_rows(cfg)
        ]
        outcomes.append((allowed, guard.refusals, rows))

    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize(("mode", "lets_through"), [("enforce", False), ("observe", True)])
def test_a_request_the_guard_could_not_judge_is_refused_unless_only_observing(
    make_guard, mode, lets_through
):
    guard, _, cfg = make_guard(mode)

    verdict = guard.verdict_on_error(ValueError("boom"))

    assert verdict is lets_through
    assert guard.refusals == 0, "a broken guard is not a refusal the agent can fix by asking"
    assert [(r["status"], r["detail"]) for r in _audit_rows(cfg)] == [("guard_error", "ValueError")]


def test_a_failure_of_the_interception_itself_goes_on_the_trail(make_guard):
    guard, _, cfg = make_guard("enforce")

    guard.record_failure("guard_lost", "connection closed")

    assert [(r["tool"], r["status"], r["detail"]) for r in _audit_rows(cfg)] == [
        ("navigation", "guard_lost", "connection closed")
    ]


# --------------------------------------------------------------------------
# Redirect hops: a hop is a navigation of its own, unless it stays where it came from
# --------------------------------------------------------------------------


def _hop(url: str, came_from: str, **kwargs) -> FakeRequest:
    request = FakeRequest(url=url, **kwargs)
    request.redirected_from = SimpleNamespace(url=came_from)
    return request


@pytest.mark.parametrize(
    ("came_from", "url", "judged"),
    [
        ("https://example.com/a", "https://example.com/b", False),  # same origin
        ("http://example.com/", "https://example.com/", False),  # upgrade, default ports
        ("https://example.com/", "http://example.com/", True),  # downgrade is somewhere new
        ("http://example.com:8080/", "https://example.com/", True),  # not the same service
        ("http://example.com/", "https://example.com:8443/", True),
        ("https://example.com/", "https://cdn.example.com/", True),  # another host
        ("https://example.com/", "https://example.com:8443/", True),  # another port
        ("data:text/html,x", "https://example.com/", True),  # an opaque origin vouches for nothing
    ],
)
def test_a_redirect_hop_is_judged_only_when_it_goes_somewhere_new(came_from, url, judged):
    assert (classify(_hop(url, came_from)) is not None) is judged


def test_a_hop_names_where_it_came_from_on_the_trail(make_guard):
    guard, _, cfg = make_guard("enforce")

    allowed = guard.decide(
        _hop("https://elsewhere.example/x", "https://example.com/r?token=secret")
    )

    assert allowed is False
    row = _audit_rows(cfg)[-1]
    assert row["status"] == "denied"
    assert row["args"]["redirect_from"].startswith("https://example.com/r")
    assert "secret" not in cfg.audit_path.read_text(), (
        "the query value is redacted like any logged URL"
    )


def test_a_first_request_has_no_redirect_source_on_the_trail(make_guard):
    guard, _, cfg = make_guard("enforce")

    guard.decide(FakeRequest(url="https://elsewhere.example/x"))

    assert "redirect_from" not in _audit_rows(cfg)[-1]["args"]


def test_a_post_redirected_within_its_origin_does_not_spend_a_second_approval(make_guard):
    """307/308 keep the method and the body. The one approval paid for the first request;
    the same origin carrying on needs no second, and asking for one would strand every
    trailing-slash redirect on a form."""
    guard, perms, cfg = make_guard("enforce")
    perms.grant("default", SITE, Capability.SUBMIT, SITE)
    first = FakeRequest(url="https://example.com/save", method="POST", post_data="a=1")
    hop = _hop(
        "https://example.com/save/", "https://example.com/save", method="POST", post_data="a=1"
    )

    assert guard.decide(first) is True
    assert guard.decide(hop) is True

    assert [r["status"] for r in _audit_rows(cfg)] == ["allowed"], "only the first was judged"


def test_a_post_redirected_to_another_origin_needs_its_own_approval(make_guard):
    guard, perms, _ = make_guard("enforce")
    perms.grant("default", SITE, Capability.SUBMIT, SITE)
    first = FakeRequest(url="https://example.com/save", method="POST", post_data="a=1")
    hop = _hop(
        "https://evil.example/collect", "https://example.com/save", method="POST", post_data="a=1"
    )

    assert guard.decide(first) is True
    assert guard.decide(hop) is False, "the body does not follow the redirect on the first yes"


# --------------------------------------------------------------------------
# autonomous channel — arrival anywhere is a lease, except where denied
# --------------------------------------------------------------------------


async def test_autonomous_lets_a_hop_to_any_site_arrive(make_guard):
    guard, perms, cfg = make_guard("enforce")
    cfg.consent_channel = "autonomous"
    route = FakeRoute()

    await guard(route, FakeRequest(url="https://other.example/"))

    assert route.calls == [("continue",)]
    assert _entries(cfg)[-1]["args"]["reason"] == "approved by the autonomous channel"


async def test_autonomous_still_refuses_a_denied_origin(make_guard):
    guard, perms, cfg = make_guard("enforce")
    cfg.consent_channel = "autonomous"
    cfg.denied_origins = ("other.example",)
    route = FakeRoute()

    await guard(route, FakeRequest(url="https://other.example/"))

    assert route.calls == [("fulfill", 204)]


async def test_autonomous_does_not_license_an_undeclared_send(make_guard):
    guard, perms, cfg = make_guard("enforce")
    cfg.consent_channel = "autonomous"
    route = FakeRoute()

    await guard(route, FakeRequest(method="POST"))

    assert route.calls == [("fulfill", 204)]


async def test_closed_default_still_refuses_a_new_site(make_guard):
    guard, perms, cfg = make_guard("enforce")
    route = FakeRoute()

    await guard(route, FakeRequest(url="https://other.example/"))

    assert route.calls == [("fulfill", 204)]
