"""Tiny HTTP server exposing the schedule-editing mini app, gated by a shared secret token.

Used when self-hosting the mini app (e.g. behind your own nginx) instead of
GitHub Pages, so only requests carrying the right ``token`` query parameter
can view it. Runs in a background thread, fully decoupled from the bot's own
asyncio polling loop.

The current schedule is injected into the page server-side on every request
(via ``get_payload``), so the shareable URL only needs the token — no long
encoded schedule data to carry around in the link. The same token also gates
two admin-only reporting views (``?view=users`` and ``?view=history``) that
render plain server-side HTML — no client-side JS needed for those.
"""

import hmac
import json
import logging
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

logger = logging.getLogger(__name__)

_MINI_APP_PATH = Path(__file__).resolve().parent.parent / "docs" / "index.html"
_INJECT_AFTER = "<head>"

_PAGE_STYLE = (
    "body{font-family:-apple-system,Roboto,Arial,sans-serif;padding:16px;"
    "max-width:720px;margin:0 auto}"
    "table{border-collapse:collapse;width:100%;margin-top:12px}"
    "td,th{border:1px solid #ccc;padding:6px 10px;text-align:right;font-size:0.9rem}"
    "th{background:#f2f3f5}"
    "a{color:#2ea6ff}"
)


def _page(title: str, body: str) -> bytes:
    html = (
        f'<!doctype html><html dir="rtl" lang="he"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title><style>{_PAGE_STYLE}</style></head>"
        f"<body><h1>{escape(title)}</h1>{body}</body></html>"
    )
    return html.encode("utf-8")


def _render_users_view(trainees: list[dict], token: str) -> bytes:
    if not trainees:
        return _page("מתאמנים", "<p>אין מתאמנים רשומים.</p>")
    rows = "".join(
        "<tr>"
        f"<td>{escape(str(t['full_name']))}</td>"
        f"<td>{escape(str(t['phone']))}</td>"
        f"<td>{escape(str(t['status']))}</td>"
        f"<td>{escape(str(t['requested_at']))}</td>"
        f'<td><a href="?view=history&user_id={t["user_id"]}&token={quote(token)}">היסטוריה</a></td>'
        f'<td><a href="?view=sessions&user_id={t["user_id"]}&token={quote(token)}">יומן אימונים</a></td>'
        "</tr>"
        for t in trainees
    )
    body = (
        "<table><tr><th>שם</th><th>טלפון</th><th>סטטוס</th>"
        f"<th>נרשם</th><th></th><th></th></tr>{rows}</table>"
    )
    return _page("מתאמנים", body)


def _render_history_view(trainee: dict | None, history: list[dict], user_id: str) -> bytes:
    if trainee is None:
        return _page("היסטוריית מתאמן", f"<p>לא נמצא מתאמן עם המזהה {escape(user_id)}.</p>")
    profile = (
        f"<p><b>שם:</b> {escape(str(trainee['full_name']))}<br>"
        f"<b>טלפון:</b> {escape(str(trainee['phone']))}<br>"
        f"<b>סטטוס:</b> {escape(str(trainee['status']))}<br>"
        f"<b>מזהה:</b> {escape(str(trainee['user_id']))}</p>"
    )
    if not history:
        table = "<p>אין פעולות רשומות.</p>"
    else:
        rows = "".join(
            "<tr>"
            f"<td>{escape(str(h['created_at']))}</td>"
            f"<td>{escape(str(h['action']))}</td>"
            f"<td>{escape(str(h['details']))}</td>"
            "</tr>"
            for h in history
        )
        table = f"<table><tr><th>מתי</th><th>פעולה</th><th>פרטים</th></tr>{rows}</table>"
    return _page("היסטוריית מתאמן", profile + table)


