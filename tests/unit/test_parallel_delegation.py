"""Parallel delegation (fan-out / fan-in) for the multi-agent runtime.

Before this, the supervisor graph and ``agent.delegate`` were both
sequential: a multi-domain review paid the sum of every specialist's
latency. CrewAI's parallel process and LangGraph's fan-out pattern are
the open-source reference — independent specialists run concurrently
and are merged afterwards.

These tests stub :func:`delegate` itself, so they prove the *orchestration*
(concurrency, upfront validation, failure isolation, budget ceiling)
without needing a model. The real child path is unchanged and is covered
by the existing delegation tests.
"""

import asyncio
from types import SimpleNamespace

import pytest

from forgeflow.runtime import delegation as delegation_mod
from forgeflow.runtime.delegation import delegate, delegate_parallel
from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget


def _ctx():
    parent_task = SimpleNamespace(
        context={
            "resources": ["res-1", "res-2"],
            "llm_runtime": {"acceptance": {"requirements": [{"id": "req-1"}]}},
        }
    )
    parent = SimpleNamespace(delegation_role=None, allowed_tools=None)
    return SimpleNamespace(runtime_context=parent, task_context=parent_task)


def _brief(role, instruction="审阅"):
    return {
        "role": role,
        "instruction": instruction,
        "resources": ["res-1"],
        "requirement_ids": ["req-1"],
    }


@pytest.mark.asyncio
async def test_parallel_delegations_run_concurrently(monkeypatch):
    seen = []

    async def fake_delegate(item, ctx):
        seen.append(item["role"])
        await asyncio.sleep(0)  # yield: a sequential loop would not interleave
        seen.append(item["role"] + ":done")
        return {
            "ok": True,
            "delegation_run_id": f"run-{item['role']}",
            "status": "completed",
            "summary": "ok",
            "summary_truncated": False,
            "artifacts": [],
            "total_tokens": 10,
            "error": None,
        }

    monkeypatch.setattr(delegation_mod, "delegate", fake_delegate)
    token = current_budget.set(ExecutionBudget())
    try:
        result = await delegate_parallel({"delegations": [_brief("research"), _brief("code_review")]}, _ctx())
    finally:
        current_budget.reset(token)
    assert result["ok"] is True and result["ok_count"] == 2 and result["failed_count"] == 0
    assert [r["role"] for r in result["results"]] == ["research", "code_review"]
    # Both starts happened before either finished: gather, not a loop.
    assert seen.index("code_review") < seen.index("research:done")


@pytest.mark.asyncio
async def test_parallel_delegation_validates_before_any_child_starts(monkeypatch):
    started = []

    async def fake_delegate(item, ctx):
        started.append(item["role"])
        return {"ok": True}

    monkeypatch.setattr(delegation_mod, "delegate", fake_delegate)
    token = current_budget.set(ExecutionBudget())
    try:
        with pytest.raises(ValueError):
            await delegate_parallel(
                {"delegations": [_brief("research"), _brief("unknown_role")]}, _ctx()
            )
        with pytest.raises(PermissionError):
            await delegate_parallel(
                {"delegations": [_brief("research"), _brief("code_review", "x") | {"resources": ["not-parent"]}]},
                _ctx(),
            )
    finally:
        current_budget.reset(token)
    assert started == []


@pytest.mark.asyncio
async def test_parallel_delegation_isolates_one_child_failure(monkeypatch):
    async def fake_delegate(item, ctx):
        if item["role"] == "code_review":
            raise RuntimeError("child exploded")
        return {
            "ok": True, "delegation_run_id": "run-r", "status": "completed",
            "summary": "ok", "summary_truncated": False, "artifacts": [],
            "total_tokens": 1, "error": None,
        }

    monkeypatch.setattr(delegation_mod, "delegate", fake_delegate)
    token = current_budget.set(ExecutionBudget())
    try:
        result = await delegate_parallel({"delegations": [_brief("research"), _brief("code_review")]}, _ctx())
    finally:
        current_budget.reset(token)
    assert result["ok"] is False and result["ok_count"] == 1 and result["failed_count"] == 1
    assert result["results"][1]["ok"] is False and "child exploded" in result["results"][1]["error"]


@pytest.mark.asyncio
async def test_parallel_delegation_rejects_too_many_and_budget_overflow(monkeypatch):
    async def fake_delegate(item, ctx):
        return {"ok": True}

    monkeypatch.setattr(delegation_mod, "delegate", fake_delegate)
    token = current_budget.set(ExecutionBudget())
    try:
        with pytest.raises(ValueError):
            await delegate_parallel({"delegations": [_brief("research")]}, _ctx())
        with pytest.raises(ValueError):
            await delegate_parallel(
                {"delegations": [_brief("research")] * 5}, _ctx()
            )
        budget = current_budget.get()
        budget.delegations = 3  # only 1 slot left, batch needs 2
        with pytest.raises(PermissionError):
            await delegate_parallel({"delegations": [_brief("research"), _brief("code_review")]}, _ctx())
    finally:
        current_budget.reset(token)


@pytest.mark.asyncio
async def test_delegate_dispatches_to_parallel_when_delegations_present(monkeypatch):
    async def fake_parallel(args, ctx):
        return {"ok": True, "ok_count": 2, "failed_count": 0, "results": []}

    monkeypatch.setattr(delegation_mod, "delegate_parallel", fake_parallel)
    result = await delegate({"delegations": [_brief("research"), _brief("code_review")]}, _ctx())
    assert result["ok"] is True and result["ok_count"] == 2
