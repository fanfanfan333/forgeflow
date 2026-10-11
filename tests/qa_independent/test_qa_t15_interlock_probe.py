"""QA-INC46-T15 — 独立对抗性复核探针（QA 严过关，第二双眼睛）。

工程师自测见 ``tests/unit/test_inc46_publish_interlock.py``。本文件**不复述**
其用例，只针对任务簿 DoD 之外的边界情形独立设计，并且每条断言都配**阳性对照**
（证明探针本身不是永真/永假）：

P1  重复批准同一 pending 版本 / 已被更新版本取代的版本 ⇒ 409，台账无重复有效记录
P2  rejected 终态不可复活（换 approver、换 admin 角色也拒绝）
P3  重复暂存（rejected 之后 / 仍 pending 时）不得复用同一 semver、不得覆盖终态
P4  非法/不存在输入 ⇒ 404/409，绝不 500（skill 不存在 / semver 非法 / 未暂存）
P5  RBAC 角色矩阵（viewer / sales_rep / service / manager / admin / 未认证）
P6  跨租户 HTTP 全栈（B 租户 token 批准 A 租户 skill ⇒ 404，A 租户状态不动）
P7  能力探针形状变异 / 诚实性（非 dict、非 callable、无证据、flag 单独不解除）
P8  incumbent 保护（回归未过 / 评估缺失 / 无可比基线 / warning 带 / 无基线 / 生产入口）
P9  台账与入参诚实性（非法 decision、未解析租户 fail-closed、租户隔离）
P11 边界：current_version 指向缺失行时暂存不得铸造低于 incumbent 的 semver（Round 2 起转常规钉子）
P10 发布状态机真值表

Round 2 修订说明（QA 自改，非源码）：Round 1 的 ``test_p8a2`` 是**特征化缺陷**探针
（把 B1/B3 的缺陷行为写成期望）。B1/B2/B3 修复后，该用例已按任务书语义翻转为规格
探针（无评估 ⇒ 不得进入可批准态），并新增 ``test_p8e``（闸门②是活的）与
``test_p11``（陈旧指针边界）。

形状探测（先探测再断言，禁手搓入参）：
* 8 个探针锚点模块（candidate_gate / real_isolation / outcome_labels /
  golden_regression / redaction / quarantine / auto_rollback / effect_benchmarks）
  落盘前实测**全部 ABSENT** ⇒ 默认 R1–R8 全未满足是真实态，非测试构造。
  **Round 3 更新（非源码，随 T08 落地）**：`forgeflow/skills/candidate_gate.py`
  已由 T08 落地 ⇒ R1 现为**真实满足**，`test_p7b` 的缺失基线随之刷新为 R2–R8；
  R1 单独满足不解除联锁（Level-1 需 R1–R6，Level-2 需 R1–R8）。
* 另含 ``test_probe_fail_closed_when_capability_module_missing``（P7 家族 fail-closed
  阳性对照，QA 自建）：锚点指向确实不存在的模块 ⇒ 未满足；真实 R2–R8 缺失 ⇒ 仍锁死；
  注入全部 8 个满足 **stub**（模拟全满足态，≠ 真实全满足态）⇒ 放行（证明非恒锁）。
* 所有 ``SkillRecord`` / ``SkillVersionRecord`` 均按真实 dataclass 字段构造；
  调用的是真实 memory 仓库（``force_memory_backend``）与真实 HTTP 栈。

命令（必须在 ForgeFlow-main 下运行）::

    D:/workbuddy-cn/profile/binaries/python/envs/agentflow/Scripts/python.exe \
        -m pytest tests/qa_independent/test_qa_t15_interlock_probe.py -p no:cacheprovider
"""

from __future__ import annotations

from tests.business_auth import authenticate_business_app

import sys
import types
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.auth.jwt import create_access_token
from forgeflow.config import get_settings
from forgeflow.repositories import get_skill_repository
from forgeflow.repositories.memory.skill_repo import clear_skill_store
from forgeflow.skills import publish_interlock as pi
from forgeflow.skills import evolution_loop as el
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.evolution_loop import (
    EVOLVE_TRIGGER_MIN_FAILURES,
    maybe_evolve,
    reset_evolution_state,
)
from forgeflow.skills.models import SkillRecord, SkillVersionRecord

#: A second tenant that owns nothing — the cross-tenant attacker's workspace.
TENANT_B = "tenant-qa-b"


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
def _tenant() -> str:
    return get_settings().default_tenant_id


def _manager_token(workspace: str | None = None, user_id: str = "qa-manager") -> str:
    return create_access_token(user_id=user_id, role="manager", workspace_id=workspace)


