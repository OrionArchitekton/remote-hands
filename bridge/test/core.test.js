'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const core = require('../src/core');
// The allowed roots these tests run with (core.allowedRoots reads this first): the made-up folders they use.
process.env.REMOTE_HANDS_ALLOWED_ROOTS = '/home/user/src/:/home/user/.worktrees/';

const RID = 'req-0001-abcd';
const SID = core.sessionIdFor(RID); // the bridge refuses any session id but the one derived from the request id
const FOLDER = '/home/user/src/repo-a';
const good = () => ({
  request_id: RID,
  session_id: SID,
  cwd: FOLDER,
  label: 'hands repo-a req-00',
  prompt: 'fix the share image',
});
const checkPrompt = (prompt) => core.validateStart({ ...good(), prompt }, [FOLDER]);
// A request id sent with its own session id, so only the request id's rules can be what refuses it.
const withOwnSession = (change) => ({ ...change, session_id: core.sessionIdFor(change.request_id) });
const char = (code) => String.fromCharCode(code);
// The service's messages for the same rules (start_request.py check_prompt), word for word.
const COMMAND_START_ERROR =
  'prompt must not start with /, !, #, or - (Claude would treat it as a command, a shell escape, a memory note, or an option)';
const ONE_WORD_ERROR =
  'prompt must be at least two words (Claude treats a single word that names one of its commands as that command)';
const PROMPT_REFUSED = 'prompt is empty, too long, or has control characters';
const NOT_IN_ROOT_ERROR = 'cwd must be a normalized path inside an allowed root';
const NOT_A_FOLDER_ERROR = "cwd must be one of this window's folders";
// Cursor runs a terminal's arguments and working directory through its variable resolver, so
// ${env:NAME} or ${selectedText} would be replaced before Claude saw the text (rule C3).
const VARIABLE_PROMPT_ERROR = 'prompt must not contain ${ (Cursor would substitute a variable there)';
const VARIABLE_CWD_ERROR = 'cwd must not contain ${ (Cursor would substitute a variable there)';
const OTHER_SID = '00000000-0000-5000-8000-000000000000'; // a valid session id that no request id derives
const SESSION_MISMATCH_ERROR = 'session_id does not match request_id';
const BAD_SESSION_ERROR = 'session_id must be a lower-case UUID';
// uuid.uuid5(SESSION_NAMESPACE, request_id) in Python (src/remote_hands/start_request.py), pinned here
// and in tests/test_bridge_contract.py: a change to either derivation fails a test on its own side.
const SESSION_ID_VECTORS = [
  ['req-0001-abcd', 'fb0acc9b-305a-594d-a7e5-b4fb434c156a'],
  ['example-l2-0001', '31b86acf-0c30-59b2-b0fe-8c96ca4fe19c'],
  ['a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6', '5deeb099-35c0-5e55-a698-655507d4746d'],
  ['abcdef123456', 'b436c3a0-a8d6-52fa-84a1-cf8ce91db92a'], // also pinned in tests/test_start_request.py
];

test('sessionIdFor gives the session id the service derives from the same request id', () => {
  for (const [requestId, expected] of SESSION_ID_VECTORS) {
    assert.equal(core.sessionIdFor(requestId), expected, requestId);
  }
});

test('sessionIdFor is a lower-case version 5 UUID that differs with every request id', () => {
  const ids = ['req-0001-abcd', 'req-0001-abce', 'req-0002-abcd', 'a'.repeat(8), 'a'.repeat(64)].map((requestId) => {
    const id = core.sessionIdFor(requestId);
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/, requestId);
    assert.equal(core.sessionIdFor(requestId), id, 'the same request id always names the same session');
    return id;
  });
  assert.equal(new Set(ids).size, ids.length);
});

test('the session id must be the one derived from the request id', () => {
  const [requestId, derived] = SESSION_ID_VECTORS[0];
  const body = { ...good(), request_id: requestId };
  assert.equal(core.validateStart({ ...body, session_id: derived }, [FOLDER]).error, undefined);
  assert.equal(core.validateStart({ ...body, session_id: OTHER_SID }, [FOLDER]).error, SESSION_MISMATCH_ERROR);
  // The format checks come first and keep their own message.
  for (const session_id of [derived.toUpperCase(), 'x', '', undefined]) {
    assert.equal(core.validateStart({ ...body, session_id }, [FOLDER]).error, BAD_SESSION_ERROR, String(session_id));
  }
});

test('one request id cannot start two sessions: only its own derived session id is accepted', () => {
  const [requestId, derived] = SESSION_ID_VECTORS[0];
  const accepted = (session_id) =>
    Boolean(core.validateStart({ ...good(), request_id: requestId, session_id }, [FOLDER]).value);
  assert.equal(accepted(derived), true);
  // Another valid UUID, the id another request derives, and the arbitrary id the old fixtures used.
  for (const session_id of [OTHER_SID, SESSION_ID_VECTORS[1][1], 'abcdef01-2345-5678-9abc-def012345678']) {
    assert.equal(accepted(session_id), false, session_id);
  }
});

test('a well-formed start request passes', () => {
  assert.deepEqual(core.validateStart(good(), [FOLDER]).value, {
    requestId: 'req-0001-abcd',
    sessionId: SID,
    cwd: FOLDER,
    label: 'hands repo-a req-00',
    prompt: 'fix the share image',
  });
});

test('cwd must be one of the window folders, not a child or a parent', () => {
  for (const cwd of ['/home/user/src', '/home/user/src/repo-a/sub', '/etc']) {
    assert.ok(core.validateStart({ ...good(), cwd }, [FOLDER]).error, cwd);
  }
});

test('cwd must be exactly a window folder: a parent or child inside an allowed root is refused', () => {
  const nested = '/home/user/src/org/repo-a';
  const worktree = '/home/user/.worktrees/x';
  for (const cwd of ['/home/user/src/org', nested + '/sub', nested + 'x']) {
    assert.equal(core.validateStart({ ...good(), cwd }, [nested]).error, NOT_A_FOLDER_ERROR, cwd);
  }
  assert.ok(core.validateStart({ ...good(), cwd: worktree }, [nested, worktree]).value);
});

test('cwd must be a normalized path inside an allowed root, even when it is one of the window folders', () => {
  const refused = [
    '/home/user', '/home/user/src', '/home/user/src/', '/home/user/.worktrees', '/home/user/.worktrees/',
    '/mnt/c/Users/user', '/tmp/x', '/etc', '/home/user/srcx/y', 'src/repo-a', '',
    '/home/user/src/a/../b', '/home/user/src/a/', '/home/user/src//a', '/home/user/src/./a', '//home/user/src/a',
    '/home/user/src/a' + char(9), '/home/user/src/a' + char(10) + 'b', '/home/user/src/a' + char(13),
    '/home/user/src/a' + char(7), '/home/user/src/a' + char(0x7f), '/home/user/src/a' + char(0x200b),
    '/home/user/src/a' + char(0xdc80), '/home/user/src/a' + String.fromCodePoint(0x1f600, 0xe0100),
  ];
  for (const cwd of refused) {
    assert.equal(core.validateStart({ ...good(), cwd }, [cwd]).error, NOT_IN_ROOT_ERROR, JSON.stringify(cwd));
  }
  for (const cwd of [undefined, null, 42, [FOLDER], {}]) {
    assert.equal(core.validateStart({ ...good(), cwd }, [FOLDER]).error, NOT_IN_ROOT_ERROR, String(cwd));
  }
  const accepted = [
    '/home/user/.worktrees/x', '/home/user/src/a b', '/home/user/src/caf' + char(0xe9), '/home/user/src/.hidden',
  ];
  for (const cwd of accepted) {
    assert.ok(core.validateStart({ ...good(), cwd }, [cwd]).value, JSON.stringify(cwd));
  }
});

test('cwd length counts characters, not UTF-16 units', () => {
  const root = '/home/user/src/';
  for (const filler of ['a', String.fromCodePoint(0x1f600)]) {
    const cwd = root + filler.repeat(core.MAX_REPO_CHARS - root.length);
    assert.equal(Array.from(cwd).length, core.MAX_REPO_CHARS);
    assert.ok(core.validateStart({ ...good(), cwd }, [cwd]).value);
    const over = cwd + 'a';
    assert.equal(core.validateStart({ ...good(), cwd: over }, [over]).error, NOT_IN_ROOT_ERROR);
  }
});

