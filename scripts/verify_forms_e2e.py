#!/usr/bin/env python3
"""Exercise the form tools against a real installed browser.

Companion to ``verify_browser_e2e.py`` (same explicit-gate contract: temporary
profile, loopback site, not part of the unit suite). It reuses that script's
``build_tools`` so both drive the server the same way. The fixture server keeps
every form POST, so the assertions read what a site would have *received*, not
what the DOM claims.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_browser_e2e import build_tools, expect, free_port  # noqa: E402

FORM = """<!doctype html><title>forms</title><h1>Forms E2E</h1>
<form id="f" action="/save" method="post" enctype="multipart/form-data">
  <label for="kind">Kind</label>
  <select id="kind" name="kind">
    <option value="a">Alpha</option><option value="b">Beta</option>
  </select>
  <input id="hidden-later" name="later" style="display:none">
  <textarea id="body" name="body"></textarea>
  <iframe id="ed" srcdoc="<body contenteditable=true></body>"></iframe>
  <label><input type="radio" name="is_draft" value="1" checked> draft</label>
  <label><input type="radio" name="is_draft" value="0"> live</label>
  <input id="pick" name="attachment" type="file" style="display:none">
  <button id="send" type="submit">Submit</button>
</form>"""

POSTS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str) -> None:
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/":
            self.send_error(404)
            return
        self._send(FORM)

    def do_POST(self) -> None:  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        text = raw.decode("utf-8", "replace")
        POSTS.append(
            {
                "kind": _field(text, "kind"),
                "is_draft": _field(text, "is_draft"),
                "has_attachment": 'filename="e2e-upload.txt"' in text,
                "attachment_body": "e2e-upload-body" in text,
            }
        )
        self._send("<title>saved</title><h1>Saved</h1>")

    def log_message(self, *_args: object) -> None:
        pass


def _field(multipart: str, name: str) -> str:
    marker = f'name="{name}"'
    at = multipart.find(marker)
    if at < 0:
        return ""
    return multipart[at:].split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]


async def run_mode(base: str, *, headless: bool) -> None:
    mode = "headless" if headless else "headful"
    tmp = tempfile.TemporaryDirectory(prefix=f"lyra-forms-{mode}-", ignore_cleanup_errors=True)
    data_dir = Path(tmp.name)
    upload = data_dir / "e2e-upload.txt"
    upload.write_text("e2e-upload-body")
    ctx, tools = await build_tools(data_dir, headless=headless)
    print(f"\n[{mode}] data_dir={data_dir}")
    POSTS.clear()
    try:
        await tools["open_browser"]()
        entered = await tools["navigate"](url=base, reason="forms E2E", confirm=True)
        expect(entered.get("title") == "forms", "fixture page loads", entered)

        form = await tools["read_form"]()
        by_name = {f.get("name"): f for f in form.get("fields", [])}
        expect(form.get("status") == "ok", "read_form succeeds", form)
        expect(
            {"kind", "body", "is_draft", "attachment"} <= set(by_name),
            "fields listed",
            sorted(by_name),
        )
        expect(
            {o.get("value") for o in by_name["kind"].get("options", [])} == {"a", "b"},
            "select options are reported",
            by_name["kind"],
        )

        picked = await tools["select_option"](selector="#kind", label="Beta")
        expect(picked.get("selected") == "b", "select_option by label changes value", picked)
        hidden = await tools["select_option"](selector="#hidden-later", value="x")
        expect(hidden.get("status") == "hidden", "select on a hidden control is refused", hidden)
        both = await tools["select_option"](selector="#kind", value="a", label="Alpha")
        expect(both.get("status") == "error", "ambiguous choice is rejected", both)

        gated = await tools["upload_file"](selector="#pick", paths=[str(upload)])
        expect(gated.get("status") == "needs_approval", "upload asks first", gated)
        missing = await tools["upload_file"](selector="#pick", paths=[str(data_dir / "nope")])
        expect(missing.get("status") == "error", "missing path reported", missing)
        done = await tools["upload_file"](
            selector="#pick", paths=[str(upload)], reason="forms E2E", confirm=True
        )
        expect(done.get("status") == "ok" and done.get("count") == 1, "hidden picker accepts", done)
        again = await tools["upload_file"](selector="#pick", paths=[str(upload)])
        expect(again.get("status") == "needs_approval", "upload grant is one-shot", again)

        editor = await tools["set_editor"](content="framed text", selector="#ed")
        expect(editor.get("status") == "ok", "framed editor is written", editor)
        expect(editor.get("surface") == "#ed > body", "iframe body is the surface", editor)

        draft = await tools["read_draft"]()
        expect(draft.get("status") == "ok", "read_draft succeeds", draft)

        no_submit = await tools["save_draft"]()
        expect(no_submit.get("status") == "needs_submit", "save_draft needs a submit control")
        expect(not POSTS, "nothing was sent so far", POSTS)

        # Armed to publish: save_draft must refuse, and nothing may reach the site.
        page = await ctx.session.page()
        await page.evaluate("document.querySelector('input[name=is_draft][value=\"0\"]').click()")
        armed = await tools["save_draft"](submit="#send", reason="forms E2E", confirm=True)
        expect(armed.get("status") == "not_private", "armed form is not saved as draft", armed)
        await page.wait_for_timeout(250)
        expect(not POSTS, "refused save_draft sent nothing", POSTS)
        await page.evaluate("document.querySelector('input[name=is_draft][value=\"1\"]').click()")

        needs = await tools["save_draft"](submit="#send")
        expect(needs.get("status") == "needs_approval", "save_draft asks first", needs)
        expect(not POSTS, "gated save_draft sent nothing", POSTS)
        saved = await tools["save_draft"](submit="#send", reason="forms E2E", confirm=True)
        expect(saved.get("published") is False, "save_draft reports not published", saved)
        await page.wait_for_timeout(500)
        expect(len(POSTS) == 1, "exactly one request reached the site", POSTS)
        expect(
            POSTS[0]["kind"] == "b" and POSTS[0]["is_draft"] == "1",
            "posted the chosen option, still as a draft",
            POSTS[0],
        )
        expect(
            POSTS[0]["has_attachment"] and POSTS[0]["attachment_body"],
            "uploaded file bytes were posted",
            POSTS[0],
        )

        # publish is the only tool that may send a live item, and only on approval.
        await tools["navigate"](url=base, confirm=True)
        live = "input[name=is_draft][value='0']"
        held = await tools["publish"](selector=live, submit="#send")
        expect(held.get("status") == "needs_approval", "publish asks first", held)
        await page.wait_for_timeout(250)
        expect(len(POSTS) == 1, "gated publish sent nothing", POSTS)
        lone = await tools["publish"](selector=live)
        expect(lone.get("status") == "needs_submit", "publish needs both controls", lone)
        out = await tools["publish"](
            selector=live, submit="#send", reason="forms E2E", confirm=True
        )
        expect(out.get("status") == "ok", "approved publish executes", out)
        await page.wait_for_timeout(500)
        expect(
            len(POSTS) == 2 and POSTS[1]["is_draft"] == "0",
            "site received the live item",
            POSTS,
        )
    finally:
        await ctx.session.stop()
        tmp.cleanup()


async def async_main(headless_only: bool, headful_only: bool) -> None:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"
    try:
        if not headful_only:
            await run_mode(base, headless=True)
        if not headless_only:
            await run_mode(base, headless=False)
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
