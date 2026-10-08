'use strict';
// The window bridge's HTTP endpoint: 127.0.0.1 only (rule C1), two fixed routes (rule C2).
const http = require('node:http');
const core = require('./core');

const MAX_BODY_BYTES = 256 * 1024;
// A request from the local service is a few KB, so a client that is slow to send one is cut off.
// Node enforces both timeouts only when it checks its connections, which is every 30 s unless told
// otherwise (a stalled client was measured staying 31.7 s with the two timeouts alone, 5.0 s with
// the check every second), so the check interval is set as well.
const HEADERS_TIMEOUT_MS = 5000;
const REQUEST_TIMEOUT_MS = 10000;
const CONNECTION_CHECK_MS = 1000;
const ERROR_CODE_RE = /^[A-Z0-9_]{1,32}$/;
const REASON_RE = /^[a-z_]{1,32}$/;

// Who wants to hear of a refusal made on a response: set once a request has passed the token check, so
// every refusal after it is audited without each of them having to remember to be.
const refusalHooks = new WeakMap();

function send(res, code, payload, headers = {}) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8');
  res.writeHead(code, { 'Content-Type': 'application/json', 'Content-Length': body.length, ...headers });
  res.end(body);
}

function refuse(res, code, reason, error, headers) {
  const hook = refusalHooks.get(res);
  if (hook) hook(reason);
  send(res, code, { status: 'refused', reason, error }, headers);
}

// A check that fails before anything has started must end as a refusal. If it threw into the
// catch-all below, the reply would be "failed", which the service reads as "may have started" and
// blocks the request id for good. The caller passes the answer that makes the request refuse.
function attempt(check, fallback) {
  try {
    return check();
  } catch {
    return fallback;
  }
}

// Fixed text only. An error from starting a terminal or reserving a session id can carry the whole
// command line, which holds the prompt (rule C3), or a path, so its own message is never sent back; a
// short system code is the one detail that is safe and useful.
function codeSuffix(err) {
  return typeof err.code === 'string' && ERROR_CODE_RE.test(err.code) ? ` (${err.code})` : '';
}

function startErrorText(err) {
  if (err.reason === 'exited') return 'claude exited right after starting';
  return `the terminal could not be started${codeSuffix(err)}`;
}

// An audit write must be synchronous, so a promise that comes back from one is never awaited; its
// failure is swallowed here rather than left to surface as an unhandled rejection.
function isPromise(value) {
  return value !== null && typeof value === 'object' && typeof value.then === 'function';
}

function swallowRejection(promise) {
  promise.then(undefined, () => {});
}

// A failed start's reason is a short slug of the bridge's own, never free text: 'exited' when the terminal's
// process ended at once, otherwise the generic 'start_failed'. It goes into the reply and the audit log, and
// an error can carry the whole command line.
function startReason(fault) {
  return typeof fault.reason === 'string' && REASON_RE.test(fault.reason) ? fault.reason : 'start_failed';
}

