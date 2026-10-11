"""INC54 §十一/§十二/§十三 — Skill 生命周期管理（退役 / 恢复 / 删除草稿）。

为什么有这个文件
----------------
技能的「下线」此前只有两种手段：改状态（语义不明）或让流程绕过。INC54 把
「退役」「恢复」「删除草稿」拆成三条**显式**的语义，本文件逐条钉死它们的
**可观察行为**与**拒绝语义**：

  * ``POST /skills/{id}/retire``  —— ``published → retired``，保留全部历史，
    仅让技能在运行期**不可执行**（``retired`` 映射 DEPRECATED，而
    ``eligibility.EXECUTABLE_STATUS == "published"``）；未知 id ⇒ 404。
  * ``POST /skills/{id}/restore`` —— ``retired → published``，退役的逆操作；
    未知 id ⇒ 404。
  * ``DELETE /skills/{id}``       —— **只有草稿可删**：非草稿 ⇒ 409（消息要求改用
    退役）；已有版本记录 ⇒ 409（INC54 §十一 引用完整性）；未知 id ⇒ 404；
    草稿删除成功 ⇒ 204，删后该 id 再也读不到（GET ⇒ 404）。

诚实纪律：每个失败分支都断言**具体状态码**（不写 ``in (...)``），并用「删后仍/不再
可读」做正反双向对照 —— 删掉了是事实，不是「接口没报错」。

沿用 ``tests/unit/test_inc34_skill_spec_validation.py`` 的既有 app 装配写法
（裸 ``FastAPI`` + ``RBACMiddleware`` + 两条 router，JWT 用
``create_access_token(workspace_id=...)`` 定租户），只把同步 ``TestClient`` 换成
httpx 的异步 ``ASGITransport``（``asyncio_mode="auto"``），以便与「造数 → 打接口」
跑在**同一个事件循环**上。
"""

from __future__ import annotations

from tests.business_auth import authenticate_business_app

import uuid

import httpx
import pytest
from fastapi import FastAPI

from forgeflow.api.routers import skills as skills_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.repositories.memory.skill_repo import clear_skill_store
from forgeflow.skills.registry import SkillRegistry

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Builders / fixtures                                                          #
# --------------------------------------------------------------------------- #
def _build_app() -> FastAPI:
    """The same minimal app the INC34 pins assemble — router + RBAC middleware."""
    app = FastAPI()
    authenticate_business_app(app)
    app.add_middleware(RBACMiddleware)
    app.include_router(skills_router.router, prefix="/skills")
    app.include_router(skills_router.candidates_router, prefix="/skill-candidates")
    return app


