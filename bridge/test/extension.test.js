'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const Module = require('node:module');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const core = require('../src/core');
// The allowed roots these tests run with (core.allowedRoots reads this first): the made-up folders they use.
process.env.REMOTE_HANDS_ALLOWED_ROOTS = '/home/user/src/:/home/user/.worktrees/';

const FOLDER = '/home/user/src/repo-a';
const RID = 'req-0001-abcd';
// The bridge refuses any session id but the one derived from the request id, so a start sends the pair
// the service would: start() derives the session id unless a test passes another on purpose.
const SID = core.sessionIdFor(RID);
const OTHER_SID = '00000000-0000-5000-8000-000000000000'; // valid, but no request id derives it
// 43 URL-safe characters like a real token, and lopsided on purpose (mixed case, digits, '-',
// '_', not a palindrome, not sorted): a uniform token reads the same reversed, sorted,
// lower-cased, or stripped, so a check that mangled the presented token would still pass.
// Made up for tests, not a credential (hence gitleaks:allow).
const TOKEN = 'uHSEiyLq_2OgI1mwLGyzugcJdL4Foz-12gmQopYLCjk'; // gitleaks:allow
const PROMPT = 'fix the --looks-like-a-flag option';
const SKIP_AS_ROOT = typeof process.getuid === 'function' && process.getuid() === 0 ? 'running as root' : false; // file modes do not stop root
const LIMIT_MS = 10000; // a stalled request fails its test instead of hanging the run

const created = [];
const shows = [];
let exitStatus;
let silentPid = false; // a terminal whose process id never arrives
let onFoldersChanged; // the window's own listener for workspace folder changes
let onCreateTerminal; // called at the instant a terminal is created
const FOLDERS = [{ uri: { scheme: 'vscode-remote', path: FOLDER } }];
const fakeVscode = {
  env: { remoteName: 'wsl' },
  workspace: {
    workspaceFolders: FOLDERS,
    onDidChangeWorkspaceFolders: (listener) => {
      onFoldersChanged = listener;
      return { dispose() {} };
    },
  },
  window: {
    state: { focused: false },
    createTerminal: (options) => {
      if (onCreateTerminal) onCreateTerminal(options);
      created.push(options);
      return {
        processId: silentPid ? new Promise(() => {}) : Promise.resolve(777),
        get exitStatus() {
          return exitStatus;
        },
        show: (preserveFocus) => shows.push(preserveFocus),
      };
    },
  },
};
const originalLoad = Module._load;
Module._load = function load(request, ...rest) {
  if (request === 'vscode') return fakeVscode;
  return originalLoad.call(this, request, ...rest);
};

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-home-'));
const userHome = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-user-'));
fs.writeFileSync(path.join(home, 'token'), TOKEN);
fs.mkdirSync(path.join(userHome, '.local', 'bin'), { recursive: true });
fs.writeFileSync(path.join(userHome, '.local', 'bin', 'claude'), '#!/bin/sh\n', { mode: 0o755 });
process.env.REMOTE_HANDS_HOME = home;
process.env.HOME = userHome;
// The bridge keeps its state in the Remote Hands folder (REMOTE_HANDS_HOME, a temporary one here): the
// session reservations, and one audit file per window. None of it may go to the WSL home, and these tests
// must never reach the real folders.
assert.equal(os.homedir(), userHome, 'HOME must point at the temporary folder');
const sessionsDir = path.join(home, 'bridge-sessions');
const auditDir = path.join(home, 'bridge-audit');
const wslStateDir = path.join(userHome, '.local', 'state'); // where it used to go: it must stay absent

// Claude's config, where it keeps folder trust, is ~/.claude.json: the temporary HOME's here, never the real
// one. The folders these tests start in are made up (a test may not make folders under the allowed roots), so
// Claude's key for each is given here as the folder itself, as for a plain repository, and both are trusted.
const OTHER_FOLDER = '/home/user/.worktrees/repo-b';
const claudeConfigFile = path.join(userHome, '.claude.json');
const claudeConfigLock = `${claudeConfigFile}.lock`;
const legacyConfigFile = path.join(userHome, '.claude', '.config.json'); // Claude's legacy config, which it prefers when it exists
const CLAUDE_CONFIG = JSON.stringify(
  {
    numStartups: 12,
    projects: {
      // Trusted, with external CLAUDE.md imports approved: Claude asks neither of its one-time questions here.
      [FOLDER]: { allowedTools: [], hasTrustDialogAccepted: true, hasClaudeMdExternalIncludesApproved: true, hasClaudeMdExternalIncludesWarningShown: true },
      [OTHER_FOLDER]: { allowedTools: [], hasTrustDialogAccepted: true, hasClaudeMdExternalIncludesApproved: true, hasClaudeMdExternalIncludesWarningShown: true },
    },
  },
  null,
  2,
);
fs.writeFileSync(claudeConfigFile, CLAUDE_CONFIG, { mode: 0o600 });
const MADE_UP_FOLDERS = new Set([FOLDER, OTHER_FOLDER]);
const realCanonicalRoot = core.canonicalRoot;
core.canonicalRoot = (folder) => (MADE_UP_FOLDERS.has(folder) ? folder : realCanonicalRoot(folder));
// Claude's key for FOLDER in a test that needs another one (for example the main repository of a worktree).
const keyFolderAs = (t, key) => t.mock.method(core, 'canonicalRoot', (folder) => (folder === FOLDER ? key : realCanonicalRoot(folder)));
const claudeFiles = () => fs.readdirSync(userHome).filter((name) => name.startsWith('.claude.json')).sort();

const extension = require('../src/extension');
extension.timing.startCheckMs = 10;
extension.timing.pidWaitMs = 10; // the fake terminal answers at once; a long wait would only keep this process alive

function registration(root = home) {
  const dir = path.join(root, 'windows');
  const files = fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => f.endsWith('.json')) : [];
  return files.map((f) => JSON.parse(fs.readFileSync(path.join(dir, f), 'utf8')));
}

function start(port, requestId = RID, cwd = FOLDER, sessionId = core.sessionIdFor(requestId)) {
  return post(port, {
    request_id: requestId,
    session_id: sessionId,
    cwd,
    label: 'hands repo-a req-00',
    prompt: PROMPT,
  });
}

function post(port, payload, token = TOKEN) {
  const data = Buffer.from(JSON.stringify(payload));
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port,
        method: 'POST',
        path: '/start-claude',
        agent: false,
        headers: { authorization: `Bearer ${token}`, 'content-length': data.length },
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => resolve({ status: res.statusCode, json: JSON.parse(Buffer.concat(chunks).toString()) }));
      },
    );
    req.on('error', reject);
    req.end(data);
  });
}

test('activate registers, starts Claude in a terminal without moving focus, and cleans up', { timeout: LIMIT_MS }, async () => {
  await extension.activate({ subscriptions: [] });
  const [reg] = registration();
  assert.deepEqual(reg.folders, [FOLDER]);
  assert.match(reg.instance, /^[0-9a-f-]{36}$/);

  const res = await start(reg.port);
  assert.equal(res.status, 200);
  assert.deepEqual(res.json, { status: 'started', session_id: SID, terminal_pid: 777 });
  assert.deepEqual(created, [
    {
      name: 'Claude: hands repo-a req-00',
      shellPath: path.join(userHome, '.local', 'bin', 'claude'),
      shellArgs: ['--session-id', SID, '--remote-control', 'hands repo-a req-00', '--name', 'hands repo-a req-00', '--', 'fix the --looks-like-a-flag option'],
      cwd: FOLDER,
      isTransient: true,
    },
  ]);
  assert.deepEqual(shows, [true]);

  const again = await start(reg.port);
  assert.equal(again.status, 409, 'the same request id, so the same session id, never starts twice in one window');

  // Two simultaneous starts of one new request id (so one new session id): the check and the claim
  // happen with no await between them, so exactly one wins.
  const twin = 'req-twin-0001';
  const both = await Promise.all([start(reg.port, twin), start(reg.port, twin)]);
  assert.deepEqual(both.map((r) => r.status).sort(), [200, 409]);
  // And the loser never got in between the check and the reservation: it was refused at the check, so it
  // wrote no intent of its own. Only the winner's intent is on record for that session id.
  const twinIntents = auditRecords().filter((r) => r.event === 'start_intent' && r.session_id === core.sessionIdFor(twin));
  assert.equal(twinIntents.length, 1, 'the twin start left one intent on record, not two');

  exitStatus = { code: 1 };
  const failed = await start(reg.port, 'req-exits-0001');
  assert.equal(failed.status, 500);
  assert.equal(failed.json.reason, 'exited');

  extension.deactivate();
  assert.deepEqual(registration(), []);
});