// The flags of Claude's that a trust record names: only the names the bridge itself sets (core.SEEDED_FLAGS),
// each once, so nothing else (free text, a huge value) reaches the audit log through them.
function flagNames(value) {
  return Array.isArray(value) ? core.SEEDED_FLAGS.filter((flag) => value.includes(flag)) : [];
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error('body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function createBridgeServer({
  instance,
  version,
  readToken,
  killSwitchOn,
  claudeAvailable,
  audit,
  reserveSession,
  folders,
  trustState,
  seedTrust,
  sessionState,
  startClaude,
  // The allowed roots for the working directory (core.allowedRoots); when not given, only the environment
  // and the default count.
  allowedRoots = () => core.allowedRoots(),
}) {
  // Every record names this window (its instance id), so a log that several windows share says which of them
  // saw a request, refused it, or started the session.
  const named = (record) => ({ instance, ...record });
  // The bridge's own audit log (C7). audit(record) must be synchronous and throw when the record cannot be
  // written. Only the intent before a start has to succeed; every other record is best effort, because a
  // reply must never depend on whether it could be logged.
  const note = (record) => {
    try {
      const written = audit(named(record));
      if (isPromise(written)) swallowRejection(written);
    } catch {
      // best effort
    }
  };
  // A record that must be on file before the bridge acts: throws when it cannot be written, or when the
  // write answers with a promise (it cannot be awaited, so it is not known to be written).
  const noteFirst = (record) => {
    const written = audit(named(record));
    if (isPromise(written)) {
      swallowRejection(written);
      throw new Error('audit must be synchronous');
    }
  };
  const options = {
    headersTimeout: HEADERS_TIMEOUT_MS,
    requestTimeout: REQUEST_TIMEOUT_MS,
    connectionsCheckingInterval: CONNECTION_CHECK_MS,
  };
  return http.createServer(options, async (req, res) => {
    try {
      if (req.url === '/ping') {
        // No token: /ping reveals only this window's instance id, which the service compares with
        // the registration before it sends the token.
        if (req.method !== 'GET') return refuse(res, 405, 'method', 'GET /ping only');
        return send(res, 200, { instance, version });
      }
      if (req.url !== '/start-claude') return refuse(res, 404, 'path', 'unknown path');
      if (req.method !== 'POST') return refuse(res, 405, 'method', 'POST /start-claude only');
      const header = req.headers.authorization || '';
      const presented = header.startsWith('Bearer ') ? header.slice(7) : '';
      let expected = '';
      try {
        expected = readToken();
      } catch {
        expected = '';
      }
      if (!core.tokenMatches(presented, expected)) {
        note({ event: 'unauthorized' }); // never the token that was presented
        return refuse(res, 401, 'unauthorized', 'bad or missing token');
      }
      // From here every refusal is audited, with the ids once the body has parsed.
      let ids = {};
      refusalHooks.set(res, (reason) => note({ event: 'refused', reason, ...ids }));
      const declared = Number(req.headers['content-length']);
      if (!Number.isInteger(declared) || declared < 1 || declared > MAX_BODY_BYTES) {
        // Close the connection: this body will not be used, and Node would go on reading it.
        return refuse(res, 413, 'too_large', `body must be 1 to ${MAX_BODY_BYTES} bytes`, { Connection: 'close' });
      }
      let body;
      try {
        body = JSON.parse((await readBody(req, MAX_BODY_BYTES)).toString('utf8'));
      } catch {
        return refuse(res, 400, 'bad_request', 'body is not JSON');
      }
      ids = core.auditIds(body);
      // Folders that cannot be read count as no folders, and roots that cannot be read as no roots, so
      // validateStart refuses the cwd.
      const checked = core.validateStart(body, attempt(folders, []), attempt(allowedRoots, []));
      if (checked.error) return refuse(res, 400, 'bad_request', checked.error);
      // A kill switch that cannot be read counts as switched off.
      if (attempt(killSwitchOn, true)) return refuse(res, 403, 'kill_switch', 'Remote Hands is switched off');
      // Claude must be there before anything is reserved: a missing command line is a refusal the service
      // can retry once it is installed, not a failed start that would burn the session id and the request
      // id. Only an exact true counts (a check that throws, or answers with a promise, is a no).
      if (attempt(claudeAvailable, false) !== true) {
        return refuse(res, 409, 'claude_unavailable', 'the claude command line is not installed for this window');
      }
      const { requestId, sessionId, cwd, label, prompt } = checked.value;
      // Claude asks once per repository whether to trust it, and whether its CLAUDE.md may import files outside
      // it, and nobody is at the keyboard to answer either, so both must be settled before Claude starts there.
      // trustState(cwd) answers 'trusted' (nothing to write), 'seedable' (something
      // to set, with the flags it would set, and the repo is inside the allowed roots), or 'outside', with the key
      // Claude keeps the answers under; anything else, or a check that throws, counts as outside. A seedable repo
      // is seeded with seedTrust(key), only once its intent is on record.
      let trust = attempt(() => trustState(cwd), null);
      if (isPromise(trust)) {
        swallowRejection(trust); // it cannot be awaited here, so it is no answer
        trust = null;
      }
      const trustKey = trust !== null && typeof trust === 'object' ? trust.key : undefined;
      if (trust?.state !== 'trusted') {
        if (trust?.state !== 'seedable' || typeof trustKey !== 'string' || trustKey === '') {
          return refuse(
            res,
            409,
            'untrusted_folder',
            'Claude has not been trusted in this folder: open Claude Code there once and accept the trust prompt, then retry with the same request id',
          );
        }
        try {
          noteFirst({ event: 'trust_intent', request_id: requestId, session_id: sessionId, cwd, key: trustKey, flags: flagNames(trust.flags) });
        } catch {
          return refuse(res, 409, 'audit_failed', 'the bridge could not write its audit log');
        }
        let seeded;
        try {
          seeded = seedTrust(trustKey);
          if (isPromise(seeded)) {
            swallowRejection(seeded);
            seeded = undefined;
          }
        } catch (err) {
          const fault = err !== null && typeof err === 'object' ? err : {};
          return refuse(res, 409, 'trust_failed', `the bridge could not record Claude's one-time answers for this folder${codeSuffix(fault)}`);
        }
        const result = seeded !== null && typeof seeded === 'object' ? seeded.result : undefined;
        if (result !== 'seeded' && result !== 'already') {
          return refuse(res, 409, 'trust_failed', "the bridge could not record Claude's one-time answers for this folder");
        }
        note({ event: 'trust_seeded', request_id: requestId, session_id: sessionId, key: trustKey, result, flags: flagNames(seeded.flags) });
      }
      // No await between this check and startClaude (nor in the audit write below): the window's
      // reservation of the session id happens before any other request can run.
      const state = attempt(() => sessionState(sessionId), 'unknown');
      if (state === 'exists') return refuse(res, 409, 'duplicate_session', 'that session id already exists');
      if (state !== 'absent') {
        return refuse(res, 409, 'session_check_failed', 'could not check whether that session id is unused');
      }
      // The intent is on record before anything starts, so a call that skipped the service still leaves a
      // trace; if it cannot be written, nothing starts. Never the prompt, only its length.
      try {
        const written = audit(named({
          event: 'start_intent',
          request_id: requestId,
          session_id: sessionId,
          cwd,
          label,
          prompt_chars: Array.from(prompt).length,
        }));
        if (isPromise(written)) {
          swallowRejection(written);
          throw new Error('audit must be synchronous');
        }
      } catch {
        return refuse(res, 409, 'audit_failed', 'the bridge could not write its audit log');
      }
      // The kill switch again (C8): `hands off` may have landed since the first check, while the trust was seeded
      // (up to a 2 s wait for Claude's lock), the session id was checked, or the intent was written. This is the
      // last moment a refusal leaves nothing behind: after the reservation it would use up the session id with
      // nothing started. Read the same way as the first check, so one that cannot be read counts as off.
      if (attempt(killSwitchOn, true)) return refuse(res, 403, 'kill_switch', 'Remote Hands is switched off');
      // The reservation of the session id (C6). The exclusive create behind it is what guarantees that a
      // start happens at most once, however many windows are asked. It is made right after the intent and
      // right before the start, and nothing from the session check to the call of startClaude may yield to
      // another request: that is what keeps a second request for the same session id from getting in
      // between the check and this reservation and writing a second intent line before it is refused (a
      // test reads this stretch of the source to pin that). Only an exact true goes on; an exact false is a
      // session id some window has already reserved; a failure, a promise (it cannot be awaited here), or
      // any other answer means nothing was reserved, so it is refused as nothing sent and can be retried.
      let reserved;
      try {
        reserved = reserveSession(sessionId);
        if (isPromise(reserved)) {
          swallowRejection(reserved);
          reserved = undefined;
        }
      } catch (err) {
        const fault = err !== null && typeof err === 'object' ? err : {};
        return refuse(res, 409, 'reservation_failed', `the bridge could not reserve that session id${codeSuffix(fault)}`);
      }
      if (reserved === false) return refuse(res, 409, 'duplicate_session', 'that session id is already reserved');
      if (reserved !== true) return refuse(res, 409, 'reservation_failed', 'the bridge could not reserve that session id');
      let started;
      try {
        started = await startClaude(checked.value);
      } catch (err) {
        const fault = err !== null && typeof err === 'object' ? err : {};
        const reason = startReason(fault);
        note({ event: 'start_result', request_id: requestId, session_id: sessionId, status: 'failed', reason });
        return send(res, 500, {
          status: 'failed',
          reason,
          error: startErrorText(fault),
          exit_code: fault.exitCode ?? null,
        });
      }
      const terminalPid = Number.isInteger(started?.terminalPid) ? started.terminalPid : null;
      note({ event: 'start_result', request_id: requestId, session_id: sessionId, status: 'started', terminal_pid: terminalPid });
      return send(res, 200, { status: 'started', session_id: sessionId, terminal_pid: terminalPid });
    } catch (err) {
      if (!res.headersSent) send(res, 500, { status: 'failed', reason: 'internal', error: String(err) });
    }
  });
}

module.exports = { createBridgeServer, MAX_BODY_BYTES };
