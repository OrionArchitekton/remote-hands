"""Request journal (rule C6): a prompt is submitted at most once per request id.

One JSON file per request id under the Remote Hands home. A request moves through
claimed, then not_submitted or submitting; submitting ends as fulfilled or unknown, or
returns to not_submitted when the request provably was never delivered. fulfilled and
unknown are final. claim() hands out only a not_submitted entry again: every other state
may mean the prompt was already sent, so a retry must not send it a second time (rule C4).
The one exception lives in the start action, not here: under its start lock it may take
over a claimed entry written since the current boot, because a claim is written before
anything is sent (see actions._start_claude).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Lower case only: NTFS ignores case in file names, so "Req-0001-ABCD" and "req-0001-abcd"
# would share one entry and the second request would silently replay the first.
REQUEST_ID_PATTERN = re.compile(r"[a-z0-9_-]{8,64}")
CLAIMED = "claimed"
NOT_SUBMITTED = "not_submitted"
SUBMITTING = "submitting"
FULFILLED = "fulfilled"
UNKNOWN = "unknown"
STATES = frozenset({CLAIMED, NOT_SUBMITTED, SUBMITTING, FULFILLED, UNKNOWN})
# The moves mark() allows. fulfilled and unknown are final. not_submitted is left only through
# claim(). submitting -> not_submitted is only for a request that provably was never delivered.
_TRANSITIONS: dict[str, frozenset[str]] = {
    CLAIMED: frozenset({NOT_SUBMITTED, SUBMITTING}),
    SUBMITTING: frozenset({FULFILLED, UNKNOWN, NOT_SUBMITTED}),
}
_REPLACE_ATTEMPTS = 3
_REPLACE_PAUSE_SECONDS = 0.05


@dataclass(frozen=True)
class Entry:
    """One journal record: a request id, its state, and the caller's detail for it."""

    request_id: str
    state: str
    detail: dict[str, Any] = field(default_factory=dict)


def check_request_id(request_id: str) -> str:
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError("request_id must be 8 to 64 lower-case letters, digits, '-' or '_'")
    return request_id


def _encode(request_id: str, state: str, detail: dict[str, Any]) -> str:
    return json.dumps({"request_id": request_id, "state": state, "detail": detail}, sort_keys=True)


class Journal:
    """The request journal: claim an id, move its entry between states, read it back."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, request_id: str) -> Path:
        return self.directory / f"{check_request_id(request_id)}.json"

    def _replace(self, path: Path, text: str) -> None:
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            attempt = 0
            while True:
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    # Windows: another handle has the entry open; it should let go shortly.
                    attempt += 1
                    if attempt >= _REPLACE_ATTEMPTS:
                        raise
                    time.sleep(_REPLACE_PAUSE_SECONDS)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def read(self, request_id: str) -> Entry | None:
        """None when the request was never claimed; UNKNOWN when the entry cannot be trusted."""
        path = self._path(request_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            # A torn or empty entry proves nothing about what was sent: treat it as unknown.
            return Entry(request_id, UNKNOWN, {"error": "journal entry unreadable"})
        # Trust an entry only on positive evidence that it is a well-formed record of THIS request.
        if not (
            isinstance(data, dict)
            and data.get("request_id") == request_id
            and isinstance(data.get("state"), str)
            and data["state"] in STATES
            and isinstance(data.get("detail"), dict)
        ):
            return Entry(request_id, UNKNOWN, {"error": "journal entry malformed"})
        return Entry(request_id, data["state"], data["detail"])

    def written_at(self, request_id: str) -> float | None:
        """When the entry file was last written (epoch seconds), or None if there is none."""
        try:
            return self._path(request_id).stat().st_mtime
        except FileNotFoundError:
            return None

    def claim(self, request_id: str) -> Entry | None:
        """Claim a request id. None means this caller owns it now; otherwise the prior entry.

        Only a not_submitted entry is reclaimed here; a claimed entry comes back as the prior
        entry (the start action may take over one written since the current boot, see
        actions._start_claude). Callers serialize claims for one request id (the service's
        start lock does): reclaiming a not_submitted entry is read-then-replace.
        """
        path = self._path(request_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            prior = self.read(request_id)
            if prior is None:
                return Entry(request_id, UNKNOWN, {"error": "journal entry vanished"})
            if prior.state == NOT_SUBMITTED:
                self._replace(path, _encode(request_id, CLAIMED, {}))
                return None
            return prior
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_encode(request_id, CLAIMED, {}))
            fh.flush()
            os.fsync(fh.fileno())
        return None

    def mark(self, request_id: str, state: str, detail: dict[str, Any] | None = None) -> None:
        """Move a request to its next state; a move the state machine forbids raises ValueError."""
        if state not in STATES:
            raise ValueError(f"unknown journal state {state!r}")
        entry = self.read(request_id)
        current = entry.state if entry is not None else "no entry"
        if state not in _TRANSITIONS.get(current, frozenset()):
            raise ValueError(f"journal cannot move {request_id} from {current} to {state}")
        self._replace(self._path(request_id), _encode(request_id, state, detail or {}))
