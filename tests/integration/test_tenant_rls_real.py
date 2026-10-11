"""Real PostgreSQL attack controls. No business auth doubles or fake SQL.

Run with TENANT_RLS_REAL=1 and a non-superuser POSTGRES_URL after migrations.
Only randomly named test rows are inserted and removed.
"""
import asyncio
import os
import uuid
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
import pytest_asyncio
from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException

from forgeflow.api.routers import approvals, auth
from forgeflow.api.schemas import ApprovalActionRequest
from forgeflow.auth.jwt import create_access_token, decode_access_token
from forgeflow.auth.tokens import RefreshError, issue_refresh_token, rotate
from forgeflow.config import get_settings
from forgeflow.database import TenantPool, _init_connection, tenant_connection
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.models import UserContext
from forgeflow.repositories.approval_requests import ApprovalRequestsRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.getenv("TENANT_RLS_REAL") != "1", reason="explicit real RLS acceptance")]


@pytest_asyncio.fixture
async def db():
    settings = get_settings()
    pool = await asyncpg.create_pool(settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://"), min_size=1, max_size=4, init=_init_connection)
    admin_url = os.getenv("POSTGRES_MIGRATION_URL") or dotenv_values(".env")["POSTGRES_MIGRATION_URL"]
    admin = await asyncpg.connect(admin_url.replace("postgresql+psycopg://", "postgresql://"))
    tenants = [str(uuid.uuid4()), str(uuid.uuid4())]
    runs, tokens, users = [], [], []
    for tenant in tenants:
        run, token, uid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        users.append(uid); runs.append(run); tokens.append(token)
        await admin.execute("INSERT INTO auth_users(id,username,password_hash,role) VALUES($1,$2,'test-only','manager')", uid, str(uid))
        await admin.execute("INSERT INTO tenant_memberships(tenant_id,user_id) VALUES($1,$2)", tenant, uid)
        await admin.execute("INSERT INTO workflow_runs(id,thread_id,tenant_id,workspace_id) VALUES($1,$2,$3,$4)", run, uuid.uuid4(), tenant, uuid.UUID(tenant))
        await admin.execute("INSERT INTO approval_requests(run_id,token,stage,payload,tenant_id,workspace_id) VALUES($1,$2,'approve','{}',$3,$4)", run, token, tenant, uuid.UUID(tenant))
    try:
        yield TenantPool(pool), admin, tenants, runs, tokens, users
    finally:
        await admin.execute("DELETE FROM approval_requests WHERE run_id=ANY($1::uuid[])", runs)
        await admin.execute("DELETE FROM workflow_runs WHERE id=ANY($1::uuid[])", runs)
        await admin.execute("DELETE FROM auth_users WHERE id=ANY($1::uuid[])", users)
        await admin.close(); await pool.close()


async def test_rls_omitted_where_and_pool_reuse(db):
    pool, _, tenants, runs, _, _ = db
    async with pool.raw.acquire() as conn:
        role = await conn.fetchrow("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
        assert not role["rolsuper"] and not role["rolbypassrls"]
        assert await conn.fetchval("SELECT count(*) FROM workflow_runs") == 0
        assert await conn.fetchval("SELECT bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class WHERE relnamespace='public'::regnamespace AND relname IN ('workflow_runs','approval_requests','workspace_runs','memory_vectors','resources','skills')")
    for tenant, own in zip(tenants, runs, strict=True):
        async with tenant_connection(pool, tenant) as conn:
            # Intentionally no WHERE: the database, not Python, enforces scope.
            assert [row["id"] for row in await conn.fetch("SELECT id FROM workflow_runs")] == [own]
            assert await conn.execute("UPDATE workflow_runs SET status='failed' WHERE id=$1", runs[1] if own == runs[0] else runs[0]) == "UPDATE 0"
        async with pool.raw.acquire() as conn:
            assert await conn.fetchval("SELECT NULLIF(current_setting('app.tenant_id',true),'')") is None
    async with tenant_connection(pool, tenants[0]) as conn:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO workflow_runs(thread_id,tenant_id) VALUES($1,$2)", uuid.uuid4(), tenants[1])


async def test_approval_http_cross_tenant_and_concurrent_decision(db, monkeypatch):
    pool, _, tenants, _, tokens, users = db
    app = FastAPI(); app.state.pool = pool; app.state.graph = object()
    app.add_middleware(RBACMiddleware)
    app.include_router(approvals.router, prefix="/approvals")
    resume = AsyncMock(return_value={"current_stage": "done", "errors": []})
    monkeypatch.setattr(approvals.SalesOpsPipeline, "resume", resume)
    token = create_access_token(user_id=str(users[0]), role="manager", workspace_id=tenants[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test", headers={"Authorization": "Bearer " + token}) as client:
        response = await client.get("/approvals/pending")
        assert response.status_code == 200 and [row["token"] for row in response.json()] == [str(tokens[0])]
        for method, suffix in [("GET", ""), ("POST", "/approve"), ("POST", "/reject")]:
            response = await client.request(method, f"/approvals/{tokens[1]}{suffix}", json={} if method == "POST" else None)
            assert response.status_code == 404
        results = await asyncio.gather(*(client.post(f"/approvals/{tokens[0]}/approve", json={}) for _ in range(2)))
        assert sorted(r.status_code for r in results) == [200,409]
        assert resume.await_count == 1
        assert (await client.post(f"/approvals/{tokens[0]}/reject", json={})).status_code == 409
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get(f"/approvals/{tokens[0]}")).status_code == 401


async def test_refresh_scope_rotation_and_membership_revocation(db):
    pool, admin, tenants, _, _, users = db
    first = await issue_refresh_token(pool, users[0], workspace_id=tenants[0])
    result = await rotate(pool, first)
    assert result["workspace_id"] == tenants[0]
    with pytest.raises(RefreshError, match="reuse"):
        await rotate(pool, first)
    with pytest.raises(RefreshError, match="reuse"):
        await rotate(pool, result["refresh_token"])
    app = FastAPI(); app.state.pool = pool
    app.include_router(auth.router, prefix="/auth")
    bound = await issue_refresh_token(pool, users[0], workspace_id=tenants[0])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/auth/refresh", json={"refresh_token": bound})
        assert response.status_code == 200
        assert decode_access_token(response.json()["access_token"])["workspace"] == tenants[0]
        await admin.execute("DELETE FROM tenant_memberships WHERE user_id=$1", users[0])
        rejected = await client.post("/auth/refresh", json={"refresh_token": response.json()["refresh_token"]})
        assert rejected.status_code == 403


async def test_approval_resume_failure_keeps_claim_and_cannot_replay(db, monkeypatch):
    pool, _, tenants, _, tokens, users = db
    repo = ApprovalRequestsRepository(pool)
    resume = AsyncMock(side_effect=RuntimeError("uncertain external result"))
    monkeypatch.setattr(approvals.SalesOpsPipeline, "resume", resume)
    with pytest.raises(HTTPException) as failure:
        await approvals._decide(str(tokens[0]), ApprovalActionRequest(), pool, object(), UserContext(user_id=str(users[0]), role="manager"), tenants[0], "approved")
    assert failure.value.status_code == 503
    with pytest.raises(HTTPException) as error:
        await repo.claim(tenants[0], str(tokens[0]), "approved", str(users[0]), "retry")
    assert error.value.status_code == 409
    assert (await repo.get(tenants[0], str(tokens[0])))["status"] == "resuming_approved"
    assert resume.await_count == 1


async def test_audit_write_retains_scope_after_request_context_ends(db):
    from forgeflow.middleware.audit import write_audit_entry
    pool, admin, tenants, *_ = db
    request_id = uuid.uuid4()
    try:
        await write_audit_entry({"workspace_id": tenants[0], "request_id": str(request_id), "action": "audit.regression"}, pool=pool)
        async with tenant_connection(pool, tenants[0]) as conn:
            assert await conn.fetchval("SELECT count(*) FROM audit_log WHERE request_id=$1", request_id) == 1
        async with tenant_connection(pool, tenants[1]) as conn:
            assert await conn.fetchval("SELECT count(*) FROM audit_log WHERE request_id=$1", request_id) == 0
    finally:
        await admin.execute("DELETE FROM audit_log WHERE request_id=$1", request_id)


async def test_new_demo_identity_gets_membership_without_restoring_revocation(db):
    from forgeflow.auth.users import upsert_local_user
    from forgeflow.auth.membership import user_is_member
    pool, admin, tenants, *_ = db
    name = "rls-seed-" + uuid.uuid4().hex
    user = await upsert_local_user(pool, name, "test-only", "manager", workspace_id=uuid.UUID(tenants[0]), tenant_id=tenants[0])
    try:
        assert await user_is_member(pool, str(user["id"]), tenants[0])
        await admin.execute("DELETE FROM tenant_memberships WHERE user_id=$1", user["id"])
        updated = await upsert_local_user(pool, name, "test-only", "manager", tenant_id=tenants[0])
        assert updated["workspace_id"] == uuid.UUID(tenants[0])
        assert not await user_is_member(pool, str(user["id"]), tenants[0])
    finally:
        await admin.execute("DELETE FROM auth_users WHERE id=$1", user["id"])
