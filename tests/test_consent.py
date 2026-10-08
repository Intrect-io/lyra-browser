"""The consent broker decides who actually answered.

The distinction the whole design rests on: `confirm=True` is the model's own
assertion, and elicitation is an answer that arrived from the client. Both can
allow an action, but they must never be recorded as the same thing — an audit
reader has to be able to tell "a person agreed" from "the model said so". The
operator's trusted-origin list is a third answer, given in advance, and is
recorded as its own channel too.

Everything that is not an explicit approval denies: no request context, an
unsupported client, a timeout, a decline, a cancel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from lyra_browser.approval import CollaborationState
from lyra_browser.audit import AuditLog
from lyra_browser.config import Config
from lyra_browser.consent import ConsentBroker
from lyra_browser.origin import parse_origin
from lyra_browser.permission import Capability, PermissionStore

SITE = parse_origin("https://example.com/")
LOCAL = parse_origin("file:///etc/passwd")


_NO_VALUE = object()


@dataclass
class FakeElicitResult:
    """A raw ``ElicitResult``. ``value`` is what a form-rendering client picked;
    leave it out for the shape an approval-only client (Hermes) sends: accept
    with empty content."""

    action: str
    value: Any = _NO_VALUE

    @property
    def content(self) -> dict:
        return {} if self.value is _NO_VALUE else {"value": self.value}


class FakeSession:
    """The half of an MCP session the broker talks to: capabilities, and the
    raw elicitation request."""

    def __init__(self, elicitation: bool, result: Any, raises: Exception | None) -> None:
        self._elicitation = elicitation
        self._result = result
        self._raises = raises
        self.prompts: list[str] = []
        self.schemas: list[dict] = []

    def check_client_capability(self, capability) -> bool:
        return self._elicitation

    async def elicit(self, message, requestedSchema, related_request_id=None):  # noqa: N803
        self.prompts.append(message)
        self.schemas.append(requestedSchema)
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeContext:
    """Stands in for a live MCP request context.

    ``elicitation=False`` is a client that declared it cannot show a prompt —
    which is a different thing from one that fails while showing it.
    """

    request_id = "req-1"

    def __init__(
        self,
        result: Any = None,
        raises: Exception | None = None,
        elicitation: bool = True,
    ) -> None:
        self.session = FakeSession(elicitation, result, raises)
        self.prompts = self.session.prompts


@pytest.fixture
def make_broker(tmp_path):
    def _make(
        channel: str = "legacy",
        trusted: tuple[str, ...] = (),
        collab: CollaborationState | None = None,
        trusted_send: tuple[str, ...] = (),
    ):
        cfg = Config(
            consent_channel=channel, trusted_origins=trusted, trusted_send_origins=trusted_send
        )
        cfg.data_dir = tmp_path
        cfg.__post_init__()
        cfg.consent_channel = channel  # __post_init__ only overrides when approval is off
        store = PermissionStore(ttl_s=600.0)
        audit = AuditLog(cfg.audit_path)
        return ConsentBroker(cfg, store, audit, collab=collab), store, cfg

    return _make


def _entries(cfg) -> list[dict]:
    return [json.loads(x) for x in cfg.audit_path.read_text().splitlines() if x.strip()]


# --------------------------------------------------------------------------
# An existing grant answers without asking
# --------------------------------------------------------------------------


async def test_existing_grant_is_used_without_asking(make_broker):
    broker, store, cfg = make_broker("elicit")  # would deny if it had to ask
    store.grant("s1", SITE, Capability.NAVIGATE)

    decision = await broker.request(
        action="navigate", origin=SITE, capability=Capability.NAVIGATE, session="s1"
    )

    assert decision.allowed
    assert decision.channel == "grant"


# --------------------------------------------------------------------------
# legacy channel — the model's assertion, recorded as such
# --------------------------------------------------------------------------


async def test_legacy_honours_confirm(make_broker):
    broker, _, cfg = make_broker("legacy")

    decision = await broker.request(
        action="navigate",
        origin=SITE,
        capability=Capability.NAVIGATE,
        legacy_confirm=True,
    )

    assert decision.allowed
    assert decision.channel == "legacy"
    assert _entries(cfg)[-1]["args"]["consent_channel"] == "legacy"


async def test_legacy_denies_without_confirm(make_broker):
    broker, _, _ = make_broker("legacy")

    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert not decision.allowed
    assert decision.channel == "denied"


async def test_off_channel_allows(make_broker):
    broker, _, _ = make_broker("off")

    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert decision.allowed
    assert decision.channel == "off"


# --------------------------------------------------------------------------
# elicit channel — the answer comes off the wire
# --------------------------------------------------------------------------


async def test_elicit_approval_allows(make_broker, monkeypatch):
    broker, _, _ = make_broker("elicit")
    ctx = FakeContext(FakeElicitResult("accept", "approve"))
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)

    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert decision.allowed
    assert decision.channel == "elicit"
    assert "example.com" in ctx.prompts[0]


@pytest.mark.parametrize(
    "result",
    [
        FakeElicitResult("decline"),
        FakeElicitResult("cancel"),
        FakeElicitResult("accept", "deny"),
    ],
)
async def test_elicit_non_approval_denies(make_broker, monkeypatch, result):
    broker, _, _ = make_broker("elicit")
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: FakeContext(result))

    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert not decision.allowed


async def test_elicit_accept_without_a_value_allows(make_broker, monkeypatch):
    """Hermes routes elicitation into an approve/deny dialog and answers accept
    with empty content. That accept is the person's yes — it used to fail
    FastMCP's schema validation and deny every gated action under Hermes."""
    broker, _, _ = make_broker("elicit")
    ctx = FakeContext(FakeElicitResult("accept"))
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)

    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert decision.allowed
    assert decision.channel == "elicit"
    assert "approval-only" in decision.detail
    # The form still offers both answers to a client that renders it.
    assert ctx.session.schemas[0]["properties"]["value"]["enum"] == ["approve", "deny"]


