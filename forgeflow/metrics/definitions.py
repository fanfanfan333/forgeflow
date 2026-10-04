"""INC46 T36 — 效果指标定义（纯函数；**未测量 ⇒ None**，绝不写 0）。

任务书 T36 §规格（指标，均遵循「未测量 ⇒ None」）
================================================
* ``first_pass_success``  —— ``ACCEPTED_EXPLICIT`` 且**无** ``REVISED`` / ``REVERTED``
  ÷ **有标签** run；
* ``adoption_rate``       —— ``approve ÷ (approve + reject)``；
* ``skill_reuse_rate``    —— 复用了既有 skill 的 run 占比；
* ``self_repair_rate``    —— T24「修复后 pass ÷ 触发修复」；
* ``clarification_rate``  —— 触发澄清的 run 占比（T20）；
* ``failure_report_rate`` —— 产生失败上报的 run 占比；
* ``rollback_rate``       —— 回滚占比（T34）；
* ``cost_per_task``       —— token / 秒（两列各自独立测量）；
* ``latency_p50`` / ``latency_p95`` —— 延迟分位。

诚实纪律（红线 4 / 12）
-----------------------
* **分母为 0 ⇒ ``None``**（无样本 ≠ 0）；``UNKNOWN`` 标签**不进**成功率分母；
* 每一列**独立**测量：某一列没测到 ⇒ 该列 ``None``，**不牵连**其它列；
* 本模块**纯函数、无 I/O、无时钟** —— 便于单独断言与反事实变异。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from forgeflow.outcomes.signals import (
    ACCEPTED_EXPLICIT,
    REVERTED,
    REVISED,
    UNKNOWN,
    counts_toward_rate,
    success_rate,
)
from forgeflow.rollout.metrics import percentile

__all__ = [
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
]

FIRST_PASS_SUCCESS = "first_pass_success"
ADOPTION_RATE = "adoption_rate"
SKILL_REUSE_RATE = "skill_reuse_rate"
SELF_REPAIR_RATE = "self_repair_rate"
CLARIFICATION_RATE = "clarification_rate"
FAILURE_REPORT_RATE = "failure_report_rate"
ROLLBACK_RATE = "rollback_rate"
COST_PER_TASK = "cost_per_task"
LATENCY_P50 = "latency_p50"
LATENCY_P95 = "latency_p95"

#: 全部可计算指标（顺序稳定，便于快照对比）。
ALL_METRICS: tuple[str, ...] = (
    FIRST_PASS_SUCCESS,
    ADOPTION_RATE,
    SKILL_REUSE_RATE,
    SELF_REPAIR_RATE,
    CLARIFICATION_RATE,
    FAILURE_REPORT_RATE,
    ROLLBACK_RATE,
    COST_PER_TASK,
    LATENCY_P50,
    LATENCY_P95,
)


@dataclass
class TaskRecord:
    """一次任务执行的**观测**（T36 指标的原子输入）。

    每个字段都是「这一列是否测到」的显式表达：``None`` = 未测量，绝不猜。
    ``outcome_label`` 遵循 T16 八标签；``UNKNOWN`` / ``None`` 不进成功率分母。
    """

    task_id: str = ""
    #: T16 标签（``UNKNOWN`` / ``None`` = 未标注）。
    outcome_label: str | None = None
    #: 该任务是否发生过返工（``REVISED`` / ``REVERTED`` 反馈）。``True`` 使
    #: ``first_pass_success`` 的分子不成立（首过失败）。
    rework: bool = False
    #: 采纳决策（``True`` approve / ``False`` reject / ``None`` 未决策）。
    approved: bool | None = None
    #: 是否复用既有 skill（``None`` 未记录）。
    reused_skill: bool | None = None
    #: T24 修复循环：是否触发修复 / 修复后是否通过（``None`` 未记录）。
    repair_triggered: bool | None = None
    repair_passed: bool | None = None
    #: T20 是否触发澄清（``None`` 未记录）。
    clarified: bool | None = None
    #: 是否产生失败上报（``None`` 未记录）。
    failure_reported: bool | None = None
    #: 本次运行所属版本/回滚（``None`` 未记录）。
    rolled_back: bool | None = None
    #: 成本（token / 秒）与延迟（毫秒）—— 逐列独立测量。
    tokens: float | None = None
    seconds: float | None = None
    latency_ms: float | None = None
    #: 任务类别（学习曲线的分组键；空 ⇒ 不参与曲线）。
    kind: str = ""
    #: 该任务在同类中的序号（从 1 起；``0`` = 未记录）。
    sequence: int = 0
    #: 版本标识（v1.0 / v1.1 对照；空 ⇒ 不参与对照）。
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "outcome_label": self.outcome_label,
            "rework": self.rework,
            "approved": self.approved,
            "reused_skill": self.reused_skill,
            "repair_triggered": self.repair_triggered,
            "repair_passed": self.repair_passed,
            "clarified": self.clarified,
            "failure_reported": self.failure_reported,
            "rolled_back": self.rolled_back,
            "tokens": self.tokens,
            "seconds": self.seconds,
            "latency_ms": self.latency_ms,
            "kind": self.kind,
            "sequence": self.sequence,
            "version": self.version,
        }


def rate(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator``；**分母 ≤ 0 ⇒ None**（不写 0，红线 4）。"""
    if denominator <= 0:
        return None
    return numerator / denominator