test('malformed fields are refused', () => {
  const cases = [
    { session_id: SID.toUpperCase() },
    { session_id: 'x' },
    withOwnSession({ request_id: '../../x' }),
    withOwnSession({ request_id: 'UPPER-case-01' }),
    { label: '-flag' },
    { label: ' lead' },
    { label: '' },
    { label: 'semi;colon' },
    { label: 'new' + String.fromCharCode(10) + 'line' },
    { label: 'a'.repeat(49) },
    { prompt: '' },
    { prompt: '   ' },
    // Two words each, so only the rule under test can refuse them.
    { prompt: 'a'.repeat(core.MAX_PROMPT_CHARS - 1) + ' b' }, // one character over the limit
    { prompt: 'ring the bell' + String.fromCharCode(7) },
    { prompt: 'clear screen' + String.fromCharCode(27) + '[2J' },
    { prompt: 42 },
  ];
  for (const change of cases) {
    assert.ok(core.validateStart({ ...good(), ...change }, [FOLDER]).error, JSON.stringify(change));
  }
  for (const body of [null, [], 'text']) {
    assert.ok(core.validateStart(body, [FOLDER]).error);
  }
});

test('prompt length counts characters, not UTF-16 units', () => {
  const emoji = String.fromCodePoint(0x1f600);
  const exactly = emoji.repeat(core.MAX_PROMPT_CHARS - 2) + ' ' + emoji; // two words
  assert.equal(Array.from(exactly).length, core.MAX_PROMPT_CHARS);
  assert.ok(checkPrompt(exactly).value);
  assert.equal(checkPrompt(exactly + emoji).error, PROMPT_REFUSED); // one character over
});

test('a blank prompt is refused as empty', () => {
  for (const prompt of ['', '   ', char(10) + char(9) + ' ']) {
    assert.equal(checkPrompt(prompt).error, PROMPT_REFUSED, JSON.stringify(prompt));
  }
});

test('invisible characters, lone surrogates, separators, and a bare CR are refused', () => {
  const hidden = [0x200b, 0x202e, 0x2066, 0xfeff, 0xe0041, 0x2028, 0x2029].map((cp) => String.fromCodePoint(cp));
  hidden.push(String.fromCharCode(0xdc80), 'a' + String.fromCharCode(13) + 'b');
  hidden.push(String.fromCodePoint(0x1f600, 0xe0100), String.fromCodePoint(0x2764, 0xfe0f, 0xfe0f));
  for (const bad of hidden) {
    // Between two words, so no other rule (such as the one-word rule) can be the one that refuses it.
    assert.equal(checkPrompt('fix ' + bad + ' now').error, PROMPT_REFUSED, JSON.stringify(bad));
  }
});

test('one ordinary variation selector after an emoji is allowed', () => {
  assert.ok(core.validateStart({ ...good(), prompt: 'love ' + String.fromCodePoint(0x2764, 0xfe0f) }, [FOLDER]).value);
});

test('control characters other than tab, newline, and CR are refused, and the edges are exact', () => {
  const refused = [0x00, 0x07, 0x08, 0x0b, 0x0c, 0x1b, 0x1f, 0x7f, 0x80, 0x85, 0x9b, 0x9f];
  for (const code of refused) {
    const name = 'U+' + code.toString(16).padStart(4, '0');
    assert.equal(checkPrompt('fix ' + char(code) + ' now').error, PROMPT_REFUSED, name + ' between words');
    assert.equal(checkPrompt('fix it' + char(code)).error, PROMPT_REFUSED, name + ' at the end');
  }
  // Just outside the refused ranges: a space, a tilde, a no-break space, and an inverted bang.
  for (const code of [0x20, 0x7e, 0xa0, 0xa1]) {
    assert.ok(checkPrompt('fix ' + char(code) + ' now').value, 'U+' + code.toString(16).padStart(4, '0'));
  }
});

test('a label may be up to 48 characters', () => {
  for (const label of ['a', 'hands repo-a req-00', 'a'.repeat(48)]) {
    assert.ok(core.validateStart({ ...good(), label }, [FOLDER]).value, label);
  }
});

test('request ids are 8 to 64 lower-case characters', () => {
  for (const [length, ok] of [[0, false], [1, false], [7, false], [8, true], [9, true], [63, true], [64, true], [65, false]]) {
    const result = core.validateStart({ ...good(), ...withOwnSession({ request_id: 'a'.repeat(length) }) }, [FOLDER]);
    assert.equal(Boolean(result.value), ok, 'length ' + length);
  }
});

test('a prompt may not start with a command or option character', () => {
  const refused = [
    '/logout now', '  /clear all', '!rm x', '# remember this', '-x hello', '  --help me', char(9) + '-v now',
    '/logout', // one word as well: the command-start message wins over the one-word message
  ];
  for (const prompt of refused) {
    assert.equal(checkPrompt(prompt).error, COMMAND_START_ERROR, JSON.stringify(prompt));
  }
  for (const prompt of ['fix the /api route', 'use -x as a flag', 'wow! fix it', 'fix it - now', 'a-b c']) {
    assert.ok(checkPrompt(prompt).value, prompt);
  }
});

test('a prompt may not contain ${, where Cursor would substitute a variable', () => {
  const refused = [
    'fix ${env:HOME} now', 'use ${selectedText} here', '${env:HOME} is set', 'ends with ${', 'fix it' + char(10) + '${file} now',
    '${x}', // one word as well: the variable message wins over the one-word message
  ];
  for (const prompt of refused) {
    assert.equal(checkPrompt(prompt).error, VARIABLE_PROMPT_ERROR, JSON.stringify(prompt));
  }
  assert.equal(checkPrompt('/${x} y').error, COMMAND_START_ERROR); // the command-start message comes first
});

test('a dollar sign or a brace alone is fine in a prompt', () => {
  for (const prompt of ['costs $5 {roughly}', 'use $HOME later', 'a {$x} b', 'price $ {x} here', 'use $(date) here', '$$ {} now']) {
    assert.ok(checkPrompt(prompt).value, prompt);
  }
});

test('cwd may not contain ${, even when it is one of the window folders', () => {
  for (const cwd of ['/home/user/src/a${x}', '/home/user/src/${env:HOME}/x', '/home/user/.worktrees/${workspaceFolder}']) {
    assert.equal(core.validateStart({ ...good(), cwd }, [cwd]).error, VARIABLE_CWD_ERROR, cwd);
  }
  for (const cwd of ['/home/user/src/a$b', '/home/user/src/{a}', '/home/user/src/a{$b}']) {
    assert.ok(core.validateStart({ ...good(), cwd }, [cwd]).value, cwd);
  }
});

test('a one-word prompt is refused: Claude would run a command it names', () => {
  const refused = [
    'update', 'mcp', 'remote-control', 'rc', 'install', '  doctor  ', 'doctor' + char(10), char(9) + 'mcp' + char(9),
    char(0xa0) + 'update' + char(0xa0), char(0x3000) + 'doctor',
  ];
  for (const prompt of refused) {
    assert.equal(checkPrompt(prompt).error, ONE_WORD_ERROR, JSON.stringify(prompt));
  }
});

test('two or more words are a prompt, whatever separates them', () => {
  const accepted = [
    'fix the bug', 'fix it', 'a b', 'mcp list', 'update the docs', 'fix' + char(9) + 'it', 'fix' + char(10) + 'it',
    'fix' + char(0xa0) + 'it', 'fix it -x', 'fix it -- now',
  ];
  for (const prompt of accepted) {
    assert.ok(checkPrompt(prompt).value, JSON.stringify(prompt));
  }
});

test('tabs and newlines are allowed in a prompt', () => {
  assert.ok(core.validateStart({ ...good(), prompt: 'line one\n\tline two\r\n' }, [FOLDER]).value);
});

test('the prompt always comes after --, even when it looks like a flag', () => {
  const args = core.claudeArgs({ sessionId: SID, label: 'hands x', prompt: '--dangerously-skip-permissions' });
  assert.deepEqual(args, [
    '--session-id', SID,
    '--remote-control', 'hands x',
    '--name', 'hands x',
    '--', '--dangerously-skip-permissions',
  ]);
});

test('token comparison', () => {
  assert.equal(core.tokenMatches('t'.repeat(40), 't'.repeat(40)), true);
  assert.equal(core.tokenMatches('t'.repeat(39), 't'.repeat(40)), false);
  assert.equal(core.tokenMatches('', ''), false);
  assert.equal(core.tokenMatches(undefined, 't'.repeat(40)), false);
  // Same length, different content: the case a token check exists for.
  assert.equal(core.tokenMatches('u'.repeat(40), 't'.repeat(40)), false);
  assert.equal(core.tokenMatches('t'.repeat(39) + 'u', 't'.repeat(40)), false);
});

