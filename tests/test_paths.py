from pathlib import Path

import pytest

from remote_hands import paths


def test_windows_and_journal_dirs_live_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(tmp_path))
    assert paths.windows_dir() == tmp_path / "windows"
    assert paths.journal_dir() == tmp_path / "journal"


def test_claude_projects_dir_defaults_to_the_users_claude_folder(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECTS_DIR", raising=False)
    assert paths.claude_projects_dir() == Path.home() / ".claude" / "projects"


def test_claude_projects_dir_override_must_be_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert paths.claude_projects_dir() == tmp_path
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", "relative/dir")
    with pytest.raises(ValueError):
        paths.claude_projects_dir()


@pytest.fixture
def no_home_override(monkeypatch):
    monkeypatch.delenv("REMOTE_HANDS_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(paths.sys, "platform", "linux")
    paths._windows_local_app_data.cache_clear()
    yield
    paths._windows_local_app_data.cache_clear()


def test_home_asks_windows_for_localappdata_once(monkeypatch, no_home_override):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[0] == "wslpath":
            return "/mnt/c/Users/user/AppData/Local\n"
        return "C:\\Users\\user\\AppData\\Local\r\n"

    monkeypatch.setattr(paths, "_run", run)
    expected = Path("/mnt/c/Users/user/AppData/Local/remote-hands")
    assert paths.home() == expected
    assert paths.token_path() == expected / "token"
    assert [argv[1:] for argv in calls] == [
        ["/d", "/c", "echo", "%LOCALAPPDATA%"],
        ["-u", "C:\\Users\\user\\AppData\\Local"],
    ]  # asked once, then remembered


@pytest.mark.parametrize("answer", ["%LOCALAPPDATA%\r\n", "\r\n"], ids=["unset", "empty"])
def test_home_refuses_when_windows_reports_no_localappdata(monkeypatch, no_home_override, answer):
    monkeypatch.setattr(paths, "_run", lambda argv: answer)
    with pytest.raises(ValueError, match="REMOTE_HANDS_HOME"):
        paths.home()


def test_home_refuses_when_windows_cannot_be_asked(monkeypatch, no_home_override):
    def run(argv):
        raise ValueError(f"{argv[0]} failed: not found")

    monkeypatch.setattr(paths, "_run", run)
    with pytest.raises(ValueError, match="REMOTE_HANDS_HOME"):
        paths.home()


def test_config_lives_under_home(monkeypatch, tmp_path):
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(tmp_path))
    assert paths.config_path() == tmp_path / "config.json"
