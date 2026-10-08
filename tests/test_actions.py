import errno
import os

import pytest

import remote_hands
from remote_hands.actions import (
    ACTIONS,
    IDLE_GUARD_SECONDS,
    PROBE_TEXT,
    Context,
    InFlight,
    dispatch,
    kill_switch_on,
)
from remote_hands.contract import (
    BAD_REQUEST,
    NEEDS_HUMAN,
    OK,
    SERVICE_OFF,
    UNKNOWN_ACTION,
    USER_ACTIVE,
    ActionRequest,
    ActionResult,
)
from remote_hands.desktop import FakeDesktop


def make_ctx(tmp_path, desktop=None, sleep=None):
    sleeps: list[float] = []
    ctx = Context(
        desktop=desktop or FakeDesktop(),
        kill_switch=tmp_path / "OFF",
        sleep=sleep or sleeps.append,
    )
    return ctx, sleeps


def probe(delay=0):
    return ActionRequest(action="probe_input", request_id="r1", args={"delay_seconds": delay})


def test_health_reports_version_kill_switch_and_desktop(tmp_path):
    ctx, _ = make_ctx(tmp_path, FakeDesktop(locked=False, idle_seconds=42.04))
    res = dispatch(ActionRequest(action="health", request_id="h1"), ctx)
    assert res.status == OK
    assert res.detail == {
        "version": remote_hands.__version__,
        "kill_switch": False,
        "desktop_locked": False,
        "session_locked": False,
        "input_desktop": "Default",
        "idle_seconds": 42.0,
        "in_flight": [],
    }


