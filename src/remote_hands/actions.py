"""The fixed Remote Hands action table (rule C2). Nothing outside ACTIONS can run."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from . import bridge_client
from .bridge_client import Bridge, HttpBridgeClient
from .contract import (
    BAD_REQUEST,
    NEEDS_HUMAN,
    OK,
    SERVICE_OFF,
    UNKNOWN_ACTION,
    USER_ACTIVE,
    ActionRequest,
    ActionResult,
)
from .desktop import Desktop
from .journal import (
    CLAIMED,
    FULFILLED,
    NOT_SUBMITTED,
    SUBMITTING,
    UNKNOWN,
    Journal,
    check_request_id,
)
from .registry import Registration, find_window
from .start_request import StartArgs, folder_uri, parse_start_args, session_id_for

IDLE_GUARD_SECONDS = 10.0
MAX_PROBE_DELAY_SECONDS = 120
PROBE_TEXT = "rhprobe"
WINDOW_WAIT_SECONDS = 90.0  # well above the bridge's 30 s heartbeat (bridge/src/extension.js)
WINDOW_POLL_SECONDS = 2.0
# The boot time is worked out from the tick count, so it can differ a little from the clock
# that stamps journal files: an entry written this close before it still counts as since boot.
BOOT_SLACK_SECONDS = 2.0
# Bridge refusals made before the bridge acts (bridge/src/server.js); any other refusal, or one
# without a reason, may have acted, so it counts as unknown.
NOTHING_SENT_REASONS = frozenset(
    {
        "kill_switch",
        "bad_request",
        "unauthorized",
        "too_large",
        "method",
        "path",
        "session_check_failed",
        "claude_unavailable",
        "audit_failed",
        "reservation_failed",
        "untrusted_folder",
        "trust_failed",
    }
)
BRIDGE_FIELDS = ("status", "reason", "error", "exit_code", "http_status", "terminal_pid")
MAX_BRIDGE_TEXT = 300


class InFlight:
    """Actions that may type, click, or start something, while they run (rule C8).

    dispatch tracks every action that is not read-only, so no action can forget to.
    `hands off` waits for this to empty before it reports that everything has stopped.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    @contextmanager
    def track(self, action: str) -> Iterator[None]:
        with self._lock:
            self._counts[action] = self._counts.get(action, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._counts[action] -= 1
                if not self._counts[action]:
                    del self._counts[action]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._counts)


@dataclass
class Context:
    desktop: Desktop
    kill_switch: Path
    sleep: Callable[[float], None] = time.sleep
    input_lock: threading.Lock = field(default_factory=threading.Lock)
    state_dir: Path | None = None  # the Remote Hands home: journal and window registrations
    token: str | None = field(default=None, repr=False)  # also presented to window bridges
    bridge: Bridge = field(default_factory=HttpBridgeClient)
    monotonic: Callable[[], float] = time.monotonic
    start_lock: threading.Lock = field(default_factory=threading.Lock)
    inflight: InFlight = field(default_factory=InFlight)


def kill_switch_on(ctx: Context) -> bool:
    """The one reading of the kill switch (C8): off unless the OFF file is provably absent.

    Any error but "not found" (an unreadable state folder, say) counts as switched off, as in the
    bridge (core.killSwitchOn). Path.exists() would not do: it reads several errors as "absent".
    """
    try:
        ctx.kill_switch.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def health(req: ActionRequest, ctx: Context) -> ActionResult:
    state = ctx.desktop.state()
    return ActionResult(
        OK,
        req.request_id,
        {
            "version": __version__,
            "kill_switch": kill_switch_on(ctx),
            "desktop_locked": state.locked,
            "session_locked": state.session_locked,
            "input_desktop": state.input_desktop,
            "idle_seconds": round(state.idle_seconds, 1),
            "in_flight": ctx.inflight.names(),
        },
    )


def _delay_seconds(req: ActionRequest) -> float:
    delay = req.args.get("delay_seconds", 0)
    if (
        isinstance(delay, bool)
        or not isinstance(delay, (int, float))
        or not 0 <= delay <= MAX_PROBE_DELAY_SECONDS
    ):
        raise ValueError(f"delay_seconds must be a number from 0 to {MAX_PROBE_DELAY_SECONDS}")
    return float(delay)


