'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const core = require('../src/core');
// The allowed roots these tests run with (core.allowedRoots reads this first): the made-up folders they use.
process.env.REMOTE_HANDS_ALLOWED_ROOTS = '/home/user/src/:/home/user/.worktrees/';
const { createBridgeServer, MAX_BODY_BYTES } = require('../src/server');

const RID = 'req-0001-abcd';
const SID = core.sessionIdFor(RID); // the bridge refuses any session id but the one derived from the request id
const FOLDER = '/home/user/src/repo-a';
// 43 URL-safe characters like a real token, and lopsided on purpose (mixed case, digits, '-',
// '_', not a palindrome, not sorted): a uniform token reads the same reversed, sorted,
// lower-cased, or stripped, so a check that mangled the presented token would still pass.
// Made up for tests, not a credential (hence gitleaks:allow).
const TOKEN = 'uHSEiyLq_2OgI1mwLGyzugcJdL4Foz-12gmQopYLCjk'; // gitleaks:allow
const INSTANCE = '11111111-2222-4333-8444-555555555555';
const OTHER_SID = '00000000-0000-5000-8000-000000000000'; // a valid session id that no request id derives

function body() {
  return { request_id: RID, session_id: SID, cwd: FOLDER, label: 'hands repo-a req-00', prompt: 'fix it' };
}

async function withServer(overrides, fn) {
  const calls = [];
  const deps = {
    instance: INSTANCE,
    version: '0.1.0',
    readToken: () => TOKEN,
    killSwitchOn: () => false,
    claudeAvailable: () => true,
    audit: () => {},
    reserveSession: () => true,
    folders: () => [FOLDER],
    trustState: () => ({ state: 'trusted', key: FOLDER }),
    seedTrust: () => ({ result: 'seeded', flags: [...core.SEEDED_FLAGS] }),
    sessionState: () => 'absent',
    startClaude: async (value) => {
      calls.push(value);
      return { terminalPid: 4242 };
    },
    ...overrides,
  };
  const server = createBridgeServer(deps);
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    await fn(server.address().port, calls);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

function request(port, { method = 'POST', path = '/start-claude', token = TOKEN, payload = body() } = {}) {
  return new Promise((resolve, reject) => {
    let data;
    if (payload !== undefined) data = Buffer.from(typeof payload === 'string' ? payload : JSON.stringify(payload));
    const headers = {};
    if (token !== null) headers.authorization = `Bearer ${token}`;
    if (data) headers['content-length'] = data.length;
    const req = http.request({ host: '127.0.0.1', port, method, path, headers, agent: false }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, json: JSON.parse(Buffer.concat(chunks).toString() || '{}') }));
    });
    req.on('error', reject);
    if (data) req.write(data);
    req.end();
  });
}

test('ping answers with the instance and needs no token', () =>
  withServer({}, async (port) => {
    const res = await request(port, { method: 'GET', path: '/ping', token: null, payload: undefined });
    assert.equal(res.status, 200);
    assert.deepEqual(res.json, { instance: INSTANCE, version: '0.1.0' });
  }));

test('a valid start runs Claude once and reports the terminal', () =>
  withServer({}, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 200);
    assert.deepEqual(res.json, { status: 'started', session_id: SID, terminal_pid: 4242 });
    assert.deepEqual(calls, [
      { requestId: RID, sessionId: SID, cwd: FOLDER, label: 'hands repo-a req-00', prompt: 'fix it' },
    ]);
  }));

test('a missing or wrong token is refused and nothing runs', () =>
  withServer({}, async (port, calls) => {
    assert.equal((await request(port, { token: null })).status, 401);
    assert.equal((await request(port, { token: 'x'.repeat(40) })).status, 401);
    assert.equal(calls.length, 0);
  }));

test('an unreadable token file refuses everything', () =>
  withServer(
    {
      readToken: () => {
        throw new Error('ENOENT');
      },
    },
    async (port, calls) => {
      assert.equal((await request(port)).status, 401);
      assert.equal(calls.length, 0);
    },
  ));

test('the kill switch refuses with its own reason', () =>
  withServer({ killSwitchOn: () => true }, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 403);
    assert.equal(res.json.status, 'refused');
    assert.equal(res.json.reason, 'kill_switch');
    assert.equal(calls.length, 0);
  }));

test('a session id that already exists is refused', () =>
  withServer({ sessionState: () => 'exists' }, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 409);
    assert.equal(res.json.reason, 'duplicate_session');
    assert.equal(calls.length, 0);
  }));

test('a session id that cannot be checked is refused with its own reason', () =>
  withServer({ sessionState: () => 'unknown' }, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 409);
    assert.equal(res.json.reason, 'session_check_failed');
    assert.equal(calls.length, 0);
  }));

test('a cwd outside the allowed roots the window was given is refused, and roots that cannot be read refuse every cwd', async () => {
  await withServer({ allowedRoots: () => ['/srv/repos/'] }, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 400);
    assert.equal(res.json.reason, 'bad_request');
    assert.equal(calls.length, 0);
  });
  const unreadable = () => {
    throw new Error('config.json is not valid JSON');
  };
  await withServer({ allowedRoots: unreadable }, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 400);
    assert.equal(res.json.reason, 'bad_request');
    assert.equal(calls.length, 0);
  });
});

test('a folder this window does not have is refused', () =>
  withServer({ folders: () => ['/home/user'] }, async (port, calls) => {
    assert.equal((await request(port)).status, 400);
    assert.equal(calls.length, 0);
  }));

test('a session id that is not the one derived from the request id is refused, and nothing starts', () =>
  withServer({}, async (port, calls) => {
    const res = await request(port, { payload: { ...body(), session_id: OTHER_SID } });
    assert.equal(res.status, 400);
    assert.deepEqual(res.json, {
      status: 'refused',
      reason: 'bad_request',
      error: 'session_id does not match request_id',
    });
    assert.equal(calls.length, 0);
  }));

test('two requests with one request id and different session ids cannot both start', () =>
  withServer({}, async (port, calls) => {
    // A caller holding the token, trying to start a second session for one request.
    const results = await Promise.all([request(port), request(port, { payload: { ...body(), session_id: OTHER_SID } })]);
    assert.deepEqual(results.map((res) => res.status), [200, 400]);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].sessionId, body().session_id);
  }));

test('bad JSON is refused', () =>
  withServer({}, async (port) => {
    assert.equal((await request(port, { payload: '{nope' })).status, 400);
  }));

test('an oversized body is refused from the header alone', () =>
  withServer({}, async (port, calls) => {
    const status = await new Promise((resolve, reject) => {
      const req = http.request(
        {
          host: '127.0.0.1',
          port,
          method: 'POST',
          path: '/start-claude',
          agent: false,
          headers: { authorization: `Bearer ${TOKEN}`, 'content-length': MAX_BODY_BYTES + 1 },
        },
        (res) => {
          res.resume();
          res.on('end', () => resolve(res.statusCode));
        },
      );
      req.on('error', reject);
      req.flushHeaders();
    });
    assert.equal(status, 413);
    assert.equal(calls.length, 0);
  }));

test('unknown paths and wrong methods are refused', () =>
  withServer({}, async (port) => {
    assert.equal((await request(port, { path: '/run', payload: undefined })).status, 404);
    assert.equal((await request(port, { method: 'GET', payload: undefined })).status, 405);
    assert.equal((await request(port, { path: '/ping', payload: undefined })).status, 405);
  }));

test('a Claude that exits at once is reported as failed', () =>
  withServer(
    {
      startClaude: async () => {
        const err = new Error('claude exited right after starting');
        err.reason = 'exited';
        err.exitCode = 1;
        throw err;
      },
    },
    async (port) => {
      const res = await request(port);
      assert.equal(res.status, 500);
      assert.equal(res.json.status, 'failed');
      assert.equal(res.json.reason, 'exited');
      assert.equal(res.json.exit_code, 1);
    },
  ));