// What the bridge has audited so far, one parsed record per line: in one window's file (named by its
// instance id), or in every window's.
const auditPath = (instance) => path.join(auditDir, `${instance}.jsonl`);
function auditFiles() {
  return fs.existsSync(auditDir) ? fs.readdirSync(auditDir).sort() : [];
}
function auditRecords(instance) {
  const names = instance ? [`${instance}.jsonl`] : auditFiles();
  return names.flatMap((name) => {
    const file = path.join(auditDir, name);
    if (!fs.existsSync(file)) return [];
    return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line));
  });
}
const withoutTime = ({ ts, ...rest }) => rest;

// The servers the glue creates, so a test can see whether one is listening and reach its events.
function captureServers(t) {
  const servers = [];
  const realCreateServer = http.createServer;
  t.mock.method(http, 'createServer', (...args) => {
    const server = realCreateServer(...args);
    servers.push(server);
    return server;
  });
  return servers;
}

// One window's lifecycle: activate, hand its registration to fn, and always deactivate.
async function withWindow(fn) {
  exitStatus = undefined;
  await extension.activate({ subscriptions: [] });
  try {
    const [reg] = registration();
    return await fn(reg);
  } finally {
    extension.deactivate();
  }
}

function canConnect(host, port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port });
    socket.once('connect', () => {
      socket.destroy();
      resolve(true);
    });
    socket.once('error', () => resolve(false));
  });
}

test('the kill switch refuses with its own reason, starts nothing, and burns no session id', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const requestId = 'req-killsw-0001';
    const off = path.join(home, 'OFF');
    const before = created.length;
    fs.writeFileSync(off, '');
    try {
      const refused = await start(reg.port, requestId);
      assert.equal(refused.status, 403);
      assert.equal(refused.json.reason, 'kill_switch');
      assert.equal(created.length, before);
    } finally {
      fs.rmSync(off, { force: true });
    }
    // With the switch file gone the same request id starts, so the refusal used nothing up.
    assert.equal((await start(reg.port, requestId)).status, 200);
  }));

test('a session id Claude already has a record for is refused, whichever project holds it', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const requestId = 'req-record-0001';
    const project = path.join(userHome, '.claude', 'projects', '-home-user-src-other');
    fs.mkdirSync(project, { recursive: true });
    fs.writeFileSync(path.join(project, `${core.sessionIdFor(requestId)}.jsonl`), '');
    const before = created.length;
    const res = await start(reg.port, requestId);
    assert.equal(res.status, 409);
    assert.equal(res.json.reason, 'duplicate_session');
    assert.equal(created.length, before);
  }));

test('the bridge listens on 127.0.0.1 only (rule C1)', { timeout: LIMIT_MS }, async (t) => {
  // Linux answers all of 127.0.0.0/8 locally, so a server bound to 127.0.0.1 alone must refuse a
  // connection to 127.0.0.2. Control: a server bound to every address does answer there. Where it
  // does not, the check would prove nothing, so it is skipped.
  const wildcard = net.createServer((socket) => socket.destroy());
  await new Promise((resolve) => wildcard.listen(0, '0.0.0.0', resolve));
  const controlReachable = await canConnect('127.0.0.2', wildcard.address().port);
  await new Promise((resolve) => wildcard.close(resolve));
  if (!controlReachable) return t.skip('127.0.0.2 is not a local address on this platform');
  return withWindow(async (reg) => {
    assert.equal(await canConnect('127.0.0.1', reg.port), true, 'the bridge answers on 127.0.0.1');
    assert.equal(await canConnect('127.0.0.2', reg.port), false, 'and on no other address');
  });
});

test('a token file that ends with a newline still works, as it does for the service', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const file = path.join(home, 'token');
    fs.writeFileSync(file, `${TOKEN}\n`);
    try {
      assert.equal((await start(reg.port, 'req-newline-0001')).status, 200);
    } finally {
      fs.writeFileSync(file, TOKEN);
    }
  }));

test('a change of workspace folders is registered at once, and the new folder can start Claude', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const other = '/home/user/.worktrees/repo-b';
    const original = fakeVscode.workspace.workspaceFolders;
    fakeVscode.workspace.workspaceFolders = [...original, { uri: { scheme: 'vscode-remote', path: other } }];
    try {
      onFoldersChanged({ added: [], removed: [] });
      const [after] = registration();
      assert.deepEqual(after.folders, [FOLDER, other]);
      assert.equal(after.instance, reg.instance, 'same window, same instance');
      assert.equal(after.port, reg.port);
      assert.equal((await start(reg.port, 'req-folder-0001', other)).status, 200);
      assert.equal(created.at(-1).cwd, other);
    } finally {
      fakeVscode.workspace.workspaceFolders = original;
    }
  }));

test('the heartbeat puts a removed registration back within 30 s', { timeout: LIMIT_MS }, async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  await withWindow(async (reg) => {
    // What the service does to a registration whose bridge missed one ping.
    fs.unlinkSync(path.join(home, 'windows', `${reg.instance}.json`));
    assert.deepEqual(registration(), []);
    t.mock.timers.tick(30000);
    assert.deepEqual(registration().map((r) => r.instance), [reg.instance]);
  });
});

test('a missing or non-executable Claude is a refusal that reserves nothing, so the same session id starts later', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const claude = path.join(userHome, '.local', 'bin', 'claude');
    const requestId = 'req-noclaude-0001';
    const before = created.length;
    try {
      fs.renameSync(claude, `${claude}.away`);
      const missing = await start(reg.port, requestId);
      assert.equal(missing.status, 409);
      assert.equal(missing.json.status, 'refused');
      assert.equal(missing.json.reason, 'claude_unavailable');
      fs.renameSync(`${claude}.away`, claude);
      fs.chmodSync(claude, 0o644);
      const notExecutable = await start(reg.port, requestId);
      assert.equal(notExecutable.json.reason, 'claude_unavailable');
      assert.equal(created.length, before, 'no terminal is created in the window');
    } finally {
      if (fs.existsSync(`${claude}.away`)) fs.renameSync(`${claude}.away`, claude);
      fs.chmodSync(claude, 0o755);
    }
    // Claude is installed now: the same request id starts, because the refusals reserved nothing.
    assert.equal((await start(reg.port, requestId)).status, 200);
    assert.equal(created.length, before + 1);
  }));

test('a terminal that never reports its process id still starts, with the pid unknown', { timeout: LIMIT_MS }, async () => {
  const requestId = 'req-silentpid-0001';
  silentPid = true;
  try {
    await withWindow(async (reg) => {
      const res = await start(reg.port, requestId);
      assert.equal(res.status, 200);
      assert.deepEqual(res.json, { status: 'started', session_id: core.sessionIdFor(requestId), terminal_pid: null });
    });
  } finally {
    silentPid = false;
  }
});

