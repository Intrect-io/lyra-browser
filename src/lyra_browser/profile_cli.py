"""``lyra-browser profile``: bring the person's own browser logins into lyra.

A command for the person at the keyboard, not a tool for the model: whoever runs
it decides that the agent may act as them on those sites. See profile_import.py
for what moves and how.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import CLIENTS, Config
from .profile_import import (
    ImportError_,
    browser_roots,
    import_profile,
    list_profiles,
    read_sidecar,
    resolve_profile,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lyra-browser profile",
        description=(
            "Import the logins (cookies, localStorage, IndexedDB) of your own Chrome "
            "profile into lyra's profile. Run it again to sync: what it imports "
            "replaces what lyra had for the same sites."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="List the browser profiles on this machine.")
    _target_args(listing)  # accepted, so a launcher may always pass --client

    imp = sub.add_parser("import", help="Import (or re-sync) one profile into lyra.")
    imp.add_argument(
        "--profile",
        required=True,
        help="The profile's directory (Default, 'Profile 2') or the name Chrome shows.",
    )
    imp.add_argument(
        "--browser",
        default="chrome",
        choices=sorted(browser_roots()),
        help="Which browser the profile belongs to (default: chrome).",
    )
    imp.add_argument(
        "--user-data-dir",
        type=Path,
        default=None,
        help="The browser's user data dir, when it is not in the usual place.",
    )
    imp.add_argument(
        "--sites",
        default="",
        help=(
            "Comma-separated sites to import (example.com covers its subdomains). "
            "Omit for every site. With a list, localStorage is left out: it is one "
            "store for all sites and cannot be filtered."
        ),
    )
    imp.add_argument(
        "--dry-run", action="store_true", help="Count what would move; change nothing."
    )
    imp.add_argument(
        "--unlock-keyring",
        action="store_true",
        help="If the login keyring is locked, let it ask you for its password.",
    )
    imp.add_argument("--json", action="store_true", help="Print the report as JSON.")
    _target_args(imp)

    status = sub.add_parser("status", help="Show what was last imported into lyra.")
    _target_args(status)
    return parser


def _target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--client",
        choices=sorted(CLIENTS),
        default=None,
        help="Whose lyra profile to import into (as for the server; default: detect).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="lyra's data dir, overriding LYRA_BROWSER_DATA_DIR and the client's.",
    )


def _target(args: argparse.Namespace) -> Config:
    config = Config.from_env(client=args.client)
    if args.data_dir is not None:
        config = Config(
            client=config.client,
            data_dir=args.data_dir,
            denied_origins=config.denied_origins,
        )
    return config


def cmd_list(_args: argparse.Namespace) -> int:
    found = False
    for browser, root in browser_roots().items():
        profiles = list_profiles(browser, root)
        if not profiles:
            continue
        found = True
        print(f"{browser}  {root}")
        for p in profiles:
            account = f"  <{p.account}>" if p.account else ""
            print(f"  {p.directory:<12} {p.name}{account}")
    if not found:
        print("no Chromium-family browser profiles found", file=sys.stderr)
        return 1
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    root = args.user_data_dir or browser_roots()[args.browser]
    config = _target(args)
    try:
        source = resolve_profile(args.browser, root, args.profile)
        result = import_profile(
            config,
            source,
            sites=[s for s in args.sites.split(",") if s.strip()],
            dry_run=args.dry_run,
            unlock_keyring=args.unlock_keyring,
        )
    except ImportError_ as exc:
        print(f"lyra-browser profile import: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.sidecar, indent=2))
        return 0
    c = result.cookies
    verb = "would import" if args.dry_run else "imported"
    print(f"{verb} {source.browser} profile {source.directory!r} ({source.name or '-'})")
    print(f"  {'into':<14} {config.profile_dir}")
    print(
        f"  {'Cookies':<14} {c.imported} {verb} of {c.in_source}"
        f" ({c.out_of_scope} out of scope, {c.session} session-only,"
        f" {c.undecryptable} undecryptable);"
        f" lyra kept {c.kept}, replaced {c.replaced}"
    )
    for name, outcome in result.components.items():
        if name != "Cookies":
            print(f"  {name:<14} {outcome}")
    if c.undecryptable:
        print(
            f"warning: {c.undecryptable} cookies could not be decrypted and were left out "
            f"(keyring: {result.keyring})",
            file=sys.stderr,
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _target(args)
    sidecar = read_sidecar(config)
    if sidecar is None:
        print(f"nothing imported into {config.profile_dir}")
        return 0
    print(json.dumps(sidecar, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return {"list": cmd_list, "import": cmd_import, "status": cmd_status}[args.command](args)
