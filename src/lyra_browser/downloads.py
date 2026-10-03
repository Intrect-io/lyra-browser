"""Downloads: a file a page hands to the browser is kept only when the agent asked.

Chromium accepts a download the moment a page offers one — a link answered with
``Content-Disposition: attachment``, ``<a download>``, a blob a script clicked —
and Playwright parks it in a temp dir. The navigation guard never sees it: it
judges requests, and a download is a *response*. Left alone, any page could write
files to the machine the agent drives.

So the default is turned around, in the same shape as every other effect:

* A tool that means to download says so (``download=true``) and buys
  ``Capability.DOWNLOAD`` first, inside the tool call, where consent can still be
  asked. The grant is single-use and belongs to that one call: the call retires
  it the moment it stops waiting (``DownloadWatch``), never later.
* ``Downloads.on_download`` judges each file *when it arrives*. A live grant pays
  for exactly that file, through ``perms.consume``, and the file is written under
  ``download_dir``. With none, the download is cancelled and deleted before
  anything reaches ``download_dir``, and the call is told (``download_blocked``)
  so the model can repeat it with the declaration. Nothing is inferred from the
  page: the tool declares, this judges what actually arrives.
* While the user holds a takeover the handler stands aside, as the navigation
  guard does: the user's own download is theirs to make, so it is saved and the
  trail says ``user_driven``. Refusing it would hand the user a browser that will
  not download, which is not what a takeover is for.
* ``observe`` mode (``LYRA_BROWSER_ENFORCEMENT=observe``) is the operator saying
  "record, do not block", and the navigation guard then only logs ``would_deny``.
  Cancelling a download there would block after all — and worse, a POST-backed
  "Export" cancelled once is re-sent by the model when it repeats the call with
  ``download=true``. So an undeclared download is saved exactly like a declared one
  (same name rules, caps and ledger), answered as ``download`` with ``observed:
  true`` and audited as ``would_block``. Like the guard, observe never spends a
  grant: it measures what enforcement would have done, and under-reports a second
  file in a call that enforcement would have cancelled.

The grant is not tied to an origin. The page that was clicked, the CDN that
serves the file, the ``blob:`` a script made and the popup that received it are
often four different origins, and binding the grant to any of them would refuse
the ordinary cases. What bounds it is time: it exists only while the call that
bought it is waiting, and it pays for one file.

One listener per tab, not ``BrowserContext.on('download')``: that event only
exists from Playwright 1.60, this project supports 1.58, and on an older driver a
context-level listener never fires — every download would go ahead unjudged. The
session puts the listener on each page it adopts, popups included (see
``BrowserSession._watch_downloads``).
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
import weakref
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .approval import CollaborationState
from .audit import AuditLog
from .config import Config
from .origin import parse_origin
from .permission import Capability, Grant, PermissionStore

# How long a navigation the driver says "is starting" a download waits for the
# download event itself. Measured: the error can land ~10ms before the event.
EVENT_WAIT_S = 2.0

# A key press returns before the browser has even sent the request, where a click waits
# for the navigation it started. Measured against Chrome 154, 20 presses of Enter on a
# link to an attachment: the download event landed up to 59ms after the press returned
# headless and up to 101ms headful (a click: up to 16ms, n=75 per mode). A window of
# 2.5x ``download_settle_s`` covers the slower of the two with room to spare.
KEYBOARD_SETTLE_SCALE = 2.5

# What a stuck cancel or delete is allowed to hold up the verdict.
_DISCARD_TIMEOUT_S = 5.0
# Room left over the transfer budget for the copy, so the call does not give up
# on a download the handler is a moment from finishing.
_COPY_SLACK_S = 15.0
# Used when the configured transfer budget is not a positive number.
_DEFAULT_TRANSFER_S = 120.0
# Records kept per session; a session that downloads every turn must not grow this.
_RECORDS_KEPT = 200

# Verdicts, decided synchronously when the download arrives.
_GRANTED = "granted"
_USER = "user_driven"
_BLOCKED = "blocked"
_OBSERVED = "observed"

# Names this server saved, one per line, oldest first, kept inside the download
# dir. Pruning needs it: a saved file keeps the site's name, so "oldest" cannot
# be read off the name (captures embed a timestamp), and the dir may hold files
# that are not ours. Only names listed here are ever deleted. It starts with a
# dot, which a sanitised name never does, so no download can collide with it.
_LEDGER = ".lyra-browser-downloads"

# Longest saved name, in bytes: filesystems allow 255, and a collision suffix
# still has to fit.
_MAX_NAME_BYTES = 180
_MAX_EXT_BYTES = 16
_FALLBACK_NAME = "download"
_UNSAFE = frozenset('<>:"|?*')
_SEPARATORS = re.compile(r"[\\/]")
# Device names Windows will not let a file be called, with or without an extension.
_RESERVED = frozenset({"con", "prn", "aux", "nul"}) | {
    f"{device}{n}" for device in ("com", "lpt") for n in range(1, 10)
}


def sanitize_filename(name: str | None, fallback: str = _FALLBACK_NAME) -> str:
    """A name that is safe to create inside a directory, whatever the site sent.

    The site chose ``name``; treat it as hostile. Only the last path component
    survives (``../../etc/x`` is ``x``), control and format characters go (the
    right-to-left override that dresses ``exe`` as ``txt`` among them), so do the
    characters Windows forbids, leading dots (no dotfile is ever created) and
    trailing dots and spaces. A reserved device name gets an underscore and a long
    name is cut at a character boundary with its extension kept.
    """
    parts = [part for part in _SEPARATORS.split(name or "") if part]
    text = parts[-1] if parts else ""
    text = "".join(ch for ch in text if ch.isprintable() and ch not in _UNSAFE)
    text = text.strip(" .")
    if text.split(".", 1)[0].lower() in _RESERVED:
        text = f"_{text}"
    return _clip(text, _MAX_NAME_BYTES) or fallback


def _split_extension(name: str) -> tuple[str, str]:
    """``(stem, extension)`` with the dot kept on the extension; ``.tar.gz`` is one."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext.encode()) > _MAX_EXT_BYTES:
        return name, ""
    if stem.lower().endswith(".tar"):
        stem, ext = stem[:-4], f"tar.{ext}"
    return stem, f".{ext}"


