"""INC-41 F-122 — ``PostgresMetricsSource`` aggregates the hub ``workspace_runs``.

Root cause (round-4 acceptance, F-122 P1): the product's **only** reachable run
path is the hub (``POST /tasks``), which persists to ``workspace_runs`` with a
**TEXT** ``run_id`` (migration 016). The legacy ``run_metrics`` table has a
``run_id uuid`` FK to ``workflow_runs(id)`` (also ``uuid``) that the hub path can
never satisfy, so aggregating ``run_metrics`` made every hub KPI structurally
``0`` (the "管道断裂"). The fix makes ``workspace_runs`` the primary source and
keeps the legacy aggregate as a compatibility fallback.

These pin the new primary source, its honest ``has_*`` flags and its tenant
scoping — with a fake pool, i.e. **no PostgreSQL server**.

Citation discipline: ``file.py::symbol`` anchors, never line numbers.
"""

from __future__ import annotations

from typing import Any

import pytest

from forgeflow.config import get_settings
from forgeflow.observability.metrics_source import PostgresMetricsSource

pytestmark = pytest.mark.asyncio


class _FakeConn:
    """Asyncpg-connection double with a scripted ``fetchrow`` / ``fetch`` result."""

    def __init__(
        self,
        captured: list[tuple[str, str, tuple]],
        *,
        fetchrow_result: Any = None,
        fetch_result: Any = None,
    ) -> None:
        self._captured = captured
        self._fetchrow_result = fetchrow_result
        self._fetch_result = fetch_result

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._captured.append(("fetchrow", sql, args))
        return self._fetchrow_result

    async def fetch(self, sql: str, *args: Any) -> Any:
        self._captured.append(("fetch", sql, args))
        return list(self._fetch_result or [])


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    """Minimal asyncpg-pool double recording every statement + its bound args."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _pool(
    *,
    captured: list[tuple[str, str, tuple]],
    fetchrow_result: Any = None,
    fetch_result: Any = None,
) -> _FakePool:
    return _FakePool(
        _FakeConn(
            captured, fetchrow_result=fetchrow_result, fetch_result=fetch_result
        )
    )


# --------------------------------------------------------------------------- #
# summary — the primary hub source                                             #
# --------------------------------------------------------------------------- #
async def test_summary_aggregates_workspace_runs_and_is_honest():
    captured: list[tuple[str, str, tuple]] = []
    pool = _pool(
        captured=captured,
        fetchrow_result={"total_runs": 5, "terminal_runs": 4, "completed_runs": 3},
    )

    summary = await PostgresMetricsSource(pool=pool).summary("tenant-a")

    # INC-41 F-135 — `source` is API surface and keeps "postgres"; which table the
    # numbers came from is surfaced in the additive `source_detail`.
    assert summary["source"] == "postgres"
    assert summary["source_detail"] == "workspace_runs"
    assert summary["total_runs"] == 5
    assert summary["terminal_runs"] == 4
    # Success rate is over TERMINAL runs only.
    assert summary["success_rate"] == pytest.approx(3 / 4)
    # workspace_runs records no latency / cost → honest "not measured".
    assert summary["avg_latency_ms"] == 0.0
    assert summary["has_latency"] is False
    assert summary["total_cost_usd"] == 0.0
    assert summary["avg_cost_usd"] == 0.0
    assert summary["has_cost"] is False
    assert summary["has_data"] is True
    assert summary["has_success_rate"] is True

    # The predicate is on workspace_runs and bound to the caller's tenant.
    _kind, sql, args = captured[0]
    assert "workspace_runs" in sql
    assert "tenant_id IS NOT DISTINCT FROM" in sql
    assert args == ("tenant-a",)


async def test_summary_running_runs_only_flags_no_success_rate():
    """Runs exist but none is terminal ⇒ a success rate is meaningless."""
    captured: list[tuple[str, str, tuple]] = []
    pool = _pool(
        captured=captured,
        fetchrow_result={"total_runs": 2, "terminal_runs": 0, "completed_runs": 0},
    )

    summary = await PostgresMetricsSource(pool=pool).summary("tenant-a")

    assert summary["source"] == "postgres"
    assert summary["source_detail"] == "workspace_runs"
    assert summary["has_data"] is True
    assert summary["success_rate"] == 0.0
    assert summary["has_success_rate"] is False


async def test_summary_defaults_tenant_to_settings_default():
    captured: list[tuple[str, str, tuple]] = []
    pool = _pool(
        captured=captured,
        fetchrow_result={"total_runs": 1, "terminal_runs": 1, "completed_runs": 1},
    )

    await PostgresMetricsSource(pool=pool).summary(None)

    assert captured[0][2] == (get_settings().default_tenant_id,)


async def test_summary_falls_back_to_legacy_when_workspace_empty(monkeypatch):
    """A tenant with no hub rows still reports the native ``/workflows/run`` path."""
    captured: list[tuple[str, str, tuple]] = []
    pool = _pool(
        captured=captured,
        fetchrow_result={"total_runs": 0, "terminal_runs": 0, "completed_runs": 0},
    )
    sentinel = {"source": "postgres", "total_runs": 7, "has_data": True}

    async def _fake_legacy(self: Any, _pool: Any) -> dict[str, Any]:
        return dict(sentinel)

    monkeypatch.setattr(PostgresMetricsSource, "_legacy_summary", _fake_legacy)

    assert await PostgresMetricsSource(pool=pool).summary("tenant-a") == sentinel


async def test_summary_degrades_without_raising_on_read_failure():
    class _BoomPool:
        def acquire(self) -> Any:  # noqa: ANN401 — a deliberate failure seam
            raise RuntimeError("db down")

    summary = await PostgresMetricsSource(pool=_BoomPool()).summary("tenant-a")

    # Never 500 the dashboard: the read degrades to an honest "no data".
    assert summary["has_data"] is False


# --------------------------------------------------------------------------- #
# recent_runs — the hub rows carry no token / cost column                      #
# --------------------------------------------------------------------------- #
async def test_recent_runs_reads_workspace_rows_with_null_tokens_and_cost():
    captured: list[tuple[str, str, tuple]] = []
    rows = [
        {
            "run_id": "run-1",
            "session_id": "sess-1",
            "workflow_type": "agentflow_task",
            "status": "completed",
            "created_at": "2026-10-01T10:00:00+00:00",
            "completed_at": "2026-10-01T10:00:05+00:00",
        }
    ]
    pool = _pool(captured=captured, fetch_result=rows)

    out = await PostgresMetricsSource(pool=pool).recent_runs("tenant-a", limit=5)

    assert len(out) == 1
    assert out[0]["run_id"] == "run-1"
    assert out[0]["thread_id"] == "sess-1"
    assert out[0]["status"] == "completed"
    assert out[0]["source"] == "postgres"
    assert out[0]["source_detail"] == "workspace_runs"
    # No token / cost column exists on workspace_runs — honest ``None``, not 0.
    assert out[0]["total_tokens"] is None
    assert out[0]["total_cost_usd"] is None

    _kind, sql, args = captured[0]
    assert "workspace_runs" in sql
    assert "tenant_id IS NOT DISTINCT FROM" in sql
    assert args == ("tenant-a", 5)


async def test_recent_runs_is_tenant_scoped():
    captured: list[tuple[str, str, tuple]] = []
    pool = _pool(captured=captured, fetch_result=[])

    await PostgresMetricsSource(pool=pool).recent_runs("team-x", limit=3)

    # The workspace query is the first read and carries the tenant predicate.
    assert captured[0][2][0] == "team-x"