def _probe_level1_only(anchor: pi.CapabilityAnchor) -> pi.RequirementStatus:
    """Test seam: R1–R6 satisfied, R7/R8 absent (Level-1 open, Level-2 locked)."""
    met = anchor.requirement in pi.LEVEL1_REQUIREMENTS
    return pi.RequirementStatus(
        anchor.requirement,
        anchor.task,
        anchor.description,
        met,
        f"测试证据:{anchor.task}" if met else f"无证据：{anchor.task} 未落地（QA 探针模拟）",
    )


def _candidate(candidate_id: str = "qa-cand-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=candidate_id,
        status="compiled",
        name="QA 联锁技能",
        domain="general",
        experience_ids=["e1", "e2", "e3"],
        draft_spec={"prompt": "改进后的提示词", "tools": []},
    )


async def _seed(
    skill_id: str = "qa-skill-1",
    *,
    incumbent_score: float | None = 0.80,
    add_incumbent_row: bool = True,
) -> tuple[object, str, SkillRecord]:
    """Seed one skill (+ its incumbent version row) on the real memory repo."""
    clear_skill_store()
    repo = get_skill_repository()
    tenant = _tenant()
    skill = SkillRecord(
        id=skill_id,
        tenant_id=tenant,
        name="QA 联锁技能",
        current_version="1.0.0",
        status="published",
    )
    await repo.create_skill(skill)
    if add_incumbent_row:
        await repo.add_version(
            tenant,
            SkillVersionRecord(
                tenant_id=tenant,
                skill_id=skill_id,
                semver="1.0.0",
                eval_score=incumbent_score,
            ),
        )
    return repo, tenant, skill


@pytest.fixture(autouse=True)
def _clean_qa_state():
    pi.reset_interlock_state()
    reset_evolution_state()
    clear_skill_store()
    yield
    pi.reset_interlock_state()
    reset_evolution_state()
    clear_skill_store()


# --------------------------------------------------------------------------- #
# P1 — 重复批准                                                                #
# --------------------------------------------------------------------------- #
async def test_p1_repeat_approval_of_a_published_version_is_409_without_duplicate_record(
    force_memory_backend, monkeypatch
):
    repo, tenant, skill = await _seed("qa-skill-p1")
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.95, skill_repo=repo
    )

    first = await pi.approve_publish(
        tenant, skill.id, staged.semver,
        approver="qa-m1", approver_role="manager", reason="首次批准", skill_repo=repo,
    )
    assert first["state"] == "published", "阳性对照：第一次批准必须成功"

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            tenant, skill.id, staged.semver,
            approver="qa-m1", approver_role="manager", skill_repo=repo,
        )
    assert exc.value.status_code == 409, f"重复批准必须 409，实际 {exc.value.status_code}"

    decisions = [r.decision for r in await pi.list_decisions(tenant, skill.id, staged.semver)]
    assert decisions == ["pending", "approved"], decisions
    assert decisions.count("approved") == 1, "台账不得出现重复有效批准记录"
    assert (await repo.get_skill(tenant, skill.id)).current_version == staged.semver


async def test_p1b_reapproving_a_superseded_approved_version_is_409(
    force_memory_backend, monkeypatch
):
    repo, tenant, skill = await _seed("qa-skill-p1b")
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    v1 = await pi.stage_pending_version(
        tenant, skill, _candidate("qa-c1"), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    await pi.approve_publish(
        tenant, skill.id, v1.semver, approver="qa-m1", approver_role="manager", skill_repo=repo
    )
    v2 = await pi.stage_pending_version(
        tenant, skill, _candidate("qa-c2"), actor="evolution", eval_score=0.96, skill_repo=repo
    )
    assert v2.semver != v1.semver, "current 已前移，第二次暂存必须换新 semver"
    await pi.approve_publish(
        tenant, skill.id, v2.semver, approver="qa-m1", approver_role="manager", skill_repo=repo
    )

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            tenant, skill.id, v1.semver, approver="qa-m1", approver_role="manager", skill_repo=repo
        )
    assert exc.value.status_code == 409, "已被取代的已批准版本不可再批准"
    decisions = [r.decision for r in await pi.list_decisions(tenant, skill.id, v1.semver)]
    assert decisions == ["pending", "approved"], decisions


