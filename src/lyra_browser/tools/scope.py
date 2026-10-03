"""Obtaining the scopes an action needs, before the action happens.

Tools declare what they intend; the enforcement layer checks what actually
occurs. That split is deliberate — a tool cannot know what a page will do with a
click, and the layer that sees the outcome cannot ask anyone anything. So the
tool buys a scope up front, and anything outside it is stopped later.

Consent must be obtained here rather than in the route handler because this is
where a live request context exists. Playwright's route dispatch task carries
the contextvars of whichever call first opened the browser, so an elicitation
from there would answer a request that already finished.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable

from ..approval import ApprovalRequired
from ..context import ServerContext, current_session_key
from ..origin import Origin, parse_origin
from ..permission import Capability


async def require(
    ctx: ServerContext,
    action: str,
    *,
    target: Origin,
    capabilities: Iterable[Capability],
    initiator: Origin | None = None,
    reason: str = "",
    confirm: bool = False,
    subject: str = "",
) -> tuple[dict | None, list]:
    """Acquire every capability, or return the envelope explaining the refusal.

    Returns ``(None, bought)`` when the action may proceed. ``bought`` is what
    the tool must hand back afterwards via ``released``, so a single-use scope
    cannot outlive the action it was bought for. The envelope keeps the
    ``needs_approval`` shape the server instructions already document — what
    changes is the reason a gate fires, not the shape callers see.
    """
    session = current_session_key(ctx)
    bought: list = []
    for capability in capabilities:
        decision = await ctx.consent.request(
            action=action,
            origin=target,
            capability=capability,
            session=session,
            initiator=initiator,
            detail=reason,
            legacy_confirm=confirm,
            subject=subject,
        )
        if not decision:
            # Nothing acquired so far may linger either — the action is not happening.
            ctx.perms.release_unspent(bought)
            return (
                ApprovalRequired(
                    action, f"{capability.value} on {target.describe()}", asked=decision.asked
                ).envelope(),
                [],
            )
        if decision.granted is not None:
            bought.append(decision.granted)
    return None, bought


@contextlib.asynccontextmanager
async def released(ctx: ServerContext, bought: list, grace_s: float | None = None):
    """Give back whatever the action did not use, shortly after it ends.

    Not immediately: a click that triggers navigation can have its request leave
    just after the call returns, and reclaiming the scope before then would
    refuse the very submission the user approved. A couple of seconds covers
    that and is far short of the ten minutes a lease would hand out.

    It is also where an action's span is marked for native-dialog answers: a
    ``handle_dialog`` answer is good for the dialogs raised inside the span of the
    one call it was armed for (``DialogHandler.acting``), and ends with it.
    """
    grace = ctx.config.scope_release_grace_s if grace_s is None else grace_s
    try:
        with ctx.session.dialogs.acting():
            yield
    finally:
        if grace > 0:
            # An async context manager always has a running loop, so this needs
            # no fallback — the zero case below is the only other path.
            asyncio.get_running_loop().call_later(grace, ctx.perms.release_unspent, bought)
        else:
            ctx.perms.release_unspent(bought)


def page_origin(page) -> Origin:
    """Origin of the page a tool is about to act on."""
    return parse_origin(getattr(page, "url", ""))
