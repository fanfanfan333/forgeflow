"""INC14 — result-first contract at the REST seam (任务结果优先, 后端半).

These are **contract** tests: they drive a real (offline, deterministic) run
through the runtime, then read the result back through the *real*
``GET /runs/{id}`` route — never off a hand-built Python object — so a field
that the orchestrator measured but the REST layer drops (this project's
recurring "looks wired, silently isn't" defect) turns red.

Covered:
  * a real offline run **really produces** an artifact, exposed verbatim on
    ``GET /runs/{id}.artifacts`` (kind / format / source / content / result_ref);
  * a run whose plan has no ``report.render`` step returns ``artifacts == []``
    (the honest empty state, never a fabricated result);
  * ``POST /tasks`` forwards the caller's ``context`` into the run's
    ``TaskCreate`` (the "继续执行" contract: the previous run id reaches the
    runtime instead of being silently dropped at the route).

Every case declares its storage tier explicitly via ``force_memory_backend`` so
the assertions read the in-process hub run store under any ``STORAGE_BACKEND``.
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import forgeflow.runtime.orchestrator as orch
from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.governance.models import EvalDecision
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.orchestrator import (
    RequestContext,
    RunHandle,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)

pytestmark = pytest.mark.asyncio

TENANT = "t-inc14"
INTENT = "为 Acme 整理一份销售线索分析摘要"


class _RecordingBus:
    """Minimal bus stub — ``run_task`` only ever calls ``emit``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


@pytest.fixture(autouse=True)
def _clean_store():
    reset_run_store()
    yield
    reset_run_store()


def _detail_client(tenant: str) -> TestClient:
    """Mount the real runs router with the tenant pinned."""
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


def _ctx() -> RequestContext:
    return RequestContext(tenant_id=TENANT, user_id="u-inc14", role="admin")


# --------------------------------------------------------------------------- #
# 1. A real offline run really produces an artifact, exposed via GET /runs/{id} #
# --------------------------------------------------------------------------- #
async def test_offline_run_exposes_a_real_artifact(force_memory_backend):
    handle = await run_task(
        TaskCreate(intent=INTENT, workflow_type="generic"),
        _ctx(),
        bus=_RecordingBus(),
    )
    assert handle.status == "completed"

    response = _detail_client(TENANT).get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    artifacts = body["artifacts"]
    assert artifacts, "a real offline run produced no artifact"
    art = artifacts[0]
    assert art["kind"] == "report_markdown"
    assert art["format"] == "markdown"
    assert art["title"] == "运行报告"
    assert art["source"] == "report.render"
    assert "运行报告" in art["content"]
    # The ref is a real digest of the content, not a constant.
    assert art["result_ref"] == hashlib.sha256(art["content"].encode("utf-8")).hexdigest()[:32]
    assert art["id"].startswith(f"{handle.run_id}:artifact:")

    # The RunHandle detail carries the same artifacts (symmetric with
    # tool_invocations) so a synchronous caller sees them without a re-fetch.
    assert handle.detail["artifacts"] == artifacts


# --------------------------------------------------------------------------- #
# 2. A run with no report.render step returns an honest empty list             #
# --------------------------------------------------------------------------- #
async def test_run_without_a_report_step_returns_empty_artifacts(
    monkeypatch, force_memory_backend
):
    monkeypatch.setattr(
        orch,
        "_DEFAULT_STEPS",
        [{"tool": "research.search", "step_type": "agent", "note": "研究助手检索资料"}],
    )

    handle = await run_task(
        TaskCreate(intent=INTENT, workflow_type="generic"),
        _ctx(),
        bus=_RecordingBus(),
    )

    response = _detail_client(TENANT).get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    assert response.json()["artifacts"] == []
    assert handle.detail["artifacts"] == []


# --------------------------------------------------------------------------- #
# 3. POST /tasks forwards the caller's context into the run                    #
# --------------------------------------------------------------------------- #
async def test_post_tasks_forwards_context_into_the_run(monkeypatch, force_memory_backend):
    """The "继续执行" contract: ``context`` in the request body reaches
    ``TaskCreate`` (where ``run_task`` can act on it) instead of being dropped."""
    from forgeflow.api.routers import tasks as tasks_router

    captured: dict[str, object] = {}

    async def _fake_run_task(task, ctx, **kwargs):
        captured["task"] = task
        return RunHandle(run_id="r-inc14", thread_id="th-inc14", status="completed", detail={})

    monkeypatch.setattr(
        tasks_router,
        "evaluate_task_entry",
        AsyncMock(return_value=EvalDecision(effect="allow", risk_level="low")),
    )
    monkeypatch.setattr(tasks_router, "run_task", _fake_run_task)

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u-inc14", role="admin"
    )
    app.dependency_overrides[resolve_tenant] = lambda: TENANT
    app.include_router(tasks_router.router, prefix="/tasks")

    response = TestClient(app).post(
        "/tasks",
        json={
            "intent": "继续执行：补充客户分层",
            "workflow_type": "generic",
            "context": {"continued_from_run_id": "run-abc", "continued_from_artifact_ref": "ref-xyz"},
        },
    )

    assert response.status_code == 200, response.text
    task = captured["task"]
    assert task.context["continued_from_run_id"] == "run-abc"
    assert task.context["continued_from_artifact_ref"] == "ref-xyz"