# --------------------------------------------------------------------------- #
# P2 — rejected 终态                                                           #
# --------------------------------------------------------------------------- #
async def test_p2_rejected_version_cannot_be_revived_even_by_admin(
    force_memory_backend, monkeypatch
):
    repo, tenant, skill = await _seed("qa-skill-p2", incumbent_score=0.95)
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.50, skill_repo=repo
    )

    with pytest.raises(GovernanceError) as first:
        await pi.approve_publish(
            tenant, skill.id, staged.semver,
            approver="qa-m1", approver_role="manager", skill_repo=repo,
        )
    assert first.value.status_code == 403, "回归未过 ⇒ 403 拒绝"

    latest = await pi.latest_decision(tenant, skill.id, staged.semver)
    assert latest == "rejected", latest
    assert (await repo.get_skill(tenant, skill.id)).current_version == "1.0.0"

    for role, approver in (("manager", "qa-m2"), ("admin", "qa-admin")):
        with pytest.raises(GovernanceError) as exc:
            await pi.approve_publish(
                tenant, skill.id, staged.semver,
                approver=approver, approver_role=role, skill_repo=repo,
            )
        assert exc.value.status_code == 409, f"{role} 再次批准 rejected 版本必须 409"
    decisions = [r.decision for r in await pi.list_decisions(tenant, skill.id, staged.semver)]
    assert decisions == ["pending", "rejected"], decisions


# --------------------------------------------------------------------------- #
# P3 — 重复暂存的版本身份                                                       #
# --------------------------------------------------------------------------- #
async def test_p3_restaging_after_rejection_must_not_reuse_the_rejected_semver(
    force_memory_backend, monkeypatch
):
    """rejected 为终态 ⇒ 下一轮暂存必须换新 semver，不能复活/覆盖已拒绝版本。"""
    repo, tenant, skill = await _seed("qa-skill-p3", incumbent_score=0.95)
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    staged = await pi.stage_pending_version(
        tenant, skill, _candidate("qa-c1"), actor="evolution", eval_score=0.50, skill_repo=repo
    )
    with pytest.raises(GovernanceError):
        await pi.approve_publish(
            tenant, skill.id, staged.semver,
            approver="qa-m1", approver_role="manager", skill_repo=repo,
        )
    assert await pi.latest_decision(tenant, skill.id, staged.semver) == "rejected"

    # 下一轮演化（新失败窗口）再次暂存 —— 真实路径：maybe_evolve ⇒ stage_pending_version。
    staged2 = await pi.stage_pending_version(
        tenant, skill, _candidate("qa-c2"), actor="evolution", eval_score=0.99, skill_repo=repo
    )

    rows = [v for v in await repo.list_versions(tenant, skill.id) if v.semver == staged2.semver]
    assert len(rows) == 1, (
        f"重复暂存铸造了重复 semver {staged2.semver}（{len(rows)} 行）——"
        "版本身份 (tenant, skill, semver) 被破坏；pg 侧 UNIQUE(skill_id, semver) 会直接报错"
    )
    assert staged2.semver != staged.semver, (
        f"rejected 版本号 {staged.semver} 被复用（应为新 semver）——rejected 非终态"
    )
    assert await pi.latest_decision(tenant, skill.id, staged.semver) == "rejected", (
        "新 pending 决策覆盖了 rejected 终态"
    )


