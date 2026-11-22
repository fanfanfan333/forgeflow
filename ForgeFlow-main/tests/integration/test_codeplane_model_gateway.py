"""Opt-in Docker network test; the upstream is a local synthetic model server."""

import asyncio
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from forgeflow.codeplane.docker_engine import DockerOpenHandsEngine
from forgeflow.codeplane.engine import CodeJob

pytestmark = pytest.mark.skipif(
    os.environ.get("FORGEFLOW_RUN_DOCKER_TESTS") != "1",
    reason="Set FORGEFLOW_RUN_DOCKER_TESTS=1 for real Docker isolation tests",
)


class ModelStub(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


def test_task_network_can_only_access_the_model_gateway():
    upstream = ThreadingHTTPServer(("0.0.0.0", 0), ModelStub)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    settings = SimpleNamespace(
        codeplane_enabled=True, codeplane_docker_image="forgeflow-codeplane:1.49.6"
    )
    engine = DockerOpenHandsEngine(settings=settings)
    assert engine.available(), engine.reason
    job = CodeJob(run_id="gateway-test", base_url=f"http://127.0.0.1:{upstream.server_port}")
    try:
        engine._prepare_model_gateway(job)
        network, _, secret = engine._networks[job.run_id]
        probe = """import json, socket, sys, urllib.error, urllib.request
secret, port = sys.argv[1:]
base = 'http://model-proxy:18765/' + secret
request = urllib.request.Request(base + '/api/chat', data=b'{}')
assert json.load(urllib.request.urlopen(request, timeout=10))['ok'] is True
for path in ('/api/pull', '/http://example.com'):
    try:
        urllib.request.urlopen(urllib.request.Request(base + path, data=b'{}'), timeout=5)
        raise AssertionError('unexpected route allowed')
    except urllib.error.HTTPError as exc:
        assert exc.code == 403
for host, port in (('1.1.1.1', 443), ('host.docker.internal', int(port))):
    try:
        socket.create_connection((host, port), timeout=2)
        raise AssertionError('direct network access allowed')
    except OSError:
        pass
print('model allowed; direct network and other routes blocked')
"""
        completed = subprocess.run(
            [
                engine.interpreter,
                "run",
                "--rm",
                "--network",
                network,
                "--add-host",
                "host.docker.internal:host-gateway",
                "--entrypoint",
                "python",
                "python:3.13-slim",
                "-c",
                probe,
                secret,
                str(upstream.server_port),
            ],
            capture_output=True,
            timeout=45,
            check=False,
            env=engine._env(),
        )
        assert completed.returncode == 0, completed.stderr.decode(errors="replace").replace(
            secret, "[redacted]"
        )
        assert "direct network" in completed.stdout.decode()
    finally:
        asyncio.run(engine._cleanup_model_gateway(job))
        upstream.shutdown()
        upstream.server_close()
