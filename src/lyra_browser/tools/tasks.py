"""Tools a task needs beyond reading and clicking: dragging, a read-only look at the
page's script state, the content of a saved download, a written deliverable, and the
place and language the browser claims.

Each goes through the same layers as the rest: a page from ``acquire_page``, the
interaction consent and the action guard where the page is acted on, and an audit row.
What a tool does *not* do is part of its contract:

- ``evaluate_readonly`` runs with V8's side-effect check (``Runtime.evaluate`` with
  ``throwOnSideEffect``): an expression that would write anything — the DOM, a global, a
  request — is refused by the engine before it runs, not by a pattern match. It also
  refuses expressions that name cookies or storage: ``storage`` reads those and asks
  before it reveals values.
- ``read_download`` reads only files this session saved (``list_downloads``).
- ``save_deliverable`` writes only inside the session's deliverables directory, under a
  plain file name, never over an existing file unless asked.
- ``set_emulation`` changes what pages are told (geolocation, locale, time zone) for this
  tab and every tab opened after it; it grants the geolocation permission it implies.

``storage`` (``tools/devtools.py``) is the read of cookies and web storage; there is no
second one here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import weakref
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from ..approval import TakeoverActive
from ..context import ServerContext, acquire_page, claim_session
from ..origin import loggable_url
from ..permission import Capability
from .actionable import DEFAULT_TIMEOUT_MS, check_target, clamp_timeout, guard_action
from .aftermath import Aftermath
from .scope import page_origin, require

_EVAL_TIMEOUT_MS = 3000
_MAX_RESULT_CHARS = 12000
_MAX_DOWNLOAD_CHARS = 50000
_MAX_DELIVERABLE_BYTES = 1_000_000
_DELIVERABLE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
# What ``storage`` reads, and asks before it reveals: not through a side door.
_STORAGE_NAMES = re.compile(
    r"\b(cookie|cookieStore|localStorage|sessionStorage|indexedDB|caches)\b"
)


def deliverables_dir(ctx: ServerContext) -> Path:
    configured = getattr(ctx.config, "deliverables_dir", None)
    return Path(configured or (ctx.config.data_dir / "deliverables")).expanduser()


async def cdp_send(page: Any, method: str, params: dict | None = None) -> dict:
    """One DevTools command on the page's own target, on either driver."""
    session = await page.context.new_cdp_session(page)
    try:
        return await session.send(method, params or {})
    finally:
        with contextlib.suppress(Exception):
            await session.detach()


def _emulation_commands(state: dict[str, Any]) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    if "geolocation" in state:
        geo = state["geolocation"]
        out.append(("Emulation.setGeolocationOverride", geo or {}))
    if "locale" in state:
        out.append(("Emulation.setLocaleOverride", {"locale": state["locale"] or ""}))
    if "timezone" in state:
        out.append(("Emulation.setTimezoneOverride", {"timezoneId": state["timezone"] or ""}))
    return out


# One DevTools session per tab for the overrides, kept open: Chrome drops an
# ``Emulation.*`` override when the session that set it detaches (measured with
# Playwright, whose ``new_cdp_session`` is a session of its own; the native driver's is
# the page's own and never detaches). Keyed weakly, so a closed tab takes its entry.
_EMULATION_SESSIONS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


async def _emulation_session(page: Any) -> Any:
    session = _EMULATION_SESSIONS.get(page)
    if session is None:
        session = await page.context.new_cdp_session(page)
        _EMULATION_SESSIONS[page] = session
    return session


async def apply_emulation(page: Any, state: dict[str, Any]) -> list[str]:
    """Send the overrides to one tab; the commands that failed, as text."""
    failed = []
    for method, params in _emulation_commands(state):
        try:
            await (await _emulation_session(page)).send(method, params)
        except Exception as exc:  # noqa: BLE001 - reported, the rest still applies
            failed.append(f"{method}: {type(exc).__name__}: {exc}"[:200])
    return failed


