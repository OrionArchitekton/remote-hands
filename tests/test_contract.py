import json

import pytest

from remote_hands.contract import (
    NEEDS_HUMAN,
    OK,
    SERVICE_DOWN,
    STATUSES,
    ActionRequest,
    ActionResult,
)


def test_request_round_trip():
    req = ActionRequest(action="health", request_id="r1", args={"a": 1})
    assert ActionRequest.from_json(req.to_json()) == req


def test_request_defaults_args_to_empty_object():
    req = ActionRequest.from_json(b'{"action": "health", "request_id": "r1"}')
    assert req.args == {}


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"request_id": "r1"}',
        b'{"action": "", "request_id": "r1"}',
        b'{"action": "health"}',
        b'{"action": "health", "request_id": 7}',
        b'{"action": "health", "request_id": "r1", "args": []}',
    ],
)
def test_request_rejects_malformed(raw):
    with pytest.raises(ValueError):
        ActionRequest.from_json(raw)


def test_result_round_trip():
    res = ActionResult(status=NEEDS_HUMAN, request_id="r1", detail={"step": "focus"})
    assert ActionResult.from_json(res.to_json()) == res


def test_result_rejects_unknown_status():
    with pytest.raises(ValueError):
        ActionResult(status="fine", request_id="r1")


def test_client_side_status_is_a_known_status():
    assert SERVICE_DOWN in STATUSES
    assert OK in STATUSES


def test_result_json_is_an_object_with_status_first_class():
    body = json.loads(ActionResult(status=OK, request_id="r1").to_json())
    assert body == {"status": "ok", "request_id": "r1", "detail": {}}


def test_deeply_nested_json_is_a_value_error():
    with pytest.raises(ValueError):
        ActionRequest.from_json(b"[" * 60000)


def test_staged_not_sent_is_a_known_status():
    from remote_hands.contract import STAGED_NOT_SENT

    assert STAGED_NOT_SENT in STATUSES


def _request_body(**fields):
    return json.dumps({"action": "health", "request_id": "r1", **fields}).encode()


@pytest.mark.parametrize(("key", "longest"), [("action", 64), ("request_id", 128)])
def test_action_and_request_id_are_capped_because_the_server_audits_them(key, longest):
    # The limits are pinned as numbers on purpose: a looser constant must fail here.
    accepted = ActionRequest.from_json(_request_body(**{key: "a" * longest}))
    assert len(getattr(accepted, key)) == longest
    message = f"{key} must be a non-empty string of at most {longest} characters"
    with pytest.raises(ValueError, match=message):
        ActionRequest.from_json(_request_body(**{key: "a" * (longest + 1)}))


def test_a_result_keeps_no_cap_on_its_fields():
    raw = json.dumps({"status": "ok", "request_id": "r" * 500}).encode()
    assert len(ActionResult.from_json(raw).request_id) == 500
