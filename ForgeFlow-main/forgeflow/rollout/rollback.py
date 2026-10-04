"""INC46 T34 — 回滚触发与「只追加」回滚（Auto-rollback, append-only）。

触发条件（初始默认，任务书 T34 §规格）
======================================
1. ``success_rate`` **下降 > 5pp** —— 用 **Wilson 置信下界** 与旧版成功率比较
   （非点估计；小样本不得误触发）；
2. ``validate_fail_rate`` **上升 > 3pp**；
3. ``P95`` 延迟 **> 1.5×**；
4. 出现 **DANGEROUS 越权事件** —— **立即**回滚，不等样本。

样本不足（``insufficient``）时**不下结论**：既不回滚也不推进（``insufficient_data``）。

红线 6 / 20
-----------
回滚 = **新增一条** ``skill_rollbacks`` 记录 + 把流量指针切回 incumbent。
已发布的候选版本与历史上任何记录**原样保留**（可审计），**绝不删除或改写**。
本模块只写不删：:func:`apply_rollback` 只 INSERT 回滚行并更新灰度状态，无任何
DELETE/UPDATE 历史行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.rollout.metrics import (
    P95_LATENCY_RATIO,
    SUCCESS_DROP_PP,
    VALIDATE_FAIL_RISE_PP,
    RolloutMetrics,
)
from forgeflow.rollout.store import (
    STATE_ROLLED_BACK,
    RollbackRecord,
    RolloutRecord,
    RolloutStore,
)

__all__ = [
    "TRIGGER_DANGEROUS",
    "TRIGGER_SUCCESS_DROP",
    "TRIGGER_VALIDATE_FAIL_RISE",
    "TRIGGER_P95_LATENCY",
    "RollbackDecision",
    "evaluate_rollback",
    "apply_rollback",
    "serving_version",
    "exposure_pct",
]

TRIGGER_DANGEROUS = "dangerous_escalation"
TRIGGER_SUCCESS_DROP = "success_rate_drop"
TRIGGER_VALIDATE_FAIL_RISE = "validate_fail_rate_rise"
TRIGGER_P95_LATENCY = "p95_latency_ratio"


@dataclass(frozen=True)
class RollbackDecision:
    """回滚判定结果（纯数据，便于反事实断言）。"""

    triggered: bool
    trigger: str | None = None
    reason: str = ""
    insufficient_data: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "trigger": self.trigger,
            "reason": self.reason,
            "insufficient_data": self.insufficient_data,
            "evidence": dict(self.evidence),
        }


def evaluate_rollback(
    candidate: RolloutMetrics,
    incumbent: RolloutMetrics,
    *,
    dangerous_event: bool = False,
) -> RollbackDecision:
    """判定候选是否需要回滚（**纯函数**，无 I/O、无时钟）。

    Args:
        candidate: 候选版本（v1.1）在该阶段窗口的实测指标。
        incumbent: 旧版（v1.0）作为对照的实测指标（影子对比，只读）。
        dangerous_event: 窗口内是否出现 DANGEROUS 越权事件（立即回滚）。

    Returns:
        :class:`RollbackDecision`。``insufficient_data=True`` 表示样本不足 ⇒
        **未下结论**（既非回滚也非通过）。
    """
    # 1) DANGEROUS 越权 —— 立即回滚，不等样本（安全优先于统计）。
    if dangerous_event:
        return RollbackDecision(
            triggered=True,
            trigger=TRIGGER_DANGEROUS,
            reason="出现 DANGEROUS 越权事件 —— 立即回滚，不等样本",
            evidence={"dangerous_event": True},
        )

    # 2) 样本不足 ⇒ 不下结论（红线 4：未测量/样本不足 ≠ 通过）。
    if candidate.insufficient or incumbent.insufficient:
        return RollbackDecision(
            triggered=False,
            trigger=None,
            reason=(
                "样本不足（未达 ≥%d 个有标签 run 且 ≥%sh 窗口）——"
                "不推进、也不判通过（insufficient_data）"
                % (30, 24)
            ),
            insufficient_data=True,
            evidence={
                "candidate_labeled": candidate.labeled_runs,
                "incumbent_labeled": incumbent.labeled_runs,
                "candidate_window_hours": candidate.window_hours,
                "incumbent_window_hours": incumbent.window_hours,
            },
        )

    # 3) 成功率：候选**置信下界** vs 旧版点估计 − 5pp。
    if candidate.success_rate_lb is not None and incumbent.success_rate is not None:
        floor = incumbent.success_rate - SUCCESS_DROP_PP / 100.0
        if candidate.success_rate_lb < floor:
            return RollbackDecision(
                triggered=True,
                trigger=TRIGGER_SUCCESS_DROP,
                reason=(
                    "成功率置信下界 %.4f 低于旧版成功率 %.4f − %.1fpp 的下限 %.4f"
                    % (candidate.success_rate_lb, incumbent.success_rate, SUCCESS_DROP_PP, floor)
                ),
                evidence={
                    "candidate_success_rate_lb": candidate.success_rate_lb,
                    "incumbent_success_rate": incumbent.success_rate,
                    "threshold_pp": SUCCESS_DROP_PP,
                },
            )

    # 4) 验证失败率上升 > 3pp。
    if candidate.validate_fail_rate is not None and incumbent.validate_fail_rate is not None:
        if candidate.validate_fail_rate > incumbent.validate_fail_rate + VALIDATE_FAIL_RISE_PP / 100.0:
            return RollbackDecision(
                triggered=True,
                trigger=TRIGGER_VALIDATE_FAIL_RISE,
                reason=(
                    "验证失败率 %.4f 高于旧版 %.4f + %.1fpp"
                    % (
                        candidate.validate_fail_rate,
                        incumbent.validate_fail_rate,
                        VALIDATE_FAIL_RISE_PP,
                    )
                ),
                evidence={
                    "candidate_validate_fail_rate": candidate.validate_fail_rate,
                    "incumbent_validate_fail_rate": incumbent.validate_fail_rate,
                    "threshold_pp": VALIDATE_FAIL_RISE_PP,
                },
            )

    # 5) P95 延迟 > 1.5×。
    if candidate.p95_latency_ms is not None and incumbent.p95_latency_ms:
        if candidate.p95_latency_ms > incumbent.p95_latency_ms * P95_LATENCY_RATIO:
            return RollbackDecision(
                triggered=True,
                trigger=TRIGGER_P95_LATENCY,
                reason=(
                    "P95 延迟 %.3f > 旧版 %.3f × %.2f"
                    % (candidate.p95_latency_ms, incumbent.p95_latency_ms, P95_LATENCY_RATIO)
                ),
                evidence={
                    "candidate_p95_latency_ms": candidate.p95_latency_ms,
                    "incumbent_p95_latency_ms": incumbent.p95_latency_ms,
                    "threshold_ratio": P95_LATENCY_RATIO,
                },
            )

    return RollbackDecision(
        triggered=False,
        trigger=None,
        reason="未触发任何回滚条件 —— 候选与旧版持平或更优",
        evidence={},
    )


def serving_version(rollout: RolloutRecord) -> str:
    """当前**默认**（流量指针）版本。

    ``promoted`` ⇒ 候选成为默认；否则（``canary`` / ``rolled_back``）默认仍是
    incumbent —— 回滚即把指针切回 incumbent。
    """
    if rollout.state == STATE_ROLLED_BACK:
        return rollout.incumbent_version
    if rollout.state == "promoted":
        return rollout.candidate_version
    return rollout.incumbent_version


def exposure_pct(rollout: RolloutRecord) -> int:
    """当前对候选版本的暴露百分比（``rolled_back`` ⇒ 0）。"""
    if rollout.state == STATE_ROLLED_BACK:
        return 0
    return int(rollout.stage_pct)


def apply_rollback(
    store: RolloutStore,
    rollout: RolloutRecord,
    decision: RollbackDecision,
    *,
    reason: str | None = None,
) -> RollbackRecord:
    """执行回滚：**新增**回滚记录 + 流量指针切回 incumbent（只写不删）。

    Raises:
        ValueError: ``decision`` 未触发（不得对未触发判定执行回滚）。
    """
    if not decision.triggered:
        raise ValueError("拒绝执行：回滚判定未触发（fail-closed）")
    record = RollbackRecord(
        tenant_id=rollout.tenant_id,
        rollout_id=rollout.id,
        from_version=rollout.candidate_version,
        to_version=rollout.incumbent_version,
        trigger=decision.trigger or "manual",
        reason=reason if reason is not None else decision.reason,
    )
    store.add_rollback(record)
    # 流量指针切回 incumbent：状态置 rolled_back、暴露归 0。
    # 候选版本本身**不删除**（红线 6 / 20）——只改这一行的状态与暴露。
    rollout.state = STATE_ROLLED_BACK
    rollout.stage_pct = 0
    store.save_rollout(rollout)
    return record