test('the registration is published by renaming a finished temporary file', { timeout: LIMIT_MS }, (t) => {
  const rename = t.mock.method(fs, 'renameSync');
  return withWindow(async (reg) => {
    const final = path.join(home, 'windows', `${reg.instance}.json`);
    assert.equal(rename.mock.callCount(), 1);
    const [from, to] = rename.mock.calls[0].arguments;
    assert.equal(to, final);
    assert.ok(from.startsWith(`${final}.`) && from.endsWith('.tmp'), from);
    assert.deepEqual(fs.readdirSync(path.dirname(final)), [path.basename(final)], 'no temporary file is left behind');
  });
});

test('the heartbeat is unref-ed, so it never keeps its process alive', { timeout: LIMIT_MS }, (t) => {
  const realSetInterval = globalThis.setInterval;
  const heartbeats = [];
  t.mock.method(globalThis, 'setInterval', (...args) => {
    const timer = realSetInterval(...args);
    heartbeats.push(timer);
    return timer;
  });
  return withWindow(async () => {
    assert.equal(heartbeats.length, 1);
    assert.equal(heartbeats[0].hasRef(), false);
  });
});

test('a registration that cannot be written does not stop the window, and the heartbeat registers it once it can', { timeout: LIMIT_MS }, async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const logged = t.mock.method(console, 'error', () => {});
  const servers = captureServers(t);
  // The Remote Hands folder cannot be made: a regular file sits where its parent should be (ENOTDIR).
  const blocker = path.join(userHome, 'blocker');
  const blockedHome = path.join(blocker, 'remote-hands');
  fs.writeFileSync(blocker, '');
  process.env.REMOTE_HANDS_HOME = blockedHome;

  await extension.activate({ subscriptions: [] }); // resolves: a failed registration is not a failed activation
  assert.equal(servers.length, 1);
  assert.equal(servers[0].listening, true);
  assert.equal(logged.mock.callCount(), 1);
  assert.match(String(logged.mock.calls[0].arguments[0]), /ENOTDIR/);

  // Once the folder can be made, the next heartbeat registers the window it already serves.
  fs.rmSync(blocker);
  fs.mkdirSync(blocker);
  t.mock.timers.tick(30000);
  const [reg] = registration(blockedHome);
  assert.equal(reg.port, servers[0].address().port);
  assert.deepEqual(reg.folders, [FOLDER]);
});

test('without REMOTE_HANDS_HOME the window uses %LOCALAPPDATA%\\remote-hands as Windows passes it in through WSLENV', { timeout: LIMIT_MS }, async (t) => {
  // A WSL path, as WSLENV=LOCALAPPDATA/p gives it, so Windows is never asked (and no real folder is touched).
  const localAppData = path.join(userHome, 'AppData', 'Local');
  const derivedHome = path.join(localAppData, 'remote-hands');
  delete process.env.REMOTE_HANDS_HOME;
  process.env.LOCALAPPDATA = localAppData;
  t.after(() => {
    delete process.env.LOCALAPPDATA;
    fs.rmSync(path.join(userHome, 'AppData'), { recursive: true, force: true });
  });
  const servers = captureServers(t);
  await extension.activate({ subscriptions: [] });
  const [reg] = registration(derivedHome);
  assert.equal(reg.port, servers[0].address().port);
  assert.deepEqual(reg.folders, [FOLDER]);
});

test('a registration whose rename fails leaves no temporary file behind', { timeout: LIMIT_MS }, async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  t.mock.method(console, 'error', () => {});
  const rename = t.mock.method(fs, 'renameSync', () => {
    throw new Error('EBUSY: simulated');
  });
  await extension.activate({ subscriptions: [] });
  assert.deepEqual(fs.readdirSync(path.join(home, 'windows')), [], 'neither a registration nor a .tmp file');
  rename.mock.restore();
  t.mock.timers.tick(30000);
  assert.equal(registration().length, 1);
});

test('a folder change whose rewrite fails is logged, not thrown, and the heartbeat retries it', { timeout: LIMIT_MS }, (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const logged = t.mock.method(console, 'error', () => {});
  return withWindow(async () => {
    const other = '/home/user/.worktrees/repo-b';
    fakeVscode.workspace.workspaceFolders = [...FOLDERS, { uri: { scheme: 'vscode-remote', path: other } }];
    const rename = t.mock.method(fs, 'renameSync', () => {
      throw new Error('EBUSY: simulated');
    });
    onFoldersChanged({ added: [], removed: [] }); // the same guarded registration: it must not throw
    assert.equal(logged.mock.callCount(), 1);
    assert.deepEqual(registration()[0].folders, [FOLDER], 'the old record is still there, stale');
    rename.mock.restore();
    t.mock.timers.tick(30000); // the file exists, but the last write failed, so the heartbeat writes again
    assert.deepEqual(registration()[0].folders, [FOLDER, other]);
  });
});

test('the heartbeat leaves a healthy registration alone', { timeout: LIMIT_MS }, (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] });
  const rename = t.mock.method(fs, 'renameSync');
  return withWindow(async () => {
    assert.equal(rename.mock.callCount(), 1);
    t.mock.timers.tick(30000);
    t.mock.timers.tick(30000);
    assert.equal(rename.mock.callCount(), 1, 'nothing was rewritten');
  });
});

test('started_at is the activation time, and a later registration keeps it', { timeout: LIMIT_MS }, (t) => {
  const activatedAt = '2026-10-04T12:00:00.000Z';
  t.mock.timers.enable({ apis: ['setInterval', 'Date'], now: Date.parse(activatedAt) });
  return withWindow(async (reg) => {
    assert.equal(reg.started_at, activatedAt);
    t.mock.timers.tick(45000); // time passes, then the window gains a folder
    const other = '/home/user/.worktrees/repo-b';
    fakeVscode.workspace.workspaceFolders = [...FOLDERS, { uri: { scheme: 'vscode-remote', path: other } }];
    onFoldersChanged({ added: [], removed: [] });
    const [after] = registration();
    assert.deepEqual(after.folders, [FOLDER, other]);
    assert.equal(after.started_at, activatedAt);
  });
});

test('a window that is not a WSL window listens on nothing and registers nothing', { timeout: LIMIT_MS }, async (t) => {
  const servers = captureServers(t);
  const logged = t.mock.method(console, 'log', () => {});
  for (const remoteName of [undefined, 'ssh-remote', 'dev-container', 'codespaces']) {
    fakeVscode.env.remoteName = remoteName;
    const subscriptions = [];
    await extension.activate({ subscriptions });
    assert.equal(servers.length, 0, String(remoteName));
    assert.deepEqual(registration(), [], String(remoteName));
    assert.equal(subscriptions.length, 0, String(remoteName));
  }
  assert.equal(logged.mock.callCount(), 4, 'each refusal says why');
});

test('a server error after listening is logged, however often it happens, and never thrown', { timeout: LIMIT_MS }, (t) => {
  const logged = t.mock.method(console, 'error', () => {});
  const servers = captureServers(t);
  return withWindow(async () => {
    servers[0].emit('error', new Error('accept failed: first'));
    servers[0].emit('error', new Error('accept failed: second'));
    assert.equal(logged.mock.callCount(), 2);
    assert.match(String(logged.mock.calls[1].arguments[0]), /accept failed: second/);
  });
});

test('the new terminal is shown only while its window is not focused, so the user is never switched out of their own terminal', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const createdBefore = created.length;
    const showsBefore = shows.length;
    fakeVscode.window.state.focused = true;
    assert.equal((await start(reg.port, 'req-focus-0001')).status, 200);
    assert.equal(created.length, createdBefore + 1, 'the terminal is still created, in the terminal list');
    assert.equal(shows.length, showsBefore, 'but it is not shown while the window has focus');
    fakeVscode.window.state.focused = false;
    assert.equal((await start(reg.port, 'req-focus-0002')).status, 200);
    assert.deepEqual(shows.slice(showsBefore), [true], 'an unfocused window shows it once, keeping the focus where it is');
  }));

