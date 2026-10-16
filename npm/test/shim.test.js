'use strict';

// The shim is exercised against a fake `uvx` that records what it was given, so
// nothing here touches the network, PyPI or a browser.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const SHIM = path.join(__dirname, '..', 'bin', 'lyra-browser.js');
const CONSTRAINTS = path.join(__dirname, '..', 'constraints.txt');
const VERSION = require('../package.json').version;
const POSIX = process.platform !== 'win32';

// A fake uvx. FAKE_UVX_ARGS: file that receives one argv entry per line.
// FAKE_UVX_EXIT: exit code. FAKE_UVX_KILL=1: die from SIGKILL.
// FAKE_UVX_WAIT=<dir>: write <dir>/ready, then wait; record the first TERM or HUP
// in <dir>/signal and exit 0.
const FAKE_UVX = `#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_UVX_ARGS"
if [ -n "$FAKE_UVX_WAIT" ]; then
  trap 'echo TERM > "$FAKE_UVX_WAIT/signal"; exit 0' TERM
  trap 'echo HUP > "$FAKE_UVX_WAIT/signal"; exit 0' HUP
  : > "$FAKE_UVX_WAIT/ready"
  while :; do /bin/sleep 0.05; done
fi
[ -n "$FAKE_UVX_KILL" ] && kill -9 $$
exit "\${FAKE_UVX_EXIT:-0}"
`;

function sandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'lyra-shim-'));
  const bin = path.join(dir, 'bin');
  fs.mkdirSync(bin);
  const uvx = path.join(bin, 'uvx');
  fs.writeFileSync(uvx, FAKE_UVX, { mode: 0o755 });
  return { dir, bin, argsFile: path.join(dir, 'args') };
}

// HOME points at the sandbox so ~/.local/bin cannot find a real uv on this machine.
function envFor(sb, extra = {}) {
  return { PATH: sb.bin, HOME: sb.dir, USERPROFILE: sb.dir, FAKE_UVX_ARGS: sb.argsFile, ...extra };
}

function run(sb, args = [], extra = {}) {
  return spawnSync(process.execPath, [SHIM, ...args], { env: envFor(sb, extra), encoding: 'utf8' });
}

const recorded = (sb) => fs.readFileSync(sb.argsFile, 'utf8').trimEnd().split('\n');

test('runs the PyPI release matching this package, pinned to constraints.txt', { skip: !POSIX }, () => {
  const sb = sandbox();
  const r = run(sb);
  assert.equal(r.status, 0);
  assert.deepEqual(recorded(sb), [
    '--from',
    `lyra-browser==${VERSION}`,
    '--constraints',
    CONSTRAINTS,
    'lyra-browser',
  ]);
});

test('passes the caller\'s arguments to the server unchanged', { skip: !POSIX }, () => {
  const sb = sandbox();
  run(sb, ['--http', '--port', '8765', '--client', 'generic']);
  assert.deepEqual(recorded(sb).slice(4), ['lyra-browser', '--http', '--port', '8765', '--client', 'generic']);
});

test('install-browser runs the installer from the same pinned environment', { skip: !POSIX }, () => {
  const sb = sandbox();
  run(sb, ['install-browser', '--with-deps']);
  assert.deepEqual(recorded(sb), [
    '--quiet',
    '--from',
    `lyra-browser==${VERSION}`,
    '--constraints',
    CONSTRAINTS,
    'playwright',
    'install',
    'chromium',
    '--with-deps',
  ]);
});

test('LYRA_BROWSER_PY_SPEC replaces the requirement', { skip: !POSIX }, () => {
  const sb = sandbox();
  run(sb, [], { LYRA_BROWSER_PY_SPEC: '/tmp/lyra_browser-0.1.2-py3-none-any.whl' });
  assert.deepEqual(recorded(sb).slice(0, 2), ['--from', '/tmp/lyra_browser-0.1.2-py3-none-any.whl']);
});

test('LYRA_BROWSER_PY_CONSTRAINTS replaces the file; an empty value drops the pin', { skip: !POSIX }, () => {
  const sb = sandbox();
  run(sb, [], { LYRA_BROWSER_PY_CONSTRAINTS: '/tmp/other.txt' });
  assert.deepEqual(recorded(sb).slice(2, 4), ['--constraints', '/tmp/other.txt']);
  run(sb, [], { LYRA_BROWSER_PY_CONSTRAINTS: '' });
  assert.deepEqual(recorded(sb), ['--from', `lyra-browser==${VERSION}`, 'lyra-browser']);
});