// Declares a body over the limit and never sends it: the server must refuse from the header alone.
function oversizedStart(port) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        host: '127.0.0.1',
        port,
        method: 'POST',
        path: '/start-claude',
        agent: false,
        headers: { authorization: `Bearer ${TOKEN}`, 'content-length': MAX_BODY_BYTES + 1 },
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => resolve({ status: res.statusCode, json: JSON.parse(Buffer.concat(chunks).toString() || '{}') }));
      },
    );
    req.on('error', reject);
    req.flushHeaders();
  });
}

// The service sorts a refusal by its reason (NOTHING_SENT_REASONS in src/remote_hands/actions.py):
// every reason here means nothing was started and the request may be retried, except
// duplicate_session, which means the session may already have run. A reason that drifts from that
// list turns a retryable refusal into an unknown one, so these are exact. Keep the two in step.
const boom = () => {
  throw new Error('boom');
};
const OUTSIDE = { state: 'outside', key: '/mnt/c/Users/user/repo' };
const SEEDABLE_KEY = '/home/user/src/main-repo';
const ALL_FLAGS = ['hasTrustDialogAccepted', 'hasClaudeMdExternalIncludesApproved', 'hasClaudeMdExternalIncludesWarningShown'];
const IMPORTS_FLAGS = ALL_FLAGS.slice(1);
const SEEDABLE = { state: 'seedable', key: SEEDABLE_KEY, flags: ALL_FLAGS };
const SEEDED = { result: 'seeded', flags: ALL_FLAGS };
const REFUSALS = [
  { name: 'a wrong token', send: (port) => request(port, { token: 'x'.repeat(40) }), status: 401, reason: 'unauthorized' },
  { name: 'a missing token', send: (port) => request(port, { token: null }), status: 401, reason: 'unauthorized' },
  {
    name: 'an unreadable token file',
    overrides: {
      readToken: () => {
        throw new Error('ENOENT');
      },
    },
    status: 401,
    reason: 'unauthorized',
  },
  { name: 'an oversized body', send: oversizedStart, status: 413, reason: 'too_large' },
  { name: 'bad JSON', send: (port) => request(port, { payload: '{nope' }), status: 400, reason: 'bad_request' },
  { name: 'a folder this window does not have', overrides: { folders: () => ['/home/user'] }, status: 400, reason: 'bad_request' },
  {
    name: 'a session id that is not the one derived from the request id',
    send: (port) => request(port, { payload: { ...body(), session_id: OTHER_SID } }),
    status: 400,
    reason: 'bad_request',
  },
  { name: 'GET on the start route', send: (port) => request(port, { method: 'GET', payload: undefined }), status: 405, reason: 'method' },
  { name: 'POST on ping', send: (port) => request(port, { path: '/ping', payload: undefined }), status: 405, reason: 'method' },
  { name: 'an unknown path', send: (port) => request(port, { path: '/run', payload: undefined }), status: 404, reason: 'path' },
  { name: 'the kill switch', overrides: { killSwitchOn: () => true }, status: 403, reason: 'kill_switch' },
  {
    name: 'the kill switch, even when the session also exists',
    overrides: { killSwitchOn: () => true, sessionState: () => 'exists' },
    status: 403,
    reason: 'kill_switch',
  },
  { name: 'a Claude command line that is not installed', overrides: { claudeAvailable: () => false }, status: 409, reason: 'claude_unavailable' },
  {
    name: 'the kill switch, even when Claude is also unavailable',
    overrides: { killSwitchOn: () => true, claudeAvailable: () => false },
    status: 403,
    reason: 'kill_switch',
  },
  {
    name: 'a missing Claude, even when the session also exists',
    overrides: { claudeAvailable: () => false, sessionState: () => 'exists' },
    status: 409,
    reason: 'claude_unavailable',
  },
  { name: 'an existing session', overrides: { sessionState: () => 'exists' }, status: 409, reason: 'duplicate_session' },
  { name: 'a session that cannot be checked', overrides: { sessionState: () => 'unknown' }, status: 409, reason: 'session_check_failed' },
  // A check that throws before anything starts is a refusal the service can retry, never an unknown.
  { name: 'folders that cannot be read (no folders, so the cwd is refused)', overrides: { folders: boom }, status: 400, reason: 'bad_request' },
  { name: 'a kill switch that cannot be read (counts as switched off)', overrides: { killSwitchOn: boom }, status: 403, reason: 'kill_switch' },
  { name: 'a session check that throws', overrides: { sessionState: boom }, status: 409, reason: 'session_check_failed' },
  { name: 'a Claude check that throws', overrides: { claudeAvailable: boom }, status: 409, reason: 'claude_unavailable' },
  // The intent is written only once the session id is known to be unused, so an audit log that cannot be
  // written never turns an earlier refusal into audit_failed.
  { name: 'an audit log that cannot be written', overrides: { audit: boom }, status: 409, reason: 'audit_failed' },
  { name: 'an existing session, even when the audit log cannot be written', overrides: { sessionState: () => 'exists', audit: boom }, status: 409, reason: 'duplicate_session' },
  { name: 'a session that cannot be checked, even when the audit log cannot be written', overrides: { sessionState: () => 'unknown', audit: boom }, status: 409, reason: 'session_check_failed' },
  { name: 'a missing Claude, even when the audit log cannot be written', overrides: { claudeAvailable: () => false, audit: boom }, status: 409, reason: 'claude_unavailable' },
  // The reservation comes after the intent: it is made only once the intent is on record, and a refusal
  // for either reason leaves nothing started.
  { name: 'a session id another window has reserved', overrides: { reserveSession: () => false }, status: 409, reason: 'duplicate_session' },
  { name: 'a reservation that cannot be made', overrides: { reserveSession: boom }, status: 409, reason: 'reservation_failed' },
  { name: 'a reservation that answers with something else', overrides: { reserveSession: () => 'yes' }, status: 409, reason: 'reservation_failed' },
  { name: 'an audit log that cannot be written, even when the session id is also reserved', overrides: { audit: boom, reserveSession: () => false }, status: 409, reason: 'audit_failed' },
  { name: 'an existing session, even when the reservation would also fail', overrides: { sessionState: () => 'exists', reserveSession: boom }, status: 409, reason: 'duplicate_session' },
  { name: 'a session that cannot be checked, even when the reservation would also fail', overrides: { sessionState: () => 'unknown', reserveSession: boom }, status: 409, reason: 'session_check_failed' },
  { name: 'the kill switch, even when the audit log cannot be written', overrides: { killSwitchOn: () => true, audit: boom }, status: 403, reason: 'kill_switch' },
  // Claude must trust the folder before anything else is checked or written for the start (trust_* records
  // aside): a repo outside the allowed roots is never marked trusted, and a seeding that fails starts nothing.
  { name: 'a folder outside the allowed roots that Claude does not trust', overrides: { trustState: () => OUTSIDE }, status: 409, reason: 'untrusted_folder' },
  { name: 'a folder whose repository cannot be worked out', overrides: { trustState: () => ({ state: 'outside', key: null }) }, status: 409, reason: 'untrusted_folder' },
  { name: 'a trust check that throws', overrides: { trustState: boom }, status: 409, reason: 'untrusted_folder' },
  { name: 'a trust check that answers with something else', overrides: { trustState: () => 'trusted' }, status: 409, reason: 'untrusted_folder' },
  { name: 'a seedable answer without a key', overrides: { trustState: () => ({ state: 'seedable' }) }, status: 409, reason: 'untrusted_folder' },
  { name: 'a trust check that answers with a promise', overrides: { trustState: async () => ({ state: 'trusted', key: FOLDER }) }, status: 409, reason: 'untrusted_folder' },
  { name: 'a seeding that fails', overrides: { trustState: () => SEEDABLE, seedTrust: boom }, status: 409, reason: 'trust_failed' },
  { name: 'a seeding that answers with something else', overrides: { trustState: () => SEEDABLE, seedTrust: () => true }, status: 409, reason: 'trust_failed' },
  { name: 'a seeding that answers with a bare word', overrides: { trustState: () => SEEDABLE, seedTrust: () => 'seeded' }, status: 409, reason: 'trust_failed' },
  { name: 'a seeding that answers with an unknown result', overrides: { trustState: () => SEEDABLE, seedTrust: () => ({ result: 'maybe', flags: [] }) }, status: 409, reason: 'trust_failed' },
  { name: 'a trust intent that cannot be written', overrides: { trustState: () => SEEDABLE, audit: boom }, status: 409, reason: 'audit_failed' },
  { name: 'an untrusted folder, even when the session also exists', overrides: { trustState: () => OUTSIDE, sessionState: () => 'exists' }, status: 409, reason: 'untrusted_folder' },
  { name: 'the kill switch, even when the folder is untrusted', overrides: { killSwitchOn: () => true, trustState: () => OUTSIDE }, status: 403, reason: 'kill_switch' },
  { name: 'a missing Claude, even when the folder is untrusted', overrides: { claudeAvailable: () => false, trustState: () => OUTSIDE }, status: 409, reason: 'claude_unavailable' },
  { name: 'a bad request, even when the folder is untrusted', overrides: { folders: () => ['/home/user'], trustState: () => OUTSIDE }, status: 400, reason: 'bad_request' },
];