@pytest.mark.parametrize(
    "content",
    [{"value": None}, {"value": "yes"}, {"approved": True}, "approve", ["approve"]],
)
async def test_elicit_unreadable_accept_denies(make_broker, monkeypatch, content):
    """Only an empty accept or an explicit ``approve`` allows; any other shape
    is one we cannot read, and a shape we cannot read is not a yes."""

    @dataclass
    class Raw:
        action: str
        content: Any

    broker, _, _ = make_broker("elicit")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_context",
        lambda: FakeContext(Raw("accept", content)),
    )

    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert not decision.allowed


async def test_elicit_empty_accept_is_still_one_shot_for_submit(make_broker, monkeypatch):
    """An approval-only yes buys exactly what a form yes buys: a SUBMIT stays
    single-use and is asked again for the next submission."""
    broker, _, _ = make_broker("elicit")
    ctx = FakeContext(FakeElicitResult("accept"))
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)

    for _ in range(2):
        decision = await broker.request(action="click", origin=SITE, capability=Capability.SUBMIT)
        assert decision.allowed

    assert len(ctx.prompts) == 2


async def test_elicit_denies_when_confirm_is_asserted(make_broker, monkeypatch):
    """On the elicit channel, confirm=True is not a substitute for an answer."""
    broker, _, _ = make_broker("elicit")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_context",
        lambda: FakeContext(FakeElicitResult("decline")),
    )

    decision = await broker.request(
        action="navigate",
        origin=SITE,
        capability=Capability.NAVIGATE,
        legacy_confirm=True,
    )

    assert not decision.allowed


def _raise_runtime():
    raise RuntimeError("no active context")


async def test_elicit_denies_without_a_request_context(make_broker, monkeypatch):
    broker, _, _ = make_broker("elicit")
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", _raise_runtime)

    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert not decision.allowed
    assert "no request context" in decision.detail


async def test_elicit_denies_when_client_declares_no_elicitation(make_broker, monkeypatch):
    """Pinned to elicit, a client that cannot be asked is refused, not waved on."""
    broker, _, _ = make_broker("elicit")
    ctx = FakeContext(elicitation=False)
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)

    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert not decision.allowed
    assert not ctx.prompts, "and was never asked"


async def test_a_failure_while_asking_denies_rather_than_downgrading(make_broker, monkeypatch):
    """The client said it could be asked, so a transport error is a lost answer.

    Sniffing exceptions cannot tell "no elicitation here" from "the connection
    broke while the user was deciding", and treating the second as the first
    let the model's own confirm=true stand in for an answer in progress.
    """
    broker, _, _ = make_broker("auto")
    ctx = FakeContext(raises=RuntimeError("connection reset"), elicitation=True)
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)

    decision = await broker.request(
        action="click",
        origin=SITE,
        capability=Capability.SUBMIT,
        legacy_confirm=True,
    )

    assert not decision.allowed, "a broken ask is not the model's turn to answer"
    assert decision.channel == "denied"


# --------------------------------------------------------------------------
# Approval turns into a grant — except where it must not
# --------------------------------------------------------------------------


async def test_approval_records_a_grant(make_broker):
    broker, store, _ = make_broker("off")

    await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)

    assert store.check("default", SITE, Capability.NAVIGATE)


async def test_opaque_origin_approval_does_not_outlive_the_request_it_bought(make_broker):
    """Approving one file:// read must not authorise the next one.

    It used to assert that the approval produced nothing at all, which meant an
    approved local file could never open once enforcement was the default. What
    must hold is narrower: the approval covers the request it was given for, and
    is gone after it.
    """
    broker, store, _ = make_broker("legacy")

    decision = await broker.request(
        action="navigate", origin=LOCAL, capability=Capability.NAVIGATE, legacy_confirm=True
    )

    assert decision, "the user said yes"
    assert store.consume("default", LOCAL, Capability.NAVIGATE), "so the request may happen"
    assert not store.consume("default", LOCAL, Capability.NAVIGATE), "and only that one"


