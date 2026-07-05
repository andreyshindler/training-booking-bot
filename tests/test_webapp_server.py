import urllib.error
import urllib.request

import pytest

from bot.webapp_server import start_webapp_server


@pytest.fixture
def payload():
    return {"recurring": [{"weekday": 0, "start_time": "10:00", "duration_min": 60, "capacity": 1}]}


@pytest.fixture
def server(payload):
    srv = start_webapp_server("s3cr3t", 0, lambda: payload)
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(server, token=None, method="GET"):
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    if token is not None:
        url += f"?token={token}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_missing_token_rejected(server):
    status, _ = _get(server)
    assert status == 403


def test_wrong_token_rejected(server):
    status, _ = _get(server, token="nope")
    assert status == 403


def test_correct_token_serves_mini_app(server):
    status, body = _get(server, token="s3cr3t")
    assert status == 200
    assert "עריכת המערכת".encode("utf-8") in body


def test_correct_token_injects_current_payload(server, payload):
    status, body = _get(server, token="s3cr3t")
    assert status == 200
    assert b"window.__MINI_APP_DATA__" in body
    assert b'"start_time": "10:00"' in body


def test_head_request_supported(server):
    # curl -I sends HEAD; must not 501 the way an unhandled method would.
    status, body = _get(server, token="s3cr3t", method="HEAD")
    assert status == 200
    assert body == b""

    status, body = _get(server, method="HEAD")
    assert status == 403
