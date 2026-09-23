"""Tests for the home-dashboard KPI aggregation (``GET /metrics/``).

The KPIs must reflect the **live hub run store** — the ``POST /tasks`` path —
not the legacy ``run_metrics`` table (which the hub path never writes). These
cover the two cases the defect report calls out: an all-success cohort and a
cohort that contains failures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from forgeflow.api.routers.metrics import get_metrics_summary
from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store

_TENANT = get_settings().default_tenant_id
_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _save(run_id: str, status: str, *, latency_ms: int = 500) -> None:
    """Persist one terminal run directly into the hub run store."""
    completed = status in ("completed", "failed")
    get_run_store().save(
        RunRecord(
            run_id=run_id,
            thread_id=f"t-{run_id}",
            tenant_id=_TENANT,
            agent_id=None,
            intent="分析销售数据",
            status=status,
            outcome="success" if status == "completed" else "failed",
            steps=[{"tool": "research.search"}],
            errors=[] if status == "completed" else ["下游工具返回异常"],
            created_at=_BASE.isoformat(),
            completed_at=(_BASE + timedelta(milliseconds=latency_ms)).isoformat()
            if completed
            else None,
            experience_id=f"exp-{run_id}",
        )
    )


@pytest.fixture(autouse=True)
def _clean_run_store():
    reset_run_store()
    yield
    reset_run_store()


@pytest.fixture(autouse=True)
def _memory_backend(force_memory_backend):
    """The dashboard KPIs read the **live hub run store** — the memory source.

    Pin the backend so these cases measure that store even when the suite runs
    with ``STORAGE_BACKEND=postgres`` (where the KPIs would otherwise read the
    unrelated ``run_metrics`` table and report 0).
    """
    return force_memory_backend



@pytest.mark.asyncio
async def test_summary_all_success() -> None:
    for i in range(4):
        _save(f"ok-{i}", "completed", latency_ms=400)

    summary = await get_metrics_summary(tenant=_TENANT)

    assert summary.total_runs == 4
    assert summary.terminal_runs == 4
    assert summary.success_rate == 1.0
    assert summary.avg_latency_ms == pytest.approx(400.0)
    assert summary.has_data is True
    # The hub path makes no billable calls, so cost is explicitly "no data"
    # rather than a fabricated 0.00.
    assert summary.has_cost is False
    assert summary.total_cost_usd == 0.0
    assert summary.avg_cost_usd == 0.0


@pytest.mark.asyncio
async def test_summary_with_failures() -> None:
    _save("ok-1", "completed", latency_ms=300)
    _save("ok-2", "completed", latency_ms=900)
    _save("bad-1", "failed", latency_ms=600)

    summary = await get_metrics_summary(tenant=_TENANT)

    assert summary.total_runs == 3
    assert summary.terminal_runs == 3
    assert summary.success_rate == pytest.approx(2 / 3)
    assert summary.avg_latency_ms == pytest.approx((300 + 900 + 600) / 3)
    assert summary.has_data is True


@pytest.mark.asyncio
async def test_summary_empty_is_flagged_no_data() -> None:
    summary = await get_metrics_summary(tenant=_TENANT)

    assert summary.total_runs == 0
    assert summary.terminal_runs == 0
    assert summary.success_rate == 0.0
    assert summary.avg_latency_ms == 0.0
    assert summary.has_data is False


@pytest.mark.asyncio
async def test_summary_is_tenant_scoped() -> None:
    _save("ok-scoped", "completed")
    # A run in a different tenant must not leak into this tenant's KPIs.
    get_run_store().save(
        RunRecord(
            run_id="other-tenant",
            thread_id="t-other",
            tenant_id="acme",
            agent_id=None,
            intent="其它租户任务",
            status="completed",
            outcome="success",
            steps=[],
            errors=[],
            created_at=_BASE.isoformat(),
            completed_at=(_BASE + timedelta(milliseconds=100)).isoformat(),
            experience_id="exp-other",
        )
    )

    summary = await get_metrics_summary(tenant=_TENANT)

    assert summary.total_runs == 1
    assert summary.success_rate == 1.0
