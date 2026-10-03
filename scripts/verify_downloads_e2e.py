#!/usr/bin/env python3
"""Exercise downloads against a real installed browser.

Companion to ``verify_browser_e2e.py`` (same explicit-gate contract: temporary
profile, loopback site, not part of the unit suite; it reuses that script's
``build_tools``). The server is driven with Config defaults — enforcement on,
approval on — so where a gate is expected the call says so with ``confirm=True``.

Every assertion reads the *disk*, not the envelope: a download nobody asked for
must leave no file in the download dir and none in the browser's own temp dir, a
declared one must be there with exactly the bytes the server sent, and a hostile
file name must not put anything outside the download dir. The fixture serves a
page whose controls start every kind of download: a link to an attachment, an
``<a download>``, a blob a script made, a link that opens a popup, and a URL
navigated to directly.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

from lyra_browser.origin import parse_origin  # noqa: E402
from lyra_browser.permission import Capability  # noqa: E402

CSV = b"a,b\n1,2\n"  # 8 bytes
BIN = b"BIN\x00DATA!"  # 9 bytes
BIG = b"x" * 300_000
BLOB_TEXT = "blob-bytes"  # 10 bytes
FLIPPED = b"flipped,csv\n1,2\n"  # 16 bytes
FLIP_PAGE = (
    "<!doctype html><title>flip</title><h1>Flip</h1>"
    "<script>window.addEventListener('unload', () => {});</script>"
)

# ``window.URL``, not ``URL``: inside an inline handler the bare name resolves to
# ``document.URL`` (a string), and the download silently never happens.
HOME = """<!doctype html><title>downloads</title><h1>Downloads E2E</h1>
<a id="csv" href="/files/report.csv">report</a>
<a id="bin" download href="/files/data.bin">data</a>
<a id="trav" href="/files/trav">traversal</a>
<a id="secret" href="/files/report.csv?token=SECRET-QUERY-VALUE">secret</a>
<a id="big" href="/files/big">big</a>
<a id="endless" href="/files/endless">endless</a>
<a id="broken" href="/files/broken">broken</a>
<a id="keylink" href="/files/report.csv">keylink</a>
<a id="popup" target="_blank" href="/files/report.csv">popup</a>
<button id="blob" onclick="blobDownload()">blob</button>
<button id="plain" onclick="document.title = 'plain clicked'">plain</button>
<script>
function blobDownload() {
  const a = document.createElement('a');
  a.href = window.URL.createObjectURL(new Blob(['blob-bytes']));
  a.download = 'made-by-script.txt';
  document.body.appendChild(a);
  a.click();
  a.remove();
}
</script>"""


class Handler(BaseHTTPRequestHandler):
    # How much of the endless download the server has written: a transfer the browser
    # really stopped stops growing, one it only stopped watching would not.
    endless_sent = 0
    # How often each /flip/... path was asked for (see _flip).
    flips: dict[str, int] = {}

    def _send(
        self, body: bytes, content_type: str, disposition: str = "", no_store: bool = False
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if disposition:
            self.send_header("Content-Disposition", disposition)
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(HOME.encode(), "text/html; charset=utf-8")
        elif path == "/files/report.csv":
            self._send(CSV, "text/csv", 'attachment; filename="report.csv"')
        elif path == "/files/data.bin":
            self._send(BIN, "application/octet-stream")
        elif path == "/files/trav":
            self._send(b"evil", "text/plain", 'attachment; filename="../../evil.txt"')
        elif path == "/files/big":
            self._send(BIG, "application/octet-stream", 'attachment; filename="big.bin"')
        elif path == "/files/endless":
            self._stream_forever()
        elif path == "/files/broken":
            self._broken()
        elif path.startswith("/flip/"):
            self._flip(path)
        else:
            self.send_error(404)

    def _flip(self, path: str) -> None:
        """A page the first time and an attachment every time after.

        How a reload or a history step comes to start a download: a plain document
        cannot. ``no-store`` and the ``unload`` listener keep the page out of every
        cache, so the second visit really asks the server again.
        """
        Handler.flips[path] = Handler.flips.get(path, 0) + 1
        if Handler.flips[path] == 1:
            page = FLIP_PAGE.encode()
            self._send(page, "text/html; charset=utf-8", no_store=True)
        else:
            self._send(FLIPPED, "text/csv", 'attachment; filename="flipped.csv"', no_store=True)

    def _broken(self) -> None:
        """Promise 500 kB, send 2 kB and hang up: a transfer that cannot finish."""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="broken.bin"')
        self.send_header("Content-Length", "500000")
        self.end_headers()
        self.wfile.write(b"z" * 2000)
        self.wfile.flush()
        time.sleep(0.2)
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _stream_forever(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", 'attachment; filename="endless.bin"')
        self.end_headers()
        chunk = b"y" * 65_536
        try:
            while True:
                self.wfile.write(chunk)
                self.wfile.flush()
                Handler.endless_sent += len(chunk)
                time.sleep(0.01)
        except OSError:  # the browser hung up
            pass

    def log_message(self, *_args: object) -> None:
        pass


def audit_rows(ctx) -> list[dict]:
    lines = ctx.config.audit_path.read_text().splitlines()
    return [json.loads(line) for line in lines if line]


def download_rows(ctx, status: str | None = None) -> list[dict]:
    return [
        row
        for row in audit_rows(ctx)
        if row["tool"] == "download" and (status is None or row["status"] == status)
    ]


def live_download_grants(ctx) -> list:
    return [g for g in ctx.perms.live_grants("default") if g.capability is Capability.DOWNLOAD]


def saved_files(directory: Path) -> list[Path]:
    """Everything a person would call a download: no ledger, no half-written file."""
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if not p.name.startswith("."))


def everything_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.rglob("*")) if directory.exists() else []


def browser_temp_files(root: Path) -> set[str]:
    """Files in this run's Playwright artifacts dir: where the browser parks a download.

    ``root`` is the private TMPDIR the run gave the browser (``isolate_tmpdir``), so
    a browser some other process is running cannot put a file in this answer.
    """
    return {
        str(p) for d in root.glob("playwright-artifacts-*") for p in d.rglob("*") if p.is_file()
    }


def isolate_tmpdir(root: Path):
    """Point TMPDIR at ``root`` and return the function that puts it back.

    Playwright's driver parks downloads in ``$TMPDIR/playwright-artifacts-*``. Left at
    /tmp that dir is shared with every browser on the machine; here it is this run's
    alone, so "nothing is left in it" is a statement about this browser.
    """
    root.mkdir(parents=True, exist_ok=True)
    before = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(root)

    def restore() -> None:
        if before is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = before

    return restore


async def wait_for(check, what: str, timeout: float = 8.0):
    """Poll ``check`` (plain or async) until it returns something truthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = check()
        if inspect.isawaitable(found):
            found = await found
        if found:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


