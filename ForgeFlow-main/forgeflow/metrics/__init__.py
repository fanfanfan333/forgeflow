"""INC46 T36 — 效果指标（Metrics & Benchmark）。

对外入口
========
* :mod:`forgeflow.metrics.definitions` —— 9 项指标的**纯函数**定义（未测量 ⇒ None）；
* :mod:`forgeflow.metrics.aggregator`  —— 快照聚合、学习曲线、版本对照、**R8 门禁**；
* :mod:`forgeflow.metrics.store`       —— 两张表（迁移 034）的双后端持久层。

联锁锚点
========
R8（关键指标较基线未回退）的锚点模块为
:mod:`forgeflow.evaluation.effect_benchmarks`（按需 re-export 本包与 benchmark 包）。
"""

from __future__ import annotations

from forgeflow.metrics.aggregator import (
    CORE_METRICS,
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    REGRESSION_THRESHOLD_PP,
    GateVerdict,
    MetricDelta,
    MetricSnapshot,
    aggregate,
    learning_curve,
    regression_gate,
    version_comparison,
)
from forgeflow.metrics.definitions import (
    ADOPTION_RATE,
    ALL_METRICS,
    CLARIFICATION_RATE,
    COST_PER_TASK,
    FAILURE_REPORT_RATE,
    FIRST_PASS_SUCCESS,
    LATENCY_P50,
    LATENCY_P95,
    ROLLBACK_RATE,
    SELF_REPAIR_RATE,
    SKILL_REUSE_RATE,
    TaskRecord,
    adoption_rate,
    clarification_rate,
    compute_all,
    cost_per_task,
    failure_report_rate,
    first_pass_success,
    latency_p50,
    latency_p95,
    rate,
    rollback_rate,
    self_repair_rate,
    skill_reuse_rate,
)
from forgeflow.metrics.store import (
    BenchmarkRunRecord,
    InMemoryMetricsStore,
    MetricSnapshotRecord,
    MetricsStore,
    MetricsStoreError,
    PostgresMetricsStore,
    get_metrics_store,
    reset_metrics_store,
    set_metrics_store,
)

__all__ = [
    # definitions
    "FIRST_PASS_SUCCESS",
    "ADOPTION_RATE",
    "SKILL_REUSE_RATE",
    "SELF_REPAIR_RATE",
    "CLARIFICATION_RATE",
    "FAILURE_REPORT_RATE",
    "ROLLBACK_RATE",
    "COST_PER_TASK",
    "LATENCY_P50",
    "LATENCY_P95",
    "ALL_METRICS",
    "TaskRecord",
    "rate",
    "first_pass_success",
    "adoption_rate",
    "skill_reuse_rate",
    "self_repair_rate",
    "clarification_rate",
    "failure_report_rate",
    "rollback_rate",
    "cost_per_task",
    "latency_p50",
    "latency_p95",
    "compute_all",
    # aggregator
    "REGRESSION_THRESHOLD_PP",
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "CORE_METRICS",
    "MetricSnapshot",
    "MetricDelta",
    "GateVerdict",
    "aggregate",
    "learning_curve",
    "version_comparison",
    "regression_gate",
    # store
    "MetricsStore",
    "InMemoryMetricsStore",
    "PostgresMetricsStore",
    "MetricSnapshotRecord",
    "BenchmarkRunRecord",
    "MetricsStoreError",
    "get_metrics_store",
    "set_metrics_store",
    "reset_metrics_store",
]
