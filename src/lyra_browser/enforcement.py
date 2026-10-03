"""Judging what actually leaves the browser, rather than what the agent intended.

The gate used to read a control's ``type`` attribute and decide from that whether
a click would submit. Pages decide that, not attributes: a bare ``<a href>``
navigates, ``onclick="form.submit()"`` submits from a control the check just
cleared, and an attribute can change between the check and the click. Counting
doors does not work when the page builds the doors.

So the judgement moves to the one place every one of those paths converges:
the request the browser is about to send. Whatever caused it — a click, a
keystroke, a script, a redirect — a document navigation is a document
navigation, and it is checked against what the user actually granted.

HTTP redirect hops are where the two guard backends differ (measured 2026-10-01, Playwright
1.58.0 and 1.63.0, patchright 1.63.0, Chrome 154). Playwright continues every request that
has a ``redirectedFrom`` before any route handler is asked about it, so ``context.route``
sees the FIRST request of a chain and never the rest — also when the handler itself answered
the first with ``route.fulfill(302)``. The only way to see a hop before it leaves from
Playwright is to make the first request from Node (``route.fetch``), which puts a Node
TLS/HTTP/1.1 fingerprint on every document: rejected. See ``scripts/probe_redirect_hops.py``.

- ``guard_backend="route"`` therefore listens to ``context.on("request")``
  (``NavigationGuard.on_request``): each hop that changes origin is judged like a first
  request but AFTER it has been sent, and in enforce mode the load is cancelled. What that
  does not do is stated on ``on_request``.
- ``guard_backend="cdp"`` (``cdp_guard.py``) is shown every hop by ``Fetch.requestPaused``
  and judges it BEFORE it leaves: a refused hop's destination sees nothing.

Both backends feed ONE judgement, ``NavigationGuard.decide`` (``classify``, then ``_judge``):
the Playwright route through ``__call__``, the redirect listener through ``on_request``, the
CDP sidecar through ``Fetch.requestPaused``.

**This layer never asks anyone anything.** Playwright dispatches route handlers
from a task created when the browser first opened, and ``asyncio.create_task``
copies the contextvars of that moment — so a handler always sees the Context of
the call that started the session, not the current one. Eliciting from here
would address a request that finished long ago. Consent is obtained up front by
the tools, where the request context is live; this layer only compares what is
happening against what was granted.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urldefrag

from .approval import CollaborationState
from .audit import AuditLog
from .config import Config, parse_trusted_origins
from .origin import Origin, loggable_url, parse_origin
from .permission import Capability, PermissionStore

# Methods that change something on the far end. A navigation carrying a body is
# a submission whatever its method says.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

Mode = Literal["observe", "enforce"]

# How long a navigation gets to finish by itself once the guard has refused something
# while it ran, and how often that is looked at. See ``wait_unless_refused``.
REFUSAL_GRACE_S = 1.0
_REFUSAL_POLL_S = 0.05

# How long the cancellation of a refused redirect hop may take before it is given up on.
_STOP_BUDGET_S = 2.0

# A refused redirect's destination is shown to a model; a hostname is at most this long.
_MAX_ORIGIN_CHARS = 120


def refused_envelope(
    url: str = "", redirected_to: str = "", *, caused_by: str = "", declare: str = ""
) -> dict:
    """A fresh envelope each time — callers own what they are handed.

    ``url`` is where the tab is now. A refusal leaves the page standing, so that is
    the page the agent was on, or the one that tried to send itself away.

    ``redirected_to`` is the origin a redirect was refused for. The agent asked for a
    URL, not for where its server sent the browser, so without it "ask for the
    destination" names nothing it can ask for. Only the origin, never the path or the
    query: it is text a server chose, shown to a model.

    ``caused_by`` names what the agent did when the navigation was the page's answer to
    an action (``"click"``, ``"key press"``) rather than a URL the agent asked for. It has
    no destination of its own to ask for, so the hint says how to get one approved and,
    because a form the page sent is refused the same way, how to declare that on purpose
    (``declare``: the argument that does, such as ``"submits=true"``).
    """
    envelope = {
        "status": "blocked_by_policy",
        "reason": "The browser refused this navigation: no approval covers it.",
        "hint": (
            "Ask for the destination explicitly, or check the audit trail for what was refused."
        ),
    }
    if caused_by:
        send = (
            f" If the {caused_by} was meant to send a form, repeat it with {declare}."
            if declare
            else ""
        )
        envelope["hint"] = (
            f"The page navigated after your {caused_by} and the browser stopped it, so the tab "
            "stayed where it was. To go there on purpose, call navigate with the destination: "
            f"that asks the user.{send} The audit trail names what was refused."
        )
    if redirected_to:
        envelope["reason"] = (
            f"The browser stopped this navigation: the site redirected it to {redirected_to}, "
            "and no approval covers that."
        )
        envelope["hint"] = (
            "Ask for that destination explicitly with navigate, or check the audit trail "
            "for what was refused."
        )
        envelope["redirected_to"] = redirected_to
    if url:
        envelope["url"] = url
    return envelope


def refused_by_guard(exc: Exception) -> bool:
    """Whether a Playwright error looks like a navigation that was turned away.

    Necessary but not sufficient: ``ERR_ABORTED`` is also what a navigation that
    became a download reports, and what a page navigating away mid-``goto``
    reports. Callers pair this with the guard's own refusal count so a download
    is never announced as a policy block.

    A ``TimeoutError`` is the other shape a refusal takes: the 204 commits
    nothing, so a call whose page sent itself away while loading is left waiting
    for a navigation that no longer exists (see ``wait_unless_refused``). Matched
    by name, since playwright and patchright each define their own.

    So is "interrupted by another navigation": that is what the guard's own
    ``about:blank`` says to a call whose redirect it refused too late to cancel
    (see ``NavigationGuard._stop``).
    """
    text = str(exc)
    return (
        "ERR_ABORTED" in text
        or "ERR_BLOCKED" in text
        or "interrupted by another navigation" in text
        or type(exc).__name__ == "TimeoutError"
    )


def _drain(task: asyncio.Future) -> None:
    """Mark the outcome of an abandoned call as seen, so the loop does not report it."""
    if not task.cancelled():
        task.exception()


async def wait_unless_refused(
    guard: NavigationGuard | None, pending: Awaitable[Any], since: int
) -> Any:
    """Await a navigation the driver is performing, but not one the guard has stranded.

    The guard refuses with a 204, which commits nothing. When a page sends itself
    elsewhere while its own load is still running — an inline ``location.href = ...``
    — ``goto`` keeps waiting for the navigation that has just ceased to exist and
    gives up only at its own timeout: thirty seconds for a refusal made in
    milliseconds. (The same redirect from a ``setTimeout`` after load is harmless:
    the call has returned by then.)

    ``since`` is ``guard.refusals`` when the call began. Once it has risen the call
    gets ``REFUSAL_GRACE_S`` to finish on its own — a refused form post in a frame
    leaves the load to complete — and is then abandoned with a ``TimeoutError``,
    which ``refused_by_guard`` reads as the refusal it is. The tab is left exactly
    as it stands; the driver's own call times out later and nobody hears of it.
    """
    task = asyncio.ensure_future(pending)
    refused_at: float | None = None
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=_REFUSAL_POLL_S)
            if guard is None or guard.refusals <= since:
                continue
            now = time.monotonic()
            if refused_at is None:
                refused_at = now
            elif now - refused_at >= REFUSAL_GRACE_S:
                raise TimeoutError("the page was sent away by the guard and its load never ended")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(_drain)


@dataclass(frozen=True, slots=True)
class Intent:
    """What a request is about to do, in permission terms.

    ``redirect_from`` is the URL that sent the browser here: empty for a first request.
    """

    capability: Capability
    target: Origin
    initiator: Origin
    subject: Origin
    """Origin the grant must be held on — not always the destination.

    A form's action is chosen by the page, and no tool can read it before the
    click without inviting the TOCTOU this layer exists to avoid. So a
    submission is authorised by the origin that *sends* it, which is the page
    the user was looking at when they approved. The destination is recorded,
    not purchased.
    """
    method: str
    url: str
    redirect_from: str = ""

    @property
    def leaves_origin(self) -> bool:
        return not self.target.same_site_as(self.subject)

    def describe(self) -> str:
        where = f"{self.subject.describe()} -> {self.target.describe()}"
        if not self.leaves_origin:
            where = self.target.describe()
        return f"{self.method} {self.capability.value} {where}"


def in_subframe(request: Any) -> bool:
    """Whether this request belongs to an iframe rather than the main frame.

    A page embedding a map, a video or an ad is composing itself, not taking the
    user somewhere — but ``is_navigation_request()`` is true for a subframe
    document just the same. Unreadable frames count as main, because refusing to
    judge is the wrong way to fail.
    """
    try:
        return request.frame.parent_frame is not None
    except Exception:  # noqa: BLE001 — unknown, so judge it
        return False


def frame_origin(request: Any, *, inherit_parent: bool = True) -> Origin:
    """Origin of the frame that started this request, opaque when unknowable.

    ``request.frame`` raises for some navigation requests — the frame may not
    exist yet — so this never lets that escape into the guard. A frame that has
    not navigated yet reports ``about:blank``; for *attributing* a navigation,
    the page doing the embedding is its parent and that is the origin worth
    naming.

    ``inherit_parent=False`` for anything that spends a capability. Attribution
    and authority are not the same thing: an injected ``about:blank`` iframe has
    no origin of its own, and letting it borrow its parent's would let it spend
    the parent's approval on a request the parent never made — invisibly, since
    a subframe navigating leaves the top document exactly as it was.
    """
    try:
        frame = request.frame
    except Exception:  # noqa: BLE001 — any failure means "we do not know"
        return parse_origin("")
    try:
        origin = parse_origin(frame.url)
        if not origin.is_opaque or not inherit_parent:
            return origin
        parent = frame.parent_frame
        return parse_origin(parent.url) if parent is not None else origin
    except Exception:  # noqa: BLE001
        return parse_origin("")


def _hop_stays_put(source: Origin, target: Origin) -> bool:
    """Whether a redirect hop stays with the origin that issued it.

    The same origin, or an ``http`` origin upgraded to ``https`` on the same host and the
    default ports: whoever approved ``http://example.com`` did not mean to refuse where it
    takes them on the secure side of the same site. The reverse, and any other host or
    port, is somewhere new.
    """
    if source.is_opaque or target.is_opaque:
        return False
    if source.same_site_as(target):
        return True
    return (
        (source.scheme, target.scheme) == ("http", "https")
        and source.host == target.host
        and source.port is None
        and target.port is None
    )


def _was_redirected(request: Any) -> bool:
    """Whether something redirected the browser to this request. ``False`` when unreadable:
    an event handler that raises takes the driver's dispatch loop down with it."""
    try:
        return getattr(request, "redirected_from", None) is not None
    except Exception:  # noqa: BLE001 — same stance as classify(): not judgeable here
        return False


