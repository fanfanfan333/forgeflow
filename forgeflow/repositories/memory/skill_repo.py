"""In-memory Skill + SkillCandidate repositories."""

from __future__ import annotations

import asyncio
from typing import Any

from forgeflow.repositories.base import TenantScopedRepository, utcnow
from forgeflow.skills.models import (
    SkillCandidateRecord,
    SkillEvaluationRecord,
    SkillRecord,
    SkillVersionRecord,
)

_SKILLS: dict[str, dict[str, SkillRecord]] = {}
_VERSIONS: dict[str, dict[str, list[SkillVersionRecord]]] = {}
_CANDIDATES: dict[str, dict[str, SkillCandidateRecord]] = {}
_EVALUATIONS: dict[str, dict[str, SkillEvaluationRecord]] = {}
_CAND_LINKS: dict[str, dict[tuple[str, str], float]] = {}
_LOCK = asyncio.Lock()


def clear_skill_store() -> None:
    """Reset all in-memory skill state. Test helper only."""
    _SKILLS.clear()
    _VERSIONS.clear()
    _CANDIDATES.clear()
    _EVALUATIONS.clear()
    _CAND_LINKS.clear()


class MemorySkillRepository(TenantScopedRepository):
    """Dict-backed ``SkillRepository``."""

    async def create_skill(self, skill: SkillRecord) -> SkillRecord:
        key = self.scope_key(skill.tenant_id)
        async with _LOCK:
            _SKILLS.setdefault(key, {})[skill.id] = skill
            _VERSIONS.setdefault(key, {}).setdefault(skill.id, [])
        return skill

    async def get_skill(self, tenant_id: str | None, skill_id: str) -> SkillRecord | None:
        return _SKILLS.get(self.scope_key(tenant_id), {}).get(skill_id)

    async def get_skill_by_name(self, tenant_id: str | None, name: str) -> SkillRecord | None:
        for skill in _SKILLS.get(self.scope_key(tenant_id), {}).values():
            if skill.name == name:
                return skill
        return None

    async def list_skills(
        self,
        tenant_id: str | None,
        *,
        domain: str | None = None,
        q: str | None = None,
        featured: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[SkillRecord], int]:
        rows = list(_SKILLS.get(self.scope_key(tenant_id), {}).values())
        if domain:
            rows = [r for r in rows if r.domain == domain]
        if featured:
            rows = [r for r in rows if r.featured]
        if q:
            needle = q.lower()
            rows = [r for r in rows if needle in r.name.lower() or needle in r.description.lower()]
        rows.sort(key=lambda r: (r.usage_count, r.created_at), reverse=True)
        total = len(rows)
        return rows[offset : offset + limit], total

    async def update_skill(self, skill: SkillRecord) -> SkillRecord:
        key = self.scope_key(skill.tenant_id)
        skill.updated_at = utcnow()
        async with _LOCK:
            _SKILLS.setdefault(key, {})[skill.id] = skill
        return skill

    async def add_version(
        self, tenant_id: str | None, version: SkillVersionRecord
    ) -> SkillVersionRecord:
        key = self.scope_key(tenant_id)
        async with _LOCK:
            _VERSIONS.setdefault(key, {}).setdefault(version.skill_id, []).append(version)
        return version

    async def list_versions(
        self, tenant_id: str | None, skill_id: str
    ) -> list[SkillVersionRecord]:
        versions = _VERSIONS.get(self.scope_key(tenant_id), {}).get(skill_id, [])
        return sorted(versions, key=lambda v: v.created_at, reverse=True)

    async def get_version(
        self, tenant_id: str | None, skill_id: str, semver: str
    ) -> SkillVersionRecord | None:
        for v in _VERSIONS.get(self.scope_key(tenant_id), {}).get(skill_id, []):
            if v.semver == semver:
                return v
        return None


class MemorySkillCandidateRepository(TenantScopedRepository):
    """Dict-backed ``SkillCandidateRepository`` (+ evaluations, N:M links)."""

    async def save_candidate(self, candidate: SkillCandidateRecord) -> SkillCandidateRecord:
        key = self.scope_key(candidate.tenant_id)
        async with _LOCK:
            _CANDIDATES.setdefault(key, {})[candidate.id] = candidate
        return candidate

    async def get_candidate(
        self, tenant_id: str | None, candidate_id: str
    ) -> SkillCandidateRecord | None:
        return _CANDIDATES.get(self.scope_key(tenant_id), {}).get(candidate_id)

    async def list_candidates(
        self,
        tenant_id: str | None,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SkillCandidateRecord]:
        rows = list(_CANDIDATES.get(self.scope_key(tenant_id), {}).values())
        if status:
            rows = [r for r in rows if r.status == status]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[offset : offset + limit]

    async def link_experience(
        self,
        tenant_id: str | None,
        candidate_id: str,
        experience_id: str,
        similarity: float = 0.0,
    ) -> None:
        key = self.scope_key(tenant_id)
        async with _LOCK:
            _CAND_LINKS.setdefault(key, {})[(candidate_id, experience_id)] = similarity

    async def list_candidate_experiences(
        self, tenant_id: str | None, candidate_id: str
    ) -> list[str]:
        links = _CAND_LINKS.get(self.scope_key(tenant_id), {})
        return [exp for (cand, exp) in links if cand == candidate_id]

    async def save_evaluation(self, evaluation: SkillEvaluationRecord) -> SkillEvaluationRecord:
        key = self.scope_key(evaluation.tenant_id)
        async with _LOCK:
            _EVALUATIONS.setdefault(key, {})[evaluation.target_id] = evaluation
        return evaluation

    async def get_evaluation_for(
        self, tenant_id: str | None, target_id: str
    ) -> SkillEvaluationRecord | None:
        return _EVALUATIONS.get(self.scope_key(tenant_id), {}).get(target_id)
