import errno
import http.client
import json

import pytest

from remote_hands.registry import find_window, load_registrations

REPO = "/home/user/src/repo-a"
OTHER_REPO = "/home/user/src/repo-b"
INST_A = "11111111-2222-4333-8444-555555555555"
INST_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def write_reg(directory, instance, port, folders):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{instance}.json"
    record = {"instance": instance, "port": port, "folders": folders, "pid": 1, "version": "0.1.0"}
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


class FakePinger:
    def __init__(self, answers):
        self.answers = answers

    def ping(self, port):
        answer = self.answers.get(port)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def test_live_window_with_the_repo_is_found(tmp_path):
    write_reg(tmp_path, INST_A, 5001, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: INST_A}))
    assert lookup.window.instance == INST_A
    assert lookup.window.port == 5001
    assert lookup.busy == ()
    assert lookup.removed == ()


def test_parent_folder_does_not_count(tmp_path):
    write_reg(tmp_path, INST_A, 5001, ["/home/user"])
    assert find_window(tmp_path, REPO, FakePinger({5001: INST_A})).window is None


@pytest.mark.parametrize("folder", [REPO + "/sub", REPO + "b"])
def test_child_or_prefix_sibling_folder_does_not_count(tmp_path, folder):
    write_reg(tmp_path, INST_A, 5001, [folder])
    assert find_window(tmp_path, REPO, FakePinger({5001: INST_A})).window is None


def test_dead_registration_is_removed(tmp_path):
    path = write_reg(tmp_path, INST_A, 5001, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: None}))
    assert lookup.window is None
    assert lookup.removed == (INST_A,)
    assert not path.exists()


def test_port_taken_by_another_bridge_is_stale(tmp_path):
    path = write_reg(tmp_path, INST_A, 5001, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: INST_B}))
    assert lookup.window is None
    assert lookup.removed == (INST_A,)
    assert not path.exists()


def test_busy_bridge_is_skipped_but_kept(tmp_path):
    path = write_reg(tmp_path, INST_A, 5001, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: TimeoutError()}))
    assert lookup.window is None
    assert lookup.busy == (INST_A,)
    assert lookup.removed == ()
    assert path.exists()


def test_busy_window_is_skipped_and_the_next_live_one_used(tmp_path):
    busy = write_reg(tmp_path, INST_A, 5001, [REPO])
    write_reg(tmp_path, INST_B, 5002, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: TimeoutError(), 5002: INST_B}))
    assert lookup.window.instance == INST_B
    assert lookup.busy == (INST_A,)
    assert busy.exists()


def test_second_live_window_is_used_when_the_first_is_dead(tmp_path):
    write_reg(tmp_path, INST_A, 5001, [REPO])
    write_reg(tmp_path, INST_B, 5002, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: None, 5002: INST_B}))
    assert lookup.window.instance == INST_B
    assert lookup.removed == (INST_A,)


def test_registration_removed_during_the_ping_does_not_crash(tmp_path):
    path = write_reg(tmp_path, INST_A, 5001, [REPO])

    class Vanishing:
        def ping(self, port):
            path.unlink()
            return None

    lookup = find_window(tmp_path, REPO, Vanishing())
    assert lookup.window is None
    assert lookup.removed == ()  # the delete failed (the file was already gone): not reported


def test_scan_stops_at_the_first_live_window(tmp_path):
    write_reg(tmp_path, INST_A, 5001, [REPO])
    write_reg(tmp_path, INST_B, 5002, [REPO])
    pinger = FakePinger({5001: INST_A, 5002: AssertionError("scanned past the live window")})
    lookup = find_window(tmp_path, REPO, pinger)
    assert lookup.window.instance == INST_A
    assert lookup.busy == ()
    assert lookup.removed == ()


def test_single_folder_window_is_preferred_over_a_multi_root_one(tmp_path):
    # The multi-root file name sorts first, so only the single-folder rule can put B ahead.
    write_reg(tmp_path, INST_A, 5001, [REPO, OTHER_REPO])
    write_reg(tmp_path, INST_B, 5002, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: INST_A, 5002: INST_B}))
    assert lookup.window.instance == INST_B


def test_multi_root_window_is_used_when_the_single_folder_one_is_dead(tmp_path):
    write_reg(tmp_path, INST_A, 5001, [REPO, OTHER_REPO])
    write_reg(tmp_path, INST_B, 5002, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: INST_A, 5002: None}))
    assert lookup.window.instance == INST_A
    assert lookup.removed == (INST_B,)


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        json.dumps([]),
        json.dumps({"instance": "nope", "port": 5001, "folders": [REPO]}),
        json.dumps({"instance": INST_A, "port": True, "folders": [REPO]}),
        json.dumps({"instance": INST_A, "port": 70000, "folders": [REPO]}),
        json.dumps({"instance": INST_A, "port": 5001, "folders": REPO}),
    ],
)
def test_malformed_registrations_are_ignored(tmp_path, payload):
    (tmp_path / "bad.json").write_text(payload, encoding="utf-8")
    assert load_registrations(tmp_path) == []


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps({"instance": 5, "port": 5001, "folders": [REPO]}),
        json.dumps({"instance": INST_A + "x", "port": 5001, "folders": [REPO]}),
        json.dumps({"instance": INST_A, "port": 0, "folders": [REPO]}),
        json.dumps({"instance": INST_A, "port": 5001, "folders": [REPO, 5]}),
    ],
)
def test_more_malformed_registrations_are_ignored(tmp_path, payload):
    (tmp_path / "bad.json").write_text(payload, encoding="utf-8")
    assert load_registrations(tmp_path) == []


@pytest.mark.parametrize("depth", [20_000, 100_000])
def test_registration_nested_too_deeply_to_parse_is_ignored(tmp_path, depth):
    (tmp_path / "deep.json").write_text("[" * depth, encoding="utf-8")
    assert load_registrations(tmp_path) == []
    assert find_window(tmp_path, REPO, FakePinger({})).window is None


@pytest.mark.parametrize(("port", "kept"), [(0, False), (1, True), (65535, True), (65536, False)])
def test_port_boundaries(tmp_path, port, kept):
    write_reg(tmp_path, INST_A, port, [REPO])
    assert [reg.port for reg in load_registrations(tmp_path)] == ([port] if kept else [])


def test_missing_directory_means_no_windows(tmp_path):
    assert load_registrations(tmp_path / "nope") == []


def test_a_refused_connection_raised_by_the_pinger_is_stale_too(tmp_path):
    path = write_reg(tmp_path, INST_A, 5001, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: ConnectionRefusedError()}))
    assert lookup.window is None
    assert lookup.removed == (INST_A,)
    assert not path.exists()


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        http.client.BadStatusLine("garbage"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
    ],
    ids=["timeout", "reset", "remote-disconnected", "protocol-error", "other-oserror"],
)
def test_a_ping_that_fails_any_other_way_is_busy_and_the_registration_is_kept(tmp_path, failure):
    # Only a refused connection or an answer that is not this window's says the window is gone: a
    # transient hiccup on the loopback must never delete a registration (C4: stop and report).
    path = write_reg(tmp_path, INST_A, 5001, [REPO])
    lookup = find_window(tmp_path, REPO, FakePinger({5001: failure}))
    assert lookup.window is None
    assert lookup.busy == (INST_A,)
    assert lookup.removed == ()
    assert path.exists()