def classify(request: Any) -> Intent | None:
    """Return what this request would do, or ``None`` if it is not a navigation.

    Subresources and XHR return ``None``: they cannot move the user somewhere
    else, and gating them would break every site. That exemption is also this
    layer's blind spot — see the module docs in the plan.
    """
    try:
        if not request.is_navigation_request():
            return None
    except Exception:  # noqa: BLE001 — a request we cannot read is not judgeable here
        return None

    method = (getattr(request, "method", "") or "GET").upper()
    try:
        body = request.post_data
    except Exception:  # noqa: BLE001
        body = None
    target = parse_origin(getattr(request, "url", ""))
    hop_source = getattr(getattr(request, "redirected_from", None), "url", "") or ""
    if hop_source and _hop_stays_put(parse_origin(hop_source), target):
        # A redirect that stays with the origin that issued it is the same request carrying
        # on, and what approved the first one covers it. Somewhere else is a new decision.
        return None
    initiator = frame_origin(request)
    subject = target
    if method in _MUTATING_METHODS or body:
        # A form posted from inside an iframe is still a form being sent.
        capability = Capability.SUBMIT
        # Judge the sender. Cross-origin actions are ordinary on the real web —
        # payment handlers, SSO, third-party form endpoints — and demanding a
        # grant on the destination would refuse the very submission the user
        # approved while forecasting that destination is impossible.
        #
        # The sender is the frame's *own* origin, never its parent's. Both halves
        # of a grant are keyed on it: naming only the subject left the borrowed
        # parent identity in `initiator`, and a frame posting to the parent's own
        # origin then matched on both and spent the parent's approval — the same
        # invisible submission, one origin closer to home.
        sender = frame_origin(request, inherit_parent=False)
        initiator = sender
        if not sender.is_opaque:
            subject = sender
    elif in_subframe(request):
        # Loading an embedded document is the page building itself. Judging it
        # would refuse every map, video and ad on an otherwise approved site.
        return None
    elif target.same_site_as(initiator):
        # Moving around inside a site is part of interacting with it.
        capability = Capability.INTERACT
    else:
        # Leaving is its own decision, and an opaque origin on either side lands
        # here too — file: and data: are never "staying put".
        capability = Capability.NAVIGATE
    return Intent(
        capability=capability,
        target=target,
        initiator=initiator,
        subject=subject,
        method=method,
        url=getattr(request, "url", ""),
        redirect_from=getattr(getattr(request, "redirected_from", None), "url", "") or "",
    )


