"""Append-only audit log (rule C7). One JSON object per line; never holds the token."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": time.time(), **record}, sort_keys=True)
    with _LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