async def test_denial_is_audited_with_the_origin(make_broker):
    broker, _, cfg = make_broker("legacy")

    await broker.request(action="click", origin=SITE, capability=Capability.SUBMIT)

    entry = _entries(cfg)[-1]
    assert entry["status"] == "denied"
    assert entry["origin"] == "https://example.com"
    assert entry["args"]["capability"] == "submit"


async def test_reusing_a_grant_does_not_extend_its_deadline(tmp_path):
    """A ten-minute bound must not become a sliding window.

    Handing the grant object back to the caller was first implemented by calling
    ``grant()`` again, which renews the expiry. Every reuse would push the
    deadline out, so a session that stays busy — including one a hostile page
    keeps busy — holds its approval for as long as it keeps working.
    """

    class FakeClock:
        now = 1000.0

        def __call__(self):
            return self.now

    clock = FakeClock()
    cfg = Config(consent_channel="off")
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    cfg.consent_channel = "off"
    store = PermissionStore(ttl_s=600.0, clock=clock)
    broker = ConsentBroker(cfg, store, AuditLog(cfg.audit_path))

    first = await broker.request(
        action="click", origin=SITE, capability=Capability.INTERACT, session="s1"
    )
    deadline = first.granted.expires_at

    clock.now += 300  # halfway through the lease
    again = await broker.request(
        action="click", origin=SITE, capability=Capability.INTERACT, session="s1"
    )

    assert again.channel == "grant", "the second call was answered by the first approval"
    assert again.granted is not None
    assert again.granted.expires_at == deadline, "using a grant is not re-approving it"


async def test_an_opaque_prompt_names_the_target(make_broker, monkeypatch):
    """ "file: (no site)" is every local file at once — nobody can answer that.

    An opaque approval used to produce nothing, so the vague prompt was inert.
    Now it authorises a real local read, and the person answering has to be told
    which file. Query values are stripped: a yes/no must not require reading a
    secret back to the user.
    """
    broker, _, _ = make_broker("elicit")
    ctx = FakeContext(FakeElicitResult("decline"))
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)

    await broker.request(
        action="navigate",
        origin=LOCAL,
        capability=Capability.NAVIGATE,
        subject="file:///Users/me/.ssh/id_rsa?token=secret",
    )

    assert "id_rsa" in ctx.prompts[0]
    assert "secret" not in ctx.prompts[0], "the value is redacted, the name is not"


# --------------------------------------------------------------------------
# auto — ask the person when there is one, and say so when there is not
# --------------------------------------------------------------------------


async def test_auto_asks_the_user_and_does_not_take_the_models_word(make_broker, monkeypatch):
    """A client that can be asked is asked, whatever the model asserted.

    The default used to be `legacy`, which meant the shipped server accepted
    `confirm=true` from the model — so a hidden instruction on a page telling it
    to re-call with confirm=true produced an approval.
    """
    broker, _, cfg = make_broker("auto")
    ctx = FakeContext(FakeElicitResult("decline"))
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)

    decision = await broker.request(
        action="click",
        origin=SITE,
        capability=Capability.SUBMIT,
        legacy_confirm=True,  # the model says yes; the person says no
    )

    assert not decision.allowed
    assert ctx.prompts, "the person was actually asked"


async def test_auto_falls_back_only_when_there_is_no_channel_to_ask_through(
    make_broker, monkeypatch
):
    """VEGA does not pass an elicitation handler yet, and must keep working."""
    broker, _, cfg = make_broker("auto")
    ctx = FakeContext(elicitation=False)
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: ctx)

    decision = await broker.request(
        action="click", origin=SITE, capability=Capability.SUBMIT, legacy_confirm=True
    )

    assert decision.allowed
    assert decision.channel == "legacy"
    assert "no user channel" in decision.detail, "and the trail says why"
    assert _entries(cfg)[-1]["args"]["consent_channel"] == "legacy"


@pytest.mark.parametrize(
    "result",
    [FakeElicitResult("decline"), FakeElicitResult("cancel"), FakeElicitResult("accept", "deny")],
)
async def test_auto_never_overrides_an_answer_with_the_models_assertion(
    make_broker, monkeypatch, result
):
    """A decline is an answer. Falling back there would let the model overrule it."""
    broker, _, _ = make_broker("auto")
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: FakeContext(result))

    decision = await broker.request(
        action="click", origin=SITE, capability=Capability.SUBMIT, legacy_confirm=True
    )

    assert not decision.allowed


