"""A fixed-destination model gateway for an isolated code task."""

from __future__ import annotations

import hmac
import http.client
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

_POST_PATHS = {"/api/chat", "/api/generate", "/v1/chat/completions", "/chat/completions"}
_GET_PATHS = {"/api/tags", "/v1/models", "/models"}
_MAX_BODY = 32 * 1024 * 1024


def make_proxy(host: str, port: int, upstream: str, secret: str) -> ThreadingHTTPServer:
    target = urlsplit(upstream)
    if (
        target.scheme not in {"http", "https"}
        or not target.hostname
        or target.username
        or target.password
        or target.query
        or target.fragment
        or not secret
        or "/" in secret
    ):
        raise ValueError("Invalid model gateway configuration")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self._forward(_GET_PATHS)

        def do_POST(self):
            self._forward(_POST_PATHS)

        def _forward(self, allowed: set[str]) -> None:
            prefix, sep, path = self.path.lstrip("/").partition("/")
            path = "/" + path
            if not sep or not hmac.compare_digest(prefix, secret) or path not in allowed:
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > _MAX_BODY or self.headers.get("Transfer-Encoding"):
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                cls = (
                    http.client.HTTPSConnection
                    if target.scheme == "https"
                    else http.client.HTTPConnection
                )
                connection = cls(target.hostname, target.port, timeout=120)
                headers = {
                    key: self.headers[key]
                    for key in ("Content-Type", "Authorization")
                    if key in self.headers
                }
                connection.request(
                    self.command, target.path.rstrip("/") + path, body=body, headers=headers
                )
                response = connection.getresponse()
                self.send_response(response.status)
                for key in ("Content-Type", "Content-Length"):
                    if response.getheader(key):
                        self.send_header(key, response.getheader(key))
                self.send_header("Connection", "close")
                self.end_headers()
                while chunk := response.read1(8192):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                connection.close()
            except (OSError, ValueError, http.client.HTTPException):
                self.close_connection = True

    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    server = make_proxy(
        "0.0.0.0", 18765, os.environ["MODEL_UPSTREAM"], os.environ["MODEL_PROXY_SECRET"]
    )
    ttl = int(os.environ.get("MODEL_PROXY_MAX_SECONDS", "300"))
    if not 1 <= ttl <= 86400:
        raise ValueError("Invalid model gateway lifetime")
    timer = threading.Timer(ttl, server.shutdown)
    timer.daemon = True
    timer.start()
    try:
        server.serve_forever()
    finally:
        timer.cancel()
        server.server_close()
