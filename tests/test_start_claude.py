import http.client
import json
import os

import pytest
from conftest import TOKEN
from fakes import INSTANCE, REMOTE, REPO, Clock, FakeBridge, register

from remote_hands.actions import (
    BOOT_SLACK_SECONDS,
    IDLE_GUARD_SECONDS,
    WINDOW_POLL_SECONDS,
    WINDOW_WAIT_SECONDS,
    Context,
    dispatch,
)
from remote_hands.bridge_client import REFUSED, STARTED, UNKNOWN, UNREACHABLE, BridgeReply
from remote_hands.contract import (
    BAD_REQUEST,
    NEEDS_HUMAN,
    OK,
    SERVICE_OFF,
    USER_ACTIVE,
    ActionRequest,
)
from remote_hands.desktop import FakeDesktop
from remote_hands.journal import CLAIMED, FULFILLED, NOT_SUBMITTED, SUBMITTING, Journal
from remote_hands.journal import UNKNOWN as JOURNAL_UNKNOWN
from remote_hands.start_request import session_id_for

RID = "req-0001-abcd"


def make(tmp_path, *, desktop=None, bridge=None):
    clock = Clock()
    bridge = bridge or FakeBridge()
    ctx = Context(
        desktop=desktop or FakeDesktop(),
        kill_switch=tmp_path / "OFF",
        sleep=clock.sleep,
        state_dir=tmp_path,
        token=TOKEN,
        bridge=bridge,
        monotonic=clock.monotonic,
    )
    return ctx, bridge, clock


def start(rid=RID, **changes):
    args = {"repo": REPO, "remote": REMOTE, "prompt": "fix the share image", **changes}
    return ActionRequest(action="start_claude", request_id=rid, args=args)


def journal_state(tmp_path):
    entry = Journal(tmp_path / "journal").read(RID)
    return None if entry is None else entry.state


def journal_text(tmp_path):
    """Everything the journal stored, as text."""
    return "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "journal").glob("*.json"))


