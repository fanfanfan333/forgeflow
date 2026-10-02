"""INC-41 — ``analysis.profile`` contract + react/deterministic symmetry.

Pins three defects from the round-4 acceptance report:

* **F-125** — the react profile did not resolve/inject ``column`` at all, so a
  data-file analysis succeeded only when the *model* happened to supply the
  column (behaviour asymmetry vs. the deterministic ``_analysis_args``).
* **F-126** — an under-specified step reported ``required_inputs == []`` so the UI
  could not tell the user *what* was missing.
* **F-124** — ``analysis.profile`` had **no** ``_TOOL_DESCRIPTIONS`` entry, so the
  model could see the tool but not what it was for.

Citation discipline: ``file.py::symbol`` anchors, never line numbers.
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import planning
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    _build_task_plan,
    _candidates_for,
    _declared_plan_tools,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.react_executor import ReactExecutor, _TOOL_DESCRIPTIONS

pytestmark = pytest.mark.asyncio

_CTX = RequestContext(tenant_id="t-inc41", user_id="u-1", role="admin")


# --------------------------------------------------------------------------- #
# F-125 — the contract declares BOTH ``paths`` and ``column``                    #
# --------------------------------------------------------------------------- #
async def test_contract_declares_paths_then_column():
    assert planning.TOOL_INPUT_CONTRACT["analysis.profile"]["required"] == (
        "paths",
        "column",
    )


async def test_resolve_inputs_reports_missing_in_paths_column_order():
    both = planning.CapabilityContext(
        intent="x", explicit_inputs={"paths": ["a.csv"], "column": "amount"}
    )
    args, missing = planning.resolve_inputs("analysis.profile", both)
    assert missing == []
    assert args["paths"] == ["a.csv"]
    assert args["column"] == "amount"

    none = planning.CapabilityContext(intent="x", explicit_inputs={})
    _args, missing = planning.resolve_inputs("analysis.profile", none)
    assert missing == ["paths", "column"], "缺键顺序必须是 paths 在前、column 在后"

    paths_only = planning.CapabilityContext(
        intent="x", explicit_inputs={"paths": ["a.csv"]}
    )
    args, missing = planning.resolve_inputs("analysis.profile", paths_only)
    assert missing == ["column"]
    assert "column" not in args


# --------------------------------------------------------------------------- #
# F-124 — the tool is described to the model                                     #
# --------------------------------------------------------------------------- #
async def test_analysis_profile_is_described_to_the_model():
    assert "analysis.profile" in _TOOL_DESCRIPTIONS
    assert _TOOL_DESCRIPTIONS["analysis.profile"].strip()


# --------------------------------------------------------------------------- #
# F-125 — react injects the platform's ``column`` exactly like deterministic     #
# --------------------------------------------------------------------------- #
async def test_react_effective_args_inject_platform_column():
    task = TaskCreate(
        intent="统计 leads.csv 的 amount 合计",
        context={"paths": ["/tmp/leads.csv"], "column": "amount"},
    )
    args, _injected = ReactExecutor._effective_args(
        "analysis.profile", {}, task, _CTX, []
    )
    assert args.get("column") == "amount", "平台声明的 column 未被注入 react 参数"
    assert args.get("paths") == ["/tmp/leads.csv"]


async def test_react_model_column_still_wins_when_platform_absent():
    """``column`` is NOT platform-owned: the model keeps its own choice."""
    task = TaskCreate(intent="统计", context={"paths": ["/tmp/leads.csv"]})
    args, _ = ReactExecutor._effective_args(
        "analysis.profile", {"column": "revenue"}, task, _CTX, []
    )
    assert args.get("column") == "revenue"


async def test_react_model_column_wins_over_platform_when_both_present():
    """A model-supplied column overrides the platform default (same as the model's
    other non-owned choices — the platform only guarantees a value is present)."""
    task = TaskCreate(
        intent="统计", context={"paths": ["/tmp/leads.csv"], "column": "amount"}
    )
    args, _ = ReactExecutor._effective_args(
        "analysis.profile", {"column": "model_col"}, task, _CTX, []
    )
    assert args.get("column") == "model_col"


# --------------------------------------------------------------------------- #
# F-126 — a react blocked step carries its required inputs                       #
# --------------------------------------------------------------------------- #
async def test_plan_from_records_carries_required_inputs():
    record = {
        "step_id": "r:0:0",
        "tool": "analysis.profile",
        "status": "blocked",
        "executed": False,
        "error": "缺少必需输入：聚合列(column)",
        "payload": {
            "ok": False,
            "blocked": True,
            "reason": "缺少必需输入：聚合列(column)",
        },
    }
    step = planning.plan_from_records([record], run_id="r", attempt=0).steps[0]
    assert step.required_inputs == ["paths", "column"], "blocked 步仍不上报 required_inputs"
    assert step.applicability == "blocked"
    assert step.blocked_reason  # non-empty, verbatim from payload["reason"]


# --------------------------------------------------------------------------- #
# F-126 — deterministic: an under-specified analysis step stays ``blocked``      #
# --------------------------------------------------------------------------- #
def _analysis_task(column: str | None) -> TaskCreate:
    context: dict = {
        "declared_tools": ["analysis.profile"],
        "paths": ["/tmp/leads.csv"],
    }
    if column is not None:
        context["column"] = column
    return TaskCreate(intent="统计 leads.csv 合计", context=context)


def _det_plan(task: TaskCreate):
    return _build_task_plan(
        _candidates_for(task, _CTX),
        task,
        _CTX,
        run_id="r",
        attempt=0,
        source="deterministic",
        records=[],
        declared_tools=_declared_plan_tools(task, _CTX),
    )


async def test_deterministic_analysis_required_when_column_present():
    step = next(
        s for s in _det_plan(_analysis_task("amount")).steps
        if s.tool == "analysis.profile"
    )
    assert step.applicability == "required"
    assert step.required_inputs == ["paths", "column"]


async def test_deterministic_analysis_blocked_not_trimmed_when_column_absent():
    """The injected step must remain visible as ``blocked`` — never trimmed."""
    plan = _det_plan(_analysis_task(None))
    tools = [s.tool for s in plan.steps]
    assert "analysis.profile" in tools, (
        "under-specified analysis step was silently trimmed instead of kept blocked"
    )
    step = next(s for s in plan.steps if s.tool == "analysis.profile")
    assert step.applicability == "blocked"
    assert step.required_inputs == ["paths", "column"]
    assert step.blocked_reason


async def test_blocked_reason_names_the_aggregation_column():
    """The UI reason must name 聚合列(column), not the raw key."""
    assert planning._MISSING_LABELS["column"] == "聚合列(column)"

    cap = planning.CapabilityContext(
        intent="统计",
        explicit_inputs={"paths": ["/tmp/leads.csv"]},
        declared_tools=["analysis.profile"],
    )
    reason = planning.blocked_reason("analysis.profile", cap)
    assert "column" in reason
    assert "聚合列" in reason


# --------------------------------------------------------------------------- #
# F-134 — the SSE ``run.plan`` first frame and the terminal plan must agree      #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_run_store_inc41():
    reset_run_store()
    yield
    reset_run_store()


async def test_first_frame_and_terminal_plan_agree_on_analysis_profile(
    monkeypatch, force_memory_backend
):
    """INC-41 F-134 — the loop-driving (first-frame) plan must not trim the
    platform-injected ``analysis.profile`` step.

    A declared data file with no ``column`` must read ``blocked`` in the SSE
    ``run.plan`` **first frame** AND in the terminal plan — they must agree (the
    first frame used to omit it because ``declared_tools`` was only passed to the
    final rebuild).
    """
    import forgeflow.runtime.orchestrator as orch

    # Minimal candidate set ⇒ a stdlib-only deterministic run; the injected
    # ``analysis.profile`` is placed before the single report step.
    monkeypatch.setattr(
        orch,
        "_DEFAULT_STEPS",
        [{"tool": "report.render", "step_type": "agent", "note": ""}],
    )
    # A declared data file (dereferenced) ⇒ the platform injects analysis.profile.
    monkeypatch.setattr(
        orch, "_resolve_resource_inputs", lambda context: {"paths": ["/tmp/leads.csv"]}
    )
    monkeypatch.setattr(orch, "resolve_agent_runtime_mode", lambda: "deterministic")

    class _Bus:
        def __init__(self) -> None:
            self.frames: list[dict] = []

        async def emit(self, run_id, event_type, data):  # noqa: ANN001
            if event_type == "run.plan":
                self.frames.append(data)

    task = TaskCreate(intent="分析这份 CSV", workflow_type="generic", context={})
    bus = _Bus()
    await run_task(
        task,
        RequestContext(tenant_id="t-f134", user_id="u-1", role="admin"),
        bus=bus,
    )

    assert bus.frames, "必须发出 run.plan"
    first_tools = [s["tool"] for s in bus.frames[0]["steps"]]
    assert "analysis.profile" in first_tools, (
        "首帧把平台注入的分析步裁掉了（F-134 回归）"
    )

    first = next(s for s in bus.frames[0]["steps"] if s["tool"] == "analysis.profile")
    terminal = next(
        s for s in task.context["plan"]["steps"] if s["tool"] == "analysis.profile"
    )
    assert first["applicability"] == terminal["applicability"] == "blocked"
    assert first["required_inputs"] == terminal["required_inputs"] == ["paths", "column"]