test('every refusal says refused, names the reason the service expects, and starts nothing', async () => {
  for (const { name, overrides = {}, send = (port) => request(port), status, reason } of REFUSALS) {
    await withServer(overrides, async (port, calls) => {
      const res = await send(port);
      assert.equal(res.status, status, name);
      assert.equal(res.json.status, 'refused', name);
      assert.equal(res.json.reason, reason, name);
      assert.ok(typeof res.json.error === 'string' && res.json.error.length > 0, name);
      assert.equal(calls.length, 0, name);
    });
  }
});

test('a session check that answers anything but absent never starts Claude', async () => {
  // The check is three-valued; a value it does not recognise (such as an old true or false) fails closed.
  // So does a promise: the check must stay synchronous, because an await between it and the start would
  // let a second request for the same session id in before the first has reserved it.
  for (const answer of [undefined, null, true, false, '', 'maybe', Promise.resolve('absent')]) {
    await withServer({ sessionState: () => answer }, async (port, calls) => {
      const res = await request(port);
      assert.equal(res.status, 409, String(answer));
      assert.equal(res.json.reason, 'session_check_failed', String(answer));
      assert.equal(calls.length, 0, String(answer));
    });
  }
});

test('a missing Claude is refused before the session id is checked, so nothing is reserved', () => {
  let sessionChecks = 0;
  return withServer(
    {
      claudeAvailable: () => false,
      sessionState: () => {
        sessionChecks += 1;
        return 'absent';
      },
    },
    async (port, calls) => {
      const res = await request(port);
      assert.equal(res.status, 409);
      assert.equal(res.json.reason, 'claude_unavailable');
      assert.equal(res.json.error, 'the claude command line is not installed for this window');
      assert.equal(sessionChecks, 0);
      assert.equal(calls.length, 0);
    },
  );
});

test('a Claude check that answers anything but true never starts Claude', async () => {
  // Like the session check it fails closed: a promise (the check must stay synchronous) or a stray
  // truthy value is not a yes.
  for (const answer of [undefined, null, false, 0, '', 'yes', 1, Promise.resolve(true)]) {
    await withServer({ claudeAvailable: () => answer }, async (port, calls) => {
      const res = await request(port);
      assert.equal(res.status, 409, String(answer));
      assert.equal(res.json.reason, 'claude_unavailable', String(answer));
      assert.equal(calls.length, 0, String(answer));
    });
  }
});

test('a start that fails without a reason is reported as start_failed', () =>
  withServer(
    {
      startClaude: async () => {
        throw new Error('claude is not installed');
      },
    },
    async (port) => {
      const res = await request(port);
      assert.equal(res.status, 500);
      // Fixed text: the error's own message is never sent back (see the next test).
      assert.deepEqual(res.json, {
        status: 'failed',
        reason: 'start_failed',
        error: 'the terminal could not be started',
        exit_code: null,
      });
    },
  ));

test('a start error never echoes its own text, which can carry the whole command line', async () => {
  // Starting a terminal can fail with a message that holds the command line, and the prompt is in it.
  const marker = 'ZZ-PROMPT-MARKER';
  const prompt = `rotate the production keys ${marker}`;
  const withCode = (code, message = prompt) => Object.assign(new Error(message), { code });
  const FAILED = 'the terminal could not be started';
  const cases = [
    [new Error(`Command failed: claude --session-id ${SID} -- ${prompt}`), FAILED],
    [withCode('ENOENT', `spawn ${prompt} ENOENT`), `${FAILED} (ENOENT)`],
    [withCode('ERR_INVALID_ARG_TYPE'), `${FAILED} (ERR_INVALID_ARG_TYPE)`],
    // Only a short upper-case code is shown: anything else could be text.
    [withCode('enoent'), FAILED],
    [withCode(prompt), FAILED],
    [withCode('E'.repeat(33)), FAILED],
    [withCode(42), FAILED],
    [Object.assign(new Error(prompt), { reason: 'exited', exitCode: 3 }), 'claude exited right after starting'],
    [Object.assign(new Error(prompt), { reason: 'exited', code: 'ENOENT' }), 'claude exited right after starting'],
    // The reason is only ever a short slug too: anything else could be text.
    [Object.assign(new Error(prompt), { reason: prompt }), FAILED],
    [Object.assign(new Error(prompt), { reason: 'Not A Slug' }), FAILED],
    // Not an error object at all.
    [prompt, FAILED],
    [null, FAILED],
  ];
  for (const [thrown, error] of cases) {
    await withServer(
      {
        startClaude: async () => {
          throw thrown;
        },
      },
      async (port) => {
        const res = await request(port);
        const label = String(thrown);
        assert.equal(res.status, 500, label);
        assert.equal(res.json.status, 'failed', label);
        assert.equal(res.json.error, error, label);
        // Only the bridge's own short slug is ever a reason; any other text becomes start_failed.
        assert.equal(res.json.reason, thrown && thrown.reason === 'exited' ? 'exited' : 'start_failed', label);
        assert.ok(!JSON.stringify(res.json).includes(marker), `${label} leaked into ${JSON.stringify(res.json)}`);
      },
    );
  }
});

// Like request(), but asks for a kept-alive connection and also returns the response headers.
function keepAliveRequest(port, { token = TOKEN } = {}) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body()));
    const headers = { connection: 'keep-alive', 'content-length': data.length, authorization: `Bearer ${token}` };
    const req = http.request({ host: '127.0.0.1', port, method: 'POST', path: '/start-claude', headers, agent: false }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () =>
        resolve({ status: res.statusCode, headers: res.headers, json: JSON.parse(Buffer.concat(chunks).toString() || '{}') }),
      );
    });
    req.on('error', reject);
    req.end(data);
  });
}

