import http.client
import json
import os
import re
import socket
import threading
import time

import pytest
from fakes import Clock, register

from remote_hands import cli, start_request
from remote_hands.start_request import session_id_for


def run(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_health_via_cli(service, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    code, out = run(capsys, "health")
    assert code == 0
    assert out["status"] == "ok"


def test_service_down_when_nothing_listens(hands_home, monkeypatch, capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("REMOTE_HANDS_URL", f"http://127.0.0.1:{port}/action")
    code, out = run(capsys, "health")
    assert code == 1
    assert out["status"] == "service_down"
    assert out["detail"]["refused"] is True  # nothing was sent


def test_kill_switch_round_trip(service, hands_home, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    code, out = run(capsys, "off")
    assert code == 0
    assert out["detail"]["service_confirms"] is True
    assert (hands_home / "OFF").exists()
    code, out = run(capsys, "probe-input")
    assert code == 1
    assert out["status"] == "service_off"
    assert service["desktop"].probe_calls == []
    assert run(capsys, "on")[0] == 0
    assert not (hands_home / "OFF").exists()


def test_probe_input_via_cli(service, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    code, out = run(capsys, "probe-input", "--delay", "3")
    assert code == 0
    assert out["detail"]["text_arrived"] is True


@pytest.mark.parametrize(
    "url",
    [
        "http://100.64.0.9:47821/action",
        "http://127.0.0.1:80@evil.example/action",
        "http://127.0.0.1:47821.evil/action",
        "http://127.0.0.1:47821/action\n",
        "https://127.0.0.1:47821/action",
        "http://localhost:47821/action",
        "http://127.0.0.1:47821/other",
    ],
)
def test_refuses_anything_but_the_loopback_action_url(hands_home, monkeypatch, capsys, url):
    monkeypatch.setenv("REMOTE_HANDS_URL", url)
    code, out = run(capsys, "health")
    assert code == 1
    assert out["status"] == "bad_request"


def test_ignores_proxy_environment(service, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    assert run(capsys, "health")[0] == 0


def test_missing_token_file_reports_service_down(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(tmp_path))
    code, out = run(capsys, "health")
    assert code == 1
    assert out["status"] == "service_down"
    assert "token" in out["detail"]["error"]


def test_off_without_a_running_service_is_unconfirmed(hands_home, monkeypatch, capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("REMOTE_HANDS_URL", f"http://127.0.0.1:{port}/action")
    code, out = run(capsys, "off")
    assert code == 0
    detail = out["detail"]
    # a refused connection means nothing is listening, so nothing can be running
    assert (detail["kill_switch"], detail["service_confirms"], detail["in_flight"]) == (
        True,
        None,
        [],
    )
    assert detail["last_error"]  # unconfirmed: the refusal is reported, not hidden
    assert set(detail) == {"kill_switch", "service_confirms", "in_flight", "last_error"}


def test_off_flags_a_service_that_looks_elsewhere(service, tmp_path_factory, monkeypatch, capsys):
    from conftest import TOKEN

    other = tmp_path_factory.mktemp("other-home")
    (other / "token").write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(other))
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    code, out = run(capsys, "off")
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["service_confirms"] is False
    assert out["detail"]["last_error"] is None  # the service answered; it just disagrees


def test_off_with_a_different_token_is_flagged(service, tmp_path_factory, monkeypatch, capsys):
    other = tmp_path_factory.mktemp("other-token-home")
    (other / "token").write_text("z" * 40, encoding="utf-8")
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(other))
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    code, out = run(capsys, "off")
    assert code == 1
    assert out["detail"]["service_confirms"] is False
    assert out["detail"]["last_error"] == "HTTP 401"


def test_off_with_a_bad_url_still_reports_json(hands_home, monkeypatch, capsys):
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    monkeypatch.setenv("REMOTE_HANDS_URL", "http://example.com:47821/action")
    code, out = run(capsys, "off")
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["error"].startswith("cannot ask the service")
    assert out["detail"]["in_flight"] is None
    assert out["detail"]["last_error"] is None  # no health call was ever made
    assert (hands_home / "OFF").exists()
    assert clock.now == 0  # a bad URL cannot fix itself: nothing to wait for


def test_on_with_a_bad_url_still_clears_the_switch(hands_home, monkeypatch, capsys):
    (hands_home / "OFF").touch()
    monkeypatch.setenv("REMOTE_HANDS_URL", "http://example.com:47821/action")
    code, out = run(capsys, "on")
    assert code == 0
    assert out["detail"]["kill_switch"] is False
    assert out["detail"]["service_confirms"] is None
    assert out["detail"]["error"].startswith("cannot ask the service")
    assert not (hands_home / "OFF").exists()


def test_off_with_an_unreadable_token_answers_at_once(tmp_path, monkeypatch, capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("REMOTE_HANDS_URL", f"http://127.0.0.1:{port}/action")
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(tmp_path))  # no token file in it
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    code, out = run(capsys, "off")
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["error"].startswith("cannot ask the service")
    assert out["detail"]["service_confirms"] is None
    assert out["detail"]["in_flight"] is None
    assert (tmp_path / "OFF").exists()
    assert clock.now == 0


RID = "req-0001-abcd"


def seed_session(projects, session_id, prompt, reply):
    folder = projects / "-home-user-src-repo-a"
    folder.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
            "cwd": "/home/user/src/repo-a",
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]},
        },
    ]
    lines = "".join(json.dumps(record) + "\n" for record in records)
    (folder / f"{session_id}.jsonl").write_text(lines, encoding="utf-8")


@pytest.fixture
def repo_env(service, hands_home, tmp_path, monkeypatch):
    work = tmp_path / "work"
    repo = work / "repo-a"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("REMOTE_HANDS_ALLOWED_ROOTS", f"{work}/")
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    projects = tmp_path / "projects"
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    register(hands_home, service["bridge"], folders=(str(repo),))
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("fix the share image", encoding="utf-8")
    return {"repo": repo, "projects": projects, "prompt_file": prompt_file}


def start_args(env, *extra, request_id=RID, prompt_file=None):
    flags = ("--request-id", request_id) if request_id is not None else ()
    return (
        "start-claude",
        "--repo",
        str(env["repo"]),
        "--prompt-file",
        str(prompt_file or env["prompt_file"]),
        *flags,
        *extra,
    )


@pytest.fixture
def unreachable(monkeypatch):
    """Point the CLI at a closed port: anything that got sent would come back as service_down.

    List it after repo_env, which sets the URL of the live test service first.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("REMOTE_HANDS_URL", f"http://127.0.0.1:{port}/action")


class FakeHttp:
    """A one-connection HTTP server on a loopback port, for transport failures.

    `respond(conn, release)` runs after the whole request was read and stored in `received`,
    so a test can show the request reached the socket before the failure.
    """

    def __init__(self, respond):
        self.received = []
        self.release = threading.Event()
        self._respond = respond
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}/action"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            try:
                self.received.append(self._read_request(conn))
                self._respond(conn, self.release)
            except OSError:
                pass

    @staticmethod
    def _read_request(conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return data
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        match = re.search(rb"(?i)content-length: *(\d+)", head)
        wanted = int(match.group(1)) if match else 0
        while len(body) < wanted:
            chunk = conn.recv(65536)
            if not chunk:
                break
            body += chunk
        return head + b"\r\n\r\n" + body

    def close(self):
        self.release.set()
        self._listener.close()


@pytest.fixture
def fake_http():
    servers = []

    def make(respond):
        servers.append(FakeHttp(respond))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def silent(conn, release):
    release.wait(5)  # accepted, read, and never answered


def answer(body, status="200 OK", delay=0.0):
    def respond(conn, release):
        time.sleep(delay)
        head = f"HTTP/1.0 {status}\r\nContent-Length: {len(body)}\r\n\r\n"
        conn.sendall(head.encode("ascii") + body)

    return respond


def test_start_claude_via_cli_relays_the_first_reply(service, repo_env, capsys):
    seed_session(repo_env["projects"], session_id_for(RID), "fix the share image", "On it.")
    code, out = run(capsys, *start_args(repo_env, "--wait", "5"))
    assert code == 0
    assert out["detail"]["reply"]["first_reply"] == "On it."
    assert out["detail"]["session_id"] == session_id_for(RID)
    assert "session_id" not in out["detail"]["reply"]  # said once, in the detail
    (call,) = service["bridge"].starts
    assert call["body"]["cwd"] == str(repo_env["repo"])
    assert call["body"]["prompt"] == "fix the share image"


def test_start_claude_refuses_stdin_as_the_prompt_source(repo_env, unreachable, capsys):
    # A heredoc whose delimiter appears in the prompt would run the rest of it as shell.
    code, out = run(capsys, *start_args(repo_env, prompt_file="-"))
    assert code == 1
    assert out["status"] == "bad_request"  # service_down would mean something was sent
    assert out["detail"]["error"] == "write the prompt to a file; stdin is not accepted"


def test_start_claude_flags_a_record_without_the_prompt(service, repo_env, monkeypatch, capsys):
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    seed_session(repo_env["projects"], session_id_for(RID), "a different prompt", "reply")
    code, out = run(capsys, *start_args(repo_env, "--wait", "10"))
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["reply"]["prompt_found"] is False


def test_start_claude_shows_hidden_reply_characters_as_escapes(service, repo_env, capsys):
    # The calling agent reads this JSON; an invisible tag character in a reply must arrive as visible
    # escape text, never as a raw invisible character.
    seed_session(
        repo_env["projects"], session_id_for(RID), "fix the share image", "ok" + chr(0xE0041)
    )
    code = cli.main(list(start_args(repo_env, "--wait", "5")))
    raw = capsys.readouterr().out
    assert code == 0
    assert chr(0xE0041) not in raw
    assert "\\udb40\\udc41" in raw


def test_start_claude_flags_an_api_error_reply(service, repo_env, capsys):
    folder = repo_env["projects"] / "-home-user-src-repo-a"
    folder.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "fix the share image"}],
            },
        },
        {
            "type": "assistant",
            "isApiErrorMessage": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "API Error: overloaded"}],
            },
        },
    ]
    lines = "".join(json.dumps(record) + "\n" for record in records)
    (folder / f"{session_id_for(RID)}.jsonl").write_text(lines, encoding="utf-8")
    code, out = run(capsys, *start_args(repo_env, "--wait", "5"))
    assert code == 1
    assert out["status"] == "needs_human"
    assert "API error" in out["detail"]["error"]


def test_start_claude_with_no_record_after_the_wait_needs_a_human(
    service, repo_env, monkeypatch, capsys
):
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    code, out = run(capsys, *start_args(repo_env, "--wait", "10"))
    assert code == 1
    assert out["detail"]["reply"]["state"] == "not_found"
    assert out["detail"]["session_id"] == session_id_for(RID)


def test_start_claude_with_wait_zero_skips_read_back(service, repo_env, capsys):
    code, out = run(capsys, *start_args(repo_env, "--wait", "0"))
    assert code == 0
    assert "reply" not in out["detail"]


def test_start_claude_waits_for_the_first_reply_by_default(service, repo_env, monkeypatch, capsys):
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    code, out = run(capsys, *start_args(repo_env))  # no --wait, and no record ever appears
    assert code == 1
    assert out["detail"]["reply"]["state"] == "not_found"
    assert 120 <= clock.now < 120 + 2.5  # it polled for the whole default wait, 120 s


def test_start_claude_accepts_the_longest_wait(service, repo_env, monkeypatch, capsys):
    clock = Clock()  # a regression must fail fast, never wait 300 real seconds
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    seed_session(repo_env["projects"], session_id_for(RID), "fix the share image", "On it.")
    code, out = run(capsys, *start_args(repo_env, "--wait", "300"))
    assert code == 0
    assert out["detail"]["reply"]["first_reply"] == "On it."


def test_start_claude_refuses_a_folder_that_is_not_a_repo(repo_env, unreachable, tmp_path, capsys):
    plain = tmp_path / "work" / "plain"
    plain.mkdir()
    code, out = run(capsys, *start_args({**repo_env, "repo": plain}))
    assert code == 1
    assert out["status"] == "bad_request"  # service_down would mean something was sent
    assert "not a git repository" in out["detail"]["error"]


def test_start_claude_needs_wsl(repo_env, unreachable, monkeypatch, capsys):
    monkeypatch.delenv("WSL_DISTRO_NAME")
    code, out = run(capsys, *start_args(repo_env))
    assert code == 1
    assert out["status"] == "bad_request"
    assert "WSL_DISTRO_NAME is not set" in out["detail"]["error"]


@pytest.mark.parametrize("extra", [("--wait", "0"), ("--wait", "5")])
def test_start_claude_refuses_a_relative_session_folder_before_starting(
    service, repo_env, monkeypatch, capsys, extra
):
    # The folder is needed to read the session back, so a bad one is refused before anything
    # starts, never discovered after a session is already running.
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "relative/dir")
    code, out = run(capsys, *start_args(repo_env, *extra))
    assert code == 1
    assert out["status"] == "bad_request"
    assert "CLAUDE_PROJECTS_DIR" in out["detail"]["error"]
    assert service["bridge"].starts == []


def test_read_back_via_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    seed_session(tmp_path, session_id_for(RID), "fix it", "On it.")
    code, out = run(capsys, "read-back", "--session", session_id_for(RID))
    assert code == 0
    assert out["detail"]["first_reply"] == "On it."


def test_read_back_of_an_api_error_reply_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    folder = tmp_path / "-home-user-src-repo-a"
    folder.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": "fix it"}]},
        },
        {
            "type": "assistant",
            "isApiErrorMessage": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "API Error: overloaded"}],
            },
        },
    ]
    lines = "".join(json.dumps(record) + "\n" for record in records)
    (folder / f"{session_id_for(RID)}.jsonl").write_text(lines, encoding="utf-8")
    code, out = run(capsys, "read-back", "--session", session_id_for(RID))
    assert code == 1
    assert out["detail"]["reply_is_error"] is True


def test_start_claude_with_a_record_missing_the_prompt_needs_a_human(
    service, repo_env, monkeypatch, capsys
):
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    folder = repo_env["projects"] / "-home-user-src-repo-a"
    folder.mkdir(parents=True, exist_ok=True)
    queued = {"type": "queue-operation", "operation": "enqueue", "sessionId": session_id_for(RID)}
    (folder / f"{session_id_for(RID)}.jsonl").write_text(
        json.dumps(queued) + "\n", encoding="utf-8"
    )
    code, out = run(capsys, *start_args(repo_env, "--wait", "10"))
    assert code == 1
    assert out["detail"]["reply"]["prompt_found"] is False


def test_read_back_of_an_unknown_session_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    code, out = run(capsys, "read-back", "--session", session_id_for(RID))
    assert code == 1
    assert out["detail"]["state"] == "not_found"


def test_read_back_refuses_a_bad_session_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    code, out = run(capsys, "read-back", "--session", "../x")
    assert code == 1
    assert out["status"] == "bad_request"


def test_windows_lists_registrations(service, hands_home, capsys):
    register(hands_home, service["bridge"], folders=("/home/user/src/repo-a",))
    code, out = run(capsys, "windows")
    assert code == 0
    assert out["detail"]["windows"][0]["folders"] == ["/home/user/src/repo-a"]


def test_off_waits_for_in_flight_actions(service, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    tracker = service["ctx"].inflight.track("start_claude")
    tracker.__enter__()
    finished = []

    def finish_during_wait(seconds):
        if not finished:
            tracker.__exit__(None, None, None)
            finished.append(seconds)

    monkeypatch.setattr(cli, "_sleep", finish_during_wait)
    code, out = run(capsys, "off")
    assert code == 0
    assert out["detail"]["in_flight"] == []
    assert finished == [cli.OFF_POLL_SECONDS]
    assert "last_error" not in out["detail"]  # confirmed and known: nothing to explain


def test_off_needs_a_human_when_the_service_state_is_unknown(service, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)

    def broken_state():
        raise RuntimeError("desktop query failed")

    monkeypatch.setattr(service["desktop"], "state", broken_state)
    code, out = run(capsys, "off")
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["in_flight"] is None
    assert "desktop query failed" in out["detail"]["last_error"]  # the service's own reason
    assert clock.now >= cli.OFF_DRAIN_SECONDS  # it kept asking until the deadline


def test_off_recovers_when_an_unknown_state_clears(service, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    clock = Clock()
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    real_state = service["desktop"].state

    def broken_state():
        raise RuntimeError("desktop query failed")

    def heal_during_wait(seconds):
        clock.sleep(seconds)
        monkeypatch.setattr(service["desktop"], "state", real_state)

    monkeypatch.setattr(service["desktop"], "state", broken_state)
    monkeypatch.setattr(cli, "_sleep", heal_during_wait)
    code, out = run(capsys, "off")
    assert code == 0
    assert out["detail"]["in_flight"] == []


def test_off_with_nothing_listening_is_ok(hands_home, monkeypatch, capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("REMOTE_HANDS_URL", f"http://127.0.0.1:{port}/action")
    code, out = run(capsys, "off")
    assert code == 0
    assert out["detail"]["service_confirms"] is None
    assert out["detail"]["in_flight"] == []


def test_off_reports_actions_that_never_finish(service, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    with service["ctx"].inflight.track("start_claude"):
        code, out = run(capsys, "off")
    assert code == 1
    assert out["detail"]["in_flight"] == ["start_claude"]
    assert clock.now >= cli.OFF_DRAIN_SECONDS


def test_call_marks_only_a_refused_connection_as_refused(monkeypatch):
    failures = [
        (ConnectionRefusedError(111, "refused"), True),
        (TimeoutError("timed out"), False),
        (ConnectionResetError(104, "reset"), False),
        (OSError(113, "no route to host"), False),
    ]
    for failure, refused in failures:

        def fake_connect(self, failure=failure):
            raise failure

        monkeypatch.setattr(http.client.HTTPConnection, "connect", fake_connect)
        result = cli.call("health", {}, url="http://127.0.0.1:9/action", token="t", timeout=1)
        assert result["status"] == "service_down"  # a failed connect: nothing was sent
        assert result["detail"]["refused"] is refused


@pytest.mark.parametrize(
    ("connect_timeout", "timeout", "expected"),
    [(5.0, 150.0, 5.0), (0.5, 10.0, 0.5), (5.0, 1.0, 1.0), (None, 150.0, 5.0)],
    ids=["connect-cap", "explicit", "reply-shorter", "default-is-5-seconds"],
)
def test_call_connects_with_its_own_short_timeout(monkeypatch, connect_timeout, timeout, expected):
    seen = []

    def fake_connect(self):
        seen.append(self.timeout)
        raise ConnectionRefusedError(111, "refused")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fake_connect)
    extra = {} if connect_timeout is None else {"connect_timeout": connect_timeout}
    cli.call("health", {}, url="http://127.0.0.1:9/action", token="t", timeout=timeout, **extra)
    assert seen == [expected]


def test_call_gives_the_reply_its_own_longer_timeout(fake_http):
    body = b'{"status": "ok", "request_id": "r", "detail": {}}'
    server = fake_http(answer(body, delay=0.2))
    result = cli.call("health", {}, url=server.url, token="t", timeout=5.0, connect_timeout=0.05)
    assert result["status"] == "ok"  # the slow reply was not cut off at the connect timeout


def test_call_returns_the_services_reply_untouched(fake_http):
    reply = {"status": "ok", "request_id": "r1", "detail": {"a": 1}}
    server = fake_http(answer(json.dumps(reply).encode()))
    assert cli.call("health", {}, url=server.url, token="t", timeout=2) == reply


def test_call_still_returns_the_services_own_error_replies(fake_http):
    server = fake_http(answer(b'{"status": "unauthorized"}', "401 Unauthorized"))
    result = cli.call("health", {}, url=server.url, token="t", timeout=2)
    assert result == {"status": "unauthorized", "http_status": 401}


def test_call_reports_a_reply_that_never_comes_as_maybe_sent(fake_http):
    server = fake_http(silent)
    result = cli.call(
        "start_claude", {"x": 1}, url=server.url, token="t", timeout=0.3, request_id=RID
    )
    assert result["status"] == "needs_human"
    assert result["request_id"] == RID
    assert result["detail"]["step"] == "transport"
    assert result["detail"]["sent"] is True
    assert "timed out" in result["detail"]["error"]
    assert b'"start_claude"' in server.received[0]  # the request did reach the service


@pytest.mark.parametrize(
    ("status_line", "body"),
    [
        ("200 OK", b"not json at all"),
        ("200 OK", b"[1, 2]"),
        ("200 OK", b""),
        ("200 OK", b'{"detail": {}}'),
        ("200 OK", b"[" * 60_000),
        ("404 Not Found", b"<html>no such page</html>"),
    ],
    ids=["not-json", "not-an-object", "empty", "no-status", "too-deeply-nested", "html-error"],
)
def test_call_reports_an_unusable_answer_as_maybe_sent(fake_http, status_line, body):
    server = fake_http(answer(body, status_line))
    result = cli.call(
        "start_claude", {"x": 1}, url=server.url, token="t", timeout=2, request_id=RID
    )
    assert result["status"] == "needs_human"
    assert result["request_id"] == RID
    detail = result["detail"]
    assert (detail["step"], detail["sent"]) == ("transport", True)
    assert detail["http_status"] == int(status_line[:3])
    assert server.received  # the request reached the service


def test_start_claude_when_the_reply_never_comes_may_have_started_a_session(
    repo_env, fake_http, monkeypatch, capsys
):
    server = fake_http(silent)
    monkeypatch.setenv("REMOTE_HANDS_URL", server.url)
    monkeypatch.setattr(cli, "START_TIMEOUT_SECONDS", 0.3)
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    code, out = run(capsys, *start_args(repo_env, "--wait", "5"))
    assert code == 1
    assert out["status"] == "needs_human"
    detail = out["detail"]
    assert (detail["step"], detail["sent"]) == ("transport", True)
    assert detail["session_id"] == session_id_for(RID)
    assert "same --request-id" in detail["error"]
    assert "TimeoutError" in detail["transport_error"]
    assert "reply" not in detail  # no read-back after a request that may have started something
    assert clock.now == 0
    assert b'"start_claude"' in server.received[0]


def test_start_claude_adds_the_session_id_when_the_answer_cannot_be_used(
    repo_env, fake_http, monkeypatch, capsys
):
    server = fake_http(answer(b"<html>proxy error</html>"))
    monkeypatch.setenv("REMOTE_HANDS_URL", server.url)
    code, out = run(capsys, *start_args(repo_env, "--wait", "0"))
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["step"] == "transport"
    assert out["detail"]["session_id"] == session_id_for(RID)


def test_start_claude_with_nothing_listening_is_service_down_with_nothing_sent(
    repo_env, unreachable, capsys
):
    code, out = run(capsys, *start_args(repo_env))
    assert code == 1
    assert out["status"] == "service_down"
    assert out["detail"]["refused"] is True
    assert "session_id" not in out["detail"]  # nothing was sent: a plain retry is safe


def test_start_claude_requires_a_request_id(service, repo_env, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(list(start_args(repo_env, request_id=None)))
    assert stop.value.code == 2
    assert "--request-id" in capsys.readouterr().err
    assert service["bridge"].starts == []


LOCAL_REFUSALS = [
    pytest.param({"request_id": ""}, "request_id must be", id="empty-request-id"),
    pytest.param({"request_id": "Short"}, "request_id must be", id="bad-request-id"),
    pytest.param({"extra": ("--label=-bad",)}, "label must be", id="bad-label"),
    pytest.param({"extra": ("--label", "")}, "label must be", id="empty-label"),
    pytest.param({"prompt": "/status"}, "must not start with", id="command-prompt"),
    pytest.param({"prompt": "-x hello"}, "must not start with", id="option-like-prompt"),
    pytest.param({"prompt": "update"}, "at least two words", id="one-word-prompt"),
    pytest.param(
        {"prompt": "fix ${env:HOME} now"}, "must not contain ${", id="variable-syntax-prompt"
    ),
    pytest.param({"prompt": "   "}, "non-empty", id="blank-prompt"),
    pytest.param({"prompt": "a" * 16_001}, "at most 16000", id="long-prompt"),
    pytest.param({"prompt": "hi" + chr(0x200B)}, "invisible", id="hidden-character"),
    pytest.param({"prompt": "a" * 64_001}, "prompt file is larger than", id="huge-prompt-file"),
    pytest.param({"prompt_bytes": b"\xff\xfe"}, "codec", id="not-utf8"),
    pytest.param({"extra": ("--wait", "301")}, "--wait must be", id="wait-too-long"),
    pytest.param({"extra": ("--wait", "-1")}, "--wait must be", id="negative-wait"),
    pytest.param({"distro": "Ubuntu 24"}, "remote must look like", id="distro-with-a-space"),
    pytest.param({"distro": "Ubuntu-" + chr(0x212A)}, "ASCII", id="non-ascii-distro"),
]


@pytest.mark.parametrize(("case", "needle"), LOCAL_REFUSALS)
def test_start_claude_validates_locally_before_sending(
    repo_env, unreachable, monkeypatch, capsys, case, needle
):
    if "prompt" in case:
        repo_env["prompt_file"].write_text(case["prompt"], encoding="utf-8")
    if "prompt_bytes" in case:
        repo_env["prompt_file"].write_bytes(case["prompt_bytes"])
    if "distro" in case:
        monkeypatch.setenv("WSL_DISTRO_NAME", case["distro"])
    argv = start_args(repo_env, *case.get("extra", ()), request_id=case.get("request_id", RID))
    code, out = run(capsys, *argv)
    assert code == 1
    assert out["status"] == "bad_request"  # service_down would mean something was sent
    assert needle in out["detail"]["error"]


def test_the_prompt_file_bound_is_four_bytes_per_allowed_character():
    assert cli.MAX_PROMPT_BYTES == start_request.MAX_PROMPT_CHARS * 4


def test_start_claude_accepts_a_prompt_of_the_largest_allowed_size(service, repo_env, capsys):
    # 16,000 characters: 15,999 of 4 UTF-8 bytes each and one 3-byte space between two words. A
    # legal prompt needs a separator and no space takes 4 bytes, so the largest one is a byte
    # under the bound.
    prompt = chr(0x1D538) * 8_000 + chr(0x3000) + chr(0x1D538) * 7_999
    assert len(prompt) == start_request.MAX_PROMPT_CHARS
    repo_env["prompt_file"].write_text(prompt, encoding="utf-8")
    assert repo_env["prompt_file"].stat().st_size == cli.MAX_PROMPT_BYTES - 1
    code, _ = run(capsys, *start_args(repo_env, "--wait", "0"))
    assert code == 0
    assert service["bridge"].starts[0]["body"]["prompt"] == prompt


def test_start_claude_does_not_read_back_after_a_refused_start(
    service, repo_env, hands_home, monkeypatch, capsys
):
    (hands_home / "OFF").touch()
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    code, out = run(capsys, *start_args(repo_env, "--wait", "5"))
    assert code == 1
    assert out["status"] == "service_off"
    assert "reply" not in out.get("detail", {})
    assert clock.now == 0
    assert service["bridge"].starts == []


@pytest.mark.parametrize("wait", ["0", "5"])
@pytest.mark.parametrize(
    "detail",
    [
        {},
        {"session_id": "../x"},
        {"session_id": "ABCDEF12-0000-4000-8000-000000000000"},
        {"session_id": 7},
    ],
    ids=["missing", "path-like", "upper-case", "not-text"],
)
def test_start_claude_needs_a_human_when_an_ok_result_has_no_valid_session_id(
    repo_env, monkeypatch, capsys, wait, detail
):
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    fake = {"status": "ok", "request_id": RID, "detail": detail}
    monkeypatch.setattr(cli, "call", lambda *args, **kwargs: fake)
    code, out = run(capsys, *start_args(repo_env, "--wait", wait))
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["error"] == "the service did not return a session id"
    assert "reply" not in out["detail"]
    assert clock.now == 0


@pytest.mark.parametrize(
    ("command", "status"),
    [("off", "needs_human"), ("on", "needs_human"), ("windows", "bad_request")],
)
def test_a_bad_home_prints_json_not_a_traceback(monkeypatch, capsys, command, status):
    # off and on: the request was valid and the environment failed, so a human has to look.
    monkeypatch.setenv("REMOTE_HANDS_HOME", "relative/home")
    code, out = run(capsys, command)
    assert code == 1
    assert out["status"] == status
    assert out["request_id"] == "local"
    assert "REMOTE_HANDS_HOME" in out["detail"]["error"]


@pytest.mark.parametrize(("command", "said"), [("off", "NOT set"), ("on", "NOT cleared")])
def test_a_switch_that_cannot_be_changed_says_so_plainly(
    tmp_path, monkeypatch, capsys, command, said
):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a folder is needed", encoding="utf-8")
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(blocker / "home"))
    code, out = run(capsys, command)
    assert code == 1
    assert out["status"] == "needs_human"  # valid request, failed environment
    assert said in out["detail"]["error"]


def test_off_treats_a_service_that_does_not_answer_as_unknown(service, monkeypatch, capsys):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    timed_out = {
        "status": "service_down",
        "request_id": "x",
        "detail": {"error": "timed out", "refused": False},
    }
    monkeypatch.setattr(cli, "call", lambda *args, **kwargs: timed_out)
    code, out = run(capsys, "off")
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["in_flight"] is None
    assert out["detail"]["last_error"] == "timed out"


def test_start_claude_passes_the_label_through(service, repo_env, capsys):
    code, _ = run(capsys, *start_args(repo_env, "--label", "phone fix", "--wait", "0"))
    assert code == 0
    assert service["bridge"].starts[0]["body"]["label"] == "phone fix"


def test_the_prompt_file_is_never_read_past_the_bound(repo_env, unreachable, monkeypatch, capsys):
    sizes = []

    class Endless:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size=-1):
            sizes.append(size)
            if size is None or size < 0:
                raise AssertionError("read the whole file with no bound")
            return b"a" * size

    monkeypatch.setattr(cli, "open", lambda *args, **kwargs: Endless(), raising=False)
    code, out = run(capsys, *start_args(repo_env))
    assert code == 1
    assert out["status"] == "bad_request"
    assert "prompt file is larger than" in out["detail"]["error"]
    assert sizes == [cli.MAX_PROMPT_BYTES + 1]  # one byte past the bound is enough to refuse


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"status": "needs_human", "request_id": "x", "detail": {}}, "the service answered"),
        (
            {"status": "ok", "request_id": "x", "detail": {"kill_switch": True}},
            "health did not report the actions in flight",
        ),
        (
            {"status": "needs_human", "request_id": "x", "detail": {"error": "audit log full"}},
            "audit log full",
        ),
    ],
    ids=["no-reason-given", "no-in-flight-list", "the-services-own-reason"],
)
def test_off_says_why_an_answer_was_not_usable(service, monkeypatch, capsys, reply, expected):
    monkeypatch.setenv("REMOTE_HANDS_URL", service["url"])
    clock = Clock()
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    monkeypatch.setattr(cli, "_monotonic", clock.monotonic)
    monkeypatch.setattr(cli, "call", lambda *args, **kwargs: reply)
    code, out = run(capsys, "off")
    assert code == 1
    assert out["status"] == "needs_human"
    assert out["detail"]["in_flight"] is None
    assert expected in out["detail"]["last_error"]


def main_within(seconds, argv, release=None):
    """Run cli.main in a thread, so a hang fails the test instead of freezing the suite."""
    box = {}

    def attempt():
        box["code"] = cli.main(argv)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        if release is not None:
            release()  # frees a thread stuck in a blocking open, so it does not leak
            worker.join(seconds)
        pytest.fail(f"hands did not return within {seconds} s")
    return box["code"]


def special_prompt_file(kind, tmp_path):
    if kind == "devnull":
        return "/dev/null"
    if kind == "directory":
        folder = tmp_path / "a-folder"
        folder.mkdir()
        return str(folder)
    fifo = tmp_path / "prompt.fifo"
    os.mkfifo(fifo)  # nobody ever opens the other end
    return str(fifo)


@pytest.mark.parametrize("kind", ["devnull", "directory", "fifo"])
def test_start_claude_refuses_a_prompt_file_that_is_not_a_regular_file(
    repo_env, unreachable, tmp_path, capsys, kind
):
    target = special_prompt_file(kind, tmp_path)

    def release():  # a reader stuck opening a pipe with no writer is freed by a writer
        try:
            os.close(os.open(target, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass

    code = main_within(5, list(start_args(repo_env, prompt_file=target)), release)
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["status"] == "bad_request"  # service_down would mean something was sent
    assert out["detail"]["error"] == (
        "the prompt file must be a regular file "
        "(write the prompt to a file; pipes and devices are not accepted)"
    )


def test_start_claude_follows_a_link_to_the_prompt_file(service, repo_env, tmp_path, capsys):
    link = tmp_path / "prompt-link.txt"
    link.symlink_to(repo_env["prompt_file"])
    code, _ = run(capsys, *start_args(repo_env, "--wait", "0", prompt_file=link))
    assert code == 0
    assert service["bridge"].starts[0]["body"]["prompt"] == "fix the share image"


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="counts open descriptors")
def test_the_prompt_file_is_closed_whether_it_was_read_or_refused(tmp_path):
    good = tmp_path / "good.txt"
    good.write_text("hello", encoding="utf-8")
    too_big = tmp_path / "too-big.txt"
    too_big.write_bytes(b"a" * (cli.MAX_PROMPT_BYTES + 1))
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(20):
        assert cli._read_prompt(str(good)) == "hello"
        for refused, why in (("/dev/null", "regular file"), (str(tmp_path), "regular file")):
            with pytest.raises(ValueError, match=why):
                cli._read_prompt(refused)
        with pytest.raises(ValueError, match="larger than"):
            cli._read_prompt(str(too_big))
    assert len(os.listdir("/proc/self/fd")) == before


def test_a_malformed_flag_exits_2_with_usage_and_sends_nothing(service, repo_env, capsys):
    with pytest.raises(SystemExit) as stop:
        cli.main(list(start_args(repo_env, "--wait", "soon")))
    assert stop.value.code == 2
    assert "usage:" in capsys.readouterr().err
    assert service["bridge"].starts == []


def test_the_module_docstring_states_the_usage_error_contract():
    flat = " ".join(cli.__doc__.split())
    assert "Missing or malformed flags exit 2 with usage on stderr; nothing is sent." in flat
