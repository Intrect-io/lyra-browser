# lyra

A **headful, collaborative browser** MCP server for AI agents. The agent drives a
single **visible Chromium window** that the user can watch and **take over** at any
time — co-browsing, not headless scraping.

The same server runs **headless** when nobody sits at a window — for example under
Hermes, where the user is reached through chat (see
[docs/HERMES_INTEGRATION.md](docs/HERMES_INTEGRATION.md)), or as a remote server (see
[docs/CLOUDFLARE.md](docs/CLOUDFLARE.md)).

> **Names.** The project is **lyra**; the repository is `lyra-browser`. The command
> (`lyra-browser`), the Python package (`lyra_browser`) and the environment variables
> (`LYRA_BROWSER_*`) keep the `lyra-browser` names for now. The UAT runner is `lyra-uat`.

> Built on Playwright (headful, persistent profile) and exposed over
> [FastMCP](https://github.com/jlowin/fastmcp), so any MCP client can use it with a
> single entry in its configuration.

## Why

An agent that is local-first and model-agnostic needs a browser the user shares: the
agent reads the page, clicks, and types, while the user can step in for the things an
agent must not do alone (passwords, CAPTCHAs, 2FA, payments) and grab full control
whenever they want. Every action is written to an append-only audit trail.

## Architecture

```
agent loop ──(MCP: stdio/http)──> lyra-browser server ──> Playwright ──> visible Chromium window
                                        │                                      ▲
                                        ├─ audit.jsonl (every action)          │ user watches / takes over
                                        └─ approval + takeover gating ─────────┘
```

- **Standalone MCP server** — registered in the MCP client's configuration; the client
  needs no code change.
- **Python + Playwright** — pins are `fastmcp>=3.2` and `playwright>=1.59`. Element refs
  (`aria_snapshot(mode="ai")`) need 1.59; see [Addressing an element](#addressing-an-element-aria-ref).
- **Dedicated headful window** — `launch_persistent_context(headless=False)` with a
  persistent profile, so logins survive across sessions. One server at a time holds
  that profile (an `flock` on `<data_dir>/profile.lock`, taken when the browser opens,
  not when the server starts). A second server on the same data dir opens a private,
  empty profile at `<data_dir>-instances/<pid>/profile` instead of failing, and
  `open_browser` says so (`profile: "instance"`, "no saved logins in this instance").
- **No setup tax** — reuses the user's **installed Chrome/Edge** by default
  (`channel="chrome"` → `"msedge"` → bundled Chromium), so `playwright install` is not
  needed when either is present. If no browser is found, tools return a
  `browser_unavailable` envelope that the client can turn into an install prompt.
- **Full preset** — human-in-the-loop (highlight, ask-the-user, approval gate),
  takeover/handoff, bot-check detection, audit trail, CI + lint + pre-commit.

## Tools

| Group | Tools |
|---|---|
| Navigation | `open_browser`, `navigate`, `go_back`, `reload_page`, `close_browser` |
| Tabs | `tabs` |
| Waiting | `wait_for` |
| Interaction | `click`, `type_text`, `press_key`, `hover`, `scroll`, `handle_dialog` |
| Downloads | `list_downloads` |
| Forms | `read_form`, `select_option`, `set_editor`, `upload_file`, `read_draft`, `save_draft`, `publish` |
| Reading | `get_url`, `read_page`, `screenshot`, `read_image` |
| Collaboration | `highlight_element`, `ask_user_to_do`, `request_takeover`, `resume_after_takeover` |

### Filling a real form

`click` and `type_text` cover what every page has, but a form on a real site needs
more. Each of these was measured on KVR's developer forms before it was
written:

- `read_form` — a form's `name`, `type`, `label` and `<select>` options.
  `read_page` returns `innerText`, so a 188-field product form arrives as a wall of
  labels with nothing to address. Hidden inputs are counted, not dumped. Every
  field carries `usable` — false means it is in the DOM but hidden or disabled
  right now, which is how a form keeps a section it has not revealed (measured on
  KVR: `event_country` sits in the Deal/Offer block). Check `usable` instead of
  finding out through a 30s locator timeout.
- `select_option` — a `<select>` is not a click target. Chooses by `value`,
  `label` or `index`, and fires `change` the way a page listens for it. Pass
  `submits=true` when the choice posts. Answers `hidden` or `disabled` by name
  when the control cannot be used as it stands.
- `set_editor` — CKEditor/TinyMCE keep the editable body in an `iframe` and leave
  the `<textarea>` the form posts hidden and empty, so `type_text` on that
  textarea does nothing. `html=true` writes markup and syncs the editor's own
  data. Leave `selector` empty to use the first editor on the page.
- `upload_file` — attaches local files to an `input[type=file]`. A styled picker
  usually keeps it hidden, which is fine. Missing paths are reported before
  anything is attached.
- `save_draft` — sends the form while keeping the item private. It names the
  control that sends (`submit`, from `read_form`'s `submitText`), first confirms
  the publish control is still on its private setting and refuses otherwise, and
  asks for `SUBMIT` but never `PUBLISH` — so it cannot release an item, and a
  page left armed by anything else will not slip out through it.
- `read_draft` — everything a human needs to approve an item, in one answer: the
  filled fields, each editor's real content (a separate document `read_page` never
  shows), the attachment slots, the submit control, and whether the item is a
  draft or already public. A read, so it needs no approval.
- `publish` — the one action that sends an item to an audience. Kept apart from
  every other tool so that writing a draft can never release it: it asks for
  `PUBLISH`, which nothing implies, and it is the only tool that does. A site
  splits the act across two controls and means neither alone — measured on KVR,
  the publish radio only arms the form and the item travels on the form's own
  Submit — so it takes both `selector` (the live control) and `submit` (the one
  that sends). Giving only the first is refused rather than left half-done:
  arming a form with no way to send it would leave the page primed to publish on
  someone else's next click.

### Drafts and release

Writing an item and releasing it are different acts, and the tool surface keeps
them apart. Every writing tool can be driven to completion with the item still
private; `publish` is the only way out, and it is `PUBLISH`-gated and single-use.
The intended flow is: fill the form → `read_draft` → show the human → only then,
on an explicit yes, `publish`.

On KVR this matches the page: a new item opens on `Draft` and stays there until
the publish radio is switched, so a form that is merely filled is not visible to
anyone. A form sent without the publish control set saves as a draft — which also
means every writing tool must stay away from Submit, since KVR has no separate
"save draft" button. That is why `publish` sends the form itself rather than
leaving the send to `click`.

`UPLOAD` and `PUBLISH` are single-use: a file handed to a page, and content shown
to an audience, cannot be recalled.

## Reading and acting on a page

### Reading: `read_page`

`read_page` has two modes, both read-only (no approval, works during a takeover):

- `mode="text"` (default) — the rendered text (`innerText`), not HTML.
  `links=true` adds the visible links as `{text, href}` (absolute, de-duplicated,
  at most 100, with `links_truncated`).
- `mode="tree"` — one line per thing that can be acted on (links, buttons,
  inputs, selects, checkboxes, tabs, menu items), each with a ref:

  ```
  aria-ref=e12 link "Pricing" -> /pricing
  aria-ref=e15 button "Save"
  aria-ref=e16 checkbox "Remember me" [checked]
  ```

  Input values are never shown. The reply carries `elements` and `refs`, plus
  `in_viewport` (on-screen elements come first) on Playwright 1.60+.

`selector` limits either mode to one region (first match; `not_found` at once
when nothing matches). `offset` and `max_chars` page through long output:
`total_chars` is the full length, `truncated` says more follows and `next_offset`
is where to continue. Tree pages break on whole lines, so a ref is never cut in
half. A reply that finds a bot check also carries `challenge` and a `hint`; see
[Bot checks](#bot-checks-challenge).

### Addressing an element: `aria-ref`

`read_page(mode="tree")` → pass the token `aria-ref=eN`, exactly as written, as
the `selector` of `click` or `type_text`. A ref reaches what a CSS or text
selector cannot name: icon-only links, several links with the same text, elements
inside open shadow roots and iframes. A ref is a handle into the *latest* tree
read, not a query over the live page: a newer read (a region read included), a
navigation or a removed frame ends it, and so does the element being removed. A
stale ref answers `not_found` (or `hidden` for an element that is gone but still
counted) with a hint to read the tree again; refs get no waiting window, because
they cannot appear later.

**Requires `playwright>=1.59`.** Refs come from `aria_snapshot(mode="ai")`, which
1.59 introduced; `pyproject.toml` still floors at 1.58 so that existing 1.58
installations keep working. On 1.58 the tree still works but answers `refs: false`
and its lines carry no ref — address elements with `role=link[name="Pricing"]` or
`text=` selectors instead.

### When an action cannot be done: `click` / `type_text`

Both fail fast and explain, instead of holding the call for Playwright's 30s:

| `status` | Meaning |
|---|---|
| `not_found` | Nothing matches the selector, even after up to ~3s for a page that builds its controls late |
| `hidden` / `disabled` | The match is in the page but stayed invisible / disabled for that window (present-but-not-ready controls are re-checked until ready, so a button enabled 0.8s after load still works) |
| `timeout` | The action outran `timeout_ms`. The click may have landed (a navigation it started can still be loading) — look at `get_url`/`read_page` before repeating it |
| `element_not_actionable` | Covered by another element (the hint names it), detached, read-only, not a text field, unstable or outside the viewport |
| `page_closed` | The tab died under the call (a popup that closed itself, a closed window) |

Each carries `selector`, `url` and a `hint` with the next move. `timeout_ms`
(default 10000, at most 30000; 0 is raised to 1ms, never "no limit") bounds the
action itself. The usability check runs *before* permission is asked, so an
unusable control neither prompts nor leaves a single-use grant behind; the same
driver failures during the action are converted into these envelopes (and
audited) rather than raised. A successful `click` also reports `matches` (how
many elements the selector hit — the first is clicked) and `clicked`
(`tag`, `role`, `name` of what was hit), with a hint when `matches > 1`.

### What an action set off: `blocked_by_policy`, `new_tab`

`click`, `type_text`, `press_key` and `hover` answer when the driver reports the action
done, and two things can still be true of that moment:

- **The guard refused a navigation the action caused** (a link to a site no approval
  covers, a form sent without `submits=true`, a page that redirected itself). The 204
  leaves the tab where it was, so the reply used to read `ok` with the same `url` and
  the agent had no idea why nothing happened — the audit trail was the only account.
  It is now `blocked_by_policy` with `url` (where the tab stands), a `hint` and, for a
  refused redirect, `redirected_to`; the fields the reply already had (`matches`,
  `clicked`, `dialogs`) are kept, and the audit row of the action carries the same
  status. The hint is the way out: `navigate` to the destination (that asks the user),
  or repeat the action with `submits=true` (`submit=true` on `type_text`) when it was
  meant to send a form. A file the call saved stays `ok` with its `download`, and a
  download the browser started and the call cancelled keeps `download_blocked`. In
  `observe` mode nothing is refused, so the reply stays `ok` and the audit says
  `would_deny`.
- **The action opened a tab** (`target=_blank`, `window.open`). The session follows
  onto it while `url` is still the page the action was made on, so the reply carries
  `new_tab: true` and `tab_count`, and a hint to use `tabs`. `get_url` and
  `open_browser` carry `tab_count` too. A popup whose first navigation the guard
  refused opens no tab at all, and the reply is `blocked_by_policy` instead.

Neither costs a click a wait of its own. The driver holds a click until the navigation it
started has been judged, so that verdict is in before the call returns (0.5 to 48 ms
before, measured over playwright and patchright, both guard backends, headless and
headful). What it does not hold for lands 3 to 22 ms after — a popup's tab, a form posted
from a frame, a page's own `setTimeout(0)` — inside the `download_settle_s` window every
click and key press already listens for. `hover` and `type_text` have no such window, so
they linger up to 40 ms after the driver returns, and only while nothing has shown (their
navigation reaches the guard 1 to 8 ms after they return); a page timer that fires after
that is only on the audit trail. Numbers in `scripts/verify_click_refusal_e2e.py`
(`INFO` lines).

### Waiting: `wait_for`

`wait_for` holds until the page is ready instead of a sleep — or a click used to
pass time. Give exactly one condition: `text` (in the visible text `read_page`
shows; `state="hidden"` waits for it to vanish), `selector` (reaches `state`:
`visible`, `hidden`, `attached`, `detached`), `url` (a glob over the whole URL,
e.g. `**/checkout**`) or `load_state` (`load`, `domcontentloaded`,
`networkidle`). It waits up to `timeout_ms` (default 10000, capped at 30000) and
returns `ok` with `waited_ms`. Running out of time is an answer, not an error:
`{"status": "timeout", "waited_ms", "last_seen"}` with a short page excerpt (the
URL for a `url` wait). Bad arguments return `error`. It only observes: no
approval, and it keeps working while the user holds a takeover.

### Tabs: `tabs`

The session follows onto any tab a page opens (`target=_blank`, `window.open`),
so the agent is never left reading the page that opened it. A `click` or
`press_key` that opened one says `new_tab: true` and `tab_count`, and `get_url` and
`open_browser` carry `tab_count`. `tabs` covers the rest:

- `action="list"` (default) — `tabs: [{index, url, title, active}]` plus
  `tab_count` and `active_index`. A read; a tab stuck in a script loop is listed
  without a title rather than hanging the list.
- `action="switch"`, `index=N` — make that tab the one to read and act on, and
  bring it to the front so the user sees what the agent sees.
- `action="close"`, `index=N` — close it. Closing the active tab returns to the
  tab that opened it, else the newest one; closing the last tab leaves a blank one
  (closing headful Chrome's last window would quit the browser).

Indexes shift whenever a tab opens or closes, so list again before using one; a
bad index answers `not_found` with `tab_count`. Switching and closing are
mutations: they stand down for a takeover, are audited, and ask for `INTERACT`
on the **tab's own site**, not the active page's — being approved for one site is
no licence to read or close another's. A tab that closes itself (a sign-in popup)
sends the session back to its opener; if the active page is ever closed, the next
tool moves onto the opener, else the newest tab, else a fresh blank one, instead
of failing on a dead page.

### What `navigate` reports

`ok` means the navigation happened, not that the page is good. `navigate`,
`go_back` and `reload_page` report `http_status` (404, 500 …) and `content_type`
(media type only, lower-cased, e.g. `application/pdf`) of the document that
answered; both are `null` when no HTTP response was involved (`about:blank`,
`data:`, a page the browser restored from history). `wait_until` chooses how much
of the load to wait for: `domcontentloaded` (default), `load`, `commit` or
`networkidle` (which never settles on a page that keeps polling); anything else is
an `error` before any prompt. A page that draws itself after loading needs
`wait_for`. A URL that turns out to be a file download does not move the tab: see
[Downloads](#downloads) for `download=true` and `download_blocked`. Only when the
driver announced a download the browser never reported does it answer
`{"status": "download_started", "url", "requested_url", "hint"}`; this server then wrote
no file to the download dir. If the page that answered is a bot check, `navigate`
answers `status: "challenge"`; see [Bot checks](#bot-checks-challenge).

### Bot checks: `challenge`

A page that asks "are you a robot" is not a failure of the tool, and it is not a page
to get past. It is a page a person has to answer. lyra names the check and stops:

- `navigate` answers `status: "challenge"` when the page it landed on is a bot check,
  and `read_page` adds a `challenge` field (the text is still returned, so the agent
  can see what it is).
- The reply carries `challenge: {vendor, kind, evidence}`. `kind` is `interactive`
  (a person has to solve it: a checkbox, a puzzle, press-and-hold) or `blocked` (refused
  outright, e.g. an HTTP 403 "Access Denied"). `vendor` is `cloudflare`, `datadome`,
  `recaptcha`, `hcaptcha`, `perimeterx`, or `unidentified` when the text names a check
  without a known host.
- The `hint` depends on who is watching. With a person at the window it says to ask them
  (`ask_user_to_do`, then `read_page` to see whether it cleared). With nobody there it
  says not to wait and not to look for another way past, to report the task as blocked
  (`finish` with `outcome=blocked`) and carry on with what remains.

Nothing solves, waits out or retries a check. Detection reads the frame hosts, the page
title and the first 3000 characters of visible text; an article that quotes a check's
wording further down is not named. A detection in `navigate` is audited as
`challenge_seen`; `read_page` only reports it in its reply.

### Hover and scroll

- `hover` — move the pointer over an element, for menus and tooltips that open only
  while it is on their trigger: `hover`, then `click` the item that appears. Nothing
  is pressed, so no form is sent. Fails fast like `click` (`not_found`, `hidden`,
  `disabled`, `timeout`, `element_not_actionable`, `page_closed`, `timeout_ms`) and
  reports `matches` and `hovered` (`tag`, `role`, `name`). Needs `INTERACT`.
- `scroll` — give exactly one of `to` (`top`/`bottom`), `by_y` (pixels, negative up,
  at most 20000 per call, done with the mouse wheel) or `selector` (bring it into
  view). The reply is `scroll_y`, `scroll_height` and `at_bottom` once the position
  has settled. It moves the **window** only: a feed or panel that scrolls inside its
  own container needs `scroll(selector=...)` on an element inside it, and a scroll
  that moved nothing says so. Lazy lists grow after a scroll, so repeat
  `scroll(to='bottom')` until `at_bottom` is true.

### Downloads

A file a page hands to the browser is kept **only when the call declared it**.
Chromium accepts a download the moment a page offers one, and the navigation guard
judges requests, not responses, so this is a separate gate on the file as it
arrives:

- `download=true` on `click`, `press_key` or `navigate` declares it and buys a
  single-use `DOWNLOAD` grant (asked like any other, never implied by a trusted
  origin) inside that call. The grant pays for exactly one file and is retired when
  the call stops waiting. The reply carries `download`:
  `{filename, path, bytes, url_origin}`.
- An **undeclared** download is cancelled and deleted before anything reaches the
  download dir, and the call answers `download_blocked` (`blocked_download`:
  `filename`, `url_origin`) — repeat it with `download=true`. A declared call that
  produced no file answers `download_not_started`; one whose file was not kept
  answers `download_failed` with a `reason` (`too_large`, `timeout`, `error`).
  `go_back` and `reload_page` cannot declare one; their hint points at `navigate`.
  Chrome reports a reload or history step that turns into a download as
  `net::ERR_ABORTED` (not "Download is starting"); an abort that a download follows
  within 2 s is answered as that download, and any other abort is still an error.
- With `LYRA_BROWSER_ENFORCEMENT=observe` ("record, do not block") nothing is cancelled
  either: an undeclared download is **saved** like a declared one — same name rules,
  caps and ledger — and answered as `download` plus `observed: true`, audited as
  `would_block`. Observe never spends a grant (like the navigation guard), so it
  under-reports a second file in one call that enforce would have cancelled. A
  download a `DOWNLOAD` grant covered is audited `saved` and is not flagged.
- The site picks the file name, so it is treated as hostile: only the last path
  component survives, control and right-to-left-override characters, characters
  Windows forbids, leading dots and reserved device names are removed, long names
  are cut keeping their extension, and a taken name becomes `name (1).ext`. Nothing
  is ever written outside `LYRA_BROWSER_DOWNLOAD_DIR`.
- Size and time are capped (`LYRA_BROWSER_DOWNLOAD_MAX_BYTES`,
  `LYRA_BROWSER_DOWNLOAD_TIMEOUT`), and the dir is pruned oldest-first to
  `LYRA_BROWSER_DOWNLOAD_KEEP` using a ledger, so files that are not this server's
  are never deleted.
- While the user holds a **takeover** the gate stands aside and their own
  downloads are saved, recorded as `user_driven`, as with navigation.
- `list_downloads` — the files this session saved (`filename`, `path`, `bytes`,
  `url_origin`), oldest first, plus `download_dir`. A read: no approval, works
  during a takeover, does not open the browser. Blocked and failed downloads are
  not listed.

The listener is **per tab** (popups included), not `BrowserContext`'s `download`
event: that only exists from Playwright 1.60 while this project supports 1.58,
where a context listener would never fire and every download would go unjudged.

### Native dialogs

`alert`, `confirm`, `prompt` and `beforeunload` stop a page until answered, and a
listener that leaves one open freezes the tab. So one context-level listener
**always answers** the moment a dialog appears. The default is what a browser with
no listener does: alerts and `beforeunload` are accepted, `confirm` is dismissed
(`false`) and `prompt` is dismissed (`null`).

- What was raised is reported as `dialogs` (`type`, `message`, how it was answered)
  in the reply of the `click`, `type_text` or `press_key` that raised it — only
  present when there were any. The message is the page's text, not the user's. The
  form tools (`save_draft`, `publish`) use the same handler and fold dialog text
  into their `errors`.
- `handle_dialog(accept, text)` chooses the answer for the dialog **the next browser
  action raises**: call it *immediately before* that action. `accept=false` answers
  no; `text` is what a `prompt` receives (empty accepts the default). Like a one-shot
  grant it belongs to that one call — `click`, `type_text`, `press_key`, `hover`,
  `scroll`, `navigate`, `go_back`, `reload_page`, `select_option`, `set_editor`,
  `upload_file`, `save_draft`, `publish` or a `tabs` switch/close — and ends with it,
  used or not, even when the call was refused (arm again before retrying). A dialog
  raised after it, by a page's own timer or on a page reached later, gets the default,
  so a yes armed for one click cannot answer a later `Delete this?`. Reading
  (`read_page`, `screenshot`, `get_url` …) does not use it up, and about 60s is the
  most it ever waits. It asks nobody (the click that raises the dialog was already
  asked for), stands down for a takeover, is audited, and the prompt text is never
  written to the audit log.
- Nothing here judges the answer: accepting a `confirm` that goes on to post a form
  is still stopped where the request leaves unless it was declared (`submits=true`).

## Seeing the page

`screenshot` (the viewport or the full page) and `read_image` (one element —
an `img`, a `canvas`, an `svg`, a chart) write a PNG to disk and return its
**path**:

```json
{"status": "ok", "image_path": "…/captures/page-20260917T123557Z-fd1a0721.png",
 "mime_type": "image/png", "width": 1280, "height": 800, "bytes": 115889}
```

The file is the handoff. Measured on the same client: a tool result is shown to the
model as its text blocks, so a base64 PNG inside the JSON arrives as tens of
thousands of tokens of prose and no picture. A path costs nothing, and a client that
can read the server's files attaches the file to the model's next turn as an image.
Captures go to `<data_dir>/captures` unless `LYRA_BROWSER_CAPTURE_DIR` says otherwise.
`inline=true` adds the base64 for a client with no access to this filesystem.

Captures are written atomically and pruned oldest-first
(`LYRA_BROWSER_CAPTURE_KEEP`, default 200), so an agent that looks every turn
does not fill the disk. The audit records the path and size, never the pixels.
`read_image` fetches nothing — it captures what the element renders, so no
request leaves the browser and nothing is gated.

## Permissions

The agent declares what it intends; the browser is judged on what it actually
does. Those are separate layers because a tool cannot know what a page will do
with a click — `onclick="form.submit()"` submits from a control that looks inert,
an `input` listener submits while the agent only typed, and an element's type can
change between being checked and being clicked.

**Grants** are `(origin × capability)` and short-lived:

| Capability | Meaning |
|---|---|
| `NAVIGATE` | Load a document from this origin. Implies `INTERACT` on it |
| `INTERACT` | Click and type while staying on this origin |
| `SUBMIT` | Send a non-idempotent request. **Single-use** |
| `UPLOAD` | Give a file to a page (a picker, a drop target). **Single-use** — requested by `upload_file` |
| `PUBLISH` | Make content publicly visible — an item an audience can see. **Single-use** — requested by `publish`, implied by nothing |
| `DOWNLOAD` | Write a file to disk. **Single-use** — requested by `click`, `press_key` or `navigate` with `download=true`; the grant pays for one file and ends with the call |

Arriving on a site carries permission to use it, so ordinary clicking is not
re-asked. Sending a form and leaving for another site are separate decisions and
are asked — declare them with `submits=true` (`submit=true` on `type_text`).

**Enforcement** watches outgoing navigations at the request, so it catches a
submission or an escape whatever produced it. A refusal answers with HTTP 204
rather than aborting, which leaves the page standing so the agent can recover.
URLs with no host — `file:`, `data:`, `javascript:` — are never "the same site"
and are asked every time. Where the guard stands — Playwright's route, or a second
DevTools connection that also sees redirect hops — is a choice, see
[Guard backends](#guard-backends).

Where the line falls, measured rather than asserted:

- **A same-origin GET navigation is interaction, not a submission.** A search box
  puts what you typed in the query string; gating that would gate every search
  box and every link with parameters. `SUBMIT` means a body or a mutating method.
- **A submission is authorised by the origin that sends it**, not by wherever the
  form points. Cross-origin form actions are ordinary — payment handlers, SSO,
  third-party endpoints — and no tool can read a form's `action` before the click
  without the TOCTOU this layer exists to avoid. The destination is recorded in
  the audit under the label `cross_origin_send` — an audit key, not a capability,
  and never purchased.
- **A page that moves itself to another host is leaving**, and is asked. A site
  bouncing to its own subdomain lands here too; the guard cannot tell that from a
  hostile page escaping. The refusal is recoverable — the original document is
  still there and the audit names the destination — so the agent can read where
  it was being sent and ask for it.
  `navigate`, `go_back` and `reload_page` answer it as `blocked_by_policy`, with the
  `url` the tab still stands on, about a second after the refusal — also when the
  page did it before it finished loading. The 204 commits nothing, so the driver
  would otherwise wait out its own 30 s timeout for a navigation that is gone
  (`scripts/verify_scriptredirect_e2e.py`; neverssl.com does exactly this). `click`,
  `type_text`, `press_key` and `hover` answer it too when the navigation was theirs
  (see [What an action set off](#what-an-action-set-off-blocked_by_policy-new_tab));
  they used to say `ok` (`scripts/verify_click_refusal_e2e.py`).
- **A redirect hop is judged like the navigation it leads to.** A 301/302/303/307/308
  to another origin is classified exactly as a first request there would be: leaving
  the site is `navigate` and is asked, a 307/308 that repeats a POST body is `submit`,
  and a hop that stays on the origin that sent it (or upgrades it from `http` to
  `https`) is not judged at all. The audit row carries `redirect_from`, and an
  approved site that answers 302 to an unapproved one is refused like the same
  destination asked for directly: `navigate`, `go_back` and `reload_page` answer
  `blocked_by_policy` and the tab stays where it was. The agent asked for one URL and
  was turned away from another, so that answer also carries `redirected_to`, the
  origin it was sent to (never the path or the query), for the agent to ask for.
  Playwright never shows a hop to a route handler, so the guard hears of it from the
  `request` event, *after* the request was sent, and cancels the load there
  (`scripts/verify_redirect_hop_e2e.py`; what that leaves open is listed below).

Verified against the shipped defaults: four undeclared POSTs (space key, JS
`form.submit()` from a `type="button"`, an `oninput` listener, and an attribute
flipped after the check) are all stopped, while the declared submit goes through.

**What this does not cover**, stated rather than discovered later:

- With the default `route` backend, an HTTP redirect hop is judged after it has left, not
  before. The request to the first hop that no grant covers is sent — with that site's
  cookies, and the body of a 307/308 POST — and only the rest of the chain is cut. A
  server that answers before the cancellation lands commits the page first, and loopback
  or a LAN, where the targets most worth refusing live, always does: the guard then
  replaces the tab with `about:blank` so the agent does not read it, the audit records
  `not_stopped`, and the call still answers `blocked_by_policy`. Seeing a hop before it
  leaves is not possible from `context.route`: Playwright continues every hop itself,
  also one that follows a `route.fulfill(302)`, and answering each document from Node
  (`route.fetch`) to see the redirect first puts a Node TLS and HTTP/1.1 fingerprint on
  every page while still leaving the later hops unseen (measured).
  `LYRA_BROWSER_GUARD=cdp` judges every hop *before* it leaves, so a refused hop's
  destination sees nothing; see [Guard backends](#guard-backends).
- A page can ask the browser to *prefetch* or *prerender* another page
  (`<script type="speculationrules">`). Chrome sends those requests, and serves the
  navigation that later activates one, without an interception point: neither
  backend is shown the request, nor the click that follows (measured, Chrome 154:
  a prefetched cross-site link was followed with the guard seeing nothing, and the
  cross-site prefetch itself reached its server). It is a GET, so it is the same
  channel as `<img src="https://elsewhere/?data">`; what it adds is a navigation
  nobody judged.
- With `route`, a navigation that a *service worker* answers is never shown to the
  guard either. Playwright's `service_workers="block"` is an init script that a
  page defeats by calling the prototype's `register`, and a worker already in the
  profile is untouched by it (measured: an origin's pre-registered worker served
  that origin's page to a link click with no judgement). The `cdp` backend turns
  workers off for every page it guards.
- A single-page app does its damage over `fetch()`, which is not a navigation and
  is not judged. Deleting mail in a webmail client is an XHR, not a form post.
- An approved page choosing where to send its form. Judging a submission on its
  sender is what makes payments and SSO work, and it is also an exfiltration
  channel: the audit records it (under the key `cross_origin_send`, which is only a
  label on the audit row, not a capability), it does not stop it.
- A single-use scope is handed back about two seconds after the action that
  bought it, not instantly, because a click's navigation can leave just after
  the call returns. A page that submits inside that window rides an approval
  meant for the click. Two seconds rather than the ten minutes it used to be
  (`LYRA_BROWSER_RELEASE_GRACE`).
- The approval for a `file:` URL names that URL and covers only it, once — every
  `file:` URL is otherwise the same origin, which would make one yes a yes to the
  whole disk.
- The content of a download. It is gated, not inspected (see [Downloads](#downloads)):
  an approved download is bytes from that site, written as-is under a sanitised name.

Enforcement narrows what a compromised turn can reach; it does not make an
approved origin safe.

**One browser, one session.** Reading checks who is driving; only a mutating
tool becomes the driver, so a passive client cannot take the browser by asking
for a screenshot. Reading still counts as *using* it, so a holder part-way
through a read-only pass is not idle and does not lose the window. Grants are kept per session, but every session drives the
*same* window, and the route handler has no way back to a request
context — so whichever session called a tool last would decide how the next
request is judged, and one client's single-use approval would be spent by
another's traffic. Rather than offer an isolation it cannot deliver, the server
serves one session at a time and answers the rest with `session_conflict`.
Ownership lasts as long as the window: `close_browser` ends a turn, drops the
grants, and lets the next session claim it. A holder that goes quiet for
`LYRA_BROWSER_OWNER_IDLE_TIMEOUT` (default 900s) also hands it on — closing is
owner-only, so a client that drops with its window open would otherwise lock the
server for the life of the process. **The handoff closes the window**: passing on
a running browser would leave the previous holder's document loaded and still
able to start navigations, which the guard would then judge, and charge, against
whoever now holds the session.

**Who is answering.** `auto` asks the user through MCP elicitation, where the
model cannot forge the answer. Only a client that cannot be asked at all — no
elicitation handler, no live request — falls back to honouring `confirm=true`,
and the audit records `consent_channel=legacy` with the reason. A decline, a
cancel and a timeout are *answers*: the model asserting `confirm=true` never
overrides one. A client that does not pass an elicitation handler takes that
fallback; wiring one is what turns the gate from a convention into a boundary, and
needs no change here. Hermes does pass one: its approval prompt answers, and its
value-less accept is read as the yes it is.

**Autonomous runs.** When nobody is there to answer, `LYRA_BROWSER_CONSENT_CHANNEL=autonomous`
(or `target.policy: autonomous` in a UAT run) approves what a task on the open web
needs: navigation to any site, and a declared `SUBMIT`, `UPLOAD` or `DOWNLOAD`. Each
approval is audited as `consent_channel=autonomous`, so a run's side effects can be
listed afterwards. It still refuses `PUBLISH`, `file:`/`data:`/`javascript:` origins,
and any site in `LYRA_BROWSER_DENIED_ORIGINS` (same entry forms as the trusted list,
and it wins over every other yes). The default stays `auto`/`elicit`, so nothing
changes unless the operator chooses it.

**Trusted origins.** Every new site costs one prompt, and a prompt nobody
answers holds its tool call for `LYRA_BROWSER_CONSENT_TIMEOUT` (default 300s)
before it becomes a denial. For sites the operator always uses, that answer can
be given in advance: `LYRA_BROWSER_TRUSTED_ORIGINS` is a comma- or
whitespace-separated list of sites that are pre-approved for `NAVIGATE` — and the
`INTERACT` it implies — so they are not asked about again. The operator writes it;
nothing the model or a page says can add to it.

| Entry | Trusts |
|---|---|
| `https://www.kvraudio.com`, `http://localhost:3000` | exactly that scheme, host and port |
| `kvraudio.com` | `https` on the default port, that host only — not `www.kvraudio.com`, not `http://` |
| `*.example.com` | `https` on the default port, any subdomain at any depth — **not** `example.com` itself |

Hosts are compared as parsed `(scheme, host, port)` fields, never as text, so
`https://kvraudio.com.evil.io`, `https://kvraudio.com@evil.io` and
`https://kvraudio.com:8443` are not `kvraudio.com`. A site that redirects between
`example.com` and `www.example.com` needs both (`example.com` and
`*.example.com`). An entry that cannot be read is dropped and the rest still load:
empty and malformed entries, a lone `*`, single-label wildcards (`*.com`),
wildcards on an IP address, IPv6 literals, paths, userinfo, a port on a bare host
(write `http://localhost:3000`), and any scheme but http(s) — `file:`, `data:`,
`about:` and `blob:` name no site. The list does not know public suffixes:
`*.co.uk` is two labels and would be accepted, so do not write it.

What it covers is deliberately small. It never answers `SUBMIT`, `UPLOAD`,
`PUBLISH` or `DOWNLOAD`: those stay single-use and are still
asked on a listed site, every time. (To pre-approve sending from your own product for
end-to-end runs, `LYRA_BROWSER_TRUSTED_SEND_ORIGINS` is a separate list that answers
`SUBMIT` and `UPLOAD` and nothing else.) The list also holds where the browser *arrives* without
a `navigate` call -- a redirect hop or a followed link to a listed site -- because the guard
applies it to NAVIGATE too. It does not apply to an opaque origin, while
the user holds a takeover, or when approval is off (that stays
`consent_channel=off`). A listed site gets a real grant with the same lifetime
and initiator binding an approved one would, so enforcement judges it identically,
and the audit records `consent_channel=trusted` with the entry that matched.
`trusted` is a way a decision was reached, not a value of
`LYRA_BROWSER_CONSENT_CHANNEL`. It applies under `auto`, `elicit` and `legacy`.

**Takeover.** While the user holds the session, every mutating tool returns
`takeover_active` and the agent waits — including `highlight_element` and
`ask_user_to_do`, which write to the page. Reading tools keep working.

Enforcement steps aside too. Grants record what the *agent* was allowed to do;
holding a person to them would refuse them their own browser, and takeover exists
for the passwords, CAPTCHAs, 2FA and payments the agent must not do alone — every
one of which is a navigation. Those are recorded as `user_driven`, so the trail
distinguishes what a person did from what was approved for the agent.

## Guard backends

Enforcement has to see every document request before it leaves the browser. There
are two ways to stand there, chosen with `LYRA_BROWSER_GUARD` (`route` unless set);
both ask the *same* judgement (`NavigationGuard.decide`), so grants, single-use
scopes, takeover, observe mode and the audit trail behave identically. They differ in
what they can see and what they cost.

| | `route` (default) | `cdp` |
|---|---|---|
| How | Playwright's `context.route("**/*")` | A second DevTools connection: `Fetch.enable` for `Document` requests on every page and every out-of-process iframe |
| Redirect hops | Judged *after* they have left: a `context.on("request")` listener cancels the load (the request to the first refused hop still goes out; on loopback the page commits first and is blanked) | Every hop judged *before* it leaves: the destination of a refused hop sees nothing |
| HTTP cache | Off: Playwright sends `Network.setCacheDisabled(true)` with any route | On |
| etsy.com (DataDome), headful, fresh profile, N=5 | refused 5/5 | let in 5/5 |
| Service workers | `service_workers="block"` is an init script a page can step around; a navigation a worker answers is never seen | Bypassed on every guarded page (`Network.setBypassServiceWorker`) |
| Open port | none | a debugging port on `127.0.0.1`, see [below](#debugging-port) |
| Guard lost | cannot happen | the browser is killed and every call answers `guard_lost` |
| Launch | Playwright's defaults | adds `--remote-debugging-port=0`, drops `--enable-unsafe-swiftshader` |
| Dependencies | none | none: a stdlib WebSocket client (`cdp_socket.py`, ~250 lines), not the `websockets` package |

**How `cdp` guards.** Chrome is started with `--remote-debugging-port=0` next to the
pipe Playwright uses, and `cdp_guard.py` opens a second connection to it. It
auto-attaches to every page and out-of-process iframe at *browser* level with
`waitForDebuggerOnStart`, so a popup or a cross-site iframe is held until
`Fetch.enable` is in place on it (a session made through Playwright after the tab
exists cannot promise that: popups' first requests and cross-site iframe requests
escaped in the earlier probe). Each paused request becomes the two objects
`classify` reads, goes through `decide`, and is answered `Fetch.continueRequest` or a
204 `Fulfill` — never an abort. Nothing is asked of the page while a request is
paused (its renderer does not answer then).

**Redirect hops.** Each hop is its own `Fetch.requestPaused`, judged as the
navigation it is: a 307/308 keeps method and body, a 301/302/303 turns a POST into a
body-less GET. A site you approved that answers `302` to an unapproved origin is
refused *before* the request leaves — the destination sees nothing — and the audit row
names `redirect_from`, and `navigate` answers `blocked_by_policy` with `redirected_to`. A hop that stays with the origin that issued it (the same
origin, or `http`→`https` of the same host on default ports) is not judged again: it is
the request carrying on, and asking again would strand every trailing-slash redirect on
a form.

**Uploads.** Chrome puts a navigation's whole body into the event (as text and as
base64: a 40 MB form is a 98 MB message). The transport reads such a message through
without keeping it and the guard judges the event without the body (it only asks
whether one exists). A 3, 12 and 40 MB multipart POST were refused when ungranted,
delivered when granted, and the guard stayed up.

**If the guard is lost.** Chrome releases every request it holds, and every target
waiting for the debugger, the moment the connection ends, so a lost connection is a
browser running unguarded. The session therefore kills the browser, records
`guard_lost` in the audit trail and answers every call `guard_lost` (also to readers,
and to `open_browser`) until `close_browser` acknowledges it; the next browser is a
fresh, guarded one. A socket that ends because the browser quit (a crash, the user
closing the last window) is told apart without waiting: the process is already gone, or it
is running with no open tab and leaves within 300 ms. With a tab open nothing explains the
socket, so it is killed at once. Measured (`scripts/verify_cdp_guard_e2e.py`): socket
aborted, process gone in 1–2 ms, and a hostile page posting a form every 4 ms got 0 of
them through to its server in 3 of 3 runs (waiting 300 ms first let 72 through). Requests
already in flight at that instant were released by Chrome and are not judged. A sidecar
that cannot start, or a target it cannot guard, is the same event: the window is closed
rather than left open.

**Debugging port.** `--remote-debugging-port` is an open, unauthenticated service, and
this is what `cdp` costs.

- *What it exposes.* Any process on the machine that finds the port can attach to the
  browser and do what the agent can and more: list tabs, read cookies (HttpOnly ones
  too), run script in any tab of the logged-in profile. Finding it needs no secret:
  `GET /json/version` hands out the WebSocket path. With `route` there is no port;
  Playwright drives Chrome over a pipe only it holds. Reading the profile directly would
  also give a same-user process the cookies, so the difference is mostly *other users* of
  the host and sandboxed code that has loopback but not the profile directory; on a
  one-user workstation it is small, on a shared host it is the whole profile.
- *What is done.* Random port, chosen by Chrome, bound to `127.0.0.1` only (`::1`
  refuses; measured). Chrome refuses a handshake that carries an `Origin` header (a web
  page cannot connect, 403) or a foreign `Host` (no DNS rebinding, 500). The profile
  directory is made `0700` before launch; `DevToolsActivePort` (written `0664`, and left
  behind when Chrome exits) is removed before launch and as soon as it is read. The port
  and the path are never put in an exception, a log line, an audit row or an envelope.
- *What remains.* The port exists as long as the browser does; Chrome has no way to close
  it or to require a token. A second consumer cannot use the pipe (Chrome serves one, and
  Playwright holds it); putting a proxy in front of a pipe-launched Chrome means
  launching Chrome ourselves, which this does not do. Chrome 136 and later ignore the
  flag on their *default* profile directory because the port is a cookie-theft vector
  [Chrome's own announcement, not measured here]; this server never uses that directory.
- Measured by the `exposure` section of `scripts/verify_cdp_guard_e2e.py`, which attaches
  from a separate process the way a scanner would.

**What neither backend sees.** `fetch()`/XHR (not navigations), WebSockets, GETs with the
data in the URL, speculation-rules prefetch/prerender and the navigation that activates
one (above), and `data:`/`about:`/`blob:`/`javascript:` URLs (not requests). Workers never
issue a document request and are not attached; a page's own `back_forward_cache` is off
under Playwright's switches, and history navigations are judged with the cache on.

**Choosing.** `cdp` for sites behind DataDome, where redirect chains matter, or where
a page's service worker must not answer navigations; `route` where an open loopback
port is unacceptable. Every `scripts/verify_*_e2e.py` gate runs on either:
`LYRA_BROWSER_GUARD=cdp python scripts/verify_tabs_e2e.py`.

## Installation

Developers working on this repo:

```bash
pip install -e ".[dev]"
python -m playwright install chromium   # only needed if you have no system Chrome
lyra-browser                            # serve over stdio (the default, what an MCP client spawns)
# or: lyra-browser --http --port 8765   # serve over HTTP for dev
```

An end user needs no pip and no `playwright install` when Chrome or Edge is installed:
the server drives that browser. If neither is found, the tools answer
`browser_unavailable` so the client can prompt for an install.

**Deploying it as a remote server (Cloudflare Containers):** see
[docs/CLOUDFLARE.md](docs/CLOUDFLARE.md).

## Configuration (env)

| Var | Default | Meaning |
|---|---|---|
| `LYRA_BROWSER_CLIENT` | detected | `generic`, `hermes` or `remote` (also `--client`). Picks default paths, window mode and how captures reach the model — never permissions. `remote` is a server nobody sits at: headless, and `screenshot`/`read_image` attach the PNG as an MCP image. Detected from `HERMES_HOME` → hermes; otherwise `generic`. |
| `LYRA_BROWSER_DATA_DIR` | — | Base data dir (profile + audit). Overrides everything. |
| `HERMES_HOME` | `~/.hermes` | hermes: data goes to `$HERMES_HOME/browser`. |
| `LYRA_BROWSER_HEADLESS` | auto | Run without a visible window (see Headless mode). Unset: headless for hermes and remote, and on Linux with no `DISPLAY`/`WAYLAND_DISPLAY`; otherwise a window. |
| `LYRA_BROWSER_VIEWPORT` | `1280x800` | `WIDTHxHEIGHT` of the page (`390x844` for a phone layout). Headless uses it as the emulated viewport; headful as the window size. Anything unreadable falls back to the default. |
| `LYRA_BROWSER_PROXY` | unset | Route the browser through a proxy, e.g. `socks5://100.x.y.z:1080`. For UAT runs that must not share the operator's egress IP, since per-IP rate limits, quotas and IP-based analytics exclusion all key on it. Unset means a direct connection. |
| `LYRA_BROWSER_REQUIRE_APPROVAL` | `true` | Ask before risky actions. `false` sets the consent channel to `off`. |
| `LYRA_BROWSER_CONSENT_CHANNEL` | `auto` | `auto` asks the user over MCP and falls back to `confirm=true` only on a client that cannot be asked; `elicit` pins asking and denies otherwise; `legacy` always takes the model's word; `off` does not ask; `autonomous` approves what an unattended run needs, except what `DENIED_ORIGINS` names (see Permissions). |
| `LYRA_BROWSER_ENFORCEMENT` | `enforce` | `observe` records what it would have blocked without blocking: navigations go through, and an undeclared download is saved (`observed: true`, audited `would_block`) instead of cancelled. |
| `LYRA_BROWSER_GRANT_TTL` | `600` | Seconds a grant stays usable. Single-use ones ignore this. |
| `LYRA_BROWSER_TRUSTED_ORIGINS` | — | Sites pre-approved for `NAVIGATE`/`INTERACT`, comma- or whitespace-separated: `https://host[:port]`, a bare `host` (https only), or `*.example.com` (https subdomains, not the apex). Never covers `SUBMIT`/`UPLOAD`/`PUBLISH`/`DOWNLOAD`. See Trusted origins. |
| `LYRA_BROWSER_TRUSTED_SEND_ORIGINS` | — | Sites where *sending* is also pre-approved — `SUBMIT` and `UPLOAD` — for acceptance runs against your own product. Same entry forms, **separate list**: a site in `TRUSTED_ORIGINS` is not send-trusted by being there. Never `PUBLISH` or `DOWNLOAD`; each approval stays single-use, and it steps aside during a takeover. Audited as `consent_channel=trusted_send`. |
| `LYRA_BROWSER_DENIED_ORIGINS` | — | Sites refused whatever else says yes: the trusted lists, a live grant and the `autonomous` channel all step aside. Same entry forms. |
| `LYRA_BROWSER_CONSENT_TIMEOUT` | `300` | Seconds a prompt waits for an answer before it counts as a denial (`timed out waiting for the user`). The tool call is held that long, so for an unattended Hermes gateway set it lower — e.g. `60`. |
| `LYRA_BROWSER_RELEASE_GRACE` | `2` | Seconds before a finished action's unused single-use scope is reclaimed. |
| `LYRA_BROWSER_OWNER_IDLE_TIMEOUT` | `900` | Seconds a session may hold the browser without using it. Handing it on closes the window. |
| `LYRA_BROWSER_CAPTURE_DIR` | — | Where `screenshot`/`read_image` write PNGs. Default: hermes `$HERMES_HOME/cache/browser` (where Hermes' vision may read), else `<data_dir>/captures`. |
| `LYRA_BROWSER_CAPTURE_KEEP` | `200` | Captures kept on disk; oldest are deleted first. `0` keeps all. |
| `LYRA_BROWSER_DOWNLOAD_DIR` | — | Where declared downloads are saved. Default `<data_dir>/downloads`. |
| `LYRA_BROWSER_DOWNLOAD_KEEP` | `200` | Saved downloads kept; oldest are deleted first, and only files this server saved (tracked in a ledger in the dir). `0` keeps all. |
| `LYRA_BROWSER_DOWNLOAD_MAX_BYTES` | `209715200` | Largest file a download may be (200 MiB); a bigger one is cancelled and answered `download_failed` (`too_large`). |
| `LYRA_BROWSER_DOWNLOAD_TIMEOUT` | `120` | Seconds a declared download may take to arrive once it has started before it is cancelled (`download_failed`, `timeout`). |
| `LYRA_BROWSER_CHANNEL` | — | Force one channel (`chrome`/`msedge`). Unset = try chrome→msedge→bundled. |
| `LYRA_BROWSER_ALLOW_BUNDLED` | `true` | Allow bundled-Chromium fallback (only exists after `playwright install`). |
| `LYRA_BROWSER_AUDIT_STDERR` | `false` | Also write every audit record to stderr as one JSON line. For a host whose disk is ephemeral but whose stderr is collected (the Cloudflare image sets it). |
| `LYRA_BROWSER_DRIVER` | `auto` | Which Playwright to launch with: `auto` takes `patchright` when installed and falls back to `playwright`; either name pins it. `open_browser` reports the one in use as `driver`. |
| `LYRA_BROWSER_GUARD` | `route` | Where the navigation guard stands: `route` (Playwright's route) or `cdp` (a second DevTools connection: every redirect hop judged, cache on, DataDome lets the browser in, a loopback debugging port, `guard_lost` if the guard fails). See Guard backends. |
| `LYRA_UAT_CLAUDE_BIN` | `claude` on `PATH` | The Claude Code executable the `claude-code` UAT brain runs. See UAT mode. |
| `LYRA_UAT_CODEX_BIN` | `codex` on `PATH` | The Codex executable the `codex` UAT brain runs. See UAT mode. |

## UAT mode

`lyra-uat` plays a *persona* — who is visiting, from where, in what language, with what goal and how many steps — against a site, through the tools above, and writes `report.json`. The model that plays it is replaceable: Claude Code, Codex, the Anthropic API, OpenRouter, Ollama Cloud, or any OpenAI-compatible endpoint. What makes a run trustworthy is not: **the server, not the model, keeps the trace**. Whichever brain drives, every tool call is recorded as it happens, the step budget is enforced, a screenshot is taken after each action, a `type_text` value that looks like a payment-card number is refused, an upload outside the persona's directory is refused, and leaving the sites under test cannot be approved by the model's own `confirm=true`.

```bash
pip install -e ".[uat]"
lyra-uat demo-site --port 8787 &             # a small site with things to find
lyra-uat run examples/uat/demo/run.yaml      # one persona, Claude Code plays it
lyra-uat batch examples/uat/demo/batch.yaml  # two personas in parallel, one process each
```

A run leaves a directory: `report.json` (and `report.md`), `trace.jsonl`, `events.jsonl`, `captures/`, `network.jsonl`, `console.jsonl`, and the browser's own `audit.jsonl`. Persona, target, brain, hooks and limits are YAML or JSON; `lyra-uat schema run|batch|report` prints the JSON Schemas. A run that should act without anyone to ask sets `target.policy: autonomous`; the default is `closed`, which keeps the run on its trusted sites. Details, the report format and each brain's requirements are in [docs/UAT.md](docs/UAT.md).

## Headless mode

```bash
lyra-browser --headless      # or LYRA_BROWSER_HEADLESS=true; the flag wins
lyra-browser --no-headless   # force a window even if the env says otherwise
```

Unset, the mode follows the client and the host: `--client hermes` is headless
(Hermes reaches the user over chat and gives its servers no display), and so is
any Linux process with neither `DISPLAY` nor `WAYLAND_DISPLAY` — launching a
window there would crash, headless is a working session that says so.

Headless does not merely hide the window — it means **nobody is watching**. The
collaboration tools have no human to reach, so they refuse with an `unattended`
envelope instead of reporting success for something no one will see:

| Tool | Headless behaviour |
|---|---|
| `highlight_element` | `unattended` — an outline nobody sees is not a signal |
| `ask_user_to_do` | `unattended` — there is no user at this window to ask |
| `request_takeover` | `unattended`, **and control is not handed over** — granting a takeover no human can release would block every later mutation |

Headless Chrome also announces itself: its UA says `HeadlessChrome`, and
Cloudflare's managed challenge refuses on that token alone (13 of 34 login
pages in the earlier survey). The session therefore sends the UA the same
binary would send with a window — the real version, the real platform, only
that token dropped — so `navigator.userAgentData` and the `Sec-CH-UA-*` headers
stay the browser's own. The version is read from the running Chrome on the
first headless launch and remembered in `<data_dir>/browser-ua.json`; that
first launch, and the one after each Chrome update, relaunch once (about 0.7s).

Navigation, interaction, and reading are unaffected. `open_browser` reports which
mode you are in via its `attended` field so the agent never assumes an audience.

## Looking like the user's Chrome

Sites that sort agents from people check the launch, not the behaviour. The
session is launched the way Playwright's own MCP server launches — with
`--disable-blink-features=AutomationControlled`, so `navigator.webdriver` is
`false` as in any Chrome a person opens, and without an emulated viewport in
headful mode, so the page sees the real screen instead of a window larger than
the screen it is on. Measured on 42 login pages behind Cloudflare, Akamai,
DataDome, PerimeterX and Kasada: of the 34 plain Chrome passes,
the Playwright defaults failed 6 headful and 19 headless; with these settings
the count is in the survey comment, per site.

What is deliberately *not* done: the `--enable-automation` infobar stays (it is
how a person at the shared window is told what is happening), and nothing is
forged — no WebGL or canvas noise, no patched CDP. Route interception and the
service-worker block contribute nothing to the Cloudflare and Akamai refusals; for
DataDome the route does matter, which is why the `cdp` guard backend exists.

Two vendors still refuse, and the cause of each is measured rather than guessed:

- **Kasada** (hyatt.com) notices one CDP call, `Runtime.enable`. Plain Chrome
  driven over raw CDP passes; the same Chrome with `Runtime.enable` on is
  refused; every other call Playwright makes on attach is fine. Playwright
  enables it on every page and has no switch not to. [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python)
  is a drop-in fork that does not send it and evaluates in an isolated world
  instead; with `pip install "lyra-browser[patchright]"` the session launches
  through it (`LYRA_BROWSER_DRIVER=auto`), and hyatt passes in both modes.
  What patchright changes for this server: `page.evaluate` runs outside the
  page's own JavaScript world (our evaluate calls only touch the DOM, which is
  shared) and console messages are not delivered (we read none).
- **DataDome** (etsy.com; tripadvisor.com does not discriminate in the
  measurements) refuses on either of two causes, each sufficient on its own
  (N=5–6 per cell, headful, one IP):
  `context.route` switches the HTTP cache off, so every document request carries
  `Cache-Control`/`Pragma: no-cache` (plain Chrome with only
  `Network.setCacheDisabled` is refused 0/6, while raw CDP `Fetch.enable` on
  documents or on all requests is fine), and on a GPU-less host
  `--enable-unsafe-swiftshader` gives the page a WebGL that plain Chrome lacks.
  The `cdp` guard backend removes both: no route, documents only over a second
  connection, and the switch dropped. Measured on that backend with the product itself
  (`scripts/measure_compat.py`, headful, fresh profile per visit, 12 s apart, N=5):
  **etsy.com let in 5/5 against 0/5 for the `route` launch; hyatt.com (patchright) 5/5
  against 4/5** (the one miss was a 429). Dropping the switch removes WebGL on a host with
  no GPU, as in plain Chrome there; on a host with a GPU it is inert [INFERENCE: no GPU
  host here]. Headless is undecidable on the measuring host.

## Development

```bash
ruff check .       # lint
pre-commit install # enable hooks
```

Real-browser gates live in `scripts/verify_*_e2e.py`, for
example `python scripts/verify_browser_e2e.py --headless-only`, or headful under
`xvfb-run -a`.

- **CDP guard gate** — `python scripts/verify_cdp_guard_e2e.py` (both modes, `--driver`)
  re-runs the browser, script-redirect and tabs gates on the `cdp` backend, then checks
  what only it can do: redirect hops (301/302/303/307/308, with and without a body), popups
  at N=10, out-of-process iframes, service workers, large uploads, kill-the-socket
  fail-closed, a sidecar that cannot start, and what another local process can do with the
  debugging port. `scripts/measure_compat.py` is the etsy/hyatt measurement above.

- **Real-sites gate** — `python scripts/verify_realsites.py` (needs the internet) visits ten
  live sites and a loopback control, reads each as a tree and as text, clicks its first
  internal link through the `aria-ref=` the tree printed, and compares judged and blocked
  navigations and page sizes with `scripts/realsites_baseline.json` (±10% plus a small
  slack; success must match exactly). A site that cannot be reached is `SKIP`, and the gate
  fails only when fewer than eight ran. It runs against live sites, so a site editing its
  header can turn it red with no code change: treat it as a report to read, not a merge
  gate (the loopback `verify_*_e2e.py` gates are the blocking ones). `--update-baseline` re-records it, `--sites a,b` and
  `--retries N` narrow and steady a run, `--driver patchright` and `--headful` pick the
  driver and the window.

## Status

The server, session manager, tools, permission layer, enforcement, bot-check
detection and audit are implemented, unit-tested (no browser needed) and exercised
end-to-end against real sites with a real Chrome — ten rounds over eleven sites, 187
judged navigations. Not yet driven by a live MCP client that implements elicitation;
that is the next milestone, and it is what would let `consent_channel=elicit` replace
the `confirm` flag.

## License

MIT