test('a window whose focus state is unknown never has the terminal shown', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const showsBefore = shows.length;
    fakeVscode.window.state = undefined;
    assert.equal((await start(reg.port, 'req-nofocus-0001')).status, 200);
    assert.equal(shows.length, showsBefore);
  }));

test('every terminal the bridge creates is transient, so Cursor never restores or re-runs it (C6)', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const before = created.length;
    fakeVscode.window.state.focused = true;
    assert.equal((await start(reg.port, 'req-transient-0001')).status, 200);
    fakeVscode.window.state.focused = false;
    assert.equal((await start(reg.port, 'req-transient-0002')).status, 200);
    assert.equal(created.length, before + 2, 'one terminal per start, shown or not');
    // These two and every terminal an earlier test made: a restored one could start in a worktree the
    // session moved into, miss its own record, and send the prompt again.
    const restorable = created.filter((options) => options.isTransient !== true);
    assert.deepEqual(restorable, []);
  }));

test('a session id that is not the one derived from the request id is refused with bad_request, and nothing starts', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const before = created.length;
    const res = await start(reg.port, RID, FOLDER, OTHER_SID);
    assert.equal(res.status, 400);
    assert.equal(res.json.status, 'refused');
    assert.equal(res.json.reason, 'bad_request');
    assert.equal(created.length, before, 'no terminal is created');
  }));

test('one request id cannot start two sessions in a window, whichever session id comes first', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const before = created.length;
    assert.equal((await start(reg.port)).status, 200);
    assert.equal((await start(reg.port, RID, FOLDER, OTHER_SID)).status, 400, 'a second session id for the same request id');
    assert.equal(created.length, before + 1, 'one terminal, not two');
  }));

test('two simultaneous starts of one request id with different session ids start at most one', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const before = created.length;
    const both = await Promise.all([start(reg.port, RID, FOLDER, OTHER_SID), start(reg.port)]);
    assert.deepEqual(both.map((res) => res.status), [400, 200]);
    assert.equal(created.length, before + 1, 'one terminal, not two');
    assert.equal(created.at(-1).shellArgs[1], SID, 'and it runs the derived session id');
  }));

test('a start reserves its session id in the shared state folder, and a second start is refused by that marker', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const requestId = 'req-marker-0001';
    const marker = path.join(sessionsDir, core.sessionIdFor(requestId));
    assert.equal(fs.existsSync(marker), false);
    const before = created.length;
    assert.equal((await start(reg.port, requestId)).status, 200);
    const written = fs.readFileSync(marker, 'utf8');
    assert.equal(new Date(written).toISOString(), written, 'the marker holds the time');
    const again = await start(reg.port, requestId);
    assert.equal(again.status, 409);
    assert.equal(again.json.status, 'refused');
    assert.equal(again.json.reason, 'duplicate_session');
    assert.equal(created.length, before + 1, 'one terminal, not two');
  }));

test('a marker made by another window refuses the start before anything happens', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const requestId = 'req-otherwin-0001';
    // What another window's extension host leaves behind: it shares this folder.
    core.reserveSession(sessionsDir, core.sessionIdFor(requestId));
    const before = created.length;
    const res = await start(reg.port, requestId);
    assert.equal(res.status, 409);
    assert.equal(res.json.reason, 'duplicate_session');
    assert.equal(created.length, before, 'no terminal is created');
  }));

test('a marker made by another process between the check and the start makes the start refuse, with no terminal', { timeout: LIMIT_MS }, (t) => {
  const requestId = 'req-race-0001';
  const sessionId = core.sessionIdFor(requestId);
  // The competing window gets in right after this window's check found no marker and no record.
  t.mock.method(core, 'sessionRecordState', () => {
    core.reserveSession(sessionsDir, sessionId);
    return 'absent';
  });
  return withWindow(async (reg) => {
    const before = created.length;
    const res = await start(reg.port, requestId);
    assert.equal(res.status, 409);
    assert.equal(res.json.status, 'refused');
    assert.equal(res.json.reason, 'duplicate_session');
    assert.equal(created.length, before, 'no terminal is created');
  });
});

test('the marker survives a deactivate and a new activate, so a reload cannot start the same session again', { timeout: LIMIT_MS }, async () => {
  const requestId = 'req-reload-0001';
  let firstInstance;
  await withWindow(async (reg) => {
    firstInstance = reg.instance;
    assert.equal((await start(reg.port, requestId)).status, 200);
  });
  const before = created.length;
  await withWindow(async (reg) => {
    assert.notEqual(reg.instance, firstInstance, 'a new activation, as after a window reload');
    const res = await start(reg.port, requestId);
    assert.equal(res.status, 409);
    assert.equal(res.json.reason, 'duplicate_session');
  });
  assert.equal(created.length, before, 'the reloaded window starts nothing');
});

test('a start leaves its intent in the audit log before the terminal exists, then its result, and never the prompt or the token', { timeout: LIMIT_MS }, () => {
  const requestId = 'req-audit-0001';
  const sessionId = core.sessionIdFor(requestId);
  let loggedWhenCreated;
  onCreateTerminal = () => {
    loggedWhenCreated = auditRecords().map((record) => record.event);
  };
  return withWindow(async (reg) => {
    assert.equal(fs.existsSync(auditDir), false, 'the audit folder is made on demand');
    assert.equal((await start(reg.port, requestId)).status, 200);
    assert.deepEqual(loggedWhenCreated, ['start_intent'], 'the intent was on record when the terminal was created');
    const records = auditRecords();
    assert.deepEqual(records.map(withoutTime), [
      { instance: reg.instance, event: 'start_intent', request_id: requestId, session_id: sessionId, cwd: FOLDER, label: 'hands repo-a req-00', prompt_chars: Array.from(PROMPT).length },
      { instance: reg.instance, event: 'start_result', request_id: requestId, session_id: sessionId, status: 'started', terminal_pid: 777 },
    ]);
    for (const record of records) assert.equal(new Date(record.ts).toISOString(), record.ts, 'an ISO time');
    const text = fs.readFileSync(auditPath(reg.instance), 'utf8');
    assert.equal(text.split('\n').filter(Boolean).length, 2, 'one line per event');
    assert.ok(!text.includes('looks-like-a-flag'), 'the prompt text is in the log');
    assert.ok(!text.includes(TOKEN), 'the token is in the log');
  });
});

test('an audit log that cannot be written refuses the start with audit_failed, reserves nothing, and the same request starts once it can', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const requestId = 'req-auditfail-0001';
    fs.mkdirSync(auditPath(reg.instance), { recursive: true }); // a folder where this window's log file should be: appending fails
    const before = created.length;
    const refused = await start(reg.port, requestId);
    assert.equal(refused.status, 409);
    assert.equal(refused.json.status, 'refused');
    assert.equal(refused.json.reason, 'audit_failed');
    assert.equal(created.length, before, 'no terminal is created');
    assert.equal(fs.existsSync(path.join(sessionsDir, core.sessionIdFor(requestId))), false, 'nothing is reserved');
    fs.rmSync(auditPath(reg.instance), { recursive: true }); // the log can be written again
    assert.equal((await start(reg.port, requestId)).status, 200);
  }));

