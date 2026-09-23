"""INC2-23 — the ``{task:{intent}}`` event envelope drives a hub Run (docs §2.14).

The new branch must dispatch to ``runtime.orchestrator.run_task`` while the three
legacy workflow branches stay byte-for-byte intact (``test_events.py`` guards the
latter in depth; here we pin the new path + a light regression).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from forgeflow.events import dispatcher as disp_mod
from forgeflow.events.dispatcher import EventDispatcher, WorkflowTrigger


# --------------------------------------------------------------------------- #
# parse_event — new envelope                                                   #
# --------------------------------------------------------------------------- #

def test_parse_task_envelope():
    trigger = EventDispatcher.parse_event(
        {
            "task": {"intent": "分析华东销售数据", "context": {"k": "v"}},
            "user_id": "alice",
            "role": "manager",
            "tenant_id": "acme",
        },
        source_event_id="evt-1",
    )
    assert trigger.workflow_type == "agentflow_task"
    assert trigger.payload["intent"] == "分析华东销售数据"
    assert trigger.user_id == "alice"
    assert trigger.role == "manager"
    assert trigger.source_event_id == "evt-1"
    assert trigger.extra["tenant_id"] == "acme"


def test_parse_explicit_agentflow_task_workflow_type():
    trigger = EventDispatcher.parse_event(
        {"workflow_type": "agentflow_task", "task": {"intent": "生成周报"}}
    )
    assert trigger.workflow_type == "agentflow_task"
    assert trigger.payload["intent"] == "生成周报"


def test_task_envelope_without_intent_is_rejected():
    with pytest.raises(ValueError, match="missing \\{task: \\{intent\\}\\}"):
        EventDispatcher.parse_event({"task": {"title": "no intent here"}})


def test_legacy_and_task_envelopes_coexist():
    legacy = EventDispatcher.parse_event(
        {"workflow_type": "sales_ops", "lead_data": {"company_name": "Acme"}}
    )
    assert legacy.workflow_type == "sales_ops"          # unchanged

    with pytest.raises(ValueError, match="unknown workflow_type"):
        EventDispatcher.parse_event({"workflow_type": "marketing_ops", "lead_data": {}})


# --------------------------------------------------------------------------- #
# dispatch — new branch routes to run_task                                     #
# --------------------------------------------------------------------------- #

async def test_dispatch_task_envelope_calls_run_task(monkeypatch):
    import forgeflow.runtime.orchestrator as orch

    calls: list[tuple] = []

    async def _fake_run_task(task, ctx, **kwargs):
        calls.append((task, ctx))
        return SimpleNamespace(run_id="run-42", thread_id="thread-42")

    monkeypatch.setattr(orch, "run_task", _fake_run_task)

    dispatcher = EventDispatcher(graphs={})   # no graph needed for a task event
    trigger = WorkflowTrigger(
        workflow_type="agentflow_task",
        payload={"intent": "分析数据", "context": {"agent_id": "a1"}},
        user_id="eve",
        role="manager",
        extra={"tenant_id": "acme"},
    )

    result = await dispatcher.dispatch(trigger)

    assert result.ok is True
    assert result.workflow_id == "run-42"
    assert result.thread_id == "thread-42"

    task, ctx = calls[0]
    assert task.intent == "分析数据"
    assert ctx.user_id == "eve"
    assert ctx.role == "manager"
    assert ctx.tenant_id == "acme"


async def test_dispatch_task_failure_becomes_error_result(monkeypatch):
    import forgeflow.runtime.orchestrator as orch

    async def _boom(task, ctx, **kwargs):
        raise RuntimeError("runtime down")

    monkeypatch.setattr(orch, "run_task", _boom)

    dispatcher = EventDispatcher(graphs={})
    result = await dispatcher.dispatch(
        WorkflowTrigger(workflow_type="agentflow_task", payload={"intent": "x"})
    )

    assert result.ok is False
    assert "runtime down" in result.error


# --------------------------------------------------------------------------- #
# regression — the legacy dispatch branch is untouched                         #
# --------------------------------------------------------------------------- #

async def test_legacy_sales_ops_dispatch_still_runs_the_pipeline(monkeypatch):
    fake_pipeline = MagicMock()
    fake_pipeline.run = AsyncMock(return_value=("wf-legacy", "th-legacy", {}))

    def fake_select(workflow_type, graph, payload):
        from forgeflow.workflows.sales_ops.models import LeadInput

        return fake_pipeline, LeadInput(**payload)

    monkeypatch.setattr(disp_mod.EventDispatcher, "_select_pipeline", staticmethod(fake_select))

    dispatcher = EventDispatcher(graphs={"sales_ops": MagicMock()})
    result = await dispatcher.dispatch(
        WorkflowTrigger(workflow_type="sales_ops", payload={"company_name": "Acme"})
    )

    assert result.ok is True
    assert result.workflow_id == "wf-legacy"
    fake_pipeline.run.assert_awaited_once()
