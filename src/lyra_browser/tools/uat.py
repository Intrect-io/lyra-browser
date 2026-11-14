"""Tools a persona uses to report, available only in UAT mode.

Registered by ``lyra_browser.uat.server`` next to the browser tools — never
by ``register_all`` — so an ordinary server has none of them. They write to
the run's recorder; the browser is not involved. None of them spends a step.
"""

from __future__ import annotations

from fastmcp import FastMCP

from ..context import ServerContext
from ..uat.recorder import RunRecorder

SEVERITIES = ("blocker", "major", "minor")
OUTCOMES = ("reached_goal", "partial", "blocked")
VERDICTS = ("FIXED", "STILL_THERE", "COULD_NOT_CHECK")

_FINISHED = {"status": "finished", "reason": "finish was already called; the run is over."}


def _choice(value: str, allowed: tuple[str, ...], field: str) -> dict | None:
    if value in allowed:
        return None
    return {"status": "error", "reason": f"{field} must be one of {list(allowed)}", "given": value}


def register(mcp: FastMCP, ctx: ServerContext, recorder: RunRecorder) -> None:
    @mcp.tool
    async def report_finding(
        severity: str,
        url: str,
        step: int,
        expected: str,
        actual: str,
        evidence: list[str] | None = None,
    ) -> dict:
        """Record one finding: a place where this persona could not continue, misread
        the screen, or saw something false or contradictory.

        Report it the moment you see it, before moving on — a finding described from
        memory at the end loses its step and its evidence. ``severity`` is ``blocker``
        (the persona cannot reach its goal), ``major`` (the goal is reachable but the
        person would likely give up or be misled) or ``minor``. ``url`` is where it
        happened and ``step`` the step number from your trace. ``expected`` says what
        a person would reasonably expect there, ``actual`` what happened. ``evidence``
        lists what shows it: screenshot paths, quoted page text.

        Opinions about taste are not findings. Returns the finding's ``id``.
        """
        if recorder.finished:
            return _FINISHED
        if (bad := _choice(severity, SEVERITIES, "severity")) is not None:
            return bad
        if not actual.strip():
            return {"status": "error", "reason": "actual must say what happened."}
        finding_id = recorder.add_finding(
            severity=severity,
            url=url,
            step=step,
            expected=expected,
            actual=actual,
            evidence=[str(e) for e in (evidence or []) if str(e).strip()],
        )
        ctx.audit.record("report_finding", {"id": finding_id, "severity": severity, "url": url})
        return {"status": "ok", "id": finding_id, "findings": recorder.finding_count}

    @mcp.tool
    async def verdict(finding_id: str, status: str, evidence: str = "") -> dict:
        """Judge one finding from an earlier round that your brief lists.

        ``FIXED`` when you reached the place and the problem is gone, ``STILL_THERE``
        when it is still there (say what you saw), ``COULD_NOT_CHECK`` when your
        path or budget never got you there (say why). ``evidence`` is what you saw:
        a screenshot path or quoted page text. One verdict per finding; a later
        call replaces the earlier one.
        """
        if recorder.finished:
            return _FINISHED
        if (bad := _choice(status, VERDICTS, "status")) is not None:
            return bad
        if recorder.prior_finding_ids and finding_id not in recorder.prior_finding_ids:
            return {
                "status": "error",
                "reason": "unknown finding_id",
                "known": sorted(recorder.prior_finding_ids),
            }
        recorder.add_verdict(finding_id=finding_id, status=status, evidence=evidence)
        ctx.audit.record("verdict", {"finding_id": finding_id, "status": status})
        return {
            "status": "ok",
            "verdicts": len(recorder.verdicts),
            "missing": recorder.missing_verdicts(),
        }

    @mcp.tool
    async def note(text: str) -> dict:
        """Leave a short observation in the trace that is not a finding — what you
        decided and why, something that worked well, a doubt. Free; use it at
        decision points so the trace explains itself."""
        if recorder.finished:
            return _FINISHED
        recorder.add_note(text)
        return {"status": "ok"}

    @mcp.tool
    async def finish(
        outcome: str,
        summary: str,
        what_worked: list[str] | None = None,
        purchase_path: list[str] | None = None,
    ) -> dict:
        """End the run and hand in the result. Call it exactly once, as your last act.

        ``outcome`` is ``reached_goal``, ``partial`` (some success criteria met) or
        ``blocked`` (the persona would have given up). ``summary`` is two to five
        sentences a tester would write. ``what_worked`` lists what helped the
        persona. ``purchase_path`` is only for briefs that ask for it: each step from
        the first price seen to the checkout, what it promised and what it did.

        Findings are reported with ``report_finding`` before this, not inside the
        summary; ``verdict`` covers every finding your brief asked you to re-check.
        After ``finish`` every browser tool answers ``finished``.
        """
        if recorder.finished:
            return _FINISHED
        if (bad := _choice(outcome, OUTCOMES, "outcome")) is not None:
            return bad
        if not summary.strip():
            return {"status": "error", "reason": "summary must not be empty."}
        recorder.set_finish(
            outcome=outcome,
            summary=summary,
            what_worked=[str(w) for w in (what_worked or []) if str(w).strip()],
            purchase_path=[str(p) for p in (purchase_path or []) if str(p).strip()],
        )
        ctx.audit.record("finish", {"outcome": outcome, **recorder.summary()})
        return {
            "status": "ok",
            "outcome": outcome,
            "steps_used": recorder.steps_used,
            "step_budget": recorder.step_budget,
            "findings": recorder.finding_count,
            "verdicts": len(recorder.verdicts),
            "missing_verdicts": recorder.missing_verdicts(),
        }