test('an expected token shorter than 32 characters never matches, even when presented', () => {
  assert.equal(core.tokenMatches('t'.repeat(32), 't'.repeat(32)), true);
  assert.equal(core.tokenMatches('t'.repeat(31), 't'.repeat(31)), false);
  assert.equal(core.tokenMatches('t', 't'), false);
  // 16 characters of 2 UTF-16 units each: 32 units, but fewer than 32 characters, as the service counts.
  const emoji = String.fromCodePoint(0x1f600).repeat(16);
  assert.equal(core.tokenMatches(emoji, emoji), false);
});

// File modes do not restrict root, so the tests that rely on chmod are skipped there.
const SKIP_AS_ROOT = typeof process.getuid === 'function' && process.getuid() === 0 ? 'running as root' : false;

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-projects-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test('session record lookup searches every project folder', (t) => {
  const dir = tempDir(t);
  fs.mkdirSync(path.join(dir, '-home-user-src-other'));
  assert.equal(core.sessionRecordState(dir, SID), 'absent');
  fs.writeFileSync(path.join(dir, '-home-user-src-other', `${SID}.jsonl`), '');
  assert.equal(core.sessionRecordState(dir, SID), 'exists');
  assert.equal(core.sessionRecordState(path.join(dir, 'missing'), SID), 'absent');
});

test('session record lookup follows a symlinked project folder', (t) => {
  const dir = tempDir(t);
  const elsewhere = tempDir(t);
  fs.writeFileSync(path.join(elsewhere, `${SID}.jsonl`), '');
  fs.symlinkSync(elsewhere, path.join(dir, '-home-user-src-linked'));
  assert.equal(core.sessionRecordState(dir, SID), 'exists');
});

test('a file among the project folders is skipped, not treated as a failure', (t) => {
  const dir = tempDir(t);
  fs.writeFileSync(path.join(dir, 'notes.txt'), '');
  fs.mkdirSync(path.join(dir, '-home-user-src-other'));
  assert.equal(core.sessionRecordState(dir, SID), 'absent');
});

test('a symlink loop among the project folders cannot be checked', (t) => {
  // Searching a looping link fails with ELOOP, which is neither "no such record" nor "not a folder".
  const dir = tempDir(t);
  fs.symlinkSync('b', path.join(dir, 'a'));
  fs.symlinkSync('a', path.join(dir, 'b'));
  assert.equal(core.sessionRecordState(dir, SID), 'unknown');
});

test('a malformed session id cannot be checked, so the start is refused', (t) => {
  const dir = tempDir(t);
  for (const sid of ['', 'x', '../../etc/passwd', SID.toUpperCase(), SID + 'x', undefined, 42]) {
    assert.equal(core.sessionRecordState(dir, sid), 'unknown', String(sid));
  }
});

test('a projects path that is not a folder cannot be checked', (t) => {
  const file = path.join(tempDir(t), 'projects');
  fs.writeFileSync(file, '');
  assert.equal(core.sessionRecordState(file, SID), 'unknown');
});

test('an unreadable projects folder cannot be checked', { skip: SKIP_AS_ROOT }, (t) => {
  const dir = tempDir(t);
  fs.chmodSync(dir, 0o000);
  try {
    assert.equal(core.sessionRecordState(dir, SID), 'unknown');
  } finally {
    fs.chmodSync(dir, 0o700);
  }
});

test('an unreadable project folder cannot be checked', { skip: SKIP_AS_ROOT }, (t) => {
  const dir = tempDir(t);
  const project = path.join(dir, '-home-user-src-locked');
  fs.mkdirSync(project);
  fs.chmodSync(project, 0o000);
  try {
    assert.equal(core.sessionRecordState(dir, SID), 'unknown');
  } finally {
    fs.chmodSync(project, 0o700);
  }
});

test('a record in a readable folder wins over an unreadable sibling searched first', { skip: SKIP_AS_ROOT }, (t) => {
  const dir = tempDir(t);
  fs.mkdirSync(path.join(dir, '-home-user-src-one'));
  fs.mkdirSync(path.join(dir, '-home-user-src-two'));
  // The search order is the file system's, so it is read back rather than assumed: the folder
  // searched first is the locked one, and the record sits in the one searched second.
  const [locked, readable] = fs.readdirSync(dir);
  fs.writeFileSync(path.join(dir, readable, `${SID}.jsonl`), '');
  fs.chmodSync(path.join(dir, locked), 0o000);
  try {
    assert.equal(core.sessionRecordState(dir, SID), 'exists');
  } finally {
    fs.chmodSync(path.join(dir, locked), 0o700);
  }
});

test('no OFF file means the kill switch is not on', (t) => {
  assert.equal(core.killSwitchOn(tempDir(t)), false);
});

test('an OFF file turns the kill switch on, whatever it holds', (t) => {
  const home = tempDir(t);
  fs.writeFileSync(path.join(home, 'OFF'), '');
  assert.equal(core.killSwitchOn(home), true);
});

test('a home folder that cannot be read counts as switched off', { skip: SKIP_AS_ROOT }, (t) => {
  // existsSync answers false here, which would fail open: the OFF file is inside, out of reach.
  const home = tempDir(t);
  fs.writeFileSync(path.join(home, 'OFF'), '');
  fs.chmodSync(home, 0o000);
  try {
    assert.equal(core.killSwitchOn(home), true);
  } finally {
    fs.chmodSync(home, 0o700);
  }
});

test('a home path that is not a folder counts as switched off', (t) => {
  // The same fail-closed branch as an unreadable folder, without chmod, so it also runs as root.
  const file = path.join(tempDir(t), 'home');
  fs.writeFileSync(file, '');
  assert.equal(core.killSwitchOn(file), true);
});

test('auditIds keeps only ids of the expected shape, so a caller cannot put text in the log through them', () => {
  assert.deepEqual(core.auditIds({ request_id: RID, session_id: SID }), { request_id: RID, session_id: SID });
  assert.deepEqual(core.auditIds({ request_id: RID }), { request_id: RID });
  assert.deepEqual(core.auditIds({ request_id: 'UPPER-case-01', session_id: 'x' }), {});
  assert.deepEqual(core.auditIds({ request_id: 'a'.repeat(100000), session_id: SID }), { session_id: SID });
  assert.deepEqual(core.auditIds({ request_id: `${RID}\nforged line`, session_id: `${SID}x` }), {});
  assert.deepEqual(core.auditIds({ request_id: 42, session_id: ['a'] }), {});
  for (const body of [null, undefined, [], 'text', 42]) {
    assert.deepEqual(core.auditIds(body), {}, String(body));
  }
});

// --- The bridge's own reservation of a session id: one marker file per id, in a folder every window
// --- shares (C6). The exclusive create is what makes it atomic.

test('reserveSession makes the folder and a marker holding the time, and says it was first', (t) => {
  const dir = path.join(tempDir(t), 'state', 'bridge-sessions'); // neither level exists yet
  assert.equal(core.reserveSession(dir, SID), true);
  const written = fs.readFileSync(path.join(dir, SID), 'utf8');
  assert.equal(new Date(written).toISOString(), written, 'an ISO time');
});

test('a session id is reserved once: the next tries say no and change nothing', (t) => {
  const dir = tempDir(t);
  const marker = path.join(dir, SID);
  assert.equal(core.reserveSession(dir, SID), true);
  const first = fs.readFileSync(marker, 'utf8');
  assert.equal(core.reserveSession(dir, SID), false);
  assert.equal(core.reserveSession(dir, SID), false);
  assert.equal(fs.readFileSync(marker, 'utf8'), first);
  assert.equal(core.reserveSession(dir, OTHER_SID), true, 'another session id is its own reservation');
});

test('reservations by separate processes at the same moment: exactly one wins', { timeout: 20000 }, async (t) => {
  // Each window's extension host is its own process, so the claim has to hold across processes.
  const dir = path.join(tempDir(t), 'bridge-sessions');
  const startAt = Date.now() + 400; // every process waits for this instant, then claims
  const script = `
    const core = require(${JSON.stringify(require.resolve('../src/core'))});
    while (Date.now() < ${startAt}) {}
    process.stdout.write(String(core.reserveSession(${JSON.stringify(dir)}, ${JSON.stringify(SID)})));
  `;
  const claim = () =>
    new Promise((resolve, reject) => {
      const child = spawn(process.execPath, ['-e', script], { stdio: ['ignore', 'pipe', 'inherit'] });
      let out = '';
      child.stdout.on('data', (chunk) => (out += chunk));
      child.on('error', reject);
      child.on('exit', (code) => (code === 0 ? resolve(out) : reject(new Error(`a claiming process exited ${code}`))));
    });
  const results = await Promise.all(Array.from({ length: 8 }, claim));
  assert.deepEqual(results.sort(), ['false', 'false', 'false', 'false', 'false', 'false', 'false', 'true']);
});