def _render_sessions_view(trainee: dict | None, sessions: list[dict], user_id: str) -> bytes:
    """Only the trainee's sessions — upcoming registrations and held sessions —
    with the package each one consumed (unlike the full audit history)."""
    if trainee is None:
        return _page("יומן אימונים", f"<p>לא נמצא מתאמן עם המזהה {escape(user_id)}.</p>")
    profile = (
        f"<p><b>שם:</b> {escape(str(trainee['full_name']))}<br>"
        f"<b>מזהה:</b> {escape(str(trainee['user_id']))}</p>"
    )
    if not sessions:
        table = "<p>אין אימונים רשומים.</p>"
    else:
        rows = "".join(
            "<tr>"
            f"<td>{escape(str(s['weekday']))} {escape(str(s['date']))}</td>"
            f"<td>{escape(str(s['start_time']))}</td>"
            f"<td>{escape(str(s['duration_min']))} דק'</td>"
            f"<td>{escape(str(s['package']))}</td>"
            f"<td>{escape(str(s['status']))}</td>"
            f"<td>{escape(str(s['booked_at']))}</td>"
            "</tr>"
            for s in sessions
        )
        table = (
            "<table><tr><th>תאריך</th><th>שעה</th><th>משך</th>"
            f"<th>חבילה</th><th>סטטוס</th><th>נרשם ב־</th></tr>{rows}</table>"
        )
    return _page("יומן אימונים", profile + table)


def _make_handler(
    secret: str, template: str, get_payload, list_trainees, get_trainee_history,
    get_trainee_sessions,
):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # The query string carries the secret token; never log it
            # (default BaseHTTPRequestHandler logging includes the full
            # request line).
            logger.info("webapp_server: %s %s", self.command, getattr(self, "_status", "?"))

        def _authorized(self, token: str) -> bool:
            return hmac.compare_digest(token, secret)

        def _render(self, query: dict) -> bytes:
            view = query.get("view", [""])[0]
            if view == "users":
                return _render_users_view(list_trainees(), query.get("token", [""])[0])
            if view == "history":
                user_id_str = query.get("user_id", [""])[0]
                trainee, history = get_trainee_history(user_id_str)
                return _render_history_view(trainee, history, user_id_str)
            if view == "sessions":
                user_id_str = query.get("user_id", [""])[0]
                trainee, sessions = get_trainee_sessions(user_id_str)
                return _render_sessions_view(trainee, sessions, user_id_str)
            data_script = f"<script>window.__MINI_APP_DATA__ = {json.dumps(get_payload())};</script>"
            html = template.replace(_INJECT_AFTER, _INJECT_AFTER + data_script, 1)
            return html.encode("utf-8")

        def _respond(self, include_body: bool):
            query = parse_qs(urlparse(self.path).query)
            if not self._authorized(query.get("token", [""])[0]):
                self._status = 403
                self.send_response(403)
                self.end_headers()
                if include_body:
                    self.wfile.write(b"Forbidden")
                return
            body = self._render(query)
            self._status = 200
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

        def do_GET(self):
            self._respond(include_body=True)

        def do_HEAD(self):
            self._respond(include_body=False)

    return Handler


def start_webapp_server(
    secret: str, port: int, get_payload, list_trainees, get_trainee_history,
    get_trainee_sessions,
) -> ThreadingHTTPServer:
    """Start serving the mini app (and admin reporting views) on ``port`` in
    a background thread.

    All callables are invoked fresh on every authorized request (no args
    except where noted) so every view always reflects live data, not a
    snapshot baked into the URL:
    - ``get_payload()`` -> the schedule dict for the calendar mini app.
    - ``list_trainees()`` -> list of trainee dict rows, for ``?view=users``.
    - ``get_trainee_history(user_id_str)`` -> (trainee dict or None, list of
      audit-log dict rows), for ``?view=history&user_id=...``.
    - ``get_trainee_sessions(user_id_str)`` -> (trainee dict or None, list of
      display-ready session dicts), for ``?view=sessions&user_id=...``.
    """
    template = _MINI_APP_PATH.read_text(encoding="utf-8")
    handler = _make_handler(
        secret, template, get_payload, list_trainees, get_trainee_history,
        get_trainee_sessions,
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Mini-app HTTP server listening on port %s", port)
    return server
