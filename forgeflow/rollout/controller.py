"""INC46 T34 — 灰度控制器（Canary rollout controller）。

职责
====
* **阶段推进**：``5% → 25% → 100%``；每阶段须 ``≥ 30`` 个有标签 run 且 ``≥ 24h``；
* **确定性分流**：``sha1(seed) % 100 < pct``（复用 :func:`forgeflow.skills.canary.should_serve`），
  **租户级开关**（未开启 ⇒ 暴露恒 0，选择层逐字节不变）；
* **影子对比**：候选与 incumbent 同一输入并行评估，只做**只读**对照（不产生副作用）；
* **决策**：样本不足 ⇒ ``insufficient_data``（不推进、不判通过）；触发回滚条件 ⇒
  ``rollback``；否则推进一档，到 100% 即 ``promote``。

诚实纪律
--------
* 未测量一律 ``None``（红线 4）；样本不足绝不折算通过；
* 租户 fail-closed：未解析租户读写被拒（红线 5）；
* 状态机**只追加**历史（回滚写新行，不删改，红线 6 / 20）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forgeflow.rollout.metrics import STAGES, RolloutMetrics
from forgeflow.rollout.rollback import (
    RollbackDecision,
    apply_rollback,
    evaluate_rollback,
    exposure_pct,
    serving_version,
)
from forgeflow.rollout.store import (
    STATE_CANARY,
    STATE_PROMOTED,
    MetricsRecord,
    RollbackRecord,
    RolloutError,
    RolloutRecord,
    RolloutStore,
)

__all__ = [
    "stage_index",
    "next_stage_pct",
    "rollout_enabled",
    "set_tenant_rollout_enabled",
    "reset_rollout_state",
    "should_route_to_candidate",
    "routed_version",
    "StageDecision",
    "decide_stage",
    "start_rollout",
    "apply_stage_decision",
    "record_stage_metrics",
    "manual_rollback",
]

#: 租户级开关（进程内）。未显式登记的租户回落到全局 flag。
_ENABLED_TENANTS: set[str] = set()


def stage_index(pct: int | None) -> int:
    """返回 ``pct`` 在 :data:`STAGES` 中的下标；不在表内 ⇒ ``-1``。"""
    try:
        return STAGES.index(int(pct))  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return -1


def next_stage_pct(pct: int | None) -> int | None:
    """下一档暴露百分比；已是最后一档 ⇒ ``None``。"""
    idx = stage_index(pct)
    if idx < 0 or idx >= len(STAGES) - 1:
        return None
    return STAGES[idx + 1]


def rollout_enabled(tenant_id: str | None) -> bool:
    """租户级灰度开关。

    **显式**登记的租户以其登记为准；**未登记**的租户回落到全局
    ``settings.skill_canary_enabled``（默认 ``False`` ⇒ 暴露恒 0）。
    """
    if not tenant_id:
        return False  # 未解析租户 ⇒ 不开启（fail-closed，红线 5）
    if str(tenant_id) in _ENABLED_TENANTS:
        return True
    try:
        from forgeflow.config import get_settings

        return bool(getattr(get_settings(), "skill_canary_enabled", False))
    except Exception:  # noqa: BLE001 — 读不到配置 ⇒ 不开启（fail-closed）
        return False


def set_tenant_rollout_enabled(tenant_id: str | None, enabled: bool) -> None:
    """登记/撤销一个租户的灰度开关（测试与运维用）。"""
    if not tenant_id:
        raise RolloutError("拒绝操作：未解析租户（fail-closed，红线 5）")
    if enabled:
        _ENABLED_TENANTS.add(str(tenant_id))
    else:
        _ENABLED_TENANTS.discard(str(tenant_id))


def reset_rollout_state() -> None:
    """清空进程内租户开关（测试用）。"""
    _ENABLED_TENANTS.clear()


def should_route_to_candidate(seed: str, pct: int) -> bool:
    """确定性分流：同一 ``(seed, pct)`` 永远给出同一答案（可重放）。"""
    from forgeflow.skills.canary import should_serve

    return should_serve(seed, pct)


def routed_version(
    tenant_id: str | None,
    *,
    skill_id: str,
    seed: str,
    store: RolloutStore,
) -> dict[str, Any]:
    """解析某租户对某 skill 的**灰度分流**结果（选择层扩展点）。

    Returns:
        ``{"enabled", "rollout_id", "serving_version", "exposure_pct", "candidate"}``。
        无进行中的灰度 / 开关关闭 ⇒ ``serving_version`` 恒为当前默认版本、
        ``exposure_pct=0``（选择层逐字节不变）。
    """
    enabled = rollout_enabled(tenant_id)
    rollout = store.latest_rollout(tenant_id, skill_id)
    if rollout is None or not enabled or rollout.state != STATE_CANARY:
        # 无进行中的灰度 / 开关关闭 ⇒ 暴露恒 0（选择层逐字节不变）。
        serving = serving_version(rollout) if rollout is not None else None
        return {
            "enabled": enabled,
            "rollout_id": rollout.id if rollout is not None else None,
            "serving_version": serving,
            "exposure_pct": 0,
            "candidate": None,
        }
    pct = exposure_pct(rollout)
    to_candidate = should_route_to_candidate(seed, pct)
    return {
        "enabled": True,
        "rollout_id": rollout.id,
        "serving_version": (
            rollout.candidate_version if to_candidate else rollout.incumbent_version
        ),
        "exposure_pct": pct,
        "candidate": rollout.candidate_version,
    }


@dataclass(frozen=True)
class StageDecision:
    """一次阶段评审的结论（纯数据）。"""

    action: str  # advance | promote | rollback | insufficient_data
    reason: str
    current_pct: int
    next_pct: int
    rollback: RollbackDecision | None = None
    gate: dict[str, Any] = field(default_factory=dict)

    @property
    def concludes(self) -> bool:
        """是否给出了**结论**（样本不足 ⇒ 未下结论）。"""
        return self.action != "insufficient_data"

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "current_pct": self.current_pct,
            "next_pct": self.next_pct,
            "concludes": self.concludes,
            "rollback": self.rollback.to_dict() if self.rollback else None,
            "gate": dict(self.gate),
        }


def decide_stage(
    candidate: RolloutMetrics,
    incumbent: RolloutMetrics,
    *,
    current_pct: int,
    dangerous_event: bool = False,
) -> StageDecision:
    """评审当前阶段：推进 / 封顶提升 / 回滚 / 样本不足。

    规则（任务书 T34）：
    * 样本不足（候选或对照）⇒ ``insufficient_data``（不推进、不判通过）；
    * 触发任一回滚条件 ⇒ ``rollback``；
    * 否则推进一档；已处 100% ⇒ ``promote``（候选成为默认）。
    """
    gate = {
        "candidate_labeled_runs": candidate.labeled_runs,
        "candidate_window_hours": candidate.window_hours,
        "incumbent_labeled_runs": incumbent.labeled_runs,
        "incumbent_window_hours": incumbent.window_hours,
        "candidate_success_rate_lb": candidate.success_rate_lb,
        "incumbent_success_rate": incumbent.success_rate,
    }

    # DANGEROUS 越权优先于一切（含样本不足）：安全 > 统计。
    # :func:`evaluate_rollback` 内部先判 dangerous 再判 insufficient，此处直接
    # 复用其优先级 —— 保证「出现 DANGEROUS 事件 ⇒ 立即回滚，不等样本」。
    rb = evaluate_rollback(candidate, incumbent, dangerous_event=dangerous_event)
    if rb.triggered:
        return StageDecision(
            action="rollback",
            reason=rb.reason,
            current_pct=int(current_pct),
            next_pct=0,
            rollback=rb,
            gate=gate,
        )

    if candidate.insufficient or incumbent.insufficient:
        return StageDecision(
            action="insufficient_data",
            reason=(
                "样本不足 —— 不推进、也不判通过（insufficient_data，未下结论）"
            ),
            current_pct=int(current_pct),
            next_pct=int(current_pct),
            gate={**gate, "requirement": "labeled_runs>=30 and window_hours>=24"},
        )

    nxt = next_stage_pct(current_pct)
    if nxt is None:
        return StageDecision(
            action="promote",
            reason="最后一档表现良好 —— 候选提升为默认版本（100%）",
            current_pct=int(current_pct),
            next_pct=STAGES[-1],
            gate=gate,
        )
    return StageDecision(
        action="advance",
        reason=f"表现良好 —— 推进至下一档 {nxt}%",
        current_pct=int(current_pct),
        next_pct=nxt,
        gate=gate,
    )


def start_rollout(
    tenant_id: str | None,
    *,
    skill_id: str,
    candidate_version: str,
    incumbent_version: str,
    store: RolloutStore,
    stage_pct: int = STAGES[0],
) -> RolloutRecord:
    """开启一次灰度：候选进入第一档（默认 5%），默认版本仍是 incumbent。"""
    if not tenant_id:
        raise RolloutError("拒绝开启灰度：未解析租户（fail-closed，红线 5）")
    if stage_index(stage_pct) < 0:
        raise RolloutError(f"非法阶段 {stage_pct!r}；必须是 {STAGES} 之一")
    record = RolloutRecord(
        tenant_id=str(tenant_id),
        skill_id=str(skill_id),
        candidate_version=str(candidate_version),
        incumbent_version=str(incumbent_version),
        stage_pct=int(stage_pct),
        state=STATE_CANARY,
    )
    return store.save_rollout(record)


def record_stage_metrics(
    tenant_id: str | None,
    rollout: RolloutRecord,
    metrics: RolloutMetrics,
    *,
    store: RolloutStore,
) -> MetricsRecord:
    """把一个阶段窗口的实测指标落库（未测量列保持 ``None``，红线 4）。"""
    if not tenant_id:
        raise RolloutError("拒绝写入指标：未解析租户（fail-closed，红线 5）")
    rec = MetricsRecord(
        tenant_id=str(tenant_id),
        rollout_id=rollout.id,
        stage_pct=int(rollout.stage_pct),
        labeled_runs=int(metrics.labeled_runs),
        successes=int(metrics.successes),
        success_rate=metrics.success_rate,
        success_rate_lb=metrics.success_rate_lb,
        validate_fail_rate=metrics.validate_fail_rate,
        rework_rate=metrics.rework_rate,
        p95_latency_ms=metrics.p95_latency_ms,
        cost=metrics.cost,
        window_hours=metrics.window_hours,
    )
    return store.add_metrics(rec)


def apply_stage_decision(
    tenant_id: str | None,
    rollout: RolloutRecord,
    decision: StageDecision,
    *,
    store: RolloutStore,
) -> dict[str, Any]:
    """把阶段结论落到状态机：推进 / 封顶提升 / 回滚（**不删改历史**）。

    Returns:
        ``{"action", "rollout", "rollback_id"}``；``rollback_id`` 仅回滚时非空。
    """
    if not tenant_id:
        raise RolloutError("拒绝执行：未解析租户（fail-closed，红线 5）")
    if decision.action == "insufficient_data":
        # 不推进、不改状态 —— 未下结论。
        return {"action": "insufficient_data", "rollout": rollout.to_dict(), "rollback_id": None}

    if decision.action == "rollback":
        assert decision.rollback is not None
        rec = apply_rollback(store, rollout, decision.rollback)
        return {"action": "rollback", "rollout": rollout.to_dict(), "rollback_id": rec.id}

    if decision.action == "promote":
        rollout.stage_pct = STAGES[-1]
        rollout.state = STATE_PROMOTED
        store.save_rollout(rollout)
        return {"action": "promote", "rollout": rollout.to_dict(), "rollback_id": None}

    # advance
    rollout.stage_pct = int(decision.next_pct)
    rollout.state = STATE_CANARY
    store.save_rollout(rollout)
    return {"action": "advance", "rollout": rollout.to_dict(), "rollback_id": None}


def manual_rollback(
    tenant_id: str | None,
    rollout: RolloutRecord,
    *,
    store: RolloutStore,
    reason: str = "",
    actor: str = "",
) -> RollbackRecord:
    """人工回滚：与自动回滚同一路径（新增记录 + 指针切回 incumbent）。"""
    decision = RollbackDecision(
        triggered=True,
        trigger="manual",
        reason=reason or f"人工回滚（{actor or 'operator'}）",
    )
    return apply_rollback(store, rollout, decision, reason=reason or decision.reason)