def probe_input(req: ActionRequest, ctx: Context) -> ActionResult:
    """Locked-PC diagnostic: type a fixed word into Remote Hands' own scratch window.

    Never types into the user's work: it refuses while the user is active (rule C5), and the desktop
    sends keys only after confirming its own window is in front.
    """
    try:
        delay = _delay_seconds(req)
    except ValueError as exc:
        return ActionResult(BAD_REQUEST, req.request_id, {"error": str(exc)})
    if kill_switch_on(ctx):
        return ActionResult(SERVICE_OFF, req.request_id)
    if not ctx.input_lock.acquire(blocking=False):
        return ActionResult(
            NEEDS_HUMAN,
            req.request_id,
            {"step": "input_lock", "error": "another input action is running"},
        )
    try:
        ctx.sleep(delay)
        if kill_switch_on(ctx):  # the user may have hit the switch during the wait.
            return ActionResult(SERVICE_OFF, req.request_id)
        state = ctx.desktop.state()
        if state.idle_seconds < IDLE_GUARD_SECONDS:
            return ActionResult(
                USER_ACTIVE,
                req.request_id,
                {"idle_seconds": round(state.idle_seconds, 1), "guard_seconds": IDLE_GUARD_SECONDS},
            )
        outcome = ctx.desktop.probe_input(PROBE_TEXT, should_stop=lambda: kill_switch_on(ctx))
        if outcome.focus_method == "aborted_kill_switch":
            return ActionResult(SERVICE_OFF, req.request_id)
        detail = {
            "desktop_locked": state.locked,
            "input_desktop": state.input_desktop,
            "foreground_ok": outcome.foreground_ok,
            "focus_method": outcome.focus_method,
            "keys_sent": outcome.keys_sent,
            "text_arrived": outcome.arrived,
            "text_seen_chars": len(outcome.text_seen),
        }
        if outcome.arrived:
            return ActionResult(OK, req.request_id, detail)
        return ActionResult(NEEDS_HUMAN, req.request_id, {"step": "probe", **detail})
    finally:
        ctx.input_lock.release()


def _bridge_summary(detail: dict) -> dict:
    """The known fields of a bridge reply, as short text or plain numbers.

    A bridge reply must never carry the prompt, or anything long, into results, the audit
    log, or the journal.
    """
    summary: dict = {}
    for key in BRIDGE_FIELDS:
        if key not in detail:
            continue
        value = detail[key]
        if isinstance(value, str):
            summary[key] = value[:MAX_BRIDGE_TEXT]
        elif value is None or (isinstance(value, int) and not isinstance(value, bool)):
            summary[key] = value
    return summary


def _written_since_boot(ctx: Context, journal: Journal, rid: str) -> bool:
    """True when the request's journal entry was written since Windows last started."""
    boot = ctx.desktop.boot_time()
    written = journal.written_at(rid)
    return boot is not None and written is not None and written >= boot - BOOT_SLACK_SECONDS


def _args_digest(args: StartArgs) -> str:
    """Fingerprint of what a request asks for, so a reused id cannot replay another request.

    The label is left out: it only names the terminal, so a retry that drops it must replay.
    """
    text = json.dumps([args.repo, args.remote, args.prompt])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _wait_for_window(
    ctx: Context, windows: Path, repo: str
) -> tuple[Registration | None, list[str], list[str], bool]:
    """Poll for the new window's bridge.

    Returns the live window (or None), every registration removed while polling, the windows
    that were still busy at the last poll, and whether the kill switch ended the wait. A busy
    answer keeps the poll going: a new window's bridge may be slow while it starts.
    """
    removed: list[str] = []
    deadline = ctx.monotonic() + WINDOW_WAIT_SECONDS
    while True:
        lookup = find_window(windows, repo, ctx.bridge)
        removed.extend(lookup.removed)
        if lookup.window is not None:
            return lookup.window, removed, list(lookup.busy), False
        if kill_switch_on(ctx):  # the switch was hit while the window starts: stop waiting
            return None, removed, list(lookup.busy), True
        if ctx.monotonic() >= deadline:  # after the switch, so a switch seen at the end still wins
            return None, removed, list(lookup.busy), False
        ctx.sleep(WINDOW_POLL_SECONDS)


