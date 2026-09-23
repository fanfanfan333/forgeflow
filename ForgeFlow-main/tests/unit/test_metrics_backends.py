"""WS-B2 / D2 — the legacy ``/metrics/*`` views must answer under BOTH backends.

The six cost / evaluation / prometheus routes previously hard-depended on
``Depends(get_pool)``, so the offline (memory) profile returned
``503 Database pool not initialised`` — breaking the "双后端不可破" rule and the
D2 ruling that ``/metrics/*`` is served uniformly regardless of backend.

Contract pinned here:

* offline profile (no pool)  → HTTP 200 with an honest **empty** payload
  (``[]`` / all-zero evaluation), never a 503 and never a fabricated value;
* postgres profile (no pool) → the historical ``503`` is preserved.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import metrics as metrics_router

COST_PATHS = [
    "/metrics/cost",
    "/metrics/cost/by_workflow_type",
    "/metrics/cost/top_runs",
    "/metrics/cost/alerts",
]

_ZERO_EVALUATION = {
    "avg_faithfulness": 0.0,
    "avg_relevance": 0.0,
    "avg_coherence": 0.0,
    "hallucination_rate": 0.0,
    "sample_count": 0,
}


def _client() -> TestClient:
    """A bare app with only the metrics router — no lifespan, so no pool."""
    app = FastAPI()
    app.include_router(metrics_router.router, prefix="/metrics")
    return TestClient(app)


def _pin_backend(monkeypatch, backend: str) -> None:
    """Make the live settings resolve ``backend`` for this test."""
    from forgeflow.config import get_settings
    from forgeflow.observability.metrics_source import reset_metrics_source

    monkeypatch.setattr(get_settings(), "storage_backend", backend)
    reset_metrics_source()


@pytest.mark.parametrize("path", COST_PATHS)
def test_cost_views_answer_empty_in_the_offline_profile(monkeypatch, path):
    _pin_backend(monkeypatch, "memory")
    response = _client().get(path)
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_evaluation_view_returns_the_honest_zero_summary_offline(monkeypatch):
    _pin_backend(monkeypatch, "memory")
    response = _client().get("/metrics/evaluation")
    assert response.status_code == 200, response.text
    assert response.json() == _ZERO_EVALUATION


def test_prometheus_scrape_does_not_require_a_pool_offline(monkeypatch):
    _pin_backend(monkeypatch, "memory")
    from forgeflow.observability.prometheus import _build_registry

    app = FastAPI()
    app.include_router(metrics_router.router, prefix="/metrics")
    registry, collectors = _build_registry()
    app.state.prom_registry = registry
    app.state.prom_metrics = collectors

    response = TestClient(app).get("/metrics/prometheus")
    assert response.status_code == 200, response.text
    assert "text/plain" in response.headers["content-type"]


@pytest.mark.parametrize("path", COST_PATHS)
def test_cost_views_still_503_without_a_pool_on_postgres(monkeypatch, path):
    _pin_backend(monkeypatch, "postgres")
    response = _client().get(path)
    assert response.status_code == 503
    assert response.json()["detail"] == "Database pool not initialised"


def test_evaluation_view_still_503_without_a_pool_on_postgres(monkeypatch):
    _pin_backend(monkeypatch, "postgres")
    response = _client().get("/metrics/evaluation")
    assert response.status_code == 503
    assert response.json()["detail"] == "Database pool not initialised"
