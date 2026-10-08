"""WindowsDesktop: the real desktop behind the Desktop protocol. Windows only.

Importing this module anywhere is safe; constructing WindowsDesktop off Windows raises.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import PureWindowsPath

from .actions import IDLE_GUARD_SECONDS
from .desktop import DesktopState, LaunchOutcome, ProbeOutcome
from .win_input import (
    INPUT,
    KEYEVENTF_KEYUP,
    LASTINPUTINFO,
    VK_MENU,
    WTS_CURRENT_SESSION,
    WTS_SESSION_INFO_EX,
    WTS_SESSIONSTATE_LOCK,
    WTS_SESSIONSTATE_UNLOCK,
    WTSINFOEXW,
    keyboard_input,
    unicode_input,
)

DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
UNLOCKED_DESKTOP = "Default"
ARRIVAL_TIMEOUT_SECONDS = 2.0
CURSOR_EXE = ("Programs", "cursor", "Cursor.exe")
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
# A Cursor started here would otherwise inherit the scheduled task's below-normal priority.
NORMAL_PRIORITY_CLASS = 0x00000020


def cursor_launch_argv(local_app_data: str, uri: str) -> list[str]:
    """Fixed argv: Cursor's own executable and one folder URI. Never a shell (rule C3)."""
    return [str(PureWindowsPath(local_app_data, *CURSOR_EXE)), "--folder-uri", uri]


def launch_cursor(
    local_app_data: str | None,
    uri: str,
    *,
    popen: Callable[..., object],
    is_file: Callable[[str], bool],
) -> LaunchOutcome:
    """Start Cursor with a fixed argv, or hand the folder to the running Cursor. Never raises."""
    if not local_app_data:
        return LaunchOutcome(False, "LOCALAPPDATA is not set")
    argv = cursor_launch_argv(local_app_data, uri)
    if not is_file(argv[0]):
        return LaunchOutcome(False, f"Cursor is not installed at {argv[0]}")
    try:
        popen(  # fixed executable; the URI is built from validated parts (rule C3)
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | NORMAL_PRIORITY_CLASS,
        )
    except (OSError, ValueError) as exc:
        return LaunchOutcome(False, f"could not launch Cursor: {exc}")
    return LaunchOutcome(True)


def lock_state(session_flag: int | None, input_desktop: str) -> tuple[bool, bool]:
    """(locked, session_locked): locked on any doubt, session_locked only on positive proof."""
    session_locked = session_flag == WTS_SESSIONSTATE_LOCK
    locked = session_flag != WTS_SESSIONSTATE_UNLOCK or input_desktop != UNLOCKED_DESKTOP
    return locked, session_locked


