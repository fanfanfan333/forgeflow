"""INC46 T24 — 共享的 run 状态词汇（**加性、绝不改既有枚举**）。

Why this module exists
----------------------
全仓此前**没有**统一的 run 状态枚举：最接近的权威词汇表是
:data:`forgeflow.workspace.store.TERMINAL_STATUSES`（``completed`` / ``failed`` /
``aborted`` / ``interrupted`` / ``rejected``）。T24 的「验证失败 ⇒ 诚实失败」需要
一个**新的终态** ``failed_validation`` —— 它在全仓 NOT_FOUND（裁决：新增共享常量，
供 T16 的 labeler 派生 ``FAILED_SYSTEM``，而非把魔法字符串散落各处）。

加性纪律（红线 1）
------------------
本模块**只新增一个常量**：

* 它 **不** 修改 ``workspace.store.TERMINAL_STATUSES`` 的既有成员，也不改其语义；
  这是一个**追加**的、独立的取值，任何既有读者（重启对账、runs API）行为逐字不变。
* 取值与 :data:`forgeflow.outcomes.labeler.FAILED_VALIDATION_STATUS` **逐字一致**
  （``"failed_validation"``）。后者是 T16 为**避免循环依赖**而在本地写死的同值常量
  （labeler 的 docstring 已声明「取值与任务书一致，并由单测钉住」）—— 本模块把它
  提升为**唯一对外共享**的来源，二者由单测钉住相等。
"""

from __future__ import annotations

__all__ = ["FAILED_VALIDATION"]

#: The run reaches this terminal state when the repair loop could not make the
#: document pass validation within budget (INC46 T24). It is an **additive** value
#: — a run whose artifact failed validation is honestly reported as failed and is
#: **never** handed back as a successful artifact (red line 15).
FAILED_VALIDATION = "failed_validation"
