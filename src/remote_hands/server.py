"""Remote Hands service: local only (rule C1), token required, fixed actions (C2)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, audit, paths
from .actions import Context, dispatch
from .auth import load_or_create_token, token_matches
from .contract import BAD_REQUEST, NEEDS_HUMAN, UNAUTHORIZED, ActionRequest, ActionResult
from .desktop import Desktop

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 47821
MAX_BODY_BYTES = 256 * 1024  # a 16,000-character prompt, JSON-escaped, fits with room
BIND_RETRY_SECONDS = 300
BIND_RETRY_INTERVAL_SECONDS = 5


class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """No address sharing: another process must not bind our port and read the caller's token."""

    allow_reuse_address = False

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)  # Windows only
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def make_server(
    *,
    port: int,
    token: str,
    ctx: Context,
    audit_file: Path,
    request_timeout: float = 10.0,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"RemoteHands/{__version__}"
        sys_version = ""
        timeout = request_timeout

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # the audit log is the record; nothing goes to stdout

        def _send(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _refuse(self, code: int, status: str, error: str | None = None) -> None:
            payload: dict[str, Any] = {"status": status}
            if error:
                payload["detail"] = {"error": error}
            self._send(code, payload)

        def do_GET(self) -> None:  # noqa: N802
            self._refuse(405, BAD_REQUEST, "POST /action only")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/action":
                self._refuse(404, BAD_REQUEST, "unknown path")
                return
            header = self.headers.get("Authorization", "")
            presented = header.removeprefix("Bearer ") if header.startswith("Bearer ") else None
            if not token_matches(presented, token):
                try:
                    audit.append(audit_file, {"event": "unauthorized"})
                except OSError:
                    pass  # refusing matters more than recording the refusal
                self._refuse(401, UNAUTHORIZED)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                self._refuse(400, BAD_REQUEST, f"body must be 1 to {MAX_BODY_BYTES} bytes")
                return
            try:
                req = ActionRequest.from_json(self.rfile.read(length))
            except ValueError as exc:
                self._refuse(400, BAD_REQUEST, str(exc))
                return
            try:
                audit.append(
                    audit_file,
                    {"event": "intent", "action": req.action, "request_id": req.request_id},
                )
            except OSError as exc:
                self._send(
                    503,
                    {
                        "status": NEEDS_HUMAN,
                        "request_id": req.request_id,
                        "detail": {"step": "audit", "error": f"audit log not writable: {exc}"},
                    },
                )
                return
            result = dispatch(req, ctx)
            try:
                audit.append(
                    audit_file,
                    {
                        "event": "action",
                        "action": req.action,
                        "request_id": req.request_id,
                        "status": result.status,
                        "detail": result.detail,
                    },
                )
            except OSError as exc:
                result = ActionResult(
                    NEEDS_HUMAN,
                    req.request_id,
                    {
                        "step": "audit",
                        "error": f"action ran but its result was not audited: {exc}",
                        "result": json.loads(result.to_json()),
                    },
                )
            self._send(200, json.loads(result.to_json()))

    return _ExclusiveHTTPServer((BIND_HOST, port), Handler)


def _bind_with_retry(
    make: Callable[[], ThreadingHTTPServer],
    audit_file: Path,
    *,
    deadline_seconds: float = BIND_RETRY_SECONDS,
    interval: float = BIND_RETRY_INTERVAL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ThreadingHTTPServer:
    started = clock()
    while True:
        try:
            return make()
        except OSError as exc:
            audit.append(audit_file, {"event": "bind_failed", "error": str(exc)})
            if clock() - started >= deadline_seconds:
                raise
            sleep(interval)


def _desktop(fake: bool) -> Desktop:
    if fake:
        from .desktop import FakeDesktop

        return FakeDesktop()
    if sys.platform != "win32":
        raise SystemExit(
            "the service drives the Windows desktop; run it on Windows or pass --fake-desktop"
        )
    from .win_desktop import WindowsDesktop

    return WindowsDesktop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="remote-hands-service")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("REMOTE_HANDS_PORT", DEFAULT_PORT))
    )
    parser.add_argument(
        "--fake-desktop", action="store_true", help="development only: use FakeDesktop"
    )
    args = parser.parse_args(argv)
    desktop = _desktop(args.fake_desktop)  # first: a refused start must leave nothing on disk
    token = load_or_create_token(paths.token_path())
    ctx = Context(
        desktop=desktop,
        kill_switch=paths.kill_switch_path(),
        state_dir=paths.home(),
        token=token,
    )
    server = _bind_with_retry(
        lambda: make_server(port=args.port, token=token, ctx=ctx, audit_file=paths.audit_path()),
        paths.audit_path(),
    )
    audit.append(paths.audit_path(), {"event": "start", "version": __version__, "port": args.port})
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