async def test_p3b_staging_twice_while_pending_must_not_mint_a_duplicate_semver(
    force_memory_backend, monkeypatch
):
    repo, tenant, skill = await _seed("qa-skill-p3b", incumbent_score=0.80)
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    first = await pi.stage_pending_version(
        tenant, skill, _candidate("qa-c1"), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    second = await pi.stage_pending_version(
        tenant, skill, _candidate("qa-c2"), actor="evolution", eval_score=0.97, skill_repo=repo
    )

    rows = [v for v in await repo.list_versions(tenant, skill.id) if v.semver == second.semver]
    assert len(rows) == 1, (
        f"pending 期间再次暂存铸造了重复 semver {second.semver}（{len(rows)} 行）"
    )
    assert first.semver != second.semver, "待批准版本仍在，第二次暂存必须换新 semver"


# --------------------------------------------------------------------------- #
# P4 — 非法路径 / 输入合法性                                                    #
# --------------------------------------------------------------------------- #
def test_p4_unknown_skill_or_bad_semver_over_http_is_404_not_500(force_memory_backend):
    authenticate_business_app(app)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {_manager_token()}"}
    urls = [
        "/skills/qa-nope/versions/1.1.0/approve-publish",
        "/skills/qa-nope/versions/not-a-version/approve-publish",
        "/skills/qa-nope/versions/1.1/approve-publish",
        "/skills/qa-nope/versions/1.1.0%3BDROP%20TABLE%20publish_approvals/approve-publish",
    ]
    for url in urls:
        resp = client.post(url, json={}, headers=headers)
        assert resp.status_code == 404, f"{url} => {resp.status_code} {resp.text[:200]}"


async def test_p4b_existing_skill_with_unknown_or_malformed_semver_is_404(
    force_memory_backend, monkeypatch
):
    repo, tenant, skill = await _seed("qa-skill-p4b")
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    for semver in ("not-a-version", "1.1", "9.9.9", "1.1.0;DROP TABLE publish_approvals"):
        with pytest.raises(GovernanceError) as exc:
            await pi.approve_publish(
                tenant, skill.id, semver,
                approver="qa-m1", approver_role="manager", skill_repo=repo,
            )
        assert exc.value.status_code == 404, f"semver={semver!r} => {exc.value.status_code}"
        assert "not found" in str(exc.value)


async def test_p4c_http_unstaged_version_is_409_before_the_level1_gate(force_memory_backend):
    """状态机先于 Level-1 闸门：未经联锁 staged 的版本 ⇒ 409（不是 500/200）。"""
    repo, tenant, skill = await _seed("qa-skill-p4c")
    await repo.add_version(
        tenant, SkillVersionRecord(tenant_id=tenant, skill_id=skill.id, semver="9.9.9")
    )
    authenticate_business_app(app)
    client = TestClient(app)
    resp = client.post(
        f"/skills/{skill.id}/versions/9.9.9/approve-publish",
        json={},
        headers={"Authorization": f"Bearer {_manager_token()}"},
    )
    assert resp.status_code == 409, f"{resp.status_code} {resp.text[:200]}"


async def test_p4d_http_pending_version_is_403_while_level1_closed_and_incumbent_stays(
    force_memory_backend,
):
    repo, tenant, skill = await _seed("qa-skill-p4d", incumbent_score=0.80)
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    authenticate_business_app(app)
    client = TestClient(app)
    resp = client.post(
        f"/skills/{skill.id}/versions/{staged.semver}/approve-publish",
        json={},
        headers={"Authorization": f"Bearer {_manager_token()}"},
    )
    assert resp.status_code == 403, f"{resp.status_code} {resp.text[:200]}"
    assert "Level-1" in resp.json()["detail"]
    assert (await repo.get_skill(tenant, skill.id)).current_version == "1.0.0"


# --------------------------------------------------------------------------- #
# P5 — RBAC 角色矩阵                                                           #
# --------------------------------------------------------------------------- #
def test_p5_rbac_role_matrix_on_approve_publish(force_memory_backend):
    authenticate_business_app(app)
    client = TestClient(app)
    url = "/skills/qa-nope/versions/1.1.0/approve-publish"

    assert client.post(url, json={}).status_code == 401, "未认证必须 401"

    for role in ("viewer", "sales_rep", "service"):
        token = create_access_token(user_id=f"qa-{role}", role=role)
        resp = client.post(url, json={}, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403, f"{role} => {resp.status_code} {resp.text[:160]}"

    # 阳性对照：持有 approve:skills 的角色必须穿过角色闸门（未知 skill ⇒ 404）
    for role in ("manager", "admin"):
        token = create_access_token(user_id=f"qa-{role}", role=role)
        resp = client.post(url, json={}, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 404, f"{role} => {resp.status_code} {resp.text[:160]}"


def test_p5b_interlock_read_endpoint_permission_semantics(force_memory_backend):
    authenticate_business_app(app)
    client = TestClient(app)
    assert client.get("/evolution/interlock").status_code == 401

    for role in ("viewer", "manager"):
        token = create_access_token(user_id=f"qa-{role}", role=role)
        resp = client.get(
            "/evolution/interlock", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, f"{role} 只读联锁状态应可读（read:skills）"
        assert resp.json()["released"] is False


# --------------------------------------------------------------------------- #
# P6 — 跨租户（HTTP 全栈）                                                      #
# --------------------------------------------------------------------------- #
async def test_p6_cross_tenant_http_approval_is_404_and_changes_nothing(
    force_memory_backend,
):
    repo, tenant, skill = await _seed("qa-skill-p6", incumbent_score=0.80)
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    authenticate_business_app(app)
    client = TestClient(app)
    url = f"/skills/{skill.id}/versions/{staged.semver}/approve-publish"

    attacker = _manager_token(workspace=TENANT_B, user_id="qa-m-b")
    resp = client.post(url, json={}, headers={"Authorization": f"Bearer {attacker}"})
    assert resp.status_code == 404, f"跨租户必须 404，实际 {resp.status_code} {resp.text[:160]}"
    assert (await repo.get_skill(tenant, skill.id)).current_version == "1.0.0"
    assert await pi.list_decisions(TENANT_B, skill.id, staged.semver) == []

    # 阳性对照：同租户 manager 能到达状态机（pending + Level-1 关闭 ⇒ 403 而非 404）
    owner = _manager_token(user_id="qa-m-a")
    resp2 = client.post(url, json={}, headers={"Authorization": f"Bearer {owner}"})
    assert resp2.status_code == 403, f"同租户应到达业务闸门，实际 {resp2.status_code}"


# --------------------------------------------------------------------------- #
# P7 — 探针形状 / 诚实性                                                        #
# --------------------------------------------------------------------------- #
def _anchor(req: str) -> pi.CapabilityAnchor:
    return next(a for a in pi.REQUIREMENTS if a.requirement == req)


def test_p7_probe_shape_variants_fail_closed_and_evidence_is_never_blank(monkeypatch):
    # (a) 非 dict 返回值 ⇒ 未满足
    mod = types.ModuleType("forgeflow.skills.auto_rollback")
    mod.INTERLOCK_PROBE = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "forgeflow.skills.auto_rollback", mod)
    st = pi._probe_capability(_anchor("R7"))
    assert st.met is False and st.evidence.strip(), st

    # (b) INTERLOCK_PROBE 非 callable ⇒ 未满足
    mod2 = types.ModuleType("forgeflow.evaluation.effect_benchmarks")
    mod2.INTERLOCK_PROBE = {"ok": True}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "forgeflow.evaluation.effect_benchmarks", mod2)
    st2 = pi._probe_capability(_anchor("R8"))
    assert st2.met is False and "INTERLOCK_PROBE" in st2.evidence, st2

    # (c) ok=True 但无 evidence ⇒ met 但证据栏必须非空（回退说明，不得空白）
    mod3 = types.ModuleType("forgeflow.skills.candidate_gate")
    mod3.INTERLOCK_PROBE = lambda: {"ok": True}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "forgeflow.skills.candidate_gate", mod3)
    st3 = pi._probe_capability(_anchor("R1"))
    assert st3.met is True and st3.evidence.strip(), st3

    # 阳性对照：ok=True + evidence ⇒ met 且证据透传（证明本探针能变绿，非永红）
    mod4 = types.ModuleType("forgeflow.evaluation.golden_regression")
    mod4.INTERLOCK_PROBE = lambda: {"ok": True, "evidence": "golden-report#1"}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "forgeflow.evaluation.golden_regression", mod4)
    st4 = pi._probe_capability(_anchor("R4"))
    assert st4.met is True and st4.evidence == "golden-report#1", st4


