# Remote Hands

Remote Hands lets an agent on a Windows PC start a real Claude Code session in a Cursor window
without touching the keyboard. An agent (or a script) running in WSL calls the `hands` CLI; a
small Windows service checks the request and asks a Cursor extension to open a terminal that runs
`claude` with the prompt as an argument. The `hands` CLI then reads the session's first reply
back from Claude's own session record.

Nothing is typed and nothing is clicked, so it works while the PC is locked.

**Status: experimental.** It starts sessions only. Driving a session that is already running
(sending it a second prompt, answering its questions) is not built.

## How it works, hop by hop

```
agent in WSL
  -> hands start-claude          (WSL; checks the prompt, the request id, the repo)
  -> POST http://127.0.0.1:47821/action   bearer token
  -> remote-hands-service        (Windows, scheduled task at logon)
       checks: token, kill switch, allowed roots, prompt rules, one submit per request id
       finds the Cursor window that has the repo open, or opens one
  -> POST http://127.0.0.1:<window port>/start-claude   same bearer token
  -> remote-hands-bridge         (Cursor extension in that WSL window)
       checks everything again, reserves the session id, writes its audit line
  -> new terminal: claude --session-id <uuid5> --remote-control <label> --name <label> -- <prompt>
  <- hands polls ~/.claude/projects/*/<session id>.jsonl for the first reply
```

1. **`hands` CLI (WSL).** Reads the prompt from a file, checks it, and POSTs one action to the
   service on `127.0.0.1` with the token from the Remote Hands folder. It prints one JSON result
   and exits 0 only when the status is `ok`.
2. **Service (Windows).** A Python HTTP server bound to `127.0.0.1` only, with exclusive use of
   its port. It accepts `POST /action` with a valid bearer token and runs only actions from a
   fixed table (`health`, `start_claude`, and the `probe_input` diagnostic). Every authorized
   action is written to the audit log before it runs.
3. **Window lookup.** Each Cursor WSL window's bridge registers its port and folders in the
   Remote Hands folder. The service pings the registered port and accepts the window only if it
   answers with the same instance id. If no window has the repo open, the service starts Cursor
   with that folder (only while the PC is locked or idle) and waits up to 90 s for its bridge.
4. **Bridge (Cursor, WSL).** Re-validates the request, refuses if the kill switch is on, makes
   sure Claude trusts the folder (see SECURITY.md), reserves the session id with an exclusive
   file, and opens a terminal whose process is the `claude` binary itself (no shell).
5. **Session id.** The session id is a UUIDv5 of the request id, computed the same way on both
   sides, so a request id can never start two sessions and the caller knows the id up front.
6. **Read-back.** `hands` polls Claude's session record in WSL until the first reply lands, then
   returns it (up to 4,000 characters). The reply is untrusted text: treat it as data.

## Safety rules

- **Allowed roots.** A session starts only in a repo inside an allowed root (by default `~/src/`
  and `~/.worktrees/` in WSL). The service and the bridge both check.
- **Prompts are data, never shell.** The prompt is one argument after `--`. No shell ever parses
  it. Prompts that Claude would read as something else are refused: a leading `/`, `!`, `#`, or
  `-`, a single word, `${`, and control or invisible characters.
- **Kill switch.** `hands off` writes an `OFF` file that the service and every bridge check
  before acting; a switch that cannot be read counts as on. `hands on` clears it.
- **One submit per request id.** A journal entry per request id records whether the prompt was
  sent. A retry with the same id replays a finished start, or runs one that provably sent
  nothing; an id whose outcome is unknown is never sent again.
- **Loopback only, bearer token.** The service and the bridges listen on `127.0.0.1` and require
  the token (constant-time compare). Nothing listens on a network interface.
- **Never types into your windows.** Starting a session sends no keystrokes. A new terminal is
  shown only in a window that is not focused, without taking focus. A new Cursor window is
  opened only while Windows reports the session locked or you have been idle for 10 s.
- **Locked PC.** Starting a session works while the PC is locked. `hands health` reports the
  lock state and idle time.
- **Audit.** The service and each bridge append JSON lines to audit logs in the Remote Hands
  folder. Neither the prompt text nor the token is ever written.

SECURITY.md has the threat model and the numbered rules the code comments cite (rule C1 to C10).

## Requirements

- Windows 10 or 11 with WSL 2 in **mirrored networking mode** (`networkingMode=mirrored` in
  `%USERPROFILE%\.wslconfig`), so WSL and Windows share `127.0.0.1`.
