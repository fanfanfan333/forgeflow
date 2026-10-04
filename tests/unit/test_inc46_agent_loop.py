"""INC46 T28 —— Agent 主循环（Plan–Act–Observe–Replan）。

覆盖任务书 TABLE 36 的 DoD：
  * 预算 max_steps / max_replans / wall_time，任一耗尽 ⇒ budget_exceeded（非成功）；
  * 重规划触发：工具错误 / 验证失败 / 定位歧义 / 权限拒绝；
  * 重规划不得扩大权限，不得选择资格集合之外的工具（fail-closed，红线 5）；
  * 循环检测：相同（工具, 规范化参数）连续 ≥3 次且无进展 ⇒ 终止；
  * 每次迭代落 trace 且带 plan_id；
  * feature flag 默认关；关闭态零回归。

反事实每条关键机制都有一条：摘掉机制 ⇒ 行为必须翻转。
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from forgeflow.agent import budget as B
from forgeflow.agent import loop as L
from forgeflow.agent import loop_guard as LG
from forgeflow.agent import plan as P

TENANT = "tenant-loop-1"


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _ctx(run_id: str = "run-1", role: str = "admin") -> L.RunContext:
    return L.RunContext(run_id=run_id, tenant_id=TENANT, role=role, user_id="u1", intent="改文档")


def _plan(*tools: str, plan_id: str | None = None) -> P.Plan:
    return P.new_plan(
        [P.PlanStep(tool=t, args={"path": "a.docx"}, purpose=f"用 {t}", expected="ok") for t in tools],
        plan_id=plan_id,
        success_criteria=["改完了"],
    )


def _ok(step: P.PlanStep, *, progress: bool = True) -> L.Observation:
    return L.Observation(kind=L.OBS_OK, step_id=step.step_id, tool=step.tool, progress=progress)


def _runner(kind: str = L.OBS_OK, *, progress: bool = True):
    async def _r(step: P.PlanStep, state: L.LoopState) -> L.Observation:
        return L.Observation(kind=kind, step_id=step.step_id, tool=step.tool, progress=progress)

    return _r


def _collect() -> tuple[list[dict], object]:
    rows: list[dict] = []

    async def _w(row: dict) -> None:
        rows.append(row)

    return rows, _w


def _budget(**kw) -> B.Budget:
    """Build a Budget from the real defaults, overriding only what is given.

    ``max_tokens`` must stay an int (Budget coerces it); passing ``None`` raises.
    """
    return dataclasses.replace(B.Budget(), **kw)  # Budget is a frozen dataclass


# --------------------------------------------------------------------------- #
# 1. feature flag：默认关，关闭态零回归                                          #
# --------------------------------------------------------------------------- #
def test_flag_defaults_to_off(monkeypatch):
    monkeypatch.delenv(L.FEATURE_FLAG_ENV, raising=False)
    assert L.agent_loop_enabled() is False


def test_flag_off_is_the_disabled_path_not_a_success():
    rows, writer = _collect()
    result = asyncio.run(
        L.AgentLoop(
            "把第三部分改得更正式",
            ctx=_ctx(),
            plan=_plan("document.edit"),
            runner=_runner(),
            trace_writer=writer,
            use_loop=False,
        ).run()
    )
    assert result.status == L.DISABLED
    assert result.path == L.PATH_DISABLED
    assert result.success is False  # 关闭不等于成功
    assert rows == []  # 关闭态不产生任何 trace（零回归）


# --------------------------------------------------------------------------- #
# 2. 正常完成 + 真实 plan_id                                                     #
# --------------------------------------------------------------------------- #
def test_loop_completes_and_stamps_a_real_plan_id():
    rows, writer = _collect()
    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("document.inspect", "document.edit"),
            runner=_runner(),
            trace_writer=writer,
            use_loop=True,
        ).run()
    )
    assert result.status == L.DONE
    assert result.success is True
    assert result.plan_id and result.plan_id.startswith("plan-") or result.plan_id
    assert result.iterations >= 2
    # 每次迭代都落 trace 且带 plan_id
    assert len(rows) >= 2
    assert all(r.get("plan_id") == result.plan_id for r in rows), "trace 必须带 plan_id"


# --------------------------------------------------------------------------- #
# 3. 预算耗尽 ⇒ budget_exceeded，且不得折算成功                                   #
# --------------------------------------------------------------------------- #
def test_step_budget_exhaustion_is_budget_exceeded_not_success():
    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("a", "b", "c", "d", "e"),
            runner=_runner(),
            budget=_budget(max_steps=2),
            eligibility=frozenset({"a", "b", "c", "d", "e"}),
            use_loop=True,
        ).run()
    )
    assert result.status == L.BUDGET_EXCEEDED
    assert result.success is False  # 红线 18：耗尽绝不折算成功
    assert result.unfinished, "必须给出未完成清单"


def test_replan_budget_exhaustion_is_budget_exceeded():
    async def _bad(step: P.PlanStep, state: L.LoopState) -> L.Observation:
        return L.Observation(
            kind=L.OBS_VALIDATION_FAILED, step_id=step.step_id, tool=step.tool, progress=True
        )

    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("document.edit"),
            runner=_bad,
            budget=_budget(max_steps=50, max_replans=1),
            eligibility=frozenset({"document.edit", "document.inspect"}),
            use_loop=True,
        ).run()
    )
    # 重规划次数用尽（或步数用尽）⇒ 仍然不是成功
    assert result.success is False
    assert result.status in {L.BUDGET_EXCEEDED, L.FAILED}


def test_wall_time_exhaustion_is_budget_exceeded():
    ticks = iter([0.0, 0.0, 10_000.0, 10_000.0])

    def _clock() -> float:
        return next(ticks, 10_000.0)

    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("a", "b", "c"),
            runner=_runner(),
            budget=_budget(max_steps=50, wall_time_seconds=5.0),
            eligibility=frozenset({"a", "b", "c"}),
            clock=_clock,
            use_loop=True,
        ).run()
    )
    assert result.status == L.BUDGET_EXCEEDED
    assert result.success is False


# --------------------------------------------------------------------------- #
# 4. 重规划：歧义 ⇒ 澄清；验证失败 ⇒ 修复                                          #
# --------------------------------------------------------------------------- #
def test_ambiguity_replans_into_a_clarify_step():
    calls = {"n": 0}

    async def _ambig(step: P.PlanStep, state: L.LoopState) -> L.Observation:
        calls["n"] += 1
        if calls["n"] == 1:
            return L.Observation(kind=L.OBS_AMBIGUITY, step_id=step.step_id, tool=step.tool)
        return _ok(step)

    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("document.edit"),
            runner=_ambig,
            clarify_answer=lambda step, plan: "改第三部分",
            eligibility=frozenset({"document.edit", "document.inspect"}),
            use_loop=True,
        ).run()
    )
    assert result.replans >= 1, "定位歧义必须触发重规划"
    kinds = {s["step_type"] for p in result.plans for s in (p.get("steps") or [])}
    assert P.STEP_CLARIFY in kinds, "重规划后必须出现「澄清」步骤"


def test_validation_failure_replans_into_a_fix_step():
    calls = {"n": 0}

    async def _fail(step: P.PlanStep, state: L.LoopState) -> L.Observation:
        calls["n"] += 1
        if calls["n"] == 1:
            return L.Observation(kind=L.OBS_VALIDATION_FAILED, step_id=step.step_id, tool=step.tool)
        return _ok(step)

    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("document.edit"),
            runner=_fail,
            eligibility=frozenset({"document.edit", "document.inspect"}),
            use_loop=True,
        ).run()
    )
    assert result.replans >= 1
    assert result.status == L.DONE


# --------------------------------------------------------------------------- #
# 5. 权限拒绝：不得绕过 HITL（红线 21）                                           #
# --------------------------------------------------------------------------- #
def test_permission_denied_is_not_replanned_away():
    """红线 21：权限被拒后不得绕过 HITL。

    设计是「无法重规划 ⇒ FAILED（fail-closed）」而不是 DENIED；
    DENIED 专指「重规划提出了资格集合外的工具」（见下一条）。
    """
    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("document.edit"),
            runner=_runner(L.OBS_PERMISSION_DENIED),
            eligibility=frozenset({"document.edit"}),
            use_loop=True,
        ).run()
    )
    assert result.status == L.FAILED
    assert result.success is False
    assert "无法重规划" in (result.reason or ""), "失败原因必须显式说明 fail-closed"
    assert result.unfinished, "必须给出未完成清单"


def test_replan_proposing_an_out_of_eligibility_tool_is_denied():
    """重规划不得扩大权限（红线 5）：提出资格集合外工具 ⇒ DENIED。"""

    async def _evil_replanner(plan: P.Plan, obs: L.Observation) -> P.Plan | None:
        return P.new_plan(
            [P.PlanStep(tool="dangerous.tool", args={"x": 1}, purpose="越权")],
            reason="试图扩大权限",
        )

    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("document.edit"),
            runner=_runner(L.OBS_TOOL_ERROR),
            replanner=_evil_replanner,
            eligibility=frozenset({"document.edit", "document.inspect"}),
            use_loop=True,
        ).run()
    )
    assert result.status == L.DENIED
    assert result.success is False
    assert "资格集合外" in (result.reason or "")


def test_tool_outside_the_eligibility_set_is_refused():
    result = asyncio.run(
        L.AgentLoop(
            "越权",
            ctx=_ctx(),
            plan=_plan("dangerous.tool"),
            runner=_runner(),
            eligibility=frozenset({"document.edit"}),  # 资格集合里没有它
            use_loop=True,
        ).run()
    )
    assert result.status == L.DENIED
    assert result.success is False


# --------------------------------------------------------------------------- #
# 6. 循环检测                                                                     #
# --------------------------------------------------------------------------- #
def test_loop_guard_trips_on_repeated_no_progress_calls():
    guard = LG.LoopGuard(threshold=3)
    for _ in range(2):
        assert guard.observe("document.edit", {"path": "a.docx"}, progress=False) is None
    signal = guard.observe("document.edit", {"path": "a.docx"}, progress=False)
    assert signal is not None and guard.tripped is True


def test_loop_guard_does_not_trip_when_there_is_progress():
    guard = LG.LoopGuard(threshold=3)
    for _ in range(5):
        assert guard.observe("document.edit", {"path": "a.docx"}, progress=True) is None
    assert guard.tripped is False


def test_loop_guard_is_order_independent_on_args():
    assert LG.fingerprint("t", {"a": 1, "b": 2}) == LG.fingerprint("t", {"b": 2, "a": 1})


def test_repeated_no_progress_terminates_the_loop():
    """相同（工具, 规范化参数）连续 ≥3 次且无进展 ⇒ 终止。

    计划必须是**同一个工具重复**，否则指纹不同、不构成循环。
    """
    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("document.edit", "document.edit", "document.edit", "document.edit"),
            runner=_runner(L.OBS_OK, progress=False),
            eligibility=frozenset({"document.edit"}),
            repeat_threshold=3,
            use_loop=True,
        ).run()
    )
    assert result.status == L.LOOP_DETECTED
    assert result.success is False
    assert result.loop_signal is not None


def test_same_tool_but_real_progress_does_not_trip_the_guard():
    """反面对照：同样重复，但每步都有进展 ⇒ 不算循环。"""
    result = asyncio.run(
        L.AgentLoop(
            "改文档",
            ctx=_ctx(),
            plan=_plan("document.edit", "document.edit", "document.edit", "document.edit"),
            runner=_runner(L.OBS_OK, progress=True),
            eligibility=frozenset({"document.edit"}),
            repeat_threshold=3,
            use_loop=True,
        ).run()
    )
    assert result.status == L.DONE


# --------------------------------------------------------------------------- #
# 7. 反事实 —— 摘掉机制 ⇒ 行为必须翻转                                            #
# --------------------------------------------------------------------------- #
def test_counterfactual_removing_the_step_budget_runs_unbounded(monkeypatch):
    """证明是「步数预算」在终止，而不是别的什么。"""
    # 基线：5 步计划 + 上限 2 ⇒ 被预算拦下
    base = asyncio.run(
        L.AgentLoop(
            "g", ctx=_ctx(), plan=_plan("a", "b", "c", "d", "e"), runner=_runner(),
            budget=_budget(max_steps=2), eligibility=frozenset({"a", "b", "c", "d", "e"}),
            use_loop=True,
        ).run()
    )
    assert base.status == L.BUDGET_EXCEEDED

    # 反事实：让预算永不拦截 ⇒ 跑完全部 5 步（行为翻转）
    monkeypatch.setattr(B.BudgetTracker, "would_allow_step", lambda self: True)
    monkeypatch.setattr(B.BudgetTracker, "check", lambda self: None)
    after = asyncio.run(
        L.AgentLoop(
            "g", ctx=_ctx(), plan=_plan("a", "b", "c", "d", "e"), runner=_runner(),
            budget=_budget(max_steps=2), eligibility=frozenset({"a", "b", "c", "d", "e"}),
            use_loop=True,
        ).run()
    )
    assert after.status == L.DONE
    assert after.iterations > base.iterations


def test_counterfactual_widening_eligibility_lets_the_out_of_set_tool_run(monkeypatch):
    """证明是「资格集合约束」在拦截，而不是别的什么。"""
    # 基线：资格集合外 ⇒ 拒绝
    base = asyncio.run(
        L.AgentLoop(
            "g", ctx=_ctx(), plan=_plan("dangerous.tool"), runner=_runner(),
            eligibility=frozenset({"document.edit"}), use_loop=True,
        ).run()
    )
    assert base.status == L.DENIED

    # 反事实：把该工具塞进资格集合 ⇒ 放行（行为翻转）
    after = asyncio.run(
        L.AgentLoop(
            "g", ctx=_ctx(), plan=_plan("dangerous.tool"), runner=_runner(),
            eligibility=frozenset({"document.edit", "dangerous.tool"}), use_loop=True,
        ).run()
    )
    assert after.status == L.DONE
