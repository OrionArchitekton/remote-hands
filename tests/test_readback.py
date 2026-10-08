import json

import pytest

from remote_hands import readback
from remote_hands.readback import (
    MAX_REPLY_CHARS,
    NOT_FOUND,
    REPLIED,
    STARTED,
    UNREADABLE,
    find_session_file,
    read_record,
    wait_for_reply,
)

SID = "abcdef01-2345-5678-9abc-def012345678"


def user(text, **extra):
    message = {"role": "user", "content": [{"type": "text", "text": text}]}
    return {"type": "user", "message": message, "cwd": "/home/user/src/repo-a", **extra}


def assistant(*blocks, **extra):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}, **extra}


def text(value):
    return {"type": "text", "text": value}


def tool_use():
    return {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}


def tool_result():
    content = [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
    return {"type": "user", "message": {"role": "user", "content": content}}


def write_session(projects, records, folder="-home-user-src-repo-a", tail=""):
    directory = projects / folder
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{SID}.jsonl"
    lines = "".join(json.dumps(record) + "\n" for record in records)
    path.write_text(lines + tail, encoding="utf-8")
    return path


def fake_clock():
    """Sleep calls, a sleep that advances the clock, and the clock itself."""
    clock = {"now": 0.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    return sleeps, sleep, lambda: clock["now"]


def test_first_text_reply_after_the_prompt(tmp_path):
    path = write_session(
        tmp_path,
        [
            {"type": "queue-operation", "operation": "enqueue", "sessionId": SID},
            user("fix it"),
            assistant(tool_use()),
            tool_result(),
            assistant(text("I fixed the share image.")),
            assistant(text("later text")),
        ],
    )
    got = read_record(path, "fix it")
    assert got["state"] == REPLIED
    assert got["prompt_found"] is True
    assert got["first_reply"] == "I fixed the share image."
    assert got["cwd"] == "/home/user/src/repo-a"
    assert got["truncated"] is False


def test_prompt_without_a_reply_yet_is_started(tmp_path):
    path = write_session(tmp_path, [user("fix it")], tail='{"type": "assist')
    got = read_record(path, "fix it")
    assert got["state"] == STARTED
    assert got["prompt_found"] is True
    assert got["first_reply"] is None


def test_meta_and_sidechain_records_are_skipped(tmp_path):
    path = write_session(
        tmp_path,
        [
            user("hook context", isMeta=True),
            user("fix it"),
            assistant(text("side"), isSidechain=True),
            assistant(text("main reply")),
        ],
    )
    assert read_record(path, "fix it")["first_reply"] == "main reply"


def test_a_different_prompt_is_not_a_match(tmp_path):
    path = write_session(tmp_path, [user("something else"), assistant(text("reply"))])
    got = read_record(path, "fix it")
    assert got["prompt_found"] is False
    assert got["has_user_record"] is True
    assert got["state"] == STARTED


def test_without_an_expected_prompt_the_first_prompt_counts(tmp_path):
    path = write_session(tmp_path, [user("anything"), assistant(text("reply"))])
    assert read_record(path)["first_reply"] == "reply"


def test_long_reply_is_truncated(tmp_path):
    path = write_session(tmp_path, [user("fix it"), assistant(text("x" * (MAX_REPLY_CHARS + 10)))])
    got = read_record(path, "fix it")
    assert len(got["first_reply"]) == MAX_REPLY_CHARS
    assert got["truncated"] is True


def test_find_session_file_searches_every_project_folder(tmp_path):
    path = write_session(tmp_path, [user("x")], folder="-home-user-src-other")
    assert find_session_file(tmp_path, SID) == path
    assert find_session_file(tmp_path / "missing", SID) is None


@pytest.mark.parametrize(
    "bad",
    [
        "*",
        "../x",
        SID.upper(),
        "not-a-uuid",
        "",
        None,
        f"{SID}/../x",
        f"{SID}*",
        f"{SID}\n",
    ],
)
def test_session_id_must_be_a_lowercase_uuid(tmp_path, bad):
    with pytest.raises(ValueError):
        find_session_file(tmp_path, bad)


def test_wait_polls_until_the_reply_lands(tmp_path):
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds
        if clock["now"] >= 4:
            write_session(tmp_path, [user("fix it"), assistant(text("done"))])

    got = wait_for_reply(
        tmp_path,
        SID,
        wait_seconds=60,
        expected_prompt="fix it",
        sleep=sleep,
        monotonic=lambda: clock["now"],
    )
    assert got["state"] == REPLIED
    assert got["session_id"] == SID
    assert clock["now"] == 4


def test_wait_gives_up_with_not_found(tmp_path):
    clock = {"now": 0.0}

    def sleep(seconds):
        clock["now"] += seconds

    got = wait_for_reply(tmp_path, SID, wait_seconds=6, sleep=sleep, monotonic=lambda: clock["now"])
    assert got == {"state": NOT_FOUND, "session_id": SID}


def test_an_id_cannot_walk_out_of_the_projects_folder(tmp_path):
    # Claude Code keeps a <session id>/ folder beside each transcript, so "<id>/../.." resolves.
    projects = tmp_path / "projects"
    (projects / "-home-user-src-repo-a" / SID).mkdir(parents=True)
    (tmp_path / "outside.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        find_session_file(projects, f"{SID}/../../../outside")


@pytest.mark.parametrize("flag", ["isMeta", "isSidechain"])
def test_flagged_records_are_neither_the_prompt_nor_the_reply(tmp_path, flag):
    path = write_session(
        tmp_path,
        [
            user("hook context", cwd="/elsewhere", **{flag: True}),
            user("fix it"),
            assistant(text("flagged reply"), **{flag: True}),
            assistant(text("main reply")),
        ],
    )
    got = read_record(path)
    assert got["first_reply"] == "main reply"
    assert got["cwd"] == "/home/user/src/repo-a"


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        ("line one\nline two", "line one\r\nline two\r\n"),
        ("fix it", "fix it\n"),
        ("  fix it  ", "fix it"),
    ],
)
def test_prompt_match_ignores_line_endings_and_edge_whitespace(tmp_path, recorded, expected):
    path = write_session(tmp_path, [user(recorded), assistant(text("ok"))])
    got = read_record(path, expected)
    assert got["prompt_found"] is True
    assert got["first_reply"] == "ok"


def test_prompt_match_keeps_inner_whitespace_significant(tmp_path):
    path = write_session(tmp_path, [user("line one\nline two"), assistant(text("ok"))])
    assert read_record(path, "line one line two")["prompt_found"] is False


def test_follow_up_user_text_is_not_a_reply(tmp_path):
    records = [user("fix it"), user("[Request interrupted by user]"), assistant(text("ok"))]
    path = write_session(tmp_path, records)
    assert read_record(path, "fix it")["first_reply"] == "ok"


def test_reply_is_stripped(tmp_path):
    path = write_session(tmp_path, [user("fix it"), assistant(text("\n\nok\n"))])
    assert read_record(path, "fix it")["first_reply"] == "ok"


@pytest.mark.parametrize(
    ("length", "truncated"),
    [(MAX_REPLY_CHARS - 1, False), (MAX_REPLY_CHARS, False), (MAX_REPLY_CHARS + 1, True)],
)
def test_truncation_starts_one_past_the_limit(tmp_path, length, truncated):
    path = write_session(tmp_path, [user("fix it"), assistant(text("x" * length))])
    got = read_record(path, "fix it")
    assert len(got["first_reply"]) == min(length, MAX_REPLY_CHARS)
    assert got["truncated"] is truncated


@pytest.mark.parametrize(
    ("marker", "expected"),
    [({"isApiErrorMessage": True}, True), ({"isApiErrorMessage": False}, False), ({}, False)],
)
def test_reply_is_error_follows_the_api_error_marker(tmp_path, marker, expected):
    path = write_session(tmp_path, [user("fix it"), assistant(text("API Error: 529"), **marker)])
    got = read_record(path, "fix it")
    assert got["state"] == REPLIED
    assert got["reply_is_error"] is expected


def test_only_the_first_reply_decides_reply_is_error(tmp_path):
    error = assistant(text("API Error: 529"), isApiErrorMessage=True)
    path = write_session(tmp_path, [user("fix it"), assistant(text("fine")), error])
    got = read_record(path, "fix it")
    assert got["first_reply"] == "fine"
    assert got["reply_is_error"] is False


def test_reply_is_error_is_false_while_there_is_no_reply(tmp_path):
    path = write_session(tmp_path, [user("fix it")])
    assert read_record(path, "fix it")["reply_is_error"] is False


def test_a_deeply_nested_line_is_skipped(tmp_path):
    deep = "[" * 100_000 + "]" * 100_000
    reply = json.dumps(assistant(text("done")))
    path = write_session(tmp_path, [user("fix it")], tail=f"{deep}\n{reply}\n")
    got = read_record(path, "fix it")
    assert got["state"] == REPLIED
    assert got["first_reply"] == "done"


def test_a_split_utf8_tail_is_tolerated(tmp_path):
    path = write_session(tmp_path, [user("fix it")])
    with open(path, "ab") as fh:
        # The writer was cut off in the middle of a two-byte character.
        fh.write(b'{"type": "assistant", "message": {"content": "caf' + bytes([0xC3]))
    got = read_record(path, "fix it")
    assert got["state"] == STARTED
    assert got["prompt_found"] is True


def test_wait_stops_at_the_deadline_after_polling_every_two_seconds(tmp_path):
    sleeps, sleep, monotonic = fake_clock()
    wait_for_reply(tmp_path, SID, wait_seconds=6, sleep=sleep, monotonic=monotonic)
    assert sleeps == [2.0, 2.0, 2.0]


def test_wait_of_zero_reads_once_and_never_sleeps(tmp_path):
    write_session(tmp_path, [user("fix it")])
    sleeps, sleep, monotonic = fake_clock()
    got = wait_for_reply(
        tmp_path, SID, wait_seconds=0, expected_prompt="fix it", sleep=sleep, monotonic=monotonic
    )
    assert got["state"] == STARTED
    assert got["prompt_found"] is True
    assert sleeps == []


@pytest.mark.parametrize("denied_call", ["open", "find_session_file"])
def test_wait_ends_as_unreadable_when_reading_is_denied(tmp_path, monkeypatch, denied_call):
    write_session(tmp_path, [user("fix it"), assistant(text("done"))])

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(readback, denied_call, denied, raising=False)
    sleeps, sleep, monotonic = fake_clock()
    got = wait_for_reply(tmp_path, SID, wait_seconds=6, sleep=sleep, monotonic=monotonic)
    assert got == {
        "state": UNREADABLE,
        "error": "PermissionError: [Errno 13] Permission denied",
        "session_id": SID,
    }
    assert sleeps == [2.0, 2.0, 2.0]


def test_a_read_error_does_not_end_the_wait_while_time_remains(tmp_path, monkeypatch):
    write_session(tmp_path, [user("fix it"), assistant(text("done"))])
    real_open = open
    opened = []

    def flaky_open(*args, **kwargs):
        opened.append(args)
        if len(opened) == 1:
            raise PermissionError(13, "Permission denied")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(readback, "open", flaky_open, raising=False)
    sleeps, sleep, monotonic = fake_clock()
    got = wait_for_reply(
        tmp_path, SID, wait_seconds=60, expected_prompt="fix it", sleep=sleep, monotonic=monotonic
    )
    assert got["state"] == REPLIED
    assert sleeps == [2.0]
