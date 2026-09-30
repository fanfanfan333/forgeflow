"""INC20 / T01 — ``GET /runs/{id}`` must distinguish "未测量" (``null``) from a real ``0``.

Root cause of the "Token 用量 0" lie this increment removes: the transport layer
collapsed BOTH cases into ``0`` — ``hub_schemas.RunDetailResponse`` defaulted
``total_tokens``/``total_cost_usd`` to ``0``/``0.0``, and ``runs.py`` coerced with
``int(... or 0)`` / ``float(... or 0.0)``. So a consumer could never tell a
pre-INC4 record (never measured) from a genuine zero.

These tests pin the fix at the REAL route (not a hand-built response object):

* a record that never carried the attribute ⇒ ``null`` (未测量);
* a record with a genuine ``0`` ⇒ ``0`` stays ``0`` (distinct from ``null``).

⚠️ Residual indivisibility (recorded here, NOT asserted as fixed): the producer
(``orchestrator.py::run_task`` 中 ``total_tokens=int(cost_summary["total_tokens"])`` 处) still writes ``int``/``float`` for every real run,
so a deterministic run carries a genuine-looking ``0``. This endpoint can only
distinguish "attribute absent" from ``0`` — consumers must judge "did a model
run?" by **model-driven evidence**, never by the token value itself (see
``frontend/src/views/runs/roles.ts::isModelDriven``).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.runtime.orchestrator import get_run_store, reset_run_store


class _BareRecord:
    """A stored run WITHOUT ``total_tokens`` / ``total_cost_usd`` (pre-INC4 shape).

    Deliberately carries only the attributes the detail route reads via plain
    attribute access; the usage fields are read via ``getattr(..., None)`` so
    their absence (not a ``0``) is what the route must report.
    """

    def __init__(self, run_id: str, tenant: str) -> None:
        self.run_id = run_id
        self.thread_id = "thread-inc20"
        self.status = "completed"
        self.outcome = "success"
        self.intent = "生成本周业务报告"
        self.steps: list[dict] = []
        self.errors: list[str] = []
        self.created_at = "2026-01-01T00:00:00+00:00"
        self.completed_at = "2026-01-01T00:00:01+00:00"
        self.experience_id = None
        self.tenant_id = tenant


class _ZeroRecord(_BareRecord):
    """Same shape, but with a REAL ``0`` — which must survive as ``0``, not ``null``."""

    total_tokens = 0
    total_cost_usd = 0.0


def _client(tenant: str, record: object) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    get_run_store().save(record)
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()


def test_missing_usage_passes_through_as_null():
    """No attribute ⇒ ``null`` (未测量), never a fabricated ``0``."""
    record = _BareRecord("run-inc20-null", "t-inc20-null")
    response = _client(record.tenant_id, record).get(f"/runs/{record.run_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_tokens"] is None
    assert body["total_cost_usd"] is None


def test_a_real_zero_is_preserved_as_zero():
    """A genuine ``0`` stays ``0`` — distinguishable from the ``null`` above."""
    record = _ZeroRecord("run-inc20-zero", "t-inc20-zero")
    response = _client(record.tenant_id, record).get(f"/runs/{record.run_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_tokens"] == 0
    assert body["total_cost_usd"] == 0.0
    # The two cases must NOT collapse onto the same value.
    assert body["total_tokens"] is not None
    assert body["total_cost_usd"] is not None
