"""Repository factory — picks the backend from ``Settings.storage_backend``.

Concrete backends are imported lazily (inside ``_construct``) so that
``import forgeflow.repositories`` never pulls in asyncpg/psycopg. With
``STORAGE_BACKEND=memory`` the whole closed loop runs with stdlib only.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

# Process-wide singletons keyed by "backend:kind". Repositories are stateless
# besides their memory dict, which is intentional (single in-process store).
_CACHE: dict[str, Any] = {}

_VALID_BACKENDS = ("memory", "postgres")


def _backend() -> str:
    backend = get_settings().storage_backend.lower()
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown STORAGE_BACKEND '{backend}'. Expected one of: {', '.join(_VALID_BACKENDS)}"
        )
    return backend


def _construct(backend: str, kind: str) -> Any:
    """Instantiate the concrete repository for ``backend``/``kind``."""
    default_tenant = get_settings().default_tenant_id
    if backend == "memory":
        from forgeflow.repositories.memory import (
            MemoryCostBudgetRepository,
            MemoryExperienceRepository,
            MemoryPolicyRepository,
            MemorySkillCandidateRepository,
            MemorySkillRepository,
        )

        mapping: dict[str, Callable[..., Any]] = {
            "experience": MemoryExperienceRepository,
            "skill": MemorySkillRepository,
            "candidate": MemorySkillCandidateRepository,
            "policy": MemoryPolicyRepository,
            "cost": MemoryCostBudgetRepository,
        }
    else:  # postgres
        from forgeflow.repositories.postgres import (
            PgCostBudgetRepository,
            PgExperienceRepository,
            PgPolicyRepository,
            PgSkillCandidateRepository,
            PgSkillRepository,
        )

        mapping = {
            "experience": PgExperienceRepository,
            "skill": PgSkillRepository,
            "candidate": PgSkillCandidateRepository,
            "policy": PgPolicyRepository,
            "cost": PgCostBudgetRepository,
        }

    factory = mapping.get(kind)
    if factory is None:
        raise ValueError(f"unknown repository kind '{kind}'")
    return factory(default_tenant=default_tenant)


def _get(kind: str) -> Any:
    backend = _backend()
    key = f"{backend}:{kind}"
    instance = _CACHE.get(key)
    if instance is None:
        instance = _construct(backend, kind)
        _CACHE[key] = instance
        logger.debug("repository built | %s", key)
    return instance


def get_experience_repository() -> Any:
    """Return the tenant-scoped Experience repository for the active backend."""
    return _get("experience")


def get_skill_repository() -> Any:
    """Return the Skill (registry + versions) repository."""
    return _get("skill")


def get_skill_candidate_repository() -> Any:
    """Return the SkillCandidate (+ evaluations) repository."""
    return _get("candidate")


def get_policy_repository() -> Any:
    """Return the Policy (+ approvals) repository."""
    return _get("policy")


def get_cost_repository() -> Any:
    """Return the CostBudget repository (INC2 A1) for the active backend."""
    return _get("cost")


def reset_repositories() -> None:
    """Drop cached instances. Used by tests when switching STORAGE_BACKEND."""
    _CACHE.clear()
