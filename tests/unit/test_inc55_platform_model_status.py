"""INC55 缺陷② 后端 —— ``GET /runs/model-status``（平台配置模型，只读、不探测）。

现场：首页底部「模型标识」显示「模型：未记录」。根因分两段 ——

  * 后端：``GET /runs/{id}`` 对**未保留执行明细**的 run 返回 ``llm = {}``（空对象，
    ``detail_retained=false``）；27 条 run 里 18 条如此。
  * 前端：``deriveModelInfo()`` 只判 ``models.length === 0``，于是把空对象 ``{}`` 误判成
    「有记录但 models 为空」⇒ 「模型：未记录」。

本文件钉住**新增只读端点**：暴露平台**当前配置**的模型（provider + model 名），供
「无 run 记录」时展示；且硬约束 —— 复用既有 provider 事实源、不硬编码模型名、**不**在
GET 内发起阻塞式 LLM 调用、真取不到就诚实返回「未知」。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.config import get_settings
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP

TENANT = "t-inc55"


def _client(tenant: str = TENANT, *, rbac: bool = False) -> TestClient:
    app = FastAPI()
    if rbac:
        app.add_middleware(RBACMiddleware)
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


def _auth(role: str, user: str = "u-inc55") -> dict[str, str]:
    token = create_access_token(user_id=user, role=role)
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 1. 正常返回 —— 复用 provider 事实源，不硬编码模型名                            #
# --------------------------------------------------------------------------- #
def test_model_status_reports_the_configured_model_from_settings():
    settings = get_settings()
    provider = (settings.llm_provider or "").strip().lower()

    body = _client().get("/runs/model-status").json()

    assert body["provider"] == provider
    # 只读端点**从不**做可达性探测 —— 语义必须透明。
    assert body["probed"] is False
    assert isinstance(body["models"], list)

    if provider == "ollama":
        assert body["models"], "ollama 档应能描述出配置的槽位"
        # 模型名来自 Settings（provider 事实源），**不是**硬编码字面量。
        assert body["models"][0]["model"] == settings.ollama_model_strong
        assert body["models"][1]["model"] == settings.ollama_model
        assert all(m["slot"] in ("strong", "worker") for m in body["models"])
        assert all(m["llm_type"] for m in body["models"])
    elif provider == "mock":
        assert len(body["models"]) == 2  # strong + worker（离线桩）


def test_static_route_wins_over_the_dynamic_run_id_route():
    """``/runs/model-status`` 不得被 ``/runs/{run_id}`` 吞掉（路由顺序）。"""
    body = _client().get("/runs/model-status").json()
    # 平台响应有 ``provider``；运行详情响应则不会有。且绝不会是 404「Run not found」。
    assert "provider" in body and "run_id" not in body


def test_endpoint_performs_no_blocking_probe(monkeypatch):
    """在 GET 内**不得**发起 Ollama 可达性探测（那会阻塞）。"""
    from forgeflow.models import provider as provider_mod

    def _boom(_settings):  # pragma: no cover — 只要被调用就失败
        raise AssertionError("model-status 端点不应触发 _ollama_available 探测")

    monkeypatch.setattr(provider_mod, "_ollama_available", _boom)
    resp = _client().get("/runs/model-status")
    assert resp.status_code == 200
    assert resp.json()["probed"] is False


# --------------------------------------------------------------------------- #
# 2. 诚实空态 —— 配置不可描述 ⇒ 空 models，绝不编造                               #
# --------------------------------------------------------------------------- #
def test_unknown_provider_yields_honest_empty_state(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "weird-provider-not-real")

    body = _client().get("/runs/model-status").json()

    assert body["provider"] == "weird-provider-not-real"
    assert body["models"] == []  # 诚实「未知」，绝不伪造模型名
    assert body["probed"] is False


def test_describe_failure_degrades_to_empty_not_five_xx(monkeypatch):
    """构建描述抛错时必须降级为诚实空态（200 + 空 models），不得 5xx。"""

    def _boom(settings, model_name):  # pragma: no cover — 由 monkeypatch 触达
        raise RuntimeError("builder exploded")

    from forgeflow.models import provider as provider_mod

    monkeypatch.setattr(provider_mod, "_build_ollama_model", _boom)
    monkeypatch.setattr(get_settings(), "llm_provider", "ollama")

    resp = _client().get("/runs/model-status")
    assert resp.status_code == 200
    assert resp.json()["models"] == []


# --------------------------------------------------------------------------- #
# 3. 授权 / 越权                                                               #
# --------------------------------------------------------------------------- #
def test_route_is_registered_and_mapped_to_read_workflows():
    assert ROUTE_PERMISSION_MAP[("GET", "/runs/model-status")] == ("read", "workflows")
    assert RBACMiddleware._resolve_permission("GET", "/runs/model-status") == (
        "read",
        "workflows",
    )


def test_unauthenticated_is_401():
    resp = _client(rbac=True).get("/runs/model-status")
    assert resp.status_code == 401


def test_manager_can_read_model_status():
    resp = _client(rbac=True).get("/runs/model-status", headers=_auth("manager"))
    assert resp.status_code == 200
    assert "provider" in resp.json()


def test_viewer_can_read_model_status():
    resp = _client(rbac=True).get("/runs/model-status", headers=_auth("viewer"))
    assert resp.status_code == 200


def test_role_without_read_workflows_is_403():
    """越权路径：`anonymous` 不持 `read:workflows` ⇒ fail-closed 403。"""
    resp = _client(rbac=True).get("/runs/model-status", headers=_auth("anonymous"))
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["manager", "sales_rep", "viewer"])
def test_all_real_reader_roles_are_allowed(role: str):
    resp = _client(rbac=True).get("/runs/model-status", headers=_auth(role))
    assert resp.status_code == 200