test('a body refused as too large closes the connection instead of being read', () =>
  withServer({}, async (port, calls) => {
    const { res, closed } = await new Promise((resolve, reject) => {
      const req = http.request(
        {
          host: '127.0.0.1',
          port,
          method: 'POST',
          path: '/start-claude',
          agent: false,
          // Keep-alive is asked for, so only the server's own Connection: close can end the connection.
          headers: { authorization: `Bearer ${TOKEN}`, connection: 'keep-alive', 'content-length': MAX_BODY_BYTES + 1 },
        },
        (res) => {
          res.resume();
          const socket = res.socket;
          resolve({
            res,
            closed: new Promise((done) => (socket.destroyed ? done(true) : socket.once('close', () => done(true)))),
          });
        },
      );
      req.on('error', reject);
      req.flushHeaders();
    });
    assert.equal(res.statusCode, 413);
    assert.equal(res.headers.connection, 'close');
    let timer;
    const gaveUp = new Promise((done) => {
      timer = setTimeout(() => done(false), 2000);
    });
    try {
      assert.equal(await Promise.race([closed, gaveUp]), true, 'the connection stayed open after the 413');
    } finally {
      clearTimeout(timer);
    }
    assert.equal(calls.length, 0);
  }));

test('a refusal made before the body is read keeps the connection, so the client can finish and read it', () =>
  // Only a body that will never be used (a 413) is cut off. The service sends its whole body before it
  // reads the reply, so closing on any other early refusal could reset the connection and make a
  // refusal look like an unknown outcome.
  withServer({}, async (port) => {
    const res = await keepAliveRequest(port, { token: 'x'.repeat(40) });
    assert.equal(res.status, 401);
    assert.equal(res.headers.connection, 'keep-alive');
  }));

test('a slow client is cut off: the header timeout is 5 s and the request timeout is 10 s', () => {
  const server = createBridgeServer({});
  assert.equal(server.headersTimeout, 5000);
  assert.equal(server.requestTimeout, 10000);
  // Node enforces both only when it checks its connections, every 30 s unless told otherwise: a client
  // that stalls its headers was measured staying connected for 31.7 s with the two timeouts alone, and
  // for 5.0 s with the check run every second.
  assert.equal(server.connectionsCheckingInterval, 1000);
});

// --- The bridge's own audit log (C7): an intent before any start, the result after, refusals, and
// --- unauthorized calls. The prompt text and the token are never written.

test('the intent is on record before Claude starts, and the result after', () => {
  const events = [];
  return withServer(
    {
      audit: (record) => events.push(['audit', record.event]),
      startClaude: async () => {
        events.push(['start']);
        return { terminalPid: 4242 };
      },
    },
    async (port) => {
      assert.equal((await request(port)).status, 200);
      assert.deepEqual(events, [['audit', 'start_intent'], ['start'], ['audit', 'start_result']]);
    },
  );
});

test('the intent says what is about to start, by ids, folder, label, and prompt length, and the result what happened', () => {
  const records = [];
  return withServer({ audit: (record) => records.push(record) }, async (port) => {
    await request(port);
    // Characters, not UTF-16 units: the service's prompt_chars counts the same way.
    const emoji = String.fromCodePoint(0x1f600);
    const second = 'req-0002-wxyz';
    await request(port, {
      payload: { ...body(), request_id: second, session_id: core.sessionIdFor(second), prompt: `fix ${emoji.repeat(3)}` },
    });
    assert.deepEqual(records, [
      { instance: INSTANCE, event: 'start_intent', request_id: RID, session_id: SID, cwd: FOLDER, label: 'hands repo-a req-00', prompt_chars: 6 },
      { instance: INSTANCE, event: 'start_result', request_id: RID, session_id: SID, status: 'started', terminal_pid: 4242 },
      {
        instance: INSTANCE, event: 'start_intent', request_id: second, session_id: core.sessionIdFor(second), cwd: FOLDER, label: 'hands repo-a req-00', prompt_chars: 7,
      },
      { instance: INSTANCE, event: 'start_result', request_id: second, session_id: core.sessionIdFor(second), status: 'started', terminal_pid: 4242 },
    ]);
  });
});

test('a start whose terminal pid is unknown is audited with a null pid', () => {
  const records = [];
  return withServer(
    { audit: (record) => records.push(record), startClaude: async () => ({}) },
    async (port) => {
      assert.equal((await request(port)).status, 200);
      assert.deepEqual(records[1], { instance: INSTANCE, event: 'start_result', request_id: RID, session_id: SID, status: 'started', terminal_pid: null });
    },
  );
});

test('a failed start is audited with its reason, and only ever a short slug', async () => {
  const marker = 'ZZ-PROMPT-MARKER';
  const prompt = `rotate the production keys ${marker}`;
  const cases = [
    [Object.assign(new Error('claude exited right after starting'), { reason: 'exited', exitCode: 1 }), 'exited'],
    [Object.assign(new Error('x'), { reason: 'no_terminal' }), 'no_terminal'],
    [new Error(`spawn ${prompt} ENOENT`), 'start_failed'],
    [Object.assign(new Error(prompt), { code: 'ENOENT' }), 'start_failed'],
    [Object.assign(new Error(prompt), { reason: prompt }), 'start_failed'],
    [prompt, 'start_failed'],
    [null, 'start_failed'],
  ];
  for (const [thrown, reason] of cases) {
    const records = [];
    await withServer(
      {
        audit: (record) => records.push(record),
        startClaude: async () => {
          throw thrown;
        },
      },
      async (port) => {
        assert.equal((await request(port)).status, 500, String(thrown));
        assert.deepEqual(records[1], { instance: INSTANCE, event: 'start_result', request_id: RID, session_id: SID, status: 'failed', reason }, String(thrown));
        assert.ok(!JSON.stringify(records).includes(marker), `${String(thrown)} leaked into the audit log`);
      },
    );
  }
});

test('an audit log that cannot be written refuses the start with audit_failed, and nothing starts', () =>
  withServer({ audit: boom }, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 409);
    assert.deepEqual(res.json, {
      status: 'refused',
      reason: 'audit_failed',
      error: 'the bridge could not write its audit log',
    });
    assert.equal(calls.length, 0);
  }));

test('an audit that answers with a promise counts as failed, because the intent must be on record before the start', async () => {
  // The write cannot be awaited without an await between the session check and the start, which would let
  // a second request in; and a rejection must never surface as an unhandled one.
  // Made inside the audit call, so a rejection exists only once the server has the promise to handle.
  for (const answer of [() => Promise.resolve(), () => Promise.reject(new Error('late failure'))]) {
    await withServer({ audit: () => answer() }, async (port, calls) => {
      const res = await request(port);
      assert.equal(res.status, 409);
      assert.equal(res.json.reason, 'audit_failed');
      assert.equal(calls.length, 0);
    });
  }
});

test('an audit that fails only for the intent leaves later records best effort, and they still try', () => {
  const records = [];
  return withServer(
    {
      audit: (record) => {
        if (record.event === 'start_intent') throw new Error('disk full');
        records.push(record);
      },
    },
    async (port, calls) => {
      assert.equal((await request(port)).status, 409);
      assert.equal(calls.length, 0);
      // The refusal itself is audited even though the intent could not be.
      assert.deepEqual(records, [{ instance: INSTANCE, event: 'refused', reason: 'audit_failed', request_id: RID, session_id: SID }]);
    },
  );
});

test('a record that cannot be written after the decision never changes the reply', () =>
  withServer(
    {
      audit: (record) => {
        if (record.event !== 'start_intent') throw new Error('disk full');
      },
    },
    async (port) => {
      const started = await request(port); // the result record fails
      assert.equal(started.status, 200);
      assert.deepEqual(started.json, { status: 'started', session_id: SID, terminal_pid: 4242 });
    },
  ));

test('refusals and unauthorized calls that cannot be audited are still answered as before', async () => {
  const sends = [
    [(port) => request(port, { token: 'x'.repeat(40) }), 401, 'unauthorized'],
    [(port) => request(port, { payload: '{nope' }), 400, 'bad_request'],
  ];
  for (const [send, status, reason] of sends) {
    await withServer({ audit: boom }, async (port, calls) => {
      const res = await send(port);
      assert.equal(res.status, status);
      assert.equal(res.json.reason, reason);
      assert.equal(calls.length, 0);
    });
  }
  await withServer({ audit: boom, killSwitchOn: () => true }, async (port) => {
    const res = await request(port);
    assert.equal(res.status, 403);
    assert.equal(res.json.reason, 'kill_switch');
  });
});

