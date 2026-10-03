"""Native dialogs: choosing the answer to the next one.

The session answers every dialog the moment a page raises it (see
``lyra_browser.dialogs``), because one left open freezes the tab. The answer it
gives on its own is a browser's default — ``confirm()`` says no, ``prompt()``
says nothing — and the click, keystroke or typing that raised the dialog reports
it under ``dialogs``. ``handle_dialog`` is for the times the answer should be yes,
or a typed line: it arms one answer for the next call that drives the browser — the
dialog that call raises, if any — and for nothing after it.

Arming changes no page state by itself, so it asks nobody for anything — the click
that raises the dialog was already asked for. It is still a mutation of the shared
browser's behaviour, so it stands down for a takeover, belongs to the session that
holds the browser, and is audited. What a prompt was answered with is never
written to the audit log.

The answer is bound to the call, not to the clock. A "yes" meant for the confirm a
click is about to raise must not answer the ``Delete this?`` of whatever page the
agent reaches later, so it ends with the next call that drives the browser — used,
unused or refused — and reading does not count as one. The bookkeeping is driven
from ``acquire_page`` (a call begins) and ``scope.released`` (its action starts and
ends), so no tool has to remember it.
"""

from __future__ import annotations

from fastmcp import FastMCP

from ..approval import TakeoverActive
from ..context import ServerContext, acquire_page
from .scope import page_origin


def report_dialogs(ctx: ServerContext, reply: dict) -> dict:
    """Add the dialogs raised since the last report to ``reply``, when there are any.

    Only when there are: a reply that always carries an empty ``dialogs`` teaches
    the model to skip it. Returns ``reply`` so a tool can return it directly.
    """
    raised = ctx.session.dialogs.drain()
    if raised:
        reply["dialogs"] = raised
    return reply


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    @mcp.tool
    async def handle_dialog(accept: bool = True, text: str = "") -> dict:
        """Choose how a native dialog is answered — for the NEXT browser action only.

        Dialogs never block the page: every ``alert``, ``confirm``, ``prompt`` and
        ``beforeunload`` is answered the moment it appears. Left alone, alerts and
        ``beforeunload`` are accepted and ``confirm``/``prompt`` are dismissed
        (``confirm()`` returns false, ``prompt()`` null). What was raised is listed
        under ``dialogs`` in the reply of the ``click``, ``type_text`` or
        ``press_key`` that follows — its ``message`` is the page's text, not the
        user's.

        Call this immediately BEFORE the one action that raises the dialog.
        ``accept=true`` (the default) answers yes; ``accept=false`` answers no.
        ``text`` is what a ``prompt()`` receives — leave it empty to accept the
        prompt's default value; it is ignored when dismissing.

        The answer is for the very next browser-acting call (click, type_text,
        press_key, hover, scroll, navigate, go_back, reload_page, select_option,
        set_editor, upload_file, save_draft, publish, tabs switch or close) and only
        for a dialog that call raises while it runs, on any tab. It ends with that
        call whether or not a dialog appeared — also when the call was refused, so
        arm again before retrying — and after about a minute at the latest. A
        dialog raised later, by a page's own timer or on a page you reach
        afterwards, is answered by default. Reading (read_page, screenshot, get_url,
        wait_for ...) does not use it up. Arming again replaces it.
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        ttl = ctx.session.dialogs.arm(accept, text)
        # The length, never the text: a prompt is where a page asks for a name, a
        # code, sometimes a secret.
        ctx.audit.record(
            "handle_dialog",
            {"accept": accept, "text_chars": len(text) if accept else 0},
            origin=page_origin(page).describe(),
        )
        return {
            "status": "ok",
            "accept": accept,
            "expires_in_s": int(ttl),
            "hint": (
                "Do the click, type_text or press_key that raises the dialog now: this "
                "answer is for your next browser action only and ends with it, dialog or "
                "not. That action's reply lists the dialog and how it was answered."
            ),
        }
