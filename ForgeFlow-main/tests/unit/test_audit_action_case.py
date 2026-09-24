"""Audit ``action`` filter — the PG and offline read paths must agree on case.

The offline ring-buffer filter (``_memory_filter``) and the PostgreSQL path
(``search_audit_log``) are two implementations of the *same* query contract.
They drifted: the offline branch compared ``action`` case-insensitively while
the PG branch used a bare ``action = upper($)``. Because the stored value is an
upper-cased HTTP method for middleware request rows but a lower-cased domain
verb for handler-emitted rows (e.g. ``skill.release``), a
``GET /audit/search?action=skill.release`` against PostgreSQL silently returned
``total = 0`` while the same query in the offline profile returned the row.

These tests pin the contract on both sides **without a PostgreSQL server** (a
fake pool captures the SQL + args) and assert the two branches agree. The fix
only touched the read path (``audit.py``) — no schema/index change.
"""

from __future__ import annotations

from typing import Any

import pytest

from forgeflow.api.routers.audit import (
    clear_audit_ring,
    record_audit_entry,
    search_audit_log,
)


class _FakeConn:
    """Captures the statements the audit search issues (COUNT + page SELECT)."""

    def __init__(self, captured: list[tuple[str, str, tuple]]) -> None:
        self._captured = captured

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        self._captured.append(("fetchrow", sql, args))
        return {"n": 0}

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self._captured.append(("fetch", sql, args))
        return []


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    """Minimal asyncpg-pool double capturing every SQL statement + its args."""

    def __init__(self) -> None:
        self.captured: list[tuple[str, str, tuple]] = []

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(_FakeConn(self.captured))

    @property
    def statements(self) -> list[str]:
        return [sql for _kind, sql, _args in self.captured]

    @property
    def args(self) -> list[tuple]:
        return [args for _kind, _sql, args in self.captured]


@pytest.fixture(autouse=True)
def _clean_ring():
    clear_audit_ring()
    yield
    clear_audit_ring()


# --------------------------------------------------------------------------- #
# PostgreSQL branch — the side that had the defect                            #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_pg_action_filter_is_case_insensitive():
    """The PG predicate must case-fold *both* sides (not ``action = upper($)``)."""
    pool = _FakePool()
    await search_audit_log(pool=pool, action="skill.release")

    assert pool.captured, "the PG branch must issue a COUNT + a page SELECT"
    for sql in pool.statements:
        assert "lower(action) = lower(" in sql
        # The previous, buggy form must be gone from both statements.
        assert "action = $" not in sql


@pytest.mark.asyncio
async def test_pg_action_value_is_passed_through_unchanged():
    """Case-folding happens in SQL, so the *bound value* keeps the caller's case
    and is never string-interpolated (it stays a parameter)."""
    pool = _FakePool()
    await search_audit_log(pool=pool, action="SKILL.RELEASE")

    for args in pool.args:
        assert "SKILL.RELEASE" in args


# --------------------------------------------------------------------------- #
# Offline branch — the reference behaviour                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_offline_action_filter_matches_a_domain_action_regardless_of_case():
    record_audit_entry(
        {"action": "skill.release", "role": "manager", "outcome": "allowed", "metadata": {}}
    )
    for query in ("skill.release", "SKILL.RELEASE", "Skill.Release"):
        result = await search_audit_log(pool=None, action=query)
        assert result["total"] == 1, query
        assert result["items"][0]["action"] == "skill.release"


@pytest.mark.asyncio
async def test_offline_action_filter_still_rejects_a_different_action():
    """Case-insensitivity must not degrade into a prefix/loose match."""
    record_audit_entry({"action": "skill.release", "metadata": {}})
    assert (await search_audit_log(pool=None, action="skill.rollback"))["total"] == 0
    assert (await search_audit_log(pool=None, action="skill.release.extra"))["total"] == 0


# --------------------------------------------------------------------------- #
# The two branches must agree                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_both_branches_agree_on_a_lowercased_domain_action():
    """Same stored action + same query ⇒ both branches match.

    Offline returns the row; PG's predicate ``lower(action) = lower($)`` with the
    identical bound value matches the very same row — the drift is closed.
    """
    record_audit_entry({"action": "skill.release", "metadata": {}})

    offline = await search_audit_log(pool=None, action="SKILL.RELEASE")
    assert offline["total"] == 1

    pool = _FakePool()
    await search_audit_log(pool=pool, action="SKILL.RELEASE")
    assert all("lower(action) = lower(" in sql for sql in pool.statements)
    assert all("SKILL.RELEASE" in args for args in pool.args)
