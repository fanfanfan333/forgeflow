"""INC9 T05 — real-PostgreSQL integration for the skill-canary / memory-lifecycle
increment, plus the ASGI RBAC gate for the two new route families.

Runs against the live dev Postgres (docker-compose on :5433 by default) and skips
cleanly when one is not reachable — the same discipline as
``tests/integration/test_agent_eval_pg.py``. What it proves that a mocked pool
cannot:

  * INC9 is **zero-migration**: it shipped on top of revision ``012`` and added
    no migration of its own; running ``head`` twice is idempotent (the global
    head itself later advanced to ``013`` in INC12-A2);
  * ``resolve_canary`` really repoints ``skills.current_version`` through real
    ``asyncpg`` (promote path) and leaves it on the incumbent on a regression;
  * the in-process memory lifecycle sweep archives + excludes, honestly;
  * the new routes are gated: ``/memory/lifecycle`` (read:memory) and
    ``/skills/{id}/canary/resolve`` (write:skills + approve:skills in the handler).
"""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.auth.jwt import create_access_token
from forgeflow.config import get_settings
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.skills.governance_gate import resolve_canary
from forgeflow.skills.models import SkillRecord, SkillVersionRecord

# Captured at import (before conftest's autouse DNS stub) so we reach a real
# 127.0.0.1 rather than the stubbed public IP.
_REAL_GETADDRINFO = socket.getaddrinfo
_DSN = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")

_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
async def pool(monkeypatch):
    from forgeflow.database import _init_connection

    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    try:
        p = await asyncpg.create_pool(
            _DSN,
            min_size=1,
            max_size=3,
            timeout=4,
            statement_cache_size=0,
            init=_init_connection,  # register the JSON/JSONB codec (like init_pool)
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    yield p
    await p.close()


def _upgrade_head(env: dict[str, str] | None = None) -> None:
    """Run ``alembic upgrade head`` in a subprocess (see test_agent_eval_pg)."""
    child_env = dict(os.environ)
    child_env["POSTGRES_SYNC_URL"] = get_settings().postgres_sync_url
    if env:
        child_env.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr[-2000:]}")


# --------------------------------------------------------------------------- #
# migration — INC9 added no revision; head is idempotent                        #
# --------------------------------------------------------------------------- #

async def test_migration_head_is_current_and_twice_is_idempotent(pool):
    _upgrade_head()
    _upgrade_head()  # re-entrant: must be a no-op, never an error
    async with pool.acquire() as conn:
        version = await conn.fetchval("SELECT version_num FROM alembic_version")
    # Head moved from 013 to 014 in INC12-A6 (run_id linkage columns → TEXT).
    assert version == "014"


def test_inc9_added_no_migration_files():
    """INC9 is explicitly zero-migration (docs/sop/12-INC9-DESIGN.md §6).

    INC9 shipped on top of revision ``012`` and added no migration file of its
    own. The head has since advanced to ``013`` — but that ``013`` belongs to a
    *later* increment (INC12-A2, the TenantId contract), so the pre-A2 form of
    this check (``assert versions[-1].startswith("012_")``) can no longer hold.

    The property re-pinned here is exactly the original one: **the first
    migration above ``012`` is INC12-A2's ``013`` — matched by its exact name,
    not merely by the ``"013_"`` prefix — and it is the *only* migration in the
    whole ``013`` era.** If INC9 had smuggled in a migration it would have been
    numbered ``013`` (the next number after the ``012`` it shipped on) and would
    therefore either be the file directly above ``012`` or a second ``013_*``
    file; both assertions below would then fail. A bare ``startswith("013_")``
    accepted *any* increment's migration and so could not prove this.
    """
    versions = sorted(
        p.name for p in (_ROOT / "alembic" / "versions").glob("*.py")
    )
    assert "012_agent_eval_samples.py" in versions
    idx = versions.index("012_agent_eval_samples.py")
    # (1) The migration immediately above 012 is A2's, by exact filename.
    assert versions[idx + 1] == "013_tenant_id_text.py"
    # (2) …and no other 013_* migration exists (a second one would be INC9's).
    assert [v for v in versions[idx:] if v.startswith("013_")] == [
        "013_tenant_id_text.py"
    ]


# --------------------------------------------------------------------------- #
# canary end-to-end through real asyncpg                                         #
# --------------------------------------------------------------------------- #

async def _seed_skill_with_canary(pool):
    from forgeflow.repositories.postgres.skill_repo import PgSkillRepository

    tenant = str(uuid.uuid4())
    repo = PgSkillRepository(pool=pool)
    now = datetime.now(timezone.utc)
    skill = SkillRecord(
        tenant_id=tenant,
        name=f"canary-pg-{uuid.uuid4().hex[:8]}",
        domain="general",
        status="published",
        current_version="1.0.0",
    )
    await repo.create_skill(skill)
    await repo.add_version(
        tenant,
        SkillVersionRecord(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.0.0",
            spec={"prompt": "v1"},
            eval_score=0.90,
            created_at=now - timedelta(minutes=5),
        ),
    )
    await repo.add_version(
        tenant,
        SkillVersionRecord(
            tenant_id=tenant,
            skill_id=skill.id,
            semver="1.1.0",
            spec={"prompt": "v2"},
            eval_score=0.95,
            created_at=now,
        ),
    )
    return tenant, repo, skill


