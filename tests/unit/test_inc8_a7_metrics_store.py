"""INC8-A7 — ``run_metrics`` write side: diagnosability + real production caller.

Two defects this closes:

  1. ``write_metric``'s failure log was ``...failed: %s`` — a wired-but-broken
     writer was indistinguishable from a healthy one. It must log the exception
     **type** + ``run_id`` + ``metric_name``. Read methods keep their swallow
     semantics untouched (``tests/unit/test_metrics_store.py::test_swallows_errors``).
  2. ``record_run_completion`` had **zero callers** (a dead machine). It is now
     invoked by the native workflow path — the only place a ``workflow_runs`` row
     exists. This test pins that wiring (the route really calls it).
"""

from __future__ import annotations

import json
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import workflows as wf
from forgeflow.observability.metrics_store import MetricsStore
from forgeflow.rbac.models import UserContext

pytestmark = pytest.mark.asyncio


def _pool_with_conn():
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = MagicMock()
    pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    return pool, conn


# --------------------------------------------------------------------------- #
# 1. diagnosable write failure                                                 #
# --------------------------------------------------------------------------- #
async def test_write_metric_failure_logs_type_run_id_and_metric_name(caplog):
    pool = MagicMock()
    pool.acquire.side_effect = RuntimeError("db down")
    store = MetricsStore(pool)

    with caplog.at_level(logging.ERROR, logger="forgeflow.observability.metrics_store"):
        await store.write_metric("run-xyz", "cost_usd", 1.23, "usd")

    text = caplog.text
    assert "RuntimeError" in text, f"exception type missing: {text!r}"
    assert "run-xyz" in text, f"run_id missing: {text!r}"
    assert "cost_usd" in text, f"metric_name missing: {text!r}"


async def test_record_run_completion_writes_the_standard_metric_set():
    pool, conn = _pool_with_conn()
    await MetricsStore(pool).record_run_completion(
        "11111111-1111-1111-1111-111111111111",
        latency_ms=812.5,
        total_tokens=120,
        total_cost_usd=0.03,
        success=True,
        agent_name="sales_ops",
    )

    assert conn.execute.await_count == 4
    # Metric names + values land in the INSERT's positional args ($2, $3).
    rows = {call.args[2]: call.args[3] for call in conn.execute.await_args_list}
    assert rows == {
        "latency_ms": 812.5,
        "tokens_used": 120,
        "cost_usd": 0.03,
        "success": 1.0,
    }
    # Tags are serialised to JSON (the column is JSONB; asyncpg needs a string),
    # and carry the success flag rather than letting it be guessed.
    first_tags = json.loads(conn.execute.await_args_list[0].args[5])
    assert first_tags["agent"] == "sales_ops"
    assert first_tags["success"] == "True"


async def test_read_methods_still_swallow_errors():
    """Guard: A7 must not have touched the read methods' swallow semantics."""
    pool = MagicMock()
    pool.acquire.side_effect = RuntimeError("nope")
    store = MetricsStore(pool)
    assert await store.get_cost_by_workflow_type(days=7) == []
    assert await store.get_summary() == {}


# --------------------------------------------------------------------------- #
# 2. the route really calls the writer                                         #
# --------------------------------------------------------------------------- #
def _route_app(*, graphs, pool, user) -> FastAPI:
    app = FastAPI()
    app.include_router(wf.router, prefix="/workflows")
    app.dependency_overrides[wf.get_graphs] = lambda: graphs
    app.dependency_overrides[wf.get_pool] = lambda: pool
    app.dependency_overrides[wf.get_current_user] = lambda: user
    app.dependency_overrides[wf.get_workspace_id] = lambda: None
    return app


async def test_workflow_route_records_run_metrics(monkeypatch):
    workflow_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())

    async def _fake_run(self, domain_input, *, user_id, role, dry_run=False):
        return workflow_id, thread_id, {
            "current_stage": "done",
            "total_tokens": 321,
            "total_cost_usd": 0.07,
        }

    monkeypatch.setattr(wf.SalesOpsPipeline, "run", _fake_run)

    recorded: list[tuple] = []

    async def _spy(self, run_id, latency_ms, total_tokens, total_cost_usd, success,
                   agent_name="workflow"):
        recorded.append((run_id, total_tokens, total_cost_usd, success, agent_name))

    monkeypatch.setattr(MetricsStore, "record_run_completion", _spy)

    pool, _ = _pool_with_conn()
    app = _route_app(
        graphs={"sales_ops": object()},
        pool=pool,
        user=UserContext(user_id="admin-1", role="admin"),
    )
    client = TestClient(app)
    resp = client.post(
        "/workflows/run", json={"lead_data": {"company_name": "Acme"}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["run_id"] == workflow_id

    assert recorded, "the native workflow route must call record_run_completion"
    assert recorded[0][0] == workflow_id
    assert recorded[0][1] == 321
    assert recorded[0][2] == pytest.approx(0.07)
    assert recorded[0][3] is True  # stage == "done"
    assert recorded[0][4] == "sales_ops"
