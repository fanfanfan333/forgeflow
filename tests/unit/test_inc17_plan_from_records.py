"""INC17 T02 — ``planning.plan_from_records``: the strict 1:1 L1 projection.

The ReAct loop lets the model call the same tool more than once, so the L1 Task
Plan can no longer be derived from :func:`build_plan` — that function
**de-duplicates** its candidates (``_candidate_tools``) and forces
``report.render`` to the tail, which collapses a repeated tool and breaks the
``len(plan.steps) == len(tool_invocations)`` contract.

:func:`plan_from_records` is the new *pure* projection: one record ⇒ one step,
order and duplicates preserved, ``not_applicable`` always empty. This file pins
that contract and — as a **non-vacuity control** — proves ``build_plan`` really
does de-duplicate, i.e. the new function solves a real defect instead of merely
skirting an assertion.
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.planning import (
    BLOCKED_STATUS,
    REPORT_TOOL,
    CapabilityContext,
    build_plan,
    plan_from_records,
)
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor


@pytest.fixture(autouse=True)
def _clean_registry():
    """Load the default tool bindings so the cross-layer cases drive the REAL
    ``ToolExecutor`` (INC23 F1) rather than a hand-built record."""
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


def _tool_ctx(run_id: str, step_id: str, *, intent: str = "分析文本", args: dict | None = None):
    return ToolCallContext(
        run_id=run_id,
        step_id=step_id,
        tenant_id="t-23",
        user_id="u-23",
        role="admin",
        intent=intent,
        attempt=0,
        args=args or {},
    )


def _record(tool: str, *, index: int = 0, status: str = "ok", executed: bool = True) -> dict:
    return {
        "tool": tool,
        "step_id": f"r-1:0:{index}",
        "status": status,
        "executed": executed,
    }


def _ctx() -> CapabilityContext:
    return CapabilityContext(intent="检索并生成报告", workflow_type="generic")


# --------------------------------------------------------------------------- #
# 1. Strict 1:1 — one step per record                                            #
# --------------------------------------------------------------------------- #
def test_one_step_per_record(force_memory_backend):
    records = [_record("research.search", index=0), _record(REPORT_TOOL, index=1)]
    plan = plan_from_records(records, run_id="r-1", attempt=0)
    assert len(plan.steps) == len(records)
    assert [s.tool for s in plan.steps] == ["research.search", REPORT_TOOL]


# --------------------------------------------------------------------------- #
# 2. Order preserved AND duplicates preserved (the whole point)                 #
# --------------------------------------------------------------------------- #
def test_duplicate_tools_are_preserved_in_order(force_memory_backend):
    records = [
        _record("research.search", index=0),
        _record("research.search", index=1),
        _record(REPORT_TOOL, index=2),
    ]
    plan = plan_from_records(records, run_id="r-1", attempt=0)
    assert [s.tool for s in plan.steps] == ["research.search", "research.search", REPORT_TOOL]
    assert len(plan.steps) == 3
    # indices mirror the record positions (0,1,2).
    assert [s.index for s in plan.steps] == [0, 1, 2]


# --------------------------------------------------------------------------- #
# 3. not_applicable is always [] (a recorded step really happened)              #
# --------------------------------------------------------------------------- #
def test_not_applicable_is_always_empty(force_memory_backend):
    records = [_record("data.query", index=0, status="blocked", executed=False)]
    plan = plan_from_records(records, run_id="r-1")
    assert plan.not_applicable == []


# --------------------------------------------------------------------------- #
# 4. step_id: record value preferred, else the f"{run_id}:{attempt}:{index}"     #
# --------------------------------------------------------------------------- #
def test_step_id_prefers_the_record_value(force_memory_backend):
    records = [{"tool": "research.search", "step_id": "custom:9:7", "status": "ok"}]
    plan = plan_from_records(records, run_id="r-X", attempt=3)
    assert plan.steps[0].step_id == "custom:9:7"


def test_step_id_falls_back_to_the_formula(force_memory_backend):
    records = [{"tool": "research.search", "status": "ok"}]  # no step_id
    plan = plan_from_records(records, run_id="r-X", attempt=3)
    assert plan.steps[0].step_id == "r-X:3:0"


# --------------------------------------------------------------------------- #
# 5. Empty / None input ⇒ an empty plan                                          #
# --------------------------------------------------------------------------- #
def test_empty_and_none_records_yield_no_steps(force_memory_backend):
    assert plan_from_records([], run_id="r-1").steps == []
    assert plan_from_records(None, run_id="r-1").steps == []


def test_malformed_records_are_ignored_not_counted(force_memory_backend):
    records = [None, "nope", 42, _record("research.search", index=0)]
    plan = plan_from_records(records, run_id="r-1")
    assert [s.tool for s in plan.steps] == ["research.search"]


# --------------------------------------------------------------------------- #
# 6. Non-vacuity control: build_plan REALLY de-duplicates (so plan_from_records
#    is not solving a non-problem)                                              #
# --------------------------------------------------------------------------- #
def test_build_plan_deduplicates_so_plan_from_records_is_necessary(force_memory_backend):
    candidates = [
        {"tool": "research.search", "note": "a"},
        {"tool": "research.search", "note": "b"},  # duplicate
        {"tool": REPORT_TOOL},
    ]
    # build_plan collapses the duplicate ⇒ its step count can never equal a real
    # 3-call trail. That is exactly the defect plan_from_records solves.
    built = build_plan(candidates, _ctx(), run_id="r-1", attempt=0)
    assert len(built.steps) == 2
    assert [s.tool for s in built.steps] == ["research.search", REPORT_TOOL]

    # The equivalent record trail keeps both calls.
    records = [
        _record("research.search", index=0),
        _record("research.search", index=1),
        _record(REPORT_TOOL, index=2),
    ]
    projected = plan_from_records(records, run_id="r-1", attempt=0)
    assert len(projected.steps) == 3 == len(records)


# --------------------------------------------------------------------------- #
# 7. INC23 F1 — L1 ``applicability`` mirrors the L2 record's status             #
#    (the cross-layer case: records produced by the REAL ``ToolExecutor``)      #
# --------------------------------------------------------------------------- #
async def test_blocked_record_projects_to_blocked_with_the_real_reason(force_memory_backend):
    """INC23 F1 — a ``blocked`` L2 record ⇒ ``applicability == "blocked"``.

    Before INC23 ``plan_from_records`` stamped ``"required"`` on **every** step, so
    one run printed 「适用性：required」 next to 「受阻（BLOCKED）」. The record here is
    produced by the **real** ``ToolExecutor`` (never a hand-built dict), so the
    projection is measured against production code.
    """
    reason = "缺少必需输入：表名(table)"
    invocation = await ToolExecutor().execute(
        "data.query", ctx=_tool_ctx("r-23", "r-23:0:0"), blocked_reason=reason
    )
    record = invocation.to_dict()
    # Guard the fixture's reality: the executor really recorded a blocked step.
    assert record["status"] == BLOCKED_STATUS
    assert record["executed"] is False

    plan = plan_from_records([record], run_id="r-23", attempt=0)
    step = plan.steps[0]

    # L1 no longer contradicts L2: status and applicability now agree.
    assert step.applicability == "blocked"
    assert step.blocked_reason == reason  # verbatim, from payload["reason"]
    # L1 (both views) and L2 now agree on the reason.
    assert step.to_dict()["blocked_reason"] == reason
    assert step.to_payload(0, status=record["status"])["blocked_reason"] == reason


async def test_non_blocked_record_stays_required_with_no_reason(force_memory_backend):
    """Discriminating half: an ``ok`` record stays ``required`` with ``""`` reason."""
    invocation = await ToolExecutor().execute(
        "docs.parse", ctx=_tool_ctx("r-23b", "r-23b:0:0", args={"text": "hello"})
    )
    record = invocation.to_dict()
    assert record["status"] == "ok"

    step = plan_from_records([record], run_id="r-23b").steps[0]
    assert step.applicability == "required"
    assert step.blocked_reason == ""
    assert step.to_payload(0, status=record["status"])["blocked_reason"] == ""


async def test_blocked_reason_falls_back_to_error_when_payload_reason_absent(force_memory_backend):
    """The documented fallback: with ``payload["reason"]`` gone, use ``error`` verbatim."""
    invocation = await ToolExecutor().execute(
        "code.run",
        ctx=_tool_ctx("r-23c", "r-23c:0:0"),
        blocked_reason="缺少必需输入：代码路径(paths)",
    )
    record = invocation.to_dict()
    assert record["status"] == BLOCKED_STATUS
    assert record["error"]
    record["payload"] = {"ok": False, "blocked": True}  # the payload loses its reason

    step = plan_from_records([record], run_id="r-23c").steps[0]
    assert step.applicability == "blocked"
    assert step.blocked_reason == record["error"]  # verbatim fallback to error
