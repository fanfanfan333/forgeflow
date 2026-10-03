"""PostgreSQL ``SkillSchemaRepository`` — asyncpg, lazy pool (INC46 T10).

Talks to the eleven tables migration ``023`` introduces / extends. ``tenant_id``
is opaque ``TEXT`` (migration ``013``): stored exactly as the application hands
it, normalised through ``scope_key`` (``None`` → the ``"default"`` bucket), and
reads use ``IS NOT DISTINCT FROM`` so the literal string ``"default"``
round-trips. There is no UUID coercion.

JSONB binding
-------------
Pass Python objects (dicts) — **not** ``json.dumps(...)`` — as ``$n::jsonb``
bindings: the pool registers a JSON/JSONB codec (``forgeflow.database.
_init_connection``), so a pre-serialised string would be double-encoded into a
JSON *string* rather than an object (verified against the live dev DB).

Tenant predicate
----------------
The tenant filter is produced by :meth:`PgSkillSchemaRepository._tenant_where`
so its removal is a single, reviewable edit — and so the counterfactual probe in
``tests/integration/test_inc46_skill_schema_pg.py`` can drop the discriminator in
one place and observe the isolation assertions go red.
"""

from __future__ import annotations

from typing import Any, Callable

from forgeflow.repositories.base import TenantScopedRepository
from forgeflow.repositories.skill_schema_repo import (
    SkillEvaluationEntry,
    SkillExample,
    SkillExperience,
    SkillFeedback,
    SkillKnowledgeRef,
    SkillPermission,
    SkillPolicy,
    SkillStep,
    SkillTestCase,
    SkillTestRun,
    SkillTool,
)


