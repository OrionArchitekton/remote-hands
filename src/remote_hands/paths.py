"""Where Remote Hands keeps its state, and where it finds Claude's.

Its own state is one folder: the token, the kill switch, the audit log, the optional config file
(config.json), one registration file per Cursor window (windows/, written by that window's bridge),
and one journal entry per start request (journal/). The service runs on Windows and uses
%LOCALAPPDATA%\\remote-hands. The `hands` CLI runs in WSL and reaches the same folder through the
Windows drive mount (/mnt/c by default), so there is one token file and one kill switch, never a
copy.

Claude's session records are the exception: they live outside that folder, in WSL (under
~/.claude/projects, or CLAUDE_PROJECTS_DIR). Remote Hands never writes them; the `hands` CLI
reads them for read-back.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR_NAME = "remote-hands"
# Used when cmd.exe is not on the WSL PATH (appendWindowsPath=false); the default drive mount.
FALLBACK_CMD_EXE = "/mnt/c/Windows/System32/cmd.exe"
WINDOWS_QUERY_TIMEOUT_SECONDS = 10


def _run(argv: list[str]) -> str:
    """Run a fixed argv (never a shell) and return its stdout; raise ValueError on any failure."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed executables and arguments
            argv,
            stdin=subprocess.DEVNULL,  # a Windows child must not inherit the WSL interop pipe
            capture_output=True,
            text=True,
            timeout=WINDOWS_QUERY_TIMEOUT_SECONDS,
            check=True,
            cwd="/",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"{argv[0]} failed: {exc}") from exc
    return done.stdout


@functools.lru_cache(maxsize=1)
def _windows_local_app_data() -> Path:
    """%LOCALAPPDATA% of the Windows user, as a WSL path.

    Taken from LOCALAPPDATA when Windows passes it in already translated (WSLENV=LOCALAPPDATA/p);
    otherwise asked of Windows once (cmd.exe, then wslpath) and remembered for this process.
    """
    passed = os.environ.get("LOCALAPPDATA", "")
    if passed.startswith("/"):
        return Path(passed)
    cmd = shutil.which("cmd.exe") or FALLBACK_CMD_EXE
    value = _run([cmd, "/d", "/c", "echo", "%LOCALAPPDATA%"]).strip()
    if not value or "%" in value:
        raise ValueError("Windows did not report %LOCALAPPDATA%")
    translated = _run(["wslpath", "-u", value]).strip()
    if not translated.startswith("/"):
        raise ValueError(f"wslpath did not translate {value!r}")
    return Path(translated)


def home() -> Path:
    override = os.environ.get("REMOTE_HANDS_HOME")
    if override:
        home_dir = Path(override)
        if not home_dir.is_absolute():
            raise ValueError("REMOTE_HANDS_HOME must be an absolute path")
        return home_dir
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / APP_DIR_NAME
    try:
        return _windows_local_app_data() / APP_DIR_NAME
    except ValueError as exc:
        raise ValueError(
            f"cannot find the Windows %LOCALAPPDATA% from WSL ({exc}); set REMOTE_HANDS_HOME to "
            f"%LOCALAPPDATA%\\{APP_DIR_NAME} as WSL sees it"
        ) from exc


def token_path() -> Path:
    return home() / "token"


def kill_switch_path() -> Path:
    return home() / "OFF"


def audit_path() -> Path:
    return home() / "audit.jsonl"


def config_path() -> Path:
    """Optional settings shared by the service, the CLI, and every window bridge."""
    return home() / "config.json"


def windows_dir() -> Path:
    """Where each Cursor window's bridge registers itself, one JSON file per window."""
    return home() / "windows"


def journal_dir() -> Path:
    """One file per start request, so a retried request never starts a second session (C6)."""
    return home() / "journal"


def claude_projects_dir() -> Path:
    """Claude Code's session records, read in WSL for read-back."""
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    if override:
        projects = Path(override)
        if not projects.is_absolute():
            raise ValueError("CLAUDE_PROJECTS_DIR must be an absolute path")
        return projects
    return Path.home() / ".claude" / "projects"
