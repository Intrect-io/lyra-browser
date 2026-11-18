"""Bring the user's own logins in when lyra opens: a first-run offer, and an opt-in sync.

``lyra-browser profile import`` (profile_cli.py) is the person at the keyboard deciding
that lyra may act as them. Two moments reach the same import from ``open_browser``,
before the window launches (the import needs ``profile.lock``, which a live browser
holds):

- **First run.** The first ``open_browser`` on an empty lyra profile lists the browser
  profiles on this machine and asks the user, by MCP elicitation, whether to import one.
  The question travels on the wire to the client and the answer comes back from it, so
  the model can neither see the choice coming nor answer it. ``skip`` is the default and
  an empty or unreadable answer means skip: an approval-only client (Hermes) accepts
  without content, and that must not import anything. Asked only where a person
  answers: never under the ``autonomous``, ``legacy`` or ``off`` channel, never in
  Claude Code's bypass mode, never without a live request from a client that declared
  elicitation, and never when ``first_run_import`` is off (UAT runs). An answer, either
  way, is remembered in ``profile-first-run.json`` and not asked again.
- **Sync on open** (``LYRA_BROWSER_PROFILE_SYNC=1``, operator opt-in). When the source
  profile's files changed since the last import (``source_stamp``), it is imported again
  with the same browser, profile and sites. It never prompts and never unlocks a keyring;
  a sync that cannot run leaves the profile as it was and says why.

Neither ever fails ``open_browser``: an import that cannot go ahead is reported in the
reply (``profile_import`` / ``profile_sync``) and the window opens on what lyra had.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from . import profile_import
from .bypass import bypass_active
from .profile_import import (
    TARGET_PROFILE,
    ImportError_,
    SourceProfile,
    import_profile,
    read_sidecar,
    source_stamp,
)

FIRST_RUN_NAME = "profile-first-run.json"
SKIP = "skip"
# Channels on which a person answers the prompt. ``auto`` asks only when the client can
# be asked (checked below); it never falls back to anything here.
_ASKING_CHANNELS = ("elicit", "auto")


def _option(profile: SourceProfile) -> str:
    return f"{profile.browser}/{profile.directory}"


def _label(profile: SourceProfile) -> str:
    """What the user sees for a profile; the account stays in the prompt, not the reply."""
    shown = profile.name or profile.directory
    account = f" <{profile.account}>" if profile.account else ""
    return f"{profile.browser}: {shown} ({profile.directory}){account}"


def prompt_for(profiles: list[SourceProfile]) -> tuple[str, dict]:
    """The message and schema of the first-run question."""
    message = (
        "lyra's browser profile is empty. It can start with the logins of one of your "
        "own browser profiles: their cookies and site storage are copied into lyra's "
        "profile (yours is only read), and on those sites the agent then acts as you. "
        "Choose a profile to import it, or skip to start with an empty profile. You can "
        "import one later with `lyra-browser profile import`."
    )
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "title": "Import a browser profile",
                "enum": [SKIP, *(_option(p) for p in profiles)],
                "enumNames": [
                    "Skip: start with an empty profile",
                    *(_label(p) for p in profiles),
                ],
                # Claude Code refuses Accept on an unset field; the default is the no.
                "default": SKIP,
            }
        },
        "required": ["value"],
    }
    return message, schema


def lyra_profile_empty(config: Any) -> bool:
    """Whether lyra's profile was never imported into, never asked about, never used."""
    if read_sidecar(config) is not None or (config.data_dir / FIRST_RUN_NAME).exists():
        return False
    target = config.profile_dir / TARGET_PROFILE
    return not any((target / rel).exists() for rel in ("Cookies", "Network/Cookies"))


def _lock_held_by_other(config: Any) -> bool:
    from .doctor import _lock_held_by_other as held
    from .session import _PROFILE_LOCK_NAME

    return bool(held(config.data_dir / _PROFILE_LOCK_NAME))