test('reserveSession throws on any trouble but "already reserved"', (t) => {
  const file = path.join(tempDir(t), 'a-file');
  fs.writeFileSync(file, '');
  assert.throws(() => core.reserveSession(path.join(file, 'bridge-sessions'), SID), { code: 'ENOTDIR' });
  // The folder itself is a file: mkdir says EEXIST, which is trouble with the folder, not "already reserved".
  assert.throws(() => core.reserveSession(file, SID), { code: 'EEXIST' });
});

test('reserveSession throws where the folder cannot be written, and leaves nothing', { skip: SKIP_AS_ROOT }, (t) => {
  const dir = tempDir(t);
  fs.chmodSync(dir, 0o555);
  try {
    assert.throws(() => core.reserveSession(dir, SID), { code: 'EACCES' });
  } finally {
    fs.chmodSync(dir, 0o700);
  }
  assert.deepEqual(fs.readdirSync(dir), []);
});

// The exclusive create is the reservation. Once it has succeeded the marker is there for good, so
// whatever is done to it afterwards (the time, forcing it to disk) is best effort and never undoes it.
// These mock fs, which core calls as fs.x at call time, and pass everything else through. Files are
// followed by path, not by descriptor number: the system hands a closed number straight back.
function spyOnFiles(t, { failOn = {}, createFails } = {}) {
  const real = { open: fs.openSync, close: fs.closeSync, fsync: fs.fsyncSync, write: fs.writeSync };
  const live = new Map(); // descriptor -> path, for what is open right now
  const state = { opened: [], closed: [], synced: [], written: [], leaked: () => [...live.values()] };
  const fail = (name, code) => Object.assign(new Error(`${code}: simulated ${name}`), { code });
  t.mock.method(fs, 'openSync', (target, ...rest) => {
    if (createFails && String(target).endsWith(SID)) throw fail('create', createFails);
    if (failOn.open && fs.statSync(target, { throwIfNoEntry: false })?.isDirectory()) throw fail('open', failOn.open);
    const fd = real.open(target, ...rest);
    live.set(fd, String(target));
    state.opened.push(String(target));
    return fd;
  });
  t.mock.method(fs, 'writeSync', (fd, ...rest) => {
    if (failOn.write) throw fail('write', failOn.write);
    state.written.push(live.get(fd));
    return real.write(fd, ...rest);
  });
  t.mock.method(fs, 'fsyncSync', (fd) => {
    state.synced.push(live.get(fd));
    if (failOn.fsync) throw fail('fsync', failOn.fsync);
    return real.fsync(fd);
  });
  t.mock.method(fs, 'closeSync', (fd) => {
    state.closed.push(live.get(fd));
    live.delete(fd);
    real.close(fd);
    if (failOn.close) throw fail('close', failOn.close);
  });
  return state;
}

test('a time that cannot be written does not undo the reservation: it still holds, and the file is closed', (t) => {
  const dir = tempDir(t);
  const spy = spyOnFiles(t, { failOn: { write: 'ENOSPC' } });
  assert.equal(core.reserveSession(dir, SID), true);
  assert.ok(spy.closed.includes(path.join(dir, SID)), 'the marker file was closed');
  assert.deepEqual(spy.leaked(), [], 'nothing is left open');
  assert.equal(core.sessionMarkerState(dir, SID), 'present', 'the marker is there, empty');
  assert.equal(core.reserveSession(dir, SID), false, 'and it holds against the next start');
});

test('a marker that cannot be forced to disk is still a reservation, and every file opened is closed', (t) => {
  const dir = tempDir(t);
  const spy = spyOnFiles(t, { failOn: { fsync: 'EIO' } });
  assert.equal(core.reserveSession(dir, SID), true);
  assert.equal(core.sessionMarkerState(dir, SID), 'present');
  assert.deepEqual([...spy.opened].sort(), [dir, path.join(dir, SID)].sort(), 'the marker and its folder');
  assert.deepEqual(spy.leaked(), [], 'both were closed, though the fsync of each failed');
  assert.ok(new Date(fs.readFileSync(path.join(dir, SID), 'utf8')).getTime() > 0, 'the time was written before the fsync failed');
});

test('a close that fails is swallowed too: the reservation holds', (t) => {
  const dir = tempDir(t);
  const spy = spyOnFiles(t, { failOn: { close: 'EIO' } });
  assert.equal(core.reserveSession(dir, SID), true);
  assert.equal(core.sessionMarkerState(dir, SID), 'present');
  assert.deepEqual([...spy.closed].sort(), [dir, path.join(dir, SID)].sort(), 'each close was still attempted');
});

test('a folder that cannot be opened for its fsync does not matter either', (t) => {
  const dir = tempDir(t);
  const spy = spyOnFiles(t, { failOn: { open: 'EACCES' } });
  assert.equal(core.reserveSession(dir, SID), true);
  assert.equal(core.sessionMarkerState(dir, SID), 'present');
  assert.deepEqual(spy.synced, [path.join(dir, SID)], 'only the marker was synced');
  assert.deepEqual(spy.leaked(), []);
});

test('the marker is forced to disk, then its folder, so it can stand behind the journal after a power loss', (t) => {
  const dir = tempDir(t);
  const spy = spyOnFiles(t);
  assert.equal(core.reserveSession(dir, SID), true);
  assert.deepEqual(spy.synced, [path.join(dir, SID), dir], 'the file first, then the directory entry that names it');
  assert.deepEqual(spy.written, [path.join(dir, SID)]);
  assert.deepEqual(spy.leaked(), [], 'both closed');
  assert.ok(new Date(fs.readFileSync(path.join(dir, SID), 'utf8')).getTime() > 0, 'holding the time');
});

test('a create that fails for any reason but "already there" throws, on every platform and as root too', (t) => {
  const dir = tempDir(t);
  const spy = spyOnFiles(t, { createFails: 'EACCES' });
  assert.throws(() => core.reserveSession(dir, SID), { code: 'EACCES' });
  assert.deepEqual(spy.synced, [], 'nothing was synced: there was no marker');
  t.mock.restoreAll();
  assert.deepEqual(fs.readdirSync(dir), [], 'and nothing was left behind');
  assert.equal(core.reserveSession(dir, SID), true, 'the same id can still be reserved afterwards');
});

test('only "already there" from the create means false', (t) => {
  const dir = tempDir(t);
  spyOnFiles(t, { createFails: 'EEXIST' });
  assert.equal(core.reserveSession(dir, SID), false);
});

test('a malformed session id is never reserved: reserveSession throws and touches nothing', (t) => {
  const base = tempDir(t);
  const dir = path.join(base, 'bridge-sessions');
  for (const id of ['', 'x', '../../etc/passwd', `../${SID}`, SID.toUpperCase(), `${SID}x`, `${SID}/x`, undefined, null, 42, {}]) {
    assert.throws(() => core.reserveSession(dir, id), { message: /malformed/ }, String(id));
  }
  assert.deepEqual(fs.readdirSync(base), [], 'not even the folder was made');
});

test('sessionMarkerState says present when the marker is there and absent only when it provably is not', (t) => {
  const dir = tempDir(t);
  assert.equal(core.sessionMarkerState(dir, SID), 'absent');
  assert.equal(core.sessionMarkerState(path.join(dir, 'not-made-yet'), SID), 'absent', 'no folder yet, so nothing is reserved');
  assert.equal(core.reserveSession(dir, SID), true);
  assert.equal(core.sessionMarkerState(dir, SID), 'present');
  assert.equal(core.sessionMarkerState(dir, OTHER_SID), 'absent', 'another session id is its own marker');
});

test('a dangling symlink in the folder is a marker, as it is for the exclusive create', (t) => {
  const dir = tempDir(t);
  fs.symlinkSync(path.join(dir, 'nowhere'), path.join(dir, SID));
  assert.equal(core.sessionMarkerState(dir, SID), 'present');
  assert.equal(core.reserveSession(dir, SID), false);
});

