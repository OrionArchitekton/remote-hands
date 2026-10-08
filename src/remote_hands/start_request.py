"""Validation for the start_claude action's arguments (rules C2, C3).

The repo path, the remote name, and the label end up in a Cursor launch and in the Claude
command line's argument list, so each is checked against a strict shape before anything runs.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import unicodedata
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths

# Where a session may start. Read, in order, from REMOTE_HANDS_ALLOWED_ROOTS (absolute WSL paths
# separated by ':'), from "allowed_roots" in config.json in the Remote Hands home, or else these
# folders under the WSL user's home. The service runs on Windows, where that home is unknown, so
# there it needs one of the first two (the installer writes config.json). The window bridge reads
# the same three in the same order (bridge/src/core.js allowedRoots).
ALLOWED_ROOTS_ENV = "REMOTE_HANDS_ALLOWED_ROOTS"
ALLOWED_ROOTS_KEY = "allowed_roots"
DEFAULT_ROOT_DIRS = ("src", ".worktrees")
MAX_REPO_CHARS = 300
MAX_PROMPT_CHARS = 16_000
REMOTE_PATTERN = re.compile(r"wsl\+[a-z0-9][a-z0-9._-]{0,63}")
LABEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}")
# Fixed namespace: the same request id always maps to the same Claude session id.
SESSION_NAMESPACE = uuid.UUID("7d1f6b0e-2c4a-4f53-9a77-3e5b8c0d9a21")
_PROMPT_CONTROLS_ALLOWED = frozenset("\t\n\r")
_REFUSED_PROMPT_STARTS = ("/", "!", "#", "-")
# Cursor runs a terminal's arguments and working directory through its variable resolver before
# the process starts, so ${env:NAME}, ${selectedText}, and the like would be replaced by Cursor
# and Claude would receive something other than what was sent (rule C3).
_VARIABLE_SYNTAX = "${"
# Invisible and line-breaking characters (format characters such as zero-width spaces, bidi
# controls, and tag characters; lone surrogates; line and paragraph separators) can hide text
# from a reader or break encoding, so neither paths nor prompts may carry them.
_HIDDEN_CATEGORIES = frozenset({"Cf", "Cs", "Zl", "Zp"})
# Variation selectors after an emoji can carry hidden bytes, one byte per selector, so a
# supplementary selector and a selector directly after another selector are refused as well.
_BASIC_SELECTORS = range(0xFE00, 0xFE10)  # U+FE00 to U+FE0F
_SUPPLEMENTARY_SELECTORS = range(0xE0100, 0xE01F0)  # U+E0100 to U+E01EF
_START_KEYS = frozenset({"repo", "remote", "prompt", "label"})


@dataclass(frozen=True)
class StartArgs:
    repo: str
    remote: str
    prompt: str
    label: str


def _is_control(ch: str) -> bool:
    code = ord(ch)
    return code < 32 or 0x7F <= code <= 0x9F


def _is_hidden(ch: str) -> bool:
    return unicodedata.category(ch) in _HIDDEN_CATEGORIES


def _is_variation_selector(ch: str) -> bool:
    code = ord(ch)
    return code in _BASIC_SELECTORS or code in _SUPPLEMENTARY_SELECTORS


def _is_refused_selector(text: str, index: int) -> bool:
    """A supplementary selector, or any selector directly after another selector.

    A single ordinary selector after a character (U+FE0F after a heart) stays allowed.
    """
    code = ord(text[index])
    if code in _SUPPLEMENTARY_SELECTORS:
        return True
    return code in _BASIC_SELECTORS and index > 0 and _is_variation_selector(text[index - 1])


def _first_refused(text: str, *, prompt: bool) -> str | None:
    """Name the first character `text` may not carry as 'U+XXXX at index N', or None.

    Indexes count code points, and the text itself is never echoed. A path carries no control
    characters; a prompt may carry tab, newline, and CRLF.
    """
    for index, ch in enumerate(text):
        if (
            _is_hidden(ch)
            or _is_refused_selector(text, index)
            or (_is_control(ch) and not (prompt and ch in _PROMPT_CONTROLS_ALLOWED))
            or (prompt and ch == "\r" and not text.startswith("\n", index + 1))
        ):
            return f"U+{ord(ch):04X} at index {index}"
    return None


def parse_roots(entries: Any) -> tuple[str, ...]:
    """Allowed roots as absolute, normalized WSL folders, each ending in '/'.

    The filesystem root, relative paths, '.', '..', and '//' are refused, so a root can only
    narrow where a session starts.
    """
    if not isinstance(entries, list) or not entries:
        raise ValueError("allowed roots must be a non-empty list of absolute paths")
    roots = []
    for entry in entries:
        folder = entry.rstrip("/") if isinstance(entry, str) else ""
        segments = folder.split("/")[1:]
        if (
            not folder.startswith("/")
            or "//" in folder
            or any(seg in ("", ".", "..") for seg in segments)
            or _first_refused(folder, prompt=False) is not None
        ):
            raise ValueError(f"allowed root {entry!r} must be an absolute, normalized path")
        roots.append(folder + "/")
    return tuple(roots)


def allowed_roots() -> tuple[str, ...]:
    raw = os.environ.get(ALLOWED_ROOTS_ENV, "")
    if raw:
        return parse_roots(raw.split(":"))
    config: Path = paths.config_path()
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = None
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"cannot read {config}: {exc}") from exc
    if data is not None:
        if not isinstance(data, dict) or ALLOWED_ROOTS_KEY not in data:
            raise ValueError(f"{config} must be a JSON object with {ALLOWED_ROOTS_KEY}")
        return parse_roots(data[ALLOWED_ROOTS_KEY])
    if sys.platform == "win32":
        raise ValueError(
            f"no allowed roots: set {ALLOWED_ROOTS_KEY} in {config} or {ALLOWED_ROOTS_ENV}"
        )
    return parse_roots([f"{Path.home()}/{name}" for name in DEFAULT_ROOT_DIRS])


def check_repo(repo: Any) -> str:
    """Lexical only: the service runs on Windows and cannot resolve WSL paths, so a symlink
    under an allowed root passes. The roots limit where a session starts, not what it can reach.
    """
    if not isinstance(repo, str) or not repo or len(repo) > MAX_REPO_CHARS:
        raise ValueError(f"repo must be a path of 1 to {MAX_REPO_CHARS} characters")
    where = _first_refused(repo, prompt=False)
    if where is not None:
        raise ValueError(f"repo must not contain control or invisible characters ({where})")
    if _VARIABLE_SYNTAX in repo:
        raise ValueError(
            f"repo must not contain {_VARIABLE_SYNTAX} (Cursor would substitute a variable there)"
        )
    if posixpath.normpath(repo) != repo:
        raise ValueError("repo must be a normalized absolute path")
    roots = allowed_roots()
    if not any(repo.startswith(root) and len(repo) > len(root) for root in roots):
        raise ValueError(f"repo must be inside one of {', '.join(roots)}")
    return repo


def check_prompt(prompt: Any) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be non-empty text")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
    where = _first_refused(prompt, prompt=True)
    if where is not None:
        raise ValueError(
            "prompt must not contain control or invisible characters; "
            f"tab, newline, and CRLF are allowed ({where})"
        )
    # Claude Code reads a leading "/" as a slash command, "!" as a shell escape, and "#" as a
    # memory note, and records the text either way. A leading "-" is refused too: "--" ends
    # option parsing, but some of the CLI reads the raw arguments and ignores it. A prompt is
    # data (rule C3), never a command or an option.
    if prompt.lstrip()[:1] in _REFUSED_PROMPT_STARTS:
        raise ValueError(
            "prompt must not start with /, !, #, or - "
            "(Claude would treat it as a command, a shell escape, a memory note, or an option)"
        )
    if _VARIABLE_SYNTAX in prompt:
        raise ValueError(
            f"prompt must not contain {_VARIABLE_SYNTAX} (Cursor would substitute a variable there)"
        )
    # "--" ends option parsing but not subcommand dispatch, so a prompt that is one word naming
    # a Claude command (update, mcp, doctor, ...) would run that command. Two words never can.
    if len(prompt.split()) < 2:
        raise ValueError(
            "prompt must be at least two words "
            "(Claude treats a single word that names one of its commands as that command)"
        )
    return prompt


def check_remote(remote: Any) -> str:
    if not isinstance(remote, str) or not REMOTE_PATTERN.fullmatch(remote):
        raise ValueError("remote must look like wsl+<distro>, in lower case")
    return remote


def check_label(label: Any) -> str:
    if not isinstance(label, str) or not LABEL_PATTERN.fullmatch(label):
        raise ValueError(
            "label must be 1 to 48 letters, digits, spaces, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    return label


def default_label(repo: str, request_id: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "-", posixpath.basename(repo))[:30] or "repo"
    return f"hands {base} {request_id[:6]}"


def parse_start_args(args: dict[str, Any], request_id: str) -> StartArgs:
    if set(args) - _START_KEYS:
        raise ValueError("only repo, remote, prompt, and label are accepted")
    repo = check_repo(args.get("repo"))
    label = args.get("label")
    return StartArgs(
        repo=repo,
        remote=check_remote(args.get("remote")),
        prompt=check_prompt(args.get("prompt")),
        label=check_label(label if label is not None else default_label(repo, request_id)),
    )


def session_id_for(request_id: str) -> str:
    return str(uuid.uuid5(SESSION_NAMESPACE, request_id))


def folder_uri(remote: str, repo: str) -> str:
    """The URI Cursor itself records for a WSL folder ('+' in the authority written %2B)."""
    authority = urllib.parse.quote(remote, safe="")
    return f"vscode-remote://{authority}{urllib.parse.quote(repo, safe='/-._~')}"
