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
        result = await delegate_parallel(
            {"delegations": [_brief("research"), _brief("code_review")]}, _ctx()
        )
    finally:
        current_budget.reset(token)
    assert result["ok"] is True and result["ok_count"] == 2 and result["failed_count"] == 0
    assert [r["role"] for r in result["results"]] == ["research", "code_review"]
    # Both starts happened before either finished: gather, not a loop.
    assert seen.index("code_review") < seen.index("research:done")


@pytest.mark.asyncio
async def test_parallel_delegation_reports_invalid_brief_without_blocking_valid(monkeypatch):
    # 2026-10-30: the old whole-batch raise let the model retry the
    # identical call until the run hit its model-call ceiling. Now an
    # invalid brief is that item's failure; valid siblings still run.
    started = []

    async def fake_delegate(item, ctx):
        started.append(item["role"])
        return {
            "ok": True,
            "delegation_run_id": "run-r",
            "status": "completed",
            "summary": "ok",
            "summary_truncated": False,
            "artifacts": [],
            "total_tokens": 1,
            "error": None,
        }

    monkeypatch.setattr(delegation_mod, "delegate", fake_delegate)
    token = current_budget.set(ExecutionBudget())
    try:
        result = await delegate_parallel(
            {"delegations": [_brief("research"), _brief("unknown_role")]}, _ctx()
        )
        result2 = await delegate_parallel(
            {
                "delegations": [
                    _brief("research"),
                    _brief("code_review", "x") | {"resources": ["not-parent"]},
                ]
            },
            _ctx(),
        )
    finally:
        current_budget.reset(token)
    assert result["ok"] is False and result["ok_count"] == 1 and result["failed_count"] == 1
    assert (
        result["results"][1]["ok"] is False
        and "Unknown specialist role" in result["results"][1]["error"]
    )
    assert result2["results"][1]["ok"] is False
    assert started == ["research", "research"]


@pytest.mark.asyncio
async def test_parallel_delegation_isolates_one_child_failure(monkeypatch):
    async def fake_delegate(item, ctx):
        if item["role"] == "code_review":
            raise RuntimeError("child exploded")
        return {
            "ok": True,
            "delegation_run_id": "run-r",
            "status": "completed",
            "summary": "ok",
            "summary_truncated": False,
            "artifacts": [],
            "total_tokens": 1,
            "error": None,
        }

    monkeypatch.setattr(delegation_mod, "delegate", fake_delegate)
    token = current_budget.set(ExecutionBudget())
    try:
        result = await delegate_parallel(
            {"delegations": [_brief("research"), _brief("code_review")]}, _ctx()
        )
    finally:
        current_budget.reset(token)
    assert result["ok"] is False and result["ok_count"] == 1 and result["failed_count"] == 1
    assert result["results"][1]["ok"] is False and "child exploded" in result["results"][1]["error"]


@pytest.mark.asyncio
async def test_parallel_delegation_single_brief_and_over_cap(monkeypatch):
    # 2026-10-30 handoff retest: a single brief in list form raised
    # "Parallel delegation requires 2-4 delegation briefs" and the model
    # looped on it until budget_exceeded. A single brief now runs, and
    # briefs beyond the cap are skipped items, not a whole-batch error.
    started = []

    async def fake_delegate(item, ctx):
        started.append(item["role"])
        return {
            "ok": True,
            "delegation_run_id": "run-r",
            "status": "completed",
            "summary": "ok",
            "summary_truncated": False,
            "artifacts": [],
            "total_tokens": 1,
            "error": None,
        }

    monkeypatch.setattr(delegation_mod, "delegate", fake_delegate)
    token = current_budget.set(ExecutionBudget())
    try:
        single = await delegate_parallel({"delegations": [_brief("research")]}, _ctx())
        over = await delegate_parallel({"delegations": [_brief("research")] * 5}, _ctx())
        with pytest.raises(ValueError):
            await delegate_parallel({"delegations": []}, _ctx())
        budget = current_budget.get()
        budget.delegations = 3  # only 1 slot left
        partial = await delegate_parallel(
            {"delegations": [_brief("research"), _brief("code_review")]}, _ctx()
        )
    finally:
        current_budget.reset(token)
    assert single["ok"] is True and single["ok_count"] == 1
    assert over["ok"] is False and len(over["results"]) == 5
    assert over["results"][4]["ok"] is False and "Skipped" in over["results"][4]["error"]
    assert partial["ok"] is False and partial["ok_count"] == 1
    assert "Skipped" in partial["results"][1]["error"]


@pytest.mark.asyncio
async def test_delegate_dispatches_to_parallel_when_delegations_present(monkeypatch):
    async def fake_parallel(args, ctx):
        return {"ok": True, "ok_count": 2, "failed_count": 0, "results": []}

    monkeypatch.setattr(delegation_mod, "delegate_parallel", fake_parallel)
    result = await delegate({"delegations": [_brief("research"), _brief("code_review")]}, _ctx())
    assert result["ok"] is True and result["ok_count"] == 2
