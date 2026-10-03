"""Skill-contract schema repository — persistence for the T10 eleven tables.

House pattern (``repositories/base.py`` + ``repositories/eval_sample_repo.py``):
a ``Protocol`` both backends implement, ``tenant_id`` as the **first positional
argument** of every read/write so row-level isolation can never be forgotten, and
an in-process dict implementation for the offline profile. The PG implementation
lives in ``repositories/postgres/skill_schema_repo.py``.

What it backs
-------------
The eleven tables migration ``023`` introduces/extend (INC46 T10), one row per
contract element:

``skill_steps`` / ``skill_tools`` / ``skill_policies`` / ``skill_evaluations`` /
``skill_test_cases`` / ``skill_test_runs`` / ``skill_experiences`` /
``skill_feedback`` / ``skill_permissions`` / ``skill_knowledge_refs`` /
``skill_examples``.

``skill_feedback`` here is feedback about the **Skill itself**; the per-*run*
signals belong to T16's ``feedback_events`` and are deliberately never merged.

Honesty (red line 4)
--------------------
All *measured* numbers (``skill_test_runs.duration_ms`` / ``score``,
``skill_experiences.similarity``, ``skill_feedback.rating``) are ``None`` when no
measurement happened — never a fabricated ``0``. The dataclass defaults encode
exactly that: ``None``, not ``0.0``.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from forgeflow.repositories.base import TenantScopedRepository, new_id, utcnow

__all__ = [
    "SkillStep",
    "SkillTool",
    "SkillPolicy",
    "SkillEvaluationEntry",
    "SkillTestCase",
    "SkillTestRun",
    "SkillExperience",
    "SkillFeedback",
    "SkillPermission",
    "SkillKnowledgeRef",
    "SkillExample",
    "SkillSchemaRepository",
    "MemorySkillSchemaRepository",
    "SKILL_SCHEMA_TABLES",
    "clear_skill_schema_store",
]

#: The eleven table names, in contract order. Used by tests and by the
#: store-clearing helper so the set stays in one obvious place.
SKILL_SCHEMA_TABLES: tuple[str, ...] = (
    "skill_steps",
    "skill_tools",
    "skill_policies",
    "skill_evaluations",
    "skill_test_cases",
    "skill_test_runs",
    "skill_experiences",
    "skill_feedback",
    "skill_permissions",
    "skill_knowledge_refs",
    "skill_examples",
)


# --------------------------------------------------------------------------- #
# records — one dataclass per table (mirrors a row)                            #
# --------------------------------------------------------------------------- #
@dataclass
class SkillStep:
    """``skill_steps`` — Procedure segment (one ordered step)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    step_index: int = 0
    text: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillTool:
    """``skill_tools`` — Tool-bindings segment (``kind`` ∈ tool|script)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    kind: str = "tool"
    ref: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillPolicy:
    """``skill_policies`` — Policies segment (``kind`` ∈ constraint|allowed_tool)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    kind: str = "constraint"
    text: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillEvaluationEntry:
    """``skill_evaluations`` — Evaluation segment grafted onto the 010 table.

    ``target_id`` is the pre-existing **NOT NULL** column; when a caller does not
    supply one a fresh UUID is generated so the row can exist (the pre-010
    column semantics are untouched). ``segment_ref`` / ``segment_note`` carry the
    contract's ``evaluation.ref`` / ``evaluation.note``.
    """

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    target_id: str = field(default_factory=new_id)
    segment_ref: str | None = None
    segment_note: str | None = None
    dataset: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    verdict: str = "pending"
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillTestCase:
    """``skill_test_cases`` — T11 fixtures for a version."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    ordinal: int = 0
    name: str = ""
    case_input: dict[str, Any] = field(default_factory=dict)
    #: ``None`` when the case declares no expectation (未测量 ⇒ NULL).
    expected: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        # The DB column is ``input``; expose it under both names for callers.
        d["input"] = d.pop("case_input")
        return d


@dataclass
class SkillTestRun:
    """``skill_test_runs`` — one case execution (measured numbers may be NULL)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    case_id: str | None = None
    run_id: str | None = None
    status: str = "pass"
    passed: bool = False
    #: Measured ⇒ ``None`` when no timing was taken (never a fabricated ``0``).
    duration_ms: float | None = None
    #: Measured ⇒ ``None`` when unmeasured (red line 4).
    score: float | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillExperience:
    """``skill_experiences`` — N:M link version ↦ source experience."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    experience_id: str = ""
    relation: str | None = None
    #: Measured ⇒ ``None`` when the link carries no similarity (never ``0``).
    similarity: float | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillFeedback:
    """``skill_feedback`` — feedback about the **Skill itself** (not a run)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    #: Measured ⇒ ``None`` when no rating was given (never ``0``).
    rating: float | None = None
    signal: str | None = None
    comment: str | None = None
    actor_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillPermission:
    """``skill_permissions`` — a declared tool permission (A12 four-level)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    tool: str = ""
    level: str = "read"
    granted: bool = False
    note: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillKnowledgeRef:
    """``skill_knowledge_refs`` — Knowledge segment (``references/*.md``)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    ordinal: int = 0
    file: str = ""
    #: ``None`` when the author gave no summary (未测量 ⇒ NULL, never '').
    summary: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class SkillExample:
    """``skill_examples`` — Examples segment (one inline example)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    version: str = ""
    ordinal: int = 0
    text: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d


# --------------------------------------------------------------------------- #
# Protocol — identical signatures for memory and postgres                       #
# --------------------------------------------------------------------------- #
@runtime_checkable
class SkillSchemaRepository(Protocol):
    """Read/write access to the T10 skill-contract tables.

    Every read/write takes ``tenant_id`` first (possibly ``None`` → the
    configured default bucket). Reads are tenant-scoped; a foreign tenant never
    observes another tenant's rows.
    """

    async def save_step(self, tenant_id: str | None, step: SkillStep) -> SkillStep: ...
    async def list_steps(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillStep]: ...

    async def save_tool(self, tenant_id: str | None, tool: SkillTool) -> SkillTool: ...
    async def list_tools(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTool]: ...

    async def save_policy(
        self, tenant_id: str | None, policy: SkillPolicy
    ) -> SkillPolicy: ...
    async def list_policies(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillPolicy]: ...

    async def save_evaluation(
        self, tenant_id: str | None, evaluation: SkillEvaluationEntry
    ) -> SkillEvaluationEntry: ...
    async def list_evaluations(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillEvaluationEntry]: ...

    async def save_test_case(
        self, tenant_id: str | None, case: SkillTestCase
    ) -> SkillTestCase: ...
    async def list_test_cases(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTestCase]: ...

    async def save_test_run(
        self, tenant_id: str | None, run: SkillTestRun
    ) -> SkillTestRun: ...
    async def list_test_runs(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTestRun]: ...

    async def save_experience(
        self, tenant_id: str | None, experience: SkillExperience
    ) -> SkillExperience: ...
    async def list_experiences(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillExperience]: ...

    async def save_feedback(
        self, tenant_id: str | None, feedback: SkillFeedback
    ) -> SkillFeedback: ...
    async def list_feedback(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillFeedback]: ...

    async def save_permission(
        self, tenant_id: str | None, permission: SkillPermission
    ) -> SkillPermission: ...
    async def list_permissions(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillPermission]: ...

    async def save_knowledge_ref(
        self, tenant_id: str | None, reference: SkillKnowledgeRef
    ) -> SkillKnowledgeRef: ...
    async def list_knowledge_refs(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillKnowledgeRef]: ...

    async def save_example(
        self, tenant_id: str | None, example: SkillExample
    ) -> SkillExample: ...
    async def list_examples(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillExample]: ...


# --------------------------------------------------------------------------- #
# in-process implementation (offline profile)                                   #
# --------------------------------------------------------------------------- #
#: table name → list of records (all tenants; reads filter by scope). The dict
#: is process-global on purpose: a single in-process store, like the sibling
#: memory repositories.
_STORE: dict[str, list[Any]] = {name: [] for name in SKILL_SCHEMA_TABLES}
_LOCK = asyncio.Lock()


def clear_skill_schema_store() -> None:
    """Reset all in-memory rows. Test helper only."""
    for rows in _STORE.values():
        rows.clear()


class MemorySkillSchemaRepository(TenantScopedRepository):
    """Dict-backed ``SkillSchemaRepository`` (offline / zero-dependency mode)."""

    # -- internal helpers -------------------------------------------------- #
    async def _insert(self, table: str, tenant_id: str | None, record: Any) -> Any:
        """Insert one record into ``table``'s partition, stamping its tenant."""
        if record.tenant_id is None:
            record.tenant_id = self.scope_key(tenant_id)
        async with _LOCK:
            _STORE.setdefault(table, []).append(record)
        return record

    def _select(
        self,
        table: str,
        tenant_id: str | None,
        *,
        skill_id: str | None,
        version: str | None,
        limit: int,
    ) -> list[Any]:
        """Return ``table`` rows for the tenant, filtered and newest-first."""
        scope = self.scope_key(tenant_id)
        rows = [r for r in _STORE.get(table, []) if self.scope_key(r.tenant_id) == scope]
        if skill_id is not None:
            rows = [r for r in rows if r.skill_id == skill_id]
        if version is not None:
            rows = [r for r in rows if r.version == version]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    # -- skill_steps ------------------------------------------------------- #
    async def save_step(self, tenant_id: str | None, step: SkillStep) -> SkillStep:
        return await self._insert("skill_steps", tenant_id, step)

    async def list_steps(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillStep]:
        rows = self._select(
            "skill_steps", tenant_id, skill_id=skill_id, version=version, limit=limit
        )
        rows.sort(key=lambda r: r.step_index)
        return rows

    # -- skill_tools ------------------------------------------------------- #
    async def save_tool(self, tenant_id: str | None, tool: SkillTool) -> SkillTool:
        return await self._insert("skill_tools", tenant_id, tool)

    async def list_tools(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTool]:
        return self._select(
            "skill_tools", tenant_id, skill_id=skill_id, version=version, limit=limit
        )

    # -- skill_policies ---------------------------------------------------- #
    async def save_policy(
        self, tenant_id: str | None, policy: SkillPolicy
    ) -> SkillPolicy:
        return await self._insert("skill_policies", tenant_id, policy)

    async def list_policies(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillPolicy]:
        return self._select(
            "skill_policies", tenant_id, skill_id=skill_id, version=version, limit=limit
        )

    # -- skill_evaluations ------------------------------------------------- #
    async def save_evaluation(
        self, tenant_id: str | None, evaluation: SkillEvaluationEntry
    ) -> SkillEvaluationEntry:
        return await self._insert("skill_evaluations", tenant_id, evaluation)

    async def list_evaluations(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillEvaluationEntry]:
        return self._select(
            "skill_evaluations", tenant_id, skill_id=skill_id,
            version=version, limit=limit,
        )

    # -- skill_test_cases -------------------------------------------------- #
    async def save_test_case(
        self, tenant_id: str | None, case: SkillTestCase
    ) -> SkillTestCase:
        return await self._insert("skill_test_cases", tenant_id, case)

    async def list_test_cases(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTestCase]:
        rows = self._select(
            "skill_test_cases", tenant_id, skill_id=skill_id,
            version=version, limit=limit,
        )
        rows.sort(key=lambda r: r.ordinal)
        return rows

    # -- skill_test_runs --------------------------------------------------- #
    async def save_test_run(
        self, tenant_id: str | None, run: SkillTestRun
    ) -> SkillTestRun:
        return await self._insert("skill_test_runs", tenant_id, run)

    async def list_test_runs(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillTestRun]:
        return self._select(
            "skill_test_runs", tenant_id, skill_id=skill_id,
            version=version, limit=limit,
        )

    # -- skill_experiences ------------------------------------------------- #
    async def save_experience(
        self, tenant_id: str | None, experience: SkillExperience
    ) -> SkillExperience:
        return await self._insert("skill_experiences", tenant_id, experience)

    async def list_experiences(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillExperience]:
        return self._select(
            "skill_experiences", tenant_id, skill_id=skill_id,
            version=version, limit=limit,
        )

    # -- skill_feedback ---------------------------------------------------- #
    async def save_feedback(
        self, tenant_id: str | None, feedback: SkillFeedback
    ) -> SkillFeedback:
        return await self._insert("skill_feedback", tenant_id, feedback)

    async def list_feedback(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillFeedback]:
        return self._select(
            "skill_feedback", tenant_id, skill_id=skill_id,
            version=version, limit=limit,
        )

    # -- skill_permissions ------------------------------------------------- #
    async def save_permission(
        self, tenant_id: str | None, permission: SkillPermission
    ) -> SkillPermission:
        return await self._insert("skill_permissions", tenant_id, permission)

    async def list_permissions(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillPermission]:
        return self._select(
            "skill_permissions", tenant_id, skill_id=skill_id,
            version=version, limit=limit,
        )

    # -- skill_knowledge_refs ---------------------------------------------- #
    async def save_knowledge_ref(
        self, tenant_id: str | None, reference: SkillKnowledgeRef
    ) -> SkillKnowledgeRef:
        return await self._insert("skill_knowledge_refs", tenant_id, reference)

    async def list_knowledge_refs(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillKnowledgeRef]:
        rows = self._select(
            "skill_knowledge_refs", tenant_id, skill_id=skill_id,
            version=version, limit=limit,
        )
        rows.sort(key=lambda r: r.ordinal)
        return rows

    # -- skill_examples ---------------------------------------------------- #
    async def save_example(
        self, tenant_id: str | None, example: SkillExample
    ) -> SkillExample:
        return await self._insert("skill_examples", tenant_id, example)

    async def list_examples(
        self, tenant_id: str | None, skill_id: str | None = None,
        version: str | None = None, limit: int = 200,
    ) -> list[SkillExample]:
        rows = self._select(
            "skill_examples", tenant_id, skill_id=skill_id,
            version=version, limit=limit,
        )
        rows.sort(key=lambda r: r.ordinal)
        return rows
