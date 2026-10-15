# lyra as a Claude Code plugin

This repository is a [Claude Code plugin](https://code.claude.com/docs/en/plugins)
and its own marketplace: the root holds `.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, `.mcp.json`, `hooks/`, `skills/` and the
launcher under `scripts/claude-plugin/`. Installing the plugin gives an
interactive Claude Code session a persistent, logged-in Chromium it drives and
you can watch, with every site change, form send, upload, download and publish
asked through Claude Code's own dialog.

Measured on Claude Code 2.1.300, Linux x86_64, uv 0.11.8, 2026-10-15 unless a
line says otherwise.

## Install

```bash
claude plugin install lyra-browser --marketplace Intrect-io/lyra-browser   # 2.1.275+
```

or in two steps, which also work in a session as `/plugin ...`:

```bash
claude plugin marketplace add Intrect-io/lyra-browser
claude plugin install lyra-browser@lyra-browser
```

Then start a session. The plugin's MCP server is `plugin:lyra-browser:lyra` in
`/mcp`, and its tools are `mcp__plugin_lyra-browser_lyra__<tool>` (that is the
name to write in a permission rule or a hook matcher; the bare `lyra` never
matches a plugin server).

A browser is found at launch in this order: Chrome, Edge, then Playwright's
bundled Chromium if one was installed. With none of them, the tools answer
`browser_unavailable` and `/lyra-browser:install-browser` installs the bundled
one into the plugin's environment.

### What the first session does

Claude Code installs a plugin's Node dependencies, not Python ones, and
documents a hook that installs into `${CLAUDE_PLUGIN_DATA}` as the way to
provide them. That is what this plugin does:

- `hooks/hooks.json` runs `scripts/claude-plugin/bootstrap.sh` at
  `SessionStart`. It builds `${CLAUDE_PLUGIN_DATA}/venv` with
  `uv sync --frozen` from the committed `uv.lock` (or `python3 -m venv` plus
  `pip install` when `uv` is not on `PATH`), writes a stamp, and logs to
  `${CLAUDE_PLUGIN_DATA}/bootstrap.log`. `${CLAUDE_PLUGIN_DATA}` is
  `~/.claude/plugins/data/lyra-browser/`: it survives plugin updates and is
  removed on uninstall.
- `.mcp.json` points the MCP server at `scripts/claude-plugin/launch.sh`,
  which runs the same bootstrap (a matching stamp returns at once) and then
  `exec`s `venv/bin/lyra-browser --client claude-code`. Neither script writes to
  stdout: the launcher's stdout is the MCP transport, and a `SessionStart`
  hook's stdout goes into the model's context.
- The two share a lock directory, so a hook and a launcher starting together
  build once; the other waits, then finds the stamp.

Measured on this machine (uv's package cache warm; a cold machine adds the
download of about 100 wheels, Playwright's being the largest):

| Path | Wall time |
|---|---|
| `uv sync` first build | 0.7 s |
| Stamp matches (every later start) | 0.00 s |
| `python3 -m venv` + `pip` fallback | 15.7 s |
| Two bootstraps started at once | one builds, the other waits and exits silently |

Claude Code gives an MCP server 30 seconds to connect (`MCP_TIMEOUT`). The
documentation does not promise that `SessionStart` hooks finish before plugin
MCP servers start, so on a machine where the first build takes longer than
that, the first connection can fail while the build completes; `/mcp` →
reconnect, or the next session, then finds the environment ready.

`playwright install` is never part of the bootstrap: it takes minutes and a
system Chrome or Edge makes it unnecessary.

## Options

Claude Code prompts for these when the plugin is enabled and lists them under
`/config`. They reach the server as `LYRA_BROWSER_*` environment variables
(`.mcp.json`), so for these six keys the plugin's value replaces anything set
in your shell.

| Option | Env var it sets | Meaning |
|---|---|---|
| Trusted sites | `LYRA_BROWSER_TRUSTED_ORIGINS` | Not asked about for navigation. `https://host`, bare `host`, or `*.example.com`, comma-separated. Never covers sending, uploading, downloading or publishing. |
| Send-trusted sites | `LYRA_BROWSER_TRUSTED_SEND_ORIGINS` | Sending a form and uploading also pre-approved, for acceptance runs against your own product. A separate list. |
| Denied sites | `LYRA_BROWSER_DENIED_ORIGINS` | Refused whatever else says yes. |
| Consent timeout | `LYRA_BROWSER_CONSENT_TIMEOUT` | Seconds a dialog waits before the action counts as denied (default 300). |
| Navigation guard backend | `LYRA_BROWSER_GUARD` | `route` or `cdp`; see the README's Guard backends. |
| Headless | `LYRA_BROWSER_HEADLESS` | Unset: a window wherever one can open. On Linux with no `DISPLAY`/`WAYLAND_DISPLAY` the server is headless regardless, and reports `attended=false`. |

The consent channel is pinned to `elicit`: the server asks through MCP
elicitation and never falls back to the model's own `confirm=true`. Every
other `LYRA_BROWSER_*` variable from the README works through your shell
environment, which the server inherits.

## What a session looks like

- **Permission dialogs.** Going to a new site, and each send, upload, download
  or publish, raises an elicitation that Claude Code shows as a form with
  `approve` / `deny`. Only your `approve` lets the action through; `deny`, a
  cancelled dialog and a timeout all deny, and the model cannot answer for you.
  The audit trail (`~/.lyra-browser/audit.jsonl`) records every decision with
  `consent_channel=elicit`. A tool call waiting on a dialog is not moved to the
  background by Claude Code's two-minute rule.
- **Screenshots.** `screenshot` and `read_image` attach the PNG to their
  result, and Claude Code shows it to the model inline and saves the original
  under the session's `tool-results` directory (2.1.283+). The file is also at
  `image_path`, under `~/.lyra-browser/captures/`.
- **The window.** The `claude-code` client is headful wherever a window can
  open, so you can watch and take over (`request_takeover`, or just use the
  window). The profile is `~/.lyra-browser/profile`, the same one the `generic`
  client uses: one logged-in browser per person per machine.
- **Two sessions.** Each Claude Code session spawns its own server. The first
  holds the profile lock; a second server gets a private instance profile
  without logins, and `open_browser` reports `profile: "instance"`. Close the
  browser in one session to hand it to the other, or let it idle out
  (15 minutes by default).
- **Server instructions.** Claude Code shows a server's instructions to the
  model only up to 4,096 characters. lyra's fit under that for every client
  (`tests/test_server.py` enforces it), and the plugin's `browser` skill
  carries the longer guidance.

### Commands

| Command | What it does |
|---|---|
| `/lyra-browser:doctor` | Prints the plugin's environment, `uv`, the browser it would drive, the display, and whether the shared profile is held by another session. |
| `/lyra-browser:install-browser` | Installs Playwright's bundled Chromium into the plugin's environment, for a machine with no Chrome or Edge. |

### Headless and `claude -p`

In `claude -p` with `--permission-prompts none`, an elicitation is cancelled,
which the server reads as a denial: nothing outside the trusted sites can be
navigated to, and nothing can be sent. That is the behaviour the UAT brain
(`docs/UAT.md`) relies on, and it is why the UAT harness keeps the `generic`
client rather than this one. For an unattended run that should approve its own
declared actions, set `LYRA_BROWSER_CONSENT_CHANNEL=autonomous` in the shell
that starts Claude Code; it does not override the plugin's `elicit`, so use
the UAT spec's `target.policy: autonomous` instead.

## Develop and test

```bash
claude --plugin-dir /path/to/lyra-browser     # loads the working tree as lyra-browser@inline
claude plugin validate /path/to/lyra-browser  # manifests, marketplace, .mcp.json
pytest tests/test_claude_plugin.py -q          # the same checks plus the launcher contract
python scripts/verify_claude_plugin_e2e.py     # a real `claude -p` session through the plugin
```

`verify_claude_plugin_e2e.py` loads the working tree with `--plugin-dir` and
runs two `claude -p` sessions against the UAT demo site: one where a
`Elicitation` hook answers every consent form with `approve`, the way a person
pressing Approve would, and one with `--permission-prompts none`, where the
form is cancelled. Measured 2026-10-15 (Claude Code 2.1.300, default model,
under `xvfb-run`):

| Run | What happened | Wall time |
|---|---|---|
| approve | `open_browser`, `navigate` (one form, answered `approve`), `read_page` read "Demo home", `screenshot` came back as an MCP image block; audit `navigate … consent_channel=elicit, allowed` | 110–147 s, 4 tool calls |
| deny | `navigate` answered `needs_approval`; audit `consent_channel=denied, detail="user cancel"`, no `legacy` fallback | 11–59 s, 2 tool calls |

Two things the gate had to learn, both set in its child environment: a plugin
server connects in the background and its tools sit behind tool search, so a
one-prompt `claude -p` answered "NO_TOOLS" in 4 s until
`MCP_CONNECTION_NONBLOCKING=0` and `ENABLE_TOOL_SEARCH=false` were set; and a
`claude` started from inside a Claude Code session exits silently unless
`CLAUDECODE` and `CLAUDE_CODE_*` are removed from its environment. Interactive
sessions need neither.

`claude plugin validate --strict` warns about a `CLAUDE.md` at the plugin
root; that file is local to a checkout and not tracked, so an installed copy
has none. `tests/test_claude_plugin.py` validates a copy of the tracked tree,
which is what Claude Code installs.

Versioning: `plugin.json` pins `version`, so users stay on a version until the
string changes; `tests/test_claude_plugin.py` keeps it equal to the package
version in `pyproject.toml`. Bump both for a release, and users run
`claude plugin update lyra-browser@lyra-browser` (third-party marketplaces do
not auto-update unless the user turns it on under `/plugin` → Marketplaces).

## Troubleshooting

- **No lyra tools in the session.** `/mcp` shows `plugin:lyra-browser:lyra`
  and its state. `claude --debug` writes the server's stderr, including the
  bootstrap's messages, to `~/.claude/debug/<session-id>.txt`. The bootstrap
  log is `~/.claude/plugins/data/lyra-browser/bootstrap.log`.
- **`CLAUDE_PLUGIN_DATA is not set`.** The launcher was run outside Claude
  Code. From a shell, run `lyra-browser` from a venv of your own instead.
- **A window never appears on Linux.** `/lyra-browser:doctor` shows the
  display variables the server got. Claude Code started from a terminal with
  no desktop session passes none, and the server runs headless.
- **`profile: "instance"` and no logins.** Another lyra server holds
  `~/.lyra-browser/profile.lock`; `/lyra-browser:doctor` says so.
