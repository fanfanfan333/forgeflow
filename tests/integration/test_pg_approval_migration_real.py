"""FF-R1 P2-SYNC — real-PostgreSQL checks: migration round trip + consume race.

Runs ONLY where a live PostgreSQL is provisioned for it — CI's
``tenant-security`` job (PostgreSQL service, ``TENANT_RLS_REAL=1``,
``POSTGRES_MIGRATION_URL`` / ``POSTGRES_URL`` set by the job) — and is
skipped everywhere else. Nothing here is faked with a substitute
database: these two properties only exist against real PostgreSQL.

1. **Migration round trip** (大川 P2-SYNC; head handling per
   FF-R1 P5-FIX): on a throwaway database, ``upgrade head`` →
   ``downgrade <the revision below the payload_digest step>`` →
   ``upgrade head``. The ``payload_digest`` step (P1's migration)
   must appear, disappear and reappear. **No revision number is
   hard-coded**: the head is read live from the alembic script
   directory (051 since P5 added the workspace-detail migration;
   P2 itself added none), the chain must keep exactly one head,
   and the downgrade target is that step's own ``down_revision``,
   also read live. **Privilege requirement**: the chain runs migration 002,
   which issues ``CREATE EXTENSION vector`` — the migration role
   (``POSTGRES_MIGRATION_URL``) must be a **superuser** (CI's
   PostgreSQL service user is). The test checks this up front and
   skips with an explicit reason when the role is not a superuser,
   rather than failing mid-chain and leaving the throwaway database
   behind.

2. **Concurrent consume** (FF-G5, P1): eight racing
   ``PgPolicyRepository.consume_approval`` calls on one approved row
   must produce exactly one ``True`` — the single conditional UPDATE
   is the claim; the in-memory repository is covered by the unit
   tests, this pins the PostgreSQL implementation.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("TENANT_RLS_REAL") != "1" or not os.getenv("POSTGRES_MIGRATION_URL"),
    reason="real PostgreSQL acceptance (CI tenant-security job only)",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _admin_url() -> str:
    url = os.environ["POSTGRES_MIGRATION_URL"]
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _with_database(url: str, database: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{database}"))


def _alembic_config():
    from alembic.config import Config

    return Config(str(_REPO_ROOT / "alembic.ini"))


def _live_chain_facts() -> tuple[str, str, str]:
    """(head, payload_digest revision, the revision below it) — read LIVE.

    FF-R1 P5-FIX: nothing here may hard-code the current head; the
    chain grows (051 arrived with P5) and this test must follow it.
    The ``payload_digest`` column names its own migration: the
    revision whose module adds it, found by scanning the chain.
    """
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(_alembic_config())
    heads = scripts.get_heads()
    assert len(heads) == 1, f"alembic chain must have exactly one head, got {heads}"
    head = heads[0]
    digest_revision = None
    for revision in scripts.walk_revisions():
        path = scripts.get_revision(revision.revision).path
        if "payload_digest" in Path(path).read_text(encoding="utf-8"):
            digest_revision = revision.revision
            break
    assert digest_revision is not None, "no migration adds payload_digest"
    below = scripts.get_revision(digest_revision).down_revision
    assert isinstance(below, str) and below
    return head, digest_revision, below


def _current_revision(url: str) -> str | None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def _payload_digest_present(url: str) -> bool:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='agent_approvals' AND column_name='payload_digest'"
        ).fetchone()
    return row is not None


def test_migration_round_trip_head_below_head(monkeypatch):
    import psycopg

    from alembic import command

    # Capture the admin URL BEFORE any monkeypatching: the finally
    # below runs while the patched environment is still in effect
    # (monkeypatch reverts only after the test returns), so resolving
    # the admin URL there would read the throwaway database's own URL
    # and the DROP would connect to the database it must drop —
    # failing and leaving the throwaway database behind.
    admin_url = _admin_url()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        is_superuser = admin.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
        ).fetchone()[0]
    if not is_superuser:
        # Migration 002 issues CREATE EXTENSION vector; without a
        # superuser migration role the chain cannot run at all. Skip
        # with the reason BEFORE creating anything, so no throwaway
        # database is left behind either.
        pytest.skip(
            "migration round trip needs a superuser migration role "
            "(migration 002 runs CREATE EXTENSION vector); the role in "
            "POSTGRES_MIGRATION_URL is not a superuser"
        )
    database = f"ff_mig_rt_{uuid.uuid4().hex[:12]}"
    throwaway = _with_database(admin_url, database)
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{database}"')
    try:
        # alembic/env.py resolves its DSN from these variables, in
        # this order — point every one of them at the throwaway DB.
        for var in ("POSTGRES_MIGRATION_URL", "POSTGRES_SYNC_URL", "SQLALCHEMY_URL"):
            monkeypatch.setenv(var, throwaway)
        cfg = _alembic_config()
        head, _digest_revision, below_digest = _live_chain_facts()

        command.upgrade(cfg, "head")
        assert _current_revision(throwaway) == head
        assert _payload_digest_present(throwaway) is True

        command.downgrade(cfg, below_digest)
        assert _current_revision(throwaway) == below_digest
        assert _payload_digest_present(throwaway) is False

        command.upgrade(cfg, "head")
        assert _current_revision(throwaway) == head
        assert _payload_digest_present(throwaway) is True
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


@pytest.mark.asyncio
async def test_pg_concurrent_consume_succeeds_exactly_once():
    import asyncpg

    from forgeflow.config import get_settings
    from forgeflow.database import TenantPool
    from forgeflow.governance.models import ApprovalRecord
    from forgeflow.repositories.base import utcnow
    from forgeflow.repositories.postgres.policy_repo import PgPolicyRepository

    settings = get_settings()
    pool = await asyncpg.create_pool(
        settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    )
    tenant = f"consume-{uuid.uuid4().hex[:12]}"
    repo = PgPolicyRepository(default_tenant=tenant, pool=TenantPool(pool))
    record = ApprovalRecord(
        tenant_id=tenant,
        run_id="run-consume-race",
        risk_level="high",
        requested_action="mcp.write:demo_tool",
        requester="u1",
        approver="u2",
        decision="approve",
        status="approved",
        payload_digest="sha256:" + "0" * 64,
        resolved_at=utcnow(),
    )
    try:
        await repo.save_approval(record)
        results = await asyncio.gather(
            *[repo.consume_approval(tenant, record.id) for _ in range(8)]
        )
        assert results.count(True) == 1
        stored = await repo.get_approval(tenant, record.id)
        assert stored is not None
        assert stored.status == "consumed"
    finally:
        admin = await asyncpg.connect(_admin_url())
        try:
            await admin.execute("DELETE FROM agent_approvals WHERE id=$1", record.id)
        finally:
            await admin.close()
            await pool.close()
