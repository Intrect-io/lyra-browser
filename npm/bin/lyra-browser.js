#!/usr/bin/env node
'use strict';

// npx entry point for lyra-browser. The server is a Python program; this file
// only finds `uvx` and runs it with the PyPI release that matches this package's
// version, pinned to the dependency set in constraints.txt (exported from uv.lock).
//
// stdout is the MCP transport from the moment the server starts: nothing in this
// file may write to it. Diagnostics go to stderr.

const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const pkg = require('../package.json');

const FORWARDED = ['SIGINT', 'SIGTERM', 'SIGHUP'];
const PARENT_POLL_MS = 250;
const STOP_GRACE_MS = 10000;

function isExecutable(file) {
  try {
    if (!fs.statSync(file).isFile()) return false;
    if (process.platform !== 'win32') fs.accessSync(file, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

// PATH first, then where uv's installers put it, in case the MCP client was
// started from an environment that never sourced the shell profile.
function findUvx(env, platform, home) {
  const names = platform === 'win32' ? ['uvx.exe'] : ['uvx'];
  const dirs = (env.PATH || '').split(path.delimiter).filter(Boolean);
  if (home) dirs.push(path.join(home, '.local', 'bin'), path.join(home, '.cargo', 'bin'));
  for (const dir of dirs) {
    for (const name of names) {
      const candidate = path.join(dir, name);
      if (isExecutable(candidate)) return candidate;
    }
  }
  return null;
}

// The uvx arguments for this package version. LYRA_BROWSER_PY_SPEC replaces the
// requirement (a wheel path or a git URL, for testing a build before it is on
// PyPI); LYRA_BROWSER_PY_CONSTRAINTS replaces the constraints file, and an empty
// value turns the pin off.
//
// `install-browser [args]` runs Playwright's installer from the same pinned
// environment. A bare `uvx --from lyra-browser playwright install` would resolve
// the newest Playwright, whose Chromium revision is not the one the server looks for.
function buildArgs(env, userArgs) {
  const spec = env.LYRA_BROWSER_PY_SPEC || `lyra-browser==${pkg.version}`;
  const installer = userArgs[0] === 'install-browser';
  // uv warns that `playwright` comes from a dependency of lyra-browser, not from
  // lyra-browser itself. That is the point here, and -q drops only uv's own notices.
  const args = installer ? ['--quiet', '--from', spec] : ['--from', spec];
  const constraints =
    env.LYRA_BROWSER_PY_CONSTRAINTS === undefined
      ? path.join(__dirname, '..', 'constraints.txt')
      : env.LYRA_BROWSER_PY_CONSTRAINTS;
  if (constraints) args.push('--constraints', constraints);
  if (installer) {
    args.push('playwright', 'install', 'chromium', ...userArgs.slice(1));
  } else {
    args.push('lyra-browser', ...userArgs);
  }
  return args;
}

const UV_MISSING = `lyra-browser: uv was not found (looked for uvx on PATH, ~/.local/bin and ~/.cargo/bin).
lyra-browser is a Python server; uv fetches it and its dependencies on the first run.
Install uv, then restart your MCP client:
  macOS / Linux  curl -LsSf https://astral.sh/uv/install.sh | sh
  Windows        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  or             brew install uv   /   pipx install uv   /   pip install uv
`;

function main() {
  const env = process.env;
  const constraints = env.LYRA_BROWSER_PY_CONSTRAINTS;
  if (constraints === undefined && !fs.existsSync(path.join(__dirname, '..', 'constraints.txt'))) {
    // Running unpinned would hand the user a dependency set nobody tested.
    process.stderr.write('lyra-browser: constraints.txt is missing from this package; reinstall it.\n');
    process.exit(1);
  }

  const uvx = findUvx(env, process.platform, os.homedir());
  if (!uvx) {
    process.stderr.write(UV_MISSING);
    process.exit(127);
  }

  const child = spawn(uvx, buildArgs(env, process.argv.slice(2)), {
    stdio: 'inherit',
    env,
    windowsHide: true,
  });

  // The child shares our stdin, so it sees the client closing the pipe itself.
  // Signals are the other way a client stops a server.
  const forward = (signal) => {
    try {
      child.kill(signal);
    } catch {
      // already gone
    }
  };
  for (const signal of FORWARDED) process.on(signal, forward);

  // npm runs a bin through `sh -c`, and sh dies on SIGTERM without passing it on. A
  // client that signals npx therefore leaves this process, the server and Chrome
  // running, with the profile lock held and the next session on an empty profile.
  // A changed parent is how that shows: stop the server the way a signal would.
  if (process.platform !== 'win32') {
    const parent = process.ppid;
    const watch = setInterval(() => {
      if (process.ppid === parent) return;
      clearInterval(watch);
      forward('SIGTERM');
      setTimeout(() => forward('SIGKILL'), STOP_GRACE_MS).unref();
    }, PARENT_POLL_MS);
    watch.unref();
  }

  child.on('error', (err) => {
    process.stderr.write(`lyra-browser: could not run ${uvx}: ${err.message}\n`);
    process.exit(err.code === 'EACCES' ? 126 : 127);
  });

  child.on('exit', (code, signal) => {
    if (signal) {
      // Die the way the child died, so the parent sees a signal and not a code.
      for (const name of FORWARDED) process.removeAllListeners(name);
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
}

if (require.main === module) {
  main();
} else {
  module.exports = { buildArgs, findUvx };
}
