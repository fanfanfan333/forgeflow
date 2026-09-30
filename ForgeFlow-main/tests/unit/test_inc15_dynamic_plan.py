"""INC15 ② — the dynamic Task Plan (按任务生成计划，而不是固定四步).

Why this file exists
--------------------
Before INC15 the runtime walked a **hard-coded** ``_DEFAULT_STEPS`` constant and
force-fed every task the same four steps. A plain intent was therefore pushed
through ``data.query`` / ``code.run`` even though it had neither a table nor a
repo path — steps that could never run. The four-layer contract had no way to say
"this step did not apply", so those steps were recorded as ``skipped`` and the
report claimed a completeness the run never had.

:mod:`forgeflow.runtime.planning` is the pure, IO-free planner. This file pins its
honest-planning contract, row by row:

* **No invented inputs** — ``table`` / ``paths`` / ``repo_path`` may only come
  from ``explicit_inputs``; ``query`` / ``text`` may be derived from the intent.
* **No fuzzy tool selection** — the intent string never keyword-matches a tool.
* **Three honest outcomes** — ``required`` (all inputs present) / ``blocked``
  (declared but input missing, kept with a reason) / ``not_applicable`` (irrelevant
  — trimmed, never a record).
* **Product-step integrity** — ``report.render`` is always emitted **last** and
  is never trimmed when it is a candidate.

Every case declares its storage tier via ``force_memory_backend`` (the planner is
pure, but the tier must never be inherited).
"""

from __future__ import annotations

from forgeflow.runtime.planning import (
    REPORT_TOOL,
    CapabilityContext,
    TaskPlan,
    applicability,
    build_plan,
    normalize_status,
    observations_from_records,
    resolve_inputs,
)


def _ctx(**kw) -> CapabilityContext:
    base = dict(intent="为 Acme 整理销售线索分析摘要", workflow_type="generic")
    base.update(kw)
    return CapabilityContext(**base)


# --------------------------------------------------------------------------- #
# 1. Candidates → steps: fixed four become a task-shaped subset                #
# --------------------------------------------------------------------------- #
def _default_candidates() -> list[dict]:
    """The deterministic candidate set as ``orchestrator._DEFAULT_STEPS`` is."""
    return [
        {"tool": "research.search", "step_type": "agent", "note": "研究"},
        {"tool": "data.query", "step_type": "agent", "note": "查数据"},
        {"tool": "code.run", "step_type": "agent", "note": "跑代码"},
        {"tool": "report.render", "step_type": "agent", "note": "生成报告"},
    ]


def test_plain_task_trims_data_and_code_and_keeps_the_report(force_memory_backend):
    plan = build_plan(_default_candidates(), _ctx(), run_id="r-1", attempt=0)
    tools = [s.tool for s in plan.steps]
    # A plain task has no table / paths ⇒ data.query / code.run do not apply.
    assert tools == ["research.search", REPORT_TOOL]
    assert tools[-1] == REPORT_TOOL, "产物步必须收尾"
    na = {n.tool for n in plan.not_applicable}
    assert na == {"data.query", "code.run"}, "无关候选步必须被裁掉"


def test_table_input_makes_data_query_applicable(force_memory_backend):
    plan = build_plan(
        _default_candidates(),
        _ctx(explicit_inputs={"table": "orders"}),
        run_id="r-1",
    )
    assert [s.tool for s in plan.steps] == ["research.search", "data.query", REPORT_TOOL]
    assert {n.tool for n in plan.not_applicable} == {"code.run"}


def test_paths_input_makes_code_run_applicable(force_memory_backend):
    plan = build_plan(
        _default_candidates(),
        _ctx(explicit_inputs={"paths": ["src/app.py"]}),
        run_id="r-1",
    )
    assert [s.tool for s in plan.steps] == ["research.search", "code.run", REPORT_TOOL]
    assert {n.tool for n in plan.not_applicable} == {"data.query"}


def test_report_render_is_always_last_and_never_trimmed(force_memory_backend):
    # Even a candidate list that names report.render FIRST still ends on it.
    plan = build_plan(
        [
            {"tool": REPORT_TOOL},
            {"tool": "data.query"},  # not applicable (no table, not declared)
        ],
        _ctx(),
        run_id="r-1",
    )
    assert [s.tool for s in plan.steps] == [REPORT_TOOL]
    assert plan.not_applicable == [] or all(
        n.tool != REPORT_TOOL for n in plan.not_applicable
    )
    assert applicability(REPORT_TOOL, _ctx()) == "required"


# --------------------------------------------------------------------------- #
# 2. declared ⇒ blocked (kept with a reason); not declared ⇒ not_applicable     #
# --------------------------------------------------------------------------- #
def test_declared_tool_with_missing_input_is_blocked_with_a_reason(force_memory_backend):
    ctx = _ctx(declared_tools=["data.query"])
    plan = build_plan([{"tool": "data.query"}, {"tool": REPORT_TOOL}], ctx, run_id="r-1")

    assert [s.tool for s in plan.steps] == ["data.query", REPORT_TOOL]
    blocked = plan.steps[0]
    assert blocked.applicability == "blocked"
    assert blocked.blocked_reason, "受阻步必须写明原因"
    assert "table" in blocked.blocked_reason


