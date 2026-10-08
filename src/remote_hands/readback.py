"""Read-back: what a Claude session said, from its own session record.

Runs in WSL, where Claude Code writes one JSON line per event to
~/.claude/projects/<folder>/<session id>.jsonl. Never reads the screen (rule C10).
The reply is untrusted text: callers treat it as data, never as instructions.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

SESSION_ID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_REPLY_CHARS = 4000
POLL_SECONDS = 2.0
NOT_FOUND = "not_found"
STARTED = "started"
REPLIED = "replied"
UNREADABLE = "unreadable"


def check_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError("session id must be a lower-case UUID")
    return session_id


def find_session_file(projects_dir: Path, session_id: str) -> Path | None:
    check_session_id(session_id)
    if not projects_dir.is_dir():
        return None
    for folder in sorted(projects_dir.iterdir()):
        candidate = folder / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _normalized(text: str) -> str:
    """A prompt file and the session record can differ in line endings and edge whitespace."""
    return text.replace("\r\n", "\n").strip()


def _records(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue  # a half-written last line, or a line nested too deeply to parse
            if (
                isinstance(record, dict)
                and not record.get("isSidechain")
                and not record.get("isMeta")
                and isinstance(record.get("message"), dict)
            ):
                yield record


def read_record(path: Path, expected_prompt: str | None = None) -> dict[str, Any]:
    """The submitted prompt and the first text reply after it, from one session file.

    `reply_is_error` is True when that first reply is one of Claude Code's own synthetic
    API-error messages, which it marks with a top-level `isApiErrorMessage: true`.
    """
    prompt: str | None = None
    reply: str | None = None
    reply_is_error = False
    cwd: str | None = None
    has_user_record = False
    for record in _records(path):
        kind = record.get("type")
        content = _text(record["message"].get("content"))
        if prompt is None:
            if kind == "user" and content:
                has_user_record = True
                if expected_prompt is None or _normalized(content) == _normalized(expected_prompt):
                    prompt = content
                    cwd = record.get("cwd") if isinstance(record.get("cwd"), str) else None
            continue
        if kind == "assistant" and content.strip():
            reply = content.strip()
            reply_is_error = record.get("isApiErrorMessage") is True
            break
    return {
        "state": REPLIED if reply is not None else STARTED,
        "prompt_found": prompt is not None,
        "has_user_record": has_user_record,
        "cwd": cwd,
        "first_reply": None if reply is None else reply[:MAX_REPLY_CHARS],
        "truncated": reply is not None and len(reply) > MAX_REPLY_CHARS,
        "reply_is_error": reply_is_error,
    }


def wait_for_reply(
    projects_dir: Path,
    session_id: str,
    *,
    wait_seconds: float,
    expected_prompt: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll the session record until the first reply lands or the wait runs out.

    A record that cannot be read (a permission error, a file that vanished mid-read) is reported
    as `unreadable` with the error text, and polling goes on until the deadline like any other
    state without a reply.
    """
    check_session_id(session_id)
    deadline = monotonic() + wait_seconds
    while True:
        result: dict[str, Any]
        try:
            path = find_session_file(projects_dir, session_id)
            result = {"state": NOT_FOUND} if path is None else read_record(path, expected_prompt)
        except OSError as exc:
            result = {"state": UNREADABLE, "error": f"{type(exc).__name__}: {exc}"}
        result["session_id"] = session_id
        if result["state"] == REPLIED or monotonic() >= deadline:
            return result
        sleep(POLL_SECONDS)
