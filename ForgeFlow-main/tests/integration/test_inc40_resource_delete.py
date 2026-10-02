"""INC40 — 资源删除：真实路由函数 + 双档（memory / postgres）。

钉住「接线 + 前端可达」的后端契约（经**真实路由函数**，语义等价前端
`request<T>`）：

  * `DELETE /resources/{id}` → 200 `ResourceDeleteResponse`（**JSON 体，非 204**）；
  * 随后 `GET /resources/{id}` → **404**；`GET /resources` 列表**不含**该 id；
  * 跨租户删 → **404**（不泄露存在性），且原租户资源仍在；
  * 不产生悬垂引用：删除后 `ResourceService.resolve_task_inputs` 不再解引用该 id
    （证 `_RESOURCE_INDEX.pop` 生效，【B6】）。

档位声明：`memory` 与 `postgres` 两档各自声明（postgres 档为判别档；需真实库）。

引文纪律：一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid

import pytest
from fastapi import HTTPException

from forgeflow.config import get_settings
from forgeflow.repositories.factory import reset_repositories
from forgeflow.repositories.memory.resource_repo import clear_resource_store
from forgeflow.resources.service import ResourceService, reset_resource_index

pytestmark = pytest.mark.asyncio

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _suffix() -> str:
    return uuid.uuid4().hex[:10]


# --------------------------------------------------------------------------- #
# 档位激活（镜像 tests/integration/test_inc33_history_rehydrate.py 的写法）       #
# --------------------------------------------------------------------------- #
def _postgres_reachable() -> bool:
    import psycopg

    dsn = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
    try:
        with psycopg.connect(dsn, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:  # noqa: BLE001 — any failure means "not usable here"
        return False


def _upgrade_head() -> None:
    child_env = dict(os.environ)
    child_env["POSTGRES_SYNC_URL"] = get_settings().postgres_sync_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr[-2000:]}")


def _require_postgres() -> None:
    if not _postgres_reachable():
        pytest.skip("live Postgres not reachable — resource delete PG case needs it")
    try:
        _upgrade_head()
    except Exception as exc:  # noqa: BLE001 — an unusable DB means "skip here"
        pytest.skip(f"dev DB could not be migrated to head: {exc}")


def _purge_resource_rows(*tenants: str) -> None:
    """Delete only this case's own resource rows (memory profile ⇒ no-op).

    Deliberately **not** ``pg_purge`` (whole-table ``DELETE``) — a targeted delete
    keeps the shared dev DB's other tenants/tests intact.
    """
    settings = get_settings()
    if settings.storage_backend.lower() != "postgres":
        return
    import psycopg

    dsn = settings.postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for tenant in tenants:
            cur.execute("DELETE FROM resources WHERE tenant_id = %s", (tenant,))
        conn.commit()


@pytest.fixture
def active_backend(request, monkeypatch):
    backend = request.param
    if backend == "memory":
        request.getfixturevalue("force_memory_backend")
        reset_resource_index()
        clear_resource_store()
        yield "memory"
        reset_resource_index()
        clear_resource_store()
        return

    _require_postgres()
    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    reset_repositories()
    reset_resource_index()
    try:
        yield "postgres"
    finally:
        reset_repositories()
        reset_resource_index()


# --------------------------------------------------------------------------- #
# 200 + 不在列表 + 详情 404                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_delete_returns_200_then_gone_from_list_and_detail(active_backend):
    from forgeflow.api.routers.resources import delete_resource as delete_route
    from forgeflow.api.routers.resources import get_resource as get_route
    from forgeflow.api.routers.resources import list_resources as list_route

    tenant = f"t-inc40-del-{_suffix()}"
    try:
        record = await ResourceService().register_database(tenant, table="public.leads")
        rid = record.id

        # DELETE → 200 + 真实 JSON 体（ResourceDeleteResponse）。
        resp = await delete_route(rid, tenant=tenant)
        assert resp.deleted is True
        assert resp.resource_id == rid

        # 详情 → 404。
        with pytest.raises(HTTPException) as ei:
            await get_route(rid, tenant=tenant)
        assert ei.value.status_code == 404

        # 列表不含该 id。
        listing = await list_route(kind=None, limit=50, offset=0, tenant=tenant)
        assert rid not in {item.id for item in listing.items}
    finally:
        _purge_resource_rows(tenant)


# --------------------------------------------------------------------------- #
# 跨租户 404（且原租户资源仍在）                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("active_backend", ["memory", "postgres"], indirect=True)
async def test_delete_cross_tenant_is_404_and_owner_keeps_resource(active_backend):
    from forgeflow.api.routers.resources import delete_resource as delete_route
    from forgeflow.api.routers.resources import get_resource as get_route

    owner = f"t-inc40-owner-{_suffix()}"
    other = f"t-inc40-other-{_suffix()}"
    try:
        record = await ResourceService().register_database(owner, table="public.orders")

        with pytest.raises(HTTPException) as ei:
            await delete_route(record.id, tenant=other)
        assert ei.value.status_code == 404

        # 原租户资源仍可读。
        still = await get_route(record.id, tenant=owner)
        assert still.id == record.id
    finally:
        _purge_resource_rows(owner, other)


# --------------------------------------------------------------------------- #
# 无悬垂引用：删除后 __RESOURCE_INDEX 不再解引用该 id【B6】                      #
# --------------------------------------------------------------------------- #
async def test_delete_drops_the_dereference_index(force_memory_backend):
    reset_resource_index()
    clear_resource_store()
    service = ResourceService()
    tenant = f"t-inc40-dangling-{_suffix()}"

    record = await service.register_database(tenant, table="public.leads")
    # 登记后：声明该 id ⇒ 解引用出真实 table（证明索引在手）。
    assert service.resolve_task_inputs({"resources": [record.id]}) == {"table": "public.leads"}

    assert await service.delete(tenant, record.id) is True

    # 删除后：索引已剔除 ⇒ 不再解引用（不留悬垂引用）。
    assert service.resolve_task_inputs({"resources": [record.id]}) == {}
