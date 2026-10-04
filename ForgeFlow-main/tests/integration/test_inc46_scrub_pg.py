"""INC46 T32 (pg-档) — the scrub columns + ``scrub_audits`` on real PostgreSQL.

Proves, against the live dev PostgreSQL, what an in-memory store cannot:

* migration ``026`` really added the two **nullable** columns
  (``experiences.scrub_status`` / ``scrub_version``) and the ``scrub_audits``
  table with its ``tenant_id``;
* a write through :class:`PgExperienceRepository` is scrubbed **in the DB row**
  (raw PII never lands) and stamped ``scrubbed`` / ``<version>``;
* the whole thing is tenant-scoped (红线 5): a second tenant reads the empty set;
* the backfill job is **idempotent on PG** (second pass ``changed == 0``) and its
  minimised audit rows are tenant-scoped (no raw PII stored).

It dials the same dev database the rest of the postgres-profile suite uses and
skips cleanly when it is unreachable (mirroring ``test_inc46_hitl_pg.py``).
"""

from __future__ import annotations

import socket
import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.experience.models import ExperienceRecord
from forgeflow.jobs.backfill_scrub import backfill_scrub
from forgeflow.privacy.audit import PostgresScrubAuditStore
from forgeflow.privacy.scrubber import SCRUB_VERSION

_TENANT_A = f"t-t32-pg-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t32-pg-b-{uuid.uuid4().hex[:8]}"


def _sync_dsn() -> str:
    return get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")


def _async_dsn() -> str:
    return get_settings().postgres_url.replace("postgresql+asyncpg://", "postgresql://")


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
async def pool():
    asyncpg = pytest.importorskip("asyncpg")
    from forgeflow.database import _init_connection

    created = await asyncpg.create_pool(
        _async_dsn(), min_size=1, max_size=2, command_timeout=15, init=_init_connection
    )
    try:
        yield created
    finally:
        try:
            async with created.acquire() as conn:
                await conn.execute(
                    "DELETE FROM scrub_audits WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
                await conn.execute(
                    "DELETE FROM experiences WHERE tenant_id = ANY($1::text[])",
                    [_TENANT_A, _TENANT_B],
                )
        except Exception:  # noqa: BLE001 — cleanup must never mask the test result
            pass
        await created.close()


async def test_schema_has_nullable_scrub_columns_and_audits_table(pool) -> None:
    """阳性 — 迁移 026 的两列（可空）与 scrub_audits（带 tenant_id）确实存在。"""
    async with pool.acquire() as conn:
        cols = await conn.fetch(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'experiences' AND column_name IN "
            "('scrub_status', 'scrub_version')"
        )
        nullable = {r["column_name"]: r["is_nullable"] for r in cols}
        assert set(nullable) == {"scrub_status", "scrub_version"}
        # 未测量 ⇒ NULL (红线 4) — both columns are nullable on purpose.
        assert nullable["scrub_status"] == "YES"
        assert nullable["scrub_version"] == "YES"

        assert await conn.fetchval("SELECT to_regclass('public.scrub_audits')") is not None
        tenant_nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'scrub_audits' AND column_name = 'tenant_id'"
        )
        assert tenant_nullable == "NO"  # tenant_id NOT NULL (红线 5)


async def test_write_scrubs_and_is_tenant_scoped(pool) -> None:
    """阳性 — 写前脱敏落库；跨租户读空集。"""
    from forgeflow.repositories.postgres.experience_repo import PgExperienceRepository

    repo = PgExperienceRepository(pool=pool)
    record = ExperienceRecord(
        tenant_id=_TENANT_A,
        run_id="r-pg-1",
        outcome="success",
        summary="客户姓名：张伟，电话：13800138000，邮箱：zhangwei@example.com",
        reusable_steps=[{"tool": "a"}],
    )
    await repo.save(record)

    stored = await repo.get(_TENANT_A, record.id)
    assert stored is not None
    assert "13800138000" not in stored.summary
    assert "张伟" not in stored.summary
    assert "[REDACTED:phone]" in stored.summary and "[REDACTED:name]" in stored.summary
    assert stored.scrub_status == "scrubbed"
    assert stored.scrub_version == SCRUB_VERSION

    # cross-tenant ⇒ empty (fail-closed)
    assert await repo.get(_TENANT_B, record.id) is None
    assert await repo.list(_TENANT_B) == []


async def test_backfill_is_idempotent_on_pg_and_audits_are_scoped(pool) -> None:
    """阳性/阴性 — 回填在真表上幂等；审计按租户隔离且不含 PII。"""
    from forgeflow.repositories.postgres.experience_repo import PgExperienceRepository

    # A legacy row that predates T32: raw PII, scrub_status / scrub_version NULL.
    legacy_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO experiences "
            "(id, tenant_id, run_id, summary, decisions, outcome, reusable_steps, "
            " tags, scrub_status, scrub_version) "
            "VALUES ($1, $2, $3, $4, '[]'::jsonb, 'success', '[]'::jsonb, "
            "        '{}'::text[], NULL, NULL)",
            legacy_id,
            _TENANT_A,
            "r-legacy",
            "联系人：李娜 电话 13900139000",
        )

    repo = PgExperienceRepository(pool=pool)
    audit = PostgresScrubAuditStore(_sync_dsn())

    first = await backfill_scrub(_TENANT_A, repo=repo, audit_store=audit, version=SCRUB_VERSION)
    assert first.scanned >= 1
    assert first.changed >= 1
    assert first.refused == 0
    assert first.residual == 0  # 无残留 PII

    second = await backfill_scrub(_TENANT_A, repo=repo, audit_store=audit, version=SCRUB_VERSION)
    assert second.changed == 0  # idempotent on the real table

    revived = await repo.get(_TENANT_A, str(legacy_id))
    assert revived is not None
    assert revived.scrub_status == "scrubbed"
    assert revived.scrub_version == SCRUB_VERSION
    assert "13900139000" not in revived.summary
    assert "[REDACTED:phone]" in revived.summary

    audits = audit.list(_TENANT_A)
    assert audits
    assert all(row.tenant_id == _TENANT_A for row in audits)
    for row in audits:
        assert "13900139000" not in str(row.to_dict())  # minimal: no raw PII
    assert audit.list(_TENANT_B) == []  # tenant-scoped audit read