class NavigationGuard:
    """Judges outgoing navigations against live grants.

    ``decide`` is the judgement. Two adapters deliver requests to it and carry the
    verdict out: ``__call__`` for Playwright's ``context.route`` and
    ``cdp_guard.CdpGuard`` for ``Fetch.requestPaused``.
    """

    def __init__(
        self,
        config: Config,
        perms: PermissionStore,
        audit: AuditLog,
        mode: Mode | None = None,
        collab: CollaborationState | None = None,
    ) -> None:
        self._config = config
        self._perms = perms
        self._audit = audit
        self._collab = collab
        self._mode: Mode = mode or config.enforcement_mode  # type: ignore[assignment]
        # Set by the tools, which know the live session. The handler cannot ask.
        self.session_key = "default"
        self.refusals = 0
        """How many navigations this guard has turned away.

        A tool that started a navigation itself only sees a generic aborted
        request, so it compares this before and after: a change means the
        refusal was ours, and an unchanged count means the navigation failed
        for its own reasons and the error belongs to the caller.
        """
        self.refused_hop = ""
        """The origin the last refusal was a redirect to, empty when it was not a redirect.

        A tool that reports a refusal reads it right after the count rises: the agent asked
        for one URL and was turned away from another, which only this can name.
        """
        self._pending: set[asyncio.Future] = set()

    @property
    def mode(self) -> Mode:
        return self._mode

    def decide(self, request: Any) -> bool:
        """Judge one request: ``True`` lets it through, ``False`` refuses it with a 204.

        Everything the guard does about a request happens here and in ``_judge`` and
        nowhere else — classify, takeover, grant check and spend, audit row, refusal
        count — so the adapters cannot drift apart. They only carry the verdict out.

        Synchronous on purpose: nothing here waits, so an adapter holding a paused
        request answers it in the same turn of the event loop.
        """
        intent = classify(request)
        return intent is None or self._judge(intent)

    def verdict_on_error(self, exc: Exception) -> bool:
        """What happens to a request the guard failed to judge: let through only when observing.

        Records the failure. Deliberately not counted as a refusal: the tools read
        that count to say "no approval covers this", and sending an agent to seek
        an approval that cannot help hides a broken guard.
        """
        self._audit.record("navigation", status="guard_error", detail=type(exc).__name__)
        return self._mode == "observe"

    def record_failure(self, status: str, detail: str = "") -> None:
        """Put a failure of the interception itself on the trail (``guard_lost`` ...)."""
        self._audit.record("navigation", status=status, detail=detail or None)

    async def __call__(self, route: Any, request: Any) -> None:
        """Let a request through, or answer it with 204 — never abort.

        ``abort()`` sends the page to ``chrome-error://`` and the DOM the agent
        was working with disappears. 204 leaves the document standing, so a
        refusal is recoverable instead of destructive.
        """
        try:
            if self.decide(request):
                await route.continue_()
            else:
                await route.fulfill(status=204)
        except Exception as exc:  # noqa: BLE001 — a broken guard must not open the gate
            await self._fail_closed(route, exc)

    def _judge(self, intent: Intent) -> bool:
        """Judge one navigation: record it, spend what it spends, say whether it may go on.

        Everything that judges a navigation comes through here — the route (a first
        request, before it leaves), the redirect listener (a hop, after it has) and the
        CDP sidecar (every request and hop, before it leaves) — so they cannot disagree
        about what a navigation costs. ``False`` only in enforce mode, and then it is
        counted in ``refusals``.
        """
        if self._collab is not None and self._collab.takeover:
            # The user is driving. Grants record what the *agent* was allowed
            # to do; measuring a person against them refuses them their own
            # browser — and takeover exists precisely for the passwords,
            # CAPTCHAs and payments the agent must not do alone, every one of
            # which is a navigation. The trail says who drove.
            self._record(intent, True, status="user_driven")
            return True
        allowed = self._perms.check(
            self.session_key,
            intent.subject,
            intent.capability,
            intent.initiator,
            intent.url,
        )
        trusted = "" if allowed else self._trusted_arrival(intent)
        if trusted:
            allowed = self._perms.check(
                self.session_key,
                intent.subject,
                intent.capability,
                intent.initiator,
                intent.url,
            )
        if allowed and self._mode != "observe":
            # Spend wherever we would also refuse — the two must be the same
            # condition, or a mode that blocks without spending turns every
            # single-use approval into an unlimited one.
            self._perms.consume(
                self.session_key,
                intent.subject,
                intent.capability,
                intent.initiator,
                intent.url,
            )
        self._record(
            intent,
            allowed,
            reason=f"operator-trusted origin (matched {trusted})" if trusted and allowed else "",
        )
        if allowed or self._mode == "observe":
            return True
        self.refusals += 1
        self.refused_hop = (
            intent.target.describe()[:_MAX_ORIGIN_CHARS] if intent.redirect_from else ""
        )
        return False

    def _trusted_arrival(self, intent: Intent) -> str:
        """Mint the lease the operator's trusted list already answers, and name the entry.

        The list pre-approves *arriving* on a site, so it has to hold wherever the browser
        arrives -- a redirect hop or a followed link as much as a ``navigate`` call, which
        is the only place the broker could mint it. Without this, a sign-in link that
        bounces from ``api.`` to ``app.`` of the same product stopped at the hop.
        NAVIGATE only (never a single-use scope), never an opaque origin, and not when
        asking is off or the user is driving, exactly as in the broker.
        """
        if intent.capability is not Capability.NAVIGATE or intent.subject.is_opaque:
            return ""
        if self._config.consent_channel == "off":
            return ""
        entry = parse_trusted_origins(self._config.trusted_origins).entry_for(intent.subject)
        if entry is None:
            return ""
        self._perms.grant(
            self.session_key, intent.subject, Capability.NAVIGATE, intent.initiator, intent.url
        )
        return entry

    def _record(self, intent: Intent, allowed: bool, status: str = "", reason: str = "") -> None:
        if not status:
            if allowed:
                status = "allowed"
            else:
                status = "would_deny" if self._mode == "observe" else "denied"
        args = {
            "capability": intent.capability.value,
            "method": intent.method,
            "url": loggable_url(intent.url),
        }
        if intent.capability is Capability.SUBMIT and intent.leaves_origin:
            # Approved on the sender, delivered somewhere else. The trail has to
            # carry that on its own line — it is the exfiltration channel this
            # layer knowingly leaves open.
            # NB: ``cross_origin_send`` is only a label on this audit row, not a Capability.
            args["cross_origin_send"] = intent.target.describe()
        if intent.redirect_from:
            args["redirect_from"] = loggable_url(intent.redirect_from)
        if reason:
            args["reason"] = reason
        self._audit.record(
            "navigation",
            args,
            status=status,
            detail=intent.describe(),
            origin=intent.target.describe(),
            initiator=intent.initiator.describe(),
        )

    async def _fail_closed(self, route: Any, exc: Exception) -> None:
        """Refuse on our own failure, and never raise out of the handler.

        An exception escaping here surfaces later on an unrelated Playwright
        call, which is a confusing way to learn the guard broke.
        """
        allow = self.verdict_on_error(exc)
        try:
            if allow:
                await route.continue_()
            else:
                await route.fulfill(status=204)
        except Exception:  # noqa: BLE001 — already handled elsewhere; nothing left to do
            pass

    def on_request(self, request: Any) -> None:
        """Judge a redirect hop as it leaves: the listener for ``context.on("request")``.

        The route never hears of a hop (see the module docs), but the event for it is
        emitted — after the request has been sent. A hop to an origin no grant covers is
        judged exactly as a first request to it would be (``classify``, ``_judge``): the
        audit row carries ``redirect_from``, a single-use grant is spent, a takeover is
        ``user_driven``, observe mode records ``would_deny``. In enforce mode the load it
        belongs to is then cancelled and ``refusals`` rises, so the tool that started it
        answers ``blocked_by_policy`` like for any other refusal.

        **What escapes:** the request itself. The first hop that no grant covers is sent —
        with that origin's cookies, and the body of a 307/308 POST — before anything here
        can run; only the rest of the chain is cut. A server that answers before the
        cancellation lands (loopback, a LAN: the targets a redirect is most worth refusing
        for) commits the page first; the tab is then blanked and the audit says
        ``not_stopped``. A hop that stays on the origin that issued it is not judged.
        """
        if not _was_redirected(request):
            return
        intent: Intent | None = None
        try:
            intent = classify(request)
            if intent is None or self._judge(intent):
                return
        except Exception as exc:  # noqa: BLE001 — a broken guard must not open the gate
            self._audit.record("navigation", status="guard_error", detail=type(exc).__name__)
            if self._mode == "observe":
                return
        self._spawn(self._stop(request, intent))

    async def refused_redirect_since(self, since: int) -> str:
        """Where a redirect refused since ``refusals`` stood at ``since`` was sending the browser.

        Empty when nothing was refused since, or when the latest refusal was not a redirect.
        Waits for the cancellations still under way first, so the tab is where it will stay.
        """
        if self.refusals <= since or not self.refused_hop:
            return ""
        await self.settled()
        return self.refused_hop

    async def settled(self) -> None:
        """Wait for the cancellations of refused hops that are still under way."""
        while self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a cancellation in the background. Never raises: this is called from an event
        handler, and one that raises takes the driver's dispatch loop down with it."""
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except Exception as exc:  # noqa: BLE001 — no running loop: nothing can stop the load
            coro.close()
            self._audit.record("navigation", status="guard_error", detail=type(exc).__name__)
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _stop(self, request: Any, intent: Intent | None) -> None:
        """Cancel the load a refused hop belongs to; record whether that was in time.

        A CDP ``Page.stopLoading`` on a session opened for the purpose. That leaves the
        document standing, as the route's 204 does; it is not interception, so the
        popup-first-request hole of per-tab sessions does not apply. ``window.stop()``
        cannot do it: the page's own context does not answer while a main-frame navigation
        is pending (measured), so it only returns once the hop has committed.

        A server that answers before the stop lands — loopback, a LAN, which is where the
        targets worth refusing live — commits the refused page first, and a stop that
        cannot be sent leaves the load running. Either way the main frame is then replaced
        with ``about:blank`` (a new navigation also supersedes a pending one), so the agent
        never reads an origin that was refused; the load a tool is waiting on is
        interrupted, which it reads as the refusal it is (``refused_by_guard``). The page
        the tab was on cannot come back: the commit already took it.
        """
        reason = ""
        frame = page = None
        try:
            frame = request.frame
            page = frame.page
            await self._send_stop(page)
        except Exception as exc:  # noqa: BLE001 — recorded, never raised
            reason = f"the stop failed ({type(exc).__name__})"
        try:
            in_main_frame = frame is not None and frame.parent_frame is None
            if (
                intent is not None
                and in_main_frame
                and urldefrag(page.url)[0] == urldefrag(intent.url)[0]
            ):
                reason = "the page had already taken it"
        except Exception:  # noqa: BLE001 — unreadable means unknown, not taken
            in_main_frame = False
        if reason and in_main_frame:
            try:
                await asyncio.wait_for(page.goto("about:blank"), _STOP_BUDGET_S)
                reason += "; the tab was blanked"
            except Exception as exc:  # noqa: BLE001 — recorded, never raised
                reason += f"; blanking it failed ({type(exc).__name__})"
        status = "not_stopped" if reason else "stopped"
        if intent is not None:
            self._record(intent, False, status=status, reason=reason)
        else:
            self._audit.record("navigation", status=status, detail=reason or None)

    @staticmethod
    async def _send_stop(page: Any) -> None:
        session = await asyncio.wait_for(page.context.new_cdp_session(page), _STOP_BUDGET_S)
        try:
            await asyncio.wait_for(session.send("Page.stopLoading"), _STOP_BUDGET_S)
        finally:
            try:
                await session.detach()
            except Exception:  # noqa: BLE001 — the target may be gone already
                pass