def _window_for(
    ctx: Context,
    args: StartArgs,
    windows: Path,
    detail: dict,
    stop: Callable[[str, dict], ActionResult],
) -> ActionResult | tuple[Registration, bool]:
    """The live window for the repo (and whether it was just opened), or the stop result."""
    lookup = find_window(windows, args.repo, ctx.bridge)
    if lookup.removed:
        detail["removed_windows"] = list(lookup.removed)  # audit stale registrations (C7)
    if lookup.window is not None:
        return lookup.window, False
    if lookup.busy:
        # The repo's window is open but its bridge did not answer in time. Opening the folder
        # again would only focus that same window, so stop and say so (rule C4).
        return stop(
            NEEDS_HUMAN,
            {
                "step": "window_busy",
                "windows": list(lookup.busy),
                "error": "a window registration matched the repo but its bridge did not answer; "
                "the window may be busy, or windows/<id>.json may be stale (see `hands windows`); "
                "retry with the same request_id",
            },
        )
    state = ctx.desktop.state()
    if not state.session_locked and state.idle_seconds < IDLE_GUARD_SECONDS:
        # A new window takes the foreground, and the user's typing would land in it. Only a
        # positively locked session or real idleness permits a new window.
        return stop(
            USER_ACTIVE,
            {
                "step": "open_window",
                "idle_seconds": round(state.idle_seconds, 1),
                "guard_seconds": IDLE_GUARD_SECONDS,
                "session_locked": state.session_locked,
            },
        )
    if kill_switch_on(ctx):
        return stop(SERVICE_OFF, {"step": "open_window"})
    launch = ctx.desktop.open_folder(folder_uri(args.remote, args.repo))
    if not launch.launched:
        error = launch.error or "could not launch Cursor"
        return stop(NEEDS_HUMAN, {"step": "open_window", "error": error})
    detail["opened_window"] = True  # recorded at once, so a crash during the wait still shows it
    reg, removed_while_waiting, still_busy, switched_off = _wait_for_window(ctx, windows, args.repo)
    if removed_while_waiting:
        detail["removed_windows"] = detail.get("removed_windows", []) + removed_while_waiting
    if reg is not None:
        return reg, True
    if switched_off:
        return stop(SERVICE_OFF, {"step": "window", "opened_window": True})
    if still_busy:
        return stop(
            NEEDS_HUMAN,
            {
                "step": "window_busy",
                "opened_window": True,
                "windows": still_busy,
                "error": f"the new window registered but its bridge did not answer "
                f"within {WINDOW_WAIT_SECONDS:.0f} s; retry with the same request_id",
            },
        )
    return stop(
        NEEDS_HUMAN,
        {
            "step": "window",
            "opened_window": True,
            "error": f"no window bridge reported {args.repo} within {WINDOW_WAIT_SECONDS:.0f} s",
        },
    )


def start_claude(req: ActionRequest, ctx: Context) -> ActionResult:
    """Start a Claude session in the terminal of the repo's Cursor window.

    No keystrokes: the window bridge starts the Claude command line with the prompt as an
    argument, so this works while the PC is locked. A new window is opened only when no
    window has the repo open, and never while the user is using the PC (rule C5).
    """
    try:
        check_request_id(req.request_id)
        args = parse_start_args(req.args, req.request_id)
    except ValueError as exc:
        return ActionResult(BAD_REQUEST, req.request_id, {"error": str(exc)})
    if ctx.state_dir is None or ctx.token is None:
        return ActionResult(
            NEEDS_HUMAN,
            req.request_id,
            {"step": "config", "error": "service started without a home or token"},
        )
    if kill_switch_on(ctx):
        return ActionResult(SERVICE_OFF, req.request_id)
    if not ctx.start_lock.acquire(blocking=False):
        return ActionResult(
            NEEDS_HUMAN,
            req.request_id,
            {"step": "busy", "error": "another start is running; retry with the same request_id"},
        )
    try:  # dispatch tracks this action as in flight (rule C8)
        return _start_claude(req.request_id, ctx, args, ctx.state_dir, ctx.token)
    finally:
        ctx.start_lock.release()


