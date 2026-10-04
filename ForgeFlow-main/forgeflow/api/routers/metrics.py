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
ordering and (missing-pool ⇒ 503) behaviour; the one deliberate change is that a
*query failure* on ``/evaluation`` is now reported explicitly (``degraded`` +
``error``) rather than fail-open-swallowed into an all-zero summary that was
indistinguishable from a genuinely empty window.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from forgeflow.api.dependencies import get_workspace_id
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.schemas import (
    AgentEvalSummaryResponse,
    EvaluationSummaryResponse,
    MetricsSummaryResponse,
    SloSummaryResponse,
)
from forgeflow.config import get_settings
from forgeflow.observability.metrics_source import get_metrics_source
from forgeflow.observability.metrics_store import MetricsStore
from forgeflow.observability.prometheus import refresh_from_db, render

router = APIRouter()

logger = logging.getLogger(__name__)


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


@router.get(
    "/evaluation",
    response_model=EvaluationSummaryResponse,
    response_model_exclude_defaults=True,
)
async def get_evaluation_summary(
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """LLM-as-judge evaluation score aggregates.

    The offline profile has no ``run_metrics`` table, so it returns the honest
    all-zero summary (``sample_count=0``) rather than 503-ing on the pool
    dependency — that is a genuine "no data", **not** a degradation, so it keeps
    ``degraded=False``.

    On the PostgreSQL profile a failed query is reported **explicitly**
    (``degraded=True`` + ``error``) instead of a silent ``except: pass``: the old
    fail-open made "the table is empty" indistinguishable from "the query broke".
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
        except Exception as exc:  # noqa: BLE001 — degrade explicitly, never 500
            logger.warning(
                "evaluation summary: PostgreSQL read unavailable (%s); "
                "returning an explicitly-degraded payload",
                exc,
            )
            return EvaluationSummaryResponse(
                avg_faithfulness=0.0,
                avg_relevance=0.0,
                avg_coherence=0.0,
                hallucination_rate=0.0,
                sample_count=0,
                degraded=True,
                error=str(exc)[:300],
            )

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


@router.get("/agent-eval", response_model=AgentEvalSummaryResponse)
async def get_agent_eval_summary(
    window_days: int = Query(30, ge=1, le=365),
    tenant: str = Depends(resolve_tenant),
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """12 Agent quality/cost/reliability metrics × 3 dimensions (INC8 Phase-B).

    The evaluation snapshot is read through ``AgentEvalSource`` — the memory
    profile aggregates the live hub run store (``source="hub_runs"``,
    ``durable=False``), PostgreSQL reads ``workflow_runs`` + ``agent_eval_samples``
    (``source="postgres"``, ``durable=True``). The two sources are never mixed.

    Each metric is honestly labelled: a ⛔ metric (no ground truth) returns
    ``value=null`` — never a fabricated ``0``. A failed PostgreSQL read is
    reported explicitly (``degraded=True`` + ``error``) rather than swallowed.

    RBAC: this path is covered by the existing ``("GET","/metrics")`` longest-
    prefix rule → ``(read, metrics)``; no new route entry is added.
    """
    from forgeflow.evaluation.eval_source import get_agent_eval_source

    active_pool = _pool_or_offline(pool)
    source = get_agent_eval_source(pool=active_pool)
    try:
        payload = await source.summary(tenant, window_days=window_days)
    except Exception as exc:  # noqa: BLE001 — degrade explicitly, never 500
        logger.warning("agent-eval summary: source read failed (%s)", exc)
        return AgentEvalSummaryResponse(
            source="hub_runs" if _is_offline() else "postgres",
            durable=not _is_offline(),
            window_days=window_days,
            degraded=True,
            error=str(exc)[:300],
        )
    return AgentEvalSummaryResponse(**payload)


@router.get("/agent-eval/samples")
async def list_agent_eval_samples(
    metric: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    tenant: str = Depends(resolve_tenant),
    pool: asyncpg.Pool | None = Depends(_optional_pool),
):
    """Persisted eval samples (newest first), optionally filtered to one metric.

    Drill-down for ``/metrics/agent-eval`` — the raw ``agent_eval_samples`` rows
    behind the groundedness / hallucination aggregates.
    """
    from forgeflow.evaluation.eval_source import get_agent_eval_source

    active_pool = _pool_or_offline(pool)
    source = get_agent_eval_source(pool=active_pool)
    try:
        return await source.samples(tenant, metric=metric, limit=limit)
    except Exception as exc:  # noqa: BLE001 — degrade to an honest empty list
        logger.warning("agent-eval samples: source read failed (%s)", exc)
        return []


# --------------------------------------------------------------------------- #
# INC46 T36 — effect metrics (outcomes) & end-to-end benchmark                 #
# --------------------------------------------------------------------------- #
def _run_latency_ms(created_at: Any, completed_at: Any) -> float | None:
    """Run wall-clock latency in ms, or ``None`` when unmeasured.

    Both timestamps are always-UTC ISO-8601 strings from the run store; a missing
    / unparsable pair means the duration was **not measured** ⇒ ``None`` (红线 4,
    never a fabricated 0).
    """
    if not isinstance(created_at, str) or not isinstance(completed_at, str):
        return None
    try:
        start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta_ms = (end - start).total_seconds() * 1000.0
    return delta_ms if delta_ms >= 0 else None


@router.get("/outcomes")
async def get_outcome_metrics(
    limit: int = Query(200, ge=1, le=2000),
    tenant: str = Depends(resolve_tenant),
) -> dict:
    """T36 效果指标（``first_pass_success`` / ``adoption_rate`` / … / P50 / P95）。

    数据源：最近 ``limit`` 条 run（``MetricsSource.recent_runs``）× 每条的 T16
    标签（``outcome_store``）与返工反馈（``REVISED`` / ``REVERTED``）。
    **每一列独立测量**：未测到的列一律 ``null``，无样本时所有指标均为 ``null``
    —— 绝不写 0（红线 4）；``UNKNOWN`` 标签不进成功率分母（红线 12）。
    ``has_data`` 直接由「有标签 run 数」决定，前端据此渲染「—」。

    RBAC：本路径被既有 ``("GET","/metrics")`` 最长前缀规则覆盖 → ``(read, metrics)``；
    不新增路由条目（与 ``/agent-eval`` 同一处置）。
    """
    from forgeflow.metrics.aggregator import REGRESSION_THRESHOLD_PP, aggregate
    from forgeflow.metrics.definitions import TaskRecord
    from forgeflow.outcomes.labeler import REWORK_KINDS
    from forgeflow.outcomes.store import get_outcome_store

    runs = await get_metrics_source().recent_runs(tenant, limit)
    outcome_store = get_outcome_store()

    records: list[TaskRecord] = []
    for run in runs:
        run_id = str(run.get("run_id") or "")
        if not run_id:
            continue
        row = outcome_store.get_outcome(tenant, run_id) or {}
        kinds = {e.get("kind") for e in outcome_store.list_feedback(tenant, run_id)}
        raw_tokens = run.get("total_tokens")
        # The run source cannot tell "0 tokens" from "not measured" ⇒ 0 is
        # treated as unmeasured so the cost column stays honest (红线 4).
        tokens = (
            float(raw_tokens)
            if isinstance(raw_tokens, (int, float)) and raw_tokens > 0
            else None
        )
        records.append(
            TaskRecord(
                task_id=run_id,
                outcome_label=row.get("outcome_label"),
                rework=bool(kinds & set(REWORK_KINDS)),
                tokens=tokens,
                latency_ms=_run_latency_ms(
                    run.get("created_at"), run.get("completed_at")
                ),
            )
        )

    snapshot = aggregate(records, tenant_id=tenant)
    return {
        "tenant_id": tenant,
        "total_runs": len(records),
        "labeled_runs": snapshot.labeled_runs,
        "has_data": snapshot.labeled_runs > 0,
        "metrics": snapshot.metrics,
        "regression_threshold_pp": REGRESSION_THRESHOLD_PP,
        "generated_at": snapshot.generated_at.isoformat(),
    }


@router.get("/benchmark/latest")
async def get_latest_benchmark(tenant: str = Depends(resolve_tenant)) -> dict:
    """T36 端到端基准：**冻结语料**的实时通过矩阵 + 最近一次已落库的运行。

    ``live`` 每次按冻结语料现算（纯规则、无 LLM、无存储写入，耗时可忽略），所以
    即使在 nightly 尚未写入 ``benchmark_runs`` 时也有可读报告；``latest`` 是
    ``benchmark_runs`` 里该租户最近一行（无 ⇒ ``null``）。语料被改动（哈希不符）
    时 :class:`~forgeflow.benchmark.runner.CorpusIntegrityError` 会让报告
    ``available=False`` 并给出原因 —— 不伪造一份「全绿」。

    RBAC：同为 ``("GET","/metrics")`` 前缀覆盖 → ``(read, metrics)``。
    """
    from forgeflow.benchmark.runner import (
        CORPUS_PATH,
        FROZEN_CORPUS_SHA256,
        CorpusIntegrityError,
        run_benchmark,
    )
    from forgeflow.metrics.store import get_metrics_store

    corpus = {
        "frozen_hash": FROZEN_CORPUS_SHA256,
        "path": str(CORPUS_PATH),
    }
    live: dict = {"available": False}
    try:
        report = run_benchmark()
        corpus["hash"] = report.corpus_hash
        corpus["contract_satisfied"] = report.stats.get("satisfied", False)
        corpus["contract"] = report.stats.get("contract", {})
        payload = report.to_dict()
        live = {
            "available": True,
            "corpus_hash": payload["corpus_hash"],
            "total_cases": payload["total_cases"],
            "passed": payload["passed"],
            "failed": payload["failed"],
            "errors": payload["errors"],
            "skipped": payload["skipped"],
            "by_category": payload["by_category"],
            "generated_at": payload["generated_at"],
        }
    except CorpusIntegrityError as exc:
        logger.warning("benchmark: frozen corpus hash mismatch (%s)", exc)
        live = {"available": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — degrade explicitly, never 500
        logger.warning("benchmark: live run failed (%s)", exc)
        live = {"available": False, "error": str(exc)[:300]}

    try:
        record = get_metrics_store().latest_benchmark_run(tenant)
    except Exception as exc:  # noqa: BLE001 — an unreadable store is "no data"
        logger.warning("benchmark: latest persisted run unavailable (%s)", exc)
        record = None

    return {
        "tenant_id": tenant,
        "corpus": corpus,
        "live": live,
        "latest": None if record is None else record.to_dict(),
        "has_persisted": record is not None,
    }
