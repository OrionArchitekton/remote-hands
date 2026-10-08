'use strict';
// Pure logic for the Remote Hands window bridge. No vscode import, so node --test covers it.
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const MAX_PROMPT_CHARS = 16000;
const MAX_REPO_CHARS = 300;
const MIN_TOKEN_LENGTH = 32; // characters, as the service counts: auth.py MIN_TOKEN_LENGTH
// Where a session may start, and where the bridge may mark a repository trusted for Claude. Read, in order,
// from REMOTE_HANDS_ALLOWED_ROOTS (absolute paths separated by ':'), from "allowed_roots" in config.json in
// the Remote Hands home, or else these folders under this WSL user's home: the same three, in the same order
// and with the same rules, as the service's start_request.py allowed_roots (tests/test_bridge_contract.py
// compares the two).
const ALLOWED_ROOTS_ENV = 'REMOTE_HANDS_ALLOWED_ROOTS';
const ALLOWED_ROOTS_KEY = 'allowed_roots';
const DEFAULT_ROOT_DIRS = Object.freeze(['src', '.worktrees']);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}$/;
const REQUEST_ID_RE = /^[a-z0-9_-]{8,64}$/; // lower case: same rule as the journal
// The same constant as SESSION_NAMESPACE in the service's start_request.py.
const SESSION_NAMESPACE = '7d1f6b0e-2c4a-4f53-9a77-3e5b8c0d9a21';
const SESSION_NAMESPACE_BYTES = Buffer.from(SESSION_NAMESPACE.replace(/-/g, ''), 'hex');

// Cursor runs a terminal's arguments and working directory through its variable resolver before the
// process starts, so ${env:NAME}, ${selectedText}, and the like would be replaced by Cursor and
// Claude would receive something other than what was sent (rule C3). Same rule as the service's.
const VARIABLE_SYNTAX = '${';
const hasVariableSyntax = (text) => text.includes(VARIABLE_SYNTAX);
const variableError = (field) => `${field} must not contain ${VARIABLE_SYNTAX} (Cursor would substitute a variable there)`;

// Invisible characters (format characters such as zero-width spaces, bidi controls, and tag
// characters; lone surrogates; line and paragraph separators) and a CR without LF: the same
// rule as the service's start_request.py (rule C3).
const HIDDEN_RE = /[\p{Cf}\p{Cs}\p{Zl}\p{Zp}]/u;
const BARE_CR_RE = /\r(?!\n)/;

function isVariationSelector(code) {
  return (code >= 0xfe00 && code <= 0xfe0f) || (code >= 0xe0100 && code <= 0xe01ef);
}

function hasControlChars(text) {
  let previousWasSelector = false;
  for (const ch of text) {
    const code = ch.codePointAt(0);
    const allowed = code === 9 || code === 10 || code === 13;
    if (!allowed && (code < 32 || (code >= 0x7f && code <= 0x9f))) return true;
    // Supplementary variation selectors, or a selector right after another, can carry hidden
    // bytes after an emoji (rule C3).
    const selector = isVariationSelector(code);
    if ((code >= 0xe0100 && code <= 0xe01ef) || (selector && previousWasSelector)) return true;
    previousWasSelector = selector;
  }
  return HIDDEN_RE.test(text) || BARE_CR_RE.test(text);
}

