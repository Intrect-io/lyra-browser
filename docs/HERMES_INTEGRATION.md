# Wiring lyra-browser into Hermes

lyra-browser is a standalone MCP server, and Hermes loads
MCP servers from `mcp_servers` in its `config.yaml` (`$HERMES_HOME/config.yaml`,
default `~/.hermes/config.yaml`). No Hermes code change is needed.

Under Hermes the browser runs **headless**: Hermes talks to the user over its
CLI, TUI or a chat gateway (Telegram, Slack, …), not at a browser window, and it
hands its stdio servers no display. Approvals still reach a person — through
Hermes' own approval prompt.

## 1. Install

```bash
/usr/bin/python3 -m venv /path/to/venv
/path/to/venv/bin/pip install -e /path/to/lyra-browser
```

The server runs in its own interpreter; Hermes only spawns it. A browser comes
from the machine: installed Chrome/Edge first, then Playwright's bundled
Chromium (`python -m playwright install chromium`) if neither exists.

**An editable install runs whatever is checked out.** `pip install -e` points the
venv at `/path/to/lyra-browser/src`, so every Hermes spawn runs the branch that
worktree has checked out at that moment — a `git switch` there changes what
Hermes runs, and restarting the servers picks up unmerged code as well as any
wrapper env change. For a Hermes you rely on, keep that worktree on `master`
(do feature work in a `git worktree add` copy), or install a release instead:
`pip install /path/to/lyra-browser` (non-editable) or a tagged build, and
re-install to upgrade.

## 2. Register in `config.yaml`

```yaml
mcp_servers:
  lyra_browser:
    command: /path/to/venv/bin/lyra-browser
    args: ["--client", "hermes"]
    timeout: 600        # per tool call; see "Timeouts" below
```

`--client hermes` is the one setting that matters. Hermes passes stdio servers
a whitelisted environment (PATH, HOME, USER, LANG, … and `XDG_*`) — not
`HERMES_HOME` and not `DISPLAY` — so the server cannot reliably tell it is under
Hermes on its own. The flag sets:

| | With `--client hermes` |
|---|---|
| Profile + audit | `$HERMES_HOME/browser` (default `~/.hermes/browser`) |
| Captures | `$HERMES_HOME/cache/browser` |
| Window | headless (`--no-headless` to show one on a desktop) |

Using a non-default Hermes profile? Pass it through, since Hermes does not:

```yaml
    env:
      HERMES_HOME: $HOME/.hermes/profiles/work
```

### Several servers on one profile

Hermes spawns a server per profile for the gateway and again for its cron worker, all
with the same `HERMES_HOME`, so they share one data dir. Chrome allows one process per
profile, so the server takes a lock on `<data_dir>/profile.lock` when it opens a
browser. The first to open keeps the saved logins (`open_browser` returns
`profile: "shared"`); the next gets a private empty profile under
`<data_dir>-instances/<pid>` and `profile: "instance"` with a note that it has no saved
logins. A server that never opens a browser holds nothing. The lock is released when the
browser closes or the process dies; dead instances' directories are swept at the next
fallback. Windows has no `fcntl`, so there the profile is not guarded.

## How approval works

Leaving a site and sending a form each need permission (see the README's
*Permissions*). lyra-browser asks over **MCP elicitation**; Hermes routes that
to its approval prompt — the CLI prompt, or a button in the chat — and the
person's accept or decline comes back over the wire. The model cannot produce
that answer, and `confirm=true` never overrides a decline.

Hermes answers an accept with no form content (its prompt is approve/deny, not
a form). lyra-browser reads that empty accept as the approval it is; a form
client that picks `deny` is still refused. The audit records both as
`consent_channel=elicit`.

A refusal comes back as `needs_approval` with a hint telling the model the user
was asked and said no — re-calling would only prompt them again.

A Hermes run with nobody to ask (cron, a gateway session without a notifier)
fails closed: Hermes declines, and the action does not happen.

## Fewer prompts: trusted origins

Every new origin costs one approval, so a run that visits the handful of sites
you use every day asks about each of them — in a chat gateway, one button per
site, and each one that goes unanswered holds the run (see *Timeouts*). Answer
those in advance with `LYRA_BROWSER_TRUSTED_ORIGINS`, set in the same `env:`
block as `HERMES_HOME` above:

```yaml
    env:
      LYRA_BROWSER_TRUSTED_ORIGINS: "kvraudio.com synthtopia.com console.cloud.google.com ads.google.com"
      LYRA_BROWSER_CONSENT_TIMEOUT: "60"
```

Entries are separated by commas or whitespace, and come in three forms:

| Entry | Trusts |
|---|---|
| `https://www.kvraudio.com`, `http://localhost:3000` | exactly that scheme, host and port |
| `kvraudio.com` | `https` on the default port, that host only — not `www.kvraudio.com`, not `http://` |
| `*.example.com` | `https` on the default port, any subdomain — **not** `example.com` itself |

A site that moves between `example.com` and `www.example.com` needs both entries
(`example.com` and `*.example.com`), because a script, a form or an HTTP redirect that moves to an
unlisted host is asked about like any other (under the default `route` guard a redirect hop is judged
after it has left, so the refusal cancels the load and the agent then asks for the destination
explicitly; `LYRA_BROWSER_GUARD=cdp` judges it before it leaves; see `enforcement.py` and the README's
*Guard backends*). Entries are compared as parsed scheme, host and port,
so `kvraudio.com.evil.io`, `kvraudio.com@evil.io` and `kvraudio.com:8443` are not
`kvraudio.com`. Anything that cannot be read as a site is dropped and the rest of
the list still loads: a lone `*`, `*.com`, a wildcard on an IP address, `file:`,
`data:`, `about:` and `blob:` URLs.

This covers **navigating to** a listed site and the clicking and typing on it
that goes with that — nothing more. `SUBMIT`, `UPLOAD`, `PUBLISH` and `DOWNLOAD`
remain single-use and are asked every time, on a listed site as much as any other;
approving one does not approve the next. A listed site is also not trusted while
you hold a takeover. The audit records what the list answered as
`consent_channel=trusted`, with the entry that matched, next to the
`consent_channel=elicit` rows for what a person answered.

## Seeing the page

`screenshot` and `read_image` return an `image_path` under
`$HERMES_HOME/cache/browser`. The model looks at it with Hermes' own
`vision_analyze` tool (`image_url=<image_path>`); the server instructions say
so. That directory is deliberate: under a sandboxed terminal backend (docker,
ssh, …) Hermes' vision reads host files only inside `$HERMES_HOME/cache` and
its siblings, so a capture anywhere else would be a file the model cannot open.

## Timeouts

Hermes waits `elicitation.timeout` (default 300s) for the person, and
lyra-browser waits `LYRA_BROWSER_CONSENT_TIMEOUT` (default 300s). One tool call
can ask twice — a click that submits a form asks for the site and for the
submission — so give the call room for both: `timeout` at least twice the
consent timeout, and `600` for the defaults. With Hermes' default of 300s, a
person who takes their time on the second prompt has the call cancelled under
them.

**An unanswered prompt is not free.** `LYRA_BROWSER_CONSENT_TIMEOUT` is how long
the tool call — and so the agent's turn — is held for an answer; only then does it
become a denial (`timed out waiting for the user` in the audit). On an unattended
gateway, where the prompt lands in a chat nobody is reading, that is five minutes
per unanswered prompt. Lower it there — `60` is a reasonable value: someone who is
at the screen still has a minute, and someone who is not costs a minute instead
of five. It changes how long the server waits, not what it asks; that is what the
trusted origins above are for.

## Headless means unattended

`open_browser` reports `attended: false`. `highlight_element`,
`ask_user_to_do` and `request_takeover` answer `unattended` rather than
pretending to reach someone at a window nobody can see. Anything that needs a
person at the browser — typing a password, a CAPTCHA, 2FA — has to happen in a
profile that is already logged in, or in an attended session.

## Verify

```bash
/path/to/hermes-agent/venv/bin/python scripts/verify_hermes_client.py \
    --server /path/to/venv/bin/lyra-browser
```

This runs lyra-browser through Hermes' real MCP client — spawn, registration,
tool dispatch, `ElicitationHandler` — against a loopback site, with a throwaway
`HERMES_HOME`. Only the person is scripted. It checks that an accept approves,
a decline refuses (and `confirm=true` cannot override it), a declared submit
reaches the site, and that Hermes' vision resolver can read the capture under a
non-local backend.
