"""The shared secret between the `hands` CLI and the Remote Hands service."""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

MIN_TOKEN_LENGTH = 32


def _check(token: str, path: Path) -> str:
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"token in {path} is too short; delete the file to regenerate it")
    return token


def load_token(path: Path) -> str:
    return _check(path.read_text(encoding="utf-8").strip(), path)


def load_or_create_token(path: Path) -> str:
    if path.exists():
        return load_token(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        try:
            # Atomic publish: the token file appears with its full content or not at all.
            os.link(tmp, path)
        except FileExistsError:
            return load_token(path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass  # a stray .tmp is harmless; failing the service start is not
    return token


def token_matches(presented: str | None, expected: str) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
