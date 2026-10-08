"""Test doubles shared by the start_claude tests."""

import json

from remote_hands.bridge_client import STARTED, BridgeReply

REPO = "/home/user/src/repo-a"
REMOTE = "wsl+ubuntu-24.04"
INSTANCE = "11111111-2222-4333-8444-555555555555"


class FakeBridge:
    """Answers pings for registered ports and records every start request."""

    def __init__(self, reply=None):
        self.instances = {}
        self.reply = reply or BridgeReply(STARTED, {"status": "started", "terminal_pid": 4242})
        self.starts = []

    def ping(self, port):
        answer = self.instances.get(port)
        if isinstance(answer, BaseException):
            raise answer  # e.g. TimeoutError(): the bridge is busy
        if callable(answer):
            return answer()  # a hook that runs at ping time; its return value is the answer
        return answer

    def start_claude(self, port, token, body):
        self.starts.append({"port": port, "token": token, "body": body})
        return self.reply


def register(state_dir, bridge, port=5001, folders=(REPO,), instance=INSTANCE):
    """Write a window registration the way a bridge does, and make its port answer."""
    windows = state_dir / "windows"
    windows.mkdir(parents=True, exist_ok=True)
    record = {"instance": instance, "port": port, "folders": list(folders)}
    (windows / f"{instance}.json").write_text(json.dumps(record), encoding="utf-8")
    bridge.instances[port] = instance


class Clock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