test('a marker that cannot be checked is unknown, not present: any error but "not found"', (t) => {
  // ENOTDIR: the folder is a file, or its parent is. Neither "reserved" nor "free" can be said.
  const file = path.join(tempDir(t), 'a-file');
  fs.writeFileSync(file, '');
  assert.equal(core.sessionMarkerState(file, SID), 'unknown', 'the folder is a file');
  assert.equal(core.sessionMarkerState(path.join(file, 'bridge-sessions'), SID), 'unknown', "the folder's parent is a file");
});

test('a marker folder that cannot be searched is unknown', { skip: SKIP_AS_ROOT }, (t) => {
  const dir = tempDir(t);
  assert.equal(core.reserveSession(dir, SID), true);
  fs.chmodSync(dir, 0o000);
  try {
    assert.equal(core.sessionMarkerState(dir, SID), 'unknown', 'EACCES, though the marker is in there');
    assert.equal(core.sessionMarkerState(dir, OTHER_SID), 'unknown');
  } finally {
    fs.chmodSync(dir, 0o700);
  }
});

test('a symlink loop in the way makes the marker unknown', (t) => {
  const dir = tempDir(t);
  fs.symlinkSync('loop-b', path.join(dir, 'loop-a'));
  fs.symlinkSync('loop-a', path.join(dir, 'loop-b'));
  assert.equal(core.sessionMarkerState(path.join(dir, 'loop-a'), SID), 'unknown', 'ELOOP');
});

test('a malformed session id is unknown, never present and never absent', (t) => {
  const dir = tempDir(t);
  for (const id of ['', 'x', '../../etc/passwd', `../${SID}`, SID.toUpperCase(), `${SID}x`, `${SID}/x`, undefined, null, 42, {}]) {
    assert.equal(core.sessionMarkerState(dir, id), 'unknown', String(id));
  }
});

// --- Claude folder trust. Claude keeps "do you trust this folder" in ~/.claude.json under the repo's
// --- canonical git root, where a linked worktree counts as its main repository. These tests use
// --- temporary folders and config files only, never the real ~/.claude.json or its lock.

const { execFileSync } = require('node:child_process');

