"""HTTP client for a window bridge on 127.0.0.1 (rule C1). Never proxied.

start_claude answers one of four ways, and the difference matters for C6:
  started      the bridge confirmed its terminal is running Claude
  refused      the bridge answered and did nothing (bad request, kill switch, duplicate)
  unreachable  the connection failed before the request was sent, so nothing happened
  unknown      the request may have been delivered but no clear answer came back
"""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

# Keep above ~2.2 s: on Windows a connect to a closed loopback port takes about 2 s to fail
# (measured on a Windows PC), and a shorter timeout would make every dead window read as busy
# instead of gone.
PING_TIMEOUT_SECONDS = 3.0
START_TIMEOUT_SECONDS = 20.0
# the bridge's /ping reply is about 70 bytes; a reply cut off here reads as not ours
MAX_PING_BYTES = 4096
MAX_REPLY_BYTES = 64 * 1024
STARTED = "started"
REFUSED = "refused"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class BridgeReply:
    status: str
    detail: dict[str, Any] = field(default_factory=dict)


class Bridge(Protocol):
    def ping(self, port: int) -> str | None: ...

    def start_claude(self, port: int, token: str, body: dict[str, Any]) -> BridgeReply: ...


def _decode(raw: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


class HttpBridgeClient:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host

    def ping(self, port: int) -> str | None:
        """The instance id in a whole reply to GET /ping, or None when nothing of ours is there: the
        connection was refused, or a whole HTTP reply came back that is not a bridge's answer.

        Any other failure (a timeout, a reset, a reply that is not HTTP, any other OSError) is
        raised, because it does not show that the window is gone (registry.find_window: busy).
        """
        conn = http.client.HTTPConnection(self.host, port, timeout=PING_TIMEOUT_SECONDS)
        try:
            conn.request("GET", "/ping")
            resp = conn.getresponse()
            data = _decode(resp.read(MAX_PING_BYTES)) if resp.status == 200 else None
        except ConnectionRefusedError:
            return None
        finally:
            conn.close()
        instance = data.get("instance") if data else None
        return instance if isinstance(instance, str) else None

    def start_claude(self, port: int, token: str, body: dict[str, Any]) -> BridgeReply:
        conn = http.client.HTTPConnection(self.host, port, timeout=START_TIMEOUT_SECONDS)
        try:
            try:
                conn.connect()
            except OSError as exc:
                return BridgeReply(UNREACHABLE, {"error": str(exc)})
            try:
                conn.request(
                    "POST",
                    "/start-claude",
                    body=json.dumps(body).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                resp = conn.getresponse()
                code, raw = resp.status, resp.read(MAX_REPLY_BYTES)
            except (OSError, http.client.HTTPException) as exc:
                return BridgeReply(UNKNOWN, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()
        data = _decode(raw) or {}
        if code == 200 and data.get("status") == STARTED:
            return BridgeReply(STARTED, data)
        if 400 <= code < 500 and data.get("status") == REFUSED:
            return BridgeReply(REFUSED, data)
        return BridgeReply(UNKNOWN, {**data, "http_status": code})
