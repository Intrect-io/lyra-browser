---
name: install-browser
description: Install a bundled Chromium into the lyra plugin's own environment, for a machine with no Chrome or Edge. Run it when a lyra tool answers browser_unavailable.
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_DATA}/venv/bin/python -m playwright install *)
---

lyra drives the system's Chrome or Edge when one is installed. When a tool
answers `browser_unavailable`, there is neither, so install Playwright's
bundled Chromium into the plugin's environment:

```bash
${CLAUDE_PLUGIN_DATA}/venv/bin/python -m playwright install chromium
```

It downloads a few hundred megabytes into `~/.cache/ms-playwright/`. Run it,
report the last lines of its output, and tell the user to call `open_browser`
again (the server tries Chrome, then Edge, then the bundled browser).

If the environment itself is missing (the command above says no such file),
the plugin's bootstrap has not finished or failed. Point the user at
`/lyra-browser:doctor` and the log it names.

On Linux the bundled browser may also need system libraries. Playwright lists
the missing ones in its error; `python -m playwright install-deps chromium`
installs them but needs root, so show the command rather than running it.
