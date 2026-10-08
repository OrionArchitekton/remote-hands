'use strict';
// Remote Hands window bridge: lets the local Remote Hands service start a Claude session in
// this window's terminal. Runs in the workspace (WSL) extension host. Fixed routes only.
const { execFileSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vscode = require('vscode');
const core = require('./core');
const { createBridgeServer } = require('./server');

const VERSION = require('../package.json').version;
// Same folder the service and the hands CLI use (%LOCALAPPDATA%\remote-hands on Windows); no VS Code setting can
// redirect it, so a repository's workspace settings never can either.
const APP_DIR_NAME = 'remote-hands';
const FALLBACK_CMD_EXE = '/mnt/c/Windows/System32/cmd.exe'; // when cmd.exe is not on the WSL PATH
const WINDOWS_QUERY_TIMEOUT_MS = 10000;
const HEARTBEAT_MS = 30000; // keep well under the service's 90 s window wait (actions.WINDOW_WAIT_SECONDS)
const timing = { startCheckMs: 2000, pidWaitMs: 5000 };

let server = null;
let registrationFile = null;
let heartbeat = null;

// Fixed executables and arguments, never a shell; stdin closed so a Windows child cannot hold the WSL interop pipe.
function runQuietly(file, args) {
  return execFileSync(file, args, {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
    timeout: WINDOWS_QUERY_TIMEOUT_MS,
    cwd: '/',
  }).trim();
}

// %LOCALAPPDATA% of the Windows user, as a WSL path, found as the hands CLI finds it (paths.py): LOCALAPPDATA when
// Windows passes it in already translated (WSLENV=LOCALAPPDATA/p), otherwise asked of Windows (cmd.exe, then
// wslpath). null when neither works.
function windowsLocalAppData() {
  const passed = process.env.LOCALAPPDATA || '';
  if (passed.startsWith('/')) return passed;
  for (const cmd of ['cmd.exe', FALLBACK_CMD_EXE]) {
    try {
      const value = runQuietly(cmd, ['/d', '/c', 'echo', '%LOCALAPPDATA%']);
      if (!value || value.includes('%')) return null;
      const translated = runQuietly('wslpath', ['-u', value]);
      return translated.startsWith('/') ? translated : null;
    } catch {
      // not found on the PATH, or failed: try the next, then give up
    }
  }
  return null;
}

function remoteHandsHome() {
  const override = process.env.REMOTE_HANDS_HOME;
  if (override && path.isAbsolute(override)) return override;
  const localAppData = windowsLocalAppData();
  return localAppData === null ? null : path.join(localAppData, APP_DIR_NAME);
}

function workspacePaths() {
  return (vscode.workspace.workspaceFolders || []).map((folder) => folder.uri.path);
}

function claudePath() {
  return path.join(os.homedir(), '.local', 'bin', 'claude');
}

// Claude's config, where it keeps whether it trusts a folder, found as Claude Code 2.1.292 finds it: its legacy
// .config.json in Claude's folder (CLAUDE_CONFIG_DIR, or ~/.claude) when that file exists; otherwise .claude.json
// in CLAUDE_CONFIG_DIR, or in the home folder when that is unset or empty.
function claudeConfigPath() {
  const configDir = process.env.CLAUDE_CONFIG_DIR;
  const legacy = path.join(configDir || path.join(os.homedir(), '.claude'), '.config.json');
  if (fs.existsSync(legacy)) return legacy;
  return path.join(configDir || os.homedir(), '.claude.json');
}

// Whether Claude can start in the folder without asking anything, by the key it keeps its answers under (the
// repository's canonical root, so a genuine worktree counts as its main repository). 'trusted': nothing to set
// (trusted, with external CLAUDE.md imports approved or declined), or, outside the allowed roots, trusted at
// all: a repository Claude already trusts is never refused, and its imports are left alone there. 'seedable':
// inside the allowed roots with something to set, named in flags (imports approval follows trust). 'outside': not trusted, and not inside the allowed roots (refused).
function trustState(cwd, roots) {
  const key = core.canonicalRoot(cwd);
  if (key === null) return { state: 'outside', key };
  let text = '';
  try {
    text = fs.readFileSync(claudeConfigPath(), 'utf8');
  } catch {
    // nothing set, then; seeding reads the file again under the lock, and refuses if it is missing
  }
  const flags = core.missingFlags(text, key);
  if (flags.length === 0) return { state: 'trusted', key };
  if (core.insideAllowedRoots(key, roots)) return { state: 'seedable', key, flags };
  return { state: flags.includes('hasTrustDialogAccepted') ? 'outside' : 'trusted', key };
}

// The server asks this before it reserves a session id, so a missing command line is a refusal the
// service can retry, not a failed start.
function claudeAvailable() {
  try {
    fs.accessSync(claudePath(), fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function registrationPath(home, instance) {
  return path.join(home, 'windows', `${instance}.json`);
}

function writeRegistration(home, record) {
  const file = registrationPath(home, record.instance);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${crypto.randomBytes(6).toString('hex')}.tmp`;
  let published = false;
  try {
    fs.writeFileSync(tmp, JSON.stringify(record));
    fs.renameSync(tmp, file);
    published = true;
  } finally {
    // A write or rename that failed must not leave its half-made file in the folder the service scans.
    if (!published) {
      try {
        fs.unlinkSync(tmp);
      } catch {
        // it was never made
      }
    }
  }
}

function errorText(err) {
  return err instanceof Error ? err.message : String(err);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function startClaudeTerminal({ sessionId, label, prompt, cwd }) {
  // shellPath is the Claude binary itself, so no shell ever parses the prompt (rule C3). The server
  // has already checked that it is there and executable.
  const terminal = vscode.window.createTerminal({
    name: `Claude: ${label}`,
    shellPath: claudePath(),
    shellArgs: core.claudeArgs({ sessionId, label, prompt }),
    cwd,
    // Never restored or re-run by Cursor: a restored terminal could start in a worktree the session
    // moved into, miss its own record, and send the prompt again (C6). A reload or restart therefore
    // ends the session; resume it with `claude --resume <session id>`.
    isTransient: true,
  });
  // Showing a terminal makes it the one the panel displays, which would take it from under the user's
  // hands while they type in another terminal of a focused window. A focused window therefore only
  // gets the new terminal in its list (rule C5); an unfocused one shows it, with preserveFocus so
  // the keyboard never moves. A focus state that cannot be read counts as focused.
  if (vscode.window.state?.focused === false) terminal.show(true);
  const pid = await Promise.race([terminal.processId, delay(timing.pidWaitMs).then(() => undefined)]);
  await delay(timing.startCheckMs);
  if (terminal.exitStatus !== undefined) {
    const err = new Error('claude exited right after starting');
    err.reason = 'exited';
    err.exitCode = terminal.exitStatus.code ?? null;
    throw err;
  }
  return { terminalPid: pid ?? null };
}

async function activate(context) {
  // The bridge serves WSL windows only: it starts the Linux Claude and reads Linux paths.
  if (vscode.env.remoteName !== 'wsl') {
    console.log('remote-hands-bridge: this is not a WSL window, so no bridge is started');
    return;
  }
  const home = remoteHandsHome();
  if (home === null) {
    console.error(
      'remote-hands-bridge: cannot find %LOCALAPPDATA% of the Windows user; set REMOTE_HANDS_HOME for the WSL server',
    );
    return;
  }
  const roots = () => core.allowedRoots(home);
  const projectsDir = path.join(os.homedir(), '.claude', 'projects');
  const instance = crypto.randomUUID();
  const startedAt = new Date().toISOString();
  // The reservations and the audit log live in the Remote Hands state folder, where the repo keeps all
  // runtime state, so they are shared by every window of every WSL distribution. Measured on a Windows PC
  // through /mnt/c (9p): an exclusive create raced by eight processes was atomic (3 rounds of
  // 200 markers, exactly one winner each) and fsync works on a file and on its folder.
  const sessions = path.join(home, 'bridge-sessions');
  // The audit log (C7) is one file per window, named by its instance id, and never a file that windows
  // share: on the same mount, eight processes appending to one file kept only 426 of 3,200 lines, while a
  // writer of its own file kept all of them. One JSON line per event, so a call that skipped the service
  // still leaves a trace; it throws when the line cannot be written, which the server turns into a refusal
  // before anything starts.
  const auditFile = path.join(home, 'bridge-audit', `${instance}.jsonl`);
  const audit = (record) => {
    fs.mkdirSync(path.dirname(auditFile), { recursive: true });
    fs.appendFileSync(auditFile, `${JSON.stringify({ ts: new Date().toISOString(), ...record })}\n`);
  };
  server = createBridgeServer({
    instance,
    version: VERSION,
    readToken: () => fs.readFileSync(path.join(home, 'token'), 'utf8').trim(),
    killSwitchOn: () => core.killSwitchOn(home),
    claudeAvailable,
    audit,
    // Made by the server, after the audit intent and before the start (see server.js).
    reserveSession: (sessionId) => core.reserveSession(sessions, sessionId),
    folders: workspacePaths,
    // Asked after the Claude check and before the session check; a seedable repository is marked trusted
    // (after a trust_intent record) under Claude's own lock on its config.
    trustState: (cwd) => trustState(cwd, core.rootsOrNone(home)),
    allowedRoots: roots,
    seedTrust: (key) => core.seedTrust(claudeConfigPath(), key),
    sessionState: (sessionId) => {
      // The reservation marker first, then Claude's own records, and only a clear "absent" goes on to the
      // records. A marker that cannot be checked is unknown, never taken and never free: the records must
      // not answer in its place, and the request is refused as unchecked so the service can retry it.
      const marker = core.sessionMarkerState(sessions, sessionId);
      if (marker === 'present') return 'exists';
      if (marker !== 'absent') return 'unknown';
      return core.sessionRecordState(projectsDir, sessionId);
    },
    // The session id is already reserved when this is called. Nothing is undone on a failure later: a marker
    // that outlives a start that did not finish is what keeps its id unused (C6).
    startClaude: startClaudeTerminal,
  });
  // Kept for the server's whole life: an error after listening has started is logged, never thrown.
  server.on('error', (err) => console.error(`remote-hands-bridge: server error: ${errorText(err)}`));
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      server.off('error', reject);
      resolve();
    });
  });
  const port = server.address().port;
  registrationFile = registrationPath(home, instance);
  let registered = false;
  // Never throws: a window that cannot register yet still serves, and the heartbeat tries again.
  const register = () => {
    try {
      writeRegistration(home, {
        instance,
        port,
        folders: workspacePaths(),
        pid: process.pid,
        version: VERSION,
        started_at: startedAt,
      });
      registered = true;
    } catch (err) {
      registered = false;
      console.error(`remote-hands-bridge: could not register this window, will retry: ${errorText(err)}`);
    }
  };
  // Started before the first registration, so a first failure is retried like any other. The beat
  // writes again after a failed write (a stale record may still be there) and puts the registration
  // back if the service removed it after a missed ping.
  heartbeat = setInterval(() => {
    if (!registered || !fs.existsSync(registrationFile)) register();
  }, HEARTBEAT_MS);
  heartbeat.unref();
  register();
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(register));
}

function deactivate() {
  if (heartbeat) clearInterval(heartbeat);
  if (registrationFile) {
    try {
      fs.unlinkSync(registrationFile);
    } catch {
      // already gone
    }
  }
  if (server) {
    server.closeAllConnections();
    server.close();
  }
}

module.exports = { activate, deactivate, timing };