- Cursor installed for the current user (`%LOCALAPPDATA%\Programs\cursor\Cursor.exe`), with WSL
  windows working.
- Claude Code installed in WSL at `~/.local/bin/claude` and signed in, and Cursor's WSL server
  installed (open any WSL folder in Cursor once).
- Python 3.12 for Windows (a per-user python.org install by default) and, in WSL,
  [uv](https://docs.astral.sh/uv/), Node.js with npm (tested with Node 24).

## Install

From a clone inside WSL:

```bash
uv sync
uv run pytest -q && (cd bridge && npm test)

# Build the wheel, write config.json, install the service as the "RemoteHands" logon task.
scripts/install-windows.sh

# Build and install the Cursor extension into Cursor's WSL server.
scripts/install-bridge.sh

uv run hands health
```

New WSL windows load the bridge at start; an open window loads it after
**Developer: Reload Window**.

Uninstall: run `scripts\uninstall-windows.ps1` in Windows PowerShell (it removes the task and
its venv; delete `%LOCALAPPDATA%\remote-hands` to remove the token and logs too), and run
`~/.cursor-server/bin/<version>/bin/cursor-server --uninstall-extension remote-hands.remote-hands-bridge`
in WSL.

## Configuration

The Remote Hands folder is `%LOCALAPPDATA%\remote-hands` on Windows. From WSL it is found through
`LOCALAPPDATA` (if passed in with `WSLENV=LOCALAPPDATA/p`) or by asking `cmd.exe` and `wslpath`.

| Setting | Read by | Default |
| --- | --- | --- |
| `REMOTE_HANDS_HOME` | all | `%LOCALAPPDATA%\remote-hands` |
| `config.json` `allowed_roots` | all | none (written by the installer) |
| `REMOTE_HANDS_ALLOWED_ROOTS` (`:`-separated, overrides `config.json`) | all | `~/src/`, `~/.worktrees/` in WSL |
| `--port` or `REMOTE_HANDS_PORT` | service | `47821` |
| `REMOTE_HANDS_URL` | `hands` | `http://127.0.0.1:47821/action` |
| `CLAUDE_PROJECTS_DIR` | `hands` read-back | `~/.claude/projects` |

`config.json` looks like `{"allowed_roots": ["/home/you/src/", "/home/you/.worktrees/"]}`. The
service runs on Windows and cannot see your WSL home, so it needs `config.json` (the installer
writes it from your WSL `$HOME`). A root must be an absolute, normalized path; `/` is refused.
Port 47821 is an arbitrary default; to change it, pass `-Port` to the PowerShell installer and
set `REMOTE_HANDS_URL` for `hands`.

## Usage

```bash
uv run hands health
```

Reports the version, whether the kill switch is on, whether the desktop is locked, idle seconds,
and the actions in flight.

```bash
printf '%s\n' 'Summarize the open TODOs in this repo.' > /tmp/prompt.txt
uv run hands start-claude --repo ~/src/my-repo --prompt-file /tmp/prompt.txt \
  --request-id todo-summary-0001 --wait 120
```

`--repo` must be a git repository inside an allowed root. Starts the session and waits up to
`--wait` seconds for the first reply. The result carries the
`session_id` and, under `reply`, the first reply. The request id (8 to 64 of `a-z 0-9 - _`)
belongs to this one request: reuse it on every retry, and a retry never starts a second
session. `--label` names the session (default `hands <repo> <id prefix>`). Give the command a
timeout of at least 150 + wait + 30 seconds.

```bash
uv run hands read-back --session <session id> --wait 30
```

Re-reads a session's prompt and first reply from its record. It does not follow later turns.

```bash
uv run hands off   # kill switch: refuse every action that types, clicks, or starts
uv run hands on    # clear it
```

`hands off` waits up to 15 s for running actions to finish and reports what it saw. Other
commands: `hands windows` lists the registered Cursor windows; `hands probe-input --delay N`
is a diagnostic that types a test word into Remote Hands' own small window (lock the PC during
the delay to see what a locked desktop allows).

A session started this way runs in a transient terminal: reloading or closing the window ends
it. Resume it with `claude --resume <session id>`.

## Development

```bash
uv run ruff format --check . && uv run ruff check . && uv run pytest -q
(cd bridge && npm test)
```

The Windows-only code (`win_desktop.py`, `win_input.py`) is covered by unit tests with fakes;
its real behavior can only be checked on a Windows desktop.

## License

MIT. See LICENSE.