// git with no user or system config, so a hook or a signing setting on this machine cannot get in the way.
function git(cwd, ...args) {
  const env = { ...process.env, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1' };
  execFileSync('git', ['-c', 'init.defaultBranch=main', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', ...args], {
    cwd,
    env,
    stdio: 'pipe',
  });
}

// A repository with one commit, so a worktree can be added to it. Paths are real, as Claude's keys are.
function repo(t) {
  const main = path.join(fs.realpathSync(tempDir(t)), 'main');
  fs.mkdirSync(main);
  git(main, 'init', '-q');
  git(main, 'commit', '-q', '--allow-empty', '--no-verify', '-m', 'init');
  return main;
}

test('canonicalRoot of a plain repository is its root, from the root or any folder inside it', (t) => {
  const main = repo(t);
  fs.mkdirSync(path.join(main, 'src', 'deep'), { recursive: true });
  assert.equal(core.canonicalRoot(main), main);
  assert.equal(core.canonicalRoot(path.join(main, 'src')), main);
  assert.equal(core.canonicalRoot(path.join(main, 'src', 'deep')), main);
});

test('canonicalRoot looks from where a symlinked folder really is, as Claude does from its working directory', (t) => {
  const main = repo(t);
  fs.mkdirSync(path.join(main, 'sub'));
  const link = path.join(fs.realpathSync(tempDir(t)), 'alias');
  fs.symlinkSync(path.join(main, 'sub'), link);
  assert.equal(core.canonicalRoot(link), main, 'the repository the folder is in, not the link as a folder of its own');
});

test('canonicalRoot of a linked worktree is its main repository, which is where Claude keeps the trust', (t) => {
  const main = repo(t);
  const worktree = path.join(path.dirname(main), 'wt');
  git(main, 'worktree', 'add', '-q', '--detach', worktree);
  fs.mkdirSync(path.join(worktree, 'sub'));
  assert.equal(core.canonicalRoot(worktree), main);
  assert.equal(core.canonicalRoot(path.join(worktree, 'sub')), main);
});

test('canonicalRoot follows a relative gitdir from the folder that holds the .git file', (t) => {
  const main = repo(t);
  const worktree = path.join(path.dirname(main), 'wt');
  git(main, 'worktree', 'add', '-q', '--detach', worktree);
  fs.writeFileSync(path.join(worktree, '.git'), 'gitdir: ../main/.git/worktrees/wt\n');
  assert.equal(core.canonicalRoot(worktree), main);
});

test('canonicalRoot of a folder in no repository is the folder itself, as a real path', (t) => {
  const dir = tempDir(t);
  const link = path.join(tempDir(t), 'link');
  fs.symlinkSync(dir, link);
  assert.equal(core.canonicalRoot(dir), fs.realpathSync(dir));
  assert.equal(core.canonicalRoot(link), fs.realpathSync(dir));
});

// A main repository with a linked worktree beside it, made by git itself.
function worktree(t, name = 'wt') {
  const main = repo(t);
  const wt = path.join(path.dirname(main), name);
  git(main, 'worktree', 'add', '-q', '--detach', wt);
  return { main, wt, gitdir: path.join(main, '.git', 'worktrees', name) };
}

test('canonicalRoot of a worktree moved without git worktree repair is the worktree itself, as for Claude', (t) => {
  const { main, wt } = worktree(t, 'wt-old');
  const moved = path.join(path.dirname(main), 'wt-moved');
  fs.renameSync(wt, moved); // its link back to the main repository still names the old place
  assert.equal(core.canonicalRoot(moved), moved);
  assert.equal(core.canonicalRoot(path.join(moved, '.')), moved);
});

test('a .git file copied from a genuine worktree does not make its folder that worktree: the folder is its own key', (t) => {
  const { main, wt } = worktree(t);
  const copy = path.join(path.dirname(main), 'copy');
  fs.mkdirSync(copy);
  fs.copyFileSync(path.join(wt, '.git'), path.join(copy, '.git')); // the backlink names wt, not copy
  assert.equal(core.canonicalRoot(copy), copy);
  assert.equal(core.canonicalRoot(wt), main, 'while the genuine worktree still counts as its main repository');
});

test('a genuine link that runs through symlinks: the backlink is compared as real paths, and the key keeps the gitdir\'s own text', (t) => {
  const { main, wt, gitdir } = worktree(t);
  const base = path.dirname(main);
  const alias = path.join(fs.realpathSync(tempDir(t)), 'alias'); // another name for the folder that holds both
  fs.symlinkSync(base, alias);
  // The backlink names this worktree's .git through the alias: as text it differs, as a real path it is the same.
  fs.writeFileSync(path.join(gitdir, 'gitdir'), `${path.join(alias, 'wt', '.git')}\n`);
  assert.equal(core.canonicalRoot(wt), main, 'the backlink still points back here');
  // The .git file names the gitdir through the alias: the key is the main repository by that same text.
  fs.writeFileSync(path.join(wt, '.git'), `gitdir: ${path.join(alias, 'main', '.git', 'worktrees', 'wt')}\n`);
  assert.equal(core.canonicalRoot(wt), path.join(alias, 'main'), 'as Claude, the key is not made a real path');
});

test('canonicalRoot of a submodule is the submodule itself', (t) => {
  const main = repo(t);
  const other = path.join(path.dirname(main), 'other');
  fs.mkdirSync(other);
  git(other, 'init', '-q');
  git(other, 'commit', '-q', '--allow-empty', '--no-verify', '-m', 'init');
  git(main, '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', other, 'sm');
  assert.ok(fs.statSync(path.join(main, 'sm', '.git')).isFile(), 'git made the submodule with a .git file');
  assert.equal(core.canonicalRoot(path.join(main, 'sm')), path.join(main, 'sm'));
});

test('canonicalRoot of a repository with a separate git dir is its work tree', (t) => {
  const base = fs.realpathSync(tempDir(t));
  const work = path.join(base, 'work');
  git(base, 'init', '-q', `--separate-git-dir=${path.join(base, 'elsewhere.git')}`, work);
  assert.ok(fs.statSync(path.join(work, '.git')).isFile());
  assert.equal(core.canonicalRoot(work), work);
});

test('canonicalRoot of a worktree of a bare repository is the bare repository itself', (t) => {
  const main = repo(t);
  const base = path.dirname(main);
  const bare = path.join(base, 'bare.git');
  git(base, 'clone', '-q', '--bare', main, bare);
  const wt = path.join(base, 'bare-wt');
  git(bare, 'worktree', 'add', '-q', '--detach', wt);
  assert.equal(core.canonicalRoot(wt), bare, 'the common dir, not its parent');
  // A common dir that holds a .git of its own is not taken as the repository: the worktree is its own key.
  fs.mkdirSync(path.join(bare, '.git'));
  assert.equal(core.canonicalRoot(wt), wt);
});

test('a worktree link that is not genuine falls back to the worktree itself', (t) => {
  const cases = {
    'the commondir file is a symlink': ({ gitdir }) => {
      fs.renameSync(path.join(gitdir, 'commondir'), path.join(gitdir, 'commondir.real'));
      fs.symlinkSync('commondir.real', path.join(gitdir, 'commondir'));
    },
    'the gitdir backlink file is a symlink': ({ gitdir }) => {
      fs.renameSync(path.join(gitdir, 'gitdir'), path.join(gitdir, 'gitdir.real'));
      fs.symlinkSync('gitdir.real', path.join(gitdir, 'gitdir'));
    },
    'the gitdir backlink file is missing': ({ gitdir }) => fs.unlinkSync(path.join(gitdir, 'gitdir')),
    'the commondir file is missing': ({ gitdir }) => fs.unlinkSync(path.join(gitdir, 'commondir')),
    'the gitdir is not in the common dir\'s worktrees': ({ gitdir, main }) => {
      const stray = path.join(path.dirname(main), 'stray', 'wt');
      fs.mkdirSync(path.dirname(stray));
      fs.renameSync(gitdir, stray); // still names main/.git as its common dir, by an absolute path
      fs.writeFileSync(path.join(stray, 'commondir'), `${path.join(main, '.git')}\n`);
      return stray;
    },
    'the gitdir is not there': ({ gitdir }) => fs.renameSync(gitdir, `${gitdir}-gone`),
    'the .git file does not say gitdir': ({ wt }) => fs.writeFileSync(path.join(wt, '.git'), 'hello\n'),
  };
  for (const [name, breakIt] of Object.entries(cases)) {
    const layout = worktree(t);
    const stray = breakIt(layout);
    if (stray) fs.writeFileSync(path.join(layout.wt, '.git'), `gitdir: ${stray}\n`);
    assert.equal(core.canonicalRoot(layout.wt), layout.wt, name);
  }
});

test('a .git that is a symlink to a repository\'s .git folder makes its folder a repository root', (t) => {
  const main = repo(t);
  const linked = path.join(path.dirname(main), 'linked');
  fs.mkdirSync(path.join(linked, 'sub'), { recursive: true });
  fs.symlinkSync(path.join(main, '.git'), path.join(linked, '.git'));
  assert.equal(core.canonicalRoot(path.join(linked, 'sub')), linked);
});

test('canonicalRoot is null only for a folder it cannot reach', (t) => {
  const base = fs.realpathSync(tempDir(t));
  assert.equal(core.canonicalRoot(path.join(base, 'not-made')), null, 'a folder that is not there');
  for (const folder of [undefined, null, 42, '', 'relative/path']) {
    assert.equal(core.canonicalRoot(folder), null, String(folder));
  }
});

test('canonicalRoot is null for a folder that cannot be reached', { skip: SKIP_AS_ROOT }, (t) => {
  const base = fs.realpathSync(tempDir(t));
  const locked = path.join(base, 'locked');
  fs.mkdirSync(path.join(locked, 'inner'), { recursive: true });
  fs.chmodSync(locked, 0o000);
  try {
    assert.equal(core.canonicalRoot(path.join(locked, 'inner')), null, 'EACCES');
  } finally {
    fs.chmodSync(locked, 0o700);
  }
});

const KEY = '/home/user/src/repo-a';

// Claude's first trust test, as the bridge reads it: whether missingFlags still names the trust flag.
const trustGiven = (text, key) => !core.missingFlags(text, key).includes('hasTrustDialogAccepted');

test('the trust counts as given only for projects[key].hasTrustDialogAccepted === true', () => {
  const config = (projects) => JSON.stringify({ numStartups: 3, projects });
  assert.equal(trustGiven(config({ [KEY]: { hasTrustDialogAccepted: true } }), KEY), true);
  const no = {
    'flag false': config({ [KEY]: { hasTrustDialogAccepted: false } }),
    'flag missing': config({ [KEY]: { allowedTools: [] } }),
    'flag is a string': config({ [KEY]: { hasTrustDialogAccepted: 'true' } }),
    'flag is 1': config({ [KEY]: { hasTrustDialogAccepted: 1 } }),
    'another key trusted': config({ '/home/user/src/repo-b': { hasTrustDialogAccepted: true } }),
    'a parent trusted': config({ '/home/user/src': { hasTrustDialogAccepted: true } }),
    'entry is null': config({ [KEY]: null }),
    'no projects': JSON.stringify({ numStartups: 3 }),
    'projects is a list': JSON.stringify({ projects: [KEY] }),
    'not JSON': '{nope',
    'empty text': '',
    'JSON null': 'null',
  };
  for (const [name, text] of Object.entries(no)) {
    assert.equal(trustGiven(text, KEY), false, name);
  }
  for (const key of [null, undefined, '', 42]) {
    assert.equal(trustGiven(config({ [String(key)]: { hasTrustDialogAccepted: true } }), key), false, String(key));
  }
  assert.equal(trustGiven(undefined, KEY), false, 'no text at all');
});

// The entry Claude itself makes when it sets trust on a folder it has no entry for (Claude Code 2.1.289).
const CLAUDE_DEFAULT_ENTRY = {
  allowedTools: [],
  mcpContextUris: [],
  mcpServers: {},
  enabledMcpjsonServers: [],
  disabledMcpjsonServers: [],
  hasTrustDialogAccepted: true,
  hasClaudeMdExternalIncludesApproved: false,
  hasClaudeMdExternalIncludesWarningShown: false,
};

// Claude keeps two one-time answers in a project's entry that would stop a start nobody can answer: trust, and
// whether CLAUDE.md files may import files outside the working directory (Claude Code 2.1.293 asks unless one of
// the two imports flags is set; "Yes" sets both true, "No" sets approved false and warning shown true).
const TRUST = 'hasTrustDialogAccepted';
const IMPORTS_APPROVED = 'hasClaudeMdExternalIncludesApproved';
const IMPORTS_WARNED = 'hasClaudeMdExternalIncludesWarningShown';
const ALL_FLAGS = [TRUST, IMPORTS_APPROVED, IMPORTS_WARNED];
// What the bridge writes for a key with no entry: Claude's default, trusted, with imports approved.
const READY_ENTRY = { ...CLAUDE_DEFAULT_ENTRY, [IMPORTS_APPROVED]: true, [IMPORTS_WARNED]: true };

test('missingFlags names what the bridge would set: trust when it is not given, imports approval only while undecided', () => {
  const text = (entry) => JSON.stringify({ projects: entry === undefined ? {} : { [KEY]: entry } });
  const cases = [
    ['no entry', text(undefined), ALL_FLAGS],
    ['text that does not parse', '{nope', ALL_FLAGS],
    ['trusted, imports undecided', text({ [TRUST]: true }), [IMPORTS_APPROVED, IMPORTS_WARNED]],
    ['trusted, imports flags false as in Claude\'s default', text({ ...CLAUDE_DEFAULT_ENTRY }), [IMPORTS_APPROVED, IMPORTS_WARNED]],
    ['trusted, imports approved', text({ [TRUST]: true, [IMPORTS_APPROVED]: true, [IMPORTS_WARNED]: true }), []],
    ['trusted, imports declined at the dialog', text({ [TRUST]: true, [IMPORTS_APPROVED]: false, [IMPORTS_WARNED]: true }), []],
    ['not trusted, imports declined', text({ [TRUST]: false, [IMPORTS_APPROVED]: false, [IMPORTS_WARNED]: true }), [TRUST]],
    ['not trusted, imports approved', text({ [IMPORTS_APPROVED]: true }), [TRUST]],
    // Trust counts only as exactly true; the imports answer counts as Claude counts it, when either flag is truthy.
    ['trust that is not exactly true, imports approved by a truthy value', text({ [TRUST]: 'true', [IMPORTS_APPROVED]: 1 }), [TRUST]],
    ['trusted, imports declined by hand with a truthy warning', text({ [TRUST]: true, [IMPORTS_APPROVED]: false, [IMPORTS_WARNED]: 1 }), []],
    ['trusted, imports flags falsy but present', text({ [TRUST]: true, [IMPORTS_APPROVED]: 0, [IMPORTS_WARNED]: '' }), [IMPORTS_APPROVED, IMPORTS_WARNED]],
  ];
  for (const [name, configText, expected] of cases) {
    assert.deepEqual(core.missingFlags(configText, KEY), expected, name);
  }
});

// A temporary stand-in for ~/.claude.json (and its lock folder), written the way Claude writes it.
function claudeConfig(t, content) {
  const dir = fs.realpathSync(tempDir(t));
  const file = path.join(dir, '.claude.json');
  if (content !== undefined) {
    fs.writeFileSync(file, typeof content === 'string' ? content : JSON.stringify(content, null, 2), { mode: 0o600 });
  }
  return { dir, file, lock: `${file}.lock` };
}
const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const PAST = new Date('2026-01-02T03:04:05Z'); // an mtime no write in this run can give

test('seedTrust gives a missing entry Claude\'s own default, trusted and with imports approved, and keeps every other key and project', (t) => {
  const before = { numStartups: 7, userID: 'abc', projects: { '/home/user/src/other': { allowedTools: ['x'], hasTrustDialogAccepted: false } } };
  const { dir, file } = claudeConfig(t, before);
  assert.deepEqual(core.seedTrust(file, KEY), { result: 'seeded', flags: ALL_FLAGS });
  const expected = { ...before, projects: { ...before.projects, [KEY]: READY_ENTRY } };
  assert.deepEqual(readJson(file), expected);
  assert.equal(fs.readFileSync(file, 'utf8'), JSON.stringify(expected, null, 2), 'formatted as Claude formats it');
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.deepEqual(fs.readdirSync(dir), ['.claude.json'], 'no lock and no temporary file left');
});

test('seedTrust makes the projects table when the config has none', (t) => {
  const { file } = claudeConfig(t, { numStartups: 1 });
  assert.deepEqual(core.seedTrust(file, KEY), { result: 'seeded', flags: ALL_FLAGS });
  assert.deepEqual(readJson(file), { numStartups: 1, projects: { [KEY]: READY_ENTRY } });
});

test('seedTrust keeps every field of an existing entry and sets only the flags', (t) => {
  const entry = {
    allowedTools: ['Bash(ls)'],
    mcpServers: { local: { command: 'x' } },
    hasTrustDialogAccepted: false,
    lastSessionId: 'abc',
    projectOnboardingSeenCount: 4,
  };
  const { file } = claudeConfig(t, { projects: { [KEY]: entry }, theme: 'dark' });
  fs.chmodSync(file, 0o644);
  assert.deepEqual(core.seedTrust(file, KEY), { result: 'seeded', flags: ALL_FLAGS });
  assert.deepEqual(readJson(file), { projects: { [KEY]: { ...entry, [TRUST]: true, [IMPORTS_APPROVED]: true, [IMPORTS_WARNED]: true } }, theme: 'dark' });
  assert.equal(fs.statSync(file).mode & 0o777, 0o600, 'written with the mode Claude uses');
});

test('seedTrust writes nothing for a folder that is trusted with its imports decided, approved or declined', (t) => {
  for (const [name, decided] of [['approved', { [IMPORTS_APPROVED]: true, [IMPORTS_WARNED]: true }], ['declined', { [IMPORTS_APPROVED]: false, [IMPORTS_WARNED]: true }]]) {
    const { dir, file } = claudeConfig(t, { projects: { [KEY]: { [TRUST]: true, allowedTools: [], ...decided } } });
    fs.utimesSync(file, PAST, PAST);
    const bytes = fs.readFileSync(file);
    assert.deepEqual(core.seedTrust(file, KEY), { result: 'already', flags: [] }, name);
    assert.equal(fs.statSync(file).mtimeMs, PAST.getTime(), `${name}: mtime unchanged`);
    assert.deepEqual(fs.readFileSync(file), bytes, name);
    assert.deepEqual(fs.readdirSync(dir), ['.claude.json'], name);
  }
});

test('a decline hand-edited with a truthy warning flag counts as decided, as Claude counts it: nothing is written', (t) => {
  const { file } = claudeConfig(t, { projects: { [KEY]: { [TRUST]: true, [IMPORTS_APPROVED]: false, [IMPORTS_WARNED]: 1 } } });
  fs.utimesSync(file, PAST, PAST);
  const bytes = fs.readFileSync(file);
  assert.deepEqual(core.seedTrust(file, KEY), { result: 'already', flags: [] });
  assert.deepEqual(fs.readFileSync(file), bytes);
  assert.equal(fs.statSync(file).mtimeMs, PAST.getTime());
});

test('a trusted folder whose imports are undecided gets only the imports approval, everything else kept', (t) => {
  const entry = { allowedTools: ['Bash(ls)'], [TRUST]: true, [IMPORTS_APPROVED]: false, [IMPORTS_WARNED]: false, lastSessionId: 'abc' };
  const { file } = claudeConfig(t, { projects: { [KEY]: entry } });
  assert.deepEqual(core.seedTrust(file, KEY), { result: 'seeded', flags: [IMPORTS_APPROVED, IMPORTS_WARNED] });
  assert.deepEqual(readJson(file).projects[KEY], { ...entry, [IMPORTS_APPROVED]: true, [IMPORTS_WARNED]: true });
});

test('imports the user declined at Claude\'s dialog are never overridden: an untrusted folder gets the trust only', (t) => {
  const entry = { [TRUST]: false, [IMPORTS_APPROVED]: false, [IMPORTS_WARNED]: true };
  const { file } = claudeConfig(t, { projects: { [KEY]: entry } });
  assert.deepEqual(core.seedTrust(file, KEY), { result: 'seeded', flags: [TRUST] });
  assert.deepEqual(readJson(file).projects[KEY], { ...entry, [TRUST]: true }, 'still declined');
});

test('a lock held by someone else is waited for about 2 s, then seedTrust fails with ELOCKED and leaves that lock alone', (t) => {
  const { dir, file, lock } = claudeConfig(t, { projects: {} });
  fs.mkdirSync(lock);
  fs.writeFileSync(path.join(lock, 'holder'), 'claude');
  fs.utimesSync(lock, PAST, PAST);
  const bytes = fs.readFileSync(file);
  const started = Date.now();
  assert.throws(() => core.seedTrust(file, KEY), { code: 'ELOCKED' });
  const waited = Date.now() - started;
  assert.ok(waited >= 1900 && waited < 4000, `waited ${waited} ms`);
  assert.equal(fs.statSync(lock).mtimeMs, PAST.getTime(), 'the lock was not touched');
  assert.deepEqual(fs.readdirSync(lock), ['holder'], 'nor what is in it');
  assert.deepEqual(fs.readFileSync(file), bytes, 'the config was not written');
  assert.deepEqual(fs.readdirSync(dir).sort(), ['.claude.json', '.claude.json.lock']);
});

test('a lock that its holder releases while seedTrust waits is taken, and the trust is seeded', async (t) => {
  const { dir, file, lock } = claudeConfig(t, { projects: {} });
  fs.mkdirSync(lock);
  // Another process holds the lock for a moment: seedTrust blocks this one while it waits.
  const holder = spawn(process.execPath, ['-e', `setTimeout(() => require('node:fs').rmdirSync(${JSON.stringify(lock)}), 300)`]);
  const exited = new Promise((resolve) => holder.on('exit', resolve));
  try {
    assert.equal(core.seedTrust(file, KEY).result, 'seeded');
  } finally {
    await exited;
  }
  assert.equal(readJson(file).projects[KEY].hasTrustDialogAccepted, true);
  assert.deepEqual(fs.readdirSync(dir), ['.claude.json']);
});

test('the config is written under the lock, atomically: a temporary file beside it, synced, then renamed over it', (t) => {
  const { dir, file, lock } = claudeConfig(t, { projects: {} });
  const real = { rename: fs.renameSync, fsync: fs.fsyncSync, open: fs.openSync };
  const opened = new Map(); // descriptor -> path, for what is open right now (a descriptor is reused once closed)
  const everOpened = [];
  const events = [];
  t.mock.method(fs, 'openSync', (target, ...rest) => {
    const fd = real.open(target, ...rest);
    opened.set(fd, String(target));
    everOpened.push(String(target));
    return fd;
  });
  t.mock.method(fs, 'fsyncSync', (fd) => {
    events.push(['fsync', opened.get(fd)]);
    return real.fsync(fd);
  });
  t.mock.method(fs, 'renameSync', (from, to) => {
    events.push(['rename', path.dirname(String(from)), String(to), fs.existsSync(lock)]);
    return real.rename(from, to);
  });
  assert.equal(core.seedTrust(file, KEY).result, 'seeded');
  const renames = events.filter(([name]) => name === 'rename');
  assert.deepEqual(renames, [['rename', dir, file, true]], 'one rename, from the same folder, over the config, with the lock held');
  const temp = everOpened.find((target) => target !== file && target !== dir && path.dirname(target) === dir);
  assert.ok(temp, 'a temporary file was opened in the same folder');
  const tempSync = events.findIndex(([name, target]) => name === 'fsync' && target === temp);
  assert.ok(tempSync >= 0 && tempSync < events.indexOf(renames[0]), 'the temporary file was synced before the rename');
  assert.ok(!fs.existsSync(lock), 'the lock was released');
});

test('a config that does not parse, or has no room for the entry, changes nothing and leaves no lock', (t) => {
  const bad = {
    'not JSON': '{"projects": {',
    'a list': '[]',
    'null': 'null',
    'projects is a list': JSON.stringify({ projects: [] }),
    'the entry is not an object': JSON.stringify({ projects: { [KEY]: 'yes' } }),
  };
  for (const [name, text] of Object.entries(bad)) {
    const { dir, file } = claudeConfig(t, text);
    assert.throws(() => core.seedTrust(file, KEY), name);
    assert.equal(fs.readFileSync(file, 'utf8'), text, name);
    assert.deepEqual(fs.readdirSync(dir), ['.claude.json'], name);
  }
});

test('a missing config throws and nothing is created: no config, no lock, no temporary file', (t) => {
  const { dir, file } = claudeConfig(t);
  assert.throws(() => core.seedTrust(file, KEY), { code: 'ENOENT' });
  assert.deepEqual(fs.readdirSync(dir), []);
  assert.throws(() => core.seedTrust(path.join(dir, 'not-made', '.claude.json'), KEY), { code: 'ENOENT' });
  assert.deepEqual(fs.readdirSync(dir), []);
});

test('a write that fails leaves the config as it was, no temporary file, and no lock', (t) => {
  for (const failing of ['renameSync', 'fsyncSync', 'writeSync']) {
    const { dir, file } = claudeConfig(t, { projects: {} });
    const bytes = fs.readFileSync(file);
    const mock = t.mock.method(fs, failing, () => {
      throw Object.assign(new Error(`EIO: simulated ${failing}`), { code: 'EIO' });
    });
    try {
      assert.throws(() => core.seedTrust(file, KEY), { code: 'EIO' }, failing);
    } finally {
      mock.mock.restore();
    }
    assert.deepEqual(fs.readFileSync(file), bytes, failing);
    assert.deepEqual(fs.readdirSync(dir), ['.claude.json'], failing);
  }
});

test('a write that does not read back as trusted throws, and the lock is still released', (t) => {
  const { dir, file } = claudeConfig(t, { projects: {} });
  const original = fs.readFileSync(file, 'utf8');
  const realRead = fs.readFileSync;
  let reads = 0;
  t.mock.method(fs, 'readFileSync', (target, ...rest) => {
    if (String(target) === file) {
      reads += 1;
      if (reads === 2) return original; // the read-back sees the old config
    }
    return realRead(target, ...rest);
  });
  assert.throws(() => core.seedTrust(file, KEY), { code: 'EVERIFY' });
  assert.equal(reads, 2);
  t.mock.restoreAll();
  assert.deepEqual(fs.readdirSync(dir), ['.claude.json']);
});

test('a write whose imports approval does not read back throws too: every flag that was set is checked', (t) => {
  const { file } = claudeConfig(t, { projects: {} });
  const realRead = fs.readFileSync;
  let reads = 0;
  t.mock.method(fs, 'readFileSync', (target, ...rest) => {
    if (String(target) === file) {
      reads += 1;
      if (reads === 2) return JSON.stringify({ projects: { [KEY]: { [TRUST]: true } } }); // trusted, imports lost
    }
    return realRead(target, ...rest);
  });
  assert.throws(() => core.seedTrust(file, KEY), { code: 'EVERIFY' });
});

test('a config that is a symlink is refused with ESYMLINK: the link stays a link and its target is not written', (t) => {
  const { dir, file } = claudeConfig(t);
  const target = path.join(fs.realpathSync(tempDir(t)), 'dotfiles-claude.json');
  fs.writeFileSync(target, JSON.stringify({ projects: {} }, null, 2));
  fs.symlinkSync(target, file);
  const bytes = fs.readFileSync(target);
  assert.throws(() => core.seedTrust(file, KEY), { code: 'ESYMLINK' });
  assert.equal(fs.lstatSync(file).isSymbolicLink(), true, 'still a link');
  assert.deepEqual(fs.readFileSync(target), bytes, 'the target was not written');
  assert.deepEqual(fs.readdirSync(dir), ['.claude.json'], 'no lock and no temporary file left');
});

test('seedTrust refuses a key that is not an absolute path, before it touches anything', (t) => {
  const { dir, file } = claudeConfig(t, { projects: {} });
  for (const key of [null, undefined, '', 'relative', 42]) {
    assert.throws(() => core.seedTrust(file, key), String(key));
  }
  assert.deepEqual(fs.readdirSync(dir), ['.claude.json']);
  assert.deepEqual(readJson(file), { projects: {} });
});

// --- Allowed roots: REMOTE_HANDS_ALLOWED_ROOTS, then config.json in the Remote Hands home, then ~/src/ and
// ~/.worktrees/ (the same order and rules as the service's start_request.allowed_roots; the service's
// tests/test_bridge_contract.py runs both parsers over the same settings).

function withEnv(name, value, fn) {
  const before = process.env[name];
  if (value === undefined) delete process.env[name];
  else process.env[name] = value;
  try {
    return fn();
  } finally {
    if (before === undefined) delete process.env[name];
    else process.env[name] = before;
  }
}

test('allowed roots come from the environment first, then config.json, then the WSL home', (t) => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-roots-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  fs.writeFileSync(path.join(home, 'config.json'), JSON.stringify({ allowed_roots: ['/srv/repos'] }));
  assert.deepEqual(Array.from(core.allowedRoots(home)), ['/home/user/src/', '/home/user/.worktrees/']);
  withEnv('REMOTE_HANDS_ALLOWED_ROOTS', undefined, () => {
    assert.deepEqual(Array.from(core.allowedRoots(home)), ['/srv/repos/']);
    withEnv('HOME', '/home/someone', () => {
      assert.deepEqual(Array.from(core.allowedRoots()), ['/home/someone/src/', '/home/someone/.worktrees/']);
      assert.deepEqual(Array.from(core.allowedRoots(path.join(home, 'none'))), ['/home/someone/src/', '/home/someone/.worktrees/']);
    });
  });
});