test('unauthorized calls and refusals are audited too, with the ids only when they parsed, and never the token', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const requestId = 'req-auditrefuse-0001';
    const wrongToken = 'Z'.repeat(43);
    const payload = { request_id: requestId, session_id: core.sessionIdFor(requestId), cwd: FOLDER, label: 'hands repo-a req-00', prompt: PROMPT };
    assert.equal((await post(reg.port, payload, wrongToken)).status, 401);
    const off = path.join(home, 'OFF');
    fs.writeFileSync(off, '');
    try {
      assert.equal((await start(reg.port, requestId)).status, 403);
    } finally {
      fs.rmSync(off, { force: true });
    }
    assert.deepEqual(auditRecords().map(withoutTime), [
      { instance: reg.instance, event: 'unauthorized' },
      { instance: reg.instance, event: 'refused', reason: 'kill_switch', request_id: requestId, session_id: core.sessionIdFor(requestId) },
    ]);
    assert.ok(!fs.readFileSync(auditPath(reg.instance), 'utf8').includes(wrongToken), 'a presented token is in the log');
  }));

test('a start that loses the race for its session id is audited as refused, after its intent', { timeout: LIMIT_MS }, (t) => {
  const requestId = 'req-auditrace-0001';
  const sessionId = core.sessionIdFor(requestId);
  t.mock.method(core, 'sessionRecordState', () => {
    core.reserveSession(sessionsDir, sessionId); // the other window gets in right after the check
    return 'absent';
  });
  return withWindow(async (reg) => {
    assert.equal((await start(reg.port, requestId)).status, 409);
    assert.deepEqual(auditRecords().map(withoutTime), [
      { instance: reg.instance, event: 'start_intent', request_id: requestId, session_id: sessionId, cwd: FOLDER, label: 'hands repo-a req-00', prompt_chars: Array.from(PROMPT).length },
      { instance: reg.instance, event: 'refused', reason: 'duplicate_session', request_id: requestId, session_id: sessionId },
    ]);
  });
});

test('a reservation that cannot be checked is unknown: refused as session_check_failed, nothing starts, and the same request starts once it can be checked', { timeout: LIMIT_MS }, (t) => {
  const requestId = 'req-markerfail-0001';
  const sessionId = core.sessionIdFor(requestId);
  const recordCheck = t.mock.method(core, 'sessionRecordState'); // wraps the real one and counts calls
  return withWindow(async (reg) => {
    // A regular file where the reservation folder should be: nothing under it can be looked up (ENOTDIR).
    fs.writeFileSync(sessionsDir, 'not a folder');
    const before = created.length;
    const refused = await start(reg.port, requestId);
    assert.equal(refused.status, 409);
    assert.equal(refused.json.status, 'refused');
    assert.equal(refused.json.reason, 'session_check_failed');
    assert.equal(created.length, before, 'no terminal is created');
    assert.equal(recordCheck.mock.callCount(), 0, "Claude's own records are not asked in its place");
    assert.equal(fs.readFileSync(sessionsDir, 'utf8'), 'not a folder', 'the reservation folder is as it was');
    assert.deepEqual(
      auditRecords().map(withoutTime),
      [{ instance: reg.instance, event: 'refused', reason: 'session_check_failed', request_id: requestId, session_id: sessionId }],
      'refused at the check: no intent was written, so startClaude was never reached',
    );
    // Once it can be checked again, the same request id starts: nothing was used up.
    fs.rmSync(sessionsDir);
    assert.equal((await start(reg.port, requestId)).status, 200);
    assert.equal(fs.existsSync(path.join(sessionsDir, sessionId)), true, 'and now it is reserved');
  });
});

test('a marker answer that is not one of the three is unknown too, and the record check is not asked', { timeout: LIMIT_MS }, (t) => {
  let answer;
  t.mock.method(core, 'sessionMarkerState', () => answer);
  const recordCheck = t.mock.method(core, 'sessionRecordState');
  return withWindow(async (reg) => {
    const before = created.length;
    for (answer of [undefined, null, true, false, '', 'maybe', 'Present', Promise.resolve('absent')]) {
      const res = await start(reg.port, 'req-markerodd-0001'); // refused at the check, so nothing is used up
      assert.equal(res.status, 409, String(answer));
      assert.equal(res.json.reason, 'session_check_failed', String(answer));
    }
    assert.equal(created.length, before);
    assert.equal(recordCheck.mock.callCount(), 0);
  });
});

test('a reservation folder that cannot be written is refused as reservation_failed, starts nothing, and the same request starts once it can', { skip: SKIP_AS_ROOT, timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const requestId = 'req-reserveperm-0001';
    const sessionId = core.sessionIdFor(requestId);
    fs.mkdirSync(sessionsDir, { recursive: true });
    fs.chmodSync(sessionsDir, 0o555); // searchable, so the check says absent, but nothing can be created in it
    const before = created.length;
    try {
      const refused = await start(reg.port, requestId);
      assert.equal(refused.status, 409);
      assert.deepEqual(refused.json, {
        status: 'refused',
        reason: 'reservation_failed',
        error: 'the bridge could not reserve that session id (EACCES)',
      });
      assert.equal(created.length, before, 'no terminal is created');
      assert.deepEqual(fs.readdirSync(sessionsDir), [], 'and nothing was reserved');
      assert.deepEqual(auditRecords().map(withoutTime), [
        { instance: reg.instance, event: 'start_intent', request_id: requestId, session_id: sessionId, cwd: FOLDER, label: 'hands repo-a req-00', prompt_chars: Array.from(PROMPT).length },
        { instance: reg.instance, event: 'refused', reason: 'reservation_failed', request_id: requestId, session_id: sessionId },
      ]);
    } finally {
      fs.chmodSync(sessionsDir, 0o755);
    }
    assert.equal((await start(reg.port, requestId)).status, 200, 'the same request id: nothing was used up');
    assert.equal(fs.existsSync(path.join(sessionsDir, sessionId)), true);
  }));

test('a reservation that throws is refused as reservation_failed whatever the cause, and the same request starts once it works', { timeout: LIMIT_MS }, (t) => {
  const requestId = 'req-reservemock-0001';
  const failing = t.mock.method(core, 'reserveSession', () => {
    throw Object.assign(new Error('EACCES: simulated'), { code: 'EACCES' });
  });
  return withWindow(async (reg) => {
    const before = created.length;
    const refused = await start(reg.port, requestId);
    assert.equal(refused.status, 409);
    assert.equal(refused.json.status, 'refused');
    assert.equal(refused.json.reason, 'reservation_failed');
    assert.equal(refused.json.error, 'the bridge could not reserve that session id (EACCES)');
    assert.equal(created.length, before, 'no terminal is created');
    assert.equal(failing.mock.callCount(), 1);
    failing.mock.restore();
    assert.equal((await start(reg.port, requestId)).status, 200);
  });
});

test('a full disk that stops the time being written into the marker does not stop the start, and the marker still holds', { timeout: LIMIT_MS }, (t) => {
  const requestId = 'req-fulldisk-0001';
  const sessionId = core.sessionIdFor(requestId);
  const realWriteSync = fs.writeSync;
  t.mock.method(fs, 'writeSync', (fd, data, ...rest) => {
    if (typeof data === 'string' && /^\d{4}-\d\d-\d\dT/.test(data)) throw Object.assign(new Error('ENOSPC: simulated'), { code: 'ENOSPC' });
    return realWriteSync(fd, data, ...rest);
  });
  return withWindow(async (reg) => {
    const before = created.length;
    assert.equal((await start(reg.port, requestId)).status, 200, 'the reservation holds, so the start goes on');
    assert.equal(created.length, before + 1);
    const marker = path.join(sessionsDir, sessionId);
    assert.equal(fs.statSync(marker).size, 0, 'an empty marker is still a marker');
    const again = await start(reg.port, requestId);
    assert.equal(again.status, 409);
    assert.equal(again.json.reason, 'duplicate_session');
  });
});

