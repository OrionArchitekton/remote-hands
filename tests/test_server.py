import http.client
import json
import socket
import sys
import time

import pytest
from conftest import TOKEN, post, serve_in_background
from fakes import REMOTE, REPO, register

from remote_hands.actions import Context
from remote_hands.contract import ActionRequest
from remote_hands.desktop import FakeDesktop
from remote_hands.server import BIND_HOST, MAX_BODY_BYTES, _bind_with_retry, main, make_server


def health_body(request_id="h1"):
    return ActionRequest(action="health", request_id=request_id).to_json()


def test_binds_loopback_only(service):
    assert BIND_HOST == "127.0.0.1"
    assert service["server"].server_address[0] == "127.0.0.1"


def test_health_with_token(service):
    code, body = post(service["url"], health_body())
    assert code == 200
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["request_id"] == "h1"


def test_missing_token_is_refused_and_audited(service):
    code, body = post(service["url"], health_body(), token=None)
    assert code == 401
    assert json.loads(body)["status"] == "unauthorized"
    events = [json.loads(x)["event"] for x in service["audit"].read_text().splitlines()]
    assert events == ["unauthorized"]


def test_wrong_token_is_refused(service):
    code, _ = post(service["url"], health_body(), token="x" * 40)
    assert code == 401


def test_malformed_json_is_bad_request(service):
    code, body = post(service["url"], b"{nope")
    assert code == 400
    assert json.loads(body)["status"] == "bad_request"


def test_oversized_body_is_bad_request(service):
    # Declare an oversized body without sending it: the server must refuse on the header
    # alone, and an unread body would make the client race a connection reset.
    host, port = service["server"].server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.putrequest("POST", "/action")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
    conn.endheaders()
    assert conn.getresponse().status == 400
    conn.close()


def test_unknown_path_is_404(service):
    code, _ = post(service["url"].replace("/action", "/other"), health_body())
    assert code == 404


def test_get_is_refused(service):
    code, _ = post(service["url"], None, method="GET")
    assert code == 405


def test_every_action_is_audited_and_the_token_never_is(service):
    post(service["url"], health_body("h9"))
    post(service["url"], health_body(), token="y" * 40)
    text = service["audit"].read_text()
    assert TOKEN not in text
    assert "y" * 40 not in text
    actions = [json.loads(x) for x in text.splitlines() if json.loads(x)["event"] == "action"]
    assert [(a["action"], a["request_id"], a["status"]) for a in actions] == [
        ("health", "h9", "ok")
    ]


def test_unwritable_audit_log_refuses_before_acting(hands_home):
    desktop = FakeDesktop()
    ctx = Context(desktop=desktop, kill_switch=hands_home / "OFF", sleep=lambda _s: None)
    bad_audit = hands_home / "audit-is-a-directory"
    bad_audit.mkdir()
    server = make_server(port=0, token=TOKEN, ctx=ctx, audit_file=bad_audit)
    serve_in_background(server)
    try:
        host, port = server.server_address[:2]
        body = ActionRequest(action="probe_input", request_id="p1").to_json()
        code, raw = post(f"http://{host}:{port}/action", body)
    finally:
        server.shutdown()
        server.server_close()
    assert code == 503
    assert json.loads(raw)["status"] == "needs_human"
    assert desktop.probe_calls == []


def test_server_refuses_address_sharing(service):
    assert service["server"].allow_reuse_address is False


def test_unauthorized_still_answers_when_audit_fails(hands_home):
    ctx = Context(desktop=FakeDesktop(), kill_switch=hands_home / "OFF")
    bad_audit = hands_home / "audit-dir"
    bad_audit.mkdir()
    server = make_server(port=0, token=TOKEN, ctx=ctx, audit_file=bad_audit)
    serve_in_background(server)
    try:
        host, port = server.server_address[:2]
        code, _ = post(f"http://{host}:{port}/action", health_body(), token="w" * 40)
    finally:
        server.shutdown()
        server.server_close()
    assert code == 401


