"""Two-sink field alignment — the guard that would have caught the ``resource_id`` gap.

``write_audit_entry`` writes to PostgreSQL (``audit_log``) or, offline, to the
in-process ring buffer, and both docstrings promise the two sinks receive the
**same fields**. They did **not**: the PG INSERT omitted ``resource_id`` — an
existing ``VARCHAR(128)`` column carrying the ``(resource, resource_id)`` index —
while the ring branch wrote it. So the release-gate audit trail put the skill
name in ``resource_id``, and in the PostgreSQL profile that field silently became
NULL: "which version of which skill was released, and why" was only recoverable
by parsing the ``metadata`` JSON, and the purpose-built index went unused.

These tests drive the *same* entry through **both** branches and assert the field
key sets are identical, using a fake pool to capture the SQL + args — so they run
on this machine with no PostgreSQL.
"""

from __future__ import annotations

import re

import pytest

import forgeflow.middleware.audit as audit_mw
from forgeflow.api.routers.audit import _RING, clear_audit_ring
from forgeflow.middleware.audit import write_audit_entry
from forgeflow.skills.governance_gate import _audit_release_decision
from forgeflow.skills.release_gate import evaluate_release

#: The nine fields both sinks must agree on (PG columns == ring keys). The ring
#: additionally stores ``id``/``timestamp``, which PostgreSQL generates
#: server-side (``BIGSERIAL`` / ``DEFAULT now()``) — excluded from the compare.
_SINK_FIELDS = {
    "user_id",
    "role",
    "action",
    "resource",
    "resource_id",
    "outcome",
    "request_id",
    "workspace_id",
    "metadata",
}


class _CapturingConn:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def execute(self, sql: str, *args) -> None:
        self._sink.append((sql, args))


class _CapturingAcquire:
    def __init__(self, conn: _CapturingConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _CapturingConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _CapturingPool:
    """Minimal asyncpg-pool double that records ``execute(sql, *args)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def acquire(self) -> _CapturingAcquire:
        return _CapturingAcquire(_CapturingConn(self.calls))


def _insert_columns(sql: str) -> list[str]:
    """The ordered column list of the ``INSERT INTO audit_log (...)`` statement."""
    match = re.search(
        r"INSERT\s+INTO\s+audit_log\s*\(([^)]*)\)", sql, re.IGNORECASE | re.DOTALL
    )
    assert match, f"no INSERT column list found in:\n{sql}"
    return [col.strip() for col in match.group(1).split(",") if col.strip()]


@pytest.fixture(autouse=True)
def _clean_ring_and_memory_backend(monkeypatch):
    """Isolate the ring and force the offline branch for ``pool=None`` calls."""
    clear_audit_ring()
    # Deterministic: ``pool=None`` ⇒ ring buffer, regardless of STORAGE_BACKEND.
    monkeypatch.setattr(audit_mw, "available_audit_pool", lambda: None)
    yield
    clear_audit_ring()


# --------------------------------------------------------------------------- #
# The alignment guard                                                          #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_both_sinks_receive_the_same_field_keys():
    """Same entry → PG columns must equal the ring keys (this failed before the
    ``resource_id`` fix: the PG column set was missing exactly that key)."""
    entry = {
        "user_id": "manager-1",  # non-UUID → NULL in both sinks
        "role": "manager",
        "action": "skill.release",
        "resource": "skills",
        "resource_id": "分析客户流失技能",
        "outcome": "allowed",
        "workspace_id": None,
        "metadata": {"skill": "分析客户流失技能", "severity": "no_baseline"},
    }

    pool = _CapturingPool()
    await write_audit_entry(dict(entry), pool=pool)  # explicit PG branch
    await write_audit_entry(dict(entry), pool=None)  # → available_audit_pool() None → ring

    assert len(pool.calls) == 1
    sql, args = pool.calls[0]
    pg_cols = set(_insert_columns(sql))
    ring_cols = set(_RING[-1]) - {"id", "timestamp"}

    assert pg_cols == ring_cols == _SINK_FIELDS
    assert len(args) == len(pg_cols) == 9


@pytest.mark.asyncio
async def test_pg_branch_truncates_resource_id_to_the_column_width():
    """``resource_id`` is ``VARCHAR(128)``: an over-long value must be truncated
    (not left to raise and be swallowed as a dropped record)."""
    long_name = "技" * 200
    pool = _CapturingPool()
    await write_audit_entry(
        {
            "action": "skill.release",
            "resource": "skills",
            "resource_id": long_name,
            "outcome": "allowed",
            "metadata": {},
        },
        pool=pool,
    )

    sql, args = pool.calls[0]
    values = dict(zip(_insert_columns(sql), args))
    assert values["resource_id"] == long_name[:128]
    assert len(values["resource_id"]) == 128


# --------------------------------------------------------------------------- #
# The feature this defect broke: the release decision's skill id in PG args    #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_release_decision_lands_resource_id_in_the_pg_args(monkeypatch):
    """``_audit_release_decision`` puts the skill name in ``resource_id`` — assert
    it reaches the PG INSERT args (previously it did not, so PG stored NULL)."""
    pool = _CapturingPool()
    monkeypatch.setattr(audit_mw, "available_audit_pool", lambda: pool)

    decision = evaluate_release({"score": 0.9}, None)  # no_baseline, allowed
    await _audit_release_decision(
        tenant_id=None,
        actor="manager-1",
        actor_role="manager",
        skill_name="分析客户流失技能",
        candidate_id="cand-1",
        decision=decision,
        version="0.1.0",
    )

    assert len(pool.calls) == 1
    sql, args = pool.calls[0]
    values = dict(zip(_insert_columns(sql), args))
    assert values["resource_id"] == "分析客户流失技能"
    assert values["action"] == "skill.release"
    assert values["resource"] == "skills"
    assert values["outcome"] == "allowed"