test('the shim itself writes nothing to stdout', { skip: !POSIX }, () => {
  const sb = sandbox();
  const r = run(sb, ['--help']);
  assert.equal(r.stdout, '');
  assert.equal(r.stderr, '');
});

test('exits with the server\'s exit code', { skip: !POSIX }, () => {
  const sb = sandbox();
  assert.equal(run(sb, [], { FAKE_UVX_EXIT: '3' }).status, 3);
});

test('dies from the same signal the server died from', { skip: !POSIX }, () => {
  const sb = sandbox();
  const r = run(sb, [], { FAKE_UVX_KILL: '1' });
  assert.equal(r.signal, 'SIGKILL');
});

for (const signal of ['SIGTERM', 'SIGHUP']) {
  test(`forwards ${signal} to the server and follows it out`, { skip: !POSIX }, async () => {
    const sb = sandbox();
    const waitDir = fs.mkdtempSync(path.join(sb.dir, 'wait-'));
    const child = spawn(process.execPath, [SHIM], {
      env: envFor(sb, { FAKE_UVX_WAIT: waitDir }),
      stdio: 'ignore',
    });
    const exited = new Promise((resolve) => child.on('exit', (code, sig) => resolve({ code, sig })));
    const ready = path.join(waitDir, 'ready');
    for (let i = 0; i < 200 && !fs.existsSync(ready); i++) await new Promise((r) => setTimeout(r, 25));
    assert.ok(fs.existsSync(ready), 'the fake server never started');
    child.kill(signal);
    const { code, sig } = await exited;
    assert.equal(fs.readFileSync(path.join(waitDir, 'signal'), 'utf8').trim(), signal.slice(3));
    assert.equal(code, 0);
    assert.equal(sig, null);
  });
}

test('stops the server when its parent dies without passing a signal on', { skip: !POSIX }, async () => {
  // npm runs the bin through `sh -c`, which dies on SIGTERM and leaves the shim
  // behind. This sh stands in for it: it starts the shim, then is killed outright.
  const sb = sandbox();
  const waitDir = fs.mkdtempSync(path.join(sb.dir, 'wait-'));
  const parent = spawn('/bin/sh', ['-c', `"${process.execPath}" "${SHIM}" & wait`], {
    env: envFor(sb, { FAKE_UVX_WAIT: waitDir }),
    stdio: 'ignore',
  });
  const ready = path.join(waitDir, 'ready');
  for (let i = 0; i < 200 && !fs.existsSync(ready); i++) await new Promise((r) => setTimeout(r, 25));
  assert.ok(fs.existsSync(ready), 'the fake server never started');
  parent.kill('SIGKILL');
  const signalFile = path.join(waitDir, 'signal');
  for (let i = 0; i < 160 && !fs.existsSync(signalFile); i++) await new Promise((r) => setTimeout(r, 25));
  assert.ok(fs.existsSync(signalFile), 'the server was left running after its parent died');
  assert.equal(fs.readFileSync(signalFile, 'utf8').trim(), 'TERM');
});

test('without uv: says how to install it on stderr, nothing on stdout, exit 127', () => {
  const sb = sandbox();
  fs.rmSync(path.join(sb.bin, 'uvx'));
  const r = run(sb);
  assert.equal(r.status, 127);
  assert.equal(r.stdout, '');
  assert.match(r.stderr, /uv was not found/);
  assert.match(r.stderr, /astral\.sh\/uv/);
});

test('finds uvx in ~/.local/bin when PATH does not have it', { skip: !POSIX }, () => {
  const sb = sandbox();
  const local = path.join(sb.dir, '.local', 'bin');
  fs.mkdirSync(local, { recursive: true });
  fs.renameSync(path.join(sb.bin, 'uvx'), path.join(local, 'uvx'));
  const r = run(sb);
  assert.equal(r.status, 0);
  assert.equal(recorded(sb)[0], '--from');
});

test('a non-executable uvx is not picked up', { skip: !POSIX || process.getuid?.() === 0 }, () => {
  const sb = sandbox();
  fs.chmodSync(path.join(sb.bin, 'uvx'), 0o644);
  assert.equal(run(sb).status, 127);
});
