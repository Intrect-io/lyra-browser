# @intrect/lyra-browser

`npx` launcher for **lyra**: an MCP server that gives an agent one persistent,
logged-in Chromium that you can watch and take over. Site changes, form sends,
uploads, downloads and publishing each ask you first.

The server is a Python program. This package is a small launcher that starts the
matching release from PyPI with [uv](https://docs.astral.sh/uv/), pinned to the
dependency set the release was tested with. It has no dependencies of its own and
runs no install script.

## Use

Claude Code:

```bash
claude mcp add lyra -- npx -y @intrect/lyra-browser
```

Any other MCP client:

```json
{
  "mcpServers": {
    "lyra": { "command": "npx", "args": ["-y", "@intrect/lyra-browser"] }
  }
}
```

Keep the `-y`. Without it npx stops to ask whether to install the package, and
over stdio there is nobody to answer.

Arguments after the package name go to the server, so
`npx -y @intrect/lyra-browser --http --port 8765` serves over HTTP.

## Needs

- **Node 18 or later.**
- **uv.** The launcher looks for `uvx` on `PATH`, then in `~/.local/bin` and
  `~/.cargo/bin`. If it finds none it says how to install uv and exits; it never
  installs anything itself.
- **Chrome or Edge.** The server drives the browser you have. With neither, its tools
  answer `browser_unavailable`; then

  ```bash
  npx -y @intrect/lyra-browser install-browser
  ```

  adds the server's `[playwright]` extra and downloads Playwright's own Chromium builds
  into Playwright's browser cache, using the pinned Playwright release (a newer one
  would fetch a different Chromium revision). With Chrome or Edge installed none of
  this is needed: the server drives that browser with no Playwright at all. Extra arguments go to Playwright's installer, e.g.
  `install-browser --with-deps`, which needs root on Linux.

## Start-up time

The first start downloads the server's Python dependencies from PyPI (71 packages on the measured machine);
later starts reuse uv's cache. Claude Code gives an MCP server 30 seconds to connect.
Measured 2026-10-16 on Linux x86_64 (Node 24.20.0, npm 11.19.0, uv 0.11.8, a fast
connection), time to the server's `initialize` reply:

| | Time | Runs |
|---|---|---|
| First start, empty npm and uv caches | 3.6 to 8.7 s | 8 |
| Later starts | 1.4 to 3.4 s | 29 |

A slow connection lengthens the first start only. If your client gives up before it
finishes, run `npx -y @intrect/lyra-browser --help` once to fill the caches (measured:
4.6 s the first time, 2.0 s the second). Two clients starting at once on empty caches
both connected (2 trials; 7.2 and 11.9 s until both had answered).

Only Linux is measured. macOS and Windows are not.

## Behaviour worth knowing

- Nothing is written to stdout except the server's own MCP traffic.
- The dependency versions are pinned to the set the release was tested with
  (`constraints.txt`, exported from the repository's `uv.lock`), not resolved afresh.
- npm runs a bin through `sh -c`, which does not pass SIGTERM on. The launcher notices
  when its parent has gone and stops the server and its Chrome, so the profile is not
  left locked. Measured on the last run: after the client closed stdin, signalled npx,
  signalled the launcher, or killed the launcher and closed stdin, the profile lock was
  free within 0.7 s each time.
- Development only: `LYRA_BROWSER_PY_SPEC` replaces the requirement the launcher passes
  to uvx (a wheel path, a git URL) and `LYRA_BROWSER_PY_CONSTRAINTS` replaces the pin
  file; an empty value turns the pin off.

## Docs

Permissions, the navigation guard, every `LYRA_BROWSER_*` setting and the tool list
are in the [full README](https://github.com/Intrect-io/lyra-browser#readme).

## License

MIT
