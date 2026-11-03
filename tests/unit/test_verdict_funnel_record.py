"""Verdict-funnel observability: the funnel's dropped errors must be
readable from GET /runs/{id} itself (2026-11-03 fc4716d round).

The fc4716d round's B run had the delegate-failed + code-succeeded
shape, but no filter before/after values could be found: the funnel
only logged to the server log, which is not part of the run evidence
a test round collects. So the funnel's drops are now recorded on the
run itself, through the same four-layer chain as the loop-breaker
audit trail::

    RunRecord.verdict_funnel_dropped_errors  ->  run_task construction
                    ->  RunDetailResponse  ->  GET /runs/{id}

A test round proves ③ by reading that field on the B run's record:
the delegate error listed there, with ``errors`` empty, is the
funnel's action — no server-log hunt required.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.orchestrator import (
    RequestContext,
    RunRecord,
    TaskCreate,
    _funnel_dropped_errors,
    get_run_store,
    reset_run_store,
    run_task,
)


class _RecordingBus:
    """Minimal bus stub — ``run_task`` only ever calls ``emit``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def _code_execute_ok() -> dict:
    return {
        "tool": "code.execute",
        "status": "ok",
        "step_id": "s-exec",
        "payload": {
            "ok": True,
            "codeplane": {
                "diff": "--- a/calc.py\n+++ b/calc.py\n",
                "tests": {"measured": True, "verdict": "passed"},
            },
        },
    }


def _delegate_error(detail: str = "子任务撞上限") -> dict:
    return {
        "tool": "agent.delegate",
        "status": "error",
        "step_id": "s-del",
        "error": detail,
        "summary": detail,
        "payload": {"ok": False, "role": "code_review", "error": detail},
    }


def _delegate_errors(detail: str = "子任务撞上限") -> list[str]:
    return [
        f"工具 'agent.delegate' 未成功执行：{detail}",
        f"工具 'agent.delegate' 尚未修复：{detail}",
    ]


def _runs_client(tenant: str, record) -> TestClient:
    """Mount the real runs router with the tenant pinned and ``record`` stored."""
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=record.actor_user_id, role="admin"
    )
    get_run_store().save(record)
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts from an empty run store."""
    reset_run_store()
    yield
    reset_run_store()


def test_funnel_dropped_errors_diff() -> None:
    assert _funnel_dropped_errors(["a", "b"], ["b"]) == ["a"]
    assert _funnel_dropped_errors(["a"], ["a"]) == []
    assert _funnel_dropped_errors([], []) == []


def test_run_record_defaults_to_empty_dropped_list() -> None:
    record = RunRecord(
        run_id="r-1",
        thread_id="t-1",
        tenant_id="tenant-a",
        agent_id=None,
        intent="i",
        status="completed",
        outcome="success",
        steps=[],
        errors=[],
        created_at="2026-11-03",
    )
    assert record.verdict_funnel_dropped_errors == []
    assert asdict(record)["verdict_funnel_dropped_errors"] == []


@pytest.mark.asyncio
async def test_run_detail_exposes_funnel_dropped_errors(monkeypatch) -> None:
    """The ③ proof, end to end: a run whose delegate error the funnel
    dropped must show it on GET /runs/{id} while ``errors`` stays empty."""
    import forgeflow.runtime.orchestrator as orch

    real = orch._default_executor

    async def _exec(task, ctx, bus, run_id, *, policy_engine=None, attempt=0):
        steps, _errors = await real(
            task, ctx, bus, run_id, policy_engine=policy_engine, attempt=attempt
        )
        trail = list(task.context.get("tool_invocations") or [])
        trail.append(_code_execute_ok())
        trail.append(_delegate_error())
        task.context["tool_invocations"] = trail
        return steps, _delegate_errors()

    monkeypatch.setattr(orch, "_default_executor", _exec)
    ctx = RequestContext(tenant_id="t-funnel-record", user_id="admin-1", role="admin")
    handle = await run_task(
        TaskCreate(intent="离线分析一次"), ctx, bus=_RecordingBus()
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None
    assert record.errors == []
    assert record.verdict_funnel_dropped_errors == _delegate_errors()

    client = _runs_client(record.tenant_id, record)
    response = client.get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    assert body["verdict_funnel_dropped_errors"] == _delegate_errors()


@pytest.mark.asyncio
async def test_run_detail_keeps_empty_dropped_list_without_funnel_action(
    monkeypatch,
) -> None:
    """Negative control: a run the funnel did not touch must not
    fabricate a drop."""
    import forgeflow.runtime.orchestrator as orch

    real = orch._default_executor

    async def _exec(task, ctx, bus, run_id, *, policy_engine=None, attempt=0):
        return await real(
            task, ctx, bus, run_id, policy_engine=policy_engine, attempt=attempt
        )

    monkeypatch.setattr(orch, "_default_executor", _exec)
    ctx = RequestContext(tenant_id="t-funnel-record-clean", user_id="admin-1", role="admin")
    handle = await run_task(
        TaskCreate(intent="离线分析一次"), ctx, bus=_RecordingBus()
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None

    client = _runs_client(record.tenant_id, record)
    response = client.get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    assert response.json()["verdict_funnel_dropped_errors"] == []