async def test_pinned_elicit_denies_rather_than_weakening(make_broker, monkeypatch):
    broker, _, _ = make_broker("elicit")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_context", lambda: FakeContext(elicitation=False)
    )

    decision = await broker.request(
        action="click", origin=SITE, capability=Capability.SUBMIT, legacy_confirm=True
    )

    assert not decision.allowed and decision.channel == "denied"


# --------------------------------------------------------------------------
# A refusal a person gave is relayed, not retried
# --------------------------------------------------------------------------


async def test_a_person_s_refusal_is_marked_as_asked(make_broker, monkeypatch):
    broker, _, _ = make_broker("auto")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_context",
        lambda: FakeContext(FakeElicitResult("decline")),
    )
    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)
    assert not decision.allowed
    assert decision.asked


async def test_no_channel_is_not_marked_as_asked(make_broker, monkeypatch):
    """VEGA today: nobody was asked, so confirm=true is still the way through."""
    broker, _, _ = make_broker("auto")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_context", lambda: FakeContext(elicitation=False)
    )
    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)
    assert not decision.allowed
    assert not decision.asked


def test_envelope_hint_follows_who_answered():
    from lyra_browser.approval import ApprovalRequired

    unasked = ApprovalRequired("navigate", "navigate on x").envelope()
    assert "confirm=true" in unasked["hint"]
    answered = ApprovalRequired("navigate", "navigate on x", asked=True).envelope()
    assert answered["status"] == "needs_approval"
    assert "Re-call with confirm=true" not in answered["hint"]
    assert "was asked" in answered["hint"]


async def test_a_broken_channel_is_not_a_refusal(make_broker, monkeypatch):
    """No answer arrived, so the model is not told the user said no."""
    broker, _, _ = make_broker("auto")
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_context",
        lambda: FakeContext(raises=RuntimeError("connection reset")),
    )
    decision = await broker.request(action="navigate", origin=SITE, capability=Capability.NAVIGATE)
    assert not decision.allowed
    assert not decision.asked


# --------------------------------------------------------------------------
# trusted origins — the operator answered in advance
# --------------------------------------------------------------------------

KVR = parse_origin("https://www.kvraudio.com/")
START = parse_origin("https://start.example/")  # the page the agent came from
ELSEWHERE = parse_origin("https://elsewhere.example/")

# Everything a list must not answer: whatever is not being on a site.
NEEDS_ITS_OWN_ANSWER = [
    c for c in Capability if c not in (Capability.NAVIGATE, Capability.INTERACT)
]


@pytest.fixture
def declining_client(monkeypatch):
    """A client that refuses whatever it is asked, so a ``prompts`` list that stays
    empty proves nobody was."""
    client = FakeContext(FakeElicitResult("decline"))
    monkeypatch.setattr("fastmcp.server.dependencies.get_context", lambda: client)
    return client


class FakeRoute:
    def __init__(self) -> None:
        self.calls: list = []

    async def continue_(self) -> None:
        self.calls.append("continue")

    async def fulfill(self, **kwargs) -> None:
        self.calls.append(("fulfill", kwargs.get("status")))


class FakeNavigation:
    """The request the browser makes once a tool has been let through."""

    method = "GET"
    post_data = None

    def __init__(self, url: str, frame_url: str) -> None:
        self.url = url
        self.frame = type("Frame", (), {"url": frame_url})()

    def is_navigation_request(self) -> bool:
        return True


@pytest.mark.parametrize("channel", ["auto", "elicit", "legacy"])
@pytest.mark.parametrize("capability", [Capability.NAVIGATE, Capability.INTERACT])
async def test_a_trusted_origin_is_not_asked_and_leaves_the_grant_an_approval_would(
    make_broker, declining_client, channel, capability
):
    """The list answers in advance; the enforcement layer must not see a difference.

    That layer reads only the permission store, so a bare "yes" would let the tool
    proceed and then have its own navigation refused. The grant is bound to the
    initiator exactly as an approved one is.
    """
    broker, store, cfg = make_broker(channel, trusted=("https://www.kvraudio.com",))

    decision = await broker.request(
        action="navigate", origin=KVR, capability=capability, initiator=START
    )

    assert decision.allowed
    assert decision.channel == "trusted"
    assert not declining_client.prompts, "nobody was asked"
    grant = store.find_live("default", KVR, capability, START)
    assert grant is not None and decision.granted is grant
    assert grant.initiator == START, "earned from where the agent was, like an approval"
    assert not store.check("default", KVR, capability, ELSEWHERE), "and not from anywhere else"
    row = _entries(cfg)[-1]
    assert row["args"]["consent_channel"] == "trusted"
    assert row["status"] == "allowed"
    assert row["origin"] == "https://www.kvraudio.com"
    assert "https://www.kvraudio.com" in row["detail"], "and the trail names the entry"


