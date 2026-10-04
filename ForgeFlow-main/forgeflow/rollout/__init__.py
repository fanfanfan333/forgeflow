"""INC46 T34 — 灰度发布与自动回滚（Canary rollout & auto-rollback）。

对外入口
========
* :mod:`forgeflow.rollout.metrics`    —— 指标与 Wilson 置信下界；
* :mod:`forgeflow.rollout.store`      —— 三张表（迁移 032）的双后端持久层；
* :mod:`forgeflow.rollout.rollback`   —— 回滚触发 + 「只追加」回滚；
* :mod:`forgeflow.rollout.controller` —— 阶段推进 / 确定性分流 / 租户开关。

R7 联锁锚点见 :mod:`forgeflow.skills.auto_rollback`（按需 re-export 本包）。
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
    MIN_LABELED_RUNS,
    MIN_WINDOW_HOURS,
    STAGES,
    RolloutMetrics,
    derive_metrics,
    percentile,
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
    MetricsRecord,
    PostgresRolloutStore,
    RollbackRecord,
    RolloutError,
    RolloutRecord,
    RolloutStore,
    get_rollout_store,
    reset_rollout_store,
    set_rollout_store,
)

__all__ = [
    "STAGES",
    "MIN_LABELED_RUNS",
    "MIN_WINDOW_HOURS",
    "RolloutMetrics",
    "derive_metrics",
    "wilson_lower_bound",
    "percentile",
    "RolloutStore",
    "InMemoryRolloutStore",
    "PostgresRolloutStore",
    "RolloutRecord",
    "MetricsRecord",
    "RollbackRecord",
    "RolloutError",
    "get_rollout_store",
    "set_rollout_store",
    "reset_rollout_store",
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
]