def _remember(config: Any, answer: str) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    path = config.data_dir / FIRST_RUN_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"answer": answer, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _chosen(result: Any, offered: dict[str, SourceProfile]) -> tuple[str, SourceProfile | None]:
    """``(answer, profile)``. Only an accept naming an offered profile imports."""
    action = getattr(result, "action", None)
    if action != "accept":
        return str(action or "declined"), None
    content = getattr(result, "content", None)
    value = content.get("value") if isinstance(content, dict) else None
    if isinstance(value, str) and value in offered:
        return value, offered[value]
    return SKIP, None


async def offer_first_run(ctx: Any) -> dict | None:
    """Ask, import what the user chose, and say what happened; None when not asked."""
    config = ctx.config
    if not getattr(config, "first_run_import", False):
        return None
    if config.consent_channel not in _ASKING_CHANNELS or bypass_active():
        return None
    if ctx.session.live or not lyra_profile_empty(config):
        return None
    try:
        from fastmcp.server.dependencies import get_context

        mcp_ctx = get_context()
    except RuntimeError:
        return None  # no live request: nobody to ask
    from .consent import _client_can_be_asked

    if not _client_can_be_asked(mcp_ctx):
        return None
    profiles = profile_import.discover_profiles()
    if not profiles or _lock_held_by_other(config):
        return None
    offered = {_option(p): p for p in profiles}
    message, schema = prompt_for(profiles)
    try:
        import anyio

        with anyio.fail_after(config.consent_timeout_s):
            result = await mcp_ctx.session.elicit(
                message=message,
                requestedSchema=schema,
                related_request_id=mcp_ctx.request_id,
            )
    except Exception as exc:  # noqa: BLE001 — a timeout or a broken channel: ask next time
        ctx.audit.record("profile_first_run", status="unanswered", detail=type(exc).__name__)
        return {"status": "unanswered", "offered": len(profiles)}
    answer, chosen = _chosen(result, offered)
    if chosen is None:
        _remember(config, answer)
        ctx.audit.record("profile_first_run", status="skipped", detail=answer)
        return {"status": "skipped", "offered": len(profiles)}
    try:
        imported = await asyncio.to_thread(import_profile, config, chosen)
    except ImportError_ as exc:
        # Not remembered: the user chose to import, so the next open offers it again.
        ctx.audit.record("profile_first_run", status="failed", detail=str(exc))
        return {"status": "failed", "browser": chosen.browser, "reason": str(exc)}
    _remember(config, answer)
    ctx.audit.record("profile_first_run", status="imported", detail=answer)
    return {
        "status": "imported",
        "browser": chosen.browser,
        "profile": chosen.directory,
        "cookies": imported.cookies.imported,
    }


async def sync_on_open(ctx: Any) -> dict | None:
    """Re-import the imported profile if it changed; None when sync is off or not due."""
    config = ctx.config
    if not getattr(config, "profile_sync", False) or ctx.session.live:
        return None
    sidecar = read_sidecar(config)
    if not sidecar or not sidecar.get("user_data_dir") or not sidecar.get("profile"):
        return None
    source = SourceProfile(
        str(sidecar.get("browser") or "chrome"),
        Path(str(sidecar["user_data_dir"])),
        str(sidecar["profile"]),
        name=str(sidecar.get("name") or ""),
    )
    if not source.path.is_dir():
        return {"status": "failed", "reason": f"the source profile {source.path} is gone"}
    if source_stamp(source) == sidecar.get("source_stamp"):
        return {"status": "unchanged"}
    if _lock_held_by_other(config):
        return {"status": "skipped", "reason": "another lyra browser holds the profile"}
    try:
        result = await asyncio.to_thread(
            import_profile, config, source, sites=list(sidecar.get("sites") or ())
        )
    except ImportError_ as exc:
        ctx.audit.record("profile_sync", status="failed", detail=str(exc))
        return {"status": "failed", "reason": str(exc)}
    ctx.audit.record("profile_sync", detail=f"{result.cookies.imported} cookies")
    return {"status": "synced", "cookies": result.cookies.imported}
