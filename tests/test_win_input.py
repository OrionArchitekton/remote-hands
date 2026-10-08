import ctypes
import sys
from types import SimpleNamespace

import pytest

from remote_hands.desktop import FakeDesktop, LaunchOutcome
from remote_hands.win_desktop import WindowsDesktop, cursor_launch_argv, launch_cursor, lock_state
from remote_hands.win_input import (
    INPUT,
    INPUT_KEYBOARD,
    KEYBDINPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    LASTINPUTINFO,
    MOUSEINPUT,
    VK_MENU,
    WTS_SESSIONSTATE_LOCK,
    WTS_SESSIONSTATE_UNLOCK,
    WTSINFOEX_LEVEL1_W,
    WTSINFOEXW,
    keyboard_input,
    unicode_input,
)

pytestmark = pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="64-bit layout")


def test_structure_sizes_match_64_bit_windows():
    assert ctypes.sizeof(KEYBDINPUT) == 24
    assert ctypes.sizeof(MOUSEINPUT) == 32
    assert ctypes.sizeof(INPUT) == 40
    assert ctypes.sizeof(LASTINPUTINFO) == 8


def test_unicode_input_carries_the_character_in_the_scan_code():
    down = unicode_input("r", 0)
    up = unicode_input("r", KEYEVENTF_KEYUP)
    assert down.type == INPUT_KEYBOARD
    assert down.u.ki.wVk == 0
    assert down.u.ki.wScan == ord("r")
    assert down.u.ki.dwFlags == KEYEVENTF_UNICODE
    assert up.u.ki.dwFlags == KEYEVENTF_UNICODE | KEYEVENTF_KEYUP


@pytest.mark.parametrize("bad", ["", "ab", "\U0001f600"])
def test_unicode_input_takes_one_bmp_character(bad):
    with pytest.raises(ValueError):
        unicode_input(bad, 0)


def test_keyboard_input_uses_the_virtual_key():
    event = keyboard_input(VK_MENU, KEYEVENTF_KEYUP)
    assert event.u.ki.wVk == VK_MENU
    assert event.u.ki.dwFlags == KEYEVENTF_KEYUP


def test_win_desktop_module_imports_on_any_platform():
    import remote_hands.win_desktop  # noqa: F401


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows refusal")
def test_windows_desktop_refuses_off_windows():
    from remote_hands.win_desktop import WindowsDesktop

    with pytest.raises(RuntimeError):
        WindowsDesktop()


def test_session_info_layout_puts_the_lock_flag_at_byte_16():
    # Verified live on a Windows PC: the int32 at byte 16 of WTSINFOEXW read 0 while
    # LockApp.exe held the foreground and 1 once unlocked.
    assert WTSINFOEXW.Data.offset == 8  # the level-1 union is 8-byte aligned (LARGE_INTEGER times)
    assert WTSINFOEX_LEVEL1_W.SessionFlags.offset == 8
    assert WTSINFOEXW.Data.offset + WTSINFOEX_LEVEL1_W.SessionFlags.offset == 16


def test_session_flag_values():
    assert WTS_SESSIONSTATE_LOCK == 0
    assert WTS_SESSIONSTATE_UNLOCK == 1


@pytest.mark.parametrize(
    "session_flag,input_desktop,expected",
    [
        (WTS_SESSIONSTATE_UNLOCK, "Default", (False, False)),
        (WTS_SESSIONSTATE_LOCK, "Default", (True, True)),
        (None, "Default", (True, False)),  # the query failed: locked on doubt, nothing proven
        (-1, "Default", (True, False)),  # WTS_SESSIONSTATE_UNKNOWN read as an int32
        (WTS_SESSIONSTATE_UNLOCK, "Winlogon", (True, False)),  # a UAC prompt
        (WTS_SESSIONSTATE_LOCK, "", (True, True)),
    ],
)
def test_lock_state_is_locked_on_doubt_but_proven_only_by_the_session_flag(
    session_flag, input_desktop, expected
):
    assert lock_state(session_flag, input_desktop) == expected


@pytest.mark.parametrize(
    "session_flag,input_desktop,locked,session_locked",
    [
        (WTS_SESSIONSTATE_UNLOCK, "Default", False, False),
        (WTS_SESSIONSTATE_LOCK, "Default", True, True),  # Win+L: the lock screen is on Default
        (WTS_SESSIONSTATE_UNLOCK, "Winlogon", True, False),  # a UAC prompt is not a locked session
        (None, "Default", True, False),  # the session query failed
    ],
)
def test_windows_desktop_state_reports_a_locked_session_only_on_proof(
    monkeypatch, session_flag, input_desktop, locked, session_locked
):
    class FakeKernel:
        def GetTickCount(self):
            return 5_000

    desktop = object.__new__(WindowsDesktop)  # skip __init__: it only runs on Windows
    desktop._kernel32 = FakeKernel()
    monkeypatch.setattr(desktop, "_input_desktop_name", lambda: input_desktop)
    monkeypatch.setattr(desktop, "_last_input_tick", lambda: 4_000)
    monkeypatch.setattr(desktop, "_session_flag", lambda: session_flag)
    state = desktop.state()
    assert (state.locked, state.session_locked) == (locked, session_locked)
    assert state.input_desktop == input_desktop
    assert state.idle_seconds == 1.0


