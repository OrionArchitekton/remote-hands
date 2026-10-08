import threading
from pathlib import Path

import pytest

from remote_hands import paths
from remote_hands.auth import load_or_create_token, load_token, token_matches


def test_home_honours_override(monkeypatch, tmp_path):
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(tmp_path))
    assert paths.home() == tmp_path
    assert paths.token_path() == tmp_path / "token"
    assert paths.kill_switch_path() == tmp_path / "OFF"
    assert paths.audit_path() == tmp_path / "audit.jsonl"


def test_home_default_on_linux_points_at_windows_localappdata(monkeypatch):
    monkeypatch.delenv("REMOTE_HANDS_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "/mnt/c/Users/user/AppData/Local")  # WSLENV=LOCALAPPDATA/p
    monkeypatch.setattr(paths.sys, "platform", "linux")
    paths._windows_local_app_data.cache_clear()
    try:
        assert paths.home() == Path("/mnt/c/Users/user/AppData/Local/remote-hands")
    finally:
        paths._windows_local_app_data.cache_clear()


def test_creates_long_token_once(tmp_path):
    path = tmp_path / "sub" / "token"
    first = load_or_create_token(path)
    assert len(first) >= 32
    assert load_or_create_token(path) == first
    assert load_token(path) == first


def test_rejects_short_token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("short", encoding="utf-8")
    with pytest.raises(ValueError):
        load_or_create_token(path)


def test_load_token_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_token(tmp_path / "token")


@pytest.mark.parametrize(
    ("presented", "expected", "ok"),
    [
        ("abc", "abc", True),
        ("abd", "abc", False),
        ("", "abc", False),
        (None, "abc", False),
    ],
)
def test_token_matches(presented, expected, ok):
    assert token_matches(presented, expected) is ok


def test_concurrent_creators_all_get_the_same_full_token(tmp_path):
    path = tmp_path / "token"
    results: list[str] = []
    errors: list[BaseException] = []

    def worker():
        try:
            results.append(load_or_create_token(path))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    for _ in range(20):
        path.unlink(missing_ok=True)
        results.clear()
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert len(set(results)) == 1
    assert not list(tmp_path.glob("token.*.tmp"))


def test_token_file_is_private_from_creation(tmp_path):
    path = tmp_path / "token"
    load_or_create_token(path)
    assert path.stat().st_mode & 0o077 == 0


def test_relative_home_override_is_refused(monkeypatch):
    monkeypatch.setenv("REMOTE_HANDS_HOME", "relative/dir")
    with pytest.raises(ValueError):
        paths.home()
