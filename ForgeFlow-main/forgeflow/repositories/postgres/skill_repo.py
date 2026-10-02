"""PostgreSQL Skill + SkillCandidate repositories (asyncpg, lazy pool)."""

from __future__ import annotations

from typing import Any

from forgeflow.repositories.base import TenantScopedRepository, utcnow
from forgeflow.skills.models import (
    SkillCandidateRecord,
    SkillEvaluationRecord,
    SkillRecord,
    SkillVersionRecord,
)


class PgSkillRepository(TenantScopedRepository):
    """asyncpg-backed ``SkillRepository``."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    @staticmethod
    def _to_skill(row: Any) -> SkillRecord:
        d = dict(row)
        return SkillRecord(
            id=str(d["id"]),
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            name=d.get("name") or "",
            domain=d.get("domain") or "",
            owner=d.get("owner"),
            description=d.get("description") or "",
            current_version=d.get("current_version"),
            status=d.get("status") or "draft",
            usage_count=int(d.get("usage_count") or 0),
            featured=bool(d.get("featured")),
            tags=list(d.get("tags") or []),
            created_at=d.get("created_at") or utcnow(),
            updated_at=d.get("updated_at") or utcnow(),
        )

    @staticmethod
    def _to_version(row: Any) -> SkillVersionRecord:
        d = dict(row)
        return SkillVersionRecord(
            id=str(d["id"]),
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            skill_id=str(d["skill_id"]),
            semver=d.get("semver") or "0.1.0",
            spec=dict(d.get("spec") or {}),
            changelog=d.get("changelog") or "",
            eval_score=d.get("eval_score"),
            source_experience_ids=[str(x) for x in (d.get("source_experience_ids") or [])],
            approved_by=d.get("approved_by"),
            created_at=d.get("created_at") or utcnow(),
        )

    async def create_skill(self, skill: SkillRecord) -> SkillRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skills
                  (id, tenant_id, name, domain, owner, description, current_version,
                   status, usage_count, featured, tags, created_at, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (id) DO UPDATE SET
                  name=EXCLUDED.name, domain=EXCLUDED.domain, owner=EXCLUDED.owner,
                  description=EXCLUDED.description, current_version=EXCLUDED.current_version,
                  status=EXCLUDED.status, usage_count=EXCLUDED.usage_count,
                  featured=EXCLUDED.featured, tags=EXCLUDED.tags, updated_at=EXCLUDED.updated_at
                """,
                skill.id,
                self.scope_key(skill.tenant_id),
                skill.name,
                skill.domain,
                skill.owner,
                skill.description,
                skill.current_version,
                skill.status,
                skill.usage_count,
                skill.featured,
                skill.tags,
                skill.created_at,
                skill.updated_at,
            )
        return skill

    async def get_skill(self, tenant_id: str | None, skill_id: str) -> SkillRecord | None:
        # INC43 §3.4 / BE-5 — fail-closed: an unresolved tenant reads NOTHING
        # (never the shared "default" bucket, never every tenant's rows).
        if not tenant_id:
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM skills WHERE id=$1 AND tenant_id IS NOT DISTINCT FROM $2",
                skill_id,
                self.scope_key(tenant_id),
            )
        return self._to_skill(row) if row else None

    async def get_skill_by_name(self, tenant_id: str | None, name: str) -> SkillRecord | None:
        if not tenant_id:  # BE-5 fail-closed
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM skills WHERE name=$1 AND tenant_id IS NOT DISTINCT FROM $2",
                name,
                self.scope_key(tenant_id),
            )
        return self._to_skill(row) if row else None

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
        if not tenant_id:  # BE-5 fail-closed (discovery returns empty, not all)
            return [], 0
        pool = await self._get_pool()
        clauses = ["tenant_id IS NOT DISTINCT FROM $1"]
        args: list[Any] = [self.scope_key(tenant_id)]
        if domain:
            args.append(domain)
            clauses.append(f"domain = ${len(args)}")
        if featured:
            clauses.append("featured = TRUE")
        if q:
            args.append(f"%{q.lower()}%")
            clauses.append(f"(lower(name) LIKE ${len(args)} OR lower(description) LIKE ${len(args)})")
        where = " AND ".join(clauses)
        async with pool.acquire() as conn:
            total_row = await conn.fetchrow(f"SELECT count(*) AS c FROM skills WHERE {where}", *args)
            args_page = list(args) + [limit, offset]
            rows = await conn.fetch(
                f"SELECT * FROM skills WHERE {where} "
                f"ORDER BY featured DESC, usage_count DESC, created_at DESC "
                f"LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}",
                *args_page,
            )
        total = int(total_row["c"]) if total_row else 0
        return [self._to_skill(r) for r in rows], total

    async def update_skill(self, skill: SkillRecord) -> SkillRecord:
        skill.updated_at = utcnow()
        return await self.create_skill(skill)

    async def add_version(
        self, tenant_id: str | None, version: SkillVersionRecord
    ) -> SkillVersionRecord:
        # INC43 §3.4 / BE-5 — fail-closed write: an unresolved tenant persists
        # nothing (never into the shared "default" bucket).
        if not tenant_id:
            return version
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_versions
                  (id, tenant_id, skill_id, semver, spec, changelog, eval_score,
                   source_experience_ids, approved_by, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT (skill_id, semver) DO NOTHING
                """,
                version.id,
                self.scope_key(tenant_id),
                version.skill_id,
                version.semver,
                version.spec,
                version.changelog,
                version.eval_score,
                list(version.source_experience_ids),
                version.approved_by,
                version.created_at,
            )
        return version

    async def list_versions(
        self, tenant_id: str | None, skill_id: str
    ) -> list[SkillVersionRecord]:
        """List a skill's versions, **tenant-scoped** (INC-AUDIT P0 fix).

        The predicate mirrors ``get_skill`` / ``list_skills`` in this same class.
        Without it the route-reachable ``GET /skills/{id}/versions`` returned
        **every** tenant's rows for the id (a cross-tenant leak, reproduced with a
        two-way canary). ``IS NOT DISTINCT FROM`` keeps a NULL-tenant legacy row
        behaving exactly as the other reads do.
        """
        if not tenant_id:  # BE-5 fail-closed
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM skill_versions WHERE skill_id=$1 "
                "AND tenant_id IS NOT DISTINCT FROM $2 "
                "ORDER BY created_at DESC",
                skill_id,
                self.scope_key(tenant_id),
            )
        return [self._to_version(r) for r in rows]

    async def get_version(
        self, tenant_id: str | None, skill_id: str, semver: str
    ) -> SkillVersionRecord | None:
        """Fetch one version, tenant-scoped (same遗漏型 fix as ``list_versions``).

        Not currently route-reachable with a foreign tenant, but it is the same
        omission and would leak the moment a caller passes a skill id it does not
        own (e.g. a future shares/canary endpoint).
        """
        if not tenant_id:  # BE-5 fail-closed
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM skill_versions WHERE skill_id=$1 AND semver=$2 "
                "AND tenant_id IS NOT DISTINCT FROM $3",
                skill_id,
                semver,
                self.scope_key(tenant_id),
            )
        return self._to_version(row) if row else None


class PgSkillCandidateRepository(TenantScopedRepository):
    """asyncpg-backed ``SkillCandidateRepository``."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    @staticmethod
    def _to_candidate(row: Any) -> SkillCandidateRecord:
        d = dict(row)
        return SkillCandidateRecord(
            id=str(d["id"]),
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            name=d.get("name") or "",
            domain=d.get("domain") or "general",
            experience_ids=[str(x) for x in (d.get("experience_ids") or [])],
            draft_spec=dict(d.get("draft_spec") or {}),
            similarity_score=float(d.get("similarity_score") or 0.0),
            status=d.get("status") or "draft",
            created_at=d.get("created_at") or utcnow(),
        )

    async def save_candidate(self, candidate: SkillCandidateRecord) -> SkillCandidateRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_candidates
                  (id, tenant_id, name, domain, experience_ids, draft_spec,
                   similarity_score, status, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (id) DO UPDATE SET
                  name=EXCLUDED.name, domain=EXCLUDED.domain,
                  experience_ids=EXCLUDED.experience_ids, draft_spec=EXCLUDED.draft_spec,
                  similarity_score=EXCLUDED.similarity_score, status=EXCLUDED.status
                """,
                candidate.id,
                self.scope_key(candidate.tenant_id),
                candidate.name,
                candidate.domain,
                list(candidate.experience_ids),
                candidate.draft_spec,
                candidate.similarity_score,
                candidate.status,
                candidate.created_at,
            )
        return candidate

    async def get_candidate(
        self, tenant_id: str | None, candidate_id: str
    ) -> SkillCandidateRecord | None:
        if not tenant_id:  # BE-5 fail-closed
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM skill_candidates WHERE id=$1 AND tenant_id IS NOT DISTINCT FROM $2",
                candidate_id,
                self.scope_key(tenant_id),
            )
        return self._to_candidate(row) if row else None

    async def list_candidates(
        self,
        tenant_id: str | None,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SkillCandidateRecord]:
        if not tenant_id:  # BE-5 fail-closed (discovery returns empty, not all)
            return []
        pool = await self._get_pool()
        clauses = ["tenant_id IS NOT DISTINCT FROM $1"]
        args: list[Any] = [self.scope_key(tenant_id)]
        if status:
            args.append(status)
            clauses.append(f"status = ${len(args)}")
        args += [limit, offset]
        sql = (
            "SELECT * FROM skill_candidates WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [self._to_candidate(r) for r in rows]

    async def link_experience(
        self,
        tenant_id: str | None,
        candidate_id: str,
        experience_id: str,
        similarity: float = 0.0,
    ) -> None:
        # INC43 §3.4 / BE-5 — fail-closed write: an unresolved tenant links
        # nothing (never into the shared "default" bucket).
        if not tenant_id:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO candidate_experience (candidate_id, experience_id, similarity)
                VALUES ($1,$2,$3)
                ON CONFLICT (candidate_id, experience_id) DO NOTHING
                """,
                candidate_id,
                experience_id,
                float(similarity),
            )

    async def list_candidate_experiences(
        self, tenant_id: str | None, candidate_id: str
    ) -> list[str]:
        """The experience ids linked to ``candidate_id`` **within the tenant** (F-121).

        ``candidate_experience`` has **no** ``tenant_id`` column (migration
        ``010``), so the tenant predicate cannot be applied to it directly — it is
        applied to the owning ``skill_candidates`` row via an inner JOIN. A caller
        from tenant A therefore can never read a candidate B's linked experiences
        (the row silently yields nothing), matching the sibling reads'
        ``tenant_id IS NOT DISTINCT FROM $n`` discipline.
        """
        if not tenant_id:  # BE-5 fail-closed
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ce.experience_id
                FROM candidate_experience ce
                JOIN skill_candidates sc ON sc.id = ce.candidate_id
                WHERE ce.candidate_id = $1
                  AND sc.tenant_id IS NOT DISTINCT FROM $2
                """,
                candidate_id,
                self.scope_key(tenant_id),
            )
        return [str(r["experience_id"]) for r in rows]

    async def save_evaluation(self, evaluation: SkillEvaluationRecord) -> SkillEvaluationRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_evaluations
                  (id, tenant_id, target_id, dataset, metrics, verdict, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (id) DO NOTHING
                """,
                evaluation.id,
                self.scope_key(evaluation.tenant_id),
                evaluation.target_id,
                evaluation.dataset,
                evaluation.metrics,
                evaluation.verdict,
                evaluation.created_at,
            )
        return evaluation

    async def get_evaluation_for(
        self, tenant_id: str | None, target_id: str
    ) -> SkillEvaluationRecord | None:
        """Latest evaluation for a target, **tenant-scoped** (INC-AUDIT fix).

        The candidate-side twin of the ``skill_versions`` omission: the same
        ``tenant_id IS NOT DISTINCT FROM $n`` predicate the sibling reads
        (``get_candidate`` / ``list_candidates``) already carry.
        """
        if not tenant_id:  # BE-5 fail-closed
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM skill_evaluations WHERE target_id=$1 "
                "AND tenant_id IS NOT DISTINCT FROM $2 "
                "ORDER BY created_at DESC LIMIT 1",
                target_id,
                self.scope_key(tenant_id),
            )
        if not row:
            return None
        d = dict(row)
        return SkillEvaluationRecord(
            id=str(d["id"]),
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            target_id=str(d["target_id"]),
            dataset=d.get("dataset"),
            metrics=dict(d.get("metrics") or {}),
            verdict=d.get("verdict") or "pending",
            created_at=d.get("created_at") or utcnow(),
        )
