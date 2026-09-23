"""Repository Protocols + shared helpers.

Every hub service (Experience / Skill / Governance) reaches storage **only**
through the Protocols defined here. Two concrete implementations ship:

  * ``repositories/memory/*``   — in-process dicts, stdlib only (offline mode)
  * ``repositories/postgres/*`` — asyncpg-backed, tenant-filtered SQL

Both implement the *same* Protocol, so switching ``STORAGE_BACKEND`` never
changes a caller (docs/sop/02-ARCHITECTURE.md §3.3).

Design rules honoured here:
  * ``tenant_id`` is the first positional argument of every read/write so
    row-level isolation can never be forgotten (docs §6.1).
  * Signatures are identical across backends.
  * ``None`` tenant maps to ``Settings.default_tenant_id`` — legacy/global rows
    land in a single well-defined bucket rather than leaking everywhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "utcnow",
    "new_id",
    "scope_key",
    "TenantScopedRepository",
    "ExperienceRepository",
    "SkillRepository",
    "SkillCandidateRepository",
    "PolicyRepository",
    "CostBudgetRepository",
]


def utcnow() -> datetime:
    """Timezone-aware UTC now — the single clock for all hub records."""
    return datetime.now(timezone.utc)


def new_id() -> str:
    """UUID v4 string — identical semantics to PG's ``gen_random_uuid()``."""
    return str(uuid.uuid4())


def scope_key(tenant_id: str | None, default: str) -> str:
    """Normalise a tenant id into a non-null partition key."""
    return tenant_id or default


class TenantScopedRepository:
    """Base class enforcing tenant-scoped access.

    Concrete repositories inherit this to get the tenant-partition key and a
    defensive ``assert_same_tenant`` guard used by write paths.
    """

    def __init__(self, default_tenant: str = "default") -> None:
        self._default_tenant = default_tenant

    def scope_key(self, tenant_id: str | None) -> str:
        """Map ``tenant_id`` (possibly ``None``) to its storage partition."""
        return scope_key(tenant_id, self._default_tenant)

    def assert_same_tenant(self, tenant_id: str | None, target: str | None) -> None:
        """Raise ``PermissionError`` if a read/write would cross tenants."""
        if self.scope_key(tenant_id) != self.scope_key(target):
            raise PermissionError("cross-tenant access denied")


# --------------------------------------------------------------------------- #
# Protocols — signatures are identical for memory and postgres backends.       #
# --------------------------------------------------------------------------- #

@runtime_checkable
class ExperienceRepository(Protocol):
    """Persistence for the first-class ``Experience`` asset + Memory N:M links."""

    async def save(self, record: Any) -> Any:
        """Insert or replace an experience (by ``record.id``). Returns it back."""
        ...

    async def get(self, tenant_id: str | None, experience_id: str) -> Any | None:
        """Fetch one experience, tenant-scoped. ``None`` when not found."""
        ...

    async def list(
        self,
        tenant_id: str | None,
        *,
        outcome: str | None = None,
        tag: str | None = None,
        run_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Any]:
        """List experiences (newest first) with optional filters."""
        ...

    async def find_similar(
        self,
        tenant_id: str | None,
        embedding: list[float] | None,
        *,
        k: int = 10,
        min_similarity: float = 0.85,
        tags: list[str] | None = None,
    ) -> list[tuple[Any, float]]:
        """Return ``(record, similarity)`` pairs >= ``min_similarity``.

        Falls back to tag/keyword overlap when ``embedding`` is ``None``.
        """
        ...

    async def link_memory(
        self,
        tenant_id: str | None,
        experience_id: str,
        memory_id: str,
        relation: str = "source",
    ) -> None:
        """Create the N:M ``experience_memory`` link (idempotent)."""
        ...

    async def list_memories(self, tenant_id: str | None, experience_id: str) -> list[str]:
        """Memory ids linked to an experience."""
        ...

    async def count(self, tenant_id: str | None) -> int:
        """Total experiences for a tenant (KPI + gate thresholds)."""
        ...


@runtime_checkable
class SkillRepository(Protocol):
    """Persistence for ``skills`` + ``skill_versions``."""

    async def create_skill(self, skill: Any) -> Any: ...
    async def get_skill(self, tenant_id: str | None, skill_id: str) -> Any | None: ...
    async def get_skill_by_name(self, tenant_id: str | None, name: str) -> Any | None: ...
    async def list_skills(
        self,
        tenant_id: str | None,
        *,
        domain: str | None = None,
        q: str | None = None,
        featured: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Any], int]: ...
    async def update_skill(self, skill: Any) -> Any: ...
    async def add_version(self, tenant_id: str | None, version: Any) -> Any: ...
    async def list_versions(self, tenant_id: str | None, skill_id: str) -> list[Any]: ...
    async def get_version(
        self, tenant_id: str | None, skill_id: str, semver: str
    ) -> Any | None: ...


@runtime_checkable
class SkillCandidateRepository(Protocol):
    """Persistence for ``skill_candidates`` + ``skill_evaluations`` + N:M links."""

    async def save_candidate(self, candidate: Any) -> Any: ...
    async def get_candidate(self, tenant_id: str | None, candidate_id: str) -> Any | None: ...
    async def list_candidates(
        self,
        tenant_id: str | None,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Any]: ...
    async def link_experience(
        self,
        tenant_id: str | None,
        candidate_id: str,
        experience_id: str,
        similarity: float = 0.0,
    ) -> None: ...
    async def list_candidate_experiences(
        self, tenant_id: str | None, candidate_id: str
    ) -> list[str]: ...
    async def save_evaluation(self, evaluation: Any) -> Any: ...
    async def get_evaluation_for(
        self, tenant_id: str | None, target_id: str
    ) -> Any | None: ...


@runtime_checkable
class PolicyRepository(Protocol):
    """Persistence for ``policies`` + ``approval_requests`` (HITL)."""

    async def save_policy(self, policy: Any) -> Any: ...
    async def list_policies(
        self,
        tenant_id: str | None,
        *,
        subject: str | None = None,
        resource: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Any]: ...
    async def get_policy(self, tenant_id: str | None, policy_id: str) -> Any | None: ...
    async def save_approval(self, approval: Any) -> Any: ...
    async def get_approval(self, tenant_id: str | None, approval_id: str) -> Any | None: ...
    async def list_approvals(
        self,
        tenant_id: str | None,
        *,
        status: str | None = None,
        risk_level: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Any]: ...


@runtime_checkable
class CostBudgetRepository(Protocol):
    """Persistence for ``cost_budgets`` (INC2 A1).

    ``tenant_id`` is the first argument of every method, per the house rule.
    ``scope`` ∈ ``tenant | team | task``; ``scope_id`` is ``None`` for the
    tenant level. An upsert is keyed by ``(tenant_id, scope, scope_id)``.
    """

    async def get(
        self,
        tenant_id: str | None,
        scope: str,
        scope_id: str | None = None,
    ) -> Any | None:
        """Fetch one budget, tenant-scoped. ``None`` when not defined."""
        ...

    async def upsert(self, budget: Any) -> Any:
        """Insert or update a budget by its natural key. Returns it back."""
        ...

    async def list_by_scope(
        self,
        tenant_id: str | None,
        scope: str | None = None,
    ) -> list[Any]:
        """List a tenant's budgets, optionally filtered to one scope."""
        ...
