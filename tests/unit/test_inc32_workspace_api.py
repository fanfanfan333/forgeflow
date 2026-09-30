"""INC32 T02 — the workspace REST surface (abort / artifact download / RBAC).

Contract tests for the two new run sub-routes and the new ``/workspace`` RBAC
entries:

  * ``POST /runs/{run_id}/abort`` — running → 200; already aborted → 200
    (idempotent, same value); completed / failed → **409** (not 200, not 500);
    unknown run → **404**; ``viewer`` → **403** and the run is untouched (AC-37).
  * ``GET /runs/{run_id}/artifacts/{artifact_id}`` — a real artifact → 200 with
    a body byte-identical to ``artifacts[i].content``; an unknown id → **404**;
    a cross-tenant read → **404** (AC-31 / AC-33).
  * the new ``/workspace`` routes hit ``ROUTE_PERMISSION_MAP`` (otherwise the
    fail-closed RBAC middleware would 403 the endpoint itself).

Every case pins the tenant explicitly and resets the run / workspace / dispatcher
state so it is deterministic under any ``STORAGE_BACKEND``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP
from forgeflow.runtime.dispatcher import reset_run_dispatcher
from forgeflow.runtime.orchestrator import (
    RunRecord,
    get_run_store,
    register_running_run,
    reset_run_store,
)
from forgeflow.workspace.store import reset_workspace_store

TENANT = "t-inc32"
OTHER_TENANT = "t-inc32-other"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    reset_run_dispatcher()
    reset_workspace_store()
    yield
    reset_run_store()
    reset_run_dispatcher()
    reset_workspace_store()


def _client(tenant: str, *, rbac: bool = False) -> TestClient:
    app = FastAPI()
    if rbac:
        app.add_middleware(RBACMiddleware)
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


def _running(run_id: str, tenant: str = TENANT):
    return register_running_run(
        run_id=run_id,
        thread_id=f"{run_id}-th",
        tenant_id=tenant,
        intent="分析销售数据并生成报告",
    )


def _terminal_record(run_id: str, status: str, tenant: str = TENANT) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        thread_id=f"{run_id}-th",
        tenant_id=tenant,
        agent_id=None,
        intent="分析销售数据并生成报告",
        status=status,
        outcome="success" if status == "completed" else "failure",
        steps=[],
        errors=[],
        created_at="2026-10-04T00:00:00+00:00",
        completed_at="2026-10-04T00:00:01+00:00",
        session_id=run_id,
    )


# --------------------------------------------------------------------------- #
# 1. POST /runs/{id}/abort — status semantics                                   #
# --------------------------------------------------------------------------- #
def test_abort_running_run_returns_200_aborted():
    _running("run-abort-1")
    response = _client(TENANT).post("/runs/run-abort-1/abort")
    assert response.status_code == 200, response.text
    assert response.json() == {"run_id": "run-abort-1", "status": "aborted"}
    assert get_run_store().get("run-abort-1").status == "aborted"
    assert get_run_store().get("run-abort-1").outcome == "aborted"


def test_abort_already_aborted_is_idempotent_200():
    _running("run-abort-2")
    client = _client(TENANT)
    first = client.post("/runs/run-abort-2/abort")
    second = client.post("/runs/run-abort-2/abort")
    assert first.status_code == 200 and second.status_code == 200, second.text
    assert first.json() == second.json() == {"run_id": "run-abort-2", "status": "aborted"}


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_abort_terminal_run_returns_409(terminal):
    get_run_store().save(_terminal_record(f"run-abort-{terminal}", terminal))
    response = _client(TENANT).post(f"/runs/run-abort-{terminal}/abort")
    assert response.status_code == 409, response.text
    # A refused Stop must not mutate the terminal record.
    assert get_run_store().get(f"run-abort-{terminal}").status == terminal


def test_abort_unknown_run_returns_404():
    response = _client(TENANT).post("/runs/does-not-exist/abort")
    assert response.status_code == 404, response.text


def test_abort_cross_tenant_is_404():
    _running("run-abort-xt", tenant=OTHER_TENANT)
    response = _client(TENANT).post("/runs/run-abort-xt/abort")
    assert response.status_code == 404, response.text
    assert get_run_store().get("run-abort-xt").status == "running"


# --------------------------------------------------------------------------- #
# 2. Abort RBAC — viewer is refused and the run is untouched (AC-37)            #
# --------------------------------------------------------------------------- #
def test_viewer_cannot_abort_and_run_is_untouched():
    _running("run-abort-rbac")
    token = create_access_token(user_id="viewer-1", role="viewer")
    response = _client(TENANT, rbac=True).post(
        "/runs/run-abort-rbac/abort", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403, response.text
    # RBAC refused before the handler ⇒ the run is still running.
    assert get_run_store().get("run-abort-rbac").status == "running"


def test_admin_can_abort_through_the_rbac_middleware():
    _running("run-abort-admin")
    token = create_access_token(user_id="admin-1", role="admin")
    response = _client(TENANT, rbac=True).post(
        "/runs/run-abort-admin/abort", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "aborted"


# --------------------------------------------------------------------------- #
# 3. GET /runs/{id}/artifacts/{aid} — download (AC-31 / AC-33)                  #
# --------------------------------------------------------------------------- #
def _artifact_run(run_id: str, tenant: str = TENANT) -> str:
    content = "# 运行报告\n\n本轮的交付正文，逐字返回。\nline2\n"
    artifact_id = f"{run_id}:artifact:ref123"
    record = _terminal_record(run_id, "completed", tenant)
    record.artifacts = [
        {
            "id": artifact_id,
            "kind": "report_markdown",
            "title": "运行报告",
            "format": "markdown",
            "content": content,
            "source": "report.render",
            "result_ref": "ref123",
            "created_at": "2026-10-04T00:00:00+00:00",
        }
    ]
    get_run_store().save(record)
    return artifact_id


def test_download_artifact_returns_exact_content():
    artifact_id = _artifact_run("run-art-1")
    response = _client(TENANT).get(f"/runs/run-art-1/artifacts/{artifact_id}")
    assert response.status_code == 200, response.text
    assert response.text == get_run_store().get("run-art-1").artifacts[0]["content"]
    assert "attachment" in response.headers.get("content-disposition", "")
    assert response.headers.get("content-type", "").startswith("text/markdown")


def test_download_unknown_artifact_returns_404():
    _artifact_run("run-art-2")
    response = _client(TENANT).get("/runs/run-art-2/artifacts/not-a-real-id")
    assert response.status_code == 404, response.text


def test_download_artifact_cross_tenant_is_404():
    artifact_id = _artifact_run("run-art-3")
    response = _client(OTHER_TENANT).get(f"/runs/run-art-3/artifacts/{artifact_id}")
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- #
# 4. /workspace routes are mapped (fail-closed RBAC would otherwise 403 them)    #
# --------------------------------------------------------------------------- #
def test_workspace_routes_hit_the_route_permission_map():
    assert ROUTE_PERMISSION_MAP[("POST", "/workspace")] == ("execute", "workflows")
    assert ROUTE_PERMISSION_MAP[("GET", "/workspace")] == ("read", "workflows")
    assert RBACMiddleware._resolve_permission("POST", "/workspace/tasks") == (
        "execute",
        "workflows",
    )
    assert RBACMiddleware._resolve_permission("GET", "/workspace/sessions") == (
        "read",
        "workflows",
    )
    assert RBACMiddleware._resolve_permission("GET", "/workspace/sessions/s-1") == (
        "read",
        "workflows",
    )
