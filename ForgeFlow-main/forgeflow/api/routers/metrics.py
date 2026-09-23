"""Metrics routes — observability data for the Streamlit dashboard + Prometheus.

Backend contract (INC2-06 / D2)
-------------------------------
``/metrics/*`` must answer under **both** backends — "双后端不可破". The
aggregations that have a hub-run-store equivalent read through the
``MetricsSource`` abstraction (``observability.metrics_source``): the memory
profile aggregates the live in-process run store, PostgreSQL keeps its
historical ``run_metrics`` / ``workflow_runs`` behaviour.

The legacy cost / evaluation / prometheus views have **no** hub-run-store
equivalent (the memory profile keeps no ``run_metrics`` ledger). For those the
router must still not hard-depend on a PostgreSQL pool: the offline profile
returns an honest empty payload — no data / no billable cost, never a fabricated
value — instead of a ``503``. The PostgreSQL profile keeps its exact SQL,
ordering and (missing-pool ⇒ 503) behaviour unchanged.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from forgeflow.api.dependencies import get_workspace_id
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.schemas import (
    EvaluationSummaryResponse,
    MetricsSummaryResponse,
    SloSummaryResponse,
)
from forgeflow.config import get_settings
from forgeflow.observability.metrics_source import get_metrics_source
from forgeflow.observability.metrics_store import MetricsStore
from forgeflow.observability.prometheus import refresh_from_db, render

router = APIRouter()


async def _optional_pool(request: Request) -> asyncpg.Pool | None:
    """Return the app's PostgreSQL pool when initialised, else ``None``.

    Unlike :func:`api.dependencies.get_pool` this never raises, so the read
    endpoints can serve the PostgreSQL-free ``memory`` profile (the D2 ruling)
    instead of short-circuiting to a 503.
    """
    return getattr(request.app.state, "pool", None)


def _is_offline() -> bool:
    """True for the offline (non-PostgreSQL) backend profile."""
    return get_settings().storage_backend.lower() != "postgres"


def _pool_or_offline(pool: asyncpg.Pool | None) -> asyncpg.Pool | None:
    """Enforce the per-backend pool contract for the legacy views.

    Returns ``None`` for the offline profile — the caller must then emit an
    honest empty payload and never touch PostgreSQL. For the PostgreSQL profile
    it returns the pool, and a *missing* pool stays a genuine misconfiguration:
    we keep the historical ``503`` ("Database pool not initialised") so the
    PostgreSQL behaviour is unchanged.
    """
    if _is_offline():
        return None
    if pool is None:
        raise HTTPException(status_code=503, detail="Database pool not initialised")
    return pool


@router.get("/prometheus", include_in_schema=False)
async def prometheus_metrics(
    request: Request,
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """Prometheus scrape endpoint. Returns text/plain in the standard format.

    The registry is held on app.state.prom_registry and was built at startup
    in forgeflow/api/main.py. We refresh DB-backed series on every scrape — but
    only when a PostgreSQL pool is available; the offline profile renders the
    in-registry series directly instead of 503-ing on the pool dependency.
    """
    registry = getattr(request.app.state, "prom_registry", None)
    metrics_collectors = getattr(request.app.state, "prom_metrics", None)
    if registry is None or metrics_collectors is None:
        return Response(
            content="# prometheus registry not initialised\n",
            media_type="text/plain",
            status_code=503,
        )

    active_pool = _pool_or_offline(pool)
    if active_pool is not None:
        await refresh_from_db(active_pool, metrics_collectors)
    body, content_type = render(registry)
    return Response(content=body, media_type=content_type)


@router.get("/", response_model=MetricsSummaryResponse)
async def get_metrics_summary(tenant: str = Depends(resolve_tenant)):
    """Aggregated KPIs for the home dashboard — read via ``MetricsSource``.

    The backend decides where the numbers come from (INC2-06/D2): the memory
    profile aggregates the live hub run store, PostgreSQL keeps its historical
    ``run_metrics``/``workflow_runs`` behaviour. The router holds no backend
    branching of its own.
    """
    payload = await get_metrics_source().summary(tenant)
    return MetricsSummaryResponse(**payload)


@router.get("/cost")
async def get_cost_breakdown(
    days: int = Query(7, ge=1, le=90),
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """Cost breakdown by agent and day — used by the cost analysis page.

    The offline profile keeps no cost ledger, so it returns an empty breakdown
    (``[]`` — "no data") rather than dialing PostgreSQL.
    """
    active_pool = _pool_or_offline(pool)
    if active_pool is None:
        return []
    store = MetricsStore(active_pool)
    return await store.get_cost_by_agent(days=days)


@router.get("/cost/by_workflow_type")
async def get_cost_by_workflow_type(
    days: int = Query(7, ge=1, le=90),
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """Cost + token breakdown by workflow_type — for multi-domain comparisons."""
    active_pool = _pool_or_offline(pool)
    if active_pool is None:
        return []
    store = MetricsStore(active_pool)
    return await store.get_cost_by_workflow_type(days=days)


@router.get("/cost/top_runs")
async def get_top_expensive_runs(
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(10, ge=1, le=50),
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """Top N highest-cost workflow runs in the window — drill-down list."""
    active_pool = _pool_or_offline(pool)
    if active_pool is None:
        return []
    store = MetricsStore(active_pool)
    return await store.get_top_expensive_runs(limit=limit, days=days)


@router.get("/cost/alerts")
async def get_budget_alerts(
    days: int = Query(7, ge=1, le=90),
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """Workflow runs that hit the budget warning (>=90%) or limit (>=100%).

    Threshold is read from settings.budget_limit_usd at call time.
    """
    active_pool = _pool_or_offline(pool)
    if active_pool is None:
        return []
    settings = get_settings()
    store = MetricsStore(active_pool)
    return await store.get_budget_alerts(
        budget_limit_usd=settings.budget_limit_usd, days=days
    )


@router.get("/evaluation", response_model=EvaluationSummaryResponse)
async def get_evaluation_summary(
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """LLM-as-judge evaluation score aggregates.

    The offline profile has no ``run_metrics`` table, so it returns the honest
    all-zero summary (``sample_count=0``) rather than 503-ing on the pool
    dependency.
    """
    active_pool = _pool_or_offline(pool)
    if active_pool is not None:
        try:
            async with active_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT
                        COALESCE(AVG(CASE WHEN metric_name='faithfulness' THEN metric_value END), 0) AS avg_faithfulness,
                        COALESCE(AVG(CASE WHEN metric_name='relevance' THEN metric_value END), 0) AS avg_relevance,
                        COALESCE(AVG(CASE WHEN metric_name='coherence' THEN metric_value END), 0) AS avg_coherence,
                        COALESCE(AVG(CASE WHEN metric_name='hallucination' THEN metric_value END), 0) AS hallucination_rate,
                        COUNT(DISTINCT run_id) AS sample_count
                    FROM run_metrics
                    WHERE metric_name IN ('faithfulness', 'relevance', 'coherence', 'hallucination')
                    """
                )
            if row:
                return EvaluationSummaryResponse(
                    avg_faithfulness=float(row["avg_faithfulness"]),
                    avg_relevance=float(row["avg_relevance"]),
                    avg_coherence=float(row["avg_coherence"]),
                    hallucination_rate=float(row["hallucination_rate"]),
                    sample_count=int(row["sample_count"]),
                )
        except Exception:
            pass

    return EvaluationSummaryResponse(
        avg_faithfulness=0.0,
        avg_relevance=0.0,
        avg_coherence=0.0,
        hallucination_rate=0.0,
        sample_count=0,
    )


@router.get("/runs")
async def list_recent_runs(
    limit: int = Query(20, ge=1, le=100),
    tenant: str = Depends(resolve_tenant),
    workspace_id: str | None = Depends(get_workspace_id),
):
    """Recent runs for the dashboard table — read via ``MetricsSource``.

    Previously this always dialed PostgreSQL (``Depends(get_pool)``), which
    meant the memory profile had an empty runs table. It now goes through the
    same factory as ``/``; the PostgreSQL branch keeps its exact SQL and
    workspace scoping.
    """
    return await get_metrics_source().recent_runs(
        tenant, limit, workspace_id=workspace_id
    )


@router.get("/slo", response_model=SloSummaryResponse)
async def get_slo_summary(tenant: str = Depends(resolve_tenant)):
    """Three-tier SLO attainment (INC2-07) — platform / task / edge.

    Targets are the frozen values from architecture §7.2 (99.5 / 99.0 / 97.0
    and p95 1500 / 2000 / 5000 ms), overridable via the ``SLO_*`` settings.
    """
    from forgeflow.observability.slo import SloRegistry

    registry = SloRegistry(tenant_id=tenant)
    return SloSummaryResponse(**await registry.summary())