class WindowsDesktop:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("WindowsDesktop only runs on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wtsapi = ctypes.WinDLL("wtsapi32", use_last_error=True)
        wtsapi.WTSQuerySessionInformationW.restype = ctypes.c_int
        wtsapi.WTSQuerySessionInformationW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        wtsapi.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.OpenInputDesktop.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        user32.GetUserObjectInformationW.restype = ctypes.c_int
        user32.GetUserObjectInformationW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        user32.GetLastInputInfo.restype = ctypes.c_int
        user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
        user32.SendInput.restype = ctypes.c_uint
        user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.SetForegroundWindow.restype = ctypes.c_int
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        kernel32.GetTickCount.restype = ctypes.c_uint32
        kernel32.GetTickCount64.restype = ctypes.c_uint64
        self._user32 = user32
        self._kernel32 = kernel32
        self._wtsapi = wtsapi

    def _input_desktop_name(self) -> str:
        handle = self._user32.OpenInputDesktop(0, 0, DESKTOP_READOBJECTS)
        if not handle:
            return ""  # the secure (lock or UAC) desktop cannot be opened from a normal process
        try:
            buf = ctypes.create_unicode_buffer(256)
            needed = ctypes.c_uint32(0)
            ok = self._user32.GetUserObjectInformationW(
                handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)
            )
            return buf.value if ok else ""
        finally:
            self._user32.CloseDesktop(handle)

    def boot_time(self) -> float | None:
        return time.time() - self._kernel32.GetTickCount64() / 1000

    def _last_input_tick(self) -> int:
        info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        if not self._user32.GetLastInputInfo(ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.dwTime)

    def _session_flag(self) -> int | None:
        """The session's lock flag when Windows reports it; None when the query fails."""
        buf = ctypes.c_void_p()
        size = ctypes.c_uint32()
        if not self._wtsapi.WTSQuerySessionInformationW(
            None, WTS_CURRENT_SESSION, WTS_SESSION_INFO_EX, ctypes.byref(buf), ctypes.byref(size)
        ):
            return None
        try:
            info = ctypes.cast(buf, ctypes.POINTER(WTSINFOEXW)).contents
            return int(info.Data.SessionFlags) if info.Level == 1 else None
        finally:
            self._wtsapi.WTSFreeMemory(buf)

    def state(self) -> DesktopState:
        name = self._input_desktop_name()
        idle_ms = (self._kernel32.GetTickCount() - self._last_input_tick()) & 0xFFFFFFFF
        # Locked if either signal says so: the session flag catches the Win+L lock screen
        # (which stays on the Default desktop), the desktop name catches UAC prompts. Only the
        # session flag proves a lock, so a UAC prompt is locked but not a locked session.
        locked, session_locked = lock_state(self._session_flag(), name)
        return DesktopState(
            input_desktop=name,
            locked=locked,
            idle_seconds=idle_ms / 1000.0,
            session_locked=session_locked,
        )

    def open_folder(self, uri: str) -> LaunchOutcome:
        # A running Cursor takes the request over and this process exits at once.
        return launch_cursor(
            os.environ.get("LOCALAPPDATA"), uri, popen=subprocess.Popen, is_file=os.path.isfile
        )

    def _send(self, events: list[INPUT]) -> int:
        array = (INPUT * len(events))(*events)
        return int(self._user32.SendInput(len(events), array, ctypes.sizeof(INPUT)))

    def _is_foreground(self, hwnd: int) -> bool:
        return (self._user32.GetForegroundWindow() or 0) == hwnd

    def probe_input(
        self, text: str, should_stop: Callable[[], bool] = lambda: False
    ) -> ProbeOutcome:
        import tkinter as tk

        previous = self._user32.GetForegroundWindow()
        hwnd = 0
        root = tk.Tk()
        try:
            root.title("Remote Hands probe")
            root.attributes("-topmost", True)
            entry = tk.Entry(root, width=32)
            entry.pack(padx=12, pady=12)
            root.update()
            hwnd = int(root.wm_frame(), 16)
            if not hwnd:
                return ProbeOutcome(False, "none", 0, text, "")

            # From here on nobody may touch the PC. Our own injected input moves the
            # last-input tick, so the baseline is re-read right after each injection.
            baseline = self._last_input_tick()
            idle_ms = (self._kernel32.GetTickCount() - baseline) & 0xFFFFFFFF
            if idle_ms < IDLE_GUARD_SECONDS * 1000:
                return ProbeOutcome(False, "aborted_user_active", 0, text, "")
            method = "none"
            root.focus_force()
            entry.focus_set()
            root.update()
            if self._is_foreground(hwnd):
                method = "focus_force"
            else:
                if self._last_input_tick() != baseline:
                    return ProbeOutcome(False, "aborted_user_active", 0, text, "")
                alt = [keyboard_input(VK_MENU, 0), keyboard_input(VK_MENU, KEYEVENTF_KEYUP)]
                if should_stop():
                    return ProbeOutcome(False, "aborted_kill_switch", 0, text, "")
                self._send(alt)
                baseline = self._last_input_tick()
                self._user32.SetForegroundWindow(hwnd)
                entry.focus_set()
                root.update()
                if self._is_foreground(hwnd):
                    method = "alt_tap"
                elif self._last_input_tick() == baseline:
                    if should_stop():
                        return ProbeOutcome(False, "aborted_kill_switch", 0, text, "")
                    self._send(alt)  # take the user's app back out of menu mode, only if untouched
            if method == "none":
                return ProbeOutcome(False, method, 0, text, "")

            # Build the events first so the three guards below run immediately before the
            # single SendInput call, with nothing slow in between.
            events = [
                ev
                for ch in text
                for ev in (unicode_input(ch, 0), unicode_input(ch, KEYEVENTF_KEYUP))
            ]
            if should_stop():
                return ProbeOutcome(False, "aborted_kill_switch", 0, text, "")
            if self._last_input_tick() != baseline:
                return ProbeOutcome(False, "aborted_user_active", 0, text, "")
            if not self._is_foreground(hwnd):
                return ProbeOutcome(False, "aborted_focus_lost", 0, text, "")
            sent = self._send(events)
            deadline = time.monotonic() + ARRIVAL_TIMEOUT_SECONDS
            while entry.get() != text and time.monotonic() < deadline:
                root.update()
                time.sleep(0.05)
            return ProbeOutcome(True, method, sent, text, entry.get())
        finally:
            # Give focus back only if nobody else took it while the probe ran.
            current = self._user32.GetForegroundWindow() or 0
            restore = bool(previous) and current in (0, hwnd)
            root.destroy()
            if restore:
                self._user32.SetForegroundWindow(previous)
