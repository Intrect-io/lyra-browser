#!/usr/bin/env python3
"""Import a real Chrome profile into lyra and see the page get its logins.

Not part of the unit suite: it runs real Chrome twice per case, once as "the
person's browser" on a scratch user data dir, once as lyra. Everything is
loopback and temporary; the person's own profiles and keyring are never read.

    .venv/bin/python scripts/verify_profile_import_e2e.py [--headless-only]

Cases, one per way Chrome seals cookies on Linux:

- v10: the source Chrome runs with a basic password store (Playwright's default).
- v11: the source Chrome runs with ``--password-store=gnome-libsecret`` against a
  private Secret Service: this script re-runs itself under ``dbus-run-session``
  with a throwaway gnome-keyring, so the key is one only that keyring knows and
  the import has to fetch it over D-Bus, as it would on a desktop.

Each case asserts what the page saw, not only what the import reported:

1. lyra, before any import, stores a cookie of its own on ``localhost``;
2. the source logs in on ``127.0.0.1`` (cookie), writes localStorage and IndexedDB;
3. full import: lyra's page on ``127.0.0.1`` is sent the cookie and reads both
   stores back; lyra's own ``localhost`` cookie was replaced (sync = replace);
4. lyra stores its own cookie again, the source logs in afresh, and a re-import
   limited to ``--sites 127.0.0.1`` brings the new cookie and keeps lyra's own;
5. the source profile's files were not modified by either import.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect  # noqa: E402

_KEYRING_ENV = "LYRA_E2E_PRIVATE_KEYRING"
_KEYRING_PASSWORD = b"lyra-e2e-keyring"

CHECK_JS = """<script>
(async () => {
  const ls = localStorage.getItem('ls') || '-';
  let idb = '-';
  try {
    idb = await new Promise((resolve) => {
      const req = indexedDB.open('lyra-e2e', 1);
      req.onupgradeneeded = () => req.result.createObjectStore('kv');
      req.onsuccess = () => {
        const tx = req.result.transaction('kv', 'readonly');
        const get = tx.objectStore('kv').get('idb');
        get.onsuccess = () => resolve(get.result || '-');
        get.onerror = () => resolve('-');
      };
      req.onerror = () => resolve('-');
    });
  } catch (e) {}
  document.getElementById('stores').textContent = `LS[${ls}] IDB[${idb}]`;
  document.title = 'checked';
})();
</script>"""

STORE_JS = """<script>
(async () => {
  localStorage.setItem('ls', '%(value)s');
  await new Promise((resolve) => {
    const req = indexedDB.open('lyra-e2e', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('kv');
    req.onsuccess = () => {
      const tx = req.result.transaction('kv', 'readwrite');
      tx.objectStore('kv').put('%(value)s', 'idb');
      tx.oncomplete = resolve;
    };
  });
  document.title = 'stored';
})();
</script>"""


class Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        value = query.get("v", ["x"])[0]
        cookie = self.headers.get("Cookie", "")
        self.send_response(200)
        if url.path == "/login":
            self.send_header("Set-Cookie", f"who={value}; Max-Age=86400; Path=/; HttpOnly")
        elif url.path == "/own":
            self.send_header("Set-Cookie", f"own={value}; Max-Age=86400; Path=/")
        if url.path == "/store":
            body = "<title>t</title>" + STORE_JS % {"value": value}
        elif url.path == "/check":
            body = f"<title>t</title><p>COOKIE[{cookie}]</p><p id=stores></p>{CHECK_JS}"
        else:
            body = f"<title>{url.path.strip('/')}</title><p>COOKIE[{cookie}]</p>"
        data = body.encode()
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_: object) -> None:
        pass


async def seed_source(user_data_dir: Path, base: str, value: str, *, keyring: bool) -> None:
    """Be the person's own Chrome: log in, write both stores, close."""
    from playwright.async_api import async_playwright

    options: dict = {"headless": True, "channel": "chrome"}
    if keyring:
        options["ignore_default_args"] = ["--password-store=basic", "--use-mock-keychain"]
        options["args"] = ["--password-store=gnome-libsecret"]
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(str(user_data_dir), **options)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{base}/login?v={value}")
        await page.goto(f"{base}/store?v={value}")
        await page.wait_for_function("document.title === 'stored'")
        await ctx.close()


def cookie_prefixes(user_data_dir: Path) -> set[bytes]:
    db = sqlite3.connect(f"{(user_data_dir / 'Default' / 'Cookies').as_uri()}?mode=ro", uri=True)
    try:
        return {bytes(r[0]) for r in db.execute("select substr(encrypted_value,1,3) from cookies")}
    finally:
        db.close()


def source_fingerprint(user_data_dir: Path) -> dict[str, tuple[int, int]]:
    profile = user_data_dir / "Default"
    out = {}
    for path in [profile / "Cookies", *(profile / "Local Storage").rglob("*")]:
        if path.is_file():
            st = path.stat()
            out[str(path)] = (st.st_size, st.st_mtime_ns)
    return out


async def lyra_visit(data_dir: Path, *urls: str, headless: bool) -> str:
    """Open lyra on ``data_dir``, load ``urls`` in turn and return the last page's text."""
    ctx, tools = await build_tools(data_dir, headless=headless)
    try:
        opened = await tools["open_browser"]()
        expect(opened.get("status") == "ok", "lyra launches", opened)
        for url in urls:
            moved = await tools["navigate"](url=url, reason="profile import e2e", confirm=True)
            expect(moved.get("status") == "ok", f"lyra reaches {urlparse(url).path}", moved)
        if url.endswith("/check"):
            page = await ctx.session.page()
            await page.wait_for_function("document.title === 'checked'", timeout=10_000)
        return (await tools["read_page"]()).get("text", "")
    finally:
        await tools["close_browser"]()
        await ctx.session.stop()


async def run_case(name: str, base: str, alt: str, *, keyring: bool, headless: bool) -> None:
    from lyra_browser.config import Config
    from lyra_browser.profile_import import SourceProfile, import_profile

    print(f"\n[{name}] {'headless' if headless else 'headful'}")
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-import-{name}-", ignore_cleanup_errors=True)
    root = Path(tmp.name)
    source_dir, data_dir = root / "source", root / "lyra"
    try:
        text = await lyra_visit(data_dir, f"{alt}/own?v=lyra1", f"{alt}/echo", headless=headless)
        expect("own=lyra1" in text, "lyra stores a cookie of its own first", text)

        await seed_source(source_dir, base, f"{name}-1", keyring=keyring)
        want = {b"v11"} if keyring else {b"v10"}
        expect(cookie_prefixes(source_dir) == want, f"source cookies are sealed {want}")
        before = source_fingerprint(source_dir)

        cfg = Config(client="generic", data_dir=data_dir, headless=headless)
        result = import_profile(cfg, SourceProfile("chrome", source_dir, "Default"))
        c = result.cookies
        expect(c.undecryptable == 0 and c.imported >= 1, "every source cookie decrypts", c)
        expect(result.keyring == ("ok" if keyring else "not-needed"), "keyring", result.keyring)

        text = await lyra_visit(data_dir, f"{base}/check", headless=headless)
        expect(f"who={name}-1" in text, "lyra is sent the imported cookie", text)
        expect(f"LS[{name}-1]" in text, "imported localStorage reads back", text)
        expect(f"IDB[{name}-1]" in text, "imported IndexedDB reads back", text)
        text = await lyra_visit(data_dir, f"{alt}/echo", headless=headless)
        expect("own=" not in text, "a full import replaces lyra's own cookies", text)

        text = await lyra_visit(data_dir, f"{alt}/own?v=lyra2", f"{alt}/echo", headless=headless)
        expect("own=lyra2" in text, "lyra stores its own cookie again", text)
        await seed_source(source_dir, base, f"{name}-2", keyring=keyring)
        before = source_fingerprint(source_dir)
        result = import_profile(
            cfg, SourceProfile("chrome", source_dir, "Default"), sites=["127.0.0.1"]
        )
        expect(result.cookies.kept >= 1, "a site-limited sync keeps other cookies", result.cookies)
        text = await lyra_visit(data_dir, f"{base}/echo", headless=headless)
        expect(f"who={name}-2" in text, "re-import brings the source's new login", text)
        text = await lyra_visit(data_dir, f"{alt}/echo", headless=headless)
        expect("own=lyra2" in text, "cookies out of the site list survive the sync", text)
        expect(source_fingerprint(source_dir) == before, "the source profile is untouched")
    finally:
        tmp.cleanup()


def start_private_keyring() -> subprocess.Popen | None:
    """Inside dbus-run-session: a throwaway, unlocked gnome-keyring on this bus."""
    proc = subprocess.Popen(
        ["gnome-keyring-daemon", "--foreground", "--unlock", "--components=secrets"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdin is not None
    proc.stdin.write(_KEYRING_PASSWORD)
    proc.stdin.close()
    return proc


async def run(headless_only: bool) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    base, alt = f"http://127.0.0.1:{port}", f"http://localhost:{port}"
    modes = [True] if headless_only else [True, False]
    try:
        for headless in modes:
            await run_case("v10", base, alt, keyring=False, headless=headless)
            await run_case("v11", base, alt, keyring=True, headless=headless)
    finally:
        server.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless-only", action="store_true")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        print("SKIP  the keyring case needs Linux (D-Bus Secret Service)")
        return 0
    if os.environ.get(_KEYRING_ENV) != "1":
        # Re-run on a private session bus, with a keyring home of its own, so the
        # v11 case never sees the person's keyring.
        if not shutil.which("dbus-run-session") or not shutil.which("gnome-keyring-daemon"):
            print("FAIL  needs dbus-run-session and gnome-keyring-daemon")
            return 1
        home = tempfile.TemporaryDirectory(prefix="lyra-import-keyring-")
        env = {**os.environ, _KEYRING_ENV: "1", "XDG_DATA_HOME": home.name}
        env.pop("GNOME_KEYRING_CONTROL", None)
        env.update(NO_AT_BRIDGE="1", GTK_USE_PORTAL="0")
        cmd = ["dbus-run-session", "--", sys.executable, __file__, *sys.argv[1:]]
        try:
            return subprocess.call(cmd, env=env)
        finally:
            home.cleanup()
    keyring = start_private_keyring()
    try:
        asyncio.run(run(args.headless_only))
    except AssertionError as exc:
        print(f"FAIL  {exc}")
        return 1
    finally:
        if keyring is not None:
            keyring.terminate()
    print("\nprofile import e2e: all passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
