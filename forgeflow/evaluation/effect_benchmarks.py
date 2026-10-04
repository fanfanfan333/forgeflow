"""INC46 T36 — 发布联锁 **R8** 的锚点模块（``关键指标较基线未回退``）。

本模块是 ``forgeflow/skills/publish_interlock.py`` 中 R8 的**能力锚点**
（``CapabilityAnchor("R8", "T36", "forgeflow.evaluation.effect_benchmarks", "关键指标较基线未回退")``）。
它把 T36 的效果度量能力在此处 re-export，作为「该能力已落地」的**稳定 import 目标**：

* :mod:`forgeflow.metrics.definitions` —— 9 项纯函数指标（**未测量 ⇒ None**）；
* :mod:`forgeflow.metrics.aggregator`  —— 聚合 / 学习曲线 / 版本对照 / **R8 门禁**
  （``regression_gate``：核心指标较上一基线回退超 **3pp** ⇒ ``blocked``）；
* :mod:`forgeflow.metrics.store`       —— ``metric_snapshots`` / ``benchmark_runs`` 双后端；
* :mod:`forgeflow.benchmark.runner`    —— 冻结语料（≥ 50 例）端到端基准参考执行器。

为何**刻意不提供** ``INTERLOCK_PROBE``（fail-closed）
=====================================================
联锁探针的语义是「该能力**真实可用** ⇒ 报 ``ok=True``，从而参与解锁」。R8 属
**Level-2（自动发布）** 的八项之一；当前 R2–R8 均未满足，联锁整体**必须保持锁死**。

若本模块此刻导出一个自报 ``ok=True`` 的探针，会立即把 R8 翻成 ``met`` ——
这会**提前**改变联锁快照（``missing`` 从 ``[R2..R8]`` 变成 ``[R2..R7]``），
打红 T13/T15 的独立探针用例与 T15/publish_interlock 的时点快照，而**收益为零**
（Level-2 仍需 R1–R8 全满足，R8 单独变 met 不解锁任何一级）。

与既有处置**一致**：T32（R5 ``forgeflow.experience.redaction``）、T33
（R6 ``forgeflow.security.quarantine``）、T34（R7 ``forgeflow.skills.auto_rollback``）
同样**只落地能力模块、不导出探针**，R5/R6/R7 至今如实为**未满足**。T36 沿用同一
纪律：**落地能力，保持 fail-closed**。

后续（探针翻转的裁决）
----------------------
探针的**翻转**须与快照解耦一并裁决（见 ``publish_interlock`` 侧登记的 TODO：把
时点快照改为**定向构造**）。届时若引入探针，必须像 R4 那样**以真实证据为闸**
（如：确有已落库的 ``benchmark_runs`` 且 ``corpus_hash`` 与冻结值一致），而非恒真自检
—— 否则就是把「模块存在」冒充「能力可用」。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from forgeflow.benchmark.runner import (
    CORPUS_PATH,
    FROZEN_CORPUS_SHA256,
    BenchmarkReport,
    CorpusIntegrityError,
    CaseResult,
    corpus_stats,
    derive_verdict,
    load_corpus,
    run_benchmark,
)
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
    ALL_METRICS,
    TaskRecord,
    compute_all,
    first_pass_success,
)
from forgeflow.metrics.store import (
    BenchmarkRunRecord,
    InMemoryMetricsStore,
    MetricSnapshotRecord,
    MetricsStore,
    PostgresMetricsStore,
    get_metrics_store,
    reset_metrics_store,
    set_metrics_store,
)

__all__ = [
    # metrics — definitions
    "ALL_METRICS",
    "TaskRecord",
    "compute_all",
    "first_pass_success",
    # metrics — aggregation + R8 gate
    "CORE_METRICS",
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "REGRESSION_THRESHOLD_PP",
    "GateVerdict",
    "MetricDelta",
    "MetricSnapshot",
    "aggregate",
    "learning_curve",
    "regression_gate",
    "version_comparison",
    # metrics — persistence
    "BenchmarkRunRecord",
    "InMemoryMetricsStore",
    "MetricSnapshotRecord",
    "MetricsStore",
    "PostgresMetricsStore",
    "get_metrics_store",
    "reset_metrics_store",
    "set_metrics_store",
    # benchmark
    "CORPUS_PATH",
    "FROZEN_CORPUS_SHA256",
    "BenchmarkReport",
    "CaseResult",
    "CorpusIntegrityError",
    "corpus_stats",
    "derive_verdict",
    "load_corpus",
    "run_benchmark",
    # convenience
    "evaluate_effect",
]

# NOTE: ``INTERLOCK_PROBE`` is deliberately NOT defined here — see the module
# docstring ("为何刻意不提供 INTERLOCK_PROBE"). Adding it would flip R8 to met
# and prematurely change the publish-interlock snapshot.


def evaluate_effect(
    records: Sequence[TaskRecord],
    baseline: Mapping[str, Any] | None = None,
    *,
    tenant_id: str = "",
    window_hours: float | None = None,
    threshold_pp: float = REGRESSION_THRESHOLD_PP,
) -> dict[str, Any]:
    """T36 效果评估一次性入口：聚合当前快照，可选与上一基线做 **R8 回退门禁**。

    返回 ``{"snapshot": MetricSnapshot, "gate": GateVerdict | None}``。
    ``baseline`` 为 ``None``（无上一基线）时 ``gate`` 也是 ``None`` —— 只有一次测量
    时**不下回退结论**（既不放行也不阻断），这是「无基线不得宣称达标」的落地。
    """
    snapshot = aggregate(
        records, tenant_id=tenant_id, window_hours=window_hours
    )
    gate: GateVerdict | None = None
    if baseline is not None:
        base_metrics = baseline.get("metrics") if "metrics" in baseline else baseline
        gate = regression_gate(
            snapshot.metrics, dict(base_metrics or {}), threshold_pp=threshold_pp
        )
    return {"snapshot": snapshot, "gate": gate}
