"""INC46 T31 (pg-档) — ``memory_preferences`` on real PostgreSQL (migration 029).

Proves, against the live dev PostgreSQL, what an in-memory store cannot:

* migration ``029`` really created the ``memory_preferences`` table with its
  ``tenant_id NOT NULL`` (红线 5) and the ``source`` ``CHECK`` constraint;
* a write through :class:`PostgresPreferenceStore` round-trips (including the
  ``source`` transition ``suggestion`` → ``confirmed_suggestion``);
* the whole thing is tenant-scoped: a second tenant reads the empty set (fail
  closed) — never another tenant's rows.

It dials the same dev database the rest of the postgres-profile suite uses and
skips cleanly when it is unreachable (mirroring ``test_inc46_scrub_pg.py``).
"""

from __future__ import annotations

import socket
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.memory.preferences import (
    KIND_GLOSSARY,
    KIND_STYLE,
    PostgresPreferenceStore,
    Preference,
    SOURCE_EXPLICIT,
    SOURCE_SUGGESTION,
)

_TENANT_A = f"t-t31-pg-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t31-pg-b-{uuid.uuid4().hex[:8]}"


def _sync_dsn() -> str:
    return get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")


def _pg_reachable() -> bool:
    """Cheap liveness probe — the suite must skip, not fail, without a database."""
    dsn = _sync_dsn()
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
def store() -> PostgresPreferenceStore:
    created = PostgresPreferenceStore(_sync_dsn())
    try:
        yield created
    finally:
        import psycopg

        try:
            with psycopg.connect(_sync_dsn()) as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_preferences WHERE tenant_id = ANY(%s)",
                    ([_TENANT_A, _TENANT_B],),
                )
                conn.commit()
        except Exception:  # noqa: BLE001 — cleanup must never mask the result
            pass


def test_schema_has_tenant_not_null_and_source_check() -> None:
    """阳性 — 迁移 029 的表存在，tenant_id NOT NULL，且 source 受 CHECK 约束。"""
    import psycopg

    with psycopg.connect(_sync_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'memory_preferences' AND column_name = 'tenant_id'"
        )
        row = cur.fetchone()
        assert row is not None and row[0] == "NO"  # tenant_id NOT NULL

        cur.execute(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "WHERE t.relname = 'memory_preferences' AND c.conname = 'memory_preferences_source_chk'"
        )
        definition = cur.fetchone()
        assert definition is not None
        assert "suggestion" in definition[0] and "explicit" in definition[0]


def test_roundtrip_and_tenant_isolation(store: PostgresPreferenceStore) -> None:
    """阳性/阴性 — 写入读回一致；跨租户读空集。"""
    pref = Preference(
        tenant_id=_TENANT_A,
        scope="tenant",
        kind=KIND_GLOSSARY,
        key="甲方",
        value="委托方",
        source=SOURCE_EXPLICIT,
        created_by="u-1",
    )
    store.record(pref)

    rows = store.list(_TENANT_A)
    assert len(rows) == 1
    assert rows[0].kind == KIND_GLOSSARY and rows[0].key == "甲方"
    assert rows[0].value == "委托方" and rows[0].active is True

    # cross-tenant ⇒ empty (fail-closed)
    assert store.list(_TENANT_B) == []
    # falsy tenant ⇒ empty
    assert store.list(None) == []


def test_confirm_transition_persists_on_pg(store: PostgresPreferenceStore) -> None:
    """阳性 — ``suggestion`` → ``confirmed_suggestion`` 在真表上持久化。"""
    pref = Preference(
        tenant_id=_TENANT_A,
        scope="tenant",
        kind=KIND_STYLE,
        value="正式",
        source=SOURCE_SUGGESTION,  # document-derived ⇒ inactive
        created_by="document",
    )
    store.record(pref)
    assert store.list(_TENANT_A, include_inactive=False) == []  # not effective yet

    assert store.confirm(_TENANT_A, pref.id) is True
    effective = store.list(_TENANT_A, include_inactive=False)
    assert len(effective) == 1
    assert effective[0].source == "confirmed_suggestion" and effective[0].active is True

    # cross-tenant confirm is a no-op
    assert store.confirm(_TENANT_B, pref.id) is False
