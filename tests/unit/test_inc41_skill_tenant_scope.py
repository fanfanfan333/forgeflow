"""INC-41 F-121 — ``list_candidate_experiences`` is tenant-scoped via a JOIN.

Root cause (round-4 acceptance, F-121 P2): ``candidate_experience`` has **no**
``tenant_id`` column (migration 010), so the read used a bare
``WHERE candidate_id=$1`` — a caller from tenant A could read candidate B's
linked experience ids. The fix applies the sibling repositories' discipline
(``tenant_id IS NOT DISTINCT FROM $n``) to the owning ``skill_candidates`` row
through an **inner JOIN**, so a foreign-tenant read silently yields nothing.

The fake pool simulates the JOIN's row filter, giving a real positive/negative
control without a PostgreSQL server.

Citation discipline: ``file.py::symbol`` anchors, never line numbers.
"""

from __future__ import annotations

from typing import Any

import pytest

from forgeflow.repositories.postgres.skill_repo import PgSkillCandidateRepository

pytestmark = pytest.mark.asyncio

_DEFAULT_TENANT = "default"


class _FakeConn:
    """Simulates the JOIN's row filter over an in-memory candidate store.

    ``store`` maps ``candidate_id -> (owner_tenant, [experience_id, ...])`` —
    exactly the ``skill_candidates`` ownership the real JOIN reads. A read whose
    bound tenant does not own the candidate returns no rows (the predicate).
    """

    def __init__(
        self,
        store: dict[str, tuple[str, list[str]]],
        captured: list[tuple[str, str, tuple]],
    ) -> None:
        self._store = store
        self._captured = captured

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self._captured.append(("fetchrow", sql, args))
        return None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self._captured.append(("fetch", sql, args))
        candidate_id, tenant_id = args[0], args[1]
        entry = self._store.get(candidate_id)
        if entry is None:
            return []
        owner_tenant, experiences = entry
        if owner_tenant != tenant_id:
            return []
        return [{"experience_id": exp} for exp in experiences]


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


def _repo(
    store: dict[str, tuple[str, list[str]]],
    captured: list[tuple[str, str, tuple]],
) -> PgSkillCandidateRepository:
    return PgSkillCandidateRepository(
        default_tenant=_DEFAULT_TENANT, pool=_FakePool(_FakeConn(store, captured))
    )


# --------------------------------------------------------------------------- #
# Positive / negative control                                                  #
# --------------------------------------------------------------------------- #
async def test_same_tenant_reads_linked_experiences():
    store = {"cand-1": ("tenant-a", ["exp-1", "exp-2"])}
    captured: list[tuple[str, str, tuple]] = []

    result = await _repo(store, captured).list_candidate_experiences("tenant-a", "cand-1")

    assert result == ["exp-1", "exp-2"]


async def test_foreign_tenant_reads_nothing():
    """A caller from tenant-b can never read tenant-a's candidate experiences."""
    store = {"cand-1": ("tenant-a", ["exp-1", "exp-2"])}
    captured: list[tuple[str, str, tuple]] = []

    result = await _repo(store, captured).list_candidate_experiences("tenant-b", "cand-1")

    assert result == [], "跨租户读必须返回空（租户谓词失效即为回归）"


async def test_default_tenant_binding_when_tenant_is_none():
    store = {"cand-9": (_DEFAULT_TENANT, ["exp-x"])}
    captured: list[tuple[str, str, tuple]] = []

    result = await _repo(store, captured).list_candidate_experiences(None, "cand-9")

    assert result == ["exp-x"]
    assert captured[0][2] == ("cand-9", _DEFAULT_TENANT)


# --------------------------------------------------------------------------- #
# The SQL carries the tenant predicate (structural pin)                        #
# --------------------------------------------------------------------------- #
async def test_sql_joins_owner_and_applies_tenant_predicate():
    store: dict[str, tuple[str, list[str]]] = {}
    captured: list[tuple[str, str, tuple]] = []

    await _repo(store, captured).list_candidate_experiences("tenant-a", "cand-1")

    _kind, sql, args = captured[0]
    assert "candidate_experience" in sql
    # The tenant predicate is applied to the owning candidate via an inner JOIN.
    assert "JOIN skill_candidates" in sql
    assert "sc.tenant_id IS NOT DISTINCT FROM" in sql
    # The old, predicate-less form is gone.
    assert "WHERE candidate_id=$1" not in sql
    assert args == ("cand-1", "tenant-a")