async def test_a_trusted_navigate_carries_the_interact_it_implies(make_broker, declining_client):
    broker, store, _ = make_broker("elicit", trusted=("kvraudio.com",))
    site = parse_origin("https://kvraudio.com/")

    await broker.request(
        action="navigate", origin=site, capability=Capability.NAVIGATE, initiator=START
    )

    assert store.check("default", site, Capability.INTERACT, ELSEWHERE), "being there is using it"


@pytest.mark.parametrize(("channel", "prompts"), [("elicit", 1), ("legacy", 0)])
@pytest.mark.parametrize("capability", NEEDS_ITS_OWN_ANSWER)
async def test_a_trusted_origin_is_still_asked_for_everything_a_lease_cannot_hold(
    make_broker, declining_client, channel, prompts, capability
):
    """Listing a site pre-approves being on it — not sending to it, uploading to it,
    publishing on it or downloading from it. Each of those stays its own decision."""
    broker, store, cfg = make_broker(channel, trusted=("https://www.kvraudio.com",))

    decision = await broker.request(
        action="click", origin=KVR, capability=capability, initiator=KVR
    )

    assert not decision.allowed
    assert decision.channel == "denied"
    assert len(declining_client.prompts) == prompts, "the ask path was taken, or refused outright"
    assert store.live_grants("default") == [], "and nothing was left behind"
    assert _entries(cfg)[-1]["args"]["consent_channel"] == "denied"


@pytest.mark.parametrize(
    "url",
    [
        "https://kvraudio.com.evil.io/",  # the listed name is only the start of the host
        "https://evilkvraudio.com/",  # ... or only the end
        "https://kvraudio.com@evil.io/",  # ... or only the userinfo
        "https://www.kvraudio.com/",  # a subdomain of an exact entry
        "https://kvraudio.com:8443/",  # another service on the same host
        "http://kvraudio.com/",  # the same host without TLS
    ],
)
async def test_a_lookalike_of_a_trusted_origin_is_asked_like_any_other(
    make_broker, declining_client, url
):
    broker, store, _ = make_broker("elicit", trusted=("kvraudio.com",))

    decision = await broker.request(
        action="navigate",
        origin=parse_origin(url),
        capability=Capability.NAVIGATE,
        initiator=START,
    )

    assert not decision.allowed
    assert len(declining_client.prompts) == 1
    assert store.live_grants("default") == []


async def test_a_wildcard_trusts_the_subdomains_and_not_the_apex(make_broker, declining_client):
    broker, _, _ = make_broker("elicit", trusted=("*.example.com",))

    sub = await broker.request(
        action="navigate",
        origin=parse_origin("https://app.example.com/"),
        capability=Capability.NAVIGATE,
        initiator=START,
    )
    apex = await broker.request(
        action="navigate",
        origin=parse_origin("https://example.com/"),
        capability=Capability.NAVIGATE,
        initiator=START,
    )

    assert sub.channel == "trusted"
    assert not apex.allowed
    assert len(declining_client.prompts) == 1, "only the apex was put to the person"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,hi",
        "about:blank",
        "javascript:alert(1)",
        "blob:https://kvraudio.com/1",
    ],
)
async def test_an_opaque_origin_is_asked_whatever_the_list_says(make_broker, declining_client, url):
    """Every ``file:`` URL is one origin, so one yes would be a yes to the whole disk.

    Entries that try to name such a thing are dropped on load; this pins that
    nothing else in the list can reach one either.
    """
    broker, store, _ = make_broker(
        "elicit",
        trusted=("file:///etc/passwd", "data:text/plain", "about:blank", "*", "kvraudio.com"),
    )

    decision = await broker.request(
        action="navigate",
        origin=parse_origin(url),
        capability=Capability.NAVIGATE,
        initiator=START,
        subject=url,
    )

    assert not decision.allowed
    assert len(declining_client.prompts) == 1
    assert store.live_grants("default") == []


async def test_the_list_does_not_relabel_a_channel_that_is_off(tmp_path):
    """With approval switched off nobody is asking, so nothing is being answered in
    advance — and the trail must keep saying ``off``."""
    cfg = Config(require_approval=False, trusted_origins=("https://www.kvraudio.com",))
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    broker = ConsentBroker(cfg, PermissionStore(ttl_s=600.0), AuditLog(cfg.audit_path))

    decision = await broker.request(action="navigate", origin=KVR, capability=Capability.NAVIGATE)

    assert decision.allowed
    assert decision.channel == "off"


