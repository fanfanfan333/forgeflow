"""Phase 7: Postgres checkpoint store against the real ``run_checkpoints`` table.

Skips when live Postgres is unreachable or migration 042 has not
been applied (the migration's own application is verified by the
alembic upgrade in the verification run, not by this test).
"""

import os

import pytest

from forgeflow.runtime.checkpoints import PostgresCheckpointStore, build_state

_TENANT = "t-checkpoints-pg"
_RUN = "run-checkpoints-pg-1"


def _dsn():
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN") or ""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture
def store():
    dsn = _dsn()
    if not dsn:
        pytest.skip("no sync Postgres DSN")
    import psycopg

    try:
        conn = psycopg.connect(dsn)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM run_checkpoints LIMIT 1")
    except Exception:  # noqa: BLE001
        conn.close()
        pytest.skip("run_checkpoints table missing (migration 042 not applied)")
    conn.close()
    pg = PostgresCheckpointStore(dsn)
    yield pg
    try:
        conn = psycopg.connect(dsn)
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM run_checkpoints WHERE tenant_id = %s AND run_id = %s",
                (_TENANT, _RUN),
            )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001 — test cleanup must not fail the test
        pass


def _state():
    return build_state(
        intent="pg checkpoint test",
        requirements=[],
        acceptance=None,
        next_step={"kind": "model_call"},
        codeplane_ref=None,
        breaker_summary={},
        stuck_summary={},
        approval=None,
        pending_writes=[],
        trajectory_cursor=3,
    )


def test_pg_save_latest_history_prune(store):
    for round_no in range(3):
        saved = store.save(_TENANT, run_id=_RUN, round=round_no, kind="round", state=_state())
        assert saved.seq is not None and saved.id
    saved_approval = store.save(_TENANT, run_id=_RUN, round=1, kind="approval", state=_state())
    latest = store.latest(_TENANT, _RUN)
    assert latest is not None and latest.kind == "approval" and latest.seq == saved_approval.seq
    assert latest.state["intent"] == "pg checkpoint test"
    assert store.latest("other-tenant", _RUN) is None
    rows = store.history(_TENANT, _RUN, limit=10)
    assert [row.kind for row in rows] == ["approval", "round", "round", "round"]
    # Prune keeps the latest 2 rounds + the approval.
    deleted = store.prune(_TENANT, _RUN, keep_latest=2)
    assert deleted == 1
    remaining = store.history(_TENANT, _RUN, limit=10)
    assert len(remaining) == 3
    assert sum(1 for row in remaining if row.kind == "approval") == 1
