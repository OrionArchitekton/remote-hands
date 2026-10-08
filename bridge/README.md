# Remote Hands window bridge

A small extension that runs inside each Cursor WSL window. It lets the local Remote Hands
service start a Claude session in this window's terminal, and nothing else.

- Listens on `127.0.0.1` only, on a free port, and registers the port and this window's
  folders in `%LOCALAPPDATA%\remote-hands\windows\`.
- Two routes: `GET /ping` (no secret, returns this window's instance id) and
  `POST /start-claude` (token required). There is no route that runs an arbitrary command.
- Starts the Claude command line directly, with the prompt as an argument after `--`;
  no shell is involved.
- Honours the Remote Hands kill switch, accepts only the session id derived from the request id
  (so one request can start at most one session), and refuses a session id that already exists
  or that any window has already reserved (an exclusive marker file per session id in
  `%LOCALAPPDATA%\remote-hands\bridge-sessions\`, shared by every window, in every WSL
  distribution).
- Makes sure Claude trusts the repo before a start: if Claude does not, and the repo (for a
  genuine worktree, its main repository, worked out exactly as Claude does) is inside the
  allowed roots, it marks it trusted in the config Claude uses (normally `~/.claude.json`) under
  Claude's own lock, after an audit record; otherwise it refuses with `untrusted_folder` and
  starts nothing. A repo Claude already trusts is never refused. Claude's other one-time
  question, whether CLAUDE.md files may import files from outside the repo, is approved together
  with the trust inside the allowed roots, unless the user already declined it.
- Keeps its own audit log, `%LOCALAPPDATA%\remote-hands\bridge-audit\<window instance>.jsonl`
  (one file per window): an intent line is written before any start (if it cannot be written,
  nothing starts), then the result; refusals are logged too. The prompt text and the token are
  never written.

The allowed roots are read, in order, from `REMOTE_HANDS_ALLOWED_ROOTS`, from `allowed_roots` in
`config.json` in the Remote Hands folder, or else `~/src/` and `~/.worktrees/` in WSL; the service
reads the same settings. The Remote Hands folder is `REMOTE_HANDS_HOME` when set, otherwise the
Windows `%LOCALAPPDATA%\remote-hands`, found from WSL. No VS Code setting can change either, so a
repository's workspace settings cannot redirect the bridge.

See the top-level README and SECURITY.md for the whole system and its safety rules.
