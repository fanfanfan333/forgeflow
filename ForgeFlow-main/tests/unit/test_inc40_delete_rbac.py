"""INC40 — 删除 / 归档接线不得打破 RBAC fail-closed 钉子（单测）。

新写端点（`DELETE /resources/{id}`、`POST`/`DELETE /cost/budgets`、
`POST /memory/{id}/archive`）必须：
  * 在 `ROUTE_PERMISSION_MAP` 里有真实条目（否则 `test_fact_source_alignment.py::
    test_no_phantom_route_permission_entries` 会红——路由与映射**同批**落地）；
  * fail-closed：无权的角色经真实 RBACMiddleware 被拒 **403**，未带令牌 **401**。

引文纪律：一律 `file.py::symbol`，不用行号。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import resources as resources_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.enforcer import RBACEnforcer
from forgeflow.rbac.policies import ROLE_PERMISSIONS, ROUTE_PERMISSION_MAP

_ENFORCER = RBACEnforcer()


# --------------------------------------------------------------------------- #
# 1. 映射：三条新条目如实存在且用对权限族                                        #
# --------------------------------------------------------------------------- #
def test_new_route_permissions_are_mapped():
    assert ROUTE_PERMISSION_MAP[("DELETE", "/resources")] == ("write", "skills")
    assert ROUTE_PERMISSION_MAP[("POST", "/cost")] == ("write", "metrics")
    assert ROUTE_PERMISSION_MAP[("DELETE", "/cost")] == ("write", "metrics")

    # 最长前缀匹配对真实路径生效。
    assert RBACMiddleware._resolve_permission("DELETE", "/resources/res-1") == ("write", "skills")
    assert RBACMiddleware._resolve_permission("POST", "/cost/budgets") == ("write", "metrics")
    assert RBACMiddleware._resolve_permission("DELETE", "/cost/budgets/tenant") == (
        "write",
        "metrics",
    )
    assert RBACMiddleware._resolve_permission("POST", "/memory/m-1/archive") == (
        "write",
        "memory",
    )


# --------------------------------------------------------------------------- #
# 2. 授权矩阵：每个新 gate 都有非 admin 持有者，无权角色被拒                      #
# --------------------------------------------------------------------------- #
def test_resource_delete_gate_grants_and_denials():
    action, resource = ("write", "skills")
    assert _ENFORCER.check("manager", action, resource)
    assert _ENFORCER.check("admin", action, resource)
    assert not _ENFORCER.check("viewer", action, resource)
    assert not _ENFORCER.check("sales_rep", action, resource)


def test_budget_write_gate_grants_and_denials():
    action, resource = ("write", "metrics")
    # manager 拿到了 write:metrics（INC40 有意授予，避免「能看看板、不能管预算」倒挂）。
    assert "write:metrics" in ROLE_PERMISSIONS["manager"]
    assert _ENFORCER.check("manager", action, resource)
    assert _ENFORCER.check("admin", action, resource)
    assert not _ENFORCER.check("viewer", action, resource)


def test_memory_archive_gate_grants_and_denials():
    # Archive reuses the existing ("POST","/memory") → write:memory entry (no new
    # permission). Only sales_rep holds write:memory; manager holds read:memory
    # only, so it is DENIED here — the design (§3.5 / §7) deliberately does not
    # widen this: "无 write:memory 者 → 403".
    action, resource = ("write", "memory")
    assert _ENFORCER.check("sales_rep", action, resource)  # holds write:memory
    assert _ENFORCER.check("admin", action, resource)  # *:*
    assert not _ENFORCER.check("manager", action, resource)  # read:memory only
    assert not _ENFORCER.check("viewer", action, resource)


# --------------------------------------------------------------------------- #
# 3. 真实中间件：无权 403 / 未带令牌 401（fail-closed 未被打破）                  #
# --------------------------------------------------------------------------- #
def _rbac_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RBACMiddleware)
    app.include_router(resources_router.router, prefix="/resources")
    return TestClient(app)


def _token(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id=f'{role}-1', role=role)}"}


def test_unauthenticated_delete_is_401():
    assert _rbac_client().delete("/resources/res-x").status_code == 401


@pytest.mark.parametrize("role", ["viewer", "sales_rep"])
def test_unauthorized_delete_is_403(role):
    resp = _rbac_client().delete("/resources/res-x", headers=_token(role))
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_authorized_delete_passes_the_middleware(role):
    # 资源不存在 ⇒ 路由 404（证明已越过 RBAC；不是 403）。
    resp = _rbac_client().delete("/resources/res-x", headers=_token(role))
    assert resp.status_code == 404, resp.text