class PgSkillSchemaRepository(TenantScopedRepository):
    """asyncpg-backed ``SkillSchemaRepository``."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    # ------------------------------------------------------------------ #
    # tenant predicate (single edit site — see the module docstring)       #
    # ------------------------------------------------------------------ #
    def _tenant_where(self) -> str:
        """The tenant discriminator used by every read / write predicate."""
        return "tenant_id IS NOT DISTINCT FROM $1"

    # ------------------------------------------------------------------ #
    # shared read helper                                                   #
    # ------------------------------------------------------------------ #
    async def _list(
        self,
        table: str,
        columns: str,
        order_by: str,
        tenant_id: str | None,
        skill_id: str | None,
        version: str | None,
        limit: int,
        factory: Callable[[Any], Any],
    ) -> list[Any]:
        pool = await self._get_pool()
        args: list[Any] = [self.scope_key(tenant_id)]
        clauses = [self._tenant_where()]
        if skill_id is not None:
            args.append(skill_id)
            clauses.append(f"skill_id = ${len(args)}")
        if version is not None:
            args.append(version)
            clauses.append(f"version = ${len(args)}")
        args.append(int(limit))
        sql = (
            f"SELECT {columns} FROM {table} WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY {order_by} LIMIT ${len(args)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [factory(r) for r in rows]

    # ------------------------------------------------------------------ #
    # row → record factories                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _s(value: Any) -> str:
        return str(value) if value is not None else ""

    @staticmethod
    def _opt(value: Any) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _f(value: Any) -> float | None:
        return float(value) if value is not None else None

    def _to_step(self, row: Any) -> SkillStep:
        d = dict(row)
        return SkillStep(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            step_index=int(d.get("step_index") or 0), text=self._s(d.get("text")),
            created_at=d.get("created_at"),
        )

    def _to_tool(self, row: Any) -> SkillTool:
        d = dict(row)
        return SkillTool(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            kind=self._s(d.get("kind")) or "tool", ref=self._s(d.get("ref")),
            created_at=d.get("created_at"),
        )

    def _to_policy(self, row: Any) -> SkillPolicy:
        d = dict(row)
        return SkillPolicy(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            kind=self._s(d.get("kind")) or "constraint", text=self._s(d.get("text")),
            created_at=d.get("created_at"),
        )

    def _to_evaluation(self, row: Any) -> SkillEvaluationEntry:
        d = dict(row)
        return SkillEvaluationEntry(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            target_id=str(d["target_id"]) if d.get("target_id") is not None else "",
            segment_ref=self._opt(d.get("segment_ref")),
            segment_note=self._opt(d.get("segment_note")),
            dataset=self._opt(d.get("dataset")),
            metrics=dict(d.get("metrics") or {}), verdict=self._s(d.get("verdict")) or "pending",
            created_at=d.get("created_at"),
        )

    def _to_test_case(self, row: Any) -> SkillTestCase:
        d = dict(row)
        return SkillTestCase(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            ordinal=int(d.get("ordinal") or 0), name=self._s(d.get("name")),
            case_input=dict(d.get("input") or {}),
            expected=dict(d["expected"]) if d.get("expected") is not None else None,
            created_at=d.get("created_at"),
        )

    def _to_test_run(self, row: Any) -> SkillTestRun:
        d = dict(row)
        return SkillTestRun(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            case_id=self._opt(d.get("case_id")), run_id=self._opt(d.get("run_id")),
            status=self._s(d.get("status")) or "pass", passed=bool(d.get("passed")),
            duration_ms=self._f(d.get("duration_ms")), score=self._f(d.get("score")),
            detail=dict(d.get("detail") or {}), created_at=d.get("created_at"),
        )

    def _to_experience(self, row: Any) -> SkillExperience:
        d = dict(row)
        return SkillExperience(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            experience_id=self._s(d.get("experience_id")), relation=self._opt(d.get("relation")),
            similarity=self._f(d.get("similarity")), created_at=d.get("created_at"),
        )

    def _to_feedback(self, row: Any) -> SkillFeedback:
        d = dict(row)
        return SkillFeedback(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            rating=self._f(d.get("rating")), signal=self._opt(d.get("signal")),
            comment=self._opt(d.get("comment")), actor_id=self._opt(d.get("actor_id")),
            created_at=d.get("created_at"),
        )

    def _to_permission(self, row: Any) -> SkillPermission:
        d = dict(row)
        return SkillPermission(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            tool=self._s(d.get("tool")), level=self._s(d.get("level")) or "read",
            granted=bool(d.get("granted")), note=self._opt(d.get("note")),
            created_at=d.get("created_at"),
        )

    def _to_knowledge_ref(self, row: Any) -> SkillKnowledgeRef:
        d = dict(row)
        return SkillKnowledgeRef(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            ordinal=int(d.get("ordinal") or 0), file=self._s(d.get("file")),
            summary=self._opt(d.get("summary")), created_at=d.get("created_at"),
        )

    def _to_example(self, row: Any) -> SkillExample:
        d = dict(row)
        return SkillExample(
            id=str(d["id"]), tenant_id=self._opt(d.get("tenant_id")),
            skill_id=self._s(d.get("skill_id")), version=self._s(d.get("version")),
            ordinal=int(d.get("ordinal") or 0), text=self._s(d.get("text")),
            created_at=d.get("created_at"),
        )

    # ------------------------------------------------------------------ #
    # skill_steps                                                          #
    # ------------------------------------------------------------------ #
    async def save_step(self, tenant_id: str | None, step: SkillStep) -> SkillStep:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_steps
                  (id, tenant_id, skill_id, version, step_index, text, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, step_index=EXCLUDED.step_index,
                  text=EXCLUDED.text
                """,
                step.id, self.scope_key(tenant_id if tenant_id is not None else step.tenant_id),
                step.skill_id, step.version, int(step.step_index), step.text, step.created_at,
            )
        return step

    async def list_steps(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillStep]:
        return await self._list(
            "skill_steps",
            "id, tenant_id, skill_id, version, step_index, text, created_at",
            "step_index ASC", tenant_id, skill_id, version, limit, self._to_step,
        )

    # ------------------------------------------------------------------ #
    # skill_tools                                                          #
    # ------------------------------------------------------------------ #
    async def save_tool(self, tenant_id: str | None, tool: SkillTool) -> SkillTool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_tools
                  (id, tenant_id, skill_id, version, kind, ref, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, kind=EXCLUDED.kind, ref=EXCLUDED.ref
                """,
                tool.id, self.scope_key(tenant_id if tenant_id is not None else tool.tenant_id),
                tool.skill_id, tool.version, tool.kind, tool.ref, tool.created_at,
            )
        return tool

    async def list_tools(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTool]:
        return await self._list(
            "skill_tools", "id, tenant_id, skill_id, version, kind, ref, created_at",
            "created_at DESC", tenant_id, skill_id, version, limit, self._to_tool,
        )

    # ------------------------------------------------------------------ #
    # skill_policies                                                       #
    # ------------------------------------------------------------------ #
    async def save_policy(self, tenant_id: str | None, policy: SkillPolicy) -> SkillPolicy:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_policies
                  (id, tenant_id, skill_id, version, kind, text, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, kind=EXCLUDED.kind, text=EXCLUDED.text
                """,
                policy.id, self.scope_key(tenant_id if tenant_id is not None else policy.tenant_id),
                policy.skill_id, policy.version, policy.kind, policy.text, policy.created_at,
            )
        return policy

    async def list_policies(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillPolicy]:
        return await self._list(
            "skill_policies", "id, tenant_id, skill_id, version, kind, text, created_at",
            "created_at DESC", tenant_id, skill_id, version, limit, self._to_policy,
        )

    # ------------------------------------------------------------------ #
    # skill_evaluations (adopted table — existing + 023 columns)           #
    # ------------------------------------------------------------------ #
    async def save_evaluation(
        self, tenant_id: str | None, evaluation: SkillEvaluationEntry
    ) -> SkillEvaluationEntry:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_evaluations
                  (id, tenant_id, skill_id, version, target_id, segment_ref,
                   segment_note, dataset, metrics, verdict, created_at)
                VALUES ($1,$2,$3,$4,$5::uuid,$6,$7,$8,$9::jsonb,$10,$11)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, segment_ref=EXCLUDED.segment_ref,
                  segment_note=EXCLUDED.segment_note, dataset=EXCLUDED.dataset,
                  metrics=EXCLUDED.metrics, verdict=EXCLUDED.verdict
                """,
                evaluation.id,
                self.scope_key(
                    tenant_id if tenant_id is not None else evaluation.tenant_id
                ),
                evaluation.skill_id or None,
                evaluation.version or None,
                evaluation.target_id,
                evaluation.segment_ref,
                evaluation.segment_note,
                evaluation.dataset,
                dict(evaluation.metrics or {}),
                evaluation.verdict,
                evaluation.created_at,
            )
        return evaluation

    async def list_evaluations(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillEvaluationEntry]:
        return await self._list(
            "skill_evaluations",
            "id, tenant_id, skill_id, version, target_id, segment_ref, segment_note, "
            "dataset, metrics, verdict, created_at",
            "created_at DESC", tenant_id, skill_id, version, limit, self._to_evaluation,
        )

    # ------------------------------------------------------------------ #
    # skill_test_cases                                                     #
    # ------------------------------------------------------------------ #
    async def save_test_case(
        self, tenant_id: str | None, case: SkillTestCase
    ) -> SkillTestCase:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_test_cases
                  (id, tenant_id, skill_id, version, ordinal, name, input, expected, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, ordinal=EXCLUDED.ordinal, name=EXCLUDED.name,
                  input=EXCLUDED.input, expected=EXCLUDED.expected
                """,
                case.id, self.scope_key(tenant_id if tenant_id is not None else case.tenant_id),
                case.skill_id, case.version, int(case.ordinal), case.name,
                dict(case.case_input or {}),
                dict(case.expected) if case.expected is not None else None,
                case.created_at,
            )
        return case

    async def list_test_cases(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTestCase]:
        return await self._list(
            "skill_test_cases",
            "id, tenant_id, skill_id, version, ordinal, name, input, expected, created_at",
            "ordinal ASC", tenant_id, skill_id, version, limit, self._to_test_case,
        )

    # ------------------------------------------------------------------ #
    # skill_test_runs                                                      #
    # ------------------------------------------------------------------ #
    async def save_test_run(
        self, tenant_id: str | None, run: SkillTestRun
    ) -> SkillTestRun:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_test_runs
                  (id, tenant_id, skill_id, version, case_id, run_id, status, passed,
                   duration_ms, score, detail, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, case_id=EXCLUDED.case_id, run_id=EXCLUDED.run_id,
                  status=EXCLUDED.status, passed=EXCLUDED.passed,
                  duration_ms=EXCLUDED.duration_ms, score=EXCLUDED.score,
                  detail=EXCLUDED.detail
                """,
                run.id, self.scope_key(tenant_id if tenant_id is not None else run.tenant_id),
                run.skill_id, run.version, run.case_id, run.run_id, run.status, run.passed,
                run.duration_ms, run.score, dict(run.detail or {}), run.created_at,
            )
        return run

    async def list_test_runs(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTestRun]:
        return await self._list(
            "skill_test_runs",
            "id, tenant_id, skill_id, version, case_id, run_id, status, passed, "
            "duration_ms, score, detail, created_at",
            "created_at DESC", tenant_id, skill_id, version, limit, self._to_test_run,
        )

    # ------------------------------------------------------------------ #
    # skill_experiences                                                    #
    # ------------------------------------------------------------------ #
    async def save_experience(
        self, tenant_id: str | None, experience: SkillExperience
    ) -> SkillExperience:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_experiences
                  (id, tenant_id, skill_id, version, experience_id, relation, similarity, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, experience_id=EXCLUDED.experience_id,
                  relation=EXCLUDED.relation, similarity=EXCLUDED.similarity
                """,
                experience.id,
                self.scope_key(tenant_id if tenant_id is not None else experience.tenant_id),
                experience.skill_id, experience.version, experience.experience_id,
                experience.relation, experience.similarity, experience.created_at,
            )
        return experience

    async def list_experiences(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillExperience]:
        return await self._list(
            "skill_experiences",
            "id, tenant_id, skill_id, version, experience_id, relation, similarity, created_at",
            "created_at DESC", tenant_id, skill_id, version, limit, self._to_experience,
        )

    # ------------------------------------------------------------------ #
    # skill_feedback                                                       #
    # ------------------------------------------------------------------ #
    async def save_feedback(
        self, tenant_id: str | None, feedback: SkillFeedback
    ) -> SkillFeedback:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_feedback
                  (id, tenant_id, skill_id, version, rating, signal, comment, actor_id, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, rating=EXCLUDED.rating, signal=EXCLUDED.signal,
                  comment=EXCLUDED.comment, actor_id=EXCLUDED.actor_id
                """,
                feedback.id,
                self.scope_key(tenant_id if tenant_id is not None else feedback.tenant_id),
                feedback.skill_id, feedback.version, feedback.rating, feedback.signal,
                feedback.comment, feedback.actor_id, feedback.created_at,
            )
        return feedback

    async def list_feedback(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillFeedback]:
        return await self._list(
            "skill_feedback",
            "id, tenant_id, skill_id, version, rating, signal, comment, actor_id, created_at",
            "created_at DESC", tenant_id, skill_id, version, limit, self._to_feedback,
        )

    # ------------------------------------------------------------------ #
    # skill_permissions                                                    #
    # ------------------------------------------------------------------ #
    async def save_permission(
        self, tenant_id: str | None, permission: SkillPermission
    ) -> SkillPermission:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_permissions
                  (id, tenant_id, skill_id, version, tool, level, granted, note, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, tool=EXCLUDED.tool, level=EXCLUDED.level,
                  granted=EXCLUDED.granted, note=EXCLUDED.note
                """,
                permission.id,
                self.scope_key(tenant_id if tenant_id is not None else permission.tenant_id),
                permission.skill_id, permission.version, permission.tool, permission.level,
                permission.granted, permission.note, permission.created_at,
            )
        return permission

    async def list_permissions(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillPermission]:
        return await self._list(
            "skill_permissions",
            "id, tenant_id, skill_id, version, tool, level, granted, note, created_at",
            "created_at DESC", tenant_id, skill_id, version, limit, self._to_permission,
        )

    # ------------------------------------------------------------------ #
    # skill_knowledge_refs                                                 #
    # ------------------------------------------------------------------ #
    async def save_knowledge_ref(
        self, tenant_id: str | None, reference: SkillKnowledgeRef
    ) -> SkillKnowledgeRef:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_knowledge_refs
                  (id, tenant_id, skill_id, version, ordinal, file, summary, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, ordinal=EXCLUDED.ordinal,
                  file=EXCLUDED.file, summary=EXCLUDED.summary
                """,
                reference.id,
                self.scope_key(tenant_id if tenant_id is not None else reference.tenant_id),
                reference.skill_id, reference.version, int(reference.ordinal),
                reference.file, reference.summary, reference.created_at,
            )
        return reference

    async def list_knowledge_refs(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillKnowledgeRef]:
        return await self._list(
            "skill_knowledge_refs",
            "id, tenant_id, skill_id, version, ordinal, file, summary, created_at",
            "ordinal ASC", tenant_id, skill_id, version, limit, self._to_knowledge_ref,
        )

    # ------------------------------------------------------------------ #
    # skill_examples                                                       #
    # ------------------------------------------------------------------ #
    async def save_example(
        self, tenant_id: str | None, example: SkillExample
    ) -> SkillExample:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO skill_examples
                  (id, tenant_id, skill_id, version, ordinal, text, created_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (id) DO UPDATE SET
                  tenant_id=EXCLUDED.tenant_id, skill_id=EXCLUDED.skill_id,
                  version=EXCLUDED.version, ordinal=EXCLUDED.ordinal, text=EXCLUDED.text
                """,
                example.id, self.scope_key(tenant_id if tenant_id is not None else example.tenant_id),
                example.skill_id, example.version, int(example.ordinal), example.text,
                example.created_at,
            )
        return example

    async def list_examples(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillExample]:
        return await self._list(
            "skill_examples",
            "id, tenant_id, skill_id, version, ordinal, text, created_at",
            "ordinal ASC", tenant_id, skill_id, version, limit, self._to_example,
        )


__all__ = ["PgSkillSchemaRepository"]
