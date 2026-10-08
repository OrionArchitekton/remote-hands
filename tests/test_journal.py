import json
import os
import time

import pytest

from remote_hands.journal import (
    CLAIMED,
    FULFILLED,
    NOT_SUBMITTED,
    SUBMITTING,
    UNKNOWN,
    Journal,
    check_request_id,
)

RID = "req-0001-abcd"


def test_first_claim_owns_the_request(tmp_path):
    journal = Journal(tmp_path / "journal")
    assert journal.claim(RID) is None
    assert journal.read(RID).state == CLAIMED


def test_second_claim_sees_the_first(tmp_path):
    journal = Journal(tmp_path)
    journal.claim(RID)
    prior = journal.claim(RID)
    assert prior is not None
    assert prior.state == CLAIMED


def test_fulfilled_request_is_never_claimed_again(tmp_path):
    journal = Journal(tmp_path)
    journal.claim(RID)
    journal.mark(RID, SUBMITTING)
    journal.mark(RID, FULFILLED, {"session_id": "s"})
    prior = journal.claim(RID)
    assert prior.state == FULFILLED
    assert prior.detail == {"session_id": "s"}


@pytest.mark.parametrize("state", [SUBMITTING, UNKNOWN])
def test_possibly_sent_request_is_never_claimed_again(tmp_path, state):
    journal = Journal(tmp_path)
    journal.claim(RID)
    journal.mark(RID, SUBMITTING)
    if state == UNKNOWN:
        journal.mark(RID, UNKNOWN)
    assert journal.claim(RID).state == state


# The legal marks that take a freshly claimed request to each state.
PATH_FROM_CLAIMED = {
    CLAIMED: [],
    SUBMITTING: [SUBMITTING],
    FULFILLED: [SUBMITTING, FULFILLED],
    UNKNOWN: [SUBMITTING, UNKNOWN],
}


@pytest.mark.parametrize("state", [CLAIMED, SUBMITTING, FULFILLED, UNKNOWN])
def test_blocked_claim_changes_nothing_and_stays_blocked(tmp_path, state):
    journal = Journal(tmp_path)
    assert journal.claim(RID) is None
    for step in PATH_FROM_CLAIMED[state]:
        journal.mark(RID, step, {"reached": step})
    before = journal.read(RID)
    assert before.state == state
    assert journal.claim(RID) == before
    assert journal.read(RID) == before
    assert journal.claim(RID) == before


def test_not_submitted_request_can_be_claimed_again(tmp_path):
    journal = Journal(tmp_path)
    journal.claim(RID)
    journal.mark(RID, NOT_SUBMITTED, {"reason": "user_active"})
    assert journal.claim(RID) is None
    assert journal.read(RID).state == CLAIMED


def test_written_at_is_the_entry_files_modification_time(tmp_path):
    journal = Journal(tmp_path)
    assert journal.written_at(RID) is None
    journal.claim(RID)
    os.utime(tmp_path / f"{RID}.json", (1_000.0, 1_000.0))
    assert journal.written_at(RID) == 1_000.0
    journal.mark(RID, SUBMITTING)  # every write moves it
    assert time.time() - journal.written_at(RID) < 60


def test_unreadable_entry_reads_as_unknown(tmp_path):
    (tmp_path / f"{RID}.json").write_text("{torn", encoding="utf-8")
    assert Journal(tmp_path).claim(RID).state == UNKNOWN


def test_empty_entry_left_by_a_crash_reads_as_unknown(tmp_path):
    (tmp_path / f"{RID}.json").write_text("", encoding="utf-8")
    assert Journal(tmp_path).claim(RID).state == UNKNOWN


def test_malformed_entry_reads_as_unknown(tmp_path):
    payload = json.dumps({"state": "weird", "detail": {}})
    (tmp_path / f"{RID}.json").write_text(payload, encoding="utf-8")
    assert Journal(tmp_path).read(RID).state == UNKNOWN


@pytest.mark.parametrize("text", ["{torn", ""], ids=["torn", "empty"])
def test_torn_and_empty_entries_read_as_unknown(tmp_path, text):
    (tmp_path / f"{RID}.json").write_text(text, encoding="utf-8")
    assert Journal(tmp_path).read(RID).state == UNKNOWN


@pytest.mark.parametrize(
    "payload",
    [
        {"request_id": RID, "state": ["claimed"], "detail": {}},
        {"request_id": RID, "state": {"a": 1}, "detail": {}},
        {"request_id": RID, "state": "fulfilled", "detail": []},
        {"request_id": "other-0001", "state": "fulfilled", "detail": {}},
        {"state": "fulfilled", "detail": {}},
        {"request_id": RID, "state": "weird", "detail": {}},
        ["claimed"],
    ],
    ids=[
        "list-state",
        "dict-state",
        "list-detail",
        "foreign-request-id",
        "no-request-id",
        "unlisted-state",
        "not-an-object",
    ],
)
def test_malformed_or_foreign_entries_read_as_unknown(tmp_path, payload):
    (tmp_path / f"{RID}.json").write_text(json.dumps(payload), encoding="utf-8")
    assert Journal(tmp_path).read(RID).state == UNKNOWN