def _clip(name: str, limit: int) -> str:
    """``name`` cut to ``limit`` bytes without splitting a character or losing its extension."""
    if len(name.encode()) <= limit:
        return name
    stem, ext = _split_extension(name)
    room = max(1, limit - len(ext.encode()))
    return stem.encode()[:room].decode(errors="ignore").rstrip(" .") + ext


def unique_name(directory: Path, name: str) -> str:
    """``name``, or ``stem (1).ext``, ``stem (2).ext`` ... — the first one not taken.

    ``lexists`` rather than ``exists``: a dangling symlink already holds the name,
    and writing "through" it would land the file wherever the link points.
    """
    if not os.path.lexists(directory / name):
        return name
    stem, ext = _split_extension(name)
    for n in range(1, 10_000):
        candidate = f"{stem} ({n}){ext}"
        if not os.path.lexists(directory / candidate):
            return candidate
    return f"{stem}-{uuid.uuid4().hex[:8]}{ext}"


def source_origin(url: str) -> str:
    """Who a download came from: ``scheme://host[:port]``, never a path or a query.

    A ``blob:`` URL is named after the page that made it (``blob:https://site/id``),
    and that page is what is worth reporting; ``data:`` and ``file:`` have no site.
    """
    raw = url or ""
    if raw.lower().startswith("blob:"):
        raw = raw[5:]
    return parse_origin(raw).describe()