async def test_a_trusted_grant_is_a_lease_like_any_other(tmp_path):
    """Reuse does not renew it, and when it lapses the list issues the next one silently.

    The second half is the one that bites: answered once and then never again, a
    long Hermes session would start being asked about its own sites ten minutes in.
    """

    class FakeClock:
        now = 1000.0

        def __call__(self):
            return self.now

    clock = FakeClock()
    cfg = Config(consent_channel="elicit", trusted_origins=("https://www.kvraudio.com",))
    cfg.data_dir = tmp_path
    cfg.__post_init__()
    cfg.consent_channel = "elicit"  # and no client to ask: only the list can say yes
    store = PermissionStore(ttl_s=600.0, clock=clock)
    broker = ConsentBroker(cfg, store, AuditLog(cfg.audit_path))

    first = await broker.request(
        action="click", origin=KVR, capability=Capability.INTERACT, session="s1", initiator=KVR
    )
    deadline = first.granted.expires_at
    clock.now += 300  # halfway through the lease
    reused = await broker.request(
        action="click", origin=KVR, capability=Capability.INTERACT, session="s1", initiator=KVR
    )
    clock.now += 301  # past it
    reissued = await broker.request(
        action="click", origin=KVR, capability=Capability.INTERACT, session="s1", initiator=KVR
    )

    assert first.channel == "trusted"
    assert reused.channel == "grant", "the first answer covered the second call"
    assert reused.granted.expires_at == deadline, "using a lease is not renewing it"
    assert reissued.channel == "trusted", "and once it lapsed the list answered again"
    assert reissued.granted.expires_at > deadline


async def test_the_list_is_read_when_asked_not_when_the_broker_was_built(
    make_broker, declining_client
):
    broker, _, cfg = make_broker("elicit")
    before = await broker.request(
        action="navigate", origin=KVR, capability=Capability.NAVIGATE, initiator=START
    )

    cfg.trusted_origins = ("https://www.kvraudio.com",)
    after = await broker.request(
        action="navigate", origin=KVR, capability=Capability.NAVIGATE, initiator=START
    )

    assert not before.allowed
    assert after.channel == "trusted"


async def test_nothing_is_pre_approved_while_the_user_holds_the_session(
    make_broker, declining_client
):
    """The one consent asked during a takeover is its release, and no list of sites
    answers who is driving."""
    collab = CollaborationState(takeover=True)
    broker, _, _ = make_broker("elicit", trusted=("https://www.kvraudio.com",), collab=collab)

    held = await broker.request(
        action="resume_after_takeover", origin=KVR, capability=Capability.INTERACT, initiator=KVR
    )
    collab.takeover = False
    handed_back = await broker.request(
        action="click", origin=KVR, capability=Capability.INTERACT, initiator=KVR
    )

    assert not held.allowed
    assert len(declining_client.prompts) == 1
    assert handed_back.channel == "trusted", "the list applies again once the user hands back"


# --- through the real tools ------------------------------------------------


async def test_navigating_to_a_trusted_site_needs_no_approval_and_the_guard_agrees(
    make_ctx, tools_of, page, declining_client
):
    ctx = make_ctx()
    ctx.config.trusted_origins = ("kvraudio.com",)
    tools = await tools_of(ctx)

    result = await tools["navigate"](url="https://kvraudio.com/forum")

    assert result["status"] == "ok"
    assert ("goto", "https://kvraudio.com/forum") in page.calls
    assert not declining_client.prompts
    # The request the browser then makes is judged against the permission store alone.
    listed, unlisted = FakeRoute(), FakeRoute()
    await ctx.guard(listed, FakeNavigation("https://kvraudio.com/forum", "https://start.example/"))
    await ctx.guard(unlisted, FakeNavigation("https://evil.example/", "https://start.example/"))
    assert listed.calls == ["continue"]
    assert unlisted.calls == [("fulfill", 204)], "a site nobody listed is still stopped"


async def test_a_trusted_site_buys_being_on_it_and_not_sending_from_it(
    make_ctx, tools_of, page, declining_client
):
    ctx = make_ctx(on_site=False)  # no approval has ever been given here
    ctx.config.trusted_origins = ("start.example",)
    tools = await tools_of(ctx)

    plain = await tools["click"](selector="#plain")
    assert plain["status"] == "ok"
    assert not declining_client.prompts

    page.calls.clear()
    sent = await tools["click"](selector="#go", submits=True)

    assert sent["status"] == "needs_approval"
    assert page.calls == [], "the click did not happen"
    assert len(declining_client.prompts) == 1
    assert "submit" in declining_client.prompts[0]


async def test_the_release_of_a_takeover_is_not_answered_by_the_list(
    make_ctx, tools_of, declining_client
):
    """An agent that can clear its own lock can ignore the lock."""
    ctx = make_ctx(on_site=False)
    ctx.config.trusted_origins = ("start.example",)
    ctx.collab.takeover = True
    ctx.collab.takeover_reason = "user is driving"
    tools = await tools_of(ctx)

    result = await tools["resume_after_takeover"]()

    assert result["status"] == "takeover_active"
    assert ctx.collab.takeover, "the lock held"
    assert len(declining_client.prompts) == 1, "the person was asked, and said no"


# ---------------------------------------------------------------------------
# trusted submit origins -- the operator pre-approved sending, for one's own product
# ---------------------------------------------------------------------------

