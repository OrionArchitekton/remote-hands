"""The window bridge (JavaScript) and the service (Python) must agree on refusal reasons and on
the session id a request id names.

The service treats a bridge refusal as "nothing was sent, safe to retry" only for the reasons in
actions.NOTHING_SENT_REASONS. Any other refusal marks the request unknown, which blocks its id for
good. A reason added on one side only would flip that decision without any test failing, so this
reads the bridge's source and compares the two lists.

The bridge derives the session id from the request id itself and refuses any other, so one request
id can never start two sessions (C6). Its uuid5 must give what the service's gives: both sides pin
the same vectors, and this file checks that the two lists are identical, so a change on one side
alone fails a test even if that side's own test was updated to match.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from remote_hands.actions import NOTHING_SENT_REASONS
from remote_hands.start_request import (
    ALLOWED_ROOTS_ENV,
    ALLOWED_ROOTS_KEY,
    DEFAULT_ROOT_DIRS,
    parse_roots,
    session_id_for,
)

SERVER_JS = Path(__file__).resolve().parents[1] / "bridge" / "src" / "server.js"
CORE_JS = Path(__file__).resolve().parents[1] / "bridge" / "src" / "core.js"
CORE_TEST_JS = Path(__file__).resolve().parents[1] / "bridge" / "test" / "core.test.js"
# uuid.uuid5(SESSION_NAMESPACE, request_id), pinned here and in bridge/test/core.test.js.
SESSION_ID_VECTORS = {
    "req-0001-abcd": "fb0acc9b-305a-594d-a7e5-b4fb434c156a",
    "example-l2-0001": "31b86acf-0c30-59b2-b0fe-8c96ca4fe19c",
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6": "5deeb099-35c0-5e55-a698-655507d4746d",
    "abcdef123456": "b436c3a0-a8d6-52fa-84a1-cf8ce91db92a",
}
JS_VECTOR = re.compile(r"\['([a-z0-9_-]+)', '([0-9a-f-]{36})'\]")
JS_ROOT_DIRS = re.compile(r"^const DEFAULT_ROOT_DIRS = Object\.freeze\(\[([^\]]*)\]", re.MULTILINE)
JS_ROOTS_ENV = re.compile(r"^const ALLOWED_ROOTS_ENV = '([A-Z_]+)';", re.MULTILINE)
JS_ROOTS_KEY = re.compile(r"^const ALLOWED_ROOTS_KEY = '([a-z_]+)';", re.MULTILINE)
# Settings both sides must read alike: accepted ones and refused ones.
ROOT_VECTORS = [
    ["/home/a/src"],
    ["/home/a/src/", "/home/a/.worktrees//"],
    ["/srv/repos", "/home/a b/c"],
    ["/"],
    ["relative"],
    ["/a//b"],
    ["/a/./b"],
    ["/a/../b"],
    ["/a\tb"],
    ["/a\u200bb"],
    [""],
    [],
    [1],
]
JS_STRING = re.compile(r"'([^']*)'")
# Every call of the bridge's refusal helper, and the calls that give a literal status code and
# reason. A reason this test cannot read would escape the comparison, so the two counts must agree.
REFUSE_CALL = re.compile(r"(?<!function )\brefuse\(")
REFUSE_LITERAL = re.compile(r"\brefuse\(\s*res,\s*\d{3},\s*'([a-z_]+)'")


def bridge_refusal_reasons() -> set[str]:
    source = SERVER_JS.read_text(encoding="utf-8")
    calls = REFUSE_CALL.findall(source)
    reasons = REFUSE_LITERAL.findall(source)
    assert len(calls) == len(reasons), (
        f"{len(calls)} refusals in {SERVER_JS.name} but {len(reasons)} with a literal status and "
        "reason: give every refusal a literal reason so this test can read it"
    )
    return set(reasons)


def test_the_bridge_refuses_only_for_reasons_the_service_knows():
    # Every refusal but a duplicate session happens before the bridge acts, as the service assumes.
    assert bridge_refusal_reasons() - {"duplicate_session"} == NOTHING_SENT_REASONS


def test_a_duplicate_session_is_a_refusal_the_service_never_retries():
    assert "duplicate_session" in bridge_refusal_reasons()
    assert "duplicate_session" not in NOTHING_SENT_REASONS


def test_the_service_derives_the_session_ids_the_bridge_pins():
    for request_id, expected in SESSION_ID_VECTORS.items():
        assert session_id_for(request_id) == expected, request_id


def test_the_bridge_tests_pin_the_same_session_ids_as_the_service_tests():
    source = CORE_TEST_JS.read_text(encoding="utf-8")
    start = source.index("const SESSION_ID_VECTORS")
    block = source[start : source.index("];", start)]
    pinned = dict(JS_VECTOR.findall(block))
    assert pinned, "could not read SESSION_ID_VECTORS as ['request id', 'session id'] pairs"
    assert pinned == SESSION_ID_VECTORS


def test_the_bridge_reads_the_same_root_settings_as_the_service():
    # The bridge refuses a cwd outside the allowed roots, and marks a repository trusted for Claude
    # only inside them; the service refuses a repo outside them. Both read the same variable and
    # config key, and fall back to the same folders.
    source = CORE_JS.read_text(encoding="utf-8")
    found = JS_ROOT_DIRS.findall(source)
    assert len(found) == 1, "could not read const DEFAULT_ROOT_DIRS = [...] in core.js exactly once"
    assert tuple(JS_STRING.findall(found[0])) == DEFAULT_ROOT_DIRS
    assert JS_ROOTS_ENV.findall(source) == [ALLOWED_ROOTS_ENV]
    assert JS_ROOTS_KEY.findall(source) == [ALLOWED_ROOTS_KEY]


def _python_roots(entries):
    try:
        return list(parse_roots(entries))
    except ValueError:
        return None


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_bridge_parses_root_settings_as_the_service_does():
    script = (
        "const core = require(process.argv[1]);"
        "const out = JSON.parse(process.argv[2]).map((entries) => {"
        "  try { return Array.from(core.parseRoots(entries)); } catch { return null; } });"
        "process.stdout.write(JSON.stringify(out));"
    )
    done = subprocess.run(  # noqa: S603
        ["node", "-e", script, str(CORE_JS), json.dumps(ROOT_VECTORS)],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(done.stdout) == [_python_roots(entries) for entries in ROOT_VECTORS]