const IDS = { request_id: RID, session_id: SID };
const INTENT = { instance: INSTANCE, event: 'start_intent', request_id: RID, session_id: SID, cwd: FOLDER, label: 'hands repo-a req-00', prompt_chars: 6 };
const AUDITED = [
  { name: 'a wrong token', send: (port) => request(port, { token: 'x'.repeat(40) }), records: [{ instance: INSTANCE, event: 'unauthorized' }] },
  { name: 'a missing token', send: (port) => request(port, { token: null }), records: [{ instance: INSTANCE, event: 'unauthorized' }] },
  {
    name: 'an unreadable token file',
    overrides: { readToken: boom },
    records: [{ instance: INSTANCE, event: 'unauthorized' }],
  },
  { name: 'an oversized body', send: oversizedStart, records: [{ instance: INSTANCE, event: 'refused', reason: 'too_large' }] },
  { name: 'bad JSON', send: (port) => request(port, { payload: '{nope' }), records: [{ instance: INSTANCE, event: 'refused', reason: 'bad_request' }] },
  {
    name: 'a body that is not an object',
    send: (port) => request(port, { payload: '[1, 2]' }),
    records: [{ instance: INSTANCE, event: 'refused', reason: 'bad_request' }],
  },
  {
    name: 'a folder this window does not have: the ids parsed, so they are named',
    overrides: { folders: () => ['/home/user'] },
    records: [{ instance: INSTANCE, event: 'refused', reason: 'bad_request', ...IDS }],
  },
  {
    name: 'a session id that is not the request id\'s: the valid ids are named',
    send: (port) => request(port, { payload: { ...body(), session_id: OTHER_SID } }),
    records: [{ instance: INSTANCE, event: 'refused', reason: 'bad_request', request_id: RID, session_id: OTHER_SID }],
  },
  {
    name: 'a malformed request id: only the well-formed id is named',
    send: (port) => request(port, { payload: { ...body(), request_id: 'UPPER-case-01' } }),
    records: [{ instance: INSTANCE, event: 'refused', reason: 'bad_request', session_id: SID }],
  },
  { name: 'the kill switch', overrides: { killSwitchOn: () => true }, records: [{ instance: INSTANCE, event: 'refused', reason: 'kill_switch', ...IDS }] },
  { name: 'a missing Claude', overrides: { claudeAvailable: () => false }, records: [{ instance: INSTANCE, event: 'refused', reason: 'claude_unavailable', ...IDS }] },
  { name: 'an existing session', overrides: { sessionState: () => 'exists' }, records: [{ instance: INSTANCE, event: 'refused', reason: 'duplicate_session', ...IDS }] },
  {
    name: 'a session that cannot be checked',
    overrides: { sessionState: () => 'unknown' },
    records: [{ instance: INSTANCE, event: 'refused', reason: 'session_check_failed', ...IDS }],
  },
  {
    name: 'a session id another window has reserved: the intent was on record, then the refusal',
    overrides: { reserveSession: () => false },
    records: [INTENT, { instance: INSTANCE, event: 'refused', reason: 'duplicate_session', ...IDS }],
  },
  {
    name: 'a reservation that cannot be made: the intent, then the refusal',
    overrides: { reserveSession: boom },
    records: [INTENT, { instance: INSTANCE, event: 'refused', reason: 'reservation_failed', ...IDS }],
  },
  {
    name: 'a folder outside the allowed roots: no trust records, only the refusal',
    overrides: { trustState: () => OUTSIDE },
    records: [{ instance: INSTANCE, event: 'refused', reason: 'untrusted_folder', ...IDS }],
  },
  {
    name: 'a seeding that fails: the trust intent, then the refusal',
    overrides: { trustState: () => SEEDABLE, seedTrust: boom },
    records: [
      { instance: INSTANCE, event: 'trust_intent', ...IDS, cwd: FOLDER, key: SEEDABLE_KEY, flags: ALL_FLAGS },
      { instance: INSTANCE, event: 'refused', reason: 'trust_failed', ...IDS },
    ],
  },
  // Before the token check nothing is known about the caller, and scanners and typos would only add noise.
  { name: 'GET on the start route', send: (port) => request(port, { method: 'GET', payload: undefined }), records: [] },
  { name: 'an unknown path', send: (port) => request(port, { path: '/run', payload: undefined }), records: [] },
  { name: 'POST on ping', send: (port) => request(port, { path: '/ping', payload: undefined }), records: [] },
];

test('refusals after the token check and unauthorized calls are audited, and nothing before the token check is', async () => {
  for (const { name, overrides = {}, send = (port) => request(port), records: expected } of AUDITED) {
    const records = [];
    await withServer({ ...overrides, audit: (record) => records.push(record) }, async (port, calls) => {
      await send(port);
      assert.deepEqual(records, expected, name);
      assert.equal(calls.length, 0, name);
    });
  }
});

test('the audit log never holds the prompt or the token, whatever happened', async () => {
  const marker = 'ZZ-PROMPT-MARKER';
  const prompt = `rotate the production keys ${marker}`;
  const wrongToken = `WRONG-TOKEN-${'p'.repeat(31)}`;
  const records = [];
  const audit = (record) => records.push(record);
  const withPrompt = (extra = {}) => ({ payload: { ...body(), prompt, ...extra } });
  const runs = [
    {},
    { send: (port) => request(port, withPrompt()) },
    { send: (port) => request(port, { token: wrongToken, ...withPrompt() }) },
    { send: (port) => request(port, withPrompt({ prompt: `${prompt} \${env:HOME}` })) }, // refused for its variable syntax
    { send: (port) => request(port, withPrompt({ prompt: `/${prompt}` })) }, // refused for its first character
    { send: (port) => request(port, withPrompt()), overrides: { killSwitchOn: () => true } },
    { send: (port) => request(port, withPrompt()), overrides: { sessionState: () => 'exists' } },
    {
      send: (port) => request(port, withPrompt()),
      overrides: {
        startClaude: async () => {
          throw Object.assign(new Error(`spawn ${prompt} ENOENT`), { code: 'ENOENT', reason: prompt });
        },
      },
    },
  ];
  for (const { overrides = {}, send = (port) => request(port, withPrompt()) } of runs) {
    await withServer({ ...overrides, audit }, async (port) => {
      await send(port);
    });
  }
  const text = JSON.stringify(records);
  assert.ok(records.length >= runs.length, `only ${records.length} records for ${runs.length} runs`);
  assert.ok(!text.includes(marker), 'the prompt text is in the audit log');
  assert.ok(!text.includes(TOKEN), 'the token is in the audit log');
  assert.ok(!text.includes(wrongToken), 'a presented token is in the audit log');
});

// --- The reservation of the session id (C6), made by the server between the audit intent and the start,
// --- with no await anywhere from the session check to the call of startClaude.

test('a reservation that is made lets the start go on, once, for that session id', () => {
  const reserved = [];
  return withServer(
    {
      reserveSession: (id) => {
        reserved.push(id);
        return true;
      },
    },
    async (port, calls) => {
      assert.equal((await request(port)).status, 200);
      assert.deepEqual(reserved, [SID]);
      assert.equal(calls.length, 1);
    },
  );
});

test('a session id another window has reserved is refused as duplicate_session, and nothing starts', () =>
  withServer({ reserveSession: () => false }, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 409);
    assert.deepEqual(res.json, { status: 'refused', reason: 'duplicate_session', error: 'that session id is already reserved' });
    assert.equal(calls.length, 0);
  }));