SENDABLE = (Capability.SUBMIT, Capability.UPLOAD)
NOT_SENT = [c for c in Capability if c not in (*SENDABLE, Capability.NAVIGATE, Capability.INTERACT)]


@pytest.mark.parametrize("channel", ["auto", "elicit", "legacy"])
@pytest.mark.parametrize("capability", SENDABLE)
async def test_a_send_trusted_origin_is_not_asked_and_leaves_a_one_shot_grant(
    make_broker, declining_client, channel, capability
):
    broker, store, cfg = make_broker(channel, trusted_send=("https://www.kvraudio.com",))

    decision = await broker.request(
        action="click", origin=KVR, capability=capability, initiator=KVR
    )

    assert decision.allowed and decision.channel == "trusted_send"
    assert not declining_client.prompts, "nobody was asked"
    assert store.is_one_shot(KVR, capability), "and the grant is still single-use"
    assert decision.granted is not None, "the enforcement layer finds what an approval leaves"
    row = _entries(cfg)[-1]
    assert row["args"]["consent_channel"] == "trusted_send"
    assert "https://www.kvraudio.com" in row["detail"]


@pytest.mark.parametrize("capability", NOT_SENT)
async def test_the_send_list_answers_nothing_but_sending(make_broker, declining_client, capability):
    """PUBLISH and DOWNLOAD stay their own decisions on a send-trusted site."""
    broker, store, _ = make_broker("elicit", trusted_send=("https://www.kvraudio.com",))

    decision = await broker.request(
        action="click", origin=KVR, capability=capability, initiator=KVR
    )

    assert not decision.allowed and decision.channel == "denied"
    assert len(declining_client.prompts) == 1, "the person was asked"
    assert store.live_grants("default") == []


async def test_roaming_trust_does_not_become_submit_trust(make_broker, declining_client):
    """The two lists are separate on purpose: listing a site for navigation says nothing
    about sending from it."""
    broker, _, _ = make_broker("elicit", trusted=("https://www.kvraudio.com",))

    decision = await broker.request(
        action="click", origin=KVR, capability=Capability.SUBMIT, initiator=KVR
    )

    assert not decision.allowed
    assert len(declining_client.prompts) == 1


async def test_submit_trust_does_not_become_roaming_trust(make_broker, declining_client):
    broker, _, _ = make_broker("elicit", trusted_send=("https://www.kvraudio.com",))

    decision = await broker.request(
        action="navigate", origin=KVR, capability=Capability.NAVIGATE, initiator=START
    )

    assert not decision.allowed, "being allowed to send is not being allowed to arrive"


@pytest.mark.parametrize(
    "url",
    [
        "https://kvraudio.com.evil.io/",
        "https://evilkvraudio.com/",
        "https://kvraudio.com@evil.io/",
        "https://kvraudio.com:8443/",
        "http://kvraudio.com/",
    ],
)
async def test_a_lookalike_of_a_submit_trusted_origin_is_asked(make_broker, declining_client, url):
    broker, _, _ = make_broker("elicit", trusted_send=("kvraudio.com",))

    decision = await broker.request(
        action="click", origin=parse_origin(url), capability=Capability.SUBMIT, initiator=START
    )

    assert not decision.allowed
    assert len(declining_client.prompts) == 1


async def test_an_opaque_origin_is_never_submit_trusted(make_broker, declining_client):
    broker, _, _ = make_broker(
        "elicit", trusted_send=("file:///etc/passwd", "data:text/plain", "*", "kvraudio.com")
    )

    for opaque in (LOCAL, parse_origin("data:text/html,<form>")):
        decision = await broker.request(
            action="click", origin=opaque, capability=Capability.SUBMIT, initiator=START
        )
        assert not decision.allowed


async def test_nothing_is_submit_trusted_while_the_user_holds_the_session(
    make_broker, declining_client
):
    collab = CollaborationState(takeover=True)
    broker, _, _ = make_broker("elicit", trusted_send=("https://www.kvraudio.com",), collab=collab)

    held = await broker.request(
        action="click", origin=KVR, capability=Capability.SUBMIT, initiator=KVR
    )
    collab.takeover = False
    handed_back = await broker.request(
        action="click", origin=KVR, capability=Capability.SUBMIT, initiator=KVR
    )

    assert not held.allowed
    assert handed_back.channel == "trusted_send", "the list applies again once the user hands back"


async def test_with_approval_off_the_trail_says_off_not_the_list(make_broker):
    broker, _, cfg = make_broker("off", trusted_send=("https://www.kvraudio.com",))

    decision = await broker.request(
        action="click", origin=KVR, capability=Capability.SUBMIT, initiator=KVR
    )

    assert decision.allowed and decision.channel == "off"
    assert _entries(cfg)[-1]["args"]["consent_channel"] == "off"


class FakePost(FakeNavigation):
    method = "POST"
    post_data = "a=1"


