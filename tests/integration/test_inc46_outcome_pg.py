"""INC46 T16 (pg-档) — ``run_outcomes`` / ``feedback_events`` on **real PostgreSQL**.

Proves, against the live dev PostgreSQL, what an in-memory store cannot:

  * migration ``024``'s two tables accept writes and read back **tenant scoped**
    — a second tenant observes an empty set (row-level isolation, 红线 5);
  * an **unmeasured** ``hard_pass`` is stored and read back as ``NULL`` — never
    a fabricated ``0`` (红线 4);
  * the **idempotency** constraint is enforced by the database itself: a second
    insert of the same ``(tenant_id, idempotency_key)`` does not create a second
    event (the store returns ``created=False`` and the count stays 1);
  * a run owned by another tenant cannot be written to by a foreign tenant.

It dials the same dev database the rest of the postgres-profile suite uses and
skips cleanly when it is unreachable (mirroring ``test_inc46_skill_schema_pg.py``).
"""

from __future__ import annotations

import socket
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.outcomes.store import CrossTenantRun, PostgresOutcomeStore

_TENANT_A = f"t-t16-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t16-b-{uuid.uuid4().hex[:8]}"


def _dsn() -> str:
    return get_settings().postgres_sync_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _pg_reachable() -> bool:
    """Cheap liveness probe — the suite must skip, not fail, without a database."""
    dsn = _dsn()
    host, _, rest = dsn.rpartition("@")[2].partition(":")
    port = int(rest.split("/")[0]) if rest.split("/")[0].isdigit() else 5432
    try:
        with socket.create_connection((host or "127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(), reason="dev PostgreSQL (5433) unreachable"
)


@pytest.fixture()
def store() -> PostgresOutcomeStore:
    return PostgresOutcomeStore(_dsn())


def test_feedback_and_outcome_roundtrip_is_tenant_scoped(store: PostgresOutcomeStore) -> None:
    """阳性 — 写入并按 tenant 隔离读回。"""
    run = f"run-{uuid.uuid4().hex[:8]}"
    event, created = store.record_feedback(
        _TENANT_A, run, "approval", idempotency_key=f"k-{uuid.uuid4().hex}", actor="u1"
    )
    assert created is True
    assert event["run_id"] == run

    store.upsert_outcome(_TENANT_A, run, "ACCEPTED_EXPLICIT", hard_pass=None)
    row = store.get_outcome(_TENANT_A, run)
    assert row is not None
    assert row["outcome_label"] == "ACCEPTED_EXPLICIT"

    # 他租户 ⇒ 空集（不泄露）
    assert store.get_outcome(_TENANT_B, run) is None
    assert store.list_feedback(_TENANT_B, run) == []


def test_unmeasured_hard_pass_reads_back_as_null_not_zero(
    store: PostgresOutcomeStore,
) -> None:
    """红线 4 — 未测量的 ``hard_pass`` 读回是 ``NULL``，绝不是 ``0``。"""
    run = f"run-{uuid.uuid4().hex[:8]}"
    store.upsert_outcome(_TENANT_A, run, "UNKNOWN", hard_pass=None)
    row = store.get_outcome(_TENANT_A, run)
    assert row is not None
    assert row["hard_pass"] is None
    assert row["hard_pass"] != 0


def test_idempotency_key_is_enforced_by_the_database(
    store: PostgresOutcomeStore,
) -> None:
    """阴性 — 同一幂等键重复提交不产生第二条事件（唯一约束承重）。"""
    run = f"run-{uuid.uuid4().hex[:8]}"
    key = f"dup-{uuid.uuid4().hex}"
    first, created_1 = store.record_feedback(
        _TENANT_A, run, "approval", idempotency_key=key
    )
    second, created_2 = store.record_feedback(
        _TENANT_A, run, "approval", idempotency_key=key
    )
    assert created_1 is True
    assert created_2 is False
    assert first["idempotency_key"] == second["idempotency_key"]
    assert len(store.list_feedback(_TENANT_A, run)) == 1


def test_same_key_under_another_tenant_is_a_different_event(
    store: PostgresOutcomeStore,
) -> None:
    """幂等作用域是**每租户**：他租户用同一个键是另一条事件，不得碰撞。"""
    key = f"shared-{uuid.uuid4().hex}"
    _, ok_a = store.record_feedback(
        _TENANT_A, f"run-a-{uuid.uuid4().hex[:6]}", "approval", idempotency_key=key
    )
    _, ok_b = store.record_feedback(
        _TENANT_B, f"run-b-{uuid.uuid4().hex[:6]}", "approval", idempotency_key=key
    )
    assert ok_a is True and ok_b is True


def test_foreign_run_is_refused(store: PostgresOutcomeStore) -> None:
    """红线 5 — 他租户已认领的 run，当前租户写入被拒（403 的存储层依据）。"""
    run = f"run-{uuid.uuid4().hex[:8]}"
    store.record_feedback(_TENANT_A, run, "approval", idempotency_key=f"own-{uuid.uuid4().hex}")
    with pytest.raises(CrossTenantRun):
        store.record_feedback(
            _TENANT_B, run, "reject", idempotency_key=f"foreign-{uuid.uuid4().hex}"
        )