test('a reservation that cannot be made is refused as reservation_failed with the error code, and nothing starts', () =>
  withServer(
    {
      reserveSession: () => {
        throw Object.assign(new Error('EACCES: permission denied, open /mnt/c/Users/user/AppData/Local/remote-hands/bridge-sessions/x'), { code: 'EACCES' });
      },
    },
    async (port, calls) => {
      const res = await request(port);
      assert.equal(res.status, 409);
      assert.deepEqual(res.json, {
        status: 'refused',
        reason: 'reservation_failed',
        error: 'the bridge could not reserve that session id (EACCES)',
      });
      assert.equal(calls.length, 0);
    },
  ));

test('the text of a failed reservation is fixed: only a short upper-case code is added, never the error\'s own words', async () => {
  const marker = 'ZZ-PROMPT-MARKER';
  const prompt = `rotate the production keys ${marker}`;
  const FIXED = 'the bridge could not reserve that session id';
  const withCode = (code, message = prompt) => Object.assign(new Error(message), { code });
  const cases = [
    [withCode('ENOSPC'), `${FIXED} (ENOSPC)`],
    [withCode('ERR_INVALID_ARG_TYPE'), `${FIXED} (ERR_INVALID_ARG_TYPE)`],
    [new Error(`open ${prompt} failed`), FIXED],
    [withCode('enoent'), FIXED],
    [withCode(prompt), FIXED],
    [withCode('E'.repeat(33)), FIXED],
    [withCode(42), FIXED],
    [prompt, FIXED], // thrown text, not an error
    [null, FIXED],
    [undefined, FIXED],
  ];
  for (const [thrown, error] of cases) {
    await withServer(
      {
        reserveSession: () => {
          throw thrown;
        },
      },
      async (port, calls) => {
        const res = await request(port);
        const label = String(thrown);
        assert.equal(res.status, 409, label);
        assert.equal(res.json.reason, 'reservation_failed', label);
        assert.equal(res.json.error, error, label);
        assert.ok(!JSON.stringify(res.json).includes(marker), `${label} leaked into ${JSON.stringify(res.json)}`);
        assert.equal(calls.length, 0, label);
      },
    );
  }
});

test('a reservation that answers with a promise or anything but a boolean counts as failed, and its rejection is not left unhandled', async () => {
  // The reservation cannot be awaited without an await between the check and the start, which would let a
  // second request in; so a promise (even one that resolves true) is not a yes, and a stray truthy value
  // is not either. Only an exact false means "already reserved".
  const answers = [
    undefined,
    null,
    0,
    1,
    '',
    'yes',
    'false',
    {},
    () => Promise.resolve(true),
    () => Promise.resolve(false),
    () => Promise.reject(Object.assign(new Error('late failure'), { code: 'EIO' })),
  ];
  for (const answer of answers) {
    await withServer({ reserveSession: () => (typeof answer === 'function' ? answer() : answer) }, async (port, calls) => {
      const res = await request(port);
      assert.equal(res.status, 409, String(answer));
      assert.equal(res.json.reason, 'reservation_failed', String(answer));
      assert.equal(res.json.error, 'the bridge could not reserve that session id', 'no code: it was not an error that was thrown');
      assert.equal(calls.length, 0, String(answer));
    });
  }
  await new Promise((resolve) => setImmediate(resolve)); // an unhandled rejection would surface by now
});

test('the order is: session check, intent, reservation, start, and then the result', () => {
  const events = [];
  return withServer(
    {
      sessionState: () => {
        events.push('session check');
        return 'absent';
      },
      audit: (record) => events.push(`audit ${record.event}`),
      reserveSession: () => {
        events.push('reserve');
        return true;
      },
      startClaude: async () => {
        events.push('start');
        return { terminalPid: 4242 };
      },
    },
    async (port) => {
      assert.equal((await request(port)).status, 200);
      assert.deepEqual(events, ['session check', 'audit start_intent', 'reserve', 'start', 'audit start_result']);
    },
  );
});

test('nothing is reserved for a request that is refused before the reservation', async () => {
  const refusals = [
    { killSwitchOn: () => true },
    { claudeAvailable: () => false },
    { sessionState: () => 'exists' },
    { sessionState: () => 'unknown' },
    { audit: boom },
    { folders: () => ['/home/user'] },
  ];
  for (const overrides of refusals) {
    let reservations = 0;
    await withServer(
      {
        ...overrides,
        reserveSession: () => {
          reservations += 1;
          return true;
        },
      },
      async (port) => {
        assert.equal((await request(port)).status === 200, false, JSON.stringify(Object.keys(overrides)));
        assert.equal(reservations, 0, JSON.stringify(Object.keys(overrides)));
      },
    );
  }
});

test('two windows asking for one session id at once: one starts, the other is refused as duplicate_session', async () => {
  // Stands in for the shared marker file: the first reservation of an id wins, the rest are told no.
  const taken = new Set();
  const reserveSession = (id) => (taken.has(id) ? false : taken.add(id) && true);
  const sessionState = (id) => (taken.has(id) ? 'exists' : 'absent');
  const slow = async (value) => {
    await new Promise((resolve) => setTimeout(resolve, 30));
    return { terminalPid: 1 };
  };
  const started = [];
  const make = (name) => ({ reserveSession, sessionState, startClaude: async (value) => { started.push(name); return slow(value); } });
  const servers = [];
  try {
    for (const name of ['window one', 'window two']) {
      const server = createBridgeServer({
        instance: INSTANCE, version: '0.1.0', readToken: () => TOKEN, killSwitchOn: () => false, claudeAvailable: () => true,
        audit: () => {}, folders: () => [FOLDER], trustState: () => ({ state: 'trusted', key: FOLDER }), ...make(name),
      });
      await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
      servers.push(server);
    }
    const replies = await Promise.all(servers.map((server) => request(server.address().port)));
    assert.deepEqual(replies.map((r) => r.status).sort(), [200, 409]);
    assert.equal(replies.find((r) => r.status === 409).json.reason, 'duplicate_session');
    assert.equal(started.length, 1, 'exactly one window started the session');
  } finally {
    for (const server of servers) {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    }
  }
});

test('every audit record names the window that wrote it', async () => {
  const records = [];
  const audit = (record) => records.push(record);
  const runs = [
    {},
    { send: (port) => request(port, { token: 'x'.repeat(40) }) },
    { send: (port) => request(port, { payload: '{nope' }) },
    { overrides: { killSwitchOn: () => true } },
    { overrides: { reserveSession: () => false } },
    { overrides: { reserveSession: boom } },
    { overrides: { startClaude: async () => { throw new Error('boom'); } } },
  ];
  for (const { overrides = {}, send = (port) => request(port) } of runs) {
    await withServer({ ...overrides, audit }, async (port) => {
      await send(port);
    });
  }
  const events = new Set(records.map((record) => record.event));
  for (const event of ['start_intent', 'start_result', 'refused', 'unauthorized']) {
    assert.ok(events.has(event), `no ${event} record was made`);
  }
  assert.ok(records.length >= runs.length);
  assert.deepEqual(records.filter((record) => record.instance !== INSTANCE), [], 'every record carries the instance id');
});