test('a config.json that cannot be used refuses every folder', (t) => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'rh-roots-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  withEnv('REMOTE_HANDS_ALLOWED_ROOTS', undefined, () => {
    for (const text of ['{torn', '[]', '{"other": 1}', '{"allowed_roots": "/srv"}', '{"allowed_roots": []}', '{"allowed_roots": ["/"]}']) {
      fs.writeFileSync(path.join(home, 'config.json'), text);
      assert.throws(() => core.allowedRoots(home), undefined, text);
      assert.deepEqual(core.rootsOrNone(home), [], text);
    }
  });
});

test('no allowed root means no folder is inside one, and no start is valid', () => {
  assert.equal(core.insideAllowedRoots(FOLDER, []), false);
  assert.equal(core.insideAllowedRoots(FOLDER, ['/srv/']), false);
  assert.equal(core.insideAllowedRoots(FOLDER, ['/home/user/src/']), true);
  assert.equal(core.validateStart(good(), [FOLDER], []).error, NOT_IN_ROOT_ERROR);
});

test('a root that could widen the check is refused, and roots end in one slash', () => {
  for (const root of ['/', '//', 'relative/src', '', '/home//src', '/home/./src', '/home/../src', '/home/a' + char(10) + 'b', 7]) {
    assert.throws(() => core.parseRoots([root]), undefined, JSON.stringify(root));
  }
  assert.throws(() => core.parseRoots([]));
  assert.throws(() => core.parseRoots('/home/a'));
  assert.deepEqual(Array.from(core.parseRoots(['/a/b', '/c/d/', '/e//'])), ['/a/b/', '/c/d/', '/e/']);
});
