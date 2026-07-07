import urllib.error
import urllib.request

import pytest

from bot.webapp_server import start_webapp_server


@pytest.fixture
def payload():
    return {"recurring": [{"weekday": 0, "start_time": "10:00", "duration_min": 60, "capacity": 1}]}


@pytest.fixture
def trainees():
    return [
        {
            "user_id": 111,
            "full_name": "Alice <script>",
            "phone": "0501234567",
            "status": "approved",
            "requested_at": "2026-07-01 10:00:00",
        }
    ]


@pytest.fixture
def history():
    return [{"created_at": "2026-07-02 09:00:00", "action": "book", "details": "Monday 10:00"}]


@pytest.fixture
def server(payload, trainees, history):
    def get_trainee_history(user_id_str):
        if user_id_str == "111":
            return trainees[0], history
        return None, []

    def get_trainee_sessions(user_id_str):
        if user_id_str == "111":
            return trainees[0], [
                {
                    "date": "06/07/2026", "weekday": "יום שני", "start_time": "10:00",
                    "duration_min": 60, "package": "חבילה #3", "status": "התקיים",
                    "booked_at": "2026-07-01 09:00:00",
                }
            ]
        return None, []

    srv = start_webapp_server(
        "s3cr3t", 0, lambda: payload, lambda: trainees, get_trainee_history,
        get_trainee_sessions,
    )
    yield srv
    srv.shutdown()
    srv.server_close()


def _get(server, token=None, method="GET", extra=""):
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    params = []
    if token is not None:
        params.append(f"token={token}")
    if extra:
        params.append(extra)
    if params:
        url += "?" + "&".join(params)
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


def test_view_users_requires_token(server):
    status, _ = _get(server, extra="view=users")
    assert status == 403


def test_view_users_lists_trainees_and_escapes_html(server):
    status, body = _get(server, token="s3cr3t", extra="view=users")
    assert status == 200
    assert b"0501234567" in body
    # the malicious name must be escaped, not rendered as a literal <script> tag
    assert b"<script>" not in body
    assert b"&lt;script&gt;" in body


def test_view_history_for_known_user(server):
    status, body = _get(server, token="s3cr3t", extra="view=history&user_id=111")
    assert status == 200
    assert b"0501234567" in body
    assert b"Monday 10:00" in body


def test_view_history_for_unknown_user(server):
    status, body = _get(server, token="s3cr3t", extra="view=history&user_id=999")
    assert status == 200
    assert "לא נמצא".encode("utf-8") in body


def test_sessions_view_lists_only_sessions_with_package(server):
    status, body = _get(server, token="s3cr3t", extra="view=sessions&user_id=111")
    text = body.decode("utf-8")
    assert status == 200
    assert "חבילה #3" in text and "התקיים" in text and "10:00" in text
    # audit-style actions do not belong in this view
    assert "Monday 10:00" not in text


def test_sessions_view_unknown_user(server):
    status, body = _get(server, token="s3cr3t", extra="view=sessions&user_id=999")
    assert status == 200 and "לא נמצא מתאמן".encode() in body


def test_users_view_links_to_sessions(server):
    status, body = _get(server, token="s3cr3t", extra="view=users")
    assert status == 200 and b"view=sessions" in body