async def test_the_guard_lets_a_trusted_arrival_through_with_no_navigate_call(
    make_ctx, tools_of, declining_client
):
    """A redirect hop or a followed link never passes through the broker, so the operator's
    list has to hold at the guard too -- api.example bouncing a sign-in to app.example."""
    ctx = make_ctx()
    ctx.config.trusted_origins = ("*.example.com",)

    hop, other = FakeRoute(), FakeRoute()
    await ctx.guard(hop, FakeNavigation("https://app.example.com/", "https://api.example.com/"))
    await ctx.guard(other, FakeNavigation("https://evil.example/", "https://api.example.com/"))

    assert hop.calls == ["continue"]
    assert other.calls == [("fulfill", 204)], "a site nobody listed is still stopped"
    assert not declining_client.prompts
    rows = [json.loads(x) for x in ctx.config.audit_path.read_text().splitlines() if x.strip()]
    allowed = [r for r in rows if r.get("origin") == "https://app.example.com"]
    assert allowed and "operator-trusted origin" in (
        allowed[-1].get("args", {}).get("reason") or ""
    )


async def test_the_guard_does_not_let_the_roaming_list_pay_for_a_submission(make_ctx):
    ctx = make_ctx()
    ctx.config.trusted_origins = ("*.example.com",)

    from_listed, from_unlisted = FakeRoute(), FakeRoute()
    await ctx.guard(
        from_listed, FakePost("https://app.example.com/pay", "https://app.example.com/")
    )
    await ctx.guard(from_unlisted, FakePost("https://app.example.com/pay", "https://evil.example/"))

    assert from_listed.calls == [("fulfill", 204)], "listing a site never approved sending from it"
    assert from_unlisted.calls == [("fulfill", 204)]


async def test_the_guard_does_not_honour_the_list_when_asking_is_off(make_ctx):
    ctx = make_ctx()
    ctx.config.trusted_origins = ("*.example.com",)
    ctx.config.consent_channel = "off"

    called = FakeRoute()
    await ctx.guard(called, FakeNavigation("https://app.example.com/", "https://api.example.com/"))

    # `off` is decided elsewhere; the guard must not mint a "trusted" lease on its behalf.
    assert not any(
        "operator-trusted" in (r.get("args", {}).get("reason") or "")
        for r in (
            json.loads(x) for x in ctx.config.audit_path.read_text().splitlines() if x.strip()
        )
    )


# --------------------------------------------------------------------------
# autonomous channel — nobody to ask, so answer, within limits
# --------------------------------------------------------------------------

OTHER = parse_origin("https://other.example/")


@pytest.mark.parametrize(
    "capability",
    [
        Capability.NAVIGATE,
        Capability.INTERACT,
        Capability.SUBMIT,
        Capability.UPLOAD,
        Capability.DOWNLOAD,
    ],
)
async def test_autonomous_approves_what_a_task_needs_and_says_so(make_broker, capability):
    broker, store, cfg = make_broker("autonomous")

    decision = await broker.request(action="x", origin=OTHER, capability=capability)

    assert decision.allowed
    assert decision.channel == "autonomous"
    assert _entries(cfg)[-1]["args"]["consent_channel"] == "autonomous"
    assert decision.granted is not None


async def test_autonomous_never_publishes(make_broker):
    broker, store, _ = make_broker("autonomous")

    decision = await broker.request(
        action="publish", origin=SITE, capability=Capability.PUBLISH, legacy_confirm=True
    )

    assert not decision.allowed
    assert not store.check("default", SITE, Capability.PUBLISH)


@pytest.mark.parametrize("capability", [Capability.NAVIGATE, Capability.SUBMIT])
async def test_autonomous_does_not_approve_a_local_file(make_broker, capability):
    broker, _, _ = make_broker("autonomous")

    decision = await broker.request(action="x", origin=LOCAL, capability=capability)

    assert not decision.allowed


async def test_autonomous_single_use_scope_is_not_reusable(make_broker):
    broker, store, _ = make_broker("autonomous")

    first = await broker.request(action="x", origin=OTHER, capability=Capability.SUBMIT)
    store.consume("default", OTHER, Capability.SUBMIT, None)

    assert first.allowed
    assert not store.check("default", OTHER, Capability.SUBMIT)


@pytest.mark.parametrize("channel", ["autonomous", "off", "legacy"])
async def test_a_denied_origin_beats_every_yes(make_broker, channel):
    broker, store, cfg = make_broker(channel, trusted=("other.example",))
    cfg.denied_origins = ("other.example",)
    store.grant("default", OTHER, Capability.NAVIGATE)

    decision = await broker.request(
        action="navigate",
        origin=OTHER,
        capability=Capability.NAVIGATE,
        legacy_confirm=True,
    )

    assert not decision.allowed
    assert "denied_origins" in decision.detail