def test_starts_claude_in_the_open_window(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.status == OK
    assert res.detail["session_id"] == session_id_for(RID)
    assert res.detail["window"] == INSTANCE
    assert res.detail["opened_window"] is False
    assert res.detail["terminal_pid"] == 4242
    assert res.detail["journal_state"] == FULFILLED
    assert res.detail["bridge_status"] == STARTED
    assert "args_sha256" not in res.detail
    (call,) = bridge.starts
    assert call["port"] == 5001
    assert call["token"] == TOKEN
    assert call["body"] == {
        "request_id": RID,
        "session_id": session_id_for(RID),
        "cwd": REPO,
        "label": "hands repo-a req-00",
        "prompt": "fix the share image",
    }
    assert journal_state(tmp_path) == FULFILLED
    assert ctx.desktop.opened == []


def test_works_while_the_pc_is_locked(tmp_path):
    ctx, bridge, _ = make(tmp_path, desktop=FakeDesktop(locked=True, idle_seconds=0.0))
    register(tmp_path, bridge)
    assert dispatch(start(), ctx).status == OK


def test_same_request_id_never_starts_twice(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    first = dispatch(start(), ctx)
    again = dispatch(start(), ctx)
    assert again.status == OK
    assert again.detail["replayed"] is True
    assert again.detail["session_id"] == first.detail["session_id"]
    assert len(bridge.starts) == 1


def test_opens_a_window_when_none_has_the_repo(tmp_path):
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)
    desktop.on_open = lambda uri: register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.status == OK
    assert res.detail["opened_window"] is True
    assert desktop.opened == ["vscode-remote://wsl%2Bubuntu-24.04/home/user/src/repo-a"]


def test_waits_for_the_new_window_to_register(tmp_path):
    ctx, bridge, clock = make(tmp_path, desktop=FakeDesktop(idle_seconds=600))

    def sleep_then_register(seconds):
        clock.sleep(seconds)
        if clock.now >= 6:
            register(tmp_path, bridge)

    ctx.sleep = sleep_then_register
    assert dispatch(start(), ctx).status == OK
    assert clock.now == 6


def test_wait_keeps_polling_while_the_new_bridge_is_busy(tmp_path):
    ctx, bridge, clock = make(tmp_path, desktop=FakeDesktop(idle_seconds=600))
    stale = "00000000-0000-4000-8000-000000000000"

    def sleep_then_change(seconds):
        clock.sleep(seconds)
        if clock.now == 2:
            register(tmp_path, bridge)
            bridge.instances[5001] = TimeoutError()  # the new bridge is still starting
            register(tmp_path, bridge, port=5002, instance=stale)
            bridge.instances[5002] = None  # and a dead registration appears
        if clock.now == 4:
            bridge.instances[5001] = INSTANCE  # now it answers

    ctx.sleep = sleep_then_change
    res = dispatch(start(), ctx)
    assert res.status == OK
    assert res.detail["opened_window"] is True
    assert res.detail["removed_windows"] == [stale]
    assert clock.now == 4


def test_new_window_that_stays_busy_is_reported_as_busy(tmp_path):
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, clock = make(tmp_path, desktop=desktop)

    def register_busy(uri):
        register(tmp_path, bridge)
        bridge.instances[5001] = TimeoutError()

    desktop.on_open = register_busy
    # The scan before the launch sees no registration, so the folder is opened.
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "window_busy"
    assert res.detail["opened_window"] is True
    assert clock.now >= WINDOW_WAIT_SECONDS
    assert journal_state(tmp_path) == NOT_SUBMITTED


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ConnectionResetError(104, "Connection reset by peer"),
        http.client.BadStatusLine("garbage"),
        OSError(113, "No route to host"),
    ],
    ids=["timeout", "reset", "protocol-error", "other-oserror"],
)
def test_a_window_whose_ping_fails_unclearly_stops_the_start_and_opens_nothing(tmp_path, failure):
    # The user is idle, so a repo with no window would get one opened; a window whose ping failed for any
    # reason but a refusal is kept and reported busy instead (C4), however the ping failed.
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)
    register(tmp_path, bridge)
    bridge.instances[5001] = failure
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "window_busy"
    assert res.detail["windows"] == [INSTANCE]
    assert res.detail["journal_state"] == NOT_SUBMITTED
    assert desktop.opened == []
    assert bridge.starts == []
    assert (tmp_path / "windows" / f"{INSTANCE}.json").exists()


def test_a_window_whose_ping_is_refused_is_removed_and_a_window_is_opened(tmp_path):
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)
    register(tmp_path, bridge)
    bridge.instances[5001] = ConnectionRefusedError(111, "Connection refused")
    dispatch(start(), ctx)
    assert not (tmp_path / "windows" / f"{INSTANCE}.json").exists()
    assert len(desktop.opened) == 1


def test_gives_up_when_the_new_window_never_registers(tmp_path):
    ctx, bridge, clock = make(tmp_path, desktop=FakeDesktop(idle_seconds=600))
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "window"
    assert clock.now >= WINDOW_WAIT_SECONDS
    assert bridge.starts == []
    assert journal_state(tmp_path) == NOT_SUBMITTED


def test_never_opens_a_window_while_the_user_is_active(tmp_path):
    desktop = FakeDesktop(idle_seconds=IDLE_GUARD_SECONDS - 1)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)
    res = dispatch(start(), ctx)
    assert res.status == USER_ACTIVE
    assert res.detail["journal_state"] == NOT_SUBMITTED
    assert res.detail["session_locked"] is False
    assert desktop.opened == []
    # Nothing was sent, so the same request may run again once the user steps away.
    desktop.idle_seconds = 600
    desktop.on_open = lambda uri: register(tmp_path, bridge)
    assert dispatch(start(), ctx).status == OK


