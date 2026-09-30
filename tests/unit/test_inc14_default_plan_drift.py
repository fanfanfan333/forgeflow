"""INC14/INC15 — the plan must end on the run's deliverable step (守卫钉子).

Why this nail exists
--------------------
INC14's whole "result-first" promise rests on the deterministic plan's **last**
step being ``report.render``: that step is the *only* offline producer of a real
``artifacts[]`` deliverable. If someone later deletes it — often the tempting
move to make an older ``len(steps) == 3`` nail go green again — the run page
silently loses its result layer while every other test still passes.

INC15 changed *how* the plan is built: ``orchestrator._DEFAULT_STEPS`` is now the
**candidate** set that :func:`forgeflow.runtime.planning.build_plan` filters by
applicability, not a fixed plan the runtime walks unconditionally. So this nail
no longer checks a constant's contents; it checks the invariant that matters —
for a plain task the plan ends on ``report.render`` and does **not** force-run
``data.query`` / ``code.run`` (which have no input), while supplying their real
input brings them back.

The storage tier is declared explicitly via ``force_memory_backend`` (the
planner is pure, but the tier must never be inherited).
"""

from __future__ import annotations

from forgeflow.runtime.orchestrator import _DEFAULT_STEPS
from forgeflow.runtime.planning import REPORT_TOOL, CapabilityContext, build_plan


def _plan(**kw):
    ctx = CapabilityContext(intent="整理销售线索分析摘要", workflow_type="generic", **kw)
    return build_plan(_DEFAULT_STEPS, ctx, run_id="r-drift", attempt=0)


def test_default_plan_ends_on_the_deliverable_step(force_memory_backend):
    plan = _plan()
    tools = [s.tool for s in plan.steps]
    assert tools[-1] == REPORT_TOOL, (
        "默认计划必须以 report.render 收尾 —— 它是离线档唯一的产物来源；"
        "删掉它会静默丢掉结果层，而不是让旧钉子变绿。"
    )


def test_plain_task_does_not_force_run_data_or_code(force_memory_backend):
    plan = _plan()
    tools = {s.tool for s in plan.steps}
    assert "data.query" not in tools, "无表名却强行规划 data.query"
    assert "code.run" not in tools, "无路径却强行规划 code.run"
    assert {n.tool for n in plan.not_applicable} == {"data.query", "code.run"}


def test_report_render_is_a_candidate_and_the_sole_last_step(force_memory_backend):
    # The deliverable step is still among the candidates (so it can never be
    # dropped from the offline plan by a careless edit).
    assert any(s["tool"] == REPORT_TOOL for s in _DEFAULT_STEPS)
    plan = _plan()
    assert [s.tool for s in plan.steps].count(REPORT_TOOL) == 1


def test_real_inputs_bring_the_trimmed_steps_back(force_memory_backend):
    with_table = _plan(explicit_inputs={"table": "orders"})
    assert "data.query" in {s.tool for s in with_table.steps}
    assert with_table.steps[-1].tool == REPORT_TOOL

    with_paths = _plan(explicit_inputs={"paths": ["src/app.py"]})
    assert "code.run" in {s.tool for s in with_paths.steps}
    assert with_paths.steps[-1].tool == REPORT_TOOL