def register_credentials(mcp: FastMCP, ctx: ServerContext, store, typer: dict) -> None:
    """``fill_credential``: registered only when the spec names credentials.

    ``typer["type_text"]`` is the raw ``type_text`` body, set once the server has listed
    its tools; typing through it keeps every gate of ``type_text`` (actionability,
    consent for ``submit``, the navigation guard, audit redaction)."""
    from ..uat.credentials import fill_envelope
    from .scope import page_origin

    @mcp.tool
    async def fill_credential(
        name: str,
        field: str,
        selector: str,
        submit: bool = False,
        reason: str = "",
        confirm: bool = False,
    ) -> dict:
        """Type one part of a stored login into ``selector`` without seeing it.

        ``name`` is a credential your brief lists. ``field`` is ``username``,
        ``password`` or ``totp`` (the current one-time code, computed now). It is typed
        only on the credential's own site: anywhere else the answer is ``wrong_origin``
        and nothing is typed. ``submit=true`` presses Enter afterwards and asks to send
        the form, as ``type_text`` does. The value never appears in your results, the
        trace or the report; what you get back is whether it was typed.
        """
        found, refused = fill_envelope(store, name, field)
        if refused is not None:
            ctx.audit.record("fill_credential", {"name": name, "field": field}, status="refused")
            return refused
        cred, value = found
        try:
            page = await ctx.session.page(revive=False)
            here = page_origin(page).describe()
        except Exception as exc:  # noqa: BLE001 - no page is an answer, not a crash
            return {"status": "error", "reason": f"no page: {type(exc).__name__}"}
        if here != cred.origin:
            ctx.audit.record(
                "fill_credential", {"name": name, "field": field}, status="wrong_origin"
            )
            return {
                "status": "wrong_origin",
                "credential": name,
                "field": field,
                "page_origin": here,
                "credential_origin": cred.origin,
                "hint": "Only type a credential on its own site. Navigate there first.",
            }
        result = await typer["type_text"](
            selector=selector, value=value, submit=submit, reason=reason, confirm=confirm
        )
        status = result.get("status") if isinstance(result, dict) else "error"
        ctx.audit.record(
            "fill_credential",
            {"name": name, "field": field, "selector": selector, "submit": submit},
            status=status,
        )
        return {**(result if isinstance(result, dict) else {}), "credential": name, "field": field}
