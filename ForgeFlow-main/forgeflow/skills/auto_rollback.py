"""INC46 T34 — 发布联锁 **R7** 的锚点模块（``灰度与自动回滚``）。

本模块是 ``forgeflow/skills/publish_interlock.py`` 中 R7 的**能力锚点**
（``CapabilityAnchor("R7", "T34", "forgeflow.skills.auto_rollback", "灰度与自动回滚")``）。
它把 T34 的灰度/回滚能力（:mod:`forgeflow.rollout`）在此处 re-export，作为
「该能力已落地」的**稳定 import 目标**。

为何**刻意不提供** ``INTERLOCK_PROBE``（fail-closed）
=====================================================
联锁探针的语义是「该能力**真实可用** ⇒ 报 ``ok=True``，从而参与解锁」。R7 属
**Level-2（自动发布）** 的八项之一；当前 R2–R7 均未满足，联锁整体**必须保持锁死**。

若本模块此刻导出一个自报 ``ok=True`` 的探针，会立即把 R7 翻成 ``met`` ——
这会**提前**改变联锁快照（``missing`` 从 ``[R2..R8]`` 变成 ``[R2,R3,R5,R6,R8]``），
打红 T13/T15 的独立探针用例与 T15/publish_interlock 的时点快照，而**收益为零**
（Level-2 仍需 R1–R8 全满足，R7 单独变 met 不解锁任何一级）。

与既有处置**一致**：T32（R5 ``forgeflow.experience.redaction``）与 T33
（R6 ``forgeflow.security.quarantine``）同样**只落地能力模块、不导出探针**，
R5/R6 至今如实为**未满足**。T34 沿用同一纪律：**落地能力，保持 fail-closed**。

后续（T36 收口）
----------------
探针的**翻转**须与快照解耦一并裁决（见「裁定 G」登记的 TODO：把
``tests/unit/test_inc46_publish_interlock.py`` 的时点快照改为**定向构造**）。
届时若引入探针，必须像 R4 那样**以真实证据为闸**（如：确有进行中的灰度/回滚记录），
而非恒真自检 —— 否则就是把「模块存在」冒充「能力可用」。
"""

from __future__ import annotations

from forgeflow.rollout.controller import (
    StageDecision,
    apply_stage_decision,
    decide_stage,
    manual_rollback,
    next_stage_pct,
    record_stage_metrics,
    reset_rollout_state,
    rollout_enabled,
    routed_version,
    set_tenant_rollout_enabled,
    should_route_to_candidate,
    stage_index,
    start_rollout,
)
from forgeflow.rollout.metrics import (
    STAGES,
    RolloutMetrics,
    derive_metrics,
    wilson_lower_bound,
)
from forgeflow.rollout.rollback import (
    RollbackDecision,
    apply_rollback,
    evaluate_rollback,
    exposure_pct,
    serving_version,
)
from forgeflow.rollout.store import (
    InMemoryRolloutStore,
    RolloutRecord,
    RolloutStore,
    get_rollout_store,
    reset_rollout_store,
    set_rollout_store,
)

__all__ = [
    "STAGES",
    "RolloutMetrics",
    "derive_metrics",
    "wilson_lower_bound",
    "RollbackDecision",
    "evaluate_rollback",
    "apply_rollback",
    "serving_version",
    "exposure_pct",
    "StageDecision",
    "decide_stage",
    "stage_index",
    "next_stage_pct",
    "rollout_enabled",
    "set_tenant_rollout_enabled",
    "reset_rollout_state",
    "should_route_to_candidate",
    "routed_version",
    "start_rollout",
    "record_stage_metrics",
    "apply_stage_decision",
    "manual_rollback",
    "RolloutRecord",
    "RolloutStore",
    "InMemoryRolloutStore",
    "get_rollout_store",
    "set_rollout_store",
    "reset_rollout_store",
]
