"""SSE integration — GET /runs/{id}/events streams data: frames + [DONE] (P0-12)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Test client with the graph + pool mocked (no PG / LLM)."""
    with patch("forgeflow.api.main.init_pool", new_callable=AsyncMock) as mock_pool, \
         patch("forgeflow.api.main.compile_graph", new_callable=AsyncMock) as mock_graph, \
         patch("forgeflow.api.main.get_mcp_tools", new_callable=AsyncMock, return_value=[]):
        from forgeflow.api.main import app

        mock_pool.return_value = MagicMock()
        mock_graph.return_value = MagicMock()
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def _admin_headers() -> dict[str, str]:
    from forgeflow.auth.jwt import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id='admin', role='admin')}"}


def test_runs_sse_streams_events_and_terminates(client):
    headers = _admin_headers()

    # Create a run first so there is a stream to read.
    resp = client.post("/tasks", json={"intent": "分析销售数据并生成报告"}, headers=headers)
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # Read the SSE stream (the run already finished → history is replayed).
    with client.stream("GET", f"/runs/{run_id}/events", headers=headers) as stream:
        assert stream.status_code == 200
        assert stream.headers.get("content-type", "").startswith("text/event-stream")
        body = "".join(stream.iter_text())

    assert "data:" in body          # at least one frame
    assert "[DONE]" in body         # terminated cleanly
    assert "run.completed" in body or "run.started" in body


def test_runs_sse_requires_auth(client):
    resp = client.get("/runs/unknown-run/events")
    assert resp.status_code == 401