def test_opens_a_window_on_a_locked_pc(tmp_path):
    desktop = FakeDesktop(locked=True, idle_seconds=0.0)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)
    desktop.on_open = lambda uri: register(tmp_path, bridge)
    assert dispatch(start(), ctx).status == OK


def test_uac_prompt_with_the_user_active_opens_nothing(tmp_path):
    # A UAC prompt reads as locked (any doubt does), but Windows does not report the session
    # locked, and the user is at the keyboard: a new window would take their typing.
    desktop = FakeDesktop(locked=True, session_locked=False, idle_seconds=0.5)
    ctx, _, _ = make(tmp_path, desktop=desktop)
    res = dispatch(start(), ctx)
    assert res.status == USER_ACTIVE
    assert res.detail["session_locked"] is False  # locked on doubt, but not proven
    assert desktop.opened == []


def test_exactly_ten_seconds_idle_opens_a_window(tmp_path):
    desktop = FakeDesktop(idle_seconds=IDLE_GUARD_SECONDS)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)
    desktop.on_open = lambda uri: register(tmp_path, bridge)
    assert dispatch(start(), ctx).status == OK


def test_busy_window_is_reported_not_reopened(tmp_path):
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)
    register(tmp_path, bridge)
    bridge.instances[5001] = TimeoutError()
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "window_busy"
    assert res.detail["windows"] == [INSTANCE]
    assert desktop.opened == []
    assert bridge.starts == []
    assert journal_state(tmp_path) == NOT_SUBMITTED


def test_stale_registrations_removed_on_the_way_are_reported(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    stale = "00000000-0000-4000-8000-000000000000"  # sorts before INSTANCE, so it is checked first
    register(tmp_path, bridge, port=5002, instance=stale)
    bridge.instances[5002] = None  # nothing answers there any more
    register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.status == OK
    assert res.detail["removed_windows"] == [stale]
    assert not (tmp_path / "windows" / f"{stale}.json").exists()


def test_crash_before_sending_leaves_the_request_retryable(tmp_path):
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)

    def broken_state():
        raise RuntimeError("desktop query failed")

    desktop.state = broken_state
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "prepare"
    assert res.detail["journal_state"] == NOT_SUBMITTED
    assert res.detail["error"] == "RuntimeError: desktop query failed"
    assert res.detail["session_id"] == session_id_for(RID)
    assert journal_state(tmp_path) == NOT_SUBMITTED
    assert bridge.starts == []
    del desktop.state  # back to the real method
    register(tmp_path, bridge)
    assert dispatch(start(), ctx).status == OK


def test_launch_failure_stops(tmp_path):
    ctx, _, _ = make(tmp_path, desktop=FakeDesktop(can_open=False))
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "open_window"
    assert journal_state(tmp_path) == NOT_SUBMITTED