def _start_claude(
    rid: str, ctx: Context, args: StartArgs, state_dir: Path, token: str
) -> ActionResult:
    journal = Journal(state_dir / "journal")
    session_id = session_id_for(rid)
    digest = _args_digest(args)
    detail: dict = {
        "session_id": session_id,
        "repo": args.repo,
        "label": args.label,
        "prompt_chars": len(args.prompt),
    }
    prior = journal.claim(rid)
    if prior is not None and prior.state == CLAIMED and _written_since_boot(ctx, journal, rid):
        # Under the start lock, a claimed entry can only belong to an attempt that died before
        # sending (the service stopped mid-start): nothing was sent, so this attempt takes over.
        # Only if the entry was written since the current boot: after an OS crash or power loss
        # the rename to submitting may have been lost, so an older claim is ambiguous and stays
        # blocked (needs_human, step journal).
        journal.mark(rid, NOT_SUBMITTED, {"error": "an earlier attempt stopped before sending"})
        prior = journal.claim(rid)
    if prior is not None:
        if prior.state == FULFILLED:
            if prior.detail.get("args_sha256") != digest:
                return ActionResult(
                    BAD_REQUEST,
                    rid,
                    {
                        "error": "a session already started for this request_id with a "
                        "different repo or prompt; read it back with its session_id "
                        "(a new request_id would start a second session)",
                        "session_id": session_id,
                        "journal_state": FULFILLED,
                    },
                )
            replay = {key: value for key, value in prior.detail.items() if key != "args_sha256"}
            return ActionResult(OK, rid, {**replay, "replayed": True})
        return ActionResult(
            NEEDS_HUMAN,
            rid,
            {
                **detail,
                "step": "journal",
                "journal_state": prior.state,
                "error": "this request may already have started a session; "
                "check read-back for its session_id before retrying",
            },
        )

    def stop(status: str, extra: dict) -> ActionResult:
        journal.mark(rid, NOT_SUBMITTED, extra)  # nothing was sent: the request may run again
        return ActionResult(status, rid, {**detail, **extra, "journal_state": NOT_SUBMITTED})

    try:
        found = _window_for(ctx, args, state_dir / "windows", detail, stop)
        if isinstance(found, ActionResult):
            return found
        reg, opened = found
        detail = {**detail, "window": reg.instance, "opened_window": opened}
        if kill_switch_on(ctx):
            return stop(SERVICE_OFF, {"step": "bridge"})
        journal.mark(rid, SUBMITTING, detail)
    except Exception as exc:
        # Nothing has been sent: the bridge call comes after this block. If the submitting mark
        # itself landed before the failure, the entry reads submitting and the id stays blocked.
        entry = journal.read(rid)
        if entry is not None and entry.state == CLAIMED:
            journal.mark(rid, NOT_SUBMITTED, {"error": "failed before anything was sent"})
            journal_state = NOT_SUBMITTED
        else:
            journal_state = entry.state if entry is not None else "missing"
        return ActionResult(
            NEEDS_HUMAN,
            rid,
            {
                **detail,
                "step": "prepare",
                "journal_state": journal_state,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    reply = ctx.bridge.start_claude(
        reg.port,
        token,
        {
            "request_id": rid,
            "session_id": session_id,
            "cwd": args.repo,
            "label": args.label,
            "prompt": args.prompt,
        },
    )
    bridge = _bridge_summary(reply.detail)
    if reply.status == bridge_client.STARTED:
        done = {
            **detail,
            "terminal_pid": bridge.get("terminal_pid"),
            "journal_state": FULFILLED,
            "bridge_status": reply.status,
        }
        journal.mark(rid, FULFILLED, {**done, "args_sha256": digest})
        return ActionResult(OK, rid, done)
    reason = bridge.get("reason")
    nothing_sent = reply.status == bridge_client.UNREACHABLE or (
        reply.status == bridge_client.REFUSED and reason in NOTHING_SENT_REASONS
    )
    if nothing_sent:
        status = SERVICE_OFF if reason == "kill_switch" else NEEDS_HUMAN
        return stop(status, {"step": "bridge", "bridge": bridge, "bridge_status": reply.status})
    # No confirmation, or a refusal the bridge may have acted on (duplicate_session: a session
    # with this id already exists): the request may have run, so it must never be sent again
    # (rule C6).
    journal.mark(rid, UNKNOWN, {**detail, "bridge": bridge, "bridge_status": reply.status})
    error = (
        "a session with this id already exists, so this request may already have run; "
        "check read-back"
        if reason == "duplicate_session"
        else "the window bridge did not confirm; the session may have started, "
        "so check read-back before retrying"
    )
    return ActionResult(
        NEEDS_HUMAN,
        rid,
        {
            **detail,
            "step": "bridge",
            "bridge": bridge,
            "bridge_status": reply.status,
            "journal_state": UNKNOWN,
            "error": error,
        },
    )


ACTIONS: dict[str, Callable[[ActionRequest, Context], ActionResult]] = {
    "health": health,
    "probe_input": probe_input,
    "start_claude": start_claude,
}

READ_ONLY_ACTIONS = frozenset({"health"})  # never listed in flight, so hands off can poll health


def dispatch(req: ActionRequest, ctx: Context) -> ActionResult:
    handler = ACTIONS.get(req.action)
    if handler is None:
        return ActionResult(UNKNOWN_ACTION, req.request_id, {"known": sorted(ACTIONS)})
    try:
        if req.action in READ_ONLY_ACTIONS:
            return handler(req, ctx)
        with ctx.inflight.track(req.action):
            return handler(req, ctx)
    except Exception as exc:  # rule C4: stop and report, never a bare 500
        return ActionResult(
            NEEDS_HUMAN,
            req.request_id,
            {"step": req.action, "error": f"{type(exc).__name__}: {exc}"},
        )
