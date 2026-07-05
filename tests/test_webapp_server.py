import urllib.error
import urllib.request

import pytest

from bot.webapp_server import start_webapp_server


@pytest.fixture
def server():
    srv = start_webapp_server("s3cr3t", 0)
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(server, token=None):
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    if token is not None:
        url += f"?token={token}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
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
