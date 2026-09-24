"""INC8-A1 — ``GET /metrics/evaluation`` must not fail-open-swallow a PG error.

Before the fix a failed PostgreSQL query was swallowed (``except Exception:
pass``) and the route returned an all-zero summary that was indistinguishable
from a genuinely empty window. The endpoint now reports a query failure
**explicitly** (``degraded=True`` + ``error``) while the offline profile keeps the
honest all-zero payload with ``degraded=False`` — "no data" is not a degradation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import metrics as metrics_router


def _pin_backend(monkeypatch, backend: str) -> None:
    from forgeflow.config import get_settings
    from forgeflow.observability.metrics_source import reset_metrics_source

    monkeypatch.setattr(get_settings(), "storage_backend", backend)
    reset_metrics_source()


def _app(pool):
    app = FastAPI()
    app.include_router(metrics_router.router, prefix="/metrics")
    app.state.pool = pool
    return app


def _failing_pool():
    """A pool whose ``acquire()`` raises — models a broken DB connection."""
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("db down"))
    return pool


def test_offline_summary_is_honest_zero_not_degraded(monkeypatch):
    _pin_backend(monkeypatch, "memory")
    resp = TestClient(_app(pool=None)).get("/metrics/evaluation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sample_count"] == 0
    # No data ≠ query broken: the optional degrade fields stay at their defaults
    # (excluded from the payload via response_model_exclude_defaults).
    assert "degraded" not in body
    assert "error" not in body


def test_pg_query_failure_is_reported_explicitly(monkeypatch):
    _pin_backend(monkeypatch, "postgres")
    resp = TestClient(_app(pool=_failing_pool())).get("/metrics/evaluation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["degraded"] is True
    assert isinstance(body["error"], str) and body["error"].strip()
    assert "db down" in body["error"]
    assert body["sample_count"] == 0


def test_pg_empty_table_is_zero_but_not_degraded(monkeypatch, mock_pool):
    # mock_pool.fetchrow -> None: a real empty table, not a broken query.
    _pin_backend(monkeypatch, "postgres")
    resp = TestClient(_app(pool=mock_pool)).get("/metrics/evaluation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "degraded" not in body
    assert body["sample_count"] == 0


def test_pg_success_path_is_unchanged(monkeypatch):
    _pin_backend(monkeypatch, "postgres")
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "avg_faithfulness": 0.8,
            "avg_relevance": 0.7,
            "avg_coherence": 0.9,
            "hallucination_rate": 0.1,
            "sample_count": 3,
        }
    )
    pool = MagicMock()
    pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=None),
        )
    )

    resp = TestClient(_app(pool=pool)).get("/metrics/evaluation")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["avg_faithfulness"] == pytest.approx(0.8)
    assert body["sample_count"] == 3
    assert "degraded" not in body