test('two windows sharing one log: each record says which window wrote it', async () => {
  const records = [];
  const taken = new Set();
  const common = {
    version: '0.1.0', readToken: () => TOKEN, killSwitchOn: () => false, claudeAvailable: () => true, folders: () => [FOLDER],
    trustState: () => ({ state: 'trusted', key: FOLDER }),
    audit: (record) => records.push(record),
    reserveSession: (id) => (taken.has(id) ? false : taken.add(id) && true),
    sessionState: () => 'absent', // both pass the check, as when the request reaches them in the same moment
    startClaude: async () => ({ terminalPid: 1 }),
  };
  const windows = [
    { instance: '11111111-1111-4111-8111-111111111111' },
    { instance: '22222222-2222-4222-8222-222222222222' },
  ];
  const servers = [];
  try {
    for (const window of windows) {
      const server = createBridgeServer({ ...common, ...window });
      await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
      servers.push(server);
    }
    assert.equal((await request(servers[0].address().port)).status, 200);
    assert.equal((await request(servers[1].address().port)).status, 409); // the same session id, now reserved
    assert.deepEqual(
      records.map((record) => [record.instance === windows[0].instance ? 'one' : 'two', record.event, record.reason ?? record.status ?? '']),
      [['one', 'start_intent', ''], ['one', 'start_result', 'started'], ['two', 'start_intent', ''], ['two', 'refused', 'duplicate_session']],
    );
  } finally {
    for (const server of servers) {
      server.closeAllConnections();
      await new Promise((resolve) => server.close(resolve));
    }
  }
});

// From the session check to the call of startClaude (the audit intent and the reservation are in between)
// nothing may yield to another request. The exclusive create carries C6 by itself, so this cannot let a
// session start twice; what the stretch keeps is a second request for the same session id from getting in
// between the check and the reservation and writing a second intent line before it is refused. That is a
// property of the source, so it is pinned by reading the source. Both ends are anchored, and the stretch
// must still hold the intent and the reservation, so that moving or renaming any of them fails here
// instead of leaving a test that scans nothing.
test('nothing yields between the session check and the call of startClaude: no await, timer, or promise chain', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'server.js'), 'utf8');
  const FROM = 'sessionState(sessionId)';
  const TO = 'await startClaude(';
  const times = (text) => source.split(text).length - 1;
  assert.equal(times(FROM), 1, `server.js must hold ${FROM} exactly once: the stretch starts there`);
  assert.equal(times(TO), 1, `server.js must hold ${TO} exactly once: the stretch ends there`);
  assert.ok(source.indexOf(FROM) < source.indexOf(TO), 'the session check comes before the start');
  const stretch = source.slice(source.indexOf(FROM), source.indexOf(TO));
  // Comments are not code (these ones talk about awaiting), so they are dropped before the scan.
  const code = stretch.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
  assert.ok(code.includes('audit(named('), 'the stretch must hold the audit intent');
  assert.ok(code.includes('reserveSession(sessionId)'), 'the stretch must hold the reservation');
  assert.ok(code.includes('attempt(killSwitchOn, true)'), 'the stretch must hold the kill switch check made just before the reservation');
  assert.ok(code.indexOf('attempt(killSwitchOn, true)') < code.indexOf('reserveSession(sessionId)'), 'and that check comes before the reservation');
  const yields = [...code.matchAll(/\bawait\b|\bsetImmediate\b|\bsetTimeout\b|\bsetInterval\b|\.then\(/g)].map((match) => match[0]);
  assert.deepEqual(yields, [], 'something here can let another request in before the reservation is made');
});

// --- Claude folder trust. Claude asks once per repository whether to trust it, and nobody
// --- can answer from the phone, so before a start the bridge makes sure Claude trusts the repo: inside the
// --- allowed roots it marks it trusted (after an intent on record), outside them it refuses.

test('a folder Claude does not trust yet, inside the allowed roots, is marked trusted after its intent is on record, and then starts', () => {
  const events = [];
  const records = [];
  return withServer(
    {
      trustState: (cwd) => {
        events.push(`trust check ${cwd}`);
        return SEEDABLE;
      },
      seedTrust: (key) => {
        events.push(`seed ${key}`);
        return SEEDED;
      },
      sessionState: () => {
        events.push('session check');
        return 'absent';
      },
      audit: (record) => {
        events.push(`audit ${record.event}`);
        records.push(record);
      },
      reserveSession: () => {
        events.push('reserve');
        return true;
      },
      startClaude: async () => {
        events.push('start');
        return { terminalPid: 4242 };
      },
    },
    async (port) => {
      const res = await request(port);
      assert.equal(res.status, 200);
      assert.deepEqual(res.json, { status: 'started', session_id: SID, terminal_pid: 4242 });
      assert.deepEqual(events, [
        `trust check ${FOLDER}`,
        'audit trust_intent',
        `seed ${SEEDABLE_KEY}`,
        'audit trust_seeded',
        'session check',
        'audit start_intent',
        'reserve',
        'start',
        'audit start_result',
      ]);
      assert.deepEqual(records.slice(0, 2), [
        { instance: INSTANCE, event: 'trust_intent', request_id: RID, session_id: SID, cwd: FOLDER, key: SEEDABLE_KEY, flags: ALL_FLAGS },
        { instance: INSTANCE, event: 'trust_seeded', request_id: RID, session_id: SID, key: SEEDABLE_KEY, result: 'seeded', flags: ALL_FLAGS },
      ]);
    },
  );
});

test('a folder Claude trusted in the meantime is recorded as already, and the start goes on', () => {
  const records = [];
  return withServer(
    { trustState: () => SEEDABLE, seedTrust: () => ({ result: 'already', flags: [] }), audit: (record) => records.push(record) },
    async (port, calls) => {
      assert.equal((await request(port)).status, 200);
      assert.deepEqual(records[1], { instance: INSTANCE, event: 'trust_seeded', request_id: RID, session_id: SID, key: SEEDABLE_KEY, result: 'already', flags: [] });
      assert.equal(calls.length, 1);
    },
  );
});

test('a trust_seeded record that cannot be written does not stop the start', () =>
  withServer(
    {
      trustState: () => SEEDABLE,
      audit: (record) => {
        if (record.event === 'trust_seeded') throw new Error('disk full');
      },
    },
    async (port, calls) => {
      assert.equal((await request(port)).status, 200);
      assert.equal(calls.length, 1);
    },
  ));

test('a seeding failure is refused as trust_failed with its code only, and nothing is checked, reserved, or started', async () => {
  const marker = 'ZZ-PATH-MARKER';
  const FIXED = 'the bridge could not record Claude\'s one-time answers for this folder';
  const withCode = (code) => Object.assign(new Error(`EACCES: open /home/user/${marker}/.claude.json`), { code });
  const cases = [
    [withCode('ELOCKED'), `${FIXED} (ELOCKED)`],
    [withCode('EBADCONFIG'), `${FIXED} (EBADCONFIG)`],
    [withCode('enoent'), FIXED],
    [withCode(marker), FIXED],
    [new Error(marker), FIXED],
    [marker, FIXED],
    [null, FIXED],
  ];
  for (const [thrown, error] of cases) {
    const records = [];
    let sessionChecks = 0;
    let reservations = 0;
    await withServer(
      {
        trustState: () => SEEDABLE,
        seedTrust: () => {
          throw thrown;
        },
        audit: (record) => records.push(record),
        sessionState: () => {
          sessionChecks += 1;
          return 'absent';
        },
        reserveSession: () => {
          reservations += 1;
          return true;
        },
      },
      async (port, calls) => {
        const res = await request(port);
        const label = String(thrown);
        assert.equal(res.status, 409, label);
        assert.deepEqual(res.json, { status: 'refused', reason: 'trust_failed', error }, label);
        assert.deepEqual(records.map((record) => record.event), ['trust_intent', 'refused'], label);
        assert.ok(!JSON.stringify([res.json, records]).includes(marker), `${label} leaked`);
        assert.equal(sessionChecks, 0, label);
        assert.equal(reservations, 0, label);
        assert.equal(calls.length, 0, label);
      },
    );
  }
});

test('a seeding that answers with a promise counts as failed, and its rejection is not left unhandled', async () => {
  for (const answer of [() => Promise.resolve(SEEDED), () => Promise.reject(Object.assign(new Error('late'), { code: 'EIO' }))]) {
    await withServer({ trustState: () => SEEDABLE, seedTrust: () => answer() }, async (port, calls) => {
      const res = await request(port);
      assert.equal(res.status, 409);
      assert.equal(res.json.reason, 'trust_failed');
      assert.equal(calls.length, 0);
    });
  }
  await new Promise((resolve) => setImmediate(resolve)); // an unhandled rejection would surface by now
});

