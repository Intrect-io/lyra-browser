# Wiring lyra-browser into VEGA

lyra-browser is a standalone MCP server. VEGA loads it through its existing MCP
client (`pipeline/mcp_client.py`), which reads server definitions from the **user
data dir** `mcp.json` — not the repo's `data/mcp.json` (see the VEGA
ARCHITECTURE.md "mcp.json path" landmine).

## End user vs developer — the "no setup tax" requirement

VEGA's target user has only **VEGA.app**: no terminal, no pip, no
`playwright install`. The plugin must work for them with zero manual setup. Two
moving parts, two answers:

1. **The `playwright` Python package** — VEGA must add `playwright` to its own
   bundled runtime (`requirements.txt` / the frozen env it ships). Then `import
   playwright` works inside VEGA.app with nothing for the user to install.
   **Bundle `playwright>=1.59`.** Element refs (`read_page(mode="tree")` →
   `aria-ref=eN`, accepted by `click`/`type_text`) come from
   `aria_snapshot(mode="ai")`, which 1.58.0 lacks; on 1.58 the tree still prints
   but reports `refs: false` and the model is back to guessing selectors.
   Downloads work on 1.58 as well (the listener is per tab).
2. **The browser binary (~150MB)** — we do **not** ship or download it. The server
   reuses the user's installed **Chrome/Edge** via Playwright channels
   (`chrome` → `msedge` → bundled Chromium). Most users already have Chrome, so
   there is no download. If none is found, every tool returns:

   ```json
   {"status": "browser_unavailable",
    "user_action": "Install Google Chrome from https://www.google.com/chrome/"}
   ```

   VEGA's UI should detect this status and show a one-click "Install Chrome"
   prompt — the UI-first onboarding path, not a CLI error.

The pip + `playwright install chromium` commands below are the **developer** path
for working on this repo, never something an end user runs.

## 1. Install the server (developer / packaging step)

```bash
cd /path/to/lyra-browser
pip install -e .                      # into the same env VEGA runs in
python -m playwright install chromium # OPTIONAL — only for the bundled fallback;
                                      # skip it to rely on the user's system Chrome
```

For a shipped VEGA.app, replace this with: vendor `lyra-browser` + `playwright`
into VEGA's bundled runtime at build time. The end user installs nothing.

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
      "LYRA_BROWSER_REQUIRE_APPROVAL": "true"
    }
  }
}
```

Nothing else needs setting: `LYRA_BROWSER_ENFORCEMENT` defaults to `enforce`, so
navigations are judged against what the user approved. `observe` exists to
measure a change to that layer before switching it on and records what it would
have blocked — a navigation, or a download nobody declared — without blocking
it: do not ship with it.

### The one thing VEGA should add

`consent_channel` defaults to `auto`: the server asks the user through MCP
elicitation and only falls back to honouring `confirm` on a client that cannot
be asked. VEGA is such a client today — `pipeline/mcp_client.py` passes no
`elicitation_handler` — so every gated action is currently approved by **the
model's own assertion**, and a page telling it to "re-call with confirm=true"
gets an approval. The audit marks each of those `consent_channel=legacy` with
the reason.

Passing an `elicitation_handler` is the whole fix, and needs no change here: the
server already prefers that channel. Once it exists, approvals arrive from the
user over the wire where the model cannot forge them, and a decline can no
longer be overridden by `confirm=true`.

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