@pytest.mark.parametrize(
    "bad",
    ["short", "a" * 7, "../../etc/x", "a" * 65, "has space1", "ok/slash123", "Upper-case-01"],
)
def test_request_id_must_be_a_safe_file_name(tmp_path, bad):
    with pytest.raises(ValueError):
        check_request_id(bad)
    with pytest.raises(ValueError):
        Journal(tmp_path).claim(bad)


def test_safe_request_ids_are_accepted():
    for good in ("a" * 8, "a" * 64, "example-l2-0001", "snake_case_id_9"):
        assert check_request_id(good) == good


def test_unknown_state_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown journal state"):
        Journal(tmp_path).mark(RID, "done")


def test_mark_follows_the_state_machine(tmp_path):
    journal = Journal(tmp_path)
    with pytest.raises(ValueError, match="cannot move"):
        journal.mark(RID, SUBMITTING)  # never claimed
    assert journal.read(RID) is None
    journal.claim(RID)
    with pytest.raises(ValueError, match="cannot move"):
        journal.mark(RID, FULFILLED)  # a claimed request must pass through submitting
    assert journal.read(RID).state == CLAIMED
    journal.mark(RID, SUBMITTING)
    journal.mark(RID, FULFILLED, {"session_id": "s"})
    for state in (NOT_SUBMITTED, SUBMITTING, UNKNOWN, CLAIMED):
        with pytest.raises(ValueError, match="cannot move"):
            journal.mark(RID, state, {"overwritten": True})
    final = journal.read(RID)
    assert (final.state, final.detail) == (FULFILLED, {"session_id": "s"})


def test_unknown_is_final(tmp_path):
    journal = Journal(tmp_path)
    journal.claim(RID)
    journal.mark(RID, SUBMITTING)
    journal.mark(RID, UNKNOWN)
    for state in (NOT_SUBMITTED, FULFILLED):
        with pytest.raises(ValueError, match="cannot move"):
            journal.mark(RID, state)
    assert journal.read(RID).state == UNKNOWN


def test_undelivered_submit_may_return_to_not_submitted(tmp_path):
    journal = Journal(tmp_path)
    journal.claim(RID)
    journal.mark(RID, SUBMITTING)
    journal.mark(RID, NOT_SUBMITTED, {"reason": "refused"})
    assert journal.claim(RID) is None
    assert journal.read(RID).state == CLAIMED


def test_not_submitted_is_left_only_through_claim(tmp_path):
    journal = Journal(tmp_path)
    journal.claim(RID)
    journal.mark(RID, NOT_SUBMITTED)
    for state in (CLAIMED, SUBMITTING, FULFILLED, UNKNOWN):
        with pytest.raises(ValueError, match="cannot move"):
            journal.mark(RID, state)
    assert journal.read(RID).state == NOT_SUBMITTED
    assert journal.claim(RID) is None


def test_vanished_entry_reads_as_unknown(tmp_path, monkeypatch):
    def already_there(*_args, **_kwargs):
        raise FileExistsError

    monkeypatch.setattr("remote_hands.journal.os.open", already_there)
    assert Journal(tmp_path).claim(RID).state == UNKNOWN
    assert not (tmp_path / f"{RID}.json").exists()


def test_entries_are_flushed_to_disk_before_they_count(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append("fsync")
        real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr("remote_hands.journal.os.fsync", fsync)
    monkeypatch.setattr("remote_hands.journal.os.replace", replace)
    journal = Journal(tmp_path)
    journal.claim(RID)
    assert events == ["fsync"]  # the first write is on disk before claim returns
    journal.mark(RID, SUBMITTING)
    assert events == ["fsync", "fsync", "replace"]  # temp file flushed, then swapped in


def test_failed_replace_leaves_no_temp_file_and_keeps_the_entry(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    journal.claim(RID)
    attempts = []

    def locked(*_args, **_kwargs):
        attempts.append(1)
        raise PermissionError("another handle has the entry open")

    monkeypatch.setattr("remote_hands.journal.os.replace", locked)
    monkeypatch.setattr("remote_hands.journal.time.sleep", lambda _seconds: None)
    with pytest.raises(PermissionError):
        journal.mark(RID, SUBMITTING)
    assert len(attempts) == 3
    assert journal.read(RID).state == CLAIMED
    assert list(tmp_path.glob("*.tmp")) == []


def test_replace_retries_a_briefly_locked_entry(tmp_path, monkeypatch):
    journal = Journal(tmp_path)
    journal.claim(RID)
    real_replace = os.replace
    calls = []
    pauses = []

    def locked_once(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise PermissionError("another handle has the entry open")
        real_replace(src, dst)

    monkeypatch.setattr("remote_hands.journal.os.replace", locked_once)
    monkeypatch.setattr("remote_hands.journal.time.sleep", pauses.append)
    journal.mark(RID, SUBMITTING)
    assert len(calls) == 2
    assert len(pauses) == 1
    assert journal.read(RID).state == SUBMITTING
    assert list(tmp_path.glob("*.tmp")) == []