def remember_and_prune(directory: Path, saved: str, keep: int) -> None:
    """Record ``saved`` in the ledger and delete the oldest files beyond ``keep``.

    Only names in the ledger are deleted, so a download dir shared with other
    files loses nothing but what this server put there. ``keep <= 0`` keeps
    everything and writes no ledger.
    """
    if keep <= 0:
        return
    ledger = directory / _LEDGER
    try:
        listed = [line for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    except OSError:
        listed = []
    # Names whose file is gone must not count toward the limit, or a directory the
    # operator tidied by hand would lose real files early.
    names = [n for n in listed if n != saved and os.path.lexists(directory / n)] + [saved]
    for stale in names[: max(0, len(names) - keep)]:
        if Path(stale).name != stale:
            continue  # only a name we wrote is a file in this directory
        try:
            (directory / stale).unlink()
        except OSError:
            pass  # gone already, or not ours to delete — never a reason to fail the save
    names = names[-keep:]
    scratch = ledger.with_name(ledger.name + ".tmp")
    try:
        scratch.write_text("\n".join(names) + "\n", encoding="utf-8")
        os.replace(scratch, ledger)
    except OSError:
        pass  # a ledger that cannot be written costs tidiness next time, not the save


@dataclass(slots=True)
class Outcome:
    """What became of one download."""

    kind: str
    """``saved``, ``blocked`` or ``failed``."""
    origin: str
    """Where the file came from, as ``source_origin`` names it."""
    filename: str = ""
    """The saved name; for the others, the site's (sanitised) suggestion."""
    record: dict | None = None
    """``saved`` only: ``{filename, path, bytes, url_origin}``."""
    why: str = ""
    """``failed`` only: ``too_large``, ``timeout`` or ``error``."""
    reason: str = ""
    """``failed`` only: the cause, in words."""
    user_driven: bool = False
    """``saved`` only: kept for a person's own download during a takeover."""
    observed: bool = False
    """``saved`` only: no permission covered it and it was kept because enforcement is
    ``observe`` — what ``enforce`` would have cancelled."""


@dataclass(slots=True)
class _Arrival:
    """A download at the moment it arrived, with the verdict already taken."""

    download: Any
    decision: str
    origin: str
    initiator: str
    suggested: str
    session: str
    watchers: list[DownloadWatch] = field(default_factory=list)


_BLOCKED_HINT = (
    "A file download started and was cancelled: no download permission covered it, "
    "so nothing was saved. If you meant to download it, repeat the call with "
    "download=true."
)
_NOT_STARTED_HINT = (
    "download=true was declared but no download started within {seconds:g}s, so there "
    "was nothing to save, and the permission was released. Look at the page "
    "(read_page or screenshot): this control may not download anything."
)
_FAILED_HINT = "The download started but was not kept: {reason}. Nothing was saved."
_OBSERVED_HINT = (
    "Enforcement is in observe mode, so this download was saved although no download "
    "permission covered it. With enforcement on it would have been cancelled: declare "
    "download=true on a call that means to download."
)
_EXTRA_SAVED = (
    "{count} more download(s) started in the same call and were saved too: "
    "list_downloads names them."
)
_EXTRA_CANCELLED = (
    "{count} more download(s) started in the same call and were cancelled: the permission "
    "pays for one file. Each needs its own call with download=true."
)
_EXTRA_FAILED = (
    "{count} more download(s) started in the same call and were not kept: the audit trail says why."
)


def _add_hint(result: dict, text: str) -> None:
    """Add to whatever the tool already said, without overwriting it."""
    result["hint"] = f"{result['hint']} {text}" if result.get("hint") else text


def _extras_hint(extras: list[Outcome]) -> str:
    """What became of the downloads after the one a call reported, one sentence a kind."""
    kinds = {"saved": _EXTRA_SAVED, "blocked": _EXTRA_CANCELLED, "failed": _EXTRA_FAILED}
    sentences = []
    for kind, template in kinds.items():
        if count := sum(1 for o in extras if o.kind == kind):
            sentences.append(template.format(count=count))
    return " ".join(sentences)


def _transfer_budget_s(config: Config) -> float:
    """How long a started download may take: the configured budget, if it is a real one."""
    return config.download_timeout_s if config.download_timeout_s > 0 else _DEFAULT_TRANSFER_S


def _first_line(exc: BaseException) -> str:
    """The headline of a driver error: its call log is noise to whoever reads a reply."""
    lines = str(exc).strip().splitlines()
    return (lines[0] if lines else type(exc).__name__)[:160]


def _failure_reason(exc: BaseException) -> str:
    """Why a transfer did not finish, without the driver's API name.

    Measured against a server that closed the connection early (Chrome 154): the
    driver said ``Download.path: canceled``. A bare "canceled" reads as if this server
    had done it — it also cancels downloads, and says ``download_blocked`` when it
    does — so that word becomes "interrupted". Anything else the driver says
    (``net::ERR_...``) is kept as it is.
    """
    detail = re.sub(r"^Download\.\w+:\s*", "", _first_line(exc))
    if detail.strip().lower() in {"canceled", "cancelled"}:
        return "the transfer was interrupted before it finished"
    return detail


class DownloadWatch:
    """The downloads one tool call set off, and what became of each.

    Use as ``async with``. It registers before the action, so nothing that
    arrives during it is missed, and on the way out retires the DOWNLOAD grant the
    call bought but did not spend: that grant is for this call's file, not for
    whatever the page starts afterwards.
    """

    def __init__(
        self,
        manager: Downloads,
        bought: Iterable[Grant],
        declared: bool,
        start_budget_s: float,
        settle_scale: float = 1.0,
    ) -> None:
        self._manager = manager
        self._grants = [g for g in bought if g.capability is Capability.DOWNLOAD]
        self.declared = declared
        self._start_budget_s = start_budget_s
        self._settle_scale = settle_scale
        self._started = 0
        self._outcomes: list[Outcome] = []
        self._changed = asyncio.Event()

    async def __aenter__(self) -> DownloadWatch:
        self._manager._watches.append(self)
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self._manager._watches.remove(self)
        self._manager._perms.release_unspent(self._grants)
        return False

    async def settle(self, *, coming: bool = False) -> None:
        """Wait for whatever the action started.

        A declared call waits for the download to start (its own budget) and then
        to be written. One that declared nothing listens for ``download_settle_s``
        — long enough for the browser to report a download the action set off, so
        it can be reported as blocked — and then only for the cancel. ``coming``:
        the driver has said a download is starting, so its event is awaited
        whatever was declared.
        """
        config = self._manager._config
        if coming:
            first = EVENT_WAIT_S
        elif self.declared:
            first = self._start_budget_s
        else:
            first = config.download_settle_s * self._settle_scale
        await self._until(lambda: self._started > 0, first)
        await self._until(
            lambda: len(self._outcomes) >= self._started,
            _transfer_budget_s(config) + _COPY_SLACK_S,
        )

    async def began_after_abort(self, exc: BaseException) -> bool:
        """Whether ``exc`` is an aborted navigation that a download explains.

        ``goto`` is told "Download is starting". ``reload`` and ``go_back`` are not:
        measured against Chrome 154, headless and headful (n=12 each), they raise
        ``net::ERR_ABORTED; maybe frame was detached?`` and the browser offers the file
        0.1-22ms later. A bare abort is a navigation interrupted for its own reasons,
        so only one that a download follows within ``EVENT_WAIT_S`` counts; the
        caller's error stands otherwise (and any other error is not waited on).
        """
        if "ERR_ABORTED" not in str(exc):
            return False
        await self._until(lambda: self._started > 0, EVENT_WAIT_S)
        return self._started > 0

    async def _until(self, done: Callable[[], bool], timeout_s: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        while not done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                return

    def apply(self, result: dict) -> dict:
        """Fold what happened into the tool's reply, in place, and return it.

        A saved file is ``download`` on an otherwise untouched reply — plus
        ``observed: true`` when nothing covered it and it was kept only because
        enforcement is ``observe``. Otherwise a download that started replaces the
        status with the reason nothing was kept, and a declared one that never
        started says so — the model must be able to tell "the click did nothing"
        from "the file did not come".
        """
        saved = [o for o in self._outcomes if o.kind == "saved"]
        failed = [o for o in self._outcomes if o.kind == "failed"]
        blocked = [o for o in self._outcomes if o.kind == "blocked"]
        if saved:
            first = saved[0]
            result["download"] = dict(first.record or {})
            if first.observed:
                result["observed"] = True
                _add_hint(result, _OBSERVED_HINT)
            if extras := [o for o in self._outcomes if o is not first]:
                _add_hint(result, _extras_hint(extras))
        elif failed:
            result["status"] = "download_failed"
            result["reason"] = failed[0].reason
            _add_hint(result, _FAILED_HINT.format(reason=failed[0].reason))
        elif blocked:
            result["status"] = "download_blocked"
            result["blocked_download"] = {
                "filename": blocked[0].filename,
                "url_origin": blocked[0].origin,
            }
            _add_hint(result, _BLOCKED_HINT)
        elif self.declared:
            result["status"] = "download_not_started"
            _add_hint(result, _NOT_STARTED_HINT.format(seconds=self._start_budget_s))
        return result


class Downloads:
    """Judges every download the browser starts and saves the ones that were asked for."""

    def __init__(
        self,
        config: Config,
        perms: PermissionStore,
        audit: AuditLog,
        collab: CollaborationState | None = None,
        session_key: Callable[[], str] = lambda: "default",
    ) -> None:
        self._config = config
        self._perms = perms
        self._audit = audit
        self._collab = collab
        # Read at arrival, not at construction: the handler runs from a Playwright
        # callback, which cannot ask the live request whose session it belongs to.
        self._session_key = session_key
        self._watches: list[DownloadWatch] = []
        self._tasks: set[asyncio.Task] = set()
        self._seen: weakref.WeakSet = weakref.WeakSet()
        self._saved: dict[str, list[dict]] = {}

    def watch(
        self,
        bought: Iterable[Grant] = (),
        *,
        declared: bool = False,
        start_budget_s: float = 10.0,
        settle_scale: float = 1.0,
    ) -> DownloadWatch:
        """Follow the downloads a call causes. ``bought`` is what its ``require`` returned.

        ``settle_scale`` stretches the listen-after-acting window for an action that
        reports back before the browser has started anything (see ``KEYBOARD_SETTLE_SCALE``).
        """
        return DownloadWatch(self, bought, declared, start_budget_s, settle_scale)

    def saved(self, session: str | None = None) -> list[dict]:
        """What this session has downloaded, oldest first."""
        key = session if session is not None else self._session_key()
        return [dict(record) for record in self._saved.get(key, [])]

    async def idle(self) -> None:
        """Return once every download that has arrived has been dealt with.

        Waits on the tasks that are still running, not on the set: a finished task
        stays in it until the loop runs its done-callback, and ``gather`` of tasks
        that are all finished returns without yielding — so looping on the set
        would spin the loop, and the callback that empties it could never run.
        """
        while pending := [task for task in self._tasks if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    # -- the verdict ----------------------------------------------------------

    def on_download(self, download: Any) -> None:
        """A tab's ``download`` event. Judges now, finishes in the background.

        The verdict is taken here, synchronously: two downloads arriving together
        are judged in the order they came and each grant pays for one file.
        Everything slow — waiting for the file, copying it, cancelling — is a task.
        Never raises: an exception out of a Playwright event handler surfaces on an
        unrelated call, and a download whose verdict failed must not be kept.
        """
        try:
            if download in self._seen:
                return
            self._seen.add(download)
        except TypeError:
            pass  # not weak-referenceable: judge it rather than risk ignoring it
        try:
            arrival = self._judge(download)
        except Exception:  # noqa: BLE001 — fail closed
            arrival = _Arrival(
                download, _BLOCKED, "(unknown)", "(unknown)", "", self._safe_session()
            )
        arrival.watchers = list(self._watches)
        for watch in arrival.watchers:
            watch._started += 1
            watch._changed.set()
        task = asyncio.get_running_loop().create_task(self._handle(arrival))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _safe_session(self) -> str:
        try:
            return self._session_key()
        except Exception:  # noqa: BLE001 — a key we cannot read pays for nothing
            return "default"

    def _judge(self, download: Any) -> _Arrival:
        # The facts first, the verdict last: spending a grant is the one step that
        # cannot be taken back, so nothing that could fail follows it.
        page = getattr(download, "page", None)
        origin = source_origin(getattr(download, "url", ""))
        initiator = parse_origin(getattr(page, "url", "")).describe()
        suggested = sanitize_filename(getattr(download, "suggested_filename", ""))
        session = self._session_key()
        if self._collab is not None and self._collab.takeover:
            decision = _USER
        elif self.observing:
            # Record, never gate and never spend, as the guard does in this mode: a
            # live grant still marks the file as covered, anything else is a file
            # that enforcement would have cancelled.
            decision = _GRANTED if self._covered(session) else _OBSERVED
        elif self._spend(session):
            decision = _GRANTED
        else:
            decision = _BLOCKED
        return _Arrival(download, decision, origin, initiator, suggested, session)

    def _spend(self, session: str) -> bool:
        """Pay for one file with a live DOWNLOAD grant, if the session holds one."""
        for grant in self._perms.live_grants(session):
            if grant.capability is Capability.DOWNLOAD and self._perms.consume(
                session, grant.origin, grant.capability, grant.initiator, grant.subject_url
            ):
                return True
        return False

    @property
    def observing(self) -> bool:
        """Whether enforcement is switched to ``observe``: record, do not cancel.

        Read at each arrival, and only the exact word counts — the guard's own rule,
        so a value nobody can read never turns the gate off.
        """
        return self._config.enforcement_mode == "observe"

    def _covered(self, session: str) -> bool:
        """Whether the session holds a live DOWNLOAD grant. Looks, never spends."""
        return any(g.capability is Capability.DOWNLOAD for g in self._perms.live_grants(session))

    # -- what happens to the file ---------------------------------------------

    async def _handle(self, arrival: _Arrival) -> None:
        try:
            if arrival.decision == _BLOCKED:
                await self._discard(arrival.download)
                outcome = Outcome("blocked", arrival.origin, arrival.suggested)
            else:
                outcome = await self._save(arrival)
        except Exception as exc:  # noqa: BLE001 — whatever went wrong, nothing is kept
            await self._discard(arrival.download)
            outcome = Outcome(
                "failed", arrival.origin, arrival.suggested, why="error", reason=_first_line(exc)
            )
        self._publish(arrival, outcome)

    async def _save(self, arrival: _Arrival) -> Outcome:
        config = self._config
        download = arrival.download
        failed = {"origin": arrival.origin, "filename": arrival.suggested}
        try:
            source = await asyncio.wait_for(download.path(), _transfer_budget_s(config))
        except TimeoutError:
            await self._discard(download)
            return Outcome(
                "failed",
                why="timeout",
                reason=f"it was still arriving after {_transfer_budget_s(config):g}s",
                **failed,
            )
        except Exception as exc:  # noqa: BLE001 — a cancelled or broken transfer is an error here
            await self._discard(download)
            return Outcome("failed", why="error", reason=_failure_reason(exc), **failed)
        size = os.stat(source).st_size
        limit = config.download_max_bytes
        if limit > 0 and size > limit:
            await self._discard(download)
            return Outcome(
                "failed",
                why="too_large",
                reason=f"{size} bytes is over the {limit} byte limit",
                **failed,
            )
        directory = config.download_dir.expanduser().absolute()
        directory.mkdir(parents=True, exist_ok=True)
        # Written beside its destination and renamed into place, so a reader (VEGA
        # attaching the file) never sees a partial one — and the name is chosen at
        # the last moment, with no await between choosing and taking it.
        part = directory / f".{uuid.uuid4().hex}.part"
        try:
            await download.save_as(part)
            name = unique_name(directory, arrival.suggested)
            os.replace(part, directory / name)
        finally:
            part.unlink(missing_ok=True)
        final = directory / name
        record = {
            "filename": name,
            "path": str(final),
            "bytes": final.stat().st_size,
            "url_origin": arrival.origin,
        }
        # The browser keeps its own copy until the window closes; a 200MB file
        # would otherwise sit on disk twice for as long as the session lives.
        await self._delete(download)
        remember_and_prune(directory, name, config.download_keep)
        return Outcome(
            "saved",
            arrival.origin,
            name,
            record=record,
            user_driven=arrival.decision == _USER,
            observed=arrival.decision == _OBSERVED,
        )

    @staticmethod
    async def _discard(download: Any) -> None:
        """Stop the transfer and delete what arrived, so nothing stays on disk.

        Cancel first: a transfer in flight is removed by cancelling it, and one
        that already finished ignores the cancel — so the delete follows, for the
        finished file. On a cancelled download ``delete`` reports "canceled",
        which is the state that was wanted. Both are best-effort.
        """
        for step in (download.cancel, download.delete):
            try:
                await asyncio.wait_for(step(), _DISCARD_TIMEOUT_S)
            except Exception:  # noqa: BLE001 — already gone, already cancelled, browser closed
                pass

    @staticmethod
    async def _delete(download: Any) -> None:
        try:
            await asyncio.wait_for(download.delete(), _DISCARD_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — the copy that matters is already in place
            pass

    # -- telling everyone ------------------------------------------------------

    def _publish(self, arrival: _Arrival, outcome: Outcome) -> None:
        if outcome.kind == "saved":
            record = outcome.record or {}
            status = "would_block" if outcome.observed else "saved"
            if outcome.user_driven:
                status = _USER
            self._audit.record(
                "download",
                record,
                status=status,
                detail=(
                    "no live download permission; saved because enforcement is in observe mode"
                    if outcome.observed
                    else None
                ),
                origin=arrival.origin,
                initiator=arrival.initiator,
            )
            records = self._saved.setdefault(arrival.session, [])
            records.append(dict(record))
            del records[:-_RECORDS_KEPT]
        elif outcome.kind == "blocked":
            self._audit.record(
                "download",
                {"filename": outcome.filename, "url_origin": outcome.origin},
                status="blocked",
                detail="no live download permission; cancelled and deleted",
                origin=arrival.origin,
                initiator=arrival.initiator,
            )
        else:
            self._audit.record(
                "download",
                {"filename": outcome.filename, "url_origin": outcome.origin},
                status=outcome.why or "failed",
                detail=outcome.reason,
                origin=arrival.origin,
                initiator=arrival.initiator,
            )
        for watch in arrival.watchers:
            watch._outcomes.append(outcome)
            watch._changed.set()
