"""Window bridge registrations: which Cursor window has which folder open.

Each window's bridge writes one JSON file into the Remote Hands home. A file can outlive
its window (a crash skips cleanup), so a registration counts only after its bridge answers
a ping with the same instance id. A bridge checks every 30 s for its registration and
rewrites it if it is missing (bridge/src/extension.js HEARTBEAT_MS), well inside the
service's 90 s wait for a new window (actions.WINDOW_WAIT_SECONDS), so a registration deleted
after a refused ping heals during that wait. Only a refused connection or an answer that is not
the window's deletes one; any other ping failure counts as busy (rule C4).
"""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

INSTANCE_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass(frozen=True)
class Registration:
    instance: str
    port: int
    folders: tuple[str, ...]
    path: Path


@dataclass(frozen=True)
class WindowLookup:
    window: Registration | None  # a live window whose folders include the repo
    busy: tuple[str, ...] = ()  # instance ids that matched the repo but did not answer in time
    removed: tuple[str, ...] = ()  # instance ids of stale registrations deleted on the way


class Pinger(Protocol):
    def ping(self, port: int) -> str | None:
        """The instance id the bridge on this port reports; None if nothing of ours is there (the
        connection was refused, or a whole reply that is not a bridge's came back).

        Raises on any other failure: TimeoutError when something accepted the connection but did
        not answer, and any other error (a reset, a reply that is not HTTP) as it came.
        """
        ...


def _parse(path: Path) -> Registration | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    instance, port, folders = data.get("instance"), data.get("port"), data.get("folders")
    if not isinstance(instance, str) or not INSTANCE_PATTERN.fullmatch(instance):
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if not isinstance(folders, list) or not all(isinstance(f, str) for f in folders):
        return None
    return Registration(instance, port, tuple(folders), path)


def load_registrations(directory: Path) -> list[Registration]:
    if not directory.is_dir():
        return []
    parsed = [_parse(path) for path in sorted(directory.glob("*.json"))]
    return [reg for reg in parsed if reg is not None]


def find_window(directory: Path, repo: str, pinger: Pinger) -> WindowLookup:
    """The live window whose workspace folders include the repo (single-folder windows first),
    plus what the scan saw.
    """
    matching = [reg for reg in load_registrations(directory) if repo in reg.folders]
    busy: list[str] = []
    removed: list[str] = []
    for reg in sorted(matching, key=lambda r: (len(r.folders) != 1, r.path.name)):
        # Only a refused connection or an answer that is not this window's shows the window is gone.
        # Any other failure of the ping itself (a timeout, a reset, a reply that is not HTTP, any other
        # OSError) is unclear, and an unclear ping never deletes a registration: the window counts as
        # busy (rule C4). Anything else is a fault of this code and is not caught here.
        try:
            answered = pinger.ping(reg.port)
        except ConnectionRefusedError:
            answered = None
        except (
            OSError,
            http.client.HTTPException,
        ):  # TimeoutError and ConnectionResetError included
            busy.append(reg.instance)  # busy, not gone: leave its registration alone
            continue
        if answered == reg.instance:
            return WindowLookup(reg, tuple(busy), tuple(removed))
        try:
            reg.path.unlink()  # nothing answers there, or another window owns the port now
        except OSError:
            pass  # already gone or locked: not reported as removed
        else:
            removed.append(reg.instance)
    return WindowLookup(None, tuple(busy), tuple(removed))
