"""INC8 Phase-B / T04 — ``GET /metrics/agent-eval`` (the API surface).

Pins the design §6 contract:

  * the memory profile serves the in-process hub-run aggregate
    (``source="hub_runs"``, ``durable=False``) with the 12 metrics × 3 dimensions;
  * a ⛔ metric (no ground truth) serialises as a real JSON ``null`` — never ``0``;
  * a failed PostgreSQL read is reported (``degraded=True`` + ``error``) rather
    than swallowed;
  * the new path is covered by the existing ``("GET","/metrics")`` RBAC prefix —
    no phantom / unmapped route is introduced.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import metrics as metrics_router
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store

_UTC = _dt.timezone.utc
_BASE = _dt.datetime(2026, 9, 24, 9, 0, 0, tzinfo=_UTC)


def _pin_backend(monkeypatch, backend: str) -> None:
    from forgeflow.config import get_settings
    from forgeflow.evaluation.eval_source import reset_agent_eval_source
    from forgeflow.observability.metrics_source import reset_metrics_source

    monkeypatch.setattr(get_settings(), "storage_backend", backend)
    reset_metrics_source()
    reset_agent_eval_source()


def _app(pool):
    app = FastAPI()
    app.include_router(metrics_router.router, prefix="/metrics")
    app.state.pool = pool
    return app


def _seed_run(run_id: str, *, status: str, offset_ms: int, cost: float) -> None:
    get_run_store().save(
        RunRecord(
            run_id=run_id,
            thread_id=f"t-{run_id}",
            tenant_id="default",
            agent_id=None,
            intent="x",
            status=status,
            outcome="ok",
            steps=[],
            errors=[],
            created_at=_BASE.isoformat(),
            completed_at=(_BASE + _dt.timedelta(milliseconds=offset_ms)).isoformat(),
            total_cost_usd=cost,
            plan_source="llm",
            skills_used=["s-a"],
        )
    )


@pytest.fixture(autouse=True)
def _clean(force_memory_backend):
    reset_run_store()
    yield
    reset_run_store()


def test_memory_summary_schema_and_honest_nulls(force_memory_backend):
    _seed_run("r1", status="completed", offset_ms=100, cost=0.02)
    _seed_run("r2", status="failed", offset_ms=300, cost=0.04)

    resp = TestClient(_app(pool=None)).get("/metrics/agent-eval")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["source"] == "hub_runs"
    assert body["durable"] is False
    assert body["sample_runs"] == 2
    assert body["degraded"] is False

    # Three dimensions, each a {metrics: {...}} map.
    for dim in ("quality", "cost", "reliability"):
        assert "metrics" in body[dim]

    assert body["quality"]["metrics"]["task_success_rate"]["value"] == pytest.approx(0.5)
    assert body["cost"]["metrics"]["cost_per_task"]["has_data"] is True

    # A ⛔ metric is a real JSON null, not a fabricated 0.
    citation = body["quality"]["metrics"]["citation_accuracy"]
    assert citation["value"] is None
    assert citation["computability"] == "not_available"


def test_empty_store_is_null_not_zero(force_memory_backend):
    resp = TestClient(_app(pool=None)).get("/metrics/agent-eval")
    body = resp.json()
    assert body["sample_runs"] == 0
    assert body["quality"]["metrics"]["task_success_rate"]["value"] is None
    assert body["reliability"]["metrics"]["latency_p95_ms"]["value"] is None


def test_offline_samples_endpoint_is_empty_list(force_memory_backend):
    resp = TestClient(_app(pool=None)).get("/metrics/agent-eval/samples")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_pg_summary_reports_postgres_source(monkeypatch, mock_pool):
    _pin_backend(monkeypatch, "postgres")
    resp = TestClient(_app(pool=mock_pool)).get("/metrics/agent-eval")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "postgres"
    assert body["durable"] is True


def test_pg_query_failure_is_reported_explicitly(monkeypatch):
    _pin_backend(monkeypatch, "postgres")
    pool = type("P", (), {})()
    pool.acquire = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    resp = TestClient(_app(pool=pool)).get("/metrics/agent-eval")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] is True
    assert "db down" in body["error"]


def test_window_days_is_validated():
    resp = TestClient(_app(pool=None)).get("/metrics/agent-eval?window_days=0")
    assert resp.status_code == 422


def test_new_routes_resolve_to_the_metrics_permission():
    """The new paths must fall under the existing ``("GET","/metrics")`` gate."""
    from forgeflow.middleware.auth import RBACMiddleware

    for path in ("/metrics/agent-eval", "/metrics/agent-eval/samples"):
        assert RBACMiddleware._resolve_permission("GET", path) == ("read", "metrics")
