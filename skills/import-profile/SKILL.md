---
name: import-profile
description: Import the logins (cookies, site storage) of one of your own Chrome or Edge profiles into lyra's browser profile, or re-sync one imported before, so lyra acts as you on those sites.
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_DATA}/venv/bin/lyra-browser --client claude-code profile *)
---

This copies the user's own logins into lyra: on those sites the agent then acts as
them. Run it only because the user asked for it, and let them pick the profile.

1. Close lyra's browser first if it is open (`close_browser`): the import needs the
   profile lock the running browser holds, and says so if it cannot get it.

2. List what can be imported, and show the list to the user:

   ```bash
   ${CLAUDE_PLUGIN_DATA}/venv/bin/lyra-browser --client claude-code profile list
   ```

   Each line is a profile's directory (`Default`, `Profile 2`) and the name the
   browser shows. If the user has not said which one, ask them; never pick for them.

3. Import it (add `--browser msedge` for an Edge profile, `--sites a.com,b.com` to
   bring only those sites; a site list leaves localStorage out, it cannot be filtered):

   ```bash
   ${CLAUDE_PLUGIN_DATA}/venv/bin/lyra-browser --client claude-code profile import --profile "Default"
   ```

   Running it again later is a sync: what it brings replaces what lyra had for the
   same sites. `profile status` shows what was imported last.

4. Report the import's lines to the user. If it says the login keyring is locked
   (a session over SSH), tell them to unlock it from their desktop session and run
   this again, or to re-run with `--unlock-keyring` there; do not retry by yourself.
   A warning about undecryptable cookies means those logins did not come across.

Then `open_browser` again: its reply carries `imported_from`. To have every
`open_browser` re-import that profile when it changed, the user turns on the
plugin's "Sync the imported profile" option (`LYRA_BROWSER_PROFILE_SYNC`).