async def settled(ctx, pause: float = 0.4) -> None:
    """Let the handler finish and the browser tidy its own temp dir."""
    await ctx.downloads.idle()
    await asyncio.sleep(pause)


async def run_mode(base: str, *, headless: bool) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-downloads-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    browser_tmp = data_dir / "browser-tmp"
    restore_tmpdir = isolate_tmpdir(browser_tmp)
    Handler.flips.clear()
    ctx, tools = await build_tools(data_dir, headless=headless)

    def temp_files() -> set[str]:
        return browser_temp_files(browser_tmp)

    # Pinned, so a LYRA_BROWSER_DOWNLOAD_DIR in the environment cannot move the gate's files.
    downloads_dir = ctx.config.download_dir = data_dir / "downloads"
    site = parse_origin(base)
    origin = base.rstrip("/")
    print(f"\n[{mode}] data_dir={data_dir}")
    try:
        await tools["open_browser"]()
        entered = await tools["navigate"](url=base, reason="downloads E2E", confirm=True)
        expect(entered.get("title") == "downloads", "fixture page loads", entered)
        page = await ctx.session.page()

        # --- a download nobody asked for --------------------------------------------
        before = temp_files()
        blocked = await tools["click"](selector="#csv")
        expect(
            blocked.get("status") == "download_blocked",
            "(enforce) an undeclared download is blocked",
            blocked,
        )
        expect("download=true" in blocked.get("hint", ""), "and the reply says how to ask", blocked)
        await settled(ctx)
        expect(
            everything_in(downloads_dir) == [],
            "(enforce) and nothing reached the download dir",
            everything_in(downloads_dir),
        )
        expect(
            temp_files() - before == set(),
            "and the browser's own temp copy is gone",
            temp_files() - before,
        )
        expect(page.url == base, "the tab did not move", page.url)
        expect(len(download_rows(ctx, "blocked")) == 1, "the block is on the audit trail")

        # --- the same click, declared -----------------------------------------------
        unasked = await tools["click"](selector="#csv", download=True)
        expect(
            unasked.get("status") == "needs_approval",
            "declaring a download asks for permission first",
            unasked,
        )
        expect(everything_in(downloads_dir) == [], "and nothing is saved before the answer")

        got = await tools["click"](selector="#csv", download=True, confirm=True)
        record = got.get("download") or {}
        expect(got.get("status") == "ok" and record, "a declared download succeeds", got)
        saved = Path(record["path"])
        expect(saved.read_bytes() == CSV, "saved with exactly the bytes the server sent", saved)
        expect(
            record["filename"] == "report.csv" and record["bytes"] == len(CSV),
            "named and sized as reported",
            record,
        )
        expect(saved.parent == downloads_dir, "inside the download dir", saved.parent)
        expect(record["url_origin"] == origin, "attributed to the site it came from", record)
        expect(live_download_grants(ctx) == [], "the permission went with the file")
        expect(len(download_rows(ctx, "saved")) == 1, "and the save is on the audit trail")

        # --- the permission is single-use -------------------------------------------
        again = await tools["click"](selector="#csv")
        expect(
            again.get("status") == "download_blocked",
            "a second download without declaring it is blocked",
            again,
        )
        await settled(ctx)
        expect(
            len(saved_files(downloads_dir)) == 1, "and saved nothing", saved_files(downloads_dir)
        )
        reask = await tools["click"](selector="#csv", download=True)
        expect(
            reask.get("status") == "needs_approval",
            "an earlier approval does not cover the next download",
            reask,
        )

        # --- an <a download> and a name that is already taken -----------------------
        got = await tools["click"](selector="#bin", download=True, confirm=True)
        expect(
            got.get("download", {}).get("bytes") == len(BIN)
            and Path(got["download"]["path"]).read_bytes() == BIN,
            "<a download> saves the exact bytes (binary, NUL included)",
            got,
        )
        got = await tools["click"](selector="#csv", download=True, confirm=True)
        second = got.get("download") or {}
        expect(
            second.get("filename") == "report (1).csv",
            "a taken name gets a suffix",
            second,
        )
        expect(
            saved.read_bytes() == CSV and Path(second["path"]).read_bytes() == CSV,
            "and neither file was overwritten",
        )

        # --- a query string is not something the trail keeps ------------------------
        await tools["click"](selector="#secret", download=True, confirm=True)
        expect(
            "SECRET-QUERY-VALUE" not in ctx.config.audit_path.read_text(),
            "the audit trail names the origin, never the URL",
        )

        # --- a script-made blob -----------------------------------------------------
        got = await tools["click"](selector="#blob", download=True, confirm=True)
        made = got.get("download") or {}
        expect(
            Path(made.get("path", "")).read_text() == BLOB_TEXT
            and made.get("filename") == "made-by-script.txt",
            "a blob a script made is saved too",
            got,
        )
        expect(made.get("url_origin") == origin, "attributed to the page that made it", made)

        # --- a hostile file name ----------------------------------------------------
        files = len(saved_files(downloads_dir))
        got = await tools["click"](selector="#trav", download=True, confirm=True)
        hostile = Path((got.get("download") or {}).get("path", "/nonexistent"))
        expect(
            got.get("status") == "ok" and hostile.read_bytes() == b"evil",
            "a path-traversal file name still downloads",
            got,
        )
        expect(
            hostile.parent == downloads_dir
            and "/" not in hostile.name
            and "\\" not in hostile.name,
            "but only inside the download dir",
            hostile,
        )
        # Where "../../evil.txt" would have landed, counted from the download dir: under
        # the data dir, and the two directories above it.
        beside = [
            *data_dir.glob("**/*evil*.txt"),
            *data_dir.parent.glob("*evil*.txt"),
            *data_dir.parent.parent.glob("*evil*.txt"),
        ]
        stray = [p for p in beside if downloads_dir not in p.parents]
        expect(stray == [], "and nothing named like it exists anywhere else", stray)
        expect(len(saved_files(downloads_dir)) == files + 1, "exactly one file was added")

        # --- a URL navigated to directly --------------------------------------------
        target = base + "files/report.csv"
        before = temp_files()
        count = len(saved_files(downloads_dir))
        dropped = await tools["navigate"](url=target)
        expect(
            dropped.get("status") == "download_blocked" and dropped.get("url") == base,
            "navigating to a file without declaring it is blocked, and the tab stays",
            dropped,
        )
        await settled(ctx)
        expect(
            len(saved_files(downloads_dir)) == count and temp_files() - before == set(),
            "and it leaves nothing on disk or in the browser's temp dir",
        )
        count = len(saved_files(downloads_dir))
        fetched = await tools["navigate"](url=target, download=True, confirm=True)
        expect(
            fetched.get("status") == "ok"
            and Path(fetched["download"]["path"]).read_bytes() == CSV
            and fetched.get("url") == base,
            "navigate(download=true) saves the file and the tab stays",
            fetched,
        )
        expect(len(saved_files(downloads_dir)) == count + 1, "one file more")
        local = data_dir / "local-data.bin"
        local.write_bytes(BIN)
        opened = await tools["navigate"](url=local.as_uri(), download=True, confirm=True)
        expect(
            opened.get("status") == "ok" and Path(opened["download"]["path"]).read_bytes() == BIN,
            "and so does a file: URL",
            opened,
        )
        expect(live_download_grants(ctx) == [], "no download permission is left over")
        landed = await tools["navigate"](url=base, download=True, confirm=True)
        expect(
            landed.get("status") == "download_not_started" and landed.get("title") == "downloads",
            "declaring a download for a URL that is a page says so",
            landed,
        )
        expect(live_download_grants(ctx) == [], "and hands the permission back")

        # --- a reload and a history step that turn into a download ------------------
        # The page is served once as HTML and as an attachment every time after, so
        # reloading it (or coming back to it) asks the server again and gets a file.
        # Chrome reports that as net::ERR_ABORTED, not "Download is starting": the
        # tools used to raise here while the download was judged behind their back.
        files = len(saved_files(downloads_dir))
        cancelled = len(download_rows(ctx, "blocked"))
        await tools["navigate"](url=base + "flip/reload", reason="downloads E2E", confirm=True)
        before = temp_files()
        reloaded = await tools["reload_page"]()
        expect(
            reloaded.get("status") == "download_blocked" and "navigate" in reloaded.get("hint", ""),
            "reload_page onto a download answers download_blocked instead of raising",
            reloaded,
        )
        await settled(ctx)
        expect(
            len(download_rows(ctx, "blocked")) == cancelled + 1,
            "and the download was cancelled",
            reloaded,
        )
        expect(
            len(saved_files(downloads_dir)) == files and temp_files() - before == set(),
            "leaving nothing on disk or in the browser's temp dir",
        )
        await tools["navigate"](url=base + "flip/back", reason="downloads E2E", confirm=True)
        await tools["navigate"](url=base, reason="downloads E2E", confirm=True)
        before = temp_files()
        back = await tools["go_back"]()
        expect(
            back.get("status") == "download_blocked" and "navigate" in back.get("hint", ""),
            "go_back onto a download answers download_blocked instead of raising",
            back,
        )
        await settled(ctx)
        expect(
            len(download_rows(ctx, "blocked")) == cancelled + 2,
            "and so was that one",
            back,
        )
        expect(
            len(saved_files(downloads_dir)) == files and temp_files() - before == set(),
            "with the same clean result",
        )
        expect(page.url == base, "and the tab is where it was", page.url)

        # --- a key press ------------------------------------------------------------
        await page.evaluate("document.getElementById('keylink').focus()")
        before = temp_files()
        files = len(saved_files(downloads_dir))
        pressed = await tools["press_key"](key="Enter")
        expect(
            pressed.get("status") == "download_blocked",
            "Enter on a focused link starts a download, blocked when undeclared",
            pressed,
        )
        await settled(ctx)
        expect(
            len(saved_files(downloads_dir)) == files and temp_files() - before == set(),
            "leaving nothing on disk or in the browser's temp dir",
        )
        await page.evaluate("document.getElementById('keylink').focus()")
        pressed = await tools["press_key"](key="Enter", download=True, confirm=True)
        expect(
            pressed.get("status") == "ok" and Path(pressed["download"]["path"]).read_bytes() == CSV,
            "and saved when declared",
            pressed,
        )

        # --- a declared download that never starts ----------------------------------
        began = time.monotonic()
        nothing = await tools["click"](
            selector="#plain", download=True, confirm=True, timeout_ms=1500
        )
        took = time.monotonic() - began
        expect(
            nothing.get("status") == "download_not_started",
            "declaring a download for a control that downloads nothing says so",
            nothing,
        )
        expect(took < 6.0, f"within its own timeout, not a stall (took {took:.1f}s)")
        expect(live_download_grants(ctx) == [], "and the bought permission is handed back")

        # --- a file over the size limit ---------------------------------------------
        ctx.config.download_max_bytes = 100_000
        before = temp_files()
        files = len(saved_files(downloads_dir))
        huge = await tools["click"](selector="#big", download=True, confirm=True)
        expect(
            huge.get("status") == "download_failed" and "limit" in huge.get("reason", ""),
            "a file over the limit is refused",
            huge,
        )
        await settled(ctx)
        expect(len(saved_files(downloads_dir)) == files, "and not kept")
        expect(temp_files() - before == set(), "the browser's copy is deleted too")
        expect(len(download_rows(ctx, "too_large")) == 1, "the refusal is on the audit trail")
        ctx.config.download_max_bytes = 200 * 1024 * 1024

        # --- a download that never ends ---------------------------------------------
        async def sent_while_idle() -> tuple[int, int]:
            first = Handler.endless_sent
            await asyncio.sleep(0.4)
            return first, Handler.endless_sent

        before = temp_files()
        files = len(saved_files(downloads_dir))
        endless = await tools["click"](selector="#endless")
        expect(
            endless.get("status") == "download_blocked",
            "a download that never ends is blocked while it is still arriving",
            endless,
        )
        await settled(ctx)
        first, second = await sent_while_idle()
        expect(
            first == second > 0, "and the transfer was stopped, not left running", (first, second)
        )
        expect(temp_files() - before == set(), "with nothing of it left in the temp dir")
        ctx.config.download_timeout_s = 1.5
        # The scan has to be able to see a download for "nothing left" to mean anything.
        pending = asyncio.ensure_future(
            tools["click"](selector="#endless", download=True, confirm=True)
        )
        parked = await wait_for(lambda: temp_files() - before, "the download in the temp dir")
        expect(
            any(name.endswith(".crdownload") for name in parked),
            "the temp-dir scan sees a download while it is in flight",
            parked,
        )
        timed_out = await pending
        expect(
            timed_out.get("status") == "download_failed"
            and "still arriving" in timed_out.get("reason", ""),
            "a declared download that never ends is cancelled when its budget runs out",
            timed_out,
        )
        await settled(ctx)
        first, second = await sent_while_idle()
        expect(first == second, "and stopped", (first, second))
        expect(len(saved_files(downloads_dir)) == files, "and not kept")
        expect(temp_files() - before == set(), "the browser's copy is gone")
        expect(len(download_rows(ctx, "timeout")) == 1, "the timeout is on the audit trail")
        expect(live_download_grants(ctx) == [], "and the permission is handed back")
        ctx.config.download_timeout_s = 120.0

        # --- a transfer that cannot finish ------------------------------------------
        before = temp_files()
        files = len(saved_files(downloads_dir))
        torn = await tools["click"](selector="#broken", download=True, confirm=True)
        expect(
            torn.get("status") == "download_failed" and "interrupted" in torn.get("reason", ""),
            "a transfer the server cut short is download_failed, and says why",
            torn,
        )
        await settled(ctx)
        expect(
            len(saved_files(downloads_dir)) == files and temp_files() - before == set(),
            "and leaves nothing on disk or in the browser's temp dir",
        )
        expect(len(download_rows(ctx, "error")) == 1, "the failure is on the audit trail")
        expect(live_download_grants(ctx) == [], "and the permission is handed back")

        # --- while the user holds the session ---------------------------------------
        ctx.collab.takeover = True
        refused = await tools["click"](selector="#csv", download=True, confirm=True)
        expect(
            refused.get("status") == "takeover_active",
            "the agent's tools stand down",
            refused,
        )
        seen = len(ctx.downloads.saved())
        await page.locator("#bin").click()  # the user's own hand
        await wait_for(lambda: len(ctx.downloads.saved()) == seen + 1, "the user's download")
        expect(
            len(download_rows(ctx, "user_driven")) == 1,
            "the user's download is theirs to make, and the trail says who drove",
        )
        ctx.collab.takeover = False
        files = len(saved_files(downloads_dir))
        blocked_before = len(download_rows(ctx, "blocked"))
        await page.locator("#bin").click()  # the same click with the agent at the wheel
        await wait_for(
            lambda: len(download_rows(ctx, "blocked")) == blocked_before + 1, "the block"
        )
        await settled(ctx)
        expect(
            len(saved_files(downloads_dir)) == files,
            "and it stops the moment the agent has the wheel again",
        )

        # --- a download from a popup ------------------------------------------------
        # A popup's first request comes from a frame that does not exist yet, so the
        # guard lets it through only when the operator has approved the site; this
        # grant plays that operator. The download is judged like any other.
        ctx.perms.grant("default", site, Capability.NAVIGATE)
        files = len(saved_files(downloads_dir))
        popped = await tools["click"](selector="#popup", download=True, confirm=True)
        expect(
            popped.get("status") == "ok"
            and Path((popped.get("download") or {}).get("path", "")).read_bytes() == CSV,
            "a download from a popup is saved when declared",
            popped,
        )
        expect(len(saved_files(downloads_dir)) == files + 1, "one file more")
        expect(live_download_grants(ctx) == [], "and the permission is spent")
        await settled(ctx)
        expect(
            temp_files() == set(),
            "the browser's temp dir ends the run empty",
            sorted(temp_files()),
        )

        # --- the trail --------------------------------------------------------------
        statuses = {row["status"] for row in download_rows(ctx)}
        expect(
            {"saved", "blocked", "too_large", "user_driven", "timeout", "error"} <= statuses,
            "every outcome is on the audit trail",
            sorted(statuses),
        )
        leftovers = [p for p in everything_in(downloads_dir) if p.endswith(".part")]
        expect(leftovers == [], "and no half-written file is left behind", leftovers)
        listed = await tools["list_downloads"]()
        expect(
            listed.get("status") == "ok"
            and {d["path"] for d in listed["downloads"]}
            == {str(p) for p in saved_files(downloads_dir)},
            "list_downloads names exactly the files on disk",
            listed,
        )
    finally:
        await ctx.session.stop()
        restore_tmpdir()
        tmp.cleanup()


