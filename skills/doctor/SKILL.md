---
name: doctor
description: Report what the lyra plugin finds on this machine - its Python environment, uv, the browser it would drive, the display, and whether the shared profile is held by another session.
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/claude-plugin/doctor.sh *)
---

Run the plugin's doctor and show its output to the user:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/claude-plugin/doctor.sh ${CLAUDE_PLUGIN_DATA}
```

Then say, in a sentence each, what it means for them:

- `venv: not built yet` or no `lyra-browser` line: the bootstrap that runs at
  session start has not finished or failed. The log is
  `${CLAUDE_PLUGIN_DATA}/bootstrap.log`; a first build takes under a minute
  with `uv`, longer with the pip fallback. Starting a new session retries it.
- No `browser:` line and `bundled: none`: offer `/lyra-browser:install-browser`.
- `display:` with neither variable on Linux: the browser runs headless, nobody
  can watch, and the collaboration tools answer `unattended`.
- `profile lock: held`: another lyra session is on the shared profile; this one
  will get an instance profile without logins until that one closes.
