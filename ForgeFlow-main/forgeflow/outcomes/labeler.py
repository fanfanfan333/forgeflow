"""T16 — 从「信号」推导出 ``outcome_label``（纯函数，可单独断言）。

裁定 Y1（主理人 2026-10-04）—— 判定顺序必须写死在这里，不得散落各处::

    显式用户信号  >  系统失败  >  隐式接受
    (revert/abandon/reject/revise/approval)
                     ↓
              FAILED_SYSTEM（run 状态 failed_validation）
                     ↓
              ACCEPTED_IMPLICIT（硬验证通过 **且** 窗口 W 内无返工）

为什么是这个顺序
----------------
* **显式信号最优先**：用户亲手点了「拒绝」，就不得因为「验证也通过了」而被
  判成接受。用户的判断盖过系统的判断。
* **系统失败次之**：``run_status == failed_validation`` 是 T24 状态常量派生的
  客观失败，它压过「没人说话所以算接受」的推断。
* **隐式接受最后，且必须等窗口**：硬验证通过**不等于**用户满意。
  §十 给定 W = 24h（T36 校准后再定）；窗口**未满**时结论是 ``UNKNOWN``，
  **不是** ``ACCEPTED_IMPLICIT`` —— 抢跑等于把「还没来得及返工」冒充「认可」。

诚实纪律
--------
* ``hard_pass`` 为 ``None``（未测量）时**绝不**走隐式接受分支 —— 没有硬验证
  结果就没有「硬验证通过」，此时只能是 ``UNKNOWN``（红线 4 + 红线 12）。
* 窗口未满 ⇒ ``UNKNOWN``，绝不提前给正向标签。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from forgeflow.outcomes.signals import (
    ABANDONED,
    ACCEPTED_EXPLICIT,
    ACCEPTED_IMPLICIT,
    FAILED_SYSTEM,
    IMPLICIT_WINDOW_HOURS,
    REJECTED,
    REVISED,
    REVERTED,
    UNKNOWN,
)

__all__ = [
    "FAILED_VALIDATION_STATUS",
    "APPROVAL_KINDS",
    "REWORK_KINDS",
    "derive_label",
]

#: T24 派生的「验证失败」run 状态常量（任务书 T16 规格原文）。
#: 这里**不导入** T24 模块，避免为取一个字符串常量引入循环依赖；
#: 取值与任务书一致，并由单测钉住。
FAILED_VALIDATION_STATUS = "failed_validation"

#: 表示「用户明确采纳」的反馈种类。
APPROVAL_KINDS: frozenset[str] = frozenset({"approval", "like", "accept"})

#: 表示「返工」的反馈种类 —— 窗口内出现任一项即不构成隐式接受。
REWORK_KINDS: frozenset[str] = frozenset({"revise", "revert"})

_KIND_TO_LABEL: dict[str, str] = {
    "revert": REVERTED,
    "abandon": ABANDONED,
    "reject": REJECTED,
    "revise": REVISED,
}


def derive_label(
    *,
    run_status: str | None = None,
    feedback_kinds: Sequence[str] | Iterable[str] = (),
    hard_pass: bool | None = None,
    verified_at: datetime | None = None,
    now: datetime | None = None,
    window_hours: int = IMPLICIT_WINDOW_HOURS,
) -> str:
    """推导一个 run 的 ``outcome_label``。

    Args:
        run_status: run 的状态；``failed_validation`` ⇒ ``FAILED_SYSTEM``。
        feedback_kinds: 该 run 上已发生的反馈种类序列（顺序无关）。
        hard_pass: T23 验证栈的硬结论；``None`` = 未测量（**禁止**走隐式接受）。
        verified_at: 硬验证通过的时刻（隐式接受窗口的起点）。
        now: 判定时刻；默认取当前 UTC 时间。
        window_hours: 隐式接受窗口，默认 :data:`IMPLICIT_WINDOW_HOURS`。

    Returns:
        :data:`~forgeflow.outcomes.signals.OUTCOME_LABELS` 中的一个标签。
    """
    kinds = set(feedback_kinds or ())

    # 1) 显式用户信号 —— 盖过一切推断。遍历顺序即终态强度（dict 有序）。
    for kind, label in _KIND_TO_LABEL.items():
        if kind in kinds:
            return label

    # 2) 系统判定的客观失败（无需用户信号）。
    if run_status == FAILED_VALIDATION_STATUS:
        return FAILED_SYSTEM

    # 3) 明确采纳。
    if kinds & APPROVAL_KINDS:
        return ACCEPTED_EXPLICIT

    # 4) 隐式接受 —— 必须「硬验证真的通过」+「窗口已满」+「窗口内无返工」。
    if hard_pass is True and verified_at is not None:
        if kinds & REWORK_KINDS:
            return UNKNOWN
        moment = now or datetime.now(timezone.utc)
        due = verified_at + timedelta(hours=window_hours)
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment >= due:
            return ACCEPTED_IMPLICIT
        # 窗口未满 ⇒ 结论未定，绝不是「接受」。
        return UNKNOWN

    return UNKNOWN
