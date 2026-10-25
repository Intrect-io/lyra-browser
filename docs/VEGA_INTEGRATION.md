# Wiring lyra-browser into VEGA

lyra-browser is a standalone MCP server. VEGA loads it through its existing MCP
client (`pipeline/mcp_client.py`), which reads server definitions from the **user
data dir** `mcp.json` — not the repo's `data/mcp.json` (see the VEGA
ARCHITECTURE.md "mcp.json path" landmine).

## End user vs developer — the "no setup tax" requirement

VEGA's target user has only **VEGA.app**: no terminal, no pip, no
`playwright install`. The plugin must work for them with zero manual setup. Two
moving parts, two answers:

1. **Playwright is no longer needed.** The default driver (`auto` → `native`) drives
   the installed browser over raw DevTools, so VEGA does not have to bundle
   `playwright` at all, and element refs (`read_page(mode="tree")` → `aria-ref=eN`,
   accepted by `click`/`type_text`) come from Chrome's own accessibility tree rather
   than from `aria_snapshot`. Until this change VEGA bundled `playwright>=1.59`
   because 1.58.0 answers `refs: false`; that floor and the bundling step both go
   away with the native driver.
2. **The browser binary (~150MB)** — we do **not** ship or download it. The server
   drives the user's installed **Chrome/Edge** directly. Most users already have
   Chrome, so there is no download. If none is found, every tool returns:

   ```json
   {"status": "browser_unavailable",
    "user_action": "Install Google Chrome from https://www.google.com/chrome/"}
   ```

   VEGA's UI should detect this status and show a one-click "Install Chrome"
   prompt — the UI-first onboarding path, not a CLI error.

**This is the default as of 0.2.0.** `auto` picks `native` whenever a browser is
installed and only falls back to Playwright on a machine with none; pinning is still
possible with `"env": {"LYRA_BROWSER_DRIVER": "native"}`. It runs with the `cdp` guard,
keeps Chrome's sandbox on and never sends `Runtime.enable`; see the README's "Drivers".
`playwright` is now an extra (`lyra-browser[playwright]`), not a dependency.

The commands below are the **developer** path for working on this repo, never
something an end user runs.

## 1. Install the server (developer / packaging step)

```bash
cd /path/to/lyra-browser
pip install -e .                      # into the same env VEGA runs in — no Playwright
```

For a shipped VEGA.app, replace this with: vendor `lyra-browser` into VEGA's bundled
runtime at build time. Nothing else is downloaded or bundled; the end user installs
nothing beyond having Chrome or Edge.

## 2. Register in the user data dir `mcp.json`

`mcp.json` lives under VEGA's user data dir (`~/Library/Application Support/VEGA/`
or `$VEGA_DATA_DIR`). VEGA's loader supports `stdio` (command + args + env).

### stdio (recommended)

```json
{
  "lyra_browser": {
    "command": "lyra-browser",
    "args": [],
    "env": {
      "LYRA_BROWSER_REQUIRE_APPROVAL": "false"
    }
  }
}
```

Nothing else needs setting: `LYRA_BROWSER_ENFORCEMENT` defaults to `enforce`, so
navigations are judged against what the user approved. `observe` exists to
measure a change to that layer before switching it on and records what it would
have blocked — a navigation, or a download nobody declared — without blocking
it: do not ship with it.

### How approvals answer (settled 2026-10-25)

VEGA's default is **asking nobody**: `data/mcp.json` sets
`LYRA_BROWSER_REQUIRE_APPROVAL=false` (consent channel `off`, audited as `off`), and
every `lyra_browser__*` tool carries a permission level, so VEGA itself does not card
them either (vega-agent PR #896). What still guards the browser: the navigation guard
(`LYRA_BROWSER_ENFORCEMENT=enforce`), `denied_origins`, refusal of opaque origins, and
takeover.

Asking a person is opt-in, and it works: set `LYRA_BROWSER_REQUIRE_APPROVAL=true` and
`LYRA_BROWSER_CONSENT_CHANNEL=elicit`, and VEGA's `pipeline/mcp_elicitation.py` turns
each request into a consent card on the turn's loop. A decline arrives over the wire
and cannot be overridden by `confirm=true`.

If `lyra-browser` is not on PATH, use the interpreter form:

```json
{
  "lyra_browser": {
    "command": "python",
    "args": ["-m", "lyra_browser"],
    "env": {}
  }
}
```

### http (dev / remote)

Run `lyra-browser --http --port 8765`, then point VEGA at the URL; VEGA infers
the transport from the URL.

```json
{
  "lyra_browser": { "url": "http://127.0.0.1:8765/mcp" }
}
```

### Letting the model see the page

`screenshot` and `read_image` return an `image_path`, not pixels. With
`VEGA_DATA_DIR` set the file lands under `$VEGA_DATA_DIR/uploads/browser`, which
is inside the root `pipeline/image_history.py` already trusts. What VEGA still
has to do — the same hook serves the in-app browser once it captures — is, in
`mcp_client.py`, to notice a tool result carrying `image_path` + `mime_type:
image/*` and attach it to the next model input as a `vega_image_path` block, the
way an uploaded image is. Today the result is text only, so the model reads the
path and never sees the picture.

## 3. Verify

On VEGA startup (or first turn), `mcp_client.init_mcp_tools()` merges the server's
tools into `TOOL_SCHEMAS` prefixed by the server name, e.g.
`lyra_browser__open_browser`, `lyra_browser__navigate`, ...

Ask VEGA to open the browser and read a page; confirm a visible Chromium window
appears and `audit.jsonl` (under the data dir) gains entries.

## Notes

- The window shares the persistent profile in `<data_dir>/profile`, so logins
  persist. Keep that dir private.
- `VEGA_DATA_DIR` is honoured: if VEGA already sets it, the browser profile and
  audit land under `$VEGA_DATA_DIR/browser` automatically.
- For remote/CE channels, gate exposure on the VEGA side: browser control grants
  real local action, so it should stay out of the CE allowlist unless you mean it.
