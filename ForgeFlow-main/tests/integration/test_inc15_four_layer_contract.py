"""INC15 ④ — the four-layer data contract, end to end at the REST seam.

The contract
------------
INC15 splits the runtime's data into four honest layers and makes the REST
surface expose each one distinctly:

======  ==================  ====================================================
Layer   name                what it is / where it lives
======  ==================  ====================================================
  L1    任务计划 (Plan)       ``GET /runs/{id}.plan`` — the per-task steps that
                            applied, the candidates that did not (with a reason),
                            and the plan/execution counts.
  L2    执行记录 (Execution)  ``GET /runs/{id}.tool_invocations`` — the single
                            source of truth: one record per step that has a
                            terminal state, with an honest ``status`` /
                            ``executed`` / ``invoked`` / ``latency_ms``.
  L3    观测 (Observation)    ``GET /runs/{id}.observations`` — **only** the
                            ``executed is True`` projection of L2 (never padded).
  L4    报告 (Report)         ``GET /runs/{id}.artifacts[0].content`` — the
                            rendered Markdown that reads L1 + L2.
======  ==================  ====================================================

These tests drive a **real** offline run (real planner → real RBAC/Policy gates →
real ``ToolExecutor`` → real handlers) and read every layer back through the
**real** ``GET /runs/{id}`` route — nothing here asserts on a value the test
invented.

The storage tier is declared via ``force_memory_backend`` (the run store is
in-process).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)

pytestmark = pytest.mark.asyncio

TENANT = "t-inc15"
INTENT = "为 Acme 整理一份销售线索分析摘要"


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


@pytest.fixture(autouse=True)
def _clean_store():
    reset_run_store()
    yield
    reset_run_store()


def _ctx() -> RequestContext:
    return RequestContext(tenant_id=TENANT, user_id="u-inc15", role="admin")


def _detail(handle, tenant: str = TENANT) -> dict:
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.include_router(runs_router.router, prefix="/runs")
    response = TestClient(app).get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def _run(context: dict | None = None) -> dict:
    handle = await run_task(
        TaskCreate(intent=INTENT, workflow_type="generic", context=context or {}),
        _ctx(),
        bus=_RecordingBus(),
    )
    return _detail(handle)


# --------------------------------------------------------------------------- #
# 1. All four layers are present and mutually consistent                        #
# --------------------------------------------------------------------------- #
async def test_four_layers_present_and_consistent(force_memory_backend):
    body = await _run()

    # L2 — the execution records (single source of truth).
    invocations = body["tool_invocations"]
    assert invocations, "a real run recorded no invocation"
    for inv in invocations:
        assert inv["executed"] is (inv["status"] in {"ok", "error"})
        assert isinstance(inv["invoked"], bool)
        if inv["executed"]:
            assert inv["invoked"] is True
            assert inv["latency_ms"] is not None and inv["latency_ms"] > 0
        else:
            assert inv["invoked"] is (inv["status"] == "blocked")
            assert inv["latency_ms"] is None

    # L1 — the Task Plan.
    plan = body["plan"]
    assert plan, "the run exposed no task plan"
    assert plan["steps"], "the plan has no steps"
    assert plan["steps"][-1]["tool"] == "report.render", "产物步必须收尾"
    assert {n["tool"] for n in plan["not_applicable"]} == {"data.query", "code.run"}

    # L3 — observations == the executed subset of L2 (never padded).
    observations = body["observations"]
    assert observations == [r for r in invocations if r["executed"] is True]
    assert all(o["executed"] is True for o in observations)

    # L4 — the report renders the layers.
    artifacts = body["artifacts"]
    assert artifacts, "a real run produced no artifact"
    content = artifacts[0]["content"]
    assert "## 一、执行记录" in content
    assert "## 二、任务计划" in content
    assert "## 三、未适用" in content


async def test_observations_are_never_padded_with_fake_rows(force_memory_backend):
    """An unexecuted (blocked) step must never appear as an observation."""
    body = await _run(context={"declared_tools": ["data.query"]})
    observations = body["observations"]
    executed = [r for r in body["tool_invocations"] if r["executed"] is True]
    assert len(observations) == len(executed)
    assert {o["tool"] for o in observations} == {r["tool"] for r in executed}
    # The blocked data.query is on the record but NOT an observation.
    assert len(observations) < len(body["tool_invocations"])
    assert all(o["tool"] != "data.query" for o in observations)


# --------------------------------------------------------------------------- #
# 2. The plan is a function of the task — a table makes data.query apply        #
# --------------------------------------------------------------------------- #
async def test_plan_is_dynamic_with_a_table_input(force_memory_backend):
    body = await _run(context={"table": "warehouse.orders"})
    tools = [s["tool"] for s in body["plan"]["steps"]]
    assert tools == ["research.search", "data.query", "report.render"]
    assert {n["tool"] for n in body["plan"]["not_applicable"]} == {"code.run"}
    # data.query really ran (the platform's dev-stub data tool).
    assert any(
        inv["tool"] == "data.query" and inv["executed"] is True
        for inv in body["tool_invocations"]
    )


async def test_plain_task_never_force_runs_data_or_code(force_memory_backend):
    body = await _run()
    called = {inv["tool"] for inv in body["tool_invocations"]}
    assert "data.query" not in called, "无关步骤被强行执行"
    assert "code.run" not in called, "无关步骤被强行执行"


# --------------------------------------------------------------------------- #
# 3. A declared-but-under-specified step is blocked, not faked as ok            #
# --------------------------------------------------------------------------- #
async def test_declared_step_with_missing_input_is_blocked(force_memory_backend):
    body = await _run(context={"declared_tools": ["data.query"]})

    blocked = {
        s["tool"]: s for s in body["plan"]["steps"] if s["applicability"] == "blocked"
    }
    assert "data.query" in blocked
    assert blocked["data.query"]["blocked_reason"]

    rec = next(inv for inv in body["tool_invocations"] if inv["tool"] == "data.query")
    assert rec["status"] == "blocked"
    assert rec["executed"] is False
    assert rec["invoked"] is False
    assert rec["latency_ms"] is None

    # A blocked step is not an observation, and it is not a failure.
    assert all(o["tool"] != "data.query" for o in body["observations"])
    assert not any("data.query" in e for e in body["errors"])
    assert body["status"] == "completed"


# --------------------------------------------------------------------------- #
# 4. The report distinguishes executed / not-applicable / blocked / failed       #
# --------------------------------------------------------------------------- #
async def test_report_body_distinguishes_the_layers(force_memory_backend):
    body = await _run(context={"declared_tools": ["data.query"]})
    content = body["artifacts"][0]["content"]
    assert "## 四、受阻" in content
    # The blocked step is listed under 受阻; the trimmed ones under 未适用.
    blocked_block = content.split("## 四、受阻")[1].split("## 五、")[0]
    assert "data.query" in blocked_block
    na_block = content.split("## 三、未适用")[1].split("## 四、")[0]
    assert "code.run" in na_block