test('every audit record names this window, by the instance id it registered under', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const wrongToken = 'Z'.repeat(43);
    assert.equal((await start(reg.port, 'req-instance-0001')).status, 200);
    assert.equal((await post(reg.port, { request_id: 'req-instance-0002' }, wrongToken)).status, 401);
    assert.equal((await post(reg.port, { request_id: 'req-instance-0003' })).status, 400);
    const records = auditRecords();
    assert.deepEqual(records.map((record) => record.event), ['start_intent', 'start_result', 'unauthorized', 'refused']);
    assert.deepEqual(records.filter((record) => record.instance !== reg.instance), [], 'all four name this window');
    assert.match(reg.instance, /^[0-9a-f-]{36}$/);
  }));

test('a start keeps its state in the Remote Hands folder: the marker in bridge-sessions, its audit lines in a file of this window, and nothing in the WSL home', { timeout: LIMIT_MS }, () =>
  withWindow(async (reg) => {
    const requestId = 'req-where-0001';
    const sessionId = core.sessionIdFor(requestId);
    assert.equal((await start(reg.port, requestId)).status, 200);
    assert.deepEqual(fs.readdirSync(sessionsDir), [sessionId], 'the marker is in <home>/bridge-sessions');
    assert.deepEqual(auditFiles(), [`${reg.instance}.jsonl`], 'one audit file in <home>/bridge-audit, named for this window');
    assert.deepEqual(auditRecords(reg.instance).map((record) => record.event), ['start_intent', 'start_result']);
    assert.equal(fs.existsSync(wslStateDir), false, 'and nothing was written under the WSL home');
  }));

test('two windows write two audit files, one each, and never one shared file', { timeout: LIMIT_MS }, async () => {
  const instances = [];
  for (const requestId of ['req-twofiles-0001', 'req-twofiles-0002']) {
    await withWindow(async (reg) => {
      instances.push(reg.instance);
      assert.equal((await start(reg.port, requestId)).status, 200);
    });
  }
  assert.equal(new Set(instances).size, 2, 'two activations, two instances');
  assert.deepEqual(auditFiles(), instances.map((instance) => `${instance}.jsonl`).sort(), 'two files, named for the windows');
  for (const instance of instances) {
    const records = auditRecords(instance);
    assert.equal(records.length, 2, 'the intent and the result of its own start');
    assert.deepEqual(records.filter((record) => record.instance !== instance), [], "a file holds only its own window's records");
  }
  assert.equal(fs.existsSync(wslStateDir), false, 'and nothing was written under the WSL home');
});

// --- Claude folder trust: before a start the bridge makes sure Claude trusts the repo, by the key Claude keeps
// --- the trust under, in ~/.claude.json (a temporary one here). Inside the allowed roots it marks the repo
// --- trusted; outside them, or with Claude's config elsewhere, it refuses.

// What the bridge writes for a key with no entry: the entry Claude makes when it sets trust (Claude Code 2.1.289),
// with external CLAUDE.md imports approved too, as Claude writes them for "Yes" at its dialog.
const SEEDED_ENTRY = {
  allowedTools: [],
  mcpContextUris: [],
  mcpServers: {},
  enabledMcpjsonServers: [],
  disabledMcpjsonServers: [],
  hasTrustDialogAccepted: true,
  hasClaudeMdExternalIncludesApproved: true,
  hasClaudeMdExternalIncludesWarningShown: true,
};
const ALL_FLAGS = ['hasTrustDialogAccepted', 'hasClaudeMdExternalIncludesApproved', 'hasClaudeMdExternalIncludesWarningShown'];
const IMPORTS_FLAGS = ALL_FLAGS.slice(1);
const MAIN_REPO = '/home/user/src/main-repo-a'; // say, the main repository of a worktree the window has open
const UNTRUSTED = 'Claude has not been trusted in this folder: open Claude Code there once and accept the trust prompt, then retry with the same request id';

test('a repository Claude does not trust yet is marked trusted in ~/.claude.json under its key, after a trust_intent, and then starts', { timeout: LIMIT_MS }, (t) => {
  keyFolderAs(t, MAIN_REPO);
  const requestId = 'req-trust-0001';
  const sessionId = core.sessionIdFor(requestId);
  return withWindow(async (reg) => {
    assert.equal((await start(reg.port, requestId)).status, 200);
    const config = JSON.parse(fs.readFileSync(claudeConfigFile, 'utf8'));
    const before = JSON.parse(CLAUDE_CONFIG);
    assert.deepEqual(config, { ...before, projects: { ...before.projects, [MAIN_REPO]: SEEDED_ENTRY } });
    assert.deepEqual(claudeFiles(), ['.claude.json'], 'no lock and no temporary file left');
    assert.deepEqual(auditRecords(reg.instance).map(withoutTime), [
      { instance: reg.instance, event: 'trust_intent', request_id: requestId, session_id: sessionId, cwd: FOLDER, key: MAIN_REPO, flags: ALL_FLAGS },
      { instance: reg.instance, event: 'trust_seeded', request_id: requestId, session_id: sessionId, key: MAIN_REPO, result: 'seeded', flags: ALL_FLAGS },
      { instance: reg.instance, event: 'start_intent', request_id: requestId, session_id: sessionId, cwd: FOLDER, label: 'hands repo-a req-00', prompt_chars: Array.from(PROMPT).length },
      { instance: reg.instance, event: 'start_result', request_id: requestId, session_id: sessionId, status: 'started', terminal_pid: 777 },
    ]);
    // Trusted from now on: the next start writes nothing to the config and no trust records.
    const written = fs.readFileSync(claudeConfigFile);
    assert.equal((await start(reg.port, 'req-trust-0002')).status, 200);
    assert.deepEqual(fs.readFileSync(claudeConfigFile), written);
    assert.deepEqual(auditRecords(reg.instance).slice(4).map((record) => record.event), ['start_intent', 'start_result']);
  });
});

test('a repository outside the allowed roots is refused as untrusted_folder, ~/.claude.json is not touched, and the same request starts once Claude trusts it', { timeout: LIMIT_MS }, (t) => {
  const requestId = 'req-outside-0001';
  return withWindow(async (reg) => {
    fs.utimesSync(claudeConfigFile, new Date('2026-01-02T03:04:05Z'), new Date('2026-01-02T03:04:05Z'));
    const mtime = fs.statSync(claudeConfigFile).mtimeMs;
    // Outside the roots, the root itself, a key with the root as a mere prefix, and no key at all.
    for (const key of ['/home/user/elsewhere/repo', '/mnt/c/Users/user/repo', '/home/user/src', '/home/user/srcx/repo', null]) {
      const mock = keyFolderAs(t, key);
      const before = created.length;
      const res = await start(reg.port, requestId);
      mock.mock.restore();
      assert.equal(res.status, 409, String(key));
      assert.deepEqual(res.json, { status: 'refused', reason: 'untrusted_folder', error: UNTRUSTED }, String(key));
      assert.equal(created.length, before, String(key));
      assert.equal(fs.readFileSync(claudeConfigFile, 'utf8'), CLAUDE_CONFIG, String(key));
      assert.equal(fs.statSync(claudeConfigFile).mtimeMs, mtime, String(key));
      assert.deepEqual(claudeFiles(), ['.claude.json'], String(key));
    }
    assert.deepEqual(
      [...new Set(auditRecords(reg.instance).map((record) => `${record.event} ${record.reason}`))],
      ['refused untrusted_folder'],
      'no trust records, only the refusals',
    );
    assert.equal((await start(reg.port, requestId)).status, 200, 'nothing was used up');
  });
});