async def _cleanup(pool, tenant, skill_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM skill_versions WHERE skill_id = $1", uuid.UUID(skill_id))
        await conn.execute("DELETE FROM skills WHERE id = $1", uuid.UUID(skill_id))


async def test_pg_resolve_canary_promotes_the_canary(pool):
    _upgrade_head()
    tenant, repo, skill = await _seed_skill_with_canary(pool)
    try:
        result = await resolve_canary(
            skill.id,
            tenant_id=tenant,
            actor="manager-1",
            actor_role="manager",
            canary_metrics={"score": 0.95},
            incumbent_metrics={"score": 0.90},
            sample_n=20,
            skill_repo=repo,
        )
        assert result["action"] == "promote"
        assert result["canary_version"] == "1.1.0"

        fresh = await repo.get_skill(tenant, skill.id)
        assert fresh.current_version == "1.1.0"
    finally:
        await _cleanup(pool, tenant, skill.id)


async def test_pg_resolve_canary_rollback_keeps_the_incumbent(pool):
    _upgrade_head()
    tenant, repo, skill = await _seed_skill_with_canary(pool)
    try:
        result = await resolve_canary(
            skill.id,
            tenant_id=tenant,
            actor="manager-1",
            actor_role="manager",
            canary_metrics={"score": 0.60},
            incumbent_metrics={"score": 0.90},
            sample_n=20,
            skill_repo=repo,
        )
        assert result["action"] == "rollback"
        fresh = await repo.get_skill(tenant, skill.id)
        assert fresh.current_version == "1.0.0"  # incumbent retained
    finally:
        await _cleanup(pool, tenant, skill.id)


# --------------------------------------------------------------------------- #
# in-process memory lifecycle (backend-independent)                             #
# --------------------------------------------------------------------------- #

async def test_memory_lifecycle_sweep_archives_and_excludes(monkeypatch):
    from forgeflow.experience.memory_store import (
        clear_memory_entries,
        list_memories,
        lifecycle_sweep,
        save_memory,
        search,
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "memory_decay_enabled", True)
    monkeypatch.setattr(settings, "memory_archive_min_score", 0.9)
    monkeypatch.setattr(settings, "memory_archive_max_age_days", 0.0)

    clear_memory_entries()
    tenant = f"t-inc9-{uuid.uuid4().hex[:8]}"
    stale = await save_memory(tenant, "semantic", "低分陈旧事实")

    result = await lifecycle_sweep(tenant)
    assert result["enabled"] is True and result["archived"] == 1

    # Excluded from the default read paths…
    assert stale.id not in [r.id for r in await list_memories(tenant)]
    assert stale.id not in [e.id for e, _ in await search(tenant, "低分陈旧事实", k=5)]
    # …but recoverable with include_archived.
    assert stale.id in [r.id for r in await list_memories(tenant, include_archived=True)]
    clear_memory_entries()


# --------------------------------------------------------------------------- #
# ASGI RBAC gate for the new routes                                             #
# --------------------------------------------------------------------------- #

def _mini_app() -> TestClient:
    from forgeflow.api.routers import memory as memory_router
    from forgeflow.api.routers import skills as skills_router

    mini = FastAPI()
    mini.add_middleware(RBACMiddleware)
    mini.include_router(memory_router.router, prefix="/memory")
    mini.include_router(skills_router.router, prefix="/skills")
    return TestClient(mini)


def _token(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id=f'{role}-1', role=role)}"}


def test_memory_lifecycle_route_requires_read_memory(force_memory_backend):
    client = _mini_app()
    assert client.get("/memory/lifecycle").status_code == 401
    assert client.get("/memory/lifecycle", headers=_token("viewer")).status_code == 403
    assert client.get("/memory/lifecycle", headers=_token("manager")).status_code == 200


def test_canary_resolve_route_requires_write_and_approve(force_memory_backend):
    client = _mini_app()
    missing = str(uuid.uuid4())
    # No token ⇒ 401; sales_rep lacks write:skills ⇒ 403 at the middleware.
    assert client.post(f"/skills/{missing}/canary/resolve", json={}).status_code == 401
    assert (
        client.post(
            f"/skills/{missing}/canary/resolve", json={}, headers=_token("sales_rep")
        ).status_code
        == 403
    )
    # manager passes write:skills + the handler's approve:skills ⇒ reaches the
    # domain layer, which reports the skill is absent (404, not a 403 gate).
    assert (
        client.post(
            f"/skills/{missing}/canary/resolve", json={}, headers=_token("manager")
        ).status_code
        == 404
    )
