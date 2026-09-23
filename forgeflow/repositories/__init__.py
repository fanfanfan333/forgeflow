"""Repository layer — the only storage boundary the AgentFlow hubs touch.

Public surface:

  * ``get_experience_repository()`` / ``get_skill_repository()`` /
    ``get_skill_candidate_repository()`` / ``get_policy_repository()``
    — factory helpers that pick memory vs PostgreSQL from
    ``Settings.storage_backend`` (see ``factory.py``).
  * ``base`` — Protocols + the ``TenantScopedRepository`` base class.

Importing this package never constructs a DB connection, a LangGraph graph or
an LLM: concrete backends are imported lazily inside the factory functions.
"""

from __future__ import annotations

from forgeflow.repositories.base import (
    ExperienceRepository,
    PolicyRepository,
    SkillCandidateRepository,
    SkillRepository,
    TenantScopedRepository,
    new_id,
    scope_key,
    utcnow,
)
from forgeflow.repositories.factory import (
    get_experience_repository,
    get_policy_repository,
    get_skill_candidate_repository,
    get_skill_repository,
    reset_repositories,
)

__all__ = [
    "ExperienceRepository",
    "SkillRepository",
    "SkillCandidateRepository",
    "PolicyRepository",
    "TenantScopedRepository",
    "new_id",
    "scope_key",
    "utcnow",
    "get_experience_repository",
    "get_skill_repository",
    "get_skill_candidate_repository",
    "get_policy_repository",
    "reset_repositories",
]
