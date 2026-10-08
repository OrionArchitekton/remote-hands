import errno
import http.client
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import serve_in_background

from remote_hands import bridge_client
from remote_hands.bridge_client import REFUSED, STARTED, UNKNOWN, UNREACHABLE, HttpBridgeClient

INSTANCE = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def fake_bridge():
    state = {"status": 200, "body": {"status": "started", "terminal_pid": 4242}, "raw": None}
    state["seen"] = []
    state["ping_status"] = 200

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _reply(self, code, payload):
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            if self.path == "/ping":
                self._reply(state["ping_status"], {"instance": INSTANCE, "version": "0.1.0"})
            else:
                self._reply(404, {})

        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            state["seen"].append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            self._reply(
                state["status"], state["raw"] if state["raw"] is not None else state["body"]
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serve_in_background(server)
    state["port"] = server.server_address[1]
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_ping_returns_the_bridge_instance(fake_bridge):
    assert HttpBridgeClient().ping(fake_bridge["port"]) == INSTANCE


def test_ping_of_a_closed_port_is_none():
    assert HttpBridgeClient().ping(free_port()) is None


def test_ping_of_a_503_reply_is_none(fake_bridge):
    # The 503 body is a perfectly good ping answer, so only the status code can reject it.
    fake_bridge["ping_status"] = 503
    assert HttpBridgeClient().ping(fake_bridge["port"]) is None


def serve_once(answer):
    """A listener on a free port that, for one connection, runs answer(conn) and closes it."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def run():
        conn, _ = server.accept()
        try:
            answer(conn)
        finally:
            conn.close()
            server.close()

    threading.Thread(target=run, daemon=True).start()
    return server.getsockname()[1]


def reset_at_once(conn):
    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close sends RST


def test_a_refused_ping_is_none(monkeypatch):
    def refusing(self, *args, **kwargs):
        raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    monkeypatch.setattr(http.client.HTTPConnection, "request", refusing)
    assert HttpBridgeClient().ping(free_port()) is None


def test_a_reset_ping_raises_instead_of_reading_as_gone():
    with pytest.raises((OSError, http.client.HTTPException)):
        HttpBridgeClient().ping(serve_once(reset_at_once))


def test_a_reply_that_is_not_http_raises_instead_of_reading_as_gone():
    def garbage(conn):
        conn.recv(4096)
        conn.sendall(b"NOT HTTP AT ALL\r\n\r\n")

    with pytest.raises(http.client.HTTPException):
        HttpBridgeClient().ping(serve_once(garbage))


def test_any_other_os_error_from_a_ping_is_raised(monkeypatch):
    def unreachable(self, *args, **kwargs):
        raise OSError(errno.EHOSTUNREACH, "No route to host")

    monkeypatch.setattr(http.client.HTTPConnection, "request", unreachable)
    with pytest.raises(OSError) as raised:
        HttpBridgeClient().ping(free_port())
    assert raised.value.errno == errno.EHOSTUNREACH


def test_silent_listener_makes_ping_time_out(monkeypatch):
    monkeypatch.setattr(bridge_client, "PING_TIMEOUT_SECONDS", 0.3)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        with pytest.raises(TimeoutError):
            HttpBridgeClient().ping(s.getsockname()[1])


def test_started_reply_carries_the_detail_and_sends_the_token(fake_bridge):
    reply = HttpBridgeClient().start_claude(fake_bridge["port"], "tok", {"session_id": "s"})
    assert reply.status == STARTED
    assert reply.detail["terminal_pid"] == 4242
    (seen,) = fake_bridge["seen"]
    assert seen == {"path": "/start-claude", "auth": "Bearer tok", "body": {"session_id": "s"}}


def test_explicit_refusal_is_refused(fake_bridge):
    fake_bridge["status"] = 409
    fake_bridge["body"] = {"status": "refused", "reason": "duplicate_session"}
    reply = HttpBridgeClient().start_claude(fake_bridge["port"], "tok", {})
    assert reply.status == REFUSED
    assert reply.detail["reason"] == "duplicate_session"


def test_closed_port_is_unreachable():
    assert HttpBridgeClient().start_claude(free_port(), "tok", {}).status == UNREACHABLE


def test_connect_timeout_is_unreachable(monkeypatch):
    # A connect that times out never got as far as sending the request, so it is safe to retry.
    def timing_out(self):
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", timing_out)
    assert HttpBridgeClient().start_claude(free_port(), "tok", {}).status == UNREACHABLE


def test_silent_listener_after_send_is_unknown(monkeypatch):
    monkeypatch.setattr(bridge_client, "START_TIMEOUT_SECONDS", 0.3)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert HttpBridgeClient().start_claude(s.getsockname()[1], "tok", {}).status == UNKNOWN


@pytest.mark.parametrize(
    "status,raw",
    [
        (500, b'{"status": "failed"}'),
        (200, b"not json"),
        (200, b'{"status": "refused"}'),
        (403, b'{"status": "other"}'),
        (500, b'{"status": "started"}'),
        (404, b'{"status": "started"}'),
        (500, b'{"status": "refused"}'),
    ],
)
def test_anything_unclear_is_unknown(fake_bridge, status, raw):
    fake_bridge["status"] = status
    fake_bridge["raw"] = raw
    assert HttpBridgeClient().start_claude(fake_bridge["port"], "tok", {}).status == UNKNOWN


def test_bridge_body_cannot_overwrite_the_real_http_status(fake_bridge):
    fake_bridge["status"] = 500
    fake_bridge["raw"] = b'{"status": "failed", "http_status": 200}'
    reply = HttpBridgeClient().start_claude(fake_bridge["port"], "tok", {})
    assert reply.status == UNKNOWN
    assert reply.detail["http_status"] == 500
