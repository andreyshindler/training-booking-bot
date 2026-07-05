"""Tiny HTTP server exposing the schedule-editing mini app, gated by a shared secret token.

Used when self-hosting the mini app (e.g. behind your own nginx) instead of
GitHub Pages, so only requests carrying the right ``token`` query parameter
can view it. Runs in a background thread, fully decoupled from the bot's own
asyncio polling loop.
"""

import hmac
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_MINI_APP_PATH = Path(__file__).resolve().parent.parent / "docs" / "index.html"


def _make_handler(secret: str, html: bytes):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # The query string carries the secret token and the schedule
            # payload; never log it (default BaseHTTPRequestHandler logging
            # includes the full request line).
            logger.info("webapp_server: %s %s", self.command, getattr(self, "_status", "?"))

        def _authorized(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            token = query.get("token", [""])[0]
            return hmac.compare_digest(token, secret)

        def _respond(self, include_body: bool):
            if not self._authorized():
                self._status = 403
                self.send_response(403)
                self.end_headers()
                if include_body:
                    self.wfile.write(b"Forbidden")
                return
            self._status = 200
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            if include_body:
                self.wfile.write(html)

        def do_GET(self):
            self._respond(include_body=True)

        def do_HEAD(self):
            self._respond(include_body=False)

    return Handler


def start_webapp_server(secret: str, port: int) -> ThreadingHTTPServer:
    """Start serving the mini app on ``port`` in a background thread."""
    html = _MINI_APP_PATH.read_bytes()
    server = ThreadingHTTPServer(("0.0.0.0", port), _make_handler(secret, html))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Mini-app HTTP server listening on port %s", port)
    return server
