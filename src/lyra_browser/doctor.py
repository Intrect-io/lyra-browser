"""``lyra-browser doctor``: what lyra would launch with here, and what would stop it.

A command for the person setting lyra up (pip, npx, the Claude Code plugin, Hermes),
not a tool for the model. It reads the same configuration the server reads and asks
the same questions a launch asks, in the same order, but it never launches a browser,
never imports a Playwright, and writes nothing except a probe file it removes again.

Every finding is either an ``error`` (a launch would fail; ``ok`` is false and the exit
code is 1) or a ``warning`` (a launch works, but not the way one might expect). Each
names the fix.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import json
import os
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .config import CLIENTS, Config, _has_display, display_alive, display_number
from .profile_import import PINNED_CHANNELS, pinned_candidates, read_sidecar

try:  # POSIX only, as in session.ProfileLock
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

INSTALL_BROWSER = "npx -y @intrect/lyra-browser install-browser"


def _find_executable(channel: str | None) -> str:
    from .native.launcher import find_executable

    return find_executable(channel)


def _lock_held_by_other(path: Path) -> bool | None:
    """Whether another process holds ``profile.lock``: True, False, or None (cannot tell).

    Opens the file only if it exists (no file, no holder) and gives a won claim back at
    once, so a doctor run never keeps a server from launching.
    """
    if fcntl is None:
        return None
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return True
        return None
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _writable(path: Path) -> tuple[bool, str]:
    """Whether lyra could write under ``path``, creating nothing that stays behind.

    A data dir that does not exist yet is created at launch, so what matters then is
    the nearest parent that does exist.
    """
    probe_dir = path
    while not probe_dir.exists():
        if probe_dir.parent == probe_dir:
            return False, "no existing parent directory"
        probe_dir = probe_dir.parent
    if not probe_dir.is_dir():
        return False, f"{probe_dir} is not a directory"
    try:
        with tempfile.NamedTemporaryFile(dir=probe_dir, prefix=".lyra-doctor-"):
            pass
    except OSError as exc:
        return False, f"cannot write in {probe_dir}: {exc.strerror or exc}"
    return True, "" if probe_dir == path else f"will be created under {probe_dir}"


def diagnose(
    config: Config,
    *,
    find: Callable[[str | None], str] | None = None,
    installed: Callable[[str], bool] | None = None,
    has_display: Callable[[], bool] | None = None,
    lock_held: Callable[[Path], bool | None] | None = None,
) -> dict[str, Any]:
    """The report, as a dict ready for JSON. The callables are seams for tests."""
    from .native.errors import Error as NativeError
    from .session import _PROFILE_LOCK_NAME, _any_executable, choose_driver
    from .session import installed as default_installed

    find = find or _find_executable
    installed = installed or default_installed
    has_display = has_display or _has_display
    lock_held = lock_held or _lock_held_by_other
    problems: list[dict[str, str]] = []

    def problem(level: str, what: str, fix: str) -> None:
        problems.append({"level": level, "what": what, "fix": fix})

    # -- browser binaries, in the order a launch tries them -------------------------
    sidecar = read_sidecar(config)
    candidates = pinned_candidates(config)
    pinned = PINNED_CHANNELS.get(str((sidecar or {}).get("browser")))
    pin_applies = pinned is not None and not config.channel
    channels: list[dict[str, Any]] = []
    for channel in candidates:
        try:
            channels.append({"channel": channel or "bundled", "path": find(channel)})
        except NativeError as exc:
            channels.append({"channel": channel or "bundled", "path": None, "error": str(exc)})

    # -- the driver ----------------------------------------------------------------
    def has_browser(chans: Sequence[str | None]) -> bool:
        return _any_executable(chans, find, NativeError)

    driver, passed_over = choose_driver(config.driver, candidates, has_browser, installed)
    available = {
        "native": has_browser(candidates),
        "patchright": installed("patchright"),
        "playwright": installed("playwright"),
    }
    if driver is None:
        why = f"none of {', '.join(passed_over)} is usable"
        # Under auto that means no browser was found and no Playwright is installed; the
        # missing browser is reported below, with the pin in mind.
        if config.driver != "auto":
            problem(
                "error",
                f"LYRA_BROWSER_DRIVER={config.driver} is not installed",
                f'pip install "lyra-browser[{config.driver}]", or unset LYRA_BROWSER_DRIVER',
            )
    elif config.driver == "auto":
        why = "auto: " + (
            f"{', '.join(passed_over)} passed over, {driver} is the first usable"
            if passed_over
            else f"{driver} is the first in the order (native, patchright, playwright)"
        )
    else:
        why = f"LYRA_BROWSER_DRIVER={config.driver}"

    # Which binary would open. native finds paths itself; a Playwright driver looks for
    # the same channels in the same places, and for "bundled" in its own browser cache.
    would = next((c for c in channels if c["path"]), None)
    if would is None:
        pin_note = f" (the imported profile pins {pinned})" if pin_applies else ""
        problem(
            "error",
            f"no browser found for {', '.join(c['channel'] for c in channels)}{pin_note}",
            f"install that browser, or run `{INSTALL_BROWSER}`"
            if not pin_applies
            else f"install {pinned}, or set LYRA_BROWSER_CHANNEL to override the pin",
        )

    # -- data dir, profile lock, display -------------------------------------------
    writable, writable_note = _writable(config.data_dir)
    if not writable:
        problem(
            "error",
            f"data dir {config.data_dir} is not writable ({writable_note})",
            "set LYRA_BROWSER_DATA_DIR (or --data-dir) to a directory you can write",
        )
    lock_path = config.data_dir / _PROFILE_LOCK_NAME
    held = lock_held(lock_path)
    if held:
        problem(
            "warning",
            "another lyra browser holds the shared profile (profile.lock)",
            "a new session gets a private, empty profile until that one closes "
            "(close_browser there, or end that server)",
        )

    # A display lyra opens its window on itself (LYRA_BROWSER_DISPLAY, or the
    # persistent VNC display `lyra-browser vnc install` keeps up) counts as one.
    display = has_display() or bool(config.display and display_alive(config.display))
    if (
        config.display
        and display_number(config.display) is not None
        and not display_alive(config.display)
    ):
        problem(
            "warning",
            f"the display {config.display} lyra was told to use is not up",
            "lyra-browser vnc status (or start the X server behind LYRA_BROWSER_DISPLAY)",
        )
    if not config.headless and not display:
        problem(
            "error",
            "a window is asked for (headless off) but there is no display",
            "set LYRA_BROWSER_HEADLESS=1 (or --headless), or run under a desktop session "
            "or xvfb-run",
        )

    imported: dict[str, Any] | None = None
    if sidecar:
        cookies = sidecar.get("cookies") or {}
        imported = {
            "browser": sidecar.get("browser"),
            "profile": sidecar.get("name") or sidecar.get("profile"),
            "imported_at": sidecar.get("imported_at"),
            "sites": sidecar.get("sites") or [],
            "cookies_imported": cookies.get("imported"),
            "pinned_channel": pinned if pin_applies else None,
        }

    guard = "cdp" if driver == "native" else config.guard_backend
    return {
        "ok": not any(p["level"] == "error" for p in problems),
        "version": __version__,
        "python": sys.version.split()[0],
        "client": config.client,
        "headless": bool(config.headless),
        "attended": config.attended,
        "consent_channel": config.consent_channel,
        "guard_backend": guard,
        "driver": {
            "configured": config.driver,
            "resolved": driver,
            "why": why,
            "available": available,
        },
        "browser": {
            "candidates": [c or "bundled" for c in candidates],
            "pinned_by_import": pinned if pin_applies else None,
            "channels": channels,
            "would_launch": would,
        },
        "data_dir": {
            "path": str(config.data_dir),
            "exists": config.data_dir.exists(),
            "writable": writable,
            "note": writable_note,
        },
        "profile_lock": {"path": str(lock_path), "held_by_another": held},
        "display": {
            "available": display,
            "needed": not config.headless,
            "x_display": config.display,
            "watch": config.watch,
        },
        "imported_profile": imported,
        "problems": problems,
    }


def render(report: dict[str, Any]) -> str:
    """The report for a person to read."""
    d, b = report["driver"], report["browser"]
    would = b["would_launch"]
    lines = [
        f"lyra {report['version']} (Python {report['python']}), client {report['client']}",
        f"driver      {d['resolved'] or 'none'}  ({d['why']})",
        f"browser     {would['channel'] + ' ' + would['path'] if would else 'none found'}",
    ]
    for c in b["channels"]:
        lines.append(f"  {c['channel']:<10} {c['path'] or 'not found'}")
    if b["pinned_by_import"]:
        lines.append(f"  pinned to {b['pinned_by_import']} by the imported profile")
    dd = report["data_dir"]
    lines += [
        f"window      {'headless' if report['headless'] else 'headful'}"
        f" (display {'yes' if report['display']['available'] else 'no'}"
        + (
            f", VNC {report['display']['watch']['vnc']} on {report['display']['x_display']}"
            if report["display"].get("watch")
            else ""
        )
        + ")",
        f"guard       {report['guard_backend']}, consent {report['consent_channel']}",
        f"data dir    {dd['path']} ({'writable' if dd['writable'] else 'NOT writable'}"
        f"{', ' + dd['note'] if dd['note'] else ''})",
    ]
    held = report["profile_lock"]["held_by_another"]
    lines.append(
        "profile     "
        + {True: "in use by another lyra browser", False: "free", None: "cannot tell"}[held]
    )
    imp = report["imported_profile"]
    if imp:
        lines.append(
            f"imported    {imp['browser']} profile {imp['profile']!r} at {imp['imported_at']}"
            f" ({imp['cookies_imported']} cookies)"
        )
    for p in report["problems"]:
        lines.append(f"{p['level'].upper():<8}{p['what']}\n        fix: {p['fix']}")
    lines.append("ok" if report["ok"] else "NOT ok: a launch would fail")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lyra-browser doctor",
        description=(
            "Report what lyra would launch with on this machine and what would stop "
            "it, without launching a browser. Exit code 0 when a launch would work."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    parser.add_argument(
        "--client", choices=sorted(CLIENTS), default=None, help="As for the server."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="lyra's data dir, overriding LYRA_BROWSER_DATA_DIR and the client's.",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="As for the server: check the window mode it would run in.",
    )
    return parser


def config_for(args: argparse.Namespace) -> Config:
    config = Config.from_env(client=args.client)
    if args.data_dir is not None:
        # replace() runs __post_init__ again, so the paths follow the new data dir.
        config = dataclasses.replace(config, data_dir=args.data_dir)
    if args.headless is not None:
        config.headless = args.headless
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = diagnose(config_for(args))
    with contextlib.suppress(BrokenPipeError):
        print(json.dumps(report, indent=2) if args.json else render(report))
    return 0 if report["ok"] else 1