def _known(values: Iterable[Any]) -> list[Any]:
    """非 ``None`` 的取值（``None`` = 未记录，不进任何分母）。"""
    return [v for v in values if v is not None]


def _labeled(records: Sequence[TaskRecord]) -> list[TaskRecord]:
    """有标签（非 ``UNKNOWN`` / 非 ``None``）的记录 —— 成功率的分母口径（红线 12）。"""
    return [r for r in records if counts_toward_rate(r.outcome_label)]


def first_pass_success(records: Sequence[TaskRecord]) -> float | None:
    """首过成功率 = (``ACCEPTED_EXPLICIT`` 且无返工) ÷ 有标签 run。"""
    labeled = _labeled(records)
    if not labeled:
        return None
    good = sum(
        1
        for r in labeled
        if r.outcome_label == ACCEPTED_EXPLICIT and not r.rework
    )
    return rate(good, len(labeled))


def adoption_rate(records: Sequence[TaskRecord]) -> float | None:
    """采纳率 = ``approve ÷ (approve + reject)``；未决策（``None``）不进分母。"""
    decisions = _known([r.approved for r in records])
    if not decisions:
        return None
    return rate(sum(1 for d in decisions if d), len(decisions))


def skill_reuse_rate(records: Sequence[TaskRecord]) -> float | None:
    """复用率 = 复用既有 skill 的 run ÷ 有记录的 run。"""
    known = _known([r.reused_skill for r in records])
    if not known:
        return None
    return rate(sum(1 for v in known if v), len(known))


def self_repair_rate(records: Sequence[TaskRecord]) -> float | None:
    """自修复率 = 修复后 pass ÷ **触发修复**的 run（未触发 ⇒ 分母 0 ⇒ None）。"""
    triggered = [r for r in records if r.repair_triggered is True]
    if not triggered:
        return None
    passed = sum(1 for r in triggered if r.repair_passed is True)
    return rate(passed, len(triggered))


def clarification_rate(records: Sequence[TaskRecord]) -> float | None:
    """澄清率 = 触发澄清的 run ÷ 有记录的 run。"""
    known = _known([r.clarified for r in records])
    if not known:
        return None
    return rate(sum(1 for v in known if v), len(known))


def failure_report_rate(records: Sequence[TaskRecord]) -> float | None:
    """失败上报率 = 有失败上报的 run ÷ 有记录的 run。"""
    known = _known([r.failure_reported for r in records])
    if not known:
        return None
    return rate(sum(1 for v in known if v), len(known))


def rollback_rate(records: Sequence[TaskRecord]) -> float | None:
    """回滚率 = 回滚的 run ÷ 有记录的 run（T34）。"""
    known = _known([r.rolled_back for r in records])
    if not known:
        return None
    return rate(sum(1 for v in known if v), len(known))


def cost_per_task(records: Sequence[TaskRecord]) -> dict[str, float | None]:
    """每任务成本（token / 秒）—— 两列**独立**测量，各自可为 ``None``。

    返回 ``{"tokens_per_task": …, "seconds_per_task": …}``；某列无样本 ⇒ 该列 ``None``。
    """
    tokens = [float(r.tokens) for r in records if r.tokens is not None]
    seconds = [float(r.seconds) for r in records if r.seconds is not None]
    return {
        "tokens_per_task": (sum(tokens) / len(tokens)) if tokens else None,
        "seconds_per_task": (sum(seconds) / len(seconds)) if seconds else None,
    }


def latency_p50(records: Sequence[TaskRecord]) -> float | None:
    """延迟 P50（最近秩分位）；无样本 ⇒ ``None``。"""
    xs = [float(r.latency_ms) for r in records if r.latency_ms is not None]
    return percentile(xs, 0.5) if xs else None


def latency_p95(records: Sequence[TaskRecord]) -> float | None:
    """延迟 P95（最近秩分位）；无样本 ⇒ ``None``。"""
    xs = [float(r.latency_ms) for r in records if r.latency_ms is not None]
    return percentile(xs, 0.95) if xs else None


def compute_all(records: Sequence[TaskRecord]) -> dict[str, float | None]:
    """把全部指标算成 ``{name: float|None}``（``cost_per_task`` 拉平成两列）。"""
    cost = cost_per_task(records)
    return {
        FIRST_PASS_SUCCESS: first_pass_success(records),
        ADOPTION_RATE: adoption_rate(records),
        SKILL_REUSE_RATE: skill_reuse_rate(records),
        SELF_REPAIR_RATE: self_repair_rate(records),
        CLARIFICATION_RATE: clarification_rate(records),
        FAILURE_REPORT_RATE: failure_report_rate(records),
        ROLLBACK_RATE: rollback_rate(records),
        f"{COST_PER_TASK}.tokens_per_task": cost["tokens_per_task"],
        f"{COST_PER_TASK}.seconds_per_task": cost["seconds_per_task"],
        LATENCY_P50: latency_p50(records),
        LATENCY_P95: latency_p95(records),
    }