async def build_observing_tools(data_dir: Path, *, headless: bool):
    """``build_tools`` with enforcement on ``observe``: the operator's "record, do not block"."""
    from fastmcp import FastMCP

    from lyra_browser.approval import CollaborationState
    from lyra_browser.audit import AuditLog
    from lyra_browser.config import Config
    from lyra_browser.context import ServerContext
    from lyra_browser.session import BrowserSession
    from lyra_browser.tools import register_all

    cfg = Config(data_dir=data_dir, headless=headless, enforcement_mode="observe")
    cfg.capture_dir = data_dir / "captures"
    ctx = ServerContext(
        config=cfg,
        session=BrowserSession(cfg),
        audit=AuditLog(cfg.audit_path),
        collab=CollaborationState(require_approval=cfg.require_approval),
    )
    mcp = FastMCP("downloads-observe-e2e")
    register_all(mcp, ctx)
    return ctx, {tool.name: tool.fn for tool in await mcp.list_tools()}


async def run_observing(base: str, *, headless: bool) -> None:
    """The same downloads with enforcement on ``observe``: nothing is cancelled."""
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(
        prefix=f"lyra-downloads-observe-{mode}-", ignore_cleanup_errors=True
    )
    data_dir = Path(tmp.name)
    browser_tmp = data_dir / "browser-tmp"
    restore_tmpdir = isolate_tmpdir(browser_tmp)
    Handler.flips.clear()
    ctx, tools = await build_observing_tools(data_dir, headless=headless)
    downloads_dir = ctx.config.download_dir = data_dir / "downloads"

    def temp_files() -> set[str]:
        return browser_temp_files(browser_tmp)

    print(f"\n[{mode}, observe] data_dir={data_dir}")
    try:
        await tools["open_browser"]()
        expect(
            ctx.guard.mode == "observe" and ctx.downloads.observing,
            "(observe) the guard and the download judge are both on observe",
        )
        entered = await tools["navigate"](url=base, reason="downloads E2E", confirm=True)
        expect(entered.get("title") == "downloads", "fixture page loads", entered)
        page = await ctx.session.page()

        # --- an undeclared click ----------------------------------------------------
        got = await tools["click"](selector="#csv")
        record = got.get("download") or {}
        expect(
            got.get("status") == "ok" and record and got.get("observed") is True,
            "(observe) an undeclared download is saved, and flagged as observed",
            got,
        )
        saved = Path(record.get("path", ""))
        expect(
            saved.read_bytes() == CSV and saved.parent == downloads_dir,
            "with exactly the bytes the server sent, in the download dir",
            saved,
        )
        rows = download_rows(ctx, "would_block")
        expect(
            len(rows) == 1 and rows[0]["args"]["path"] == record["path"],
            "and audited as would_block",
            rows,
        )
        expect(live_download_grants(ctx) == [], "no permission was asked, bought or spent")
        await settled(ctx)
        expect(
            temp_files() == set(),
            "and the browser's own copy is deleted, as for a declared download",
            sorted(temp_files()),
        )
        expect(page.url == base, "the tab did not move", page.url)

        # --- a key press and a navigation -------------------------------------------
        await page.evaluate("document.getElementById('keylink').focus()")
        pressed = await tools["press_key"](key="Enter")
        expect(
            pressed.get("status") == "ok"
            and pressed.get("observed") is True
            and Path(pressed["download"]["path"]).read_bytes() == CSV,
            "(observe) a download a key press started is saved too",
            pressed,
        )
        fetched = await tools["navigate"](url=base + "files/report.csv")
        expect(
            fetched.get("status") == "ok"
            and fetched.get("observed") is True
            and Path(fetched["download"]["path"]).read_bytes() == CSV
            and fetched.get("url") == base,
            "(observe) and one a navigation started, with the tab where it was",
            fetched,
        )

        # --- a reload and a history step that turn into a download ------------------
        def flipped_saved() -> list[dict]:
            return [
                row
                for row in download_rows(ctx, "would_block")
                if row["args"].get("filename", "").startswith("flipped")
            ]

        await tools["navigate"](url=base + "flip/reload", reason="downloads E2E", confirm=True)
        reloaded = await tools["reload_page"]()
        await settled(ctx)
        expect(
            reloaded.get("status") == "ok"
            and reloaded.get("observed") is True
            and Path(reloaded["download"]["path"]).read_bytes() == FLIPPED,
            "(observe) reload_page onto a download answers with the saved file instead of raising",
            reloaded,
        )
        expect(
            len(flipped_saved()) == 1
            and Path(flipped_saved()[0]["args"]["path"]).read_bytes() == FLIPPED,
            "and the audit says would_block",
            flipped_saved(),
        )
        await tools["navigate"](url=base + "flip/back", reason="downloads E2E", confirm=True)
        await tools["navigate"](url=base, reason="downloads E2E", confirm=True)
        back = await tools["go_back"]()
        await settled(ctx)
        expect(
            back.get("status") == "ok"
            and back.get("observed") is True
            and Path(back["download"]["path"]).read_bytes() == FLIPPED,
            "(observe) go_back onto a download answers with the saved file instead of raising",
            back,
        )
        expect(
            len(flipped_saved()) == 2
            and Path(flipped_saved()[1]["args"]["path"]).read_bytes() == FLIPPED,
            "and the audit says would_block",
            flipped_saved(),
        )

        # --- what observe does not change -------------------------------------------
        ctx.config.download_max_bytes = 100_000
        files = len(saved_files(downloads_dir))
        huge = await tools["click"](selector="#big")
        expect(
            huge.get("status") == "download_failed" and "limit" in huge.get("reason", ""),
            "(observe) the size limit still applies",
            huge,
        )
        await settled(ctx)
        expect(len(saved_files(downloads_dir)) == files, "and the file is not kept")
        ctx.config.download_max_bytes = 200 * 1024 * 1024
        hostile = await tools["click"](selector="#trav")
        confined = Path((hostile.get("download") or {}).get("path", "/nonexistent"))
        expect(
            confined.parent == downloads_dir
            and "/" not in confined.name
            and "\\" not in confined.name,
            "(observe) a hostile file name is confined to the download dir as ever",
            hostile,
        )
        covered = await tools["click"](selector="#bin", download=True, confirm=True)
        expect(
            covered.get("status") == "ok"
            and "observed" not in covered
            and covered.get("download", {}).get("bytes") == len(BIN),
            "(observe) a download a permission covered is not flagged",
            covered,
        )
        expect(live_download_grants(ctx) == [], "and its permission is handed back")

        # --- the trail --------------------------------------------------------------
        await settled(ctx)
        statuses = {row["status"] for row in download_rows(ctx)}
        expect(
            "blocked" not in statuses and {"would_block", "saved", "too_large"} <= statuses,
            "(observe) nothing was cancelled, and every outcome is on the audit trail",
            sorted(statuses),
        )
        expect(
            temp_files() == set(),
            "(observe) the browser's temp dir ends the run empty",
            sorted(temp_files()),
        )
    finally:
        await ctx.session.stop()
        restore_tmpdir()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool) -> None:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    try:
        if not headful_only:
            await run_mode(base, headless=True)
            await run_observing(base, headless=True)
        if not headless_only:
            await run_mode(base, headless=False)
            await run_observing(base, headless=False)
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--headless-only", action="store_true")
    group.add_argument("--headful-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(async_main(args.headless_only, args.headful_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