def test_undeclared_tool_with_missing_input_is_not_applicable(force_memory_backend):
    plan = build_plan([{"tool": "data.query"}, {"tool": REPORT_TOOL}], _ctx(), run_id="r-1")
    assert [s.tool for s in plan.steps] == [REPORT_TOOL]
    assert [n.tool for n in plan.not_applicable] == ["data.query"]
    assert plan.not_applicable[0].reason


# --------------------------------------------------------------------------- #
# INC17 control case — build_plan DE-DUPLICATES candidates.                     #
#                                                                              #
# This is the evidence that ``plan_from_records`` solves a real defect: the     #
# ReAct loop may call the same tool twice, and ``build_plan`` collapses the     #
# repeat (so it can never give a 1:1 projection of such a trail). The new pure  #
# function is therefore necessary, not a workaround.                           #
# --------------------------------------------------------------------------- #
def test_build_plan_deduplicates_repeated_candidates(force_memory_backend):
    candidates = [
        {"tool": "research.search", "note": "第一次检索"},
        {"tool": "research.search", "note": "第二次检索（重复工具）"},
        {"tool": REPORT_TOOL},
    ]
    plan = build_plan(candidates, _ctx(), run_id="r-1", attempt=0)
    # Three candidates in, but the repeated research.search is collapsed to one.
    assert [s.tool for s in plan.steps] == ["research.search", REPORT_TOOL]
    assert len(plan.steps) == 2


# --------------------------------------------------------------------------- #
# 3. Input resolution never invents a table / path                             #
# --------------------------------------------------------------------------- #
def test_resolve_inputs_never_invents_a_table_or_path(force_memory_backend):
    ctx = _ctx()  # intent only, no explicit table / paths
    args, missing = resolve_inputs("data.query", ctx)
    assert "table" not in args
    assert missing == ["table"]

    args, missing = resolve_inputs("code.run", ctx)
    assert "paths" not in args
    assert "repo_path" not in args
    assert missing == ["paths"]


def test_resolve_inputs_derives_query_and_text_from_intent(force_memory_backend):
    ctx = _ctx(intent="检索 Stripe 融资进展")
    args, missing = resolve_inputs("research.search", ctx)
    assert args["query"] == "检索 Stripe 融资进展"
    assert missing == []

    args, _ = resolve_inputs("docs.parse", ctx)
    assert args["text"] == "检索 Stripe 融资进展"


def test_explicit_table_is_used_verbatim(force_memory_backend):
    ctx = _ctx(explicit_inputs={"table": "warehouse.orders"})
    args, missing = resolve_inputs("data.query", ctx)
    assert args["table"] == "warehouse.orders"
    assert missing == []


# --------------------------------------------------------------------------- #
# 4. The L3 projection: an observation exists only for a truly-executed step    #
# --------------------------------------------------------------------------- #
def test_observations_only_cover_executed_records(force_memory_backend):
    records = [
        {"tool": "research.search", "status": "ok", "executed": True},
        {"tool": "data.query", "status": "blocked", "executed": False},
        {"tool": "code.run", "status": "unavailable", "executed": False},
        {"tool": "report.render", "status": "ok", "executed": True},
    ]
    obs = observations_from_records(records)
    assert [o["tool"] for o in obs] == ["research.search", "report.render"]
    assert obs == [r for r in records if r["executed"] is True]


def test_observations_from_none_is_empty(force_memory_backend):
    assert observations_from_records(None) == []
    assert observations_from_records([]) == []


# --------------------------------------------------------------------------- #
# 5. The retired reader alias: skipped → blocked                                #
# --------------------------------------------------------------------------- #
def test_normalize_status_maps_legacy_skipped_to_blocked(force_memory_backend):
    assert normalize_status("skipped") == "blocked"
    assert normalize_status("SKIPPED") == "blocked"
    assert normalize_status("ok") == "ok"
    assert normalize_status(None) == ""


# --------------------------------------------------------------------------- #
# 6. The plan summary counts are honest                                         #
# --------------------------------------------------------------------------- #
def test_plan_summary_counts_plan_vs_records(force_memory_backend):
    plan: TaskPlan = build_plan(_default_candidates(), _ctx(), run_id="r-1")
    # Plan-only view: blocked counted from the plan's blocked steps (none here).
    summary = plan.summary()
    assert summary["planned"] == len(plan.steps)
    assert summary["not_applicable"] == len(plan.not_applicable)

    records = [
        {"tool": "research.search", "status": "ok", "executed": True},
        {"tool": "data.query", "status": "blocked", "executed": False},
        {"tool": "report.render", "status": "ok", "executed": True},
    ]
    summary = plan.summary(records)
    assert summary["executed"] == 2
    assert summary["succeeded"] == 2
    assert summary["blocked"] == 1
    assert summary["failed"] == 0