test('a folder outside the allowed roots is refused as untrusted_folder, and nothing is seeded or started', () => {
  const seeded = [];
  return withServer({ trustState: () => OUTSIDE, seedTrust: (key) => seeded.push(key) }, async (port, calls) => {
    const res = await request(port);
    assert.equal(res.status, 409);
    assert.deepEqual(res.json, {
      status: 'refused',
      reason: 'untrusted_folder',
      error:
        'Claude has not been trusted in this folder: open Claude Code there once and accept the trust prompt, then retry with the same request id',
    });
    assert.deepEqual(seeded, []);
    assert.equal(calls.length, 0);
  });
});

test('a folder Claude already trusts writes no trust records and seeds nothing', () => {
  const records = [];
  const seeded = [];
  return withServer(
    { audit: (record) => records.push(record), seedTrust: (key) => seeded.push(key) },
    async (port) => {
      assert.equal((await request(port)).status, 200);
      assert.deepEqual(records.map((record) => record.event), ['start_intent', 'start_result']);
      assert.deepEqual(seeded, []);
    },
  );
});

test('a trust intent that cannot be written refuses with audit_failed, and nothing is seeded', () => {
  const seeded = [];
  return withServer(
    {
      trustState: () => SEEDABLE,
      seedTrust: (key) => seeded.push(key),
      audit: (record) => {
        if (record.event === 'trust_intent') throw new Error('disk full');
      },
    },
    async (port, calls) => {
      const res = await request(port);
      assert.equal(res.status, 409);
      assert.deepEqual(res.json, { status: 'refused', reason: 'audit_failed', error: 'the bridge could not write its audit log' });
      assert.deepEqual(seeded, []);
      assert.equal(calls.length, 0);
    },
  );
});

test('a trust intent audit that answers with a promise counts as failed, and nothing is seeded', async () => {
  const seeded = [];
  for (const answer of [() => Promise.resolve(), () => Promise.reject(new Error('late failure'))]) {
    await withServer(
      {
        trustState: () => SEEDABLE,
        seedTrust: (key) => seeded.push(key),
        audit: (record) => (record.event === 'trust_intent' ? answer() : undefined),
      },
      async (port, calls) => {
        const res = await request(port);
        assert.equal(res.status, 409);
        assert.equal(res.json.reason, 'audit_failed');
        assert.equal(calls.length, 0);
      },
    );
  }
  assert.deepEqual(seeded, []);
  await new Promise((resolve) => setImmediate(resolve));
});

test('the trust check gets the request\'s folder and nothing is asked of it for a request refused earlier', async () => {
  const asked = [];
  await withServer({ trustState: (cwd) => (asked.push(cwd), { state: 'trusted', key: FOLDER }) }, async (port) => {
    assert.equal((await request(port)).status, 200);
  });
  assert.deepEqual(asked, [FOLDER]);
  for (const overrides of [{ killSwitchOn: () => true }, { claudeAvailable: () => false }, { folders: () => ['/home/user'] }]) {
    const before = asked.length;
    await withServer({ ...overrides, trustState: (cwd) => (asked.push(cwd), OUTSIDE) }, async (port) => {
      await request(port);
    });
    assert.equal(asked.length, before, JSON.stringify(Object.keys(overrides)));
  }
});

// --- The kill switch, again just before the reservation (C8): a `hands off` written while the bridge seeds trust
// --- (up to a 2 s lock wait), checks the session, or writes the intent still stops the start, and nothing is
// --- reserved, so the same request id can run once Remote Hands is on again.

test('a kill switch set after the first check stops the start before the reservation: refused kill_switch, nothing reserved or started', async () => {
  const setters = {
    'while the trust is seeded': (off) => ({ trustState: () => SEEDABLE, seedTrust: () => ((off.on = true), SEEDED) }),
    'while the session id is checked': (off) => ({ sessionState: () => ((off.on = true), 'absent') }),
    'while the intent is written': (off) => ({ audit: (record) => (record.event === 'start_intent' ? (off.on = true) : undefined) }),
  };
  for (const [name, set] of Object.entries(setters)) {
    const off = { on: false };
    const records = [];
    let reservations = 0;
    const overrides = set(off);
    const audit = overrides.audit;
    await withServer(
      {
        ...overrides,
        killSwitchOn: () => off.on,
        audit: (record) => {
          if (audit) audit(record);
          records.push(record);
        },
        reserveSession: () => {
          reservations += 1;
          return true;
        },
      },
      async (port, calls) => {
        const res = await request(port);
        assert.equal(res.status, 403, name);
        assert.deepEqual(res.json, { status: 'refused', reason: 'kill_switch', error: 'Remote Hands is switched off' }, name);
        assert.equal(reservations, 0, name);
        assert.equal(calls.length, 0, name);
        const events = records.map((record) => (record.event === 'refused' ? `refused ${record.reason}` : record.event));
        assert.deepEqual(events.slice(-2), ['start_intent', 'refused kill_switch'], name);
      },
    );
  }
});

test('a kill switch that cannot be read at the second check counts as switched off', () => {
  let reads = 0;
  let reservations = 0;
  return withServer(
    {
      killSwitchOn: () => {
        reads += 1;
        if (reads > 1) throw new Error('EACCES');
        return false;
      },
      reserveSession: () => {
        reservations += 1;
        return true;
      },
    },
    async (port, calls) => {
      const res = await request(port);
      assert.equal(res.json.reason, 'kill_switch');
      assert.equal(reads, 2);
      assert.equal(reservations, 0);
      assert.equal(calls.length, 0);
    },
  );
});

// --- Claude's external CLAUDE.md imports dialog (imports approval follows trust). The audit
// --- records say which of Claude's one-time answers the bridge set.

test('a trusted folder whose imports are undecided: the intent and the result name the imports flags, and the start goes on', () => {
  const records = [];
  return withServer(
    {
      trustState: () => ({ state: 'seedable', key: SEEDABLE_KEY, flags: IMPORTS_FLAGS }),
      seedTrust: () => ({ result: 'seeded', flags: IMPORTS_FLAGS }),
      audit: (record) => records.push(record),
    },
    async (port, calls) => {
      assert.equal((await request(port)).status, 200);
      assert.deepEqual(records.slice(0, 2), [
        { instance: INSTANCE, event: 'trust_intent', request_id: RID, session_id: SID, cwd: FOLDER, key: SEEDABLE_KEY, flags: IMPORTS_FLAGS },
        { instance: INSTANCE, event: 'trust_seeded', request_id: RID, session_id: SID, key: SEEDABLE_KEY, result: 'seeded', flags: IMPORTS_FLAGS },
      ]);
      assert.equal(calls.length, 1);
    },
  );
});

test('only the names of Claude\'s three flags ever reach the audit log as flags', () => {
  const marker = 'ZZ-FLAG-MARKER';
  const records = [];
  return withServer(
    {
      trustState: () => ({ state: 'seedable', key: SEEDABLE_KEY, flags: [marker, 'hasTrustDialogAccepted', 42] }),
      seedTrust: () => ({ result: 'seeded', flags: ['hasClaudeMdExternalIncludesApproved', marker, 'hasClaudeMdExternalIncludesApproved'] }),
      audit: (record) => records.push(record),
    },
    async (port) => {
      assert.equal((await request(port)).status, 200);
      assert.deepEqual(records[0].flags, ['hasTrustDialogAccepted']);
      assert.deepEqual(records[1].flags, ['hasClaudeMdExternalIncludesApproved']);
      assert.ok(!JSON.stringify(records).includes(marker));
    },
  );
});