def test_kill_switch_refuses_before_anything(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    ctx.kill_switch.touch()
    assert dispatch(start(), ctx).status == SERVICE_OFF
    assert bridge.starts == []
    assert journal_state(tmp_path) is None


def test_an_unreadable_kill_switch_refuses_the_start_as_service_off(tmp_path, monkeypatch):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    real = os.stat

    def stat(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(ctx.kill_switch):
            raise PermissionError(13, "Permission denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    assert dispatch(start(), ctx).status == SERVICE_OFF
    assert bridge.starts == []


def test_kill_switch_hit_while_a_window_opens(tmp_path):
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)

    def open_and_hit_the_switch(uri):
        register(tmp_path, bridge)
        ctx.kill_switch.touch()

    desktop.on_open = open_and_hit_the_switch
    res = dispatch(start(), ctx)
    assert res.status == SERVICE_OFF
    assert bridge.starts == []
    assert journal_state(tmp_path) == NOT_SUBMITTED


@pytest.mark.parametrize(
    "reply,status",
    [
        (BridgeReply(REFUSED, {"status": "refused", "reason": "kill_switch"}), SERVICE_OFF),
        (BridgeReply(REFUSED, {"status": "refused", "reason": "bad_request"}), NEEDS_HUMAN),
        (
            BridgeReply(REFUSED, {"status": "refused", "reason": "session_check_failed"}),
            NEEDS_HUMAN,
        ),
        (
            BridgeReply(REFUSED, {"status": "refused", "reason": "claude_unavailable"}),
            NEEDS_HUMAN,
        ),
        (
            BridgeReply(REFUSED, {"status": "refused", "reason": "audit_failed"}),
            NEEDS_HUMAN,
        ),
        (
            BridgeReply(REFUSED, {"status": "refused", "reason": "reservation_failed"}),
            NEEDS_HUMAN,
        ),
        (
            BridgeReply(REFUSED, {"status": "refused", "reason": "untrusted_folder"}),
            NEEDS_HUMAN,
        ),
        (
            BridgeReply(REFUSED, {"status": "refused", "reason": "trust_failed"}),
            NEEDS_HUMAN,
        ),
        (BridgeReply(UNREACHABLE, {"error": "connection refused"}), NEEDS_HUMAN),
    ],
)
def test_bridge_refusal_leaves_the_request_retryable(tmp_path, reply, status):
    ctx, bridge, _ = make(tmp_path, bridge=FakeBridge(reply))
    register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.status == status
    assert res.detail["step"] == "bridge"
    assert res.detail["journal_state"] == NOT_SUBMITTED
    assert res.detail["bridge_status"] == reply.status
    assert journal_state(tmp_path) == NOT_SUBMITTED


def test_duplicate_session_refusal_is_never_retried(tmp_path):
    reply = BridgeReply(REFUSED, {"status": "refused", "reason": "duplicate_session"})
    ctx, bridge, _ = make(tmp_path, bridge=FakeBridge(reply))
    register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "bridge"
    assert res.detail["journal_state"] == JOURNAL_UNKNOWN
    assert journal_state(tmp_path) == JOURNAL_UNKNOWN
    assert dispatch(start(), ctx).detail["step"] == "journal"
    assert len(bridge.starts) == 1


def test_unclear_bridge_answer_blocks_retries(tmp_path):
    ctx, bridge, _ = make(tmp_path, bridge=FakeBridge(BridgeReply(UNKNOWN, {"error": "timeout"})))
    register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["session_id"] == session_id_for(RID)
    assert res.detail["journal_state"] == JOURNAL_UNKNOWN
    assert res.detail["bridge_status"] == UNKNOWN
    assert journal_state(tmp_path) == JOURNAL_UNKNOWN
    bridge.reply = BridgeReply(STARTED, {"terminal_pid": 1})
    again = dispatch(start(), ctx)
    assert again.status == NEEDS_HUMAN
    assert again.detail["step"] == "journal"
    assert again.detail["journal_state"] == JOURNAL_UNKNOWN
    assert len(bridge.starts) == 1


def test_second_start_while_one_runs_is_refused(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    ctx.start_lock.acquire()
    try:
        res = dispatch(start(), ctx)
    finally:
        ctx.start_lock.release()
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "busy"
    assert journal_state(tmp_path) is None


@pytest.mark.parametrize(
    "change",
    [
        {"repo": "/etc"},
        {"remote": "ssh+evil"},
        {"prompt": ""},
        {"prompt": "update"},
        {"prompt": "-x hello"},
        {"prompt": "fix ${env:HOME} now"},
        {"repo": "/home/user/src/a${x}"},
        {"label": "-x"},
    ],
)
def test_bad_arguments_are_refused_before_anything(tmp_path, change):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    assert dispatch(start(**change), ctx).status == BAD_REQUEST
    assert bridge.starts == []


def test_unsafe_request_id_is_refused(tmp_path):
    ctx, _, _ = make(tmp_path)
    assert dispatch(start(rid="../../x"), ctx).status == BAD_REQUEST


def test_missing_token_needs_a_human(tmp_path):
    ctx, _, _ = make(tmp_path)
    ctx.token = None
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "config"


def test_result_never_carries_the_prompt_text(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    res = dispatch(start(prompt="zebra prompt words"), ctx)
    assert "zebra prompt words" not in json.dumps(res.detail)
    assert res.detail["prompt_chars"] == len("zebra prompt words")
    assert ctx.inflight.names() == []


def test_submitting_mark_failure_leaves_the_request_retryable(tmp_path, monkeypatch):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    real_mark = Journal.mark
    failures = []

    def mark_that_fails_once_on_submitting(self, request_id, state, detail=None):
        if state == SUBMITTING and not failures:
            failures.append(state)
            raise PermissionError("journal entry is locked by another process")
        return real_mark(self, request_id, state, detail)

    monkeypatch.setattr("remote_hands.actions.Journal.mark", mark_that_fails_once_on_submitting)
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "prepare"
    assert res.detail["journal_state"] == NOT_SUBMITTED
    assert res.detail["session_id"] == session_id_for(RID)
    assert bridge.starts == []
    assert journal_state(tmp_path) == NOT_SUBMITTED
    again = dispatch(start(), ctx)
    assert again.status == OK
    assert len(bridge.starts) == 1


def test_a_failure_after_the_submitting_mark_landed_keeps_the_request_blocked(
    tmp_path, monkeypatch
):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    real_mark = Journal.mark

    def mark_that_raises_after_writing_submitting(self, request_id, state, detail=None):
        real_mark(self, request_id, state, detail)
        if state == SUBMITTING:
            raise PermissionError("the rename landed but reported an error")

    monkeypatch.setattr(
        "remote_hands.actions.Journal.mark", mark_that_raises_after_writing_submitting
    )
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "prepare"
    assert res.detail["journal_state"] == SUBMITTING  # never downgraded to not_submitted
    assert journal_state(tmp_path) == SUBMITTING
    assert bridge.starts == []
    again = dispatch(start(), ctx)
    assert again.detail["step"] == "journal"
    assert again.detail["journal_state"] == SUBMITTING
    assert bridge.starts == []


def test_bridge_sees_submitting_when_called(tmp_path):
    class Spy(FakeBridge):
        state_during_start = None

        def start_claude(self, port, token, body):
            self.state_during_start = journal_state(tmp_path)
            return super().start_claude(port, token, body)

    ctx, bridge, _ = make(tmp_path, bridge=Spy())
    register(tmp_path, bridge)
    assert dispatch(start(), ctx).status == OK
    assert bridge.state_during_start == SUBMITTING


def test_kill_switch_during_the_window_wait_stops_it(tmp_path):
    ctx, bridge, clock = make(tmp_path, desktop=FakeDesktop(idle_seconds=600))

    def sleep_then_hit_the_switch(seconds):
        clock.sleep(seconds)
        ctx.kill_switch.touch()  # the user runs `hands off` while the new window starts

    ctx.sleep = sleep_then_hit_the_switch
    res = dispatch(start(), ctx)
    assert res.status == SERVICE_OFF
    assert res.detail["step"] == "window"
    assert res.detail["opened_window"] is True
    assert clock.now == WINDOW_POLL_SECONDS  # stopped at the next poll, not after 90 s
    assert bridge.starts == []
    assert journal_state(tmp_path) == NOT_SUBMITTED


def test_kill_switch_wins_over_the_deadline_at_the_last_poll(tmp_path):
    ctx, bridge, clock = make(tmp_path, desktop=FakeDesktop(idle_seconds=600))

    def sleep_and_hit_the_switch_at_the_deadline(seconds):
        clock.sleep(seconds)
        if clock.now >= WINDOW_WAIT_SECONDS:
            ctx.kill_switch.touch()  # first seen at the poll where the deadline also passes

    ctx.sleep = sleep_and_hit_the_switch_at_the_deadline
    res = dispatch(start(), ctx)
    assert res.status == SERVICE_OFF
    assert res.detail["step"] == "window"
    assert clock.now == WINDOW_WAIT_SECONDS
    assert bridge.starts == []
    assert journal_state(tmp_path) == NOT_SUBMITTED


def test_a_crash_during_the_window_wait_still_records_the_open(tmp_path):
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)

    def open_then_break_the_ping(uri):
        register(tmp_path, bridge)
        bridge.instances[5001] = RuntimeError("ping failed")

    desktop.on_open = open_then_break_the_ping
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "prepare"
    assert res.detail["opened_window"] is True
    assert res.detail["journal_state"] == NOT_SUBMITTED
    assert bridge.starts == []


def test_start_claude_is_in_flight_during_the_bridge_call(tmp_path):
    seen = []

    class Spy(FakeBridge):
        def start_claude(self, port, token, body):
            seen.append(ctx.inflight.names())
            return super().start_claude(port, token, body)

    ctx, bridge, _ = make(tmp_path, bridge=Spy())
    register(tmp_path, bridge)
    assert dispatch(start(), ctx).status == OK
    assert seen == [["start_claude"]]


def test_kill_switch_set_during_the_first_scan_opens_nothing(tmp_path):
    desktop = FakeDesktop(idle_seconds=600)
    ctx, bridge, _ = make(tmp_path, desktop=desktop)
    register(tmp_path, bridge)

    def hit_the_switch_and_stay_silent():
        ctx.kill_switch.touch()  # the user runs `hands off` while the scan pings the window
        return None  # and nothing answers there: the registration is stale

    bridge.instances[5001] = hit_the_switch_and_stay_silent
    res = dispatch(start(), ctx)
    assert res.status == SERVICE_OFF
    assert res.detail["step"] == "open_window"
    assert desktop.opened == []
    assert journal_state(tmp_path) == NOT_SUBMITTED


def test_unlisted_refusal_reason_is_unknown(tmp_path):
    reply = BridgeReply(REFUSED, {"status": "refused", "reason": "weird"})
    ctx, bridge, _ = make(tmp_path, bridge=FakeBridge(reply))
    register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "bridge"
    assert res.detail["journal_state"] == JOURNAL_UNKNOWN
    assert journal_state(tmp_path) == JOURNAL_UNKNOWN
    assert dispatch(start(), ctx).detail["step"] == "journal"
    assert len(bridge.starts) == 1


def test_bridge_reply_is_summarized(tmp_path):
    reply = BridgeReply(
        UNKNOWN, {"status": "failed", "error": "e" * 1000, "prompt": "zebra prompt words"}
    )
    ctx, bridge, _ = make(tmp_path, bridge=FakeBridge(reply))
    register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.detail["bridge"]["status"] == "failed"
    assert "prompt" not in res.detail["bridge"]
    assert len(res.detail["bridge"]["error"]) <= 300
    for text in (json.dumps(res.detail), journal_text(tmp_path)):
        assert "zebra prompt words" not in text
        assert "e" * 301 not in text


def test_bridge_reply_keeps_only_known_scalar_fields(tmp_path):
    reply = BridgeReply(
        UNKNOWN,
        {
            "status": "failed",
            "reason": None,
            "http_status": 502,
            "exit_code": True,  # a bool is not a number
            "error": ["not", "text"],
            "terminal_pid": {"nested": 1},
            "stack": "an unlisted field",
        },
    )
    ctx, bridge, _ = make(tmp_path, bridge=FakeBridge(reply))
    register(tmp_path, bridge)
    res = dispatch(start(), ctx)
    assert res.detail["bridge"] == {"status": "failed", "reason": None, "http_status": 502}


def test_same_id_with_different_arguments_is_refused(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    first = dispatch(start(), ctx)
    assert first.status == OK
    other = dispatch(start(prompt="a different prompt"), ctx)
    assert other.status == BAD_REQUEST
    assert "read it back with its session_id" in other.detail["error"]
    assert "use a new request_id" not in other.detail["error"]
    assert other.detail["session_id"] == session_id_for(RID)
    assert other.detail["journal_state"] == FULFILLED
    assert len(bridge.starts) == 1
    same = dispatch(start(), ctx)
    assert same.status == OK
    assert same.detail["replayed"] is True
    assert "args_sha256" not in same.detail
    assert len(bridge.starts) == 1


def test_same_id_with_a_different_label_replays(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    first = dispatch(start(), ctx)
    again = dispatch(start(label="renamed"), ctx)  # the label only names the terminal
    assert again.status == OK
    assert again.detail["replayed"] is True
    assert again.detail["label"] == first.detail["label"]
    assert len(bridge.starts) == 1


def test_stale_claimed_entry_is_taken_over(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    assert Journal(tmp_path / "journal").claim(RID) is None  # an earlier attempt died after this
    res = dispatch(start(), ctx)
    assert res.status == OK
    assert len(bridge.starts) == 1
    assert journal_state(tmp_path) == FULFILLED


BOOT = 1_000_000.0  # a Windows start time, in epoch seconds


def claim_as_a_dead_attempt(tmp_path, written):
    """Leave a claimed entry behind, as an attempt that died before sending would."""
    assert Journal(tmp_path / "journal").claim(RID) is None
    os.utime(tmp_path / "journal" / f"{RID}.json", (written, written))


@pytest.mark.parametrize(
    "written,taken_over",
    [
        (BOOT - BOOT_SLACK_SECONDS - 0.5, False),  # written before the boot, beyond the slack
        (BOOT - BOOT_SLACK_SECONDS, True),  # right at the edge of the slack
        (BOOT + 50.0, True),  # written since the boot
    ],
)
def test_claimed_entry_is_taken_over_only_if_written_since_the_boot(tmp_path, written, taken_over):
    ctx, bridge, _ = make(tmp_path, desktop=FakeDesktop(boot_time=BOOT))
    register(tmp_path, bridge)
    claim_as_a_dead_attempt(tmp_path, written)
    res = dispatch(start(), ctx)
    if taken_over:
        assert res.status == OK
        assert len(bridge.starts) == 1
    else:
        # After an OS crash or power loss the rename to submitting may have been lost, so an
        # older claim is ambiguous: it must not send again.
        assert res.status == NEEDS_HUMAN
        assert res.detail["step"] == "journal"
        assert res.detail["journal_state"] == CLAIMED
        assert bridge.starts == []
        assert journal_state(tmp_path) == CLAIMED


def test_claimed_entry_is_not_taken_over_when_the_boot_time_is_unknown(tmp_path):
    ctx, bridge, _ = make(tmp_path, desktop=FakeDesktop(boot_time=None))
    register(tmp_path, bridge)
    claim_as_a_dead_attempt(tmp_path, written=BOOT + 50.0)
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "journal"
    assert res.detail["journal_state"] == CLAIMED
    assert bridge.starts == []


def test_a_fresh_request_does_not_need_the_boot_time(tmp_path):
    class NoBootTime(FakeDesktop):
        def boot_time(self):
            raise OSError("tick count unavailable")

    ctx, bridge, _ = make(tmp_path, desktop=NoBootTime())
    register(tmp_path, bridge)
    assert dispatch(start(), ctx).status == OK


def test_stale_submitting_entry_is_never_taken_over(tmp_path):
    ctx, bridge, _ = make(tmp_path)
    register(tmp_path, bridge)
    journal = Journal(tmp_path / "journal")
    assert journal.claim(RID) is None
    journal.mark(RID, SUBMITTING, {})  # an earlier attempt died around the send: it may have run
    res = dispatch(start(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "journal"
    assert res.detail["journal_state"] == SUBMITTING
    assert bridge.starts == []
    assert journal_state(tmp_path) == SUBMITTING
