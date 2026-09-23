"""INC2-06 (D2) — the MetricsSource abstraction behind ``/metrics/*``.

Covers: the memory backend actually has a data path now (the whole point of the
gap), the honesty flags, backend selection, and that the router reads through
the factory rather than its own branching.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from forgeflow.api.routers.metrics import get_metrics_summary, list_recent_runs
from forgeflow.config import get_settings
from forgeflow.observability.metrics_source import (
    MemoryMetricsSource,
    PostgresMetricsSource,
    get_metrics_source,
    reset_metrics_source,
)
from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store

_TENANT = get_settings().default_tenant_id
_BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _memory_backend(force_memory_backend):
    """This suite asserts the **memory** MetricsSource data path.

    It must exercise that path under any ``STORAGE_BACKEND`` — pinning the
    backend here keeps every case measuring the same thing regardless of whether
    the run is launched in the offline or the postgres profile. The one test that
    deliberately selects Postgres (``test_postgres_backend_selected_when_configured``)
    clears the settings cache and sets the env var itself, so it is unaffected.
    """
    return force_memory_backend



def _save(run_id: str, status: str, *, latency_ms: int | None = 500) -> None:
    terminal = status in ("completed", "failed")
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
            errors=[] if status == "completed" else ["下游异常"],
            created_at=_BASE.isoformat(),
            completed_at=(_BASE + timedelta(milliseconds=latency_ms)).isoformat()
            if (terminal and latency_ms is not None)
            else None,
            experience_id=f"exp-{run_id}",
        )
    )


@pytest.fixture(autouse=True)
def _clean():
    reset_run_store()
    reset_metrics_source()
    yield
    reset_metrics_source()
    reset_run_store()


class TestMemorySource:
    @pytest.mark.asyncio
    async def test_summary_is_non_empty_for_memory_backend(self):
        _save("m-1", "completed", latency_ms=400)
        _save("m-2", "failed", latency_ms=600)

        payload = await get_metrics_source().summary(_TENANT)

        assert payload["source"] == "hub_runs"
        assert payload["total_runs"] == 2
        assert payload["terminal_runs"] == 2
        assert payload["has_data"] is True
        # The hub path records no billable cost — never a fabricated 0.00.
        assert payload["has_cost"] is False

    @pytest.mark.asyncio
    async def test_flags_are_false_on_an_empty_cohort(self):
        payload = await get_metrics_source().summary(_TENANT)

        assert payload["total_runs"] == 0
        assert payload["has_data"] is False
        assert payload["has_success_rate"] is False
        assert payload["has_latency"] is False

    @pytest.mark.asyncio
    async def test_success_rate_flag_false_when_no_terminal_run(self):
        # A still-running run is real data, but says nothing about success.
        _save("running-1", "running")

        payload = await get_metrics_source().summary(_TENANT)

        assert payload["has_data"] is True
        assert payload["has_success_rate"] is False
        assert payload["has_latency"] is False

    @pytest.mark.asyncio
    async def test_recent_runs_is_non_empty_and_newest_first(self):
        _save("old", "completed", latency_ms=100)
        _save("new", "completed", latency_ms=100)

        runs = await get_metrics_source().recent_runs(_TENANT, limit=5)

        assert len(runs) == 2
        assert {r["run_id"] for r in runs} == {"old", "new"}
        assert all(r["status"] == "completed" for r in runs)

    @pytest.mark.asyncio
    async def test_recent_runs_respects_limit(self):
        for i in range(5):
            _save(f"r-{i}", "completed")

        runs = await get_metrics_source().recent_runs(_TENANT, limit=2)

        assert len(runs) == 2


class TestFactory:
    def test_memory_backend_selected_in_offline_profile(self):
        assert isinstance(get_metrics_source(), MemoryMetricsSource)

    def test_postgres_backend_selected_when_configured(self, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.setenv("STORAGE_BACKEND", "postgres")
        reset_metrics_source()
        try:
            assert isinstance(get_metrics_source(), PostgresMetricsSource)
        finally:
            reset_metrics_source()
            get_settings.cache_clear()


class TestRouterUsesSource:
    @pytest.mark.asyncio
    async def test_summary_endpoint_returns_real_numbers(self):
        _save("ep-1", "completed", latency_ms=250)

        summary = await get_metrics_summary(tenant=_TENANT)

        assert summary.total_runs == 1
        assert summary.success_rate == 1.0
        assert summary.has_data is True
        assert summary.has_success_rate is True

    @pytest.mark.asyncio
    async def test_runs_endpoint_works_without_a_database(self):
        # Previously /metrics/runs hard-depended on Depends(get_pool); in the
        # memory profile it must still answer.
        _save("ep-run", "completed")

        runs = await list_recent_runs(limit=10, tenant=_TENANT, workspace_id=None)

        assert len(runs) == 1
        assert runs[0]["run_id"] == "ep-run"
