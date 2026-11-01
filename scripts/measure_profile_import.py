#!/usr/bin/env python3
"""Measure an import of a real profile: does lyra's Chrome hold every cookie it was given?

Reads the person's own profile (read-only), imports it into a throwaway lyra data
dir, opens lyra's Chrome there on ``about:blank`` (no page is loaded, no site is
contacted) and asks it over CDP for its cookies. Prints counts and whether every
imported cookie came back with the value the source holds; never prints a value.

    .venv/bin/python scripts/measure_profile_import.py --profile Default [--profile ...]
        [--browser chrome] [--unlock-keyring]

A report to read, not a gate: it depends on what is in the profiles today.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

# Chrome's cookie times: microseconds since 1601-01-01.
_EPOCH_DELTA_S = 11_644_473_600


def _now_chrome() -> int:
    return int((time.time() + _EPOCH_DELTA_S) * 1_000_000)


def expected(lyra_cookies: Path) -> dict[tuple[str, str, str], str]:
    """What lyra's Chrome should report: unexpired rows of the imported file, hashed."""
    from lyra_browser.profile_import import CookieCrypto

    crypto = CookieCrypto()
    db = sqlite3.connect(lyra_cookies)
    try:
        schema = int(db.execute("select value from meta where key='version'").fetchone()[0])
        rows = db.execute(
            "select host_key, name, path, encrypted_value, expires_utc, has_expires from cookies"
        ).fetchall()
    finally:
        db.close()
    now = _now_chrome()
    out = {}
    for host, name, path, enc, expires, has_expires in rows:
        if has_expires and expires and expires < now:
            continue  # Chrome drops an expired cookie when it loads the store
        plain = crypto.decrypt(bytes(enc), host, schema) or b""
        out[(host, name, path)] = hashlib.sha256(plain).hexdigest()
    return out


async def lyra_cookies(data_dir: Path) -> dict[tuple[str, str, str], str]:
    from lyra_browser.config import Config
    from lyra_browser.session import BrowserSession

    cfg = Config(client="generic", data_dir=data_dir, headless=True)
    session = BrowserSession(cfg)
    await session.start()
    try:
        page = await session.page()
        cdp = await page.context.new_cdp_session(page)
        got = (await cdp.send("Storage.getCookies"))["cookies"]
        await cdp.detach()
    finally:
        await session.stop()
    return {
        (c["domain"], c["name"], c["path"]): hashlib.sha256(
            c["value"].encode("latin-1")
        ).hexdigest()
        for c in got
    }


async def measure(browser: str, wanted: str, unlock: bool) -> bool:
    from lyra_browser.config import Config
    from lyra_browser.profile_import import (
        ImportError_,
        browser_roots,
        import_profile,
        resolve_profile,
        source_version,
    )

    root = browser_roots()[browser]
    source = resolve_profile(browser, root, wanted)
    with tempfile.TemporaryDirectory(prefix="lyra-measure-") as tmp:
        cfg = Config(client="generic", data_dir=Path(tmp), headless=True)
        try:
            result = import_profile(cfg, source, unlock_keyring=unlock)
        except ImportError_ as exc:
            print(f"{source.directory}: not imported: {exc}")
            return False
        c = result.cookies
        want = expected(cfg.profile_dir / "Default" / "Cookies")
        got = await lyra_cookies(Path(tmp))
    same_keys = set(want) == set(got)
    same_values = same_keys and all(want[k] == got[k] for k in want)
    print(
        f"{source.directory}: source {c.in_source} {c.by_prefix}, imported {c.imported}, "
        f"session-only {c.session}, undecryptable {c.undecryptable}, keyring {result.keyring}; "
        f"unexpired {len(want)}, lyra's Chrome reports {len(got)}, "
        f"same cookies {same_keys}, same values {same_values} "
        f"(source {browser} {source_version(root)})"
    )
    if not same_keys:
        print(f"  missing {len(set(want) - set(got))}, unexpected {len(set(got) - set(want))}")
    return same_values and c.undecryptable == 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default="chrome")
    parser.add_argument("--profile", action="append", required=True)
    parser.add_argument("--unlock-keyring", action="store_true")
    args = parser.parse_args()
    print(time.strftime("%Y-%m-%d %H:%M %Z"))
    ok = [asyncio.run(measure(args.browser, p, args.unlock_keyring)) for p in args.profile]
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
