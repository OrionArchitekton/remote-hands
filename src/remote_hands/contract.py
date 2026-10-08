"""The Remote Hands action contract: the one seam every caller and test goes through."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

OK = "ok"
NEEDS_HUMAN = "needs_human"
DESKTOP_LOCKED = "desktop_locked"
USER_ACTIVE = "user_active"
SERVICE_OFF = "service_off"
SERVICE_DOWN = "service_down"
BAD_REQUEST = "bad_request"
UNAUTHORIZED = "unauthorized"
UNKNOWN_ACTION = "unknown_action"
STAGED_NOT_SENT = "staged_not_sent"

STATUSES = frozenset(
    {
        OK,
        NEEDS_HUMAN,
        DESKTOP_LOCKED,
        USER_ACTIVE,
        SERVICE_OFF,
        SERVICE_DOWN,
        BAD_REQUEST,
        UNAUTHORIZED,
        UNKNOWN_ACTION,
        STAGED_NOT_SENT,
    }
)

# The server writes both into the audit log before it dispatches, so neither may be unbounded.
MAX_ACTION_CHARS = 64
MAX_REQUEST_ID_CHARS = 128


def _load_object(raw: bytes) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except RecursionError as exc:
        raise ValueError("body is nested too deeply") from exc
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def _non_empty_str(data: dict[str, Any], key: str, max_length: int | None = None) -> str:
    value = data.get(key)
    if isinstance(value, str) and value and (max_length is None or len(value) <= max_length):
        return value
    limit = "" if max_length is None else f" of at most {max_length} characters"
    raise ValueError(f"{key} must be a non-empty string{limit}")


@dataclass(frozen=True)
class ActionRequest:
    action: str
    request_id: str
    args: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: bytes) -> ActionRequest:
        data = _load_object(raw)
        args = data.get("args", {})
        if not isinstance(args, dict):
            raise ValueError("args must be a JSON object")
        return cls(
            action=_non_empty_str(data, "action", MAX_ACTION_CHARS),
            request_id=_non_empty_str(data, "request_id", MAX_REQUEST_ID_CHARS),
            args=args,
        )

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")


@dataclass(frozen=True)
class ActionResult:
    status: str
    request_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}")

    @classmethod
    def from_json(cls, raw: bytes) -> ActionResult:
        data = _load_object(raw)
        detail = data.get("detail", {})
        if not isinstance(detail, dict):
            raise ValueError("detail must be a JSON object")
        return cls(
            status=_non_empty_str(data, "status"),
            request_id=_non_empty_str(data, "request_id"),
            detail=detail,
        )

    def to_json(self) -> bytes:
        return json.dumps(asdict(self)).encode("utf-8")