def test_health_still_answers_with_kill_switch_on(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    ctx.kill_switch.touch()
    res = dispatch(ActionRequest(action="health", request_id="h1"), ctx)
    assert res.status == OK
    assert res.detail["kill_switch"] is True


def test_health_tells_proof_of_a_lock_from_doubt(tmp_path):
    ctx, _ = make_ctx(tmp_path, FakeDesktop(locked=True, session_locked=False))
    res = dispatch(ActionRequest(action="health", request_id="h1"), ctx)
    assert res.detail["desktop_locked"] is True
    assert res.detail["session_locked"] is False


def test_probe_refused_when_kill_switch_on(tmp_path):
    fake = FakeDesktop()
    ctx, _ = make_ctx(tmp_path, fake)
    ctx.kill_switch.touch()
    assert dispatch(probe(), ctx).status == SERVICE_OFF
    assert fake.probe_calls == []


def test_probe_rechecks_kill_switch_after_the_wait(tmp_path):
    fake = FakeDesktop()
    ctx, _ = make_ctx(tmp_path, fake, sleep=lambda _s: (tmp_path / "OFF").touch())
    assert dispatch(probe(5), ctx).status == SERVICE_OFF
    assert fake.probe_calls == []


def test_probe_refused_while_the_user_is_active(tmp_path):
    fake = FakeDesktop(locked=False, idle_seconds=IDLE_GUARD_SECONDS - 8)
    ctx, _ = make_ctx(tmp_path, fake)
    res = dispatch(probe(), ctx)
    assert res.status == USER_ACTIVE
    assert fake.probe_calls == []


def test_probe_refused_when_locked_but_recently_active(tmp_path):
    fake = FakeDesktop(locked=True, idle_seconds=1.0)
    ctx, _ = make_ctx(tmp_path, fake)
    assert dispatch(probe(), ctx).status == USER_ACTIVE
    assert fake.probe_calls == []


def test_probe_runs_when_locked_and_idle(tmp_path):
    fake = FakeDesktop(locked=True, idle_seconds=600.0, can_focus=False)
    ctx, _ = make_ctx(tmp_path, fake)
    res = dispatch(probe(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["desktop_locked"] is True
    assert res.detail["foreground_ok"] is False
    assert res.detail["keys_sent"] == 0
    assert res.detail["text_arrived"] is False
    assert fake.probe_calls == [PROBE_TEXT]


def test_probe_returns_needs_human_when_text_does_not_arrive(tmp_path):
    ctx, _ = make_ctx(tmp_path, FakeDesktop(input_works=False))
    res = dispatch(probe(), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "probe"
    assert res.detail["text_arrived"] is False


def test_probe_reports_arrival_when_unlocked_and_idle(tmp_path):
    ctx, _ = make_ctx(tmp_path, FakeDesktop())
    res = dispatch(probe(), ctx)
    assert res.status == OK
    assert res.detail["text_arrived"] is True
    assert res.detail["focus_method"] == "focus_force"


def test_probe_waits_the_requested_delay(tmp_path):
    ctx, sleeps = make_ctx(tmp_path)
    dispatch(probe(15), ctx)
    assert sleeps == [15.0]


@pytest.mark.parametrize("delay", [-1, 121, "5", True, None])
def test_probe_rejects_bad_delay(tmp_path, delay):
    fake = FakeDesktop()
    ctx, _ = make_ctx(tmp_path, fake)
    assert dispatch(probe(delay), ctx).status == BAD_REQUEST
    assert fake.probe_calls == []


def test_kill_switch_between_check_and_typing_stops_the_probe(tmp_path):
    class SwitchFlippedDuringState(FakeDesktop):
        def state(self):
            (tmp_path / "OFF").touch()  # the user runs `hands off` as the probe starts
            return super().state()

    fake = SwitchFlippedDuringState()
    ctx, _ = make_ctx(tmp_path, fake)
    res = dispatch(probe(), ctx)
    assert res.status == SERVICE_OFF


def test_probe_refuses_while_another_input_action_runs(tmp_path):
    fake = FakeDesktop()
    ctx, _ = make_ctx(tmp_path, fake)
    ctx.input_lock.acquire()
    try:
        res = dispatch(probe(), ctx)
    finally:
        ctx.input_lock.release()
    assert res.status == NEEDS_HUMAN
    assert res.detail["step"] == "input_lock"
    assert fake.probe_calls == []


def test_unknown_action_lists_the_known_ones(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    res = dispatch(ActionRequest(action="run_shell", request_id="x"), ctx)
    assert res.status == UNKNOWN_ACTION
    assert res.detail == {"known": ["health", "probe_input", "start_claude"]}


def test_probe_never_reports_the_typed_text(tmp_path):
    ctx, _ = make_ctx(tmp_path, FakeDesktop(input_works=False))
    res = dispatch(probe(), ctx)
    assert "text_seen" not in res.detail
    assert res.detail["text_seen_chars"] == 0


def test_desktop_error_becomes_needs_human(tmp_path):
    class Broken(FakeDesktop):
        def state(self):
            raise OSError("access denied")

    ctx, _ = make_ctx(tmp_path, Broken())
    res = dispatch(ActionRequest(action="health", request_id="h1"), ctx)
    assert res.status == NEEDS_HUMAN
    assert res.detail == {"step": "health", "error": "OSError: access denied"}


def test_health_lists_actions_in_flight(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    with ctx.inflight.track("start_claude"):
        busy = dispatch(ActionRequest(action="health", request_id="h1"), ctx)
    idle = dispatch(ActionRequest(action="health", request_id="h2"), ctx)
    assert busy.detail["in_flight"] == ["start_claude"]
    assert idle.detail["in_flight"] == []


def test_probe_is_in_flight_while_it_types(tmp_path):
    seen = []

    class Spy(FakeDesktop):
        def probe_input(self, text, should_stop=lambda: False):
            seen.append(ctx.inflight.names())
            return super().probe_input(text, should_stop)

    ctx, _ = make_ctx(tmp_path, Spy())
    dispatch(probe(), ctx)
    assert seen == [["probe_input"]]
    assert ctx.inflight.names() == []


def test_probe_is_in_flight_during_its_wait(tmp_path):
    fake = FakeDesktop()
    seen = []

    def sleep(_seconds):
        seen.append(ctx.inflight.names())
        ctx.kill_switch.touch()  # the user hits the switch during the wait

    ctx, _ = make_ctx(tmp_path, fake, sleep=sleep)
    res = dispatch(probe(delay=5), ctx)
    assert seen == [["probe_input"]]
    assert res.status == SERVICE_OFF
    assert fake.probe_calls == []
    assert ctx.inflight.names() == []


def test_in_flight_clears_when_the_action_raises(tmp_path):
    class Broken(FakeDesktop):
        def probe_input(self, text, should_stop=lambda: False):
            raise OSError("boom")

    ctx, _ = make_ctx(tmp_path, Broken())
    assert dispatch(probe(), ctx).status == NEEDS_HUMAN
    assert ctx.inflight.names() == []


def test_same_name_stays_listed_until_the_last_holder_leaves():
    tracker = InFlight()
    with tracker.track("a"):
        with tracker.track("a"):
            pass
        assert tracker.names() == ["a"]
    assert tracker.names() == []


def test_new_actions_are_tracked_automatically(tmp_path, monkeypatch):
    seen = []

    def fake_action(req, ctx):
        seen.append(ctx.inflight.names())  # the handler's first act: no check has run yet
        return ActionResult(OK, req.request_id)

    monkeypatch.setitem(ACTIONS, "fake_action", fake_action)
    ctx, _ = make_ctx(tmp_path)
    res = dispatch(ActionRequest(action="fake_action", request_id="f1"), ctx)
    assert res.status == OK
    assert seen == [["fake_action"]]
    assert ctx.inflight.names() == []


def test_health_is_never_listed_in_flight(tmp_path, monkeypatch):
    seen = []

    def fake_action(req, ctx):
        seen.append(dispatch(ActionRequest(action="health", request_id="h1"), ctx))
        return ActionResult(OK, req.request_id)

    monkeypatch.setitem(ACTIONS, "fake_action", fake_action)
    ctx, _ = make_ctx(tmp_path)
    dispatch(ActionRequest(action="fake_action", request_id="f1"), ctx)
    assert [res.detail["in_flight"] for res in seen] == [["fake_action"]]


def test_token_is_not_in_repr(tmp_path):
    token = "t" * 40
    ctx = Context(desktop=FakeDesktop(), kill_switch=tmp_path / "OFF", token=token)
    assert token not in repr(ctx)


def fail_stat_of(monkeypatch, target, exc):
    """Make os.stat (behind Path.stat and Path.exists alike) raise exc for target only."""
    real = os.stat

    def stat(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(target):
            raise exc
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


def test_the_kill_switch_is_off_only_while_the_off_file_is_provably_absent(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    assert kill_switch_on(ctx) is False  # not there
    ctx.kill_switch.touch()
    assert kill_switch_on(ctx) is True


@pytest.mark.parametrize(
    "exc",
    [
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(errno.EIO, "Input/output error"),
        NotADirectoryError(errno.ENOTDIR, "Not a directory"),
        OSError(errno.ELOOP, "Too many levels of symbolic links"),
    ],
    ids=["EACCES", "EIO", "ENOTDIR", "ELOOP"],
)
def test_an_off_file_that_cannot_be_checked_counts_as_switched_off(tmp_path, monkeypatch, exc):
    # Same rule as the bridge (core.killSwitchOn): any error but "not found" is off, never on.
    ctx, _ = make_ctx(tmp_path)
    fail_stat_of(monkeypatch, ctx.kill_switch, exc)
    assert kill_switch_on(ctx) is True
    assert dispatch(probe(), ctx).status == SERVICE_OFF


def test_a_state_folder_path_that_is_a_file_counts_as_switched_off(tmp_path):
    # The OFF path's parent is a file, so stat fails with ENOTDIR: not provably absent.
    blocker = tmp_path / "home"
    blocker.write_text("")
    ctx = Context(desktop=FakeDesktop(), kill_switch=blocker / "OFF", sleep=lambda _s: None)
    assert kill_switch_on(ctx) is True
