import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from forgeflow.codeplane.model_proxy import make_proxy


class ModelStub(BaseHTTPRequestHandler):
    def do_POST(self):
        data = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def proxy():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
    server = make_proxy("127.0.0.1", 0, f"http://127.0.0.1:{upstream.server_port}", "secret")
    for item in (upstream, server):
        threading.Thread(target=item.serve_forever, daemon=True).start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        upstream.shutdown()
        server.server_close()
        upstream.server_close()


def test_proxy_allows_only_authenticated_model_requests(proxy):
    conn = http.client.HTTPConnection("127.0.0.1", proxy, timeout=2)
    conn.request("POST", "/secret/api/chat", json.dumps({"model": "test"}))
    response = conn.getresponse()
    assert response.status == 200 and json.loads(response.read()) == {"model": "test"}
    conn.close()
    for path in ("/wrong/api/chat", "/secret/api/pull", "/secret/http://127.0.0.1:22"):
        conn = http.client.HTTPConnection("127.0.0.1", proxy, timeout=2)
        conn.request("POST", path, b"{}")
        assert conn.getresponse().status == 403
        conn.close()