def test_bind_retries_then_succeeds(tmp_path):
    attempts = []

    def make():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("address in use")
        return "server"

    audit_file = tmp_path / "audit.jsonl"
    clock = iter(range(0, 1000, 5))
    result = _bind_with_retry(make, audit_file, sleep=lambda _s: None, clock=lambda: next(clock))
    assert result == "server"
    events = [json.loads(x)["event"] for x in audit_file.read_text().splitlines()]
    assert events == ["bind_failed", "bind_failed"]


def test_bind_gives_up_after_the_deadline(tmp_path):
    def make():
        raise OSError("address in use")

    clock = iter(range(0, 10000, 100))
    with pytest.raises(OSError):
        _bind_with_retry(
            make,
            tmp_path / "audit.jsonl",
            deadline_seconds=300,
            sleep=lambda _s: None,
            clock=lambda: next(clock),
        )


def test_idle_connections_are_closed(hands_home):
    ctx = Context(desktop=FakeDesktop(), kill_switch=hands_home / "OFF")
    server = make_server(
        port=0,
        token=TOKEN,
        ctx=ctx,
        audit_file=hands_home / "audit.jsonl",
        request_timeout=0.5,
    )
    serve_in_background(server)
    try:
        with socket.create_connection(server.server_address[:2], timeout=5) as conn:
            started = time.monotonic()
            assert conn.recv(1) == b""  # the server hangs up on a silent client
            assert time.monotonic() - started < 4
    finally:
        server.shutdown()
        server.server_close()


def test_start_claude_end_to_end_over_http(service, hands_home):
    register(hands_home, service["bridge"])
    body = json.dumps(
        {
            "action": "start_claude",
            "request_id": "req-0001-abcd",
            "args": {"repo": REPO, "remote": REMOTE, "prompt": "zebra prompt words"},
        }
    ).encode()
    status, raw = post(service["url"], body)
    out = json.loads(raw)
    assert status == 200
    assert out["status"] == "ok"
    audit_text = service["audit"].read_text(encoding="utf-8")
    last = json.loads(audit_text.splitlines()[-1])
    assert last["action"] == "start_claude"
    assert last["detail"]["session_id"] == out["detail"]["session_id"]
    assert "zebra prompt words" not in audit_text
    assert TOKEN not in audit_text
    starts = service["bridge"].starts
    assert len(starts) == 1
    assert starts[0]["body"]["prompt"] == "zebra prompt words"
    assert starts[0]["token"] == TOKEN


def test_body_limit_fits_a_full_prompt():
    assert MAX_BODY_BYTES >= 16_000 * 12 + 4096  # worst case: every character JSON-escaped


class _StubServer:
    def serve_forever(self):
        pass

    def server_close(self):
        pass


def test_main_wires_home_token_audit_file_and_kill_switch_into_the_service(hands_home, monkeypatch):
    # Without a home and a token in its Context, every start_claude answers needs_human.
    built = {}

    def fake_make_server(**kwargs):
        built.update(kwargs)
        return _StubServer()

    monkeypatch.setattr("remote_hands.server.make_server", fake_make_server)
    assert main(["--fake-desktop", "--port", "0"]) == 0
    assert built["token"] == TOKEN
    assert built["ctx"].token == TOKEN
    assert built["ctx"].state_dir == hands_home
    assert built["audit_file"] == hands_home / "audit.jsonl"
    assert built["ctx"].kill_switch == hands_home / "OFF"


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows refusal")
def test_a_refused_start_leaves_nothing_on_disk(tmp_path, monkeypatch):
    # An empty home on purpose: the hands_home fixture pre-writes a token.
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(tmp_path))
    with pytest.raises(SystemExit, match="run it on Windows"):
        main([])
    assert list(tmp_path.iterdir()) == []
