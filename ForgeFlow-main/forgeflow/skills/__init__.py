"""Skill Hub — registry, versioning, candidate compiler, evaluation, gate."""

from __future__ import annotations

from forgeflow.skills.candidate_compiler import (
    compile_candidate,
    compile_candidate_or_raise,
)
from forgeflow.skills.draft_spec import DraftSpec
from forgeflow.skills.errors import (
    EvaluationError,
    GovernanceError,
    InsufficientExperiencesError,
    SkillError,
)
from forgeflow.skills.evaluator import evaluate_candidate
from forgeflow.skills.governance_gate import promote_candidate
from forgeflow.skills.models import (
    CANDIDATE_STATUSES,
    SKILL_STATUSES,
    SkillCandidateRecord,
    SkillEvaluationRecord,
    SkillRecord,
    SkillVersionRecord,
)
from forgeflow.skills.registry import SkillRegistry
from forgeflow.skills.versioning import (
    bump_semver,
    create_version,
    diff_specs,
    parse_semver,
    rollback,
)

__all__ = [
    "SkillRecord",
    "SkillVersionRecord",
    "SkillCandidateRecord",
    "SkillEvaluationRecord",
    "SKILL_STATUSES",
    "CANDIDATE_STATUSES",
    "DraftSpec",
    "SkillRegistry",
    "compile_candidate",
    "compile_candidate_or_raise",
    "evaluate_candidate",
    "promote_candidate",
    "create_version",
    "rollback",
    "diff_specs",
    "bump_semver",
    "parse_semver",
    "SkillError",
    "InsufficientExperiencesError",
    "EvaluationError",
    "GovernanceError",
]
