"""INC46 T35 — Skill 生命周期治理（去重 / 合并 / 冲突 / 淘汰）。

对外入口
========
* :mod:`forgeflow.lifecycle.state_machine` —— 状态机 ``draft → candidate → published
  → deprecated → archived``（非法跃迁 fail-closed）；
* :mod:`forgeflow.lifecycle.store`        —— 两张表（迁移 033）的双后端持久层；
* :mod:`forgeflow.lifecycle.similarity`   —— 去重 / 合并**提案**（余弦 ≥ 0.85 且
  Jaccard ≥ 0.7，不自动合并；合并保留来源链）；
* :mod:`forgeflow.lifecycle.conflicts`    —— 跨 Skill 冲突（复用 T08 接口）+ 检索降权；
* :mod:`forgeflow.lifecycle.retirement`   —— 淘汰（90 天 / 30 次 < 50%）+ 14 天宽限 + 撤销。

检索生效点
==========
状态写进 ``SkillRecord.status`` 后，T09 的
:data:`forgeflow.skills.retrieval.EXCLUDED_STATUSES`（``deprecated`` / ``archived``）
在 :func:`forgeflow.skills.retrieval.build_pool` 里**自动生效** —— 此前无该状态时
它是空操作，T35 落地状态机后才真正开始剔除。
"""

from __future__ import annotations

from forgeflow.lifecycle.conflicts import (
    CONFLICT_DOWNWEIGHT_FACTOR,
    ConflictPair,
    apply_conflict_downweighting,
    conflict_partner_ids,
    detect_skill_conflict,
    scan_conflicts,
)
from forgeflow.lifecycle.retirement import (
    GRACE_DAYS,
    IDLE_DAYS,
    MIN_RUNS,
    SUCCESS_FLOOR,
    RetirementDecision,
    build_notice,
    evaluate_retirement,
    finalize_grace,
    grace_deadline,
    grace_started_at,
    revoke_grace,
    start_grace,
    within_grace,
)
from forgeflow.lifecycle.similarity import (
    MERGE_COSINE_THRESHOLD,
    MERGE_JACCARD_THRESHOLD,
    MergeCandidate,
    approve_proposal,
    description_cosine,
    find_merge_candidates,
    plan_merged_skill,
    propose_merges,
    tool_ids,
    tool_sequence_jaccard,
)
from forgeflow.lifecycle.state_machine import (
    ALLOWED_TRANSITIONS,
    DEFAULT_STATE,
    LIFECYCLE_STATES,
    RETRIEVAL_EXCLUDED_STATES,
    IllegalTransition,
    LifecycleError,
    LifecycleService,
    apply_transition,
    can_transition,
    current_state,
    retrieval_excluded,
    validate_transition,
)
from forgeflow.lifecycle.store import (
    PROPOSAL_APPROVED,
    PROPOSAL_PROPOSED,
    PROPOSAL_REJECTED,
    InMemoryLifecycleStore,
    LifecycleEvent,
    LifecycleStore,
    LifecycleStoreError,
    MergeProposalRecord,
    PostgresLifecycleStore,
    get_lifecycle_store,
    reset_lifecycle_store,
    set_lifecycle_store,
)

__all__ = [
    # state machine
    "LIFECYCLE_STATES",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_STATE",
    "RETRIEVAL_EXCLUDED_STATES",
    "IllegalTransition",
    "LifecycleError",
    "LifecycleService",
    "can_transition",
    "validate_transition",
    "current_state",
    "apply_transition",
    "retrieval_excluded",
    # store
    "LifecycleStore",
    "InMemoryLifecycleStore",
    "PostgresLifecycleStore",
    "LifecycleEvent",
    "MergeProposalRecord",
    "LifecycleStoreError",
    "PROPOSAL_PROPOSED",
    "PROPOSAL_APPROVED",
    "PROPOSAL_REJECTED",
    "get_lifecycle_store",
    "set_lifecycle_store",
    "reset_lifecycle_store",
    # similarity / merge
    "MERGE_COSINE_THRESHOLD",
    "MERGE_JACCARD_THRESHOLD",
    "MergeCandidate",
    "tool_ids",
    "tool_sequence_jaccard",
    "description_cosine",
    "find_merge_candidates",
    "propose_merges",
    "plan_merged_skill",
    "approve_proposal",
    # conflicts
    "CONFLICT_DOWNWEIGHT_FACTOR",
    "ConflictPair",
    "detect_skill_conflict",
    "scan_conflicts",
    "conflict_partner_ids",
    "apply_conflict_downweighting",
    # retirement
    "IDLE_DAYS",
    "MIN_RUNS",
    "SUCCESS_FLOOR",
    "GRACE_DAYS",
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