test('with CLAUDE_CONFIG_DIR set, Claude\'s config is $CLAUDE_CONFIG_DIR/.claude.json, so trust is read and seeded there', { timeout: LIMIT_MS }, (t) => {
  const configDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-claude-config-'));
  t.after(() => fs.rmSync(configDir, { recursive: true, force: true }));
  const otherConfig = path.join(configDir, '.claude.json');
  fs.writeFileSync(otherConfig, JSON.stringify({ projects: {} }, null, 2), { mode: 0o600 });
  process.env.CLAUDE_CONFIG_DIR = configDir;
  const requestId = 'req-configdir-0001';
  return withWindow(async (reg) => {
    // FOLDER is trusted in ~/.claude.json, but that is not Claude's config now: it is seeded in the other one.
    assert.equal((await start(reg.port, requestId)).status, 200);
    assert.deepEqual(JSON.parse(fs.readFileSync(otherConfig, 'utf8')).projects[FOLDER], SEEDED_ENTRY);
    assert.equal(fs.readFileSync(claudeConfigFile, 'utf8'), CLAUDE_CONFIG, '~/.claude.json was not touched');
    assert.deepEqual(fs.readdirSync(configDir), ['.claude.json'], 'no lock and no temporary file left there');
    assert.deepEqual(auditRecords(reg.instance).slice(0, 2).map((record) => [record.event, record.key]), [['trust_intent', FOLDER], ['trust_seeded', FOLDER]]);
  });
});

test('an empty CLAUDE_CONFIG_DIR counts as unset, as it does for Claude, and a config dir without a config refuses as trust_failed', { timeout: LIMIT_MS }, (t) => {
  const configDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-claude-config-'));
  t.after(() => fs.rmSync(configDir, { recursive: true, force: true }));
  return withWindow(async (reg) => {
    process.env.CLAUDE_CONFIG_DIR = '';
    assert.equal((await start(reg.port, 'req-configdir-0002')).status, 200, 'FOLDER is trusted in ~/.claude.json');
    process.env.CLAUDE_CONFIG_DIR = configDir; // no .claude.json in it
    const before = created.length;
    const res = await start(reg.port, 'req-configdir-0003');
    assert.equal(res.status, 409);
    assert.deepEqual(res.json, { status: 'refused', reason: 'trust_failed', error: 'the bridge could not record Claude\'s one-time answers for this folder (ENOENT)' });
    assert.equal(created.length, before);
    assert.deepEqual(fs.readdirSync(configDir), [], 'nothing was created');
  });
});

test('a key Claude already trusts starts even outside the allowed roots, and nothing is written', { timeout: LIMIT_MS }, (t) => {
  const outside = '/home/user/elsewhere/main-repo'; // say, the main repository of a worktree under ~/.worktrees
  const config = JSON.parse(CLAUDE_CONFIG);
  config.projects[outside] = { allowedTools: [], hasTrustDialogAccepted: true }; // the user accepted the prompt at the desk
  const text = JSON.stringify(config, null, 2);
  fs.writeFileSync(claudeConfigFile, text);
  fs.utimesSync(claudeConfigFile, new Date('2026-01-02T03:04:05Z'), new Date('2026-01-02T03:04:05Z'));
  const mtime = fs.statSync(claudeConfigFile).mtimeMs;
  keyFolderAs(t, outside);
  return withWindow(async (reg) => {
    assert.equal((await start(reg.port, 'req-trustout-0001')).status, 200);
    assert.equal(fs.readFileSync(claudeConfigFile, 'utf8'), text);
    assert.equal(fs.statSync(claudeConfigFile).mtimeMs, mtime);
    assert.deepEqual(claudeFiles(), ['.claude.json']);
    assert.deepEqual(auditRecords(reg.instance).map((record) => record.event), ['start_intent', 'start_result'], 'no trust records');
  });
});

test('a trusted repository whose CLAUDE.md imports are undecided gets the imports approved, the audit says so, and it starts', { timeout: LIMIT_MS }, (t) => {
  const config = JSON.parse(CLAUDE_CONFIG);
  const entry = { allowedTools: ['Bash(ls)'], hasTrustDialogAccepted: true }; // as Claude leaves it before the imports dialog
  config.projects[MAIN_REPO] = entry;
  fs.writeFileSync(claudeConfigFile, JSON.stringify(config, null, 2));
  keyFolderAs(t, MAIN_REPO);
  const requestId = 'req-imports-0001';
  const sessionId = core.sessionIdFor(requestId);
  return withWindow(async (reg) => {
    assert.equal((await start(reg.port, requestId)).status, 200);
    assert.deepEqual(JSON.parse(fs.readFileSync(claudeConfigFile, 'utf8')).projects[MAIN_REPO], {
      ...entry,
      hasClaudeMdExternalIncludesApproved: true,
      hasClaudeMdExternalIncludesWarningShown: true,
    });
    assert.deepEqual(auditRecords(reg.instance).slice(0, 2).map(withoutTime), [
      { instance: reg.instance, event: 'trust_intent', request_id: requestId, session_id: sessionId, cwd: FOLDER, key: MAIN_REPO, flags: IMPORTS_FLAGS },
      { instance: reg.instance, event: 'trust_seeded', request_id: requestId, session_id: sessionId, key: MAIN_REPO, result: 'seeded', flags: IMPORTS_FLAGS },
    ]);
    assert.deepEqual(claudeFiles(), ['.claude.json']);
  });
});

test('imports the user declined at Claude\'s dialog are their choice: nothing is written, and the start goes on', { timeout: LIMIT_MS }, (t) => {
  const config = JSON.parse(CLAUDE_CONFIG);
  config.projects[MAIN_REPO] = { hasTrustDialogAccepted: true, hasClaudeMdExternalIncludesApproved: false, hasClaudeMdExternalIncludesWarningShown: true };
  const text = JSON.stringify(config, null, 2);
  fs.writeFileSync(claudeConfigFile, text);
  keyFolderAs(t, MAIN_REPO);
  return withWindow(async (reg) => {
    assert.equal((await start(reg.port, 'req-declined-0001')).status, 200);
    assert.equal(fs.readFileSync(claudeConfigFile, 'utf8'), text);
    assert.deepEqual(auditRecords(reg.instance).map((record) => record.event), ['start_intent', 'start_result'], 'no trust records');
  });
});

test('outside the allowed roots, a trusted key starts with its imports left alone, and an untrusted one is refused', { timeout: LIMIT_MS }, (t) => {
  const outside = '/home/user/elsewhere/repo-x';
  const config = JSON.parse(CLAUDE_CONFIG);
  config.projects[outside] = { hasTrustDialogAccepted: true }; // imports undecided: the bridge cannot know whether Claude will ask
  const text = JSON.stringify(config, null, 2);
  fs.writeFileSync(claudeConfigFile, text);
  const mock = keyFolderAs(t, outside);
  return withWindow(async (reg) => {
    assert.equal((await start(reg.port, 'req-outimports-0001')).status, 200);
    assert.equal(fs.readFileSync(claudeConfigFile, 'utf8'), text, 'nothing seeded outside the roots');
    mock.mock.restore();
    keyFolderAs(t, '/home/user/elsewhere/repo-y'); // no entry at all
    const res = await start(reg.port, 'req-outimports-0002');
    assert.equal(res.status, 409);
    assert.equal(res.json.reason, 'untrusted_folder');
    assert.equal(fs.readFileSync(claudeConfigFile, 'utf8'), text);
  });
});

