"""Domain models for the Skill Hub (registry, versions, candidates, evals)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.repositories.base import new_id, utcnow

# Skill lifecycle: draft -> evaluating -> published -> retired
SKILL_STATUSES = ("draft", "evaluating", "published", "retired")
CANDIDATE_STATUSES = ("draft", "evaluating", "approved", "rejected", "insufficient", "promoted")


@dataclass
class SkillRecord:
    """A reusable capability asset (``skills`` table)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    name: str = ""
    domain: str = ""
    owner: str | None = None
    description: str = ""
    current_version: str | None = None
    status: str = "draft"
    usage_count: int = 0
    featured: bool = False
    tags: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        data["updated_at"] = self.updated_at.isoformat()
        return data


@dataclass
class SkillVersionRecord:
    """An immutable, semver-tagged snapshot of a skill (``skill_versions``)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    semver: str = "0.1.0"
    spec: dict[str, Any] = field(default_factory=dict)
    changelog: str = ""
    eval_score: float | None = None
    source_experience_ids: list[str] = field(default_factory=list)
    approved_by: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


@dataclass
class SkillCandidateRecord:
    """A compiled-but-unpromoted skill draft (``skill_candidates``)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    name: str = ""
    domain: str = "general"
    experience_ids: list[str] = field(default_factory=list)
    draft_spec: dict[str, Any] = field(default_factory=dict)
    similarity_score: float = 0.0
    status: str = "draft"
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


@dataclass
class SkillEvaluationRecord:
    """A deterministic evaluation verdict for a candidate or version."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    target_id: str = ""
    dataset: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    verdict: str = "pending"  # pass | fail | pending
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data