def test_p7b_auto_publish_flag_alone_never_lifts_the_interlock(monkeypatch):
    monkeypatch.setattr(get_settings(), "evolution_auto_publish", True)
    status = pi.evaluate_interlock(_tenant())
    assert status.auto_publish_flag is True, "flag 必须诚实回显"
    # 核心钉子：flag 单独打开绝不解除联锁 —— 只要还有任一未满足项就锁死。
    assert status.released is False, "仅开 flag（能力未落地）不得解除联锁"
    assert pi.auto_publish_permitted(_tenant()) is False
    # 载荷断言①（非恒真）：R1 已由 T08 真实满足（锚点 forgeflow.skills.candidate_gate），
    # 不得出现在缺失集 —— 钉住“真实锚点被真实探针识别为 met”。
    assert "R1" not in status.missing, status.missing
    # 载荷断言②（非恒真，**含时点依赖**）：只要还有任一未满足项，flag 单独打开也不得放行。
    # 注意：该断言变红 ⇒ 说明 R2–R8 有真实能力落地，属联锁语义变化信号，须人工复核，**非回归**。
    assert status.missing, "联锁必须至少有一个未满足项才不算被解除"
    # 自洽性检查（关系式，**非独立回归证据**）：missing 是 @property，其定义恰为
    # ``[r.requirement for r in requirements if not r.met]``，故本等式对同一对象**恒真**，
    # 无法变红；保留它只为钉住“接口形状”（missing 仍是未满足项的投影）。
    # 相对硬编码 [R2..R8] 的好处：T13/T16/T17/T32/T33 每落地一项都不会误红本钉子。
    expected_missing = [r.requirement for r in status.requirements if not r.met]
    assert status.missing == expected_missing, status.missing


