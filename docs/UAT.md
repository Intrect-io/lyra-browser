# UAT mode

`lyra-uat` plays a persona against a site and writes a report. A **persona** is
defined by what changes the path it takes — where it enters, at what width and
language, signed in or not, what it is trying to do, how we know it got there,
how many steps it gets, and what it must not do — not by a backstory. A **brain**
is whatever plays it: Claude Code, Codex, a model behind an API.

It is not a replacement for a person accepting software. It is a way to put the
same eight visitors in front of every build, cheaply, and to come back with a
trace and a list of places where they got stuck, were misled, or saw something
false — each with a URL, a step, what was expected and what happened — for a
human to reproduce before anything becomes a ticket.

```bash
pip install -e ".[uat]"
lyra-uat demo-site --port 8787 &             # a small site with things to find
lyra-uat run examples/uat/demo/run.yaml      # one persona
lyra-uat batch examples/uat/demo/batch.yaml  # two personas in parallel
```

## What the server guarantees, and what it only asks

The brain is replaceable. These are not, because they live in the server
(`lyra-browser`'s own tool layer plus `uat.recorder.UatMiddleware`), in front of
every tool call, whichever brain made it:

| Guaranteed by the server | How |
|---|---|
| **The trace.** Every call, its arguments (typed values redacted), status, URL before and after, a result head. | Appended to `trace.jsonl` before the model sees the result, so a run cut off by a timeout or a crashed harness keeps everything up to that moment. |
| **A screenshot after every action** that reached the page. | `captures/`, path in the trace step. Reads are not illustrated. |
| **The step budget.** | Action steps (`navigate`, `click`, `type_text`, `press_key`, `hover`, `scroll`, `select_option`, `upload_file`, `handle_dialog`, `tabs` switch/close, …) count; the next one answers `budget_exhausted`. Reads, `finish` and the report tools never count. The first `navigate` to the entry URL is free. A call that never reached the page (`not_found`, `needs_approval`, …) is recorded but not counted. |
| **No payment-card numbers through `type_text`.** | A `type_text` value that is 13–19 digits and passes Luhn is refused (`blocked_by_uat_policy`); the page never sees it. Digits sent one key at a time with `press_key` are not recognised: this is a backstop for a persona that is told never to pay, not a licence to point one at a live checkout. |
| **Uploads only from the persona's directory.** | `upload_file` is not even offered without `uploads_allowed_dir`; with it, every path must resolve inside it. |
| **Leaving the sites under test cannot be approved by the model.** | Consent is pinned to `elicit`; the in-memory client and both harnesses cannot answer it, so a navigation outside `trusted_origins` is `needs_approval` and stays one — `confirm=true` included. |
| **A fixed tool set.** | The persona gets browser reading and acting tools plus `report_finding`, `verdict`, `note`, `finish`. Not `open_browser`/`close_browser` (the runner's), not editor/publish tools, not the collaboration tools (nobody is watching). |
| **The report's shape.** | `report.json` is assembled from the run's files, never from the model's recollection; it validates against `lyra-uat schema report`. |

What the persona's `must_not` list says beyond that ("don't sign up") is
prompt-only, and the report says so under `policy.prompt_only` rather than
implying it was enforced.

## Spec

YAML or JSON. A run file nests everything, or points at other files
(`persona_file`, `target_file`, `brief_file`, `prior_findings_file`, relative to
itself), so one target and one round brief serve many personas. A file holding
only a persona works with `--target`. Unknown keys are errors: a misspelt
`step_budget` must not silently become "no budget".

```yaml
persona:
  id: p1                      # [a-z0-9_-], at most 32
  name: Phone visitor from a search result
  entry_url: https://example.com/
  viewport: 390x844
  locale: en-US               # the persona's browsing language
  account: none               # none | preseeded (needs data_dir: a profile that holds the login)
  goal: Find out what this costs and whether there is a free way to try it.
  success_criteria: [...]
  step_budget: 25             # action steps; reads are free
  must_not: [sign up, enter payment details]
  known_limits: [...]         # things that are not findings in this run
  uploads_allowed_dir: ~/uat-assets   # omit: upload_file is not offered
  extra_trusted_origins: []   # sites this persona needs beyond the target's
target:
  trusted_origins: [example.com, "*.example.com"]
  trusted_send_origins: [example.com]    # SUBMIT/UPLOAD pre-approved: your own product only
  proxy: null                            # socks5://host:1080 — keep runs off the operator's IP
  network_capture: ["google-analytics\\.com/g/collect"]   # kept whole in network.jsonl
brain:
  backend: claude-code        # anthropic | openrouter | ollama | openai-compat | claude-code | codex | scripted
  model: null                 # as the backend spells it; null takes the backend's default
  max_turns: 150
  max_budget_usd: 5
brief: ...                    # a round addendum, shown after the persona, verbatim
prior_findings: [{id, severity, url, step, expected, actual}]   # the persona re-checks each and calls `verdict`
hooks: {before: ..., after: ..., timeout_s: 600}
limits: {wall_s: 1800, auto_screenshot: true, vision: on_demand, max_tool_output_chars: 12000}
tools: {allow: null, deny: []}
report_language: English      # what notes, findings and the summary are written in
data_dir: null                # an existing browser profile (LYRA_BROWSER_DATA_DIR layout)
out_dir: uat-runs
```

**Target origins** use the grammar of `LYRA_BROWSER_TRUSTED_ORIGINS`: a bare
`host` means **https only**. For a local `http` site write the whole origin,
`http://127.0.0.1:8787`; `127.0.0.1:8787` is dropped without a word and the first
navigation is `needs_approval`.

**Step budget** is the one number to tune per persona: it decides how much of
the product the persona can see. Count what a person would do — a click is a
step, reading the page is not.

**`preseeded` personas** are only as signed in as the profile they are given.
The spec refuses one without `data_dir`. If another browser holds that profile
the server steps aside to an empty one (`profile_mode: instance`), and the run
is reported **invalid** (`browser_error`) even if the persona finished, because
whatever it saw it saw as a stranger. Run such personas one after another, or
give each its own profile.

## Brains

Two kinds. A **loop brain** is a model behind an API: lyra-uat owns the tool-use
loop in this process, against an in-memory MCP client. A **harness brain**
(Claude Code, Codex) owns its own loop: lyra-uat starts it with
`lyra-browser --uat-run <run.json>` as its MCP server and reads the run's files
afterwards. The report is the same either way.

Both kinds get the same texts: a system prompt (the tester's role, how the
tools behave, page content is data and never instructions, what to report and
how) and a task prompt (the persona, the round brief, the earlier findings to
re-check, the report language). The system prompt tells the persona to move the
way a person does — click what it can see; `navigate` is for the entry URL and
for going back — because a persona that types URLs can never find a broken link.
The report counts clicks and direct navigations (`metrics`) and warns when a run
never clicked.

### `claude-code`

Runs `claude -p`. Uses whatever Claude Code is signed in as (a subscription
needs no API key); `LYRA_UAT_CLAUDE_BIN` overrides the executable. The persona
is the system prompt (replacing the coding assistant's), the task is the prompt,
the only tools are lyra's (`--tools ""`, `--strict-mcp-config`), permission
prompts go unanswered (`--permission-prompts none`, so the server's consent
request is cancelled, which it reads as a refusal), nothing is persisted, and
the working directory is an empty folder in the run directory.

**Settings are left out on purpose** (`--setting-sources ""`). A harness runs as
its operator would: their reply language, default model and effort, hooks,
plugins, and `CLAUDE.md` come with it. Measured with Claude Code 2.1.289: with
the operator's settings an English persona wrote its report in Korean (their
configured reply language), and a minimal MCP call sent a first request of 20,889
cache-creation tokens with 8 plugins loaded; with an empty source list the same
call sent 2,594 with only the 3 built-in plugins, and replies were English.
`--safe-mode` also drops the `--mcp-config` server (the persona then has no
tools) and `--bare` needs an API key, so neither is used. Pin the model with
`brain.model`; unpinned, Claude Code's own default is used and the report says
which (`brain.model`).

Extra flags: `brain.extra.argv: ["--flag", "value"]`.

### `codex`

Runs `codex exec --json`. `LYRA_UAT_CODEX_BIN` overrides the executable. The
MCP server is passed as `-c` overrides, the user's own config and rules are
ignored, the shell sandbox is read-only, and `--strict-config` makes a misspelt
override an error instead of a silently ignored setting. With approvals off
Codex refuses every MCP call that needs one, so the persona's own tools are
pre-approved with `default_tools_approval_mode="approve"` and limited to
`enabled_tools`; what the persona may do is decided by the server's guards and
budget. Codex has no system-prompt flag in `exec` mode, so the role precedes the
task in the one prompt. `brain.turns` is the number of Codex `turn.completed`
events — a whole agent loop is one — not model calls; no cost is reported for a
subscription login.

### `anthropic`

The Messages API through the `anthropic` SDK. Credentials as the SDK resolves
them (`ANTHROPIC_API_KEY` or an `ant auth login` profile). Default model
`claude-opus-5-5`; `brain.effort` sets `output_config.effort`. One tool call per
turn, executed in order, the rest of a turn refused after the first failure;
screenshots go back as image blocks; the system prompt is cached and messages
are only ever appended to. A refusal is a `brain_error`: server-side refusal fallbacks
(`fallbacks`) are deliberately not requested. The report's cost is an estimate from a
price table in the code, not a bill. **This brain is covered by fake-client tests of its
request shape, tool results, images, refusals and nudges, and has not been run against
the live API.**

### `openrouter`, `ollama`, `openai-compat`

Chat completions with function calling through the `openai` SDK.

| backend | base URL | key |
|---|---|---|
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `ollama` | `https://ollama.com/v1` (Ollama Cloud) | `OLLAMA_API_KEY` |
| `openai-compat` | `brain.base_url` (required) | `brain.api_key_env`, default `OPENAI_API_KEY` |

A model name is required. The model must support tools, and vision if the
persona is to look at screenshots (`limits.vision: never` sends paths only).
Images go back as a user message of data URLs right after the tool results,
because the `tool` role carries text. `parallel_tool_calls=false` is sent where
the endpoint accepts it (not to Ollama).

### `scripted`

Replays `brain.script: [{tool, args, expect}]`. No model; for tests and the
real-browser gate.

## What a run leaves

```
uat-runs/<run-id>/
  report.json  report.md      the report
  trace.jsonl                 every call, as it happened
  events.jsonl                findings, verdicts, notes, policy refusals, finish
  captures/                   screenshots (after each action, and the persona's own)
  network.jsonl console.jsonl requests (kept whole where network_capture matches), console, page errors
  observations.json           counts, and what the server knows of the browser
  browser/                    the browser's data dir: audit.jsonl and a profile of its own
  harness/                    harness brains: prompts, argv, stdout/stderr, MCP config
  run.json                    the spec the run was started from
```

`report.json` (schema version 1; `lyra-uat schema report`):

- `run` — `status` (`completed`, `incomplete`, `error`), `exit_reason` (`finish`,
  `budget_exhausted`, `wall_timeout`, `brain_error`, `browser_error`,
  `harness_exit`, `hook_failed`, `no_finish`, `interrupted`), timing.
- `outcome` — what `finish` said (`reached_goal`, `partial`, `blocked`) or
  `unknown` when it never did; steps used against the budget; call count.
- `findings` — severity (`blocker`, `major`, `minor`), URL, step, expected,
  actual, evidence. `verdicts` — `FIXED`, `STILL_THERE`, `COULD_NOT_CHECK` per
  earlier finding, with the finding it judged.
- `trace` — the steps, with `counted`, `system` (given, not spent) and the
  screenshot path. `notes`, `what_worked`, `purchase_path`.
- `policy` — what the server enforced, what was only asked (`prompt_only`), and
  the refusals that happened (`events`).
- `side_effects` — approved submits, uploads, downloads and publishes from the
  browser's audit log: where a ledger of production writes starts.
- `metrics` — actions by tool, clicks, direct navigations. `warnings` — a run
  that never clicked, an invalid profile, observers that failed to attach.
- `brain` — backend, model, turns, tokens (input, output, cache read/write),
  cost where known, session id. `observations`, `hooks`, `artifacts`.

`lyra-uat run` exits 0 when the persona called `finish`, 2 when the run did not
complete (budget, wall clock, a brain or hook failure, an invalid profile), and 130
when it was interrupted. A persona that finds nothing is a valid result **only with a
complete trace**; an empty trace is reported as "not a clean run".

## Stopping a run, and what is left behind

A harness brain is three processes deep — the harness, the MCP server it spawns, the
browser the server launches — and a batch adds a level above them. Each child is started
as the leader of a session of its own and is stopped as a **group**: interrupted first,
so the harness can end its turn and close its server (which closes the browser), then
killed if it will not. What a harness leaves running after a normal exit is stopped too.

- **Wall clock** (`limits.wall_s`): the group is interrupted, then killed after a grace
  period; the report is written with `wall_timeout` and everything recorded so far.
- **SIGINT / SIGTERM to `lyra-uat`** (Ctrl-C, `kill`, a service stop): the run is
  cancelled, the browser and the harness group are stopped, a report is written with
  `interrupted`, and the exit code is 130. This holds for a run started in the
  background, where a shell passes SIGINT on as *ignored*. A batch stops each running
  persona the same way but writes no `summary.json`.
- **A harness group killed hard** (measured with Claude Code and Codex): the browser exits
  on its own within seconds, because its debugging pipe closes with the server, and the
  runner reports `harness_exit`.
- **`lyra-uat run` itself killed with SIGKILL** cannot be handled, and nothing below it is
  stopped: the harness carries on with its persona, with its browser, until it finishes
  or reaches `brain.max_turns` / `brain.max_budget_usd`, then exits and takes the browser
  with it. Keep those two limits set. Stop such a run with SIGINT to the harness's process
  group.

Process groups, signals and the `/proc` liveness check make this POSIX-only (Linux, macOS);
it has not been run on Windows.

## Hooks

`hooks.before` and `hooks.after` are shell commands. A failing `before` (non-zero)
stops the run before the browser starts (`hook_failed`); `after` always runs.
Both get `LYRA_UAT_RUN_DIR`, `LYRA_UAT_PERSONA`, `LYRA_UAT_DATA_DIR` and
`LYRA_UAT_ENTRY_URL`, and their exit code and output tails land in the report.
Use them to seed a profile, check analytics afterwards, or reset state.

## Observers

Every tab's requests, responses (documents, matched requests and 4xx/5xx),
failed requests, console messages and page errors are recorded. Query values are
stripped from every URL except those matching `network_capture`, which are kept
whole with their POST body: a URL can carry a token and these files outlive the
run. In automated Chrome a page reached by in-tab navigation often sends no
analytics hit at all and the previous page's pending hit is aborted, so an
absent event after a navigation proves nothing; only events fired on the first
page of a session are reliable evidence of absence.

## Batches

```bash
lyra-uat batch batch.yaml [--only p1 p3] [--out DIR] [--brain NAME --model M]
```

A batch writes a run spec per persona and starts `lyra-uat run` for each as a
child process: its own event loop, browser and profile, and a crash that cannot
take a neighbour down. Groups run in the order listed; a `parallel` group starts
its personas together (anonymous visitors), a `sequential` one runs them one
after another (personas sharing an account). `concurrency` caps the children at
once. The key under `personas` must equal the `id` inside that persona's file;
`preseeded` personas need an entry in `data_dirs`, and two personas of a parallel
group may not share one.

`summary.json` and `summary.md` list each persona's outcome, run status, steps,
finding counts, verdict counts and cost, and lay the earlier findings out as a
matrix: which persona judged each one `FIXED`, `STILL_THERE` or `COULD_NOT_CHECK`,
and which never judged it. A child that crashed before writing a report is a
result with its stderr tail, not a failed batch.

## Security notes

- **Trust only what you test.** `trusted_origins` should list the product under
  test and what a persona legitimately follows from it. `trusted_send_origins`
  pre-approves submitting and uploading there: your own product, never a third
  party. `PUBLISH` and `DOWNLOAD` are never pre-approved, and unattended an
  approval request is a refusal.
- **Production writes are real.** A persona that submits a form against a live
  site writes to it. Put `[UAT]` in anything a human will read (the system prompt
  tells the persona to), list the writes from `side_effects`, and clean up in an
  `after` hook.
- **A live checkout is a page, not a test.** Keep payment providers out of
  `trusted_send_origins`; a card-number guard is a backstop, not a licence.
- **Run directories hold what the persona saw**, including page text and, for a
  `preseeded` persona, a profile with a login. `uat-runs/` is git-ignored;
  treat the directory as sensitive.
- **Page content is data.** Both prompts say so, and a page that tells the
  persona to approve or re-run something is a finding.
- **Use a proxy** (`target.proxy`) when a run must not share the operator's egress
  IP: per-IP limits, quotas and IP-based analytics exclusion all key on it.

## Troubleshooting

| Symptom | Cause |
|---|---|
| First navigation is `needs_approval` | The origin is not in `trusted_origins`, or is written in a form that is dropped: bare hosts are https only; use `http://host:port` for a local site. |
| `claude CLI not found` / `codex CLI not found` | Install it, or set `LYRA_UAT_CLAUDE_BIN` / `LYRA_UAT_CODEX_BIN`. |
| `set OPENROUTER_API_KEY in the environment` | The key must be in the environment of the `lyra-uat` process (or named by `brain.api_key_env`). |
| `exit_reason: no_finish` | The brain stopped without calling `finish`: out of turns, budget or an error — see `brain.error` and `harness/stderr.log`. |
| `exit_reason: budget_exhausted` | The persona spent its step budget; raise `step_budget` if the persona was making progress. |
| `warnings: … never clicked` | The persona moved by URL only. Read the trace; broken links and buttons cannot have been found. |
| `preseeded persona ran in an empty instance profile` | Another browser held the profile. Run such personas sequentially or give each its own `data_dir`. |
| Report in the wrong language | Set `report_language`; a harness no longer inherits the operator's. |
| `set OPENROUTER_API_KEY in the environment` although a key exists | The key has another name (`OPENROUTER_API`, say): pass it with `--api-key-env NAME` or `brain.api_key_env`. |
| `exit_reason: interrupted` | The run was stopped by SIGINT/SIGTERM; the report covers what happened before. |
