"""INC46 T03 — Skill Runtime 真执行：eligibility / procedure / 诚实降级（单元级）。

本文件逐一钉死设计 §1.3 / §3.2 的契约：

1. **eligibility fail-closed**：未解析 tenant / tenant 不匹配 / 未发布 / 角色不符 /
   能力缺失 —— 一律不可用；未解析 tenant ⇒ 空集（`filter_eligible`）。
2. **`load_procedure` 显式读新键 `spec["procedure"]`**：未声明 / 空 / 非 list ⇒ `[]`；
   `spec["steps"]`（list[str]）**一字不动**、永不参与驱动。
3. **工具选择只来自 procedure + 白名单**：`to_plan_candidates` 逐字取 `step.tool`，
   非白名单工具被**剔除且绝不替换**（无 intent 关键词 / 模糊匹配）。
4. **三种诚实降级逐字可断言**：未声明 procedure / 工具不在白名单 / 权限不足。
5. **`_candidates_for` 无 procedure 时与今日逐字节相同**（既有的 plane 注入不破）。
6. **`SkillNotExecutable` 携带 verbatim reason + `status_code < 500`**。

引文纪律：一律 ``file.py::symbol``，不写行号。
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS, describe_denial
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
from forgeflow.skills.eligibility import (
    filter_eligible,
    is_eligible,
)
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.runtime import (
    UNDECLARED_PROCEDURE_REASON,
    SkillNotExecutable,
    SkillRuntime,
    SkillStep,
    load_procedure,
    to_plan_candidates,
)

# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _skill(**kw) -> SkillRecord:
    base = dict(
        id="skill-1",
        tenant_id="t-1",
        name="客户流失分析",
        domain="数据分析",
        description="识别流失并给出挽留建议",
        current_version="1.0.0",
        status="published",
        tags=[],
    )
    base.update(kw)
    return SkillRecord(**base)


def _step(tool: str, *, purpose: str = "", **kw) -> SkillStep:
    return SkillStep(purpose=purpose or f"step {tool}", tool=tool, **kw)


# --------------------------------------------------------------------------- #
# 1. eligibility — fail-closed                                                  #
# --------------------------------------------------------------------------- #
def test_unresolved_tenant_is_ineligible_and_filter_returns_empty():
    verdict = is_eligible(
        _skill(), tenant_id=None, role="manager", capabilities=["cap:x"]
    )
    assert verdict.eligible is False
    assert "租户未解析" in verdict.reason
    # Empty / "" is treated identically.
    assert is_eligible("", tenant_id="", role="manager", capabilities=[]).eligible is False
    assert (
        filter_eligible(
            [_skill()], tenant_id=None, role="manager", capabilities=[]
        )
        == []
    )


def test_tenant_mismatch_is_ineligible_and_none_tenant_never_matches():
    assert (
        is_eligible(
            _skill(tenant_id="t-other"),
            tenant_id="t-1",
            role="manager",
            capabilities=[],
        ).eligible
        is False
    )
    # A ``None`` tenant record matches no tenant (no "global" leak).
    assert (
        is_eligible(
            _skill(tenant_id=None), tenant_id="t-1", role="manager", capabilities=[]
        ).eligible
        is False
    )


def test_only_published_skills_are_eligible():
    for status in ("draft", "evaluating", "retired"):
        verdict = is_eligible(
            _skill(status=status), tenant_id="t-1", role="manager", capabilities=[]
        )
        assert verdict.eligible is False
        assert "未发布" in verdict.reason
    assert (
        is_eligible(
            _skill(status="published"), tenant_id="t-1", role="manager", capabilities=[]
        ).eligible
        is True
    )


def test_role_tag_restricts_audience_and_empty_role_is_fail_closed():
    gated = _skill(tags=["role:manager", "数据分析"])
    assert (
        is_eligible(gated, tenant_id="t-1", role="manager", capabilities=[]).eligible
        is True
    )
    assert (
        is_eligible(gated, tenant_id="t-1", role="sales_rep", capabilities=[]).eligible
        is False
    )
    # No ``role:`` tag ⇒ open to any resolved role; an unresolved role is denied.
    assert (
        is_eligible(_skill(), tenant_id="t-1", role="sales_rep", capabilities=[]).eligible
        is True
    )
    assert (
        is_eligible(_skill(), tenant_id="t-1", role="", capabilities=[]).eligible is False
    )


def test_capability_tag_requires_the_caller_to_hold_it():
    gated = _skill(tags=["cap:skills:execute", "cap:approve:skills"])
    assert (
        is_eligible(
            gated,
            tenant_id="t-1",
            role="manager",
            capabilities=["skills:execute", "approve:skills"],
        ).eligible
        is True
    )
    verdict = is_eligible(
        gated, tenant_id="t-1", role="manager", capabilities=["skills:execute"]
    )
    assert verdict.eligible is False
    assert "approve:skills" in verdict.reason


def test_filter_eligible_preserves_order_and_drops_ineligible():
    a = _skill(id="a")
    b = _skill(id="b", status="draft")
    c = _skill(id="c", tags=["role:manager"])
    out = filter_eligible(
        [a, b, c], tenant_id="t-1", role="manager", capabilities=[]
    )
    assert [s.id for s in out] == ["a", "c"]


# --------------------------------------------------------------------------- #
# 2. load_procedure — explicit new key only                                    #
# --------------------------------------------------------------------------- #
def test_load_procedure_is_empty_when_undeclared_or_malformed():
    assert load_procedure(None) == []
    assert load_procedure({}) == []
    assert load_procedure({"steps": ["拉取数据", "计算概率"]}) == []  # legacy ignored
    assert load_procedure({"procedure": []}) == []
    assert load_procedure({"procedure": "not-a-list"}) == []
    # Non-dict entries are skipped (never fabricated into a step).
    assert load_procedure({"procedure": [None, 7, "x"]}) == []


def test_load_procedure_reads_the_declared_fields_verbatim():
    spec = {
        "steps": ["legacy text step"],
        "procedure": [
            {
                "purpose": "读取客户行为",
                "tool": "data.query",
                "input": ["table"],
                "output": ["rows"],
                "validation": "rows>0",
            },
            {"tool": "analysis.score"},
        ],
    }
    steps = load_procedure(spec)
    assert len(steps) == 2
    first, second = steps
    assert first.purpose == "读取客户行为"
    assert first.tool == "data.query"
    assert first.input_keys == ["table"]
    assert first.output_keys == ["rows"]
    assert first.validation == "rows>0"
    # Missing optional fields normalise to honest defaults.
    assert second.purpose == ""
    assert second.tool == "analysis.score"
    assert second.input_keys == []
    assert second.output_keys == []
    assert second.validation == ""


# --------------------------------------------------------------------------- #
# 3. to_plan_candidates — verbatim tool + whitelist, never substitute          #
# --------------------------------------------------------------------------- #
def test_to_plan_candidates_filters_whitelist_and_never_substitutes():
    procedure = [
        _step("policy.check", purpose="合规检查"),
        _step("evil.tool", purpose="任何看起来像分析的任务"),  # NOT whitelisted
        _step("", purpose="data.query is mentioned but no tool declared"),
    ]
    candidates = to_plan_candidates(_skill(), procedure)
    tools = [c["tool"] for c in candidates]
    assert tools == ["policy.check"]
    # No fuzzy substitution: the purpose text naming ``data.query`` never adds it.
    assert "data.query" not in tools
    assert "evil.tool" not in tools
    assert candidates[0]["step_type"] == "skill"
    assert "客户流失分析" in candidates[0]["note"]


def test_execution_candidates_matches_the_module_projection():
    procedure = [_step("policy.check"), _step("evil.tool")]
    runtime = SkillRuntime()
    assert runtime.execution_candidates(procedure) == to_plan_candidates(None, procedure)


def test_whitelist_is_the_single_fact_source():
    # Every whitelisted tool is admitted; a near-miss is not.
    procedure = [_step(t) for t in sorted(PLATFORM_PLAN_TOOLS)]
    assert {c["tool"] for c in to_plan_candidates(None, procedure)} == set(
        PLATFORM_PLAN_TOOLS
    )


# --------------------------------------------------------------------------- #
# 4. degrade_reason — three honest degradations, verbatim                      #
# --------------------------------------------------------------------------- #
def test_degrade_reason_no_procedure_is_verbatim():
    # An empty procedure (the "skill declared no procedure" case) is verbatim.
    assert (
        SkillRuntime().degrade_reason([], role="manager")
        == UNDECLARED_PROCEDURE_REASON
    )
    assert UNDECLARED_PROCEDURE_REASON == "技能未声明 procedure，无法逐步执行"
    # A procedurally-loaded spec without ``procedure`` yields the same reason.
    assert (
        SkillRuntime().degrade_reason(load_procedure({"steps": ["x"]}), role="manager")
        == UNDECLARED_PROCEDURE_REASON
    )


def test_degrade_reason_tool_outside_whitelist_is_verbatim_and_not_substituted():
    reason = SkillRuntime().degrade_reason(
        [_step("policy.check"), _step("evil.tool")], role="manager"
    )
    assert reason == "技能声明的工具 'evil.tool' 不在平台白名单内，拒绝执行"


def test_degrade_reason_permission_denial_uses_describe_denial():
    reason = SkillRuntime().degrade_reason([_step("policy.check")], role="viewer")
    # The existing gate wording (verbatim) — a permission denial, not a 5xx.
    assert reason == describe_denial("viewer", "policy.check")
    assert "权限不足" in reason
    # A role that holds the run grant can drive the same procedure.
    assert SkillRuntime().degrade_reason([_step("policy.check")], role="manager") is None
    assert SkillRuntime().degrade_reason([_step("policy.check")], role="sales_rep") is None


def test_require_raises_skill_not_executable_below_500():
    runtime = SkillRuntime()
    with pytest.raises(SkillNotExecutable) as einfo:
        runtime.require([_step("evil.tool")], role="manager")
    assert einfo.value.status_code < 500
    assert einfo.value.reason == "技能声明的工具 'evil.tool' 不在平台白名单内，拒绝执行"

    with pytest.raises(SkillNotExecutable) as perr:
        runtime.require([_step("policy.check")], role="viewer")
    assert perr.value.status_code == 403 and perr.value.status_code < 500

    # An executable procedure is a no-op.
    assert runtime.require([_step("policy.check")], role="manager") is None


# --------------------------------------------------------------------------- #
# 5. SkillRuntime — tenant gate before any read, and the load path             #
# --------------------------------------------------------------------------- #
class _StubRegistry:
    def __init__(self, *, skills=None, versions=None) -> None:
        self._skills = list(skills or [])
        self._versions = list(versions or [])
        self.read_calls = 0

    async def list_skills(self, tenant_id, **kw):
        self.read_calls += 1
        return list(self._skills), len(self._skills)

    async def select(self, tenant_id, intent, k=3, **kw):
        self.read_calls += 1
        return list(self._skills)[:k]

    async def get(self, tenant_id, skill_id):
        self.read_calls += 1
        return next((s for s in self._skills if s.id == skill_id), None)

    async def versions(self, tenant_id, skill_id):
        self.read_calls += 1
        return list(self._versions)


async def test_unresolved_tenant_fails_closed_403_before_any_read():
    registry = _StubRegistry(skills=[_skill()])
    runtime = SkillRuntime(registry=registry)  # type: ignore[arg-type]
    with pytest.raises(GovernanceError) as exc:
        await runtime.eligible(None, role="manager", capabilities=[])
    assert exc.value.status_code == 403
    with pytest.raises(GovernanceError):
        await runtime.select("t-1" if False else None, "x", role="manager", capabilities=[])
    with pytest.raises(GovernanceError):
        await runtime.load(None, "skill-1")
    # Nothing was read before the gate raised.
    assert registry.read_calls == 0


async def test_eligible_and_select_apply_the_filter():
    eligible = _skill(id="ok")
    draft = _skill(id="draft", status="draft")
    registry = _StubRegistry(skills=[draft, eligible])
    runtime = SkillRuntime(registry=registry)  # type: ignore[arg-type]

    out = await runtime.eligible("t-1", role="manager", capabilities=[])
    assert [s.id for s in out] == ["ok"]

    chosen = await runtime.select("t-1", "流失", role="manager", capabilities=[], k=3)
    assert chosen is not None and chosen.id == "ok"


async def test_select_returns_none_when_no_eligible_candidate():
    only_draft = _StubRegistry(skills=[_skill(status="draft")])
    runtime = SkillRuntime(registry=only_draft)  # type: ignore[arg-type]
    assert await runtime.select("t-1", "x", role="manager", capabilities=[]) is None


async def test_load_reads_the_current_version_procedure():
    version = SkillVersionRecord(
        id="v1",
        tenant_id="t-1",
        skill_id="skill-1",
        semver="1.0.0",
        spec={
            "steps": ["legacy"],
            "procedure": [{"purpose": "检查", "tool": "policy.check"}],
        },
    )
    registry = _StubRegistry(skills=[_skill()], versions=[version])
    runtime = SkillRuntime(registry=registry)  # type: ignore[arg-type]
    steps = await runtime.load("t-1", "skill-1")
    assert [s.tool for s in steps] == ["policy.check"]

    # A missing skill / version degrades to no steps (never fabricated).
    assert await runtime.load("t-1", "does-not-exist") == []


# --------------------------------------------------------------------------- #
# 6. orchestrator wiring — no procedure ⇒ byte-for-byte identical              #
# --------------------------------------------------------------------------- #
def test_candidates_for_is_byte_identical_without_procedure():
    ctx = RequestContext(tenant_id="t-1", user_id="u", role="manager")
    task = TaskCreate(intent="整理销售线索分析摘要", context={})
    baseline = orch._candidates_for(task, ctx)
    # Today's deterministic candidate set for a plain task — unchanged.
    assert [c["tool"] for c in baseline] == [
        "research.search",
        "data.query",
        "code.run",
        "report.render",
    ]

    # A skill that declares only the legacy ``steps`` text adds nothing.
    legacy_ctx = RequestContext(
        tenant_id="t-1",
        user_id="u",
        role="manager",
        injected_skills=[
            {
                "id": "skill-1",
                "name": "客户流失分析",
                "version": "1.0.0",
                "description": "d",
                "steps": ["拉取数据", "计算概率"],
            }
        ],
    )
    assert orch._candidates_for(task, legacy_ctx) == baseline
    assert orch._skill_candidates(legacy_ctx) == []
    # No injected skills at all ⇒ also identical (and declared-tools unchanged).
    assert orch._candidates_for(task, ctx) == baseline
    assert orch._declared_plan_tools(task, legacy_ctx) == orch._declared_plan_tools(
        task, ctx
    )


def test_skill_procedure_candidates_are_prepended_and_whitelist_filtered():
    ctx = RequestContext(
        tenant_id="t-1",
        user_id="u",
        role="manager",
        injected_skills=[
            {
                "id": "skill-1",
                "name": "健康检查",
                "version": "1.0.0",
                "description": "d",
                "steps": ["legacy text"],
                "procedure": [
                    {"purpose": "合规检查", "tool": "policy.check"},
                    {"purpose": "越权工具", "tool": "evil.tool"},
                ],
            }
        ],
    )
    task = TaskCreate(intent="检查策略", context={})
    tools = [c["tool"] for c in orch._candidates_for(task, ctx)]
    # Prepended (planned first) and whitelist-filtered — never substituted.
    assert tools[0] == "policy.check"
    assert "evil.tool" not in tools
    assert tools[-1] == "report.render"

    # A skill-declared tool is registered as *declared* so it stays blocked
    # (not trimmed) when its real input is missing.
    assert "policy.check" in orch._declared_plan_tools(task, ctx)


def test_skill_declared_under_specified_step_stays_blocked_in_the_plan():
    from forgeflow.runtime.orchestrator import _build_task_plan

    ctx = RequestContext(
        tenant_id="t-1",
        user_id="u",
        role="manager",
        injected_skills=[
            {
                "id": "skill-1",
                "name": "报告",
                "version": "1.0.0",
                "procedure": [{"tool": "analysis.profile", "purpose": "分析"}],
            }
        ],
    )
    task = TaskCreate(intent="分析", context={})
    plan = _build_task_plan(
        orch._candidates_for(task, ctx),
        task,
        ctx,
        run_id="r-1",
        attempt=0,
        source="deterministic",
        records=[],
        declared_tools=orch._declared_plan_tools(task, ctx),
    )
    by_tool = {s.tool: s for s in plan.steps}
    assert "analysis.profile" in by_tool, "skill 声明的步不得被裁掉"
    assert by_tool["analysis.profile"].applicability == "blocked"
    assert by_tool["analysis.profile"].blocked_reason