// The session id the service gives a request id (start_request.py session_id_for): uuid5 (RFC 4122)
// of the request id in the fixed namespace, computed as Python's uuid.uuid5 does. The bridge derives
// it itself and refuses any other, so a caller holding the token cannot start two sessions for one
// request id (C6). Pinned vectors in both languages keep the two derivations identical.
function sessionIdFor(requestId) {
  const digest = crypto.createHash('sha1').update(SESSION_NAMESPACE_BYTES).update(requestId, 'utf8').digest();
  const bytes = digest.subarray(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x50; // version 5
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // the RFC 4122 variant
  const hex = bytes.toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

// Allowed roots as absolute, normalized folders, each ending in '/' (start_request.py parse_roots). The
// filesystem root, relative paths, '.', '..', and '//' are refused, so a root can only narrow where a session
// starts. Throws on any entry that breaks the rule.
function parseRoots(entries) {
  if (!Array.isArray(entries) || entries.length === 0) {
    throw new Error('allowed roots must be a non-empty list of absolute paths');
  }
  return Object.freeze(
    entries.map((entry) => {
      const folder = typeof entry === 'string' ? entry.replace(/\/+$/, '') : '';
      const segments = folder.split('/').slice(1);
      if (
        !folder.startsWith('/') ||
        folder.includes('//') ||
        segments.some((seg) => seg === '' || seg === '.' || seg === '..') ||
        hasControlChars(folder) ||
        /[\t\n\r]/.test(folder)
      ) {
        throw new Error(`allowed root ${JSON.stringify(entry)} must be an absolute, normalized path`);
      }
      return `${folder}/`;
    }),
  );
}

// The allowed roots (see ALLOWED_ROOTS_ENV above). home is the Remote Hands home whose config.json is read;
// without one only the environment and the default count. Throws when the setting is malformed or the file
// cannot be read.
function allowedRoots(home = null) {
  const raw = process.env[ALLOWED_ROOTS_ENV] || '';
  if (raw) return parseRoots(raw.split(':'));
  if (home) {
    const file = path.join(home, 'config.json');
    let text;
    try {
      text = fs.readFileSync(file, 'utf8');
    } catch (err) {
      if (err.code !== 'ENOENT') throw err;
    }
    if (text !== undefined) {
      const data = JSON.parse(text);
      if (!isObject(data) || !own(data, ALLOWED_ROOTS_KEY)) {
        throw new Error(`${file} must be a JSON object with ${ALLOWED_ROOTS_KEY}`);
      }
      return parseRoots(data[ALLOWED_ROOTS_KEY]);
    }
  }
  return parseRoots(DEFAULT_ROOT_DIRS.map((name) => `${os.homedir()}/${name}`));
}

// allowedRoots, with a setting that cannot be read counting as no root at all, so every folder is refused.
function rootsOrNone(home = null) {
  try {
    return allowedRoots(home);
  } catch {
    return [];
  }
}

// The service's repo rule (start_request.py check_repo), applied to the working directory as well
// as the folder check, so even a caller holding the token cannot start Claude in the home folder or
// on a Windows drive. Lexical only: a symlink under an allowed root passes, as it does in the service.
function checkRepo(cwd, roots) {
  if (typeof cwd !== 'string') return false;
  const chars = Array.from(cwd).length; // characters, as in the service, not UTF-16 units
  if (chars < 1 || chars > MAX_REPO_CHARS) return false;
  if (hasControlChars(cwd) || /[\t\n\r]/.test(cwd)) return false;
  if (hasVariableSyntax(cwd)) return false;
  // path.posix.normalize keeps a trailing slash that Python's normpath (the service's test) strips,
  // so it is refused explicitly to stay identical.
  if (cwd.endsWith('/') || path.posix.normalize(cwd) !== cwd) return false;
  return insideAllowedRoots(cwd, roots);
}

// Strictly inside one of the allowed roots, by text: a root itself is not inside it.
function insideAllowedRoots(folder, roots = rootsOrNone()) {
  return (
    typeof folder === 'string' &&
    Array.isArray(roots) &&
    roots.some((root) => folder.startsWith(root) && folder.length > root.length)
  );
}

// Same rules as the service (start_request.py); checked again here because the bridge
// must not trust its caller with the arguments of a process it starts. roots: the allowed roots.
function validateStart(body, folders, roots = rootsOrNone()) {
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    return { error: 'body must be a JSON object' };
  }
  const { request_id: requestId, session_id: sessionId, cwd, label, prompt } = body;
  if (typeof requestId !== 'string' || !REQUEST_ID_RE.test(requestId)) {
    return { error: 'request_id is malformed' };
  }
  if (typeof sessionId !== 'string' || !UUID_RE.test(sessionId)) {
    return { error: 'session_id must be a lower-case UUID' };
  }
  if (sessionId !== sessionIdFor(requestId)) {
    return { error: 'session_id does not match request_id' };
  }
  if (!checkRepo(cwd, roots)) {
    return {
      error:
        typeof cwd === 'string' && hasVariableSyntax(cwd)
          ? variableError('cwd')
          : 'cwd must be a normalized path inside an allowed root',
    };
  }
  if (!folders.includes(cwd)) {
    return { error: "cwd must be one of this window's folders" };
  }
  if (typeof label !== 'string' || !LABEL_RE.test(label)) {
    return { error: 'label is malformed' };
  }
  // A leading / is a slash command, ! a shell escape, # a memory note; a leading - is refused too
  // because some of the Claude CLI reads raw arguments and ignores '--' (rule C3).
  if (typeof prompt === 'string' && /^[-/!#]/.test(prompt.trimStart())) {
    return {
      error:
        'prompt must not start with /, !, #, or - (Claude would treat it as a command, a shell escape, a memory note, or an option)',
    };
  }
  if (
    typeof prompt !== 'string' ||
    prompt.trim() === '' ||
    Array.from(prompt).length > MAX_PROMPT_CHARS ||
    hasControlChars(prompt)
  ) {
    return { error: 'prompt is empty, too long, or has control characters' };
  }
  if (hasVariableSyntax(prompt)) return { error: variableError('prompt') };
  // '--' ends option parsing but not subcommand dispatch, so a prompt that is one word naming a
  // Claude command (update, mcp, doctor, ...) would run that command. Two words never can.
  if (!/\s/.test(prompt.trim())) {
    return {
      error:
        'prompt must be at least two words (Claude treats a single word that names one of its commands as that command)',
    };
  }
  return { value: { requestId, sessionId, cwd, label, prompt } };
}

// The ids of a request body, for the audit log: only values of the expected shape, so a caller cannot put
// text of its own (or a huge string) into the log through them.
function auditIds(body) {
  const ids = {};
  if (body !== null && typeof body === 'object' && !Array.isArray(body)) {
    if (typeof body.request_id === 'string' && REQUEST_ID_RE.test(body.request_id)) ids.request_id = body.request_id;
    if (typeof body.session_id === 'string' && UUID_RE.test(body.session_id)) ids.session_id = body.session_id;
  }
  return ids;
}

function claudeArgs({ sessionId, label, prompt }) {
  // The prompt always follows '--', which ends option parsing. That alone is not enough, so
  // validateStart also refuses a leading '-' and a one-word prompt (rule C3).
  return ['--session-id', sessionId, '--remote-control', label, '--name', label, '--', prompt];
}

// An expected token shorter than the service's minimum is a broken setup, never a match.
function tokenMatches(presented, expected) {
  if (
    typeof presented !== 'string' ||
    typeof expected !== 'string' ||
    Array.from(expected).length < MIN_TOKEN_LENGTH
  ) {
    return false;
  }
  const a = Buffer.from(presented, 'utf8');
  const b = Buffer.from(expected, 'utf8');
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

// 'exists' when a record for this session id is found, 'absent' when none is, and 'unknown'
// when the folders cannot be read (or the id is malformed): the server refuses 'unknown'
// with its own reason, so the service can tell a failed check from a duplicate.
function sessionRecordState(projectsDir, sessionId) {
  if (typeof sessionId !== 'string' || !UUID_RE.test(sessionId)) return 'unknown';
  let names;
  try {
    names = fs.readdirSync(projectsDir);
  } catch (err) {
    return err.code === 'ENOENT' ? 'absent' : 'unknown';
  }
  let unknown = false;
  for (const name of names) {
    try {
      // statSync follows symlinks, so a symlinked project folder is searched too.
      fs.statSync(path.join(projectsDir, name, `${sessionId}.jsonl`));
      return 'exists';
    } catch (err) {
      if (err.code !== 'ENOENT' && err.code !== 'ENOTDIR') unknown = true;
    }
  }
  return unknown ? 'unknown' : 'absent';
}

// The bridge's own reservation of a session id (C6): one marker file per id, in a folder that every
// window shares (the Remote Hands state folder on Windows, so windows of every WSL distribution). The
// exclusive create is the atomic step: of any number of windows racing for one id, exactly one makes the
// file, and that create is the whole reservation. It answers true when this call made the file and false
// when it was already there; a malformed id, a folder that cannot be made, and any other trouble with the
// create throw (nothing was reserved, so the caller can refuse and the same id can be tried again).
//
// Everything after a successful create is best effort and swallowed, because the reservation already
// holds: the time is written, the file is forced to disk, and then its folder, so the marker can stand
// behind the journal after a power loss. A write or sync that fails (a full disk, say) leaves a marker
// that is still a reservation, and must not turn a start that has been reserved into a failure.
function reserveSession(dir, sessionId) {
  if (typeof sessionId !== 'string' || !UUID_RE.test(sessionId)) throw new Error('session id is malformed');
  fs.mkdirSync(dir, { recursive: true });
  let fd;
  try {
    fd = fs.openSync(path.join(dir, sessionId), 'wx');
  } catch (err) {
    if (err.code === 'EEXIST') return false;
    throw err;
  }
  try {
    fs.writeSync(fd, new Date().toISOString());
    fs.fsyncSync(fd);
  } catch {
    // best effort: the file exists, and that is the reservation
  }
  closeQuietly(fd);
  syncFolder(dir);
  return true;
}

function closeQuietly(fd) {
  try {
    fs.closeSync(fd);
  } catch {
    // best effort
  }
}

// Forces the directory entry of a new marker to disk. Best effort: not every platform can open a folder.
function syncFolder(dir) {
  let fd;
  try {
    fd = fs.openSync(dir, 'r');
    fs.fsyncSync(fd);
  } catch {
    // best effort
  }
  if (fd !== undefined) closeQuietly(fd);
}

// Whether a window has reserved this session id, three-valued like sessionRecordState: 'present' when the
// marker is there, 'absent' when it provably is not (not found), and 'unknown' for anything else: any
// other error (a folder that cannot be searched, say) and a malformed id. A marker that cannot be
// checked is neither taken nor free, so the caller refuses the request as unchecked, which the service
// can retry once the folder is readable, instead of reading a broken folder as a reservation. An entry
// of any kind is present (lstat, not stat), exactly as the exclusive create refuses to make a file
// where one is.
function sessionMarkerState(dir, sessionId) {
  if (typeof sessionId !== 'string' || !UUID_RE.test(sessionId)) return 'unknown';
  try {
    fs.lstatSync(path.join(dir, sessionId));
    return 'present';
  } catch (err) {
    return err.code === 'ENOENT' ? 'absent' : 'unknown';
  }
}

// The key Claude Code keeps a folder's trust under, worked out as Claude works it out (its git root and
// canonical root functions, read in Claude Code 2.1.289 and 2.1.292). The search starts where the folder
// really is, because Claude's working directory is a real path. A folder in no repository is its own key;
// otherwise the key is the canonical root of the repository's git root (see mainRoot). null only when the
// folder itself cannot be reached.
function canonicalRoot(folder) {
  if (typeof folder !== 'string' || !path.isAbsolute(folder)) return null;
  let start;
  try {
    start = fs.realpathSync(folder);
  } catch {
    return null;
  }
  const root = gitRoot(start);
  return root === null ? start : mainRoot(root);
}

// Claude's git root: the nearest folder, from start up to /, whose .git is a folder or a file (a .git that is a
// symlink counts when it leads to one). A .git that cannot be checked counts as no .git, and the search goes on.
function gitRoot(start) {
  for (let dir = start; ; dir = path.dirname(dir)) {
    if (folderOrFile(path.join(dir, '.git'))) return dir;
    if (path.dirname(dir) === dir) return null;
  }
}

function folderOrFile(target) {
  try {
    const found = fs.statSync(target);
    return found.isDirectory() || found.isFile();
  } catch {
    return false;
  }
}

// A file Claude refuses to read a worktree link from: a symlink, something other than a file or folder, or
// anything it cannot check (one that is not there is read, and fails, like any other missing file).
function linkOrOdd(target) {
  try {
    const found = fs.lstatSync(target);
    return found.isSymbolicLink() || !(found.isFile() || found.isDirectory());
  } catch (err) {
    return err.code !== 'ENOENT' && err.code !== 'ENOTDIR';
  }
}

// Claude's canonical root of a git root. A .git folder: the root itself. A .git file counts as a linked
// worktree only when the link is genuine: "gitdir: <gitdir>", the gitdir's commondir file (not a symlink)
// names a common dir whose worktrees folder holds the gitdir, and the gitdir's own gitdir file (not a symlink)
// points back to this root's .git, real paths compared. Then the key is the main repository, the common dir's
// parent; for a common dir not named .git (a bare repository), the common dir itself, unless it holds a .git of
// its own. Anything else (a moved worktree, a submodule, a separate git dir, a broken link, any error) leaves
// the root itself as the key. Like Claude's, the answer is not made a real path.
function mainRoot(root) {
  try {
    const text = fs.readFileSync(path.join(root, '.git'), 'utf8').trim(); // a .git folder throws here: the root
    if (!text.startsWith('gitdir:')) return root;
    const gitdir = path.resolve(root, text.slice(7).trim());
    if (linkOrOdd(path.join(gitdir, 'commondir'))) return root;
    const commonDir = path.resolve(gitdir, fs.readFileSync(path.join(gitdir, 'commondir'), 'utf8').trim());
    if (path.dirname(gitdir) !== path.join(commonDir, 'worktrees')) return root;
    if (linkOrOdd(path.join(gitdir, 'gitdir'))) return root;
    const backlink = fs.readFileSync(path.join(gitdir, 'gitdir'), 'utf8').trim();
    if (fs.realpathSync(path.resolve(gitdir, backlink)) !== path.join(fs.realpathSync(root), '.git')) return root;
    if (path.basename(commonDir) !== '.git') return folderOrFile(path.join(commonDir, '.git')) ? root : commonDir;
    return path.dirname(commonDir);
  } catch {
    return root;
  }
}

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

// The project entry for key in a parsed Claude config, when there is one.
function projectEntry(config, key) {
  if (!isObject(config) || !isObject(config.projects) || !own(config.projects, key)) return undefined;
  return config.projects[key];
}

// Two one-time questions Claude asks in a folder, which nobody can answer from the phone, keep their answers in
// the project's entry (Claude Code 2.1.293): the trust dialog (hasTrustDialogAccepted), and "Allow external
// CLAUDE.md file imports?", asked when a CLAUDE.md or .claude/rules imports files outside the working directory
// and neither imports flag is set ("Yes" sets both true; "No" sets approved false and warning shown true).
const TRUST_FLAG = 'hasTrustDialogAccepted';
const IMPORTS_APPROVED_FLAG = 'hasClaudeMdExternalIncludesApproved';
const IMPORTS_WARNED_FLAG = 'hasClaudeMdExternalIncludesWarningShown';
const SEEDED_FLAGS = Object.freeze([TRUST_FLAG, IMPORTS_APPROVED_FLAG, IMPORTS_WARNED_FLAG]);

// The flags the bridge would set in an entry so that Claude asks neither question there: the trust unless it is
// given, and the imports approval (both imports flags) only while the imports are undecided. Imports the user
// declined at the dialog (warning shown, not approved) are their choice and are never overridden. The trust counts only as an
// exact true (Claude's trust test); the imports count as decided when either flag is truthy, as Claude tests them.
function flagsToSeed(entry) {
  const value = (flag) => (isObject(entry) ? entry[flag] : undefined);
  const flags = value(TRUST_FLAG) === true ? [] : [TRUST_FLAG];
  if (!value(IMPORTS_APPROVED_FLAG) && !value(IMPORTS_WARNED_FLAG)) flags.push(IMPORTS_APPROVED_FLAG, IMPORTS_WARNED_FLAG);
  return flags;
}

// flagsToSeed for key in Claude's config text; text that does not parse, or a malformed key, has no entry, so
// every flag is missing. The trust counts as given only for projects[key].hasTrustDialogAccepted === true, which
// is Claude's first trust test.
function missingFlags(configText, key) {
  if (typeof key !== 'string' || key === '') return [...SEEDED_FLAGS];
  let config;
  try {
    config = JSON.parse(configText);
  } catch {
    config = undefined;
  }
  return flagsToSeed(projectEntry(config, key));
}

// The entry Claude makes when it sets trust on a folder it has no entry for (Claude Code 2.1.289).
const CLAUDE_DEFAULT_ENTRY = Object.freeze({
  allowedTools: [],
  mcpContextUris: [],
  mcpServers: {},
  enabledMcpjsonServers: [],
  disabledMcpjsonServers: [],
  hasTrustDialogAccepted: true,
  hasClaudeMdExternalIncludesApproved: false,
  hasClaudeMdExternalIncludesWarningShown: false,
});
const LOCK_WAIT_MS = 2000;
const LOCK_STEP_MS = 50;
const sleeper = new Int32Array(new SharedArrayBuffer(4));

function sleepSync(ms) {
  Atomics.wait(sleeper, 0, 0, ms);
}

const configError = (message) => Object.assign(new Error(message), { code: 'EBADCONFIG' });

// Claude saves its config under a proper-lockfile lock: the folder <config>.lock, made with mkdir (atomic),
// whose mtime its holder refreshes. Taking the same lock means Claude's next save, which re-reads the file
// under the lock and applies only its own change, keeps what was written here. A lock someone else holds is
// waited for in short synchronous steps, 2 s in all, and is never removed or touched here, however old it
// looks: then this fails with the code ELOCKED. Any other trouble making the folder throws at once.
function takeConfigLock(lock) {
  for (let waited = 0; ; waited += LOCK_STEP_MS) {
    try {
      fs.mkdirSync(lock);
      return;
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      if (waited >= LOCK_WAIT_MS) {
        throw Object.assign(new Error('another process holds the lock on the Claude config'), { code: 'ELOCKED' });
      }
    }
    sleepSync(LOCK_STEP_MS);
  }
}

function writeAll(fd, buffer) {
  let offset = 0;
  while (offset < buffer.length) offset += fs.writeSync(fd, buffer, offset, buffer.length - offset);
}

// Replaces file with text in one step: a temporary file in the same folder (mode 0600, as Claude writes its
// config), forced to disk, then renamed over the file, then the folder forced to disk (best effort). A
// temporary file is never left behind.
function writeAtomically(file, text) {
  const dir = path.dirname(file);
  const temp = path.join(dir, `${path.basename(file)}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`);
  let renamed = false;
  try {
    const fd = fs.openSync(temp, 'wx', 0o600);
    try {
      writeAll(fd, Buffer.from(text, 'utf8'));
      fs.fsyncSync(fd);
    } finally {
      closeQuietly(fd);
    }
    fs.renameSync(temp, file);
    renamed = true;
  } finally {
    if (!renamed) {
      try {
        fs.unlinkSync(temp);
      } catch {
        // it was never made
      }
    }
  }
  syncFolder(dir);
}

// Marks key trusted in Claude's config at configPath, with its external CLAUDE.md imports approved unless they
// were decided before, as Claude does when its trust prompt and its imports dialog are
// accepted. Under Claude's own lock, the file is read and parsed (a missing file, one that does not parse, and a
// symlink (code ESYMLINK) throw, and nothing is written or created); then the flags flagsToSeed names are set to
// true, keeping every other field, starting from Claude's default entry when there is none. Answers
// { result: 'already', flags: [] } without writing when nothing is missing, and { result: 'seeded', flags } once
// the new file is in place and every flag set reads back true (otherwise it throws, code EVERIFY).
function seedTrust(configPath, key) {
  if (typeof key !== 'string' || !path.isAbsolute(key)) throw new Error('the trust key must be an absolute path');
  const lock = `${configPath}.lock`;
  takeConfigLock(lock); // throws if this call did not make the lock, so the finally below never runs for one
  try {
    // The rename below would replace a symlinked config (a dotfiles link, say) with a file of its own.
    if (fs.lstatSync(configPath, { throwIfNoEntry: false })?.isSymbolicLink()) {
      throw Object.assign(new Error('the Claude config is a symlink'), { code: 'ESYMLINK' });
    }
    let text;
    try {
      text = fs.readFileSync(configPath, 'utf8');
    } catch (err) {
      // A config is never created here: one that is not there is refused, before anything is written.
      if (err.code === 'ENOENT') throw Object.assign(new Error('there is no Claude config to mark trusted in'), { code: 'ENOENT' });
      throw err;
    }
    let config;
    try {
      config = JSON.parse(text);
    } catch {
      throw configError('the Claude config is not valid JSON');
    }
    if (!isObject(config)) throw configError('the Claude config is not a JSON object');
    if (config.projects !== undefined && !isObject(config.projects)) throw configError('its projects are not an object');
    const entry = projectEntry(config, key);
    if (entry !== undefined && !isObject(entry)) throw configError('the project entry is not an object');
    const flags = flagsToSeed(entry);
    if (flags.length === 0) return { result: 'already', flags };
    const next = entry === undefined ? { ...CLAUDE_DEFAULT_ENTRY } : { ...entry };
    for (const flag of flags) next[flag] = true;
    config.projects = config.projects ?? {};
    config.projects[key] = next;
    writeAtomically(configPath, JSON.stringify(config, null, 2));
    let written;
    try {
      written = projectEntry(JSON.parse(fs.readFileSync(configPath, 'utf8')), key);
    } catch {
      written = undefined;
    }
    if (!isObject(written) || flags.some((flag) => written[flag] !== true)) {
      throw Object.assign(new Error('the Claude config did not read back with every flag set'), { code: 'EVERIFY' });
    }
    return { result: 'seeded', flags };
  } finally {
    // Released only here, where this call made it. Best effort: a lock left behind goes stale for Claude
    // after about 10 s, and an error here must not hide what the write did.
    try {
      fs.rmdirSync(lock);
    } catch {
      // see above
    }
  }
}

// Switched off unless the OFF file is provably absent: any error but "not found" (an unreadable
// folder, say) counts as off.
function killSwitchOn(home) {
  try {
    fs.statSync(path.join(home, 'OFF'));
    return true;
  } catch (err) {
    return err.code !== 'ENOENT';
  }
}

module.exports = {
  MAX_PROMPT_CHARS,
  MAX_REPO_CHARS,
  ALLOWED_ROOTS_ENV,
  ALLOWED_ROOTS_KEY,
  DEFAULT_ROOT_DIRS,
  parseRoots,
  allowedRoots,
  rootsOrNone,
  insideAllowedRoots,
  sessionIdFor,
  validateStart,
  auditIds,
  claudeArgs,
  tokenMatches,
  sessionRecordState,
  reserveSession,
  sessionMarkerState,
  canonicalRoot,
  missingFlags,
  SEEDED_FLAGS,
  seedTrust,
  killSwitchOn,
};
