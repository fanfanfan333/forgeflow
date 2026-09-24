"""INC8-A4 — the Cost/SLO degrade action chain must be *really* wired.

Before the fix ``swap_model`` / ``pause_noncritical`` were computed (and
reported on ``/metrics/slo``) but never consumed: no production caller acted on
them, so the platform kept serving every run with the strong model. The active
degrade state now feeds the runtime **admission** point (refuse non-core new
runs with an honest 503) and the **model-selection** point (serve the weak
model). The default state must remain a byte-identical no-op.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from forgeflow.cost.degrade import (
    DegradeState,
    current_degrade_state,
    effective_model_strong,
    reset_degrade_state,
    trigger_degrade,
)


@pytest.fixture(autouse=True)
def _clean_degrade_state():
    reset_degrade_state()
    yield
    reset_degrade_state()


# --- default: no side effects ---------------------------------------------- #


def test_default_state_is_a_no_op():
    assert current_degrade_state() == DegradeState()
    assert current_degrade_state().denies_new_run("finance_recon") is False
    assert effective_model_strong(True) is True
    assert effective_model_strong(False) is False


def test_admission_guard_is_a_no_op_by_default():
    from forgeflow.api.routers.tasks import _admission_guard

    # No degrade in force ⇒ nothing is refused.
    _admission_guard("finance_recon")
    _admission_guard("sales_ops")


# --- admission: pause_noncritical ------------------------------------------ #


def test_pause_noncritical_refuses_non_core_only():
    from forgeflow.api.routers.tasks import _admission_guard

    trigger_degrade("edge", ["pause_noncritical"], reason="SLO breached")

    with pytest.raises(HTTPException) as excinfo:
        _admission_guard("finance_recon")
    assert excinfo.value.status_code == 503
    assert "降级" in excinfo.value.detail

    # Core workflow types keep running.
    _admission_guard("sales_ops")
    _admission_guard("agentflow_task")


def test_post_tasks_returns_503_not_403_when_paused():
    from forgeflow.api.dependencies import get_current_user
    from forgeflow.api.hub_deps import resolve_tenant
    from forgeflow.api.routers import tasks as tasks_router
    from forgeflow.rbac.models import UserContext

    trigger_degrade("edge", ["pause_noncritical"], reason="budget exceeded")

    app = FastAPI()
    app.include_router(tasks_router.router, prefix="/tasks")
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u-1", role="manager"
    )
    app.dependency_overrides[resolve_tenant] = lambda: "tenant-x"

    resp = TestClient(app).post(
        "/tasks", json={"intent": "分析销售数据", "workflow_type": "finance_recon"}
    )
    assert resp.status_code == 503
    assert "降级" in resp.json()["detail"]


# --- model selection: swap_model ------------------------------------------- #


def test_swap_model_serves_the_weak_model():
    trigger_degrade("important", ["swap_model"], reason="cost")

    assert effective_model_strong(True) is False
    assert effective_model_strong(False) is False


def test_swap_model_reaches_the_runtime_model_selection(monkeypatch):
    """The runtime must build the weak model for the strong slot under degrade."""
    from forgeflow.runtime import orchestrator

    calls: list[bool] = []

    def _fake_get_model(strong: bool = True, **kwargs):
        calls.append(strong)
        return object()

    monkeypatch.setattr("forgeflow.models.get_model", _fake_get_model, raising=False)

    # No degrade ⇒ (strong, worker) = (True, False) — the historical default.
    orchestrator._build_planner_models()
    assert calls == [True, False]

    calls.clear()
    trigger_degrade("important", ["swap_model"], reason="cost")

    # Degrade in force ⇒ the strong slot is served by the weak model.
    orchestrator._build_planner_models()
    assert calls == [False, False]


# --- recovery -------------------------------------------------------------- #


def test_reset_recovers_everything():
    trigger_degrade("edge", ["swap_model", "pause_noncritical"], reason="x")
    assert current_degrade_state().denies_new_run("finance_recon") is True
    assert effective_model_strong(True) is False

    reset_degrade_state()

    assert current_degrade_state().denies_new_run("finance_recon") is False
    assert effective_model_strong(True) is True


def test_notify_only_does_not_clear_an_active_degrade():
    trigger_degrade("edge", ["pause_noncritical"], reason="x")
    trigger_degrade("critical", ["notify"], reason="ping")
    # A notify-only trigger must not lift the real degradation.
    assert current_degrade_state().denies_new_run("finance_recon") is True
