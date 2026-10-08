"""hands: the thin client an agent calls. One action per call, one JSON result on stdout.

Exit code 0 means status "ok"; anything else exits 1 so scripts can gate on it. Missing or
malformed flags exit 2 with usage on stderr; nothing is sent.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import stat
import time
import urllib.parse
import uuid
from typing import Any

from . import paths
from .auth import load_token
from .contract import BAD_REQUEST, NEEDS_HUMAN, OK, SERVICE_DOWN, ActionRequest
from .journal import check_request_id
from .readback import REPLIED, STARTED, check_session_id, wait_for_reply
from .registry import load_registrations
from .start_request import (
    MAX_PROMPT_CHARS,
    check_label,
    check_prompt,
    check_remote,
    session_id_for,
)

DEFAULT_URL = "http://127.0.0.1:47821/action"
CONNECT_TIMEOUT_SECONDS = 5.0  # on mirrored WSL a closed Windows port times out instead of refusing
START_TIMEOUT_SECONDS = 150  # the service may wait 90 s for a new window to register
DEFAULT_START_WAIT_SECONDS = 120.0
MAX_WAIT_SECONDS = 300
MAX_PROMPT_BYTES = MAX_PROMPT_CHARS * 4  # UTF-8 uses at most 4 bytes per character
OFF_DRAIN_SECONDS = 15.0
OFF_POLL_SECONDS = 0.5
_sleep = time.sleep  # module-level so tests can drive the clock
_monotonic = time.monotonic


def _url() -> str:
    url = os.environ.get("REMOTE_HANDS_URL", DEFAULT_URL)
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValueError("REMOTE_HANDS_URL contains whitespace or control characters")
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"REMOTE_HANDS_URL has a bad port: {exc}") from exc
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.username is not None
        or parts.password is not None
        or port is None
        or parts.path != "/action"
        or parts.query
        or parts.fragment
    ):
        raise ValueError("REMOTE_HANDS_URL must be http://127.0.0.1:<port>/action (rule C1)")
    return url


def _transport_failure(request_id: str, error: str, **extra: Any) -> dict:
    """The request was sent, so the service may have acted on it: not "nothing sent"."""
    detail = {"step": "transport", "sent": True, "error": error, **extra}
    return {"status": NEEDS_HUMAN, "request_id": request_id, "detail": detail}


def call(
    action: str,
    args: dict[str, Any],
    *,
    url: str,
    token: str,
    timeout: float,
    request_id: str | None = None,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
) -> dict:
    """POST one action to the service and return its reply.

    Only a failed connect is `service_down`: nothing was sent, so a retry is safe. Once the
    request is on the wire, a reply that never comes, a reset, or a body that is not a JSON
    object is `needs_human` step `transport`: the service may already have acted on it.
    http.client never uses a proxy, so loopback is never routed through one.
    """
    req = ActionRequest(action=action, request_id=request_id or uuid.uuid4().hex, args=args)
    parts = urllib.parse.urlsplit(url)  # callers pass a URL that _url() has checked
    conn = http.client.HTTPConnection(
        parts.hostname, parts.port, timeout=min(connect_timeout, timeout)
    )
    try:
        try:
            conn.connect()
        except OSError as exc:  # nothing was sent
            refused = isinstance(exc, ConnectionRefusedError)
            detail = {"error": str(exc), "refused": refused}
            return {"status": SERVICE_DOWN, "request_id": req.request_id, "detail": detail}
        conn.sock.settimeout(timeout)
        try:
            conn.request(
                "POST",
                parts.path,
                body=req.to_json(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            code, raw = resp.status, resp.read()
        except (OSError, http.client.HTTPException) as exc:  # the request may have arrived
            return _transport_failure(req.request_id, f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()
    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, RecursionError):  # too deeply nested to parse counts as unusable
        payload = None
    if not isinstance(payload, dict):
        error = "the service answered with something that is not a JSON object"
        return _transport_failure(req.request_id, error, http_status=code)
    if code == 200:
        if not isinstance(payload.get("status"), str):
            error = "the service answered without a status"
            return _transport_failure(req.request_id, error, http_status=code)
        return payload
    payload.setdefault("status", "bad_request")
    return {**payload, "http_status": code}


def _emit(result: dict) -> int:
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == OK else 1


def _local_error(message: str) -> dict:
    return {"status": BAD_REQUEST, "request_id": "local", "detail": {"error": message}}


def _needs_human(message: str) -> dict:
    """The request was valid; the environment failed, so a person has to look."""
    return {"status": NEEDS_HUMAN, "request_id": "local", "detail": {"error": message}}


def _error_text(result: dict) -> str | None:
    """Why a health call was not a plain success, or None when it was."""
    if result.get("status") == OK:
        return None
    detail = result.get("detail")
    error = detail.get("error") if isinstance(detail, dict) else None
    if isinstance(error, str) and error:
        return error
    if "http_status" in result:
        return f"HTTP {result['http_status']}"
    return f"the service answered {result.get('status')!r}"


def _service_sees(
    kill_switch: bool, url: str, token: str
) -> tuple[bool | None, list[str] | None, str | None]:
    """(does the service see the switch as expected, actions still in flight or None if unknown,
    the health call's error text or None).

    Nothing listening means nothing can act, so in flight is empty. A service that answers
    with an error, or does not answer in time, may still be running an action: unknown.
    """
    result = call("health", {}, url=url, token=token, timeout=3, connect_timeout=3)
    error = _error_text(result)
    if result.get("http_status") == 401:
        return False, None, error  # the service holds a different token, so another home
    detail = result.get("detail")
    if not isinstance(detail, dict):
        detail = {}
    if result.get("status") == SERVICE_DOWN and detail.get("refused"):
        return None, [], error
    if result.get("status") != OK:
        return None, None, error
    in_flight = detail.get("in_flight")
    if not isinstance(in_flight, list):
        return None, None, "health did not report the actions in flight"
    return detail.get("kill_switch") == kill_switch, in_flight, None


def _local(kill_switch: bool) -> dict:
    """Confirm the switch with the service; when switching off, wait for running actions."""
    try:
        url = _url()
        token = load_token(paths.token_path())
    except (OSError, ValueError) as exc:
        # A bad URL or an unreadable token cannot fix itself, so there is nothing to wait for.
        detail: dict[str, Any] = {
            "kill_switch": kill_switch,
            "service_confirms": None,
            "in_flight": None,
            "error": f"cannot ask the service: {exc}",
        }
        if kill_switch:
            detail["last_error"] = None  # no health call was made
        # The switch file is already written or removed; only confirming it is impossible.
        return {
            "status": NEEDS_HUMAN if kill_switch else OK,
            "request_id": "local",
            "detail": detail,
        }
    deadline = _monotonic() + (OFF_DRAIN_SECONDS if kill_switch else 0.0)
    while True:
        confirms, in_flight, last_error = _service_sees(kill_switch, url, token)
        # Settled: the service disagrees (polling will not change that), or nothing is in
        # flight. Unknown or running: keep polling until the deadline.
        settled = confirms is False or in_flight == []
        if not kill_switch or settled or _monotonic() >= deadline:
            break
        _sleep(OFF_POLL_SECONDS)
    still_running = kill_switch and (in_flight is None or bool(in_flight))
    detail = {"kill_switch": kill_switch, "service_confirms": confirms, "in_flight": in_flight}
    if kill_switch and (confirms is not True or in_flight is None):
        detail["last_error"] = last_error  # why the answer is unconfirmed or unknown
    return {
        "status": NEEDS_HUMAN if confirms is False or still_running else OK,
        "request_id": "local",
        "detail": detail,
    }


def _check_wait(wait: float) -> float:
    if not 0 <= wait <= MAX_WAIT_SECONDS:
        raise ValueError(f"--wait must be 0 to {MAX_WAIT_SECONDS} seconds")
    return wait


def _read_prompt(source: str) -> str:
    if source == "-":
        raise ValueError("write the prompt to a file; stdin is not accepted")
    # O_NONBLOCK keeps a pipe with no writer from hanging the open. The type is checked on the
    # open descriptor, not on the path, so the file that was checked is the file that is read.
    fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(
                "the prompt file must be a regular file "
                "(write the prompt to a file; pipes and devices are not accepted)"
            )
        with open(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_PROMPT_BYTES + 1)  # never read a file of any size into memory
    finally:
        os.close(fd)
    if len(raw) > MAX_PROMPT_BYTES:
        raise ValueError(f"prompt file is larger than {MAX_PROMPT_BYTES} bytes")
    return raw.decode("utf-8")


def _wsl_remote() -> str:
    distro = os.environ.get("WSL_DISTRO_NAME", "")
    if not distro:
        raise ValueError("WSL_DISTRO_NAME is not set; run hands inside WSL")
    if not distro.isascii():  # lower() folds some non-ASCII letters (the Kelvin sign) to ASCII
        raise ValueError("WSL_DISTRO_NAME must be ASCII")
    return "wsl+" + distro.lower()


def _start_claude(args: argparse.Namespace, url: str, token: str) -> dict:
    repo = os.path.normpath(os.path.abspath(args.repo))
    if not os.path.isdir(repo) or not os.path.exists(os.path.join(repo, ".git")):
        return _local_error(f"{repo} is not a git repository")
    # Whatever the service would refuse anyway is refused here, before anything is sent.
    try:
        request_id = check_request_id(args.request_id)
        wait = _check_wait(args.wait)
        prompt = check_prompt(_read_prompt(args.prompt_file))
        label = None if args.label is None else check_label(args.label)
        remote = check_remote(_wsl_remote())
        projects = paths.claude_projects_dir()  # needed for read-back: refuse before starting
    except (OSError, ValueError) as exc:  # a bad UTF-8 prompt raises a ValueError too
        return _local_error(str(exc))
    payload: dict[str, Any] = {"repo": repo, "remote": remote, "prompt": prompt}
    if label is not None:
        payload["label"] = label
    result = call(
        "start_claude",
        payload,
        url=url,
        token=token,
        timeout=START_TIMEOUT_SECONDS,
        request_id=request_id,
    )
    detail = result.get("detail")
    if not isinstance(detail, dict):
        detail = {}
    if detail.get("step") == "transport":
        # The request may have reached the service, so a session may have started.
        error = (
            "the request may have reached the service, so a session may have started: "
            "read it back with this session_id, and retry only with the same --request-id"
        )
        return {
            **result,
            "detail": {
                **detail,
                "session_id": session_id_for(request_id),
                "transport_error": detail.get("error"),
                "error": error,
            },
        }
    if result.get("status") != OK:
        return result
    try:
        session_id = check_session_id(detail.get("session_id"))
    except ValueError:
        error = "the service did not return a session id"
        return {**result, "status": NEEDS_HUMAN, "detail": {**detail, "error": error}}
    if wait == 0:
        return result
    reply = wait_for_reply(
        projects,
        session_id,
        wait_seconds=wait,
        expected_prompt=prompt,
        sleep=_sleep,
        monotonic=_monotonic,
    )
    reply.pop("session_id", None)  # already in the detail
    out = {**result, "detail": {**detail, "reply": reply}}
    problem = None
    if reply["state"] not in (STARTED, REPLIED):
        problem = f"the session wrote no readable record within {wait:.0f} s; read it back later"
    elif not reply.get("prompt_found"):
        problem = "the session record does not show the submitted prompt"  # rule C4
    elif reply.get("reply_is_error"):
        problem = "the session's first reply is an API error"
    if problem:
        out["status"] = NEEDS_HUMAN
        out["detail"]["error"] = problem
    return out


def _read_back(args: argparse.Namespace) -> dict:
    try:
        reply = wait_for_reply(
            paths.claude_projects_dir(),
            args.session,
            wait_seconds=_check_wait(args.wait),
            sleep=_sleep,
            monotonic=_monotonic,
        )
    except ValueError as exc:
        return _local_error(str(exc))
    readable = reply["state"] in (STARTED, REPLIED)
    status = OK if readable and not reply.get("reply_is_error") else NEEDS_HUMAN
    return {"status": status, "request_id": "local", "detail": reply}


def _windows() -> dict:
    try:
        registrations = load_registrations(paths.windows_dir())
    except (OSError, ValueError) as exc:
        return _local_error(f"cannot list the windows: {exc}")
    windows = [
        {"instance": reg.instance, "port": reg.port, "folders": list(reg.folders)}
        for reg in registrations
    ]
    return {"status": OK, "request_id": "local", "detail": {"windows": windows}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hands", description="Remote Hands client")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health", help="is Remote Hands up, and is the desktop locked")
    probe = sub.add_parser(
        "probe-input", help="diagnostic: type a test word into Remote Hands' own window"
    )
    probe.add_argument(
        "--delay", type=float, default=0.0, help="seconds to wait first (lock the PC during it)"
    )
    start = sub.add_parser(
        "start-claude", help="start a Claude session in the terminal of a repo's Cursor window"
    )
    start.add_argument("--repo", required=True, help="the repo's absolute path in WSL")
    start.add_argument(
        "--prompt-file",
        required=True,
        help="path of a UTF-8 file holding the prompt (stdin is not accepted)",
    )
    start.add_argument(
        "--request-id",
        required=True,
        help="8 to 64 lower-case letters, digits, '-' or '_'; mint one per request and reuse "
        "it on every retry of that request, so a retry never starts a second session",
    )
    start.add_argument("--label", help="session name (shown in Claude remote control)")
    start.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_START_WAIT_SECONDS,
        help=f"seconds to wait for the first reply (0 to {MAX_WAIT_SECONDS}, default "
        f"{DEFAULT_START_WAIT_SECONDS:.0f}); the caller must give this command a timeout of at "
        f"least {START_TIMEOUT_SECONDS} + wait + 30 seconds",
    )
    back = sub.add_parser("read-back", help="a session's first reply, from its own record")
    back.add_argument("--session", required=True, help="the session id start-claude returned")
    back.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help=f"seconds to wait for the first reply (0 to {MAX_WAIT_SECONDS})",
    )
    sub.add_parser("windows", help="Cursor windows whose bridge registered (not pinged)")
    sub.add_parser("off", help="kill switch: refuse every action that types, clicks, or starts")
    sub.add_parser("on", help="clear the kill switch")
    args = parser.parse_args(argv)

    if args.cmd == "off":
        try:
            path = paths.kill_switch_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        except (OSError, ValueError) as exc:
            return _emit(_needs_human(f"the kill switch was NOT set: {exc}"))
        return _emit(_local(True))
    if args.cmd == "on":
        try:
            paths.kill_switch_path().unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            return _emit(_needs_human(f"the kill switch was NOT cleared: {exc}"))
        return _emit(_local(False))
    if args.cmd == "read-back":
        return _emit(_read_back(args))
    if args.cmd == "windows":
        return _emit(_windows())

    try:
        url = _url()
    except ValueError as exc:
        return _emit(_local_error(str(exc)))
    try:
        token = load_token(paths.token_path())
    except (OSError, ValueError) as exc:
        return _emit(
            {"status": SERVICE_DOWN, "request_id": "local", "detail": {"error": f"token: {exc}"}}
        )
    if args.cmd == "health":
        return _emit(call("health", {}, url=url, token=token, timeout=10))
    if args.cmd == "start-claude":
        return _emit(_start_claude(args, url, token))
    return _emit(
        call(
            "probe_input",
            {"delay_seconds": args.delay},
            url=url,
            token=token,
            timeout=args.delay + 30,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
