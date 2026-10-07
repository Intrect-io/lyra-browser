# Deploying lyra-browser on Cloudflare Containers

lyra-browser cannot run on Workers itself: it needs a subprocess (the Playwright
driver), `fcntl` file locks and a real Chrome, none of which exist in a Worker
isolate. So Chrome and the Python server run in a **Cloudflare Container**, and a
small **Worker + Durable Object** in front authenticates callers and routes every
request to that one container.

```
MCP client ──HTTPS + Bearer──> Worker ──> Durable Object ──> Container
                                                              lyra-browser --http (client=remote)
                                                              + Chrome stable, headless
```

## What you get

- One operator, one container (`"default"`), one shared Bearer token.
- A **headless** browser. Nobody sits at it, so `ask_user_to_do`, `highlight` and
  takeover answer `unattended`. Anything that needs a person (a login, a CAPTCHA)
  is not possible here.
- `screenshot` / `read_image` return the PNG as an MCP **image block**, so the
  model sees the page. `image_path` is a file inside the container; ignore it.
- Every audit record is also written to stderr as a JSON line, which Workers Logs
  collects.

## What you do not get

- **State is volatile.** The container disk is ephemeral. Logins, cookies and
  `audit.jsonl` last only while the container is awake and vanish when it sleeps
  (30 min after the last request) or is replaced. Audit survives in Workers Logs,
  for as long as Cloudflare retains them on your plan.
- No file transfer: a download stays in the container, and `upload_file` can only
  read files inside it.
- No private-network blocking and no noVNC takeover.
- No OAuth. Clients that can only authenticate with OAuth (Claude.ai custom
  connectors) cannot use this Worker.

## Prerequisites

- A Cloudflare account on **Workers Paid** (Containers require it).
- Docker running locally: `wrangler deploy` builds the image from the repository
  `Dockerfile` (linux/amd64) and pushes it.
- Node 20+.

## Deploy

```bash
cd deploy/cloudflare
npm ci
npx wrangler secret put LYRA_MCP_TOKEN     # a long random string; clients send it as a Bearer token
npm run deploy
```

The first deploy takes several minutes while the container image rolls out.
Check it:

```bash
.venv/bin/python scripts/verify_remote_e2e.py \
    --url https://lyra-browser.<subdomain>.workers.dev/mcp \
    --token "$LYRA_MCP_TOKEN" --expect-auth
```

## Connect a client

```json
{
  "mcpServers": {
    "lyra-browser": {
      "url": "https://lyra-browser.<subdomain>.workers.dev/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

A client that cannot set headers can go through a bridge:
`npx mcp-remote <url> --header "Authorization: Bearer ${TOKEN}"`.

## Configuration

Every **string** var on the Worker whose name starts with `LYRA_BROWSER_` is passed
to the server, so settings change without rebuilding the image. In
`wrangler.jsonc`:

```jsonc
"vars": {
  "LYRA_BROWSER_TRUSTED_ORIGINS": "https://example.com",
  "LYRA_BROWSER_CONSENT_CHANNEL": "elicit"
}
```

`LYRA_MCP_TOKEN` is not forwarded; it stays in the Worker.

**Consent.** The default channel is `auto`: a client that supports MCP elicitation
is asked; one that does not falls back to the model passing `confirm=true`. That
fallback means the model can approve its own actions. For strict human approval set
`LYRA_BROWSER_CONSENT_CHANNEL=elicit` — and use a client that implements
elicitation.

The image fixes `LYRA_BROWSER_CLIENT=remote`, `HEADLESS=true`, `CHANNEL=chrome`,
`ALLOW_BUNDLED=false` (a missing Chrome fails loudly rather than running a different
browser) and `AUDIT_STDERR=true`.

## Operating notes

- `instance_type` is `standard-2` (1 vCPU, 6 GiB). `max_instances` is 1: the
  server holds one profile and refuses a second claim.
- `sleepAfter` is `30m`, longer than the owner-idle (15 min) and consent-wait
  (5 min) windows, so a pause mid-task does not lose the tabs.
- The container image has no CJK fonts; add `fonts-noto-cjk` to the `Dockerfile` if
  Korean/Chinese/Japanese pages screenshot as boxes.
- Rotate the token with `npx wrangler secret put LYRA_MCP_TOKEN`.

## Local checks

```bash
docker build -t lyra-browser:cf .
docker run --rm -d --name lyra-cf -p 127.0.0.1:8765:8765 --shm-size=64m lyra-browser:cf
curl -fsS localhost:8765/healthz          # ok
.venv/bin/python scripts/verify_remote_e2e.py
docker stop lyra-cf
```

`wrangler dev` runs the Worker and the container locally (needs Docker). Put
`LYRA_MCP_TOKEN=dev-token` in `deploy/cloudflare/.dev.vars`.