def test_probe_fail_closed_when_capability_module_missing(monkeypatch):
    """【阳性对照·fail-closed】锚点指向**确实不存在**的模块 ⇒ 必须视为未满足。

    三条断言各带一个方向，保证探针既能变红也能变绿（非恒真/恒假）：
    1) 缺失模块 ⇒ ``_probe_capability`` 判 ``met=False`` 且证据非空；
    2) 真实态 fail-closed：R2–R8 能力模块确实缺失 ⇒ 锁死（用真实
       ``REQUIREMENTS``，**不缩集**、不改 ``missing``/``released``）；
    3) 正向对照：为**全部 8 个锚点**注入 ``ok=True`` 的 ``INTERLOCK_PROBE`` **stub**
       （模拟“全满足态”，≠ 真实全满足态）⇒ ``missing==[]`` ⇒ ``released=True``
       （证明放行逻辑存在，第 2 条不是恒锁）。
    """
    # (1) 锚点指向确实不存在的模块 ⇒ 未满足。
    ghost = pi.CapabilityAnchor(
        "RX", "T99", "forgeflow.__qa_definitely_absent_probe__", "QA 缺失模块对照"
    )
    st = pi._probe_capability(ghost)
    assert st.met is False, f"缺失模块不得判 met：{st}"
    assert st.evidence.strip(), "缺失时证据栏不得为空（无证据必须显式注明）"

    # (2) 真实态 fail-closed：真实 R2–R8 模块缺失 ⇒ 锁死（不缩集）。
    monkeypatch.setattr(get_settings(), "evolution_auto_publish", True)
    real = pi.evaluate_interlock(_tenant())
    assert real.released is False
    assert {"R2", "R3", "R4", "R5", "R6", "R7", "R8"} <= set(real.missing), real.missing
    assert pi.auto_publish_permitted(_tenant()) is False

    # [局限] 下面注入 stub 模拟的“全满足态” ≠ 真实全满足态（真实 R2–R8 未落地，造不出 8/8）。
    # (3) 正向对照：为全部 8 个锚点注入满足的 INTERLOCK_PROBE ⇒ 全满足态 ⇒ 放行。
    for anchor in pi.REQUIREMENTS:
        stub = types.ModuleType(anchor.module)
        stub.INTERLOCK_PROBE = (
            lambda _m=anchor.module: {"ok": True, "evidence": f"qa-stub:{_m}"}
        )
        monkeypatch.setitem(sys.modules, anchor.module, stub)
    full = pi.evaluate_interlock(_tenant())
    assert full.missing == [], full.missing
    assert full.released is True, "全满足 + flag 应放行（正向对照）"


# --------------------------------------------------------------------------- #
# P8 — incumbent 保护                                                          #
# --------------------------------------------------------------------------- #
async def test_p8a_candidate_without_any_evaluation_must_not_be_publishable(
    force_memory_backend, monkeypatch
):
    """评估缺失 ⇒ 无任何回归证据，批准必须被拒绝且 incumbent 不动。"""
    repo, tenant, skill = await _seed("qa-skill-p8a", incumbent_score=0.80)
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)  # Level-1 开放
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=None, skill_repo=repo
    )
    assert staged.eval_score is None, "前提：候选确实无评估分数"

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            tenant, skill.id, staged.semver,
            approver="qa-m1", approver_role="manager", skill_repo=repo,
        )
    assert exc.value.status_code == 403, "评估缺失的候选不得被批准发布"
    assert (await repo.get_skill(tenant, skill.id)).current_version == "1.0.0"


async def test_p8b_passing_candidate_publishes_the_same_path_as_positive_control(
    force_memory_backend, monkeypatch
):
    repo, tenant, skill = await _seed("qa-skill-p8b", incumbent_score=0.80)
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    result = await pi.approve_publish(
        tenant, skill.id, staged.semver,
        approver="qa-m1", approver_role="manager", skill_repo=repo,
    )
    assert result["state"] == "published"
    assert result["regression"]["allowed"] is True
    assert (await repo.get_skill(tenant, skill.id)).current_version == staged.semver


async def test_p8c_small_slip_inside_the_warn_band_publishes_and_is_recorded(
    force_memory_backend, monkeypatch
):
    """0.80 → 0.77（-0.03）落在 warn 带（>0.02, <0.05）⇒ 放行但 severity=warning。"""
    repo, tenant, skill = await _seed("qa-skill-p8c", incumbent_score=0.80)
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.77, skill_repo=repo
    )
    result = await pi.approve_publish(
        tenant, skill.id, staged.semver,
        approver="qa-m1", approver_role="manager", skill_repo=repo,
    )
    assert result["state"] == "published"
    assert result["regression"]["severity"] == "warning", result["regression"]


async def test_p8d_missing_incumbent_row_reports_no_baseline_honestly(
    force_memory_backend, monkeypatch
):
    """incumbent 行缺失 ⇒ 无基线；报告必须是 no_baseline（诚实），不是伪造的 ok。"""
    repo, tenant, skill = await _seed("qa-skill-p8d", add_incumbent_row=False)
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    result = await pi.approve_publish(
        tenant, skill.id, staged.semver,
        approver="qa-m1", approver_role="manager", skill_repo=repo,
    )
    assert result["regression"]["severity"] == "no_baseline", result["regression"]
    assert result["regression"]["baseline_present"] is False, result["regression"]