async def grant_geolocation(page: Any) -> str | None:
    """The permission an overridden position needs, or why it could not be granted."""
    context = page.context
    try:
        if hasattr(context, "grant_permissions"):
            await context.grant_permissions(["geolocation"])
        else:  # the native driver: the browser-level command itself
            await context._browser_send(
                "Browser.grantPermissions", {"permissions": ["geolocation"]}
            )
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"[:200]
    return None


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    emulation: dict[str, Any] = {}
    tabs_seen: set[int] = set()

    def on_new_tab(page: Any) -> None:
        # Every tab opened after set_emulation is told the same; a popup that loads at
        # once may run a line of script before this lands, which the README says.
        if emulation and id(page) not in tabs_seen:
            tabs_seen.add(id(page))
            asyncio.ensure_future(apply_emulation(page, dict(emulation)))

    observers = getattr(ctx.session, "page_observers", None)
    if isinstance(observers, list):
        observers.append(on_new_tab)

    @mcp.tool
    async def drag(
        source: str,
        target: str,
        reason: str = "",
        confirm: bool = False,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> dict:
        """Press the pointer on ``source``, move it onto ``target`` and release it.

        For sliders, sortable lists, kanban cards, file-drop areas fed from the page and
        anything else moved with the mouse. Both are selectors (or aria-refs); each is
        checked as ``click`` checks its target (``not_found``, ``hidden``, ``disabled``,
        ``element_not_actionable``) before anyone is asked. Asks to interact with the
        page, like ``hover``. The pointer moves in steps, so a page that tracks
        ``pointermove`` sees a drag rather than a jump.
        """
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page)
        try:
            ctx.collab.assert_agent_may_act()
        except TakeoverActive as exc:
            return exc.envelope()
        args = {"source": source, "target": target}
        timeout = clamp_timeout(timeout_ms)
        start, end = page.locator(source).first, page.locator(target).first
        for selector, locator in ((source, start), (target, end)):
            unusable = await check_target(
                ctx,
                tool="drag",
                page=page,
                origin=origin,
                locator=locator,
                selector=selector,
                args=args,
            )
            if unusable:
                return unusable
        denied, bought = await require(
            ctx,
            "drag",
            target=origin,
            capabilities=[Capability.INTERACT],
            initiator=origin,
            reason=reason,
            confirm=confirm,
            subject=getattr(page, "url", ""),
        )
        if denied:
            return denied
        after = Aftermath(ctx, page)
        async with guard_action(
            ctx,
            bought,
            tool="drag",
            selector=source,
            page=page,
            origin=origin,
            args=args,
            timeout_ms=timeout,
        ) as done:
            await start.hover(timeout=timeout)
            await page.mouse.down()
            try:
                await end.hover(timeout=timeout)
                # A last small move on the target: some pages only accept the drop after
                # a pointermove that starts and ends inside it.
                mouse = page.mouse
                x, y = getattr(mouse, "x", None), getattr(mouse, "y", None)
                if x is not None and y is not None:
                    await mouse.move(x + 1, y + 1, steps=2)
            finally:
                await page.mouse.up()
            after.acted()
            done.result = {"status": "ok", "url": page.url, "source": source, "target": target}
            status = await after.fold(done.result, caused_by="drag")
            ctx.audit.record("drag", args, status=status, origin=origin.describe())
        return done.result

    @mcp.tool
    async def evaluate_readonly(expression: str) -> dict:
        """Evaluate a JavaScript expression that only reads, in the current tab.

        For what the page knows but does not show: a value in a global, a computed
        style, how many items a list holds, a data attribute. The engine itself refuses
        anything with a side effect (assigning, calling a method that changes the DOM,
        sending a request) and the answer is then ``refused_side_effect``; nothing ran.
        Cookies and web storage are read with ``storage``, which asks before it reveals
        values: an expression that names them is refused here. No approval is asked,
        because nothing can change. Returns the value as JSON, or the exception.
        """
        if not expression.strip():
            return {"status": "invalid_argument", "error": "expression is empty"}
        if _STORAGE_NAMES.search(expression):
            return {
                "status": "refused_storage",
                "hint": "Read cookies and web storage with the storage tool.",
            }
        page, err = await acquire_page(ctx)
        if err:
            return err
        origin = page_origin(page).describe()
        try:
            reply = await cdp_send(
                page,
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "throwOnSideEffect": True,
                    "returnByValue": True,
                    "timeout": _EVAL_TIMEOUT_MS,
                },
            )
        except Exception as exc:  # noqa: BLE001 - the tab went away, the session failed
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
        details = reply.get("exceptionDetails")
        if details:
            text = str(
                (details.get("exception") or {}).get("description") or details.get("text") or ""
            )
            side_effect = "side-effect" in text.lower() or "side effect" in text.lower()
            status = "refused_side_effect" if side_effect else "exception"
            ctx.audit.record(
                "evaluate_readonly", {"expression": expression}, status=status, origin=origin
            )
            return {"status": status, "error": text[:2000], "url": loggable_url(page.url)}
        result = reply.get("result") or {}
        value = result.get("value", result.get("description"))

        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            encoded = json.dumps(str(value), ensure_ascii=False)
        ctx.audit.record("evaluate_readonly", {"expression": expression}, origin=origin)
        return {
            "status": "ok",
            "result": encoded[:_MAX_RESULT_CHARS],
            "truncated": len(encoded) > _MAX_RESULT_CHARS,
            "type": result.get("type"),
            "url": loggable_url(page.url),
        }

    @mcp.tool
    async def read_download(filename: str, max_chars: int = 20000) -> dict:
        """Read a file this session downloaded (one ``list_downloads`` shows).

        ``filename`` is its ``filename`` or ``path``. Text (UTF-8) comes back as
        ``text``, cut at ``max_chars`` (up to 50000); anything else is described
        (``bytes``, ``sha256``, the first bytes in hex). Only saved downloads can be
        read: any other path is ``not_found``. Reads only; asks nobody.
        """
        conflict = await claim_session(ctx, claim=False)
        if conflict is not None:
            return conflict
        saved = ctx.downloads.saved()
        entry = next(
            (d for d in reversed(saved) if filename in (d.get("filename"), d.get("path"))),
            None,
        )
        if entry is None:
            return {
                "status": "not_found",
                "known": [d.get("filename") for d in saved],
            }
        path = Path(str(entry["path"]))
        try:
            data = path.read_bytes()
        except OSError as exc:
            return {"status": "error", "error": f"cannot read {path.name}: {exc.strerror}"}
        limit = max(1, min(int(max_chars), _MAX_DOWNLOAD_CHARS))
        out: dict[str, Any] = {
            "status": "ok",
            "filename": entry.get("filename"),
            "path": str(path),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            out["binary"] = True
            out["head_hex"] = data[:64].hex()
        else:
            out["text"] = text[:limit]
            out["truncated"] = len(text) > limit
        ctx.audit.record("read_download", {"filename": entry.get("filename")})
        return out

    @mcp.tool
    async def save_deliverable(name: str, content: str, overwrite: bool = False) -> dict:
        """Write a result of the task to a file: an extract, a table, a summary.

        ``name`` is a plain file name (letters, digits, ``.``, ``_``, ``-``; no folders),
        ``content`` the text (UTF-8, up to 1 MB). It lands in the session's deliverables
        directory, whose path is returned. An existing file is kept unless
        ``overwrite=true``. Writes only there; the page is not involved.
        """
        if not _DELIVERABLE_NAME.match(name) or ".." in name:
            return {
                "status": "invalid_argument",
                "error": "name must be a plain file name: letters, digits, '.', '_', '-'",
            }
        data = content.encode("utf-8")
        if len(data) > _MAX_DELIVERABLE_BYTES:
            return {"status": "invalid_argument", "error": "content is over 1 MB"}
        directory = deliverables_dir(ctx)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if path.exists() and not overwrite:
            return {"status": "exists", "path": str(path), "hint": "pass overwrite=true"}
        tmp = path.with_name(f".{name}.part")
        tmp.write_bytes(data)
        tmp.replace(path)
        ctx.audit.record("save_deliverable", {"name": name, "bytes": len(data)})
        return {"status": "ok", "path": str(path), "bytes": len(data)}

    @mcp.tool
    async def set_emulation(
        latitude: float | None = None,
        longitude: float | None = None,
        accuracy: float = 50.0,
        locale: str | None = None,
        timezone: str | None = None,
        clear: bool = False,
    ) -> dict:
        """Set where and in which language the browser says it is, for a task that
        depends on it (local prices, a store finder, a date format).

        ``latitude``/``longitude`` (with ``accuracy`` in metres) override the position
        ``navigator.geolocation`` reports, and the geolocation permission is granted.
        ``locale`` (``de-DE``) changes what ``Intl`` and ``navigator.language`` say;
        ``timezone`` (``Europe/Berlin``) the time zone. The request's
        ``Accept-Language`` and the IP address do not change. Applies to this tab and
        every tab opened after; ``clear=true`` removes them all.
        """
        if (latitude is None) != (longitude is None):
            return {"status": "invalid_argument", "error": "give latitude and longitude"}
        if latitude is not None and not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return {"status": "invalid_argument", "error": "latitude/longitude out of range"}
        page, err = await acquire_page(ctx)
        if err:
            return err
        if clear:
            state = {"geolocation": None, "locale": None, "timezone": None}
            emulation.clear()
        else:
            state = {}
            if latitude is not None:
                state["geolocation"] = {
                    "latitude": latitude,
                    "longitude": longitude,
                    "accuracy": accuracy,
                }
            if locale is not None:
                state["locale"] = locale
            if timezone is not None:
                state["timezone"] = timezone
            if not state:
                return {"status": "invalid_argument", "error": "nothing to set"}
            emulation.update(state)
        failed = []
        if state.get("geolocation"):
            problem = await grant_geolocation(page)
            if problem:
                failed.append(f"Browser.grantPermissions: {problem}")
        for tab in ctx.session.open_pages() if hasattr(ctx.session, "open_pages") else [page]:
            tabs_seen.add(id(tab))
            failed += await apply_emulation(tab, state)
        ctx.audit.record("set_emulation", {k: v for k, v in state.items()})
        return {
            "status": "error" if failed else "ok",
            "emulation": dict(emulation),
            **({"failed": failed} if failed else {}),
        }