test('Claude\'s legacy config, .config.json in ~/.claude or in CLAUDE_CONFIG_DIR, is used when it exists, as Claude prefers it', { timeout: LIMIT_MS }, (t) => {
  keyFolderAs(t, MAIN_REPO);
  const configDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-claude-config-'));
  t.after(() => fs.rmSync(configDir, { recursive: true, force: true }));
  const empty = JSON.stringify({ projects: {} }, null, 2);
  fs.mkdirSync(path.dirname(legacyConfigFile), { recursive: true });
  fs.writeFileSync(legacyConfigFile, empty);
  return withWindow(async (reg) => {
    assert.equal((await start(reg.port, 'req-legacy-0001')).status, 200);
    assert.deepEqual(JSON.parse(fs.readFileSync(legacyConfigFile, 'utf8')).projects[MAIN_REPO], SEEDED_ENTRY);
    assert.equal(fs.readFileSync(claudeConfigFile, 'utf8'), CLAUDE_CONFIG, '~/.claude.json was not touched');
    // With CLAUDE_CONFIG_DIR set, the legacy file is looked for there, and ~/.claude's no longer counts.
    process.env.CLAUDE_CONFIG_DIR = configDir;
    fs.writeFileSync(path.join(configDir, '.claude.json'), empty);
    fs.writeFileSync(path.join(configDir, '.config.json'), empty);
    assert.equal((await start(reg.port, 'req-legacy-0002')).status, 200);
    assert.deepEqual(JSON.parse(fs.readFileSync(path.join(configDir, '.config.json'), 'utf8')).projects[MAIN_REPO], SEEDED_ENTRY);
    assert.equal(fs.readFileSync(path.join(configDir, '.claude.json'), 'utf8'), empty, 'the newer name is not used while the legacy file exists');
    assert.deepEqual(fs.readdirSync(configDir).sort(), ['.claude.json', '.config.json']);
  });
});

test('hands off written after the first kill switch check still stops the start before the reservation: no marker, no terminal', { timeout: LIMIT_MS }, (t) => {
  const requestId = 'req-lateoff-0001';
  const sessionId = core.sessionIdFor(requestId);
  const off = path.join(home, 'OFF');
  const realMarkerState = core.sessionMarkerState;
  // `hands off` lands while the bridge checks the session id, after it has checked the switch once.
  const mock = t.mock.method(core, 'sessionMarkerState', (...args) => {
    fs.writeFileSync(off, '');
    return realMarkerState(...args);
  });
  return withWindow(async (reg) => {
    try {
      const before = created.length;
      const res = await start(reg.port, requestId);
      assert.equal(res.status, 403);
      assert.equal(res.json.reason, 'kill_switch');
      assert.equal(created.length, before, 'no terminal');
      assert.equal(fs.existsSync(path.join(sessionsDir, sessionId)), false, 'no reservation marker');
      assert.deepEqual(auditRecords(reg.instance).map(withoutTime), [
        { instance: reg.instance, event: 'start_intent', request_id: requestId, session_id: sessionId, cwd: FOLDER, label: 'hands repo-a req-00', prompt_chars: Array.from(PROMPT).length },
        { instance: reg.instance, event: 'refused', reason: 'kill_switch', request_id: requestId, session_id: sessionId },
      ]);
    } finally {
      mock.mock.restore();
      fs.rmSync(off, { force: true });
    }
    assert.equal((await start(reg.port, requestId)).status, 200, 'nothing was used up: the same request id starts once Remote Hands is on');
  });
});

test('an empty CLAUDE_CONFIG_DIR never makes the bridge read a .config.json relative to its working folder', { timeout: LIMIT_MS }, (t) => {
  // Claude itself would look for its legacy config as ".config.json" relative to its working directory then;
  // the bridge looks in ~/.claude instead, so a file in a working folder is never taken for Claude's config.
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-workdir-'));
  const planted = path.join(workDir, '.config.json');
  const plantedText = JSON.stringify({ projects: {} }, null, 2);
  fs.writeFileSync(planted, plantedText);
  const cwd = process.cwd();
  process.chdir(workDir);
  t.after(() => {
    process.chdir(cwd);
    fs.rmSync(workDir, { recursive: true, force: true });
  });
  process.env.CLAUDE_CONFIG_DIR = '';
  return withWindow(async (reg) => {
    assert.equal((await start(reg.port, 'req-relconfig-0001')).status, 200, 'FOLDER is trusted in ~/.claude.json');
    assert.equal(fs.readFileSync(planted, 'utf8'), plantedText, 'the planted file was neither used nor written');
    assert.deepEqual(fs.readdirSync(workDir), ['.config.json']);
    assert.deepEqual(auditRecords(reg.instance).map((record) => record.event), ['start_intent', 'start_result'], 'no trust records');
  });
});

test('a lock on ~/.claude.json held by another Claude refuses the start as trust_failed (ELOCKED), leaves that lock alone, and starts nothing', { timeout: LIMIT_MS }, (t) => {
  keyFolderAs(t, MAIN_REPO);
  const requestId = 'req-locked-0001';
  const sessionId = core.sessionIdFor(requestId);
  return withWindow(async (reg) => {
    fs.mkdirSync(claudeConfigLock);
    const before = created.length;
    const res = await start(reg.port, requestId);
    assert.equal(res.status, 409);
    assert.deepEqual(res.json, { status: 'refused', reason: 'trust_failed', error: 'the bridge could not record Claude\'s one-time answers for this folder (ELOCKED)' });
    assert.equal(created.length, before);
    assert.equal(fs.statSync(claudeConfigLock).isDirectory(), true, 'the lock is still there');
    assert.equal(fs.readFileSync(claudeConfigFile, 'utf8'), CLAUDE_CONFIG);
    assert.equal(fs.existsSync(path.join(sessionsDir, sessionId)), false, 'nothing is reserved');
    assert.deepEqual(auditRecords(reg.instance).map(withoutTime), [
      { instance: reg.instance, event: 'trust_intent', request_id: requestId, session_id: sessionId, cwd: FOLDER, key: MAIN_REPO, flags: ALL_FLAGS },
      { instance: reg.instance, event: 'refused', reason: 'trust_failed', request_id: requestId, session_id: sessionId },
    ]);
    fs.rmdirSync(claudeConfigLock); // the other Claude finished its save
    assert.equal((await start(reg.port, requestId)).status, 200, 'the same request id starts');
    assert.equal(JSON.parse(fs.readFileSync(claudeConfigFile, 'utf8')).projects[MAIN_REPO].hasTrustDialogAccepted, true);
  });
});

test('a folder that is not there has no key Claude could trust, so it is refused as untrusted_folder', { timeout: LIMIT_MS }, () => {
  const missing = '/home/user/.worktrees/remote-hands-test-not-there-0001'; // not one of the made-up folders, so the real lookup runs
  assert.equal(fs.existsSync(missing), false);
  fakeVscode.workspace.workspaceFolders = [...FOLDERS, { uri: { scheme: 'vscode-remote', path: missing } }];
  return withWindow(async (reg) => {
    const res = await start(reg.port, 'req-notthere-0001', missing);
    assert.equal(res.status, 409);
    assert.equal(res.json.reason, 'untrusted_folder');
  });
});

// After every test, pass or fail: close the window it left running. A red test must not leave a
// listening server (which would hang the run) or a registration behind for the next test.
test.afterEach(() => {
  extension.deactivate();
  // Reservations and the audit files are durable by design, so each test starts without any (the home
  // folder itself, with its token, stays).
  fs.rmSync(sessionsDir, { recursive: true, force: true });
  fs.rmSync(auditDir, { recursive: true, force: true });
  fs.rmSync(wslStateDir, { recursive: true, force: true });
  process.env.REMOTE_HANDS_HOME = home;
  fakeVscode.env.remoteName = 'wsl';
  fakeVscode.workspace.workspaceFolders = FOLDERS;
  fakeVscode.window.state = { focused: false };
  silentPid = false;
  onCreateTerminal = undefined;
  delete process.env.CLAUDE_CONFIG_DIR;
  fs.rmSync(path.join(home, 'OFF'), { force: true });
  fs.rmSync(legacyConfigFile, { force: true });
  fs.rmSync(claudeConfigLock, { recursive: true, force: true });
  fs.writeFileSync(claudeConfigFile, CLAUDE_CONFIG, { mode: 0o600 });
});

test.after(() => {
  fs.rmSync(home, { recursive: true, force: true });
  fs.rmSync(userHome, { recursive: true, force: true });
});
