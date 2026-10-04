"""INC46 T36 — 指标聚合、学习曲线、版本对照与 **R8 门禁**（基线未回退）。

任务书 T36 §规格
================
* **学习有效性**：同类任务第 n 次 vs 第 1 次的 ``first_pass_success`` 与成本曲线；
  ``v1.1 vs v1.0`` 对照。
* **门禁**：核心指标较上一基线**回退超阈值**（初始默认 **3pp**）⇒ 经 T15 联锁阻断
  发布（即联锁 **R8**）。

诚实纪律
--------
* 任何指标未测量（``None``）⇒ 门禁对该项**不下结论**（既不算回退、也不算通过）；
* 门禁只比较**同一口径**的量（比率对比率、成本对成本）；分母不同不硬比。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from forgeflow.metrics.definitions import (
    ADOPTION_RATE,
    FAILURE_REPORT_RATE,
    FIRST_PASS_SUCCESS,
    ROLLBACK_RATE,
    SELF_REPAIR_RATE,
    SKILL_REUSE_RATE,
    TaskRecord,
    compute_all,
    first_pass_success,
)

__all__ = [
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
]

#: 初始默认回退阈值（§十：3pp；T36 基线方差校准后再定）。
REGRESSION_THRESHOLD_PP: float = 3.0

#: 越大越好的指标（回退 = 下降超阈值）。
HIGHER_IS_BETTER: frozenset[str] = frozenset(
    {FIRST_PASS_SUCCESS, ADOPTION_RATE, SELF_REPAIR_RATE, SKILL_REUSE_RATE}
)

#: 越小越好的指标（回退 = 上升超阈值）。
LOWER_IS_BETTER: frozenset[str] = frozenset(
    {
        ROLLBACK_RATE,
        FAILURE_REPORT_RATE,
        "cost_per_task.tokens_per_task",
        "cost_per_task.seconds_per_task",
        "latency_p50",
        "latency_p95",
    }
)

#: R8 门禁盯住的「核心指标」。
CORE_METRICS: tuple[str, ...] = (
    FIRST_PASS_SUCCESS,
    ADOPTION_RATE,
    SELF_REPAIR_RATE,
    SKILL_REUSE_RATE,
    ROLLBACK_RATE,
    FAILURE_REPORT_RATE,
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class MetricSnapshot:
    """一次指标快照（写入 ``metric_snapshots``；未测量列一律 ``None``）。"""

    tenant_id: str = ""
    window_hours: float | None = None
    total_tasks: int = 0
    labeled_runs: int = 0
    metrics: dict[str, float | None] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=_now)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "window_hours": self.window_hours,
            "total_tasks": self.total_tasks,
            "labeled_runs": self.labeled_runs,
            "metrics": dict(self.metrics),
            "generated_at": self.generated_at.isoformat()
            if isinstance(self.generated_at, datetime)
            else str(self.generated_at),
        }


def aggregate(
    records: Sequence[TaskRecord],
    *,
    tenant_id: str = "",
    window_hours: float | None = None,
    generated_at: datetime | None = None,
) -> MetricSnapshot:
    """把一批 :class:`TaskRecord` 聚合成一个 :class:`MetricSnapshot`。

    空输入（0 个有标签 run）⇒ 全部指标为 ``None``（**不是 0**，红线 4）。
    """
    records = list(records)
    from forgeflow.outcomes.signals import counts_toward_rate

    labeled = sum(1 for r in records if counts_toward_rate(r.outcome_label))
    snapshot = MetricSnapshot(
        tenant_id=str(tenant_id or ""),
        window_hours=None if window_hours is None else float(window_hours),
        total_tasks=len(records),
        labeled_runs=labeled,
        metrics=compute_all(records),
    )
    if generated_at is not None:
        snapshot.generated_at = generated_at
    return snapshot


def learning_curve(records: Sequence[TaskRecord]) -> dict[str, list[dict[str, Any]]]:
    """按 ``kind`` 分组的学习曲线（累积口径，第 n 次 vs 第 1 次）。

    每个 ``kind`` 产出按 ``sequence`` 升序的采样点；第 i 个点用**前 i 个任务**
    （累积）计算 ``first_pass_success`` 与 ``tokens_per_task`` —— 这正是「同类任务
    第 n 次 vs 第 1 次」的可比口径。``kind`` 为空或 ``sequence<=0`` 的记录不参与。
    """
    grouped: dict[str, list[TaskRecord]] = {}
    for r in records:
        if r.kind and r.sequence > 0:
            grouped.setdefault(r.kind, []).append(r)

    curves: dict[str, list[dict[str, Any]]] = {}
    for kind, items in grouped.items():
        items = sorted(items, key=lambda r: r.sequence)
        points: list[dict[str, Any]] = []
        for i in range(1, len(items) + 1):
            window = items[:i]
            tokens = [float(r.tokens) for r in window if r.tokens is not None]
            points.append(
                {
                    "sequence": i,
                    "n_tasks": i,
                    "first_pass_success": first_pass_success(window),
                    "tokens_per_task": (sum(tokens) / len(tokens)) if tokens else None,
                }
            )
        curves[kind] = points
    return curves


def version_comparison(
    treatment: Sequence[TaskRecord], control: Sequence[TaskRecord]
) -> dict[str, Any]:
    """``v1.1 vs v1.0`` 对照（两组的全部指标 + 差值；差值仅在双测到时有值）。"""
    t = compute_all(list(treatment))
    c = compute_all(list(control))
    deltas: dict[str, float | None] = {}
    for key in set(t) | set(c):
        tv, cv = t.get(key), c.get(key)
        deltas[key] = (tv - cv) if (tv is not None and cv is not None) else None
    return {"treatment": t, "control": c, "delta": deltas}


@dataclass(frozen=True)
class MetricDelta:
    """单个核心指标的回退判定。"""

    metric: str
    baseline: float | None
    current: float | None
    delta: float | None
    regressed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "baseline": self.baseline,
            "current": self.current,
            "delta": self.delta,
            "regressed": self.regressed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GateVerdict:
    """R8 门禁结论：是否有核心指标回退超阈值（阻断发布）。"""

    blocked: bool
    threshold_pp: float
    deltas: tuple[MetricDelta, ...] = ()
    unjudged: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "threshold_pp": self.threshold_pp,
            "deltas": [d.to_dict() for d in self.deltas],
            "unjudged": list(self.unjudged),
        }


def regression_gate(
    current: dict[str, float | None],
    baseline: dict[str, float | None],
    *,
    threshold_pp: float = REGRESSION_THRESHOLD_PP,
    metrics: Iterable[str] = CORE_METRICS,
) -> GateVerdict:
    """R8：把 ``current`` 与**上一基线** ``baseline`` 逐项比较，回退超阈值即阻断。

    * 比率类指标按 **pp**（百分点）比较：``threshold_pp = 3.0`` ⇒ 3 个百分点；
    * 未测量（任一侧 ``None``）⇒ 该项进 ``unjudged``，**不下结论**（既不阻断也不放行）；
    * 只对 :data:`CORE_METRICS`（默认）判定；方向由 :data:`HIGHER_IS_BETTER` /
      :data:`LOWER_IS_BETTER` 决定。
    """
    deltas: list[MetricDelta] = []
    unjudged: list[str] = []
    blocked = False
    for metric in metrics:
        cur = current.get(metric)
        base = baseline.get(metric)
        if cur is None or base is None:
            unjudged.append(metric)
            continue
        delta = float(cur) - float(base)
        threshold = threshold_pp / 100.0
        if metric in HIGHER_IS_BETTER:
            regressed = delta < -threshold
            why = f"下降 {abs(delta) * 100:.2f}pp > 阈值 {threshold_pp}pp"
        elif metric in LOWER_IS_BETTER:
            regressed = delta > threshold
            why = f"上升 {delta * 100:.2f}pp > 阈值 {threshold_pp}pp"
        else:
            unjudged.append(metric)
            continue
        if regressed:
            blocked = True
        deltas.append(
            MetricDelta(
                metric=metric,
                baseline=float(base),
                current=float(cur),
                delta=delta,
                regressed=regressed,
                reason=why if regressed else "未回退",
            )
        )
    return GateVerdict(
        blocked=blocked,
        threshold_pp=threshold_pp,
        deltas=tuple(deltas),
        unjudged=tuple(unjudged),
    )