def test_windows_desktop_boot_time_is_now_minus_the_tick_count(monkeypatch):
    import remote_hands.win_desktop as win_desktop

    class FakeKernel:
        def GetTickCount64(self):
            return 3_600_000  # one hour since Windows started, in milliseconds

    desktop = object.__new__(WindowsDesktop)  # skip __init__: it only runs on Windows
    desktop._kernel32 = FakeKernel()
    monkeypatch.setattr(win_desktop, "time", SimpleNamespace(time=lambda: 10_000.0))
    assert desktop.boot_time() == 10_000.0 - 3_600.0


def test_cursor_launch_is_a_fixed_argv_with_one_uri():
    uri = "vscode-remote://wsl%2Bx/home/user/src/a; calc.exe"
    argv = cursor_launch_argv(r"C:\Users\user\AppData\Local", uri)
    assert argv == [
        r"C:\Users\user\AppData\Local\Programs\cursor\Cursor.exe",
        "--folder-uri",
        uri,
    ]


def test_fake_desktop_records_opened_folders_and_runs_the_hook():
    seen = []
    fake = FakeDesktop()
    fake.on_open = seen.append
    assert fake.open_folder("vscode-remote://wsl%2Bx/home/user/src/a") == LaunchOutcome(True)
    assert fake.opened == seen == ["vscode-remote://wsl%2Bx/home/user/src/a"]


def test_fake_desktop_can_fail_to_open():
    fake = FakeDesktop(can_open=False)
    outcome = fake.open_folder("vscode-remote://x/y")
    assert outcome == LaunchOutcome(False, "could not launch Cursor (fake)")
    assert fake.opened == ["vscode-remote://x/y"]


@pytest.mark.parametrize("outcome", [LaunchOutcome(True), LaunchOutcome(False, "no Cursor")])
def test_launch_outcome_is_never_a_truth_value(outcome):
    # A dataclass is always truthy, so `if not outcome:` would never fire and a failed launch
    # would pass for a success. The guard makes that misuse fail loudly instead.
    with pytest.raises(TypeError, match=r"check \.launched"):
        bool(outcome)
    with pytest.raises(TypeError, match=r"check \.launched"):
        if not outcome:
            raise AssertionError("unreachable: the truth test must raise first")


LOCAL_APP_DATA = r"C:\Users\user\AppData\Local"
CURSOR_PATH = r"C:\Users\user\AppData\Local\Programs\cursor\Cursor.exe"
FOLDER_URI = "vscode-remote://wsl%2Bx/home/user/src/a"


class RecordingPopen:
    """Stands in for subprocess.Popen: remembers every launch and can fail on request."""

    def __init__(self, error=None):
        self.error = error
        self.launches = []

    def __call__(self, argv, **kwargs):
        self.launches.append((argv, kwargs))
        if self.error is not None:
            raise self.error


def test_launch_cursor_starts_the_fixed_argv_at_normal_priority():
    popen = RecordingPopen()
    uri = "vscode-remote://wsl%2Bx/home/user/src/a; calc.exe"
    outcome = launch_cursor(
        LOCAL_APP_DATA, uri, popen=popen, is_file=lambda path: path == CURSOR_PATH
    )
    assert outcome == LaunchOutcome(True)
    [(argv, kwargs)] = popen.launches
    assert argv == [CURSOR_PATH, "--folder-uri", uri]
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS
    assert kwargs["creationflags"] == 0x8 | 0x200 | 0x20
    assert "shell" not in kwargs


@pytest.mark.parametrize("missing", [None, ""])
def test_launch_cursor_stops_when_local_app_data_is_missing(missing):
    popen = RecordingPopen()
    outcome = launch_cursor(missing, FOLDER_URI, popen=popen, is_file=lambda path: True)
    assert outcome == LaunchOutcome(False, "LOCALAPPDATA is not set")
    assert popen.launches == []


def test_launch_cursor_names_the_path_when_cursor_is_not_installed():
    popen = RecordingPopen()
    outcome = launch_cursor(LOCAL_APP_DATA, FOLDER_URI, popen=popen, is_file=lambda path: False)
    assert outcome.launched is False
    assert CURSOR_PATH in outcome.error
    assert popen.launches == []


def test_launch_cursor_turns_an_os_error_into_an_outcome():
    popen = RecordingPopen(error=OSError("boom"))
    outcome = launch_cursor(LOCAL_APP_DATA, FOLDER_URI, popen=popen, is_file=lambda path: True)
    assert outcome.launched is False
    assert outcome.error.startswith("could not launch Cursor")
    assert "boom" in outcome.error


def test_launch_cursor_turns_a_value_error_into_an_outcome():
    # Popen raises ValueError, not OSError, for an embedded NUL in argv.
    popen = RecordingPopen(error=ValueError("embedded null byte"))
    outcome = launch_cursor(LOCAL_APP_DATA, FOLDER_URI, popen=popen, is_file=lambda path: True)
    assert outcome.launched is False
    assert outcome.error.startswith("could not launch Cursor")
    assert "embedded null byte" in outcome.error


def test_windows_desktop_open_folder_survives_a_missing_local_app_data(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    desktop = object.__new__(WindowsDesktop)  # skip __init__: it only runs on Windows
    assert desktop.open_folder(FOLDER_URI) == LaunchOutcome(False, "LOCALAPPDATA is not set")
