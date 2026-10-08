import threading
import urllib.error
import urllib.request

import pytest
from fakes import FakeBridge

from remote_hands.actions import Context
from remote_hands.desktop import FakeDesktop
from remote_hands.server import make_server

# 43 URL-safe characters like a real token, and deliberately lopsided: mixed case, digits, '-'
# and '_', not a palindrome, not sorted, no repeating unit. A uniform token ("t" * 40) reads the
# same reversed, sorted, lower-cased, or stripped, so a check that mangled the presented token
# would still pass every test. Made up for tests, not a credential (hence gitleaks:allow).
TOKEN = "uHSEiyLq_2OgI1mwLGyzugcJdL4Foz-12gmQopYLCjk"  # gitleaks:allow

# The allowed roots every test runs with, unless it sets its own: the made-up folders the tests use.
ALLOWED_ROOTS = "/home/user/src/:/home/user/.worktrees/"


@pytest.fixture(autouse=True)
def allowed_roots(monkeypatch):
    monkeypatch.setenv("REMOTE_HANDS_ALLOWED_ROOTS", ALLOWED_ROOTS)
    return ALLOWED_ROOTS


# serve_forever polls for shutdown every 0.5 s by default, and shutdown() waits for the next
# poll, so every test server would cost half a second at teardown.
SERVER_POLL_SECONDS = 0.05


def serve_in_background(server):
    """Run a test server on a daemon thread that notices shutdown() quickly."""
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": SERVER_POLL_SECONDS}, daemon=True
    )
    thread.start()
    return thread


@pytest.fixture
def hands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("REMOTE_HANDS_HOME", str(tmp_path))
    (tmp_path / "token").write_text(TOKEN, encoding="utf-8")
    return tmp_path


@pytest.fixture
def service(hands_home):
    desktop = FakeDesktop()
    bridge = FakeBridge()
    ctx = Context(
        desktop=desktop,
        kill_switch=hands_home / "OFF",
        sleep=lambda _s: None,
        state_dir=hands_home,
        token=TOKEN,
        bridge=bridge,
    )
    server = make_server(port=0, token=TOKEN, ctx=ctx, audit_file=hands_home / "audit.jsonl")
    serve_in_background(server)
    host, port = server.server_address[:2]
    try:
        yield {
            "server": server,
            "url": f"http://{host}:{port}/action",
            "desktop": desktop,
            "ctx": ctx,
            "bridge": bridge,
            "audit": hands_home / "audit.jsonl",
        }
    finally:
        server.shutdown()
        server.server_close()


def post(url, body, token=TOKEN, method="POST"):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