class _LoopSkillRepo:
    """Minimal tenant-scoped skill repo for the production-entry probe."""

    def __init__(self, skill: SkillRecord, incumbent: SkillVersionRecord) -> None:
        self.skill = skill
        self.versions = [incumbent]

    async def get_skill(self, tenant, skill_id):
        if self.skill.tenant_id != tenant or self.skill.id != skill_id:
            return None
        return self.skill

    async def get_version(self, tenant, skill_id, semver):
        for version in self.versions:
            if (
                version.tenant_id == tenant
                and version.skill_id == skill_id
                and version.semver == semver
            ):
                return version
        return None

    async def list_versions(self, tenant, skill_id):
        return [
            v for v in self.versions if v.tenant_id == tenant and v.skill_id == skill_id
        ]

    async def add_version(self, tenant, version):
        self.versions.append(version)
        return version

    async def update_skill(self, skill):
        self.skill = skill
        return skill


class _LoopCandidateRepo:
    def __init__(self, metrics: dict) -> None:
        self.saved: list = []
        self.evaluation = SimpleNamespace(metrics=dict(metrics), verdict="pass")

    async def save_candidate(self, candidate):
        self.saved.append(candidate)
        return candidate

    async def get_evaluation_for(self, *args):
        return self.evaluation


class _LoopExperienceRepo:
    def __init__(self) -> None:
        self.saved: list = []

    async def save(self, record):
        self.saved.append(record)
        return record


class _LoopPolicyRepo:
    async def save_approval(self, record):
        return record


async def test_p8a2_evolution_loop_must_not_stage_an_unscored_candidate(
    monkeypatch,
):
    """生产入口（Round 2 修订）：候选评估缺 score ⇒ **不得**进入可批准态。

    Round 1 时本用例是「特征化缺陷」探针（断言缺陷行为为真）。B1/B3 修复后按任务书
    语义翻转为规格探针：无评估 ⇒ 不生成待批准版本、不得称「回归通过」、incumbent 不动。
    """
    import forgeflow.skills.candidate_compiler as cc
    import forgeflow.skills.engineering as eng

    tenant = _tenant()
    skill = SkillRecord(
        id="qa-skill-p8a2",
        tenant_id=tenant,
        name="无评估候选",
        current_version="1.0.0",
        status="published",
    )
    incumbent = SkillVersionRecord(
        tenant_id=tenant, skill_id=skill.id, semver="1.0.0", eval_score=0.80
    )
    skill_repo = _LoopSkillRepo(skill, incumbent)
    cand_repo = _LoopCandidateRepo(metrics={})  # ← 评估记录存在，但没有任何可比指标
    exp_repo = _LoopExperienceRepo()
    pol_repo = _LoopPolicyRepo()

    async def _many(t, *, skill_tools, window_days=30):
        return [
            {"run_id": f"qa-run-{i}", "modes": ["tool_error"], "steps": []}
            for i in range(EVOLVE_TRIGGER_MIN_FAILURES + 1)
        ]

    async def _compile(t, ids, mode, **kwargs):
        return SimpleNamespace(
            id="qa-cand-p8a2",
            status="compiled",
            name=skill.name,
            domain="general",
            draft_spec={"prompt": "改进"},
            experience_ids=list(ids),
        )

    async def _engineering(*args, **kwargs):
        return SimpleNamespace(passed=True, degraded_reason=None)

    monkeypatch.setattr(el, "collect_skill_failures", _many)
    monkeypatch.setattr(cc, "compile_candidate", _compile)
    monkeypatch.setattr(eng, "run_engineering_loop", _engineering)

    out = await maybe_evolve(
        tenant, skill.id, actor="qa",
        skill_repo=skill_repo, candidate_repo=cand_repo,
        experience_repo=exp_repo, policy_repo=pol_repo,
    )
    assert out.publish_state != "pending_approval", (
        f"无评估候选进入了可批准态：publish_state={out.publish_state!r}"
    )
    assert out.to_version is None, f"无评估候选仍铸造了版本 {out.to_version!r}"
    assert "回归通过" not in out.reason, f"未做比较却称回归通过：{out.reason!r}"
    assert out.applied is False
    assert (await skill_repo.get_version(tenant, skill.id, "1.1.0")) is None, (
        "无评估候选不得落任何待批准版本"
    )
    assert str(skill_repo.skill.current_version) == "1.0.0", "incumbent 必须保留"


