"""INC46 T35 — Skill 淘汰（deprecated + 宽限期）与撤销。

任务书 T35 §规格
================
* **淘汰触发**（初始默认，§十）：
  1. **90 天无调用**（``last_used_at`` 早于 ``now - 90d``）；或
  2. **最近 30 次有标签 run 的 ``success_rate < 50%``**。
* 触发 ⇒ 进入 ``deprecated``，**宽限期 14 天**并**通知 owner**；
* **宽限期内恢复则撤销**（``deprecated → published``）；
* 宽限期满 ⇒ ``archived``（仍可读、可审计，**不可被检索**）。

诚实纪律（红线 4 / 12）
-----------------------
* ``success_rate`` 复用 T16 的权威算术：``UNKNOWN`` **不进分母**；样本不足 30 条
  ⇒ ``success_rate is None``（**不是** 0），此时「低成功率」触发条件**不成立**
  —— 不能用「没数据」冒充「表现差」。
* 时间一律由调用方传入（``now``），本模块无时钟、无 I/O。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from forgeflow.outcomes.signals import counts_toward_rate, success_rate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forgeflow.lifecycle.store import LifecycleStore

__all__ = [
    "IDLE_DAYS",
    "MIN_RUNS",
    "SUCCESS_FLOOR",
    "GRACE_DAYS",
    "TRIGGER_IDLE",
    "TRIGGER_LOW_SUCCESS",
    "RetirementDecision",
    "evaluate_retirement",
    "grace_deadline",
    "grace_started_at",
    "within_grace",
    "build_notice",
    "start_grace",
    "revoke_grace",
    "finalize_grace",
]

#: 初始默认（§十：90 天无调用 / 最近 30 次成功率 < 50%；宽限 14 天）。
IDLE_DAYS: int = 90
MIN_RUNS: int = 30
SUCCESS_FLOOR: float = 0.5
GRACE_DAYS: int = 14

TRIGGER_IDLE = "idle_90d"
TRIGGER_LOW_SUCCESS = "low_success_rate"


@dataclass(frozen=True)
class RetirementDecision:
    """一次淘汰评估的结论（未触发时 ``should_retire=False``）。"""

    should_retire: bool = False
    trigger: str | None = None
    reason: str = ""
    idle_days: float | None = None
    labeled_runs: int = 0
    success_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_retire": self.should_retire,
            "trigger": self.trigger,
            "reason": self.reason,
            "idle_days": self.idle_days,
            "labeled_runs": self.labeled_runs,
            "success_rate": self.success_rate,
        }


def evaluate_retirement(
    *,
    now: datetime,
    last_used_at: datetime | None = None,
    recent_labels: Iterable[str | None] = (),
    idle_days: int = IDLE_DAYS,
    min_runs: int = MIN_RUNS,
    success_floor: float = SUCCESS_FLOOR,
) -> RetirementDecision:
    """判定是否应淘汰（``idle`` 优先于 ``low_success``，二者任一成立即触发）。

    Args:
        now: 评估时刻。
        last_used_at: 最近一次调用时间；``None`` ⇒ 无调用记录，**不**据此判闲置
            （没有记录 ≠ 没有使用，避免误淘汰）。
        recent_labels: 最近的有标签 run 序列（``UNKNOWN`` 不进分母）。
        idle_days / min_runs / success_floor: 阈值（默认 §十 初始值）。
    """
    labels = list(recent_labels)
    counted = [x for x in labels if counts_toward_rate(x)]
    rate = success_rate(labels)  # 全 UNKNOWN / 无样本 ⇒ None

    idle: float | None = None
    if last_used_at is not None:
        idle = (now - last_used_at).total_seconds() / 86400.0

    if idle is not None and idle >= idle_days:
        return RetirementDecision(
            should_retire=True,
            trigger=TRIGGER_IDLE,
            reason=f"连续 {idle:.1f} 天无调用（阈值 {idle_days} 天）",
            idle_days=idle,
            labeled_runs=len(counted),
            success_rate=rate,
        )

    if len(counted) >= min_runs and rate is not None and rate < success_floor:
        return RetirementDecision(
            should_retire=True,
            trigger=TRIGGER_LOW_SUCCESS,
            reason=(
                f"最近 {len(counted)} 次有标签 run 成功率 {rate:.3f} < {success_floor}"
            ),
            idle_days=idle,
            labeled_runs=len(counted),
            success_rate=rate,
        )

    return RetirementDecision(
        should_retire=False,
        trigger=None,
        reason="未达淘汰条件",
        idle_days=idle,
        labeled_runs=len(counted),
        success_rate=rate,
    )


def grace_deadline(started_at: datetime, *, grace_days: int = GRACE_DAYS) -> datetime:
    """宽限期截止时刻 = 进入 ``deprecated`` 的时刻 + 14 天。"""
    return started_at + timedelta(days=grace_days)


def grace_started_at(
    store: "LifecycleStore", tenant_id: str | None, skill_id: str
) -> datetime | None:
    """最近一次进入 ``deprecated`` 的时刻（即宽限期起点）；从未进入 ⇒ ``None``。"""
    latest: datetime | None = None
    for event in store.list_events(tenant_id, skill_id):
        if event.to_state == "deprecated":
            latest = event.created_at
    return latest


def within_grace(
    store: "LifecycleStore",
    tenant_id: str | None,
    skill_id: str,
    *,
    now: datetime,
    grace_days: int = GRACE_DAYS,
) -> bool:
    """该 skill 是否**当前**仍在折旧宽限期内。

    两个条件缺一不可：① 当前状态确为 ``deprecated``（一旦宽限期内恢复 / 撤销，
    状态回到 ``published``，就不再处于宽限期 —— 否则「撤销」形同虚设）；
    ② ``now`` 早于宽限期截止。从未进入 ``deprecated`` ⇒ ``False``。
    """
    from forgeflow.lifecycle.state_machine import current_state

    if current_state(store, tenant_id, skill_id) != "deprecated":
        return False
    started = grace_started_at(store, tenant_id, skill_id)
    if started is None:
        return False
    return now < grace_deadline(started, grace_days=grace_days)


def build_notice(
    skill_id: str,
    *,
    owner: str | None,
    decision: RetirementDecision,
    deadline: datetime,
) -> dict[str, Any]:
    """组装「通知 owner」的通知体（纯数据；投递由调用方/集成层负责）。"""
    return {
        "kind": "skill_retirement_grace",
        "skill_id": skill_id,
        "owner": owner,
        "trigger": decision.trigger,
        "reason": decision.reason,
        "grace_deadline": deadline.isoformat(),
        "message": (
            f"技能 {skill_id} 触发淘汰（{decision.reason}），进入 14 天宽限期，"
            f"截止 {deadline.isoformat()}；期间恢复调用或通过审批可撤销。"
        ),
    }


def start_grace(
    store: "LifecycleStore",
    tenant_id: str | None,
    skill_id: str,
    *,
    owner: str | None = None,
    decision: RetirementDecision | None = None,
    actor: str = "system",
    now: datetime | None = None,
    grace_days: int = GRACE_DAYS,
) -> dict[str, Any]:
    """进入淘汰宽限期：``published → deprecated`` + 记录审计事件 + 产出通知体。

    Returns:
        通知体（见 :func:`build_notice`），其中含宽限期截止时刻。
    """
    from forgeflow.lifecycle.state_machine import apply_transition

    decision = decision or RetirementDecision(
        should_retire=True, trigger=TRIGGER_IDLE, reason="手动进入折旧宽限期"
    )
    event = apply_transition(
        store,
        tenant_id,
        skill_id,
        "deprecated",
        reason=decision.reason,
        actor=actor,
        now=now,
    )
    deadline = grace_deadline(event.created_at, grace_days=grace_days)
    return build_notice(skill_id, owner=owner, decision=decision, deadline=deadline)


def revoke_grace(
    store: "LifecycleStore",
    tenant_id: str | None,
    skill_id: str,
    *,
    reason: str = "宽限期内恢复",
    actor: str = "system",
    now: datetime | None = None,
) -> Any:
    """撤销淘汰：``deprecated → published``（宽限期内恢复即撤销）。"""
    from forgeflow.lifecycle.state_machine import apply_transition

    return apply_transition(
        store, tenant_id, skill_id, "published", reason=reason, actor=actor, now=now
    )


def finalize_grace(
    store: "LifecycleStore",
    tenant_id: str | None,
    skill_id: str,
    *,
    reason: str = "宽限期满归档",
    actor: str = "system",
    now: datetime | None = None,
) -> Any:
    """宽限期满：``deprecated → archived``（仍可读可审计，不可被检索）。"""
    from forgeflow.lifecycle.state_machine import apply_transition

    return apply_transition(
        store, tenant_id, skill_id, "archived", reason=reason, actor=actor, now=now
    )
