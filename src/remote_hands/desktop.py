"""The desktop as Remote Hands sees it, plus a fake for tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DesktopState:
    input_desktop: str  # "Default" when unlocked; "Winlogon" or "" on the lock/UAC screen
    locked: bool  # any doubt reads as locked: nothing may be typed
    idle_seconds: float
    session_locked: bool = False  # True only when Windows positively reports the session locked


@dataclass(frozen=True)
class ProbeOutcome:
    foreground_ok: bool  # Remote Hands' own scratch window was in front before any key was sent
    focus_method: (
        str  # "focus_force", "alt_tap", "none", "aborted_user_active", "aborted_focus_lost",
        # or "aborted_kill_switch"
    )
    keys_sent: int  # events SendInput reported as injected
    text_expected: str
    text_seen: str

    @property
    def arrived(self) -> bool:
        return self.foreground_ok and self.text_seen == self.text_expected


@dataclass(frozen=True)
class LaunchOutcome:
    launched: bool  # Cursor was started, or the request was handed to the running Cursor
    error: str = ""  # why not, when launched is False

    def __bool__(self) -> bool:
        # A dataclass is always truthy, so `if not outcome:` would silently never fire.
        raise TypeError("check .launched, not the outcome itself")


class Desktop(Protocol):
    def state(self) -> DesktopState: ...

    def boot_time(self) -> float | None:
        """Epoch seconds when Windows last started, or None if unknown."""
        ...

    def probe_input(
        self, text: str, should_stop: Callable[[], bool] = lambda: False
    ) -> ProbeOutcome: ...

    def open_folder(self, uri: str) -> LaunchOutcome:
        """Ask Cursor to open (or focus) the window for this folder URI."""
        ...


class FakeDesktop:
    def __init__(
        self,
        *,
        locked: bool = False,
        idle_seconds: float = 600.0,
        can_focus: bool = True,
        input_works: bool = True,
        can_open: bool = True,
        session_locked: bool | None = None,
        boot_time: float | None = 0.0,
    ) -> None:
        self.locked = locked
        self._session_locked = session_locked
        self._boot_time = boot_time
        self.idle_seconds = idle_seconds
        self.can_focus = can_focus
        self.input_works = input_works
        self.probe_calls: list[str] = []
        self.can_open = can_open
        self.opened: list[str] = []
        self.on_open: Callable[[str], None] | None = None

    @property
    def session_locked(self) -> bool:
        """Follows `locked` unless a test sets it: a UAC prompt is locked without a locked session."""
        return self.locked if self._session_locked is None else self._session_locked

    def state(self) -> DesktopState:
        return DesktopState(
            input_desktop="Winlogon" if self.locked else "Default",
            locked=self.locked,
            idle_seconds=self.idle_seconds,
            session_locked=self.session_locked,
        )

    def boot_time(self) -> float | None:
        return self._boot_time

    def probe_input(
        self, text: str, should_stop: Callable[[], bool] = lambda: False
    ) -> ProbeOutcome:
        self.probe_calls.append(text)
        if should_stop():
            return ProbeOutcome(False, "aborted_kill_switch", 0, text, "")
        if not self.can_focus:
            return ProbeOutcome(False, "none", 0, text, "")
        seen = text if self.input_works else ""
        return ProbeOutcome(True, "focus_force", 2 * len(text), text, seen)

    def open_folder(self, uri: str) -> LaunchOutcome:
        self.opened.append(uri)
        if not self.can_open:
            return LaunchOutcome(False, "could not launch Cursor (fake)")
        if self.on_open is not None:
            self.on_open(uri)
        return LaunchOutcome(True)