async def test_p8e_uncomparable_baseline_is_refused_by_the_second_gate(
    force_memory_backend, monkeypatch
):
    """B1 闸门②独立探针：incumbent 有基线但无可比指标 ⇒ 403（证明该闸门是活的）。"""
    import forgeflow.skills.release_gate as rg

    repo, tenant, skill = await _seed("qa-skill-p8e", incumbent_score=0.80)
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    # 注入一个与候选("score")不重叠的基线 ⇒ evaluate_release 报 no_baseline 但基线存在
    monkeypatch.setattr(rg, "baseline_from_version", lambda incumbent: {"hallucination_rate": 0.1})
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            tenant, skill.id, staged.semver,
            approver="qa-m1", approver_role="manager", skill_repo=repo,
        )
    assert exc.value.status_code == 403, "无可比指标的候选不得发布"
    assert (await repo.get_skill(tenant, skill.id)).current_version == "1.0.0"
    assert await pi.latest_decision(tenant, skill.id, staged.semver) == "rejected"


async def test_p11_staging_never_mints_a_semver_below_the_incumbent(
    force_memory_backend,
):
    """边界：current_version 指向缺失行（异常/迁移遗留）时，暂存不得铸造低于 incumbent 的 semver。"""
    from forgeflow.skills.versioning import parse_semver

    repo, tenant, skill = await _seed("qa-skill-p11", add_incumbent_row=False)
    skill.current_version = "2.0.0"  # 指针前移，但 2.0.0 行不存在
    await repo.update_skill(skill)
    await repo.add_version(
        tenant,
        SkillVersionRecord(
            tenant_id=tenant, skill_id=skill.id, semver="1.0.0", eval_score=0.80
        ),
    )

    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    # 加固后 base = max(现存 1.0.0 ∪ {current 2.0.0}) = 2.0.0 ⇒ minor bump = 2.1.0。
    assert parse_semver(staged.semver) > parse_semver("2.0.0"), (
        f"暂存 semver {staged.semver} 不高于 incumbent 2.0.0 ⇒ 批准会把 current_version 回退"
    )
    assert staged.semver == "2.1.0", f"期望 2.1.0（max(current,现存)+minor），实得 {staged.semver}"
    rows = [v for v in await repo.list_versions(tenant, skill.id) if v.semver == staged.semver]
    assert len(rows) == 1, "暂存不得铸造重复 semver"


# --------------------------------------------------------------------------- #
# P9 — 台账 / 入参诚实性                                                        #
# --------------------------------------------------------------------------- #
async def test_p9_illegal_decision_and_unresolved_tenant_fail_closed(force_memory_backend):
    with pytest.raises(ValueError):
        await pi.record_decision(_tenant(), "s1", "1.0.0", "u1", "maybe", None)

    with pytest.raises(GovernanceError) as exc:
        await pi.stage_pending_version(
            None,
            SkillRecord(id="qa-ghost", tenant_id=None, current_version="1.0.0"),
            _candidate(),
            actor="evolution",
            eval_score=0.5,
            skill_repo=get_skill_repository(),
        )
    assert exc.value.status_code == 403
    assert await get_skill_repository().list_versions(_tenant(), "qa-ghost") == []


async def test_p9b_decision_ledger_is_tenant_isolated(force_memory_backend):
    await pi.record_decision(_tenant(), "s1", "1.0.0", "u1", pi.DECISION_PENDING, None)
    assert len(await pi.list_decisions(_tenant(), "s1", "1.0.0")) == 1
    assert await pi.list_decisions(TENANT_B, "s1", "1.0.0") == []


# --------------------------------------------------------------------------- #
# P10 — 状态机真值表                                                           #
# --------------------------------------------------------------------------- #
def test_p10_publish_state_machine_truth_table():
    skill_current = SimpleNamespace(current_version="2.0.0")
    cases = [
        ("2.0.0", pi.DECISION_REJECTED, pi.STATE_PUBLISHED),  # 已生效优先于任何决策
        ("1.0.0", None, pi.STATE_CANDIDATE),
        ("1.0.0", pi.DECISION_PENDING, pi.STATE_PENDING_APPROVAL),
        ("1.0.0", pi.DECISION_APPROVED, pi.STATE_APPROVED),
        ("1.0.0", pi.DECISION_REJECTED, pi.STATE_REJECTED),
    ]
    for semver, latest, expected in cases:
        got = pi.publish_state_of(skill_current, SimpleNamespace(semver=semver), latest)
        assert got == expected, f"semver={semver} latest={latest} => {got}, 期望 {expected}"

    # 阳性对照：current_version 为空 ⇒ 无决策即 candidate（不得伪装成 published）
    blank = SimpleNamespace(current_version="")
    assert (
        pi.publish_state_of(blank, SimpleNamespace(semver="1.0.0"), None)
        == pi.STATE_CANDIDATE
    )


@pytest.fixture(autouse=True)
def _restore_auth_overrides():
    original = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original)
