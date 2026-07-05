"""Tiny HTTP server exposing the schedule-editing mini app, gated by a shared secret token.

Used when self-hosting the mini app (e.g. behind your own nginx) instead of
GitHub Pages, so only requests carrying the right ``token`` query parameter
can view it. Runs in a background thread, fully decoupled from the bot's own
asyncio polling loop.

The current schedule is injected into the page server-side on every request
(via ``get_payload``), so the shareable URL only needs the token — no long
encoded schedule data to carry around in the link.
"""

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_MINI_APP_PATH = Path(__file__).resolve().parent.parent / "docs" / "index.html"
_INJECT_AFTER = "<head>"


def _make_handler(secret: str, template: str, get_payload):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # The query string carries the secret token; never log it
            # (default BaseHTTPRequestHandler logging includes the full
            # request line).
            logger.info("webapp_server: %s %s", self.command, getattr(self, "_status", "?"))

        def _authorized(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            token = query.get("token", [""])[0]
            return hmac.compare_digest(token, secret)

        def _render(self) -> bytes:
            data_script = f"<script>window.__MINI_APP_DATA__ = {json.dumps(get_payload())};</script>"
            html = template.replace(_INJECT_AFTER, _INJECT_AFTER + data_script, 1)
            return html.encode("utf-8")

        def _respond(self, include_body: bool):
            if not self._authorized():
                self._status = 403
                self.send_response(403)
                self.end_headers()
                if include_body:
                    self.wfile.write(b"Forbidden")
                return
            body = self._render()
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


def start_webapp_server(secret: str, port: int, get_payload) -> ThreadingHTTPServer:
    """Start serving the mini app on ``port`` in a background thread.

    ``get_payload`` is called fresh on every authorized request (no args,
    returns the JSON-serializable schedule dict) so the page always opens
    with the live schedule, not a snapshot baked into the URL.
    """
    template = _MINI_APP_PATH.read_text(encoding="utf-8")
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(secret, template, get_payload))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Mini-app HTTP server listening on port %s", port)
    return server
