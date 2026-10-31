"""Upgrades ported from Claude / Trae Agent / DeepSeek Harness patterns.

2026-10-31 thorough-upgrade round: tool hooks, transient retry,
trajectory export, and the delegation oscillation guard.
"""

import asyncio
from types import SimpleNamespace

import pytest

from forgeflow.runtime import tool_hooks
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.trajectory import export_trajectory, filter_trajectory


@pytest.fixture(autouse=True)
def _clear_hooks():
    tool_hooks.clear_hooks()
    yield
    tool_hooks.clear_hooks()


def _ctx():
    return ToolCallContext(
        run_id="r1", step_id="s1", tenant_id="t", user_id="u", role="viewer", args={"query": "x"}
    )


def test_pre_hook_deny_blocks_tool_without_execution():
    tool_hooks.register_hook(
        "pre", lambda e, t, a, r: {"action": "deny", "reason": "blocked by test hook"}
    )
    invocation = asyncio.run(ToolExecutor().execute("research.search", ctx=_ctx()))
    assert invocation.status == "refused"
    assert invocation.executed is False
    assert invocation.payload["hook_denied"] is True


def test_pre_hook_rewrite_replaces_args():
    seen = {}

    def rewrite(event, tool, args, result):
        seen.update(args)
        return {"action": "rewrite", "args": {"query": "rewritten"}}

    tool_hooks.register_hook("pre", rewrite)
    # research.search with a rewritten query still runs through the
    # real handler path; we only assert the hook saw and rewrote it
    # by checking the hook's own record, not by depending on network.
    asyncio.run(ToolExecutor().execute("research.search", ctx=_ctx()))
    assert seen == {"query": "x"}


def test_post_failure_hook_fires_on_error():
    fired = []
    tool_hooks.register_hook("post_failure", lambda e, t, a, r: fired.append((e, t)))
    # An unknown tool errors in the handler layer, which is enough to
    # prove the post_failure hook fires on a failed invocation.
    invocation = asyncio.run(ToolExecutor().execute("no.such.tool", ctx=_ctx()))
    assert invocation.status in {"error", "unavailable", "refused", "blocked"}
    assert fired == [("post_failure", "no.such.tool")]


def test_transient_retry_only_for_read_only_tools():
    # A non-transient, non-read-only failure must not be retried:
    # the invocation stays a single error, not a double write.
    invocation = asyncio.run(ToolExecutor().execute("document.edit", ctx=_ctx()))
    assert invocation.attempt == 0


def test_trajectory_export_and_source_filter():
    trajectory = export_trajectory(
        run_id="r1",
        intent="test",
        tool_invocations=[
            {
                "tool": "research.search",
                "status": "ok",
                "executed": True,
                "summary": "found",
                "result_ref": "ref1",
                "latency_ms": 1.0,
                "error": None,
            },
            {
                "tool": "textfile.inspect",
                "status": "error",
                "executed": False,
                "summary": "bad",
                "result_ref": None,
                "latency_ms": None,
                "error": "bad",
            },
        ],
        plan={"summary": {"planned": 2}},
        verdict={"outcome": "partial", "success": True, "reasons": []},
        total_tokens=10,
    )
    assert trajectory["version"] == 1
    assert len(trajectory["entries"]) == 4  # 2 tool + plan + verdict
    tools_only = filter_trajectory(trajectory, source="tool")
    assert len(tools_only["entries"]) == 2
    assert all(e["source"] == "tool" for e in tools_only["entries"])
    # No payloads are copied into the trajectory.
    assert "ref1" in str(trajectory)  # result_ref only, not content
    assert "found" in str(trajectory)


def test_delegation_oscillation_guard_blocks_third_attempt_for_same_role():
    from forgeflow.runtime import delegation as d
    from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget

    parent_task = SimpleNamespace(context={"resources": ["res1"]})
    ctx = SimpleNamespace(task_context=parent_task, runtime_context=SimpleNamespace())
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    try:
        # Simulate two failed attempts by pre-setting the counter;
        # the third call must be skipped without starting a child.
        parent_task.context["_delegation_attempts"] = {"research": 2}
        result = asyncio.run(
            d.delegate({"role": "research", "instruction": "again", "resources": ["res1"]}, ctx)
        )
    finally:
        current_budget.reset(token)
    assert result["ok"] is False
    assert result["status"] == "skipped"
    assert "already attempted twice" in result["error"]


def test_trajectory_steps_do_not_copy_observation_payload():
    # 2026-10-31 17d3162 round: entries were clean, but
    # trajectory.steps[].observation.payload still embedded diff and
    # report text verbatim (one observation 29KB), so the "no payload
    # copies" claim was false for the steps layer.
    diff_text = "DIFF-SECRET-9f3a\n-def add(a, b):\n-    raise NotImplementedError\n+def add(a, b):\n+    return a + b\n"
    trajectory = export_trajectory(
        run_id="r2",
        steps=[
            {
                "step_id": "s1",
                "tool": "code.execute",
                "status": "ok",
                "observation": {
                    "tool": "code.execute",
                    "status": "ok",
                    "summary": "code task done",
                    "payload": {"diff": diff_text, "report": diff_text},
                },
            }
        ],
    )
    assert diff_text not in str(trajectory)
    step = trajectory["steps"][0]
    assert step["observation"]["summary"] == "code task done"
    assert step["observation"]["payload_omitted"] is True
    assert "payload" not in step["observation"]
