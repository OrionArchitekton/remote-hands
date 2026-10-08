# Security

Remote Hands starts Claude Code sessions on your PC on behalf of whoever holds its token. Read
this before you install it.

## Reporting a vulnerability

Please report vulnerabilities privately to the maintainer (GitHub private vulnerability
reporting on this repository), not in a public issue.

## Threat model

**What it protects against**

- Other machines. The service and every window bridge listen on `127.0.0.1` only, and the
  `hands` CLI refuses any URL that is not `http://127.0.0.1:<port>/action`. Nothing is exposed
  on a network interface, a VPN, or a tunnel.
- Callers without the token. Every action and every bridge start needs the bearer token, which
  is compared in constant time. A web page in a browser can send a request to loopback, but it
  cannot read the token, so it is refused.
- Prompt text turning into commands. The prompt is never put into a shell command line. The
  bridge starts the `claude` binary directly with the prompt as one argument after `--`, and
  refuses prompts Claude would read as a command, a shell escape, a memory note, an option, or a
  Cursor variable.
- Duplicate starts. One request id can start at most one session, even across retries, crashes,
  and several windows.
- Typing into your work. Starting a session sends no keystrokes; a new window is opened only
  while the session is locked or you are idle.

**What it does not protect against**

- **Any local process that can read the token can start sessions.** The token lives in
  `%LOCALAPPDATA%\remote-hands\token`, readable by your Windows user and, through the Windows
  drive mount, by your user (and root) in every WSL distribution. Such a process can start a
  Claude session in any repo inside the allowed roots with any prompt that passes the rules,
  and read back its first reply. Treat the token like a credential: do not copy it elsewhere,
  and delete the file to rotate it (the service makes a new one at its next start).
- **What the session may do is decided by your Claude configuration, not by Remote Hands.** The
  bridge passes no permission flags. The session runs with your Claude settings (permission
  mode, allowed tools, hooks, MCP servers) in WSL, as your user. **Do not run Claude in
  bypass-permissions mode (`--dangerously-skip-permissions` or
  `permissions.defaultMode: "bypassPermissions"`) for sessions started this way:** whoever can
  write a prompt would then control a shell on your machine without any approval step.
- **Folder trust is granted automatically inside the allowed roots.** Claude asks once per
  repository whether to trust it, and nobody is at the keyboard to answer. So, before a start,
  the bridge marks a repository inside the allowed roots as trusted in Claude's config, and
  approves its CLAUDE.md imports from outside the repo unless you declined them before. Trusting
  a repository lets its own Claude settings, hooks, and MCP servers run. Keep the allowed roots
  to folders that hold only code you trust; never point them at a downloads folder or at
  repositories you have not reviewed. A repository outside the allowed roots is never marked
  trusted (a start there is refused unless you already trusted it yourself).
- **Prompt injection.** A session reads whatever the repository and the prompt contain, and its
  reply is untrusted text. Callers must treat read-back output as data, never as instructions.
- **Remote Control.** Sessions are started with `--remote-control <label>`, so they appear in
  Claude's Remote Control for your account. Anyone with access to your Claude account can see
  and continue them there.
- **Windows-only checks.** Lock and idle detection use Windows APIs. They are unit-tested with
  fakes in this repository; real behavior depends on your Windows version.

## Safety rules

The code comments cite these rules by number.

- **C1 Local only.** The service and every window bridge accept requests only from the same
  machine (`127.0.0.1`) and only with the token. The service's port is bound exclusively, so
  another process cannot bind it and receive the token.
- **C2 Fixed actions.** The service runs only the actions in its fixed table; a bridge has two
  fixed routes. Neither offers a way to run an arbitrary command, a shell, or terminal typing.
- **C3 Prompt text is data.** A prompt is carried as an argument and never assembled into a
  shell command. Paths and prompts with control or invisible characters, or with Cursor's `${`
  variable syntax, are refused on both sides.
- **C4 Stop, don't guess.** Every step checks that it worked before the next one runs. A failed
  or unclear check stops the action and returns `needs_human` with the step name; nothing that
  may already have happened is retried blindly.
- **C5 Never type into the user's work.** If the keyboard or mouse was used in the last 10 s, or
  focus changes before a keypress, Remote Hands aborts instead of typing or opening a window.
- **C6 One submit per request.** Each request carries an id; a prompt is submitted at most once
  per id, across retries, service restarts, and windows (a journal entry per request id in the
  service, and an exclusive reservation per session id in the bridge).
- **C7 Everything is logged.** Every authorized action and its result, every request with a
  bad token, and every bridge refusal is appended to an audit log in the Remote Hands folder,
  never in a temporary folder. The prompt text and the token are never logged.
- **C8 Kill switch.** `hands off` stops every action that types, clicks, or starts something,
  and waits for running ones to finish. A switch that cannot be read counts as on.
- **C9** is reserved (an approval feature that is not part of this release).
- **C10 No screenshots.** Remote Hands never reads the screen; results and errors are compact
  text, and read-back uses Claude's own session record.
