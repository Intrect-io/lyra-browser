---
name: install-browser
description: Install a bundled Chromium into the lyra plugin's own environment, for a machine with no Chrome or Edge. Run it when a lyra tool answers browser_unavailable.
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_DATA}/venv/bin/python -m pip install *), Bash(${CLAUDE_PLUGIN_DATA}/venv/bin/python -m playwright install *)
---

lyra drives the system's Chrome or Edge when one is installed, with no Playwright
at all. When a tool answers `browser_unavailable`, there is neither, so give the
plugin's environment Playwright and its bundled Chromium:

```bash
${CLAUDE_PLUGIN_DATA}/venv/bin/python -m pip install "playwright>=1.59,<2"
${CLAUDE_PLUGIN_DATA}/venv/bin/python -m playwright install chromium
```

The first line is needed because Playwright is no longer one of the server's own
dependencies (it is the `[playwright]` extra). The second downloads a few hundred
megabytes into `~/.cache/ms-playwright/`. Run both, report the last lines of their
output, and tell the user to call `open_browser` again: with no Chrome or Edge on
the machine, the server's `auto` driver falls back to Playwright and its bundled
browser.

If the environment itself is missing (the commands above say no such file),
the plugin's bootstrap has not finished or failed. Point the user at
`/lyra-browser:doctor` and the log it names.

On Linux the bundled browser may also need system libraries. Playwright lists
the missing ones in its error; `python -m playwright install-deps chromium`
installs them but needs root, so show the command rather than running it.
