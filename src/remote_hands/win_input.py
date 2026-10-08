"""Fixed-width ctypes structures for SendInput and GetLastInputInfo (64-bit Windows layout).

Fixed-width types, not ctypes.wintypes, so the layout is identical on every platform and can
be size-tested on Linux.
"""

from __future__ import annotations

import ctypes

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_MENU = 0x12  # ALT


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("dwTime", ctypes.c_uint32)]


def keyboard_input(vk: int, flags: int) -> INPUT:
    ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=ki))


def unicode_input(char: str, flags: int) -> INPUT:
    if len(char) != 1 or ord(char) > 0xFFFF:
        raise ValueError("unicode_input takes exactly one BMP character")
    ki = KEYBDINPUT(
        wVk=0, wScan=ord(char), dwFlags=KEYEVENTF_UNICODE | flags, time=0, dwExtraInfo=0
    )
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=ki))


# Session lock state. On Windows 10 and later the lock screen (LockApp.exe) runs on the
# Default desktop, so the input-desktop name cannot detect it; the session's lock flag can.
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_SESSION_INFO_EX = 25  # WTS_INFO_CLASS.WTSSessionInfoEx
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1


class WTSINFOEX_LEVEL1_W(ctypes.Structure):  # noqa: N801 (Win32 name)
    # WCHAR arrays are declared as uint16 so the layout matches Windows on every platform.
    _fields_ = [
        ("SessionId", ctypes.c_uint32),
        ("SessionState", ctypes.c_int32),
        ("SessionFlags", ctypes.c_int32),
        ("WinStationName", ctypes.c_uint16 * 33),
        ("UserName", ctypes.c_uint16 * 21),
        ("DomainName", ctypes.c_uint16 * 18),
        ("LogonTime", ctypes.c_int64),
        ("ConnectTime", ctypes.c_int64),
        ("DisconnectTime", ctypes.c_int64),
        ("LastInputTime", ctypes.c_int64),
        ("CurrentTime", ctypes.c_int64),
        ("IncomingBytes", ctypes.c_uint32),
        ("OutgoingBytes", ctypes.c_uint32),
        ("IncomingFrames", ctypes.c_uint32),
        ("OutgoingFrames", ctypes.c_uint32),
        ("IncomingCompressedBytes", ctypes.c_uint32),
        ("OutgoingCompressedBytes", ctypes.c_uint32),
    ]


class WTSINFOEXW(ctypes.Structure):
    _fields_ = [("Level", ctypes.c_uint32), ("Data", WTSINFOEX_LEVEL1_W)]