@pytest.fixture
async def client():
    """An httpx async client bound to the minimal app (one loop for the test)."""
    transport = httpx.ASGITransport(app=_build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _memory_profile(force_memory_backend):
    """Pin to the memory backend and give each test a clean skill store."""
    clear_skill_store()
    yield
    clear_skill_store()


def _tenant_headers(role: str = "manager") -> tuple[str, dict[str, str]]:
    """Return ``(tenant, headers)`` — the tenant is the JWT ``workspace`` claim."""
    tenant = f"t-inc54-{uuid.uuid4().hex[:8]}"
    token = create_access_token(user_id=f"{role}-1", role=role, workspace_id=tenant)
    return tenant, {"Authorization": f"Bearer {token}"}


async def _create_draft(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/skills",
        json={"name": f"skill-{uuid.uuid4().hex[:6]}", "domain": "general"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"  # the create default is a draft
    return resp.json()["id"]


async def _publish(tenant: str, skill_id: str) -> None:
    """Flip a freshly-created draft to ``published`` via the domain service."""
    updated = await SkillRegistry().set_status(tenant, skill_id, "published")
    assert updated is not None and updated.status == "published"


async def _read_back(tenant: str, skill_id: str):
    """Read the persisted skill through the same repository the routes use."""
    return await SkillRegistry().get(tenant, skill_id)


# --------------------------------------------------------------------------- #
# 1 · retire: published → retired（响应 status 字段 + 持久化）                   #
# --------------------------------------------------------------------------- #
async def test_retire_published_becomes_retired(client):
    tenant, headers = _tenant_headers()
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)

    resp = await client.post(f"/skills/{skill_id}/retire", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "retired"
    # 持久化事实（不是仅回显）：重新读出的状态也是 retired。
    stored = await _read_back(tenant, skill_id)
    assert stored is not None and stored.status == "retired"


async def test_archived_skill_disappears_from_library_but_preserves_versions(client):
    from forgeflow.repositories import get_skill_repository
    from forgeflow.skills.models import SkillVersionRecord

    tenant, headers = _tenant_headers()
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)
    await get_skill_repository().add_version(
        tenant,
        SkillVersionRecord(
            tenant_id=tenant,
            skill_id=skill_id,
            semver="1.0.0",
            spec={"tools": ["textfile.inspect"]},
        )
    )
    before = await SkillRegistry().versions(tenant, skill_id)
    assert len(before) == 1
    response = await client.post(f"/skills/{skill_id}/archive", headers=headers)
    assert response.status_code == 200, response.text
    listed = (await client.get("/skills", headers=headers)).json()
    assert skill_id not in {s["id"] for s in listed["items"]}
    stored = await _read_back(tenant, skill_id)
    assert stored.status == "archived"
    assert await SkillRegistry().versions(tenant, skill_id) == before
    other_tenant, other_headers = _tenant_headers()
    assert other_tenant != tenant
    assert (
        await client.post(f"/skills/{skill_id}/archive", headers=other_headers)
    ).status_code == 404


async def test_retired_skill_is_not_executable(client):
    """退役的语义 = 运行期不可执行（``retired`` ≠ ``published``）。"""
    from forgeflow.skills.eligibility import EXECUTABLE_STATUS

    tenant, headers = _tenant_headers()
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)
    await client.post(f"/skills/{skill_id}/retire", headers=headers)

    stored = await _read_back(tenant, skill_id)
    assert stored.status == "retired" and stored.status != EXECUTABLE_STATUS


# --------------------------------------------------------------------------- #
# 2 · restore: retired → published                                              #
# --------------------------------------------------------------------------- #
async def test_restore_retired_becomes_published(client):
    tenant, headers = _tenant_headers()
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)
    await client.post(f"/skills/{skill_id}/retire", headers=headers)  # → retired

    resp = await client.post(f"/skills/{skill_id}/restore", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "published"
    stored = await _read_back(tenant, skill_id)
    assert stored is not None and stored.status == "published"


# --------------------------------------------------------------------------- #
# 3 · retire / restore 未知 id ⇒ 404                                            #
# --------------------------------------------------------------------------- #
async def test_retire_unknown_skill_is_404(client):
    _tenant, headers = _tenant_headers()
    resp = await client.post("/skills/does-not-exist/retire", headers=headers)
    assert resp.status_code == 404


async def test_restore_unknown_skill_is_404(client):
    _tenant, headers = _tenant_headers()
    resp = await client.post("/skills/does-not-exist/restore", headers=headers)
    assert resp.status_code == 404


async def test_retire_foreign_tenant_id_is_404(client):
    """跨租户读不到 ⇒ 404（租户 fail-closed，绝不跨租户改状态）。"""
    owner_tenant, owner_headers = _tenant_headers()
    skill_id = await _create_draft(client, owner_headers)
    await _publish(owner_tenant, skill_id)

    _other_tenant, other_headers = _tenant_headers()
    resp = await client.post(f"/skills/{skill_id}/retire", headers=other_headers)
    assert resp.status_code == 404
    # 未被越权修改：owner 读到的仍是 published。
    assert (await _read_back(owner_tenant, skill_id)).status == "published"


# --------------------------------------------------------------------------- #
# 4 · delete draft ⇒ 204；删后 GET ⇒ 404                                        #
# --------------------------------------------------------------------------- #
async def test_delete_draft_is_204_then_unreadable(client):
    tenant, headers = _tenant_headers()
    skill_id = await _create_draft(client, headers)

    resp = await client.delete(f"/skills/{skill_id}", headers=headers)
    assert resp.status_code == 204
    assert resp.content == b""  # 204 不该带内容

    # 删后该 id 再也读不到 —— 用单技能 GET（/skills/{id}/export）如实 404。
    assert (await client.get(f"/skills/{skill_id}/export", headers=headers)).status_code == 404
    assert await _read_back(tenant, skill_id) is None


# --------------------------------------------------------------------------- #
# 5 · delete published ⇒ 409（要求改用退役）                                     #
# --------------------------------------------------------------------------- #
async def test_delete_published_is_409_and_survives(client):
    tenant, headers = _tenant_headers()
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)

    resp = await client.delete(f"/skills/{skill_id}", headers=headers)

    assert resp.status_code == 409
    assert "退役" in resp.json()["detail"]
    # 被拒绝后技能仍在（未误删）。
    assert (await client.get(f"/skills/{skill_id}/export", headers=headers)).status_code == 200
    assert (await _read_back(tenant, skill_id)) is not None


async def test_delete_retired_is_409(client):
    """已退役的技能与已发布一样受保护（走退役，不做硬删除）。"""
    tenant, headers = _tenant_headers()
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)
    await client.post(f"/skills/{skill_id}/retire", headers=headers)

    resp = await client.delete(f"/skills/{skill_id}", headers=headers)
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# 6 · delete 未知 id ⇒ 404                                                      #
# --------------------------------------------------------------------------- #
async def test_delete_unknown_skill_is_404(client):
    _tenant, headers = _tenant_headers()
    resp = await client.delete("/skills/does-not-exist", headers=headers)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# 7 · 引用完整性 —— 有版本的草稿不可硬删除（INC54 §十一）                         #
# --------------------------------------------------------------------------- #
async def test_delete_draft_with_versions_is_409(client):
    tenant, headers = _tenant_headers()
    skill_id = await _create_draft(client, headers)
    # 给草稿挂一个版本（API 直接指定 semver，落 skill_versions 行）。
    created = await client.post(
        f"/skills/{skill_id}/versions",
        json={"semver": "1.0.0", "spec": {"prompt": "示例"}, "changelog": "初版"},
        headers=headers,
    )
    assert created.status_code == 200, created.text

    resp = await client.delete(f"/skills/{skill_id}", headers=headers)

    assert resp.status_code == 409
    assert "引用完整性" in resp.json()["detail"]
    # 未误删：技能与其版本都还在。
    assert (await _read_back(tenant, skill_id)) is not None
    versions = await SkillRegistry().versions(tenant, skill_id)
    assert [v.semver for v in versions] == ["1.0.0"]


# --------------------------------------------------------------------------- #
# 8 · RBAC —— 无 write:skills 的角色被中间件拒绝（403）                          #
# --------------------------------------------------------------------------- #
async def test_viewer_cannot_retire(client):
    tenant, manager_headers = _tenant_headers()
    skill_id = await _create_draft(client, manager_headers)
    await _publish(tenant, skill_id)

    _viewer_tenant, viewer_headers = _tenant_headers(role="viewer")
    resp = await client.post(f"/skills/{skill_id}/retire", headers=viewer_headers)

    assert resp.status_code == 403
    # 未越权：仍是 published。
    assert (await _read_back(tenant, skill_id)).status == "published"
