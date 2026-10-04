"""INC46 T35 — Skill 生命周期状态机（draft → candidate → published → deprecated → archived）。

任务书 T35 §规格
================
* **状态机**：``draft → candidate → published → deprecated → archived``；
  ``archived`` 仍可读、可审计，**不可被检索**。
* 非法跃迁（如 ``archived → published``）⇒ **拒绝**（fail-closed）。
* 检索前过滤 ``deprecated`` / ``archived``：T09 已在 :func:`retrieval.build_pool`
  定义 :data:`~forgeflow.skills.retrieval.EXCLUDED_STATUSES`「此前无该状态则为空操作」；
  本状态机把状态真正写进 ``SkillRecord.status`` 后，那道过滤**自动生效**。

设计纪律
--------
* **状态即 ``SkillRecord.status``**：本模块不新增第二套状态字段，迁移后的 ``to_state``
  = 写回 ``skills.status`` 的值。既有 ``draft`` / ``published`` 字符串与旧数据兼容
  （加性：``evaluating`` / ``retired`` 不是本状态机的状态，但**不被本模块改写**）。
* **审计只追加**：每次迁移新增一条 :class:`~forgeflow.lifecycle.store.LifecycleEvent`
  （红线 6）。状态机本身无 I/O，持久化交给 :mod:`forgeflow.lifecycle.store`。
* 纯函数 + 显式 ``now``：便于单测与反事实变异。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from forgeflow.lifecycle.store import LifecycleEvent, LifecycleStore

__all__ = [
    "LIFECYCLE_STATES",
    "STATE_DRAFT",
    "STATE_CANDIDATE",
    "STATE_PUBLISHED",
    "STATE_DEPRECATED",
    "STATE_ARCHIVED",
    "RETRIEVAL_EXCLUDED_STATES",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_STATE",
    "IllegalTransition",
    "LifecycleError",
    "can_transition",
    "validate_transition",
    "current_state",
    "apply_transition",
    "retrieval_excluded",
    "LifecycleService",
]

STATE_DRAFT = "draft"
STATE_CANDIDATE = "candidate"
STATE_PUBLISHED = "published"
STATE_DEPRECATED = "deprecated"
STATE_ARCHIVED = "archived"

#: The ordered lifecycle (任务书 T35 原文)。
LIFECYCLE_STATES: tuple[str, ...] = (
    STATE_DRAFT,
    STATE_CANDIDATE,
    STATE_PUBLISHED,
    STATE_DEPRECATED,
    STATE_ARCHIVED,
)

#: 状态机允许的跃迁。``archived`` 是**终态**（无出边）—— ``archived → published``
#: 非法，必须被拒（任务书 T35 阴性探针）。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_DRAFT: frozenset({STATE_CANDIDATE}),
    STATE_CANDIDATE: frozenset({STATE_PUBLISHED, STATE_DRAFT}),
    STATE_PUBLISHED: frozenset({STATE_DEPRECATED}),
    # deprecated 有两条出边：宽限期内恢复（撤销淘汰）⇒ published；宽限期满 ⇒ archived。
    STATE_DEPRECATED: frozenset({STATE_PUBLISHED, STATE_ARCHIVED}),
    STATE_ARCHIVED: frozenset(),
}

#: 一个没有任何迁移记录时的默认状态 —— 既有 skill 都是「已发布」语义（保持召回不变）。
DEFAULT_STATE = STATE_PUBLISHED

#: 检索前剔除的状态（与 :data:`forgeflow.skills.retrieval.EXCLUDED_STATUSES` 同义）。
RETRIEVAL_EXCLUDED_STATES: frozenset[str] = frozenset({STATE_DEPRECATED, STATE_ARCHIVED})


class LifecycleError(ValueError):
    """非法生命周期操作（fail-closed）。"""


class IllegalTransition(LifecycleError):
    """一次被拒绝的状态跃迁（任务书 T35 阴性探针）。"""

    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(f"非法状态跃迁：{from_state!r} → {to_state!r} 不被允许")
        self.from_state = from_state
        self.to_state = to_state


def can_transition(from_state: str, to_state: str) -> bool:
    """``from_state → to_state`` 是否合法（``from`` 未知 ⇒ 一律拒绝）。"""
    allowed = ALLOWED_TRANSITIONS.get(str(from_state))
    return bool(allowed) and str(to_state) in allowed


def validate_transition(from_state: str, to_state: str) -> None:
    """校验跃迁；非法即抛 :class:`IllegalTransition`（fail-closed）。

    ``to_state`` 必须先属于 :data:`LIFECYCLE_STATES`，再判有没有那条边。
    """
    if str(to_state) not in LIFECYCLE_STATES:
        raise IllegalTransition(from_state, to_state)
    if not can_transition(from_state, to_state):
        raise IllegalTransition(from_state, to_state)


def current_state(
    store: "LifecycleStore", tenant_id: str | None, skill_id: str, *, default: str = DEFAULT_STATE
) -> str:
    """该 skill 的当前生命周期状态（取最近一条迁移的 ``to_state``；无则 ``default``）。"""
    event = store.latest_event(tenant_id, skill_id)
    return event.to_state if event is not None else default


def apply_transition(
    store: "LifecycleStore",
    tenant_id: str | None,
    skill_id: str,
    to_state: str,
    *,
    reason: str = "",
    actor: str | None = None,
    now: datetime | None = None,
    default_from: str = DEFAULT_STATE,
) -> "LifecycleEvent":
    """校验并**追加**一条状态迁移事件（不修改任何既有行）。

    Args:
        store: 持久层（内存或 PG）。
        tenant_id: 调用方租户；``None`` ⇒ 拒绝（fail-closed，红线 5）。
        skill_id: 目标 skill。
        to_state: 目标状态。
        reason / actor: 审计理由与操作者。
        now: 事件时间（缺省由 store 生成）。
        default_from: 无历史事件时假定的起始状态。

    Returns:
        新增的 :class:`~forgeflow.lifecycle.store.LifecycleEvent`。

    Raises:
        LifecycleError: 租户未解析。
        IllegalTransition: 跃迁非法（此时**不写入任何行**）。
    """
    from forgeflow.lifecycle.store import LifecycleEvent

    if not tenant_id:
        raise LifecycleError("拒绝迁移：未解析租户（fail-closed，红线 5）")
    from_state = current_state(store, tenant_id, skill_id, default=default_from)
    # 关键承重点：非法跃迁必须在这里被拦住（反事实「去掉状态机校验 ⇒ 非法跃迁转红」）。
    validate_transition(from_state, to_state)
    event = LifecycleEvent(
        tenant_id=str(tenant_id),
        skill_id=str(skill_id),
        from_state=from_state,
        to_state=str(to_state),
        reason=reason,
        actor=actor,
    )
    if now is not None:
        event.created_at = now
    store.append_event(event)
    return event


def retrieval_excluded(
    store: "LifecycleStore", tenant_id: str | None, skill_id: str, *, default: str = DEFAULT_STATE
) -> bool:
    """该 skill 是否应被检索剔除（``deprecated`` / ``archived``）。"""
    return current_state(store, tenant_id, skill_id, default=default) in RETRIEVAL_EXCLUDED_STATES


@dataclass
class LifecycleService:
    """把状态机接到一个 ``status`` 写回器上（可选）。

    ``status_setter`` 是一个 ``(tenant_id, skill_id, state) -> None`` 回调，用来把迁移
    后的状态**同步写进** ``skills.status``（从而让 T09 的 :data:`EXCLUDED_STATUSES`
    过滤生效）。不注入回调时，本服务只维护事件流（纯离线可用）。
    """

    store: Any
    status_setter: Callable[[str, str, str], None] | None = None

    def transition(
        self,
        tenant_id: str | None,
        skill_id: str,
        to_state: str,
        *,
        reason: str = "",
        actor: str | None = None,
        now: datetime | None = None,
    ) -> "LifecycleEvent":
        event = apply_transition(
            self.store, tenant_id, skill_id, to_state, reason=reason, actor=actor, now=now
        )
        if self.status_setter is not None:
            self.status_setter(str(tenant_id), str(skill_id), event.to_state)
        return event

    def state(self, tenant_id: str | None, skill_id: str) -> str:
        return current_state(self.store, tenant_id, skill_id)
