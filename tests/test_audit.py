import json
import threading

from remote_hands import audit
from remote_hands.desktop import FakeDesktop, ProbeOutcome


def test_append_writes_one_json_line_per_event(tmp_path):
    path = tmp_path / "nested" / "audit.jsonl"
    audit.append(path, {"event": "start"})
    audit.append(path, {"event": "action", "action": "health"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["start", "action"]
    assert all("ts" in json.loads(line) for line in lines)


def test_probe_outcome_arrived_needs_foreground_and_exact_text():
    assert ProbeOutcome(True, "focus_force", 14, "rhprobe", "rhprobe").arrived
    assert not ProbeOutcome(True, "focus_force", 14, "rhprobe", "rhprob").arrived
    assert not ProbeOutcome(False, "none", 0, "rhprobe", "rhprobe").arrived


def test_fake_desktop_models_lock_focus_and_input():
    fake = FakeDesktop(locked=True, idle_seconds=3.0)
    assert fake.state().locked
    assert fake.state().input_desktop == "Winlogon"
    assert not FakeDesktop(can_focus=False).probe_input("x").foreground_ok
    assert FakeDesktop(input_works=False).probe_input("x").text_seen == ""
    working = FakeDesktop()
    assert working.probe_input("x").arrived
    assert working.probe_calls == ["x"]


def test_fake_desktop_honors_should_stop():
    fake = FakeDesktop()
    outcome = fake.probe_input("x", should_stop=lambda: True)
    assert outcome.focus_method == "aborted_kill_switch"
    assert outcome.keys_sent == 0


def test_concurrent_appends_keep_every_line(tmp_path):
    path = tmp_path / "audit.jsonl"

    def worker(n):
        for i in range(200):
            audit.append(path, {"event": "action", "worker": n, "i": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1600
    assert all(json.loads(line)["event"] == "action" for line in lines)
