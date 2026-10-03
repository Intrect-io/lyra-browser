"""Drive lyra-browser through Hermes' own MCP client, headless, end to end.

Runs under the *Hermes* interpreter, not this repo's venv:

    /path/to/hermes-agent/venv/bin/python scripts/verify_hermes_client.py \
        --server /path/to/venv/bin/lyra-browser

Everything between the model and the browser is real: Hermes spawns the server
over stdio with its whitelisted environment, registers the tools, dispatches
calls through its registry handlers, and answers elicitation with its own
``ElicitationHandler``. The single substitution is the person: Hermes routes an
elicitation to a CLI/Telegram approval prompt, and this script answers that
prompt (``request_elicitation_consent``) with a scripted accept or decline.

A throwaway HERMES_HOME keeps the run out of the real ~/.hermes. Exit status is
the verdict: 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

PAGE = b"""<!doctype html><title>hermes-e2e</title>
<body><p id="marker">lyra-browser under hermes</p>
<form method="post" action="/submit"><input name="q" value="x">
<button id="send" type="submit">send</button></form></body>"""


class _Site(http.server.BaseHTTPRequestHandler):
    posts = 0

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):  # noqa: N802
        type(self).posts += 1
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>received</p>")

    def log_message(self, *args):
        pass


class Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
        self.failed += 0 if ok else 1
        return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hermes-root", default=str(Path.home() / ".hermes" / "hermes-agent"))
    parser.add_argument("--server", required=True, help="lyra-browser executable")
    parser.add_argument(
        "--server-args",
        default="--client hermes",
        help="Arguments for the server (a control run against an older build uses --headless).",
    )
    parser.add_argument(
        "--server-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra env for the server (e.g. PYTHONPATH=<worktree>/src for a control run).",
    )
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="vb-hermes-e2e-"))
    hermes_home = scratch / "hermes-home"
    hermes_home.mkdir()
    os.environ["HERMES_HOME"] = str(hermes_home)
    sys.path.insert(0, args.hermes_root)

    site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"
    # localhost and 127.0.0.1 are different origins: a second site to be refused.
    other = f"http://localhost:{site.server_port}/elsewhere"

    import tools.approval_prompt as approval_prompt
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_schema import mcp_prefixed_tool_name
    from tools.registry import registry

    answers: list[str] = []
    asked: list[str] = []

    def scripted_person(message, description, **kwargs):
        asked.append(message)
        return answers.pop(0) if answers else "decline"

    approval_prompt.request_elicitation_consent = scripted_person

    server_env = {"HERMES_HOME": str(hermes_home)}
    for pair in args.server_env:
        key, _, value = pair.partition("=")
        server_env[key] = value
    names = register_mcp_servers(
        {
            "lyra_browser": {
                "command": args.server,
                "args": args.server_args.split(),
                "env": server_env,
                "timeout": 120,
            }
        }
    )

    def call(tool: str, **kwargs) -> dict:
        entry = registry.get_entry(mcp_prefixed_tool_name("lyra_browser", tool))
        raw = entry.handler(kwargs)
        payload = json.loads(raw)
        result = payload.get("result", payload)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                pass
        return result

    c = Checks()
    try:
        c.check(
            "hermes registered the tool surface",
            mcp_prefixed_tool_name("lyra_browser", "navigate") in names,
            f"{sum(n.startswith('mcp__lyra_browser__') for n in names)} tools",
        )

        opened = call("open_browser")
        c.check("open_browser runs headless, unattended", opened.get("attended") is False, opened)

        answers[:] = ["accept"]
        nav = call("navigate", url=base + "/", reason="e2e")
        c.check("an accepted elicitation approves the navigation", nav.get("status") == "ok", nav)
        c.check("the user was asked exactly once", len(asked) == 1, asked)

        page = call("read_page")
        c.check(
            "read_page sees the page", "lyra-browser under hermes" in page.get("text", ""), page
        )

        shot = call("screenshot")
        image = Path(shot.get("image_path", ""))
        c.check(
            "capture lands under $HERMES_HOME/cache",
            image.is_file() and image.is_relative_to(hermes_home / "cache"),
            image,
        )

        # What vision_analyze does with that path under a sandboxed backend.
        import asyncio

        from tools.image_source import ResolveContext, resolve_image_source

        os.environ["TERMINAL_ENV"] = "docker"
        try:
            resolved = asyncio.run(resolve_image_source(str(image), ResolveContext()))
            c.check(
                "hermes vision can read the capture (non-local backend)",
                resolved.data[:8] == b"\x89PNG\r\n\x1a\n",
                f"{len(resolved.data)} bytes",
            )
        except Exception as exc:  # noqa: BLE001
            c.check("hermes vision can read the capture (non-local backend)", False, repr(exc))
        outside = scratch / "outside.png"
        shutil.copy(image, outside)
        try:
            asyncio.run(resolve_image_source(str(outside), ResolveContext()))
            c.check("negative control: a path outside HERMES_HOME/cache is not readable", False)
        except Exception as exc:  # noqa: BLE001
            c.check(
                "negative control: a path outside HERMES_HOME/cache is not readable",
                True,
                type(exc).__name__,
            )
        finally:
            os.environ.pop("TERMINAL_ENV", None)

        before = _Site.posts
        answers[:] = ["accept"]
        sent = call("click", selector="#send", submits=True, reason="e2e submit")
        c.check("a declared submit is approved and sent", sent.get("status") == "ok", sent)
        c.check("the POST reached the site", _Site.posts == before + 1, f"posts={_Site.posts}")

        asked.clear()
        answers[:] = ["decline"]
        refused = call("navigate", url=other, reason="e2e decline")
        c.check(
            "a declined elicitation refuses the navigation",
            refused.get("status") == "needs_approval",
            refused,
        )
        c.check(
            "the refusal is relayed, not bounced back as 're-call with confirm'",
            "confirm=true" not in refused.get("hint", ""),
            refused.get("hint"),
        )
        answers[:] = ["decline"]
        forced = call("navigate", url=other, confirm=True)
        c.check(
            "confirm=true cannot override a person's decline",
            forced.get("status") == "needs_approval",
            forced,
        )
        where = call("get_url")
        c.check("the browser did not move", where.get("url", "").startswith(base), where)

        audit = hermes_home / "browser" / "audit.jsonl"
        rows = (
            [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        )
        channels = {
            r.get("args", {}).get("consent_channel") for r in rows if r.get("status") == "allowed"
        }
        c.check(
            "audit lives under $HERMES_HOME/browser and names elicit",
            "elicit" in channels,
            channels,
        )

        c.check("close_browser", call("close_browser").get("status") == "ok")
    finally:
        shutdown_mcp_servers()
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"\n{'ALL PASS' if not c.failed else f'{c.failed} FAILED'}")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
