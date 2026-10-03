"""INC46 T15 — publish interlock: fail-closed probes, staging, approval flow.

Scope (六问 ③④):

* **阳性** — 联锁未解除 + 回归通过 ⇒ ``maybe_evolve`` 只生成 ``pending_approval``
  候选版本（不发布，``applied=False``，incumbent 不动）；Level-1 开放后人工批准
  才发布，且写 ``publish_approvals`` 记录。
* **阴性** — 无权限批准 403；跨租户 403/404；批准回归未通过的候选 ⇒ 拒绝且
  incumbent 保留；R1–R8 任一缺失 ⇒ ``released=False`` 且列缺失项；Level-1 未
  开放时批准 ⇒ 显式 403。
* **fail-closed 探针** — 能力模块不存在 / 无 ``INTERLOCK_PROBE`` / 探针抛异常 /
  自报未满足，一律 = 未满足，且证据栏注明「无证据」。
* **反事实（套件内）** — 把闸门短路（``auto_publish_permitted`` ⇒ True）后，同一
  条回归通过的路径必须走向 ``published``；证明阳性断言是由闸门决定的，而不是
  vacuous。物理摘掉闸门调用的验证记录在 T15 汇报中。

Every test drives the real functions (never a re-implementation).
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.api.hub_schemas import ApprovePublishRequest
from forgeflow.api.routers.skills import approve_publish_version
from forgeflow.auth.jwt import create_access_token
from forgeflow.config import get_settings
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.models import UserContext
from forgeflow.skills import evolution_loop as el
from forgeflow.skills import publish_interlock as pi
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.evolution_loop import (
    EVOLVE_TRIGGER_MIN_FAILURES,
    maybe_evolve,
    reset_evolution_state,
)
from forgeflow.skills.models import SkillRecord, SkillVersionRecord

TENANT = "tenant-interlock-1"
SKILL_ID = "skill-interlock-1"


# --------------------------------------------------------------------------- #
# fakes — only the seams the real code actually touches                        #
# --------------------------------------------------------------------------- #
class _FakeSkillRepo:
    """Tenant-scoped fake (mirrors the memory repo's fail-closed scoping)."""

    def __init__(self, skill: SkillRecord | None, versions: list[SkillVersionRecord] | None = None):
        self.skill = skill
        self.versions = list(versions or [])

    def _scoped(self, tenant):
        if self.skill is None or self.skill.tenant_id != tenant:
            return None
        return self.skill

    async def get_skill(self, tenant, skill_id):
        skill = self._scoped(tenant)
        return skill if (skill is not None and skill.id == skill_id) else None

    async def get_skill_by_name(self, tenant, name):
        skill = self._scoped(tenant)
        return skill if (skill is not None and skill.name == name) else None

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


class _FakeCandidateRepo:
    def __init__(self) -> None:
        self.saved: list = []
        self.evaluation = SimpleNamespace(metrics={"score": 0.95}, verdict="pass")

    async def save_candidate(self, candidate):
        self.saved.append(candidate)
        return candidate

    async def get_candidate(self, *args):
        wanted = args[-1] if args else None
        for cand in self.saved:
            if getattr(cand, "id", None) == wanted:
                return cand
        return None

    async def get_evaluation_for(self, *args):
        return self.evaluation


class _FakeExperienceRepo:
    def __init__(self) -> None:
        self.saved: list = []

    async def save(self, record):
        self.saved.append(record)
        return record

    async def get(self, tenant, exp_id):
        for rec in self.saved:
            if getattr(rec, "id", None) == exp_id:
                return rec
        return None


class _FakePolicyRepo:
    async def save_approval(self, record):
        return record


def _skill(tenant: str = TENANT, current: str = "1.0.0") -> SkillRecord:
    return SkillRecord(
        id=SKILL_ID,
        tenant_id=tenant,
        name="联锁演示技能",
        current_version=current,
        status="published",
    )


def _incumbent(tenant: str, skill_id: str, semver: str = "1.0.0", score: float = 0.80):
    return SkillVersionRecord(
        tenant_id=tenant, skill_id=skill_id, semver=semver, eval_score=score
    )


def _candidate(label: str = "1") -> SimpleNamespace:
    # ``label`` 只用于区分同一测试内的多个候选（B2 重复暂存场景）；
    # 默认值保持既有调用（``_candidate()``）的行为不变。
    # 注意：draft_spec 现为**过闸门**的完整契约（prompt+steps+tools+io）——T08 R1 接线
    # （裁定 K）后，自动发布分支会在 promote 前调 ``blocks_auto_publish``。旧的
    # ``{"prompt":...,"tools":[]}`` 会因 ``procedure_missing``/``tools_missing`` 被闸门拦下。
    return SimpleNamespace(
        id=f"cand-{label}",
        status="compiled",
        name="联锁演示技能",
        domain="general",
        experience_ids=["e1", "e2", "e3"],
        draft_spec={
            "prompt": "当用户需要生成周报时使用",
            "steps": ["读取数据", "渲染周报"],
            "tools": ["report.render"],
            "io_schema": {"input": {"topic": "str"}, "output": {"path": "str"}},
        },
    )


@pytest.fixture(autouse=True)
def _clean():
    pi.reset_interlock_state()
    reset_evolution_state()
    yield
    pi.reset_interlock_state()
    reset_evolution_state()


# --- probe seams ------------------------------------------------------------ #
def _probe_all_met(anchor: pi.CapabilityAnchor) -> pi.RequirementStatus:
    return pi.RequirementStatus(
        anchor.requirement, anchor.task, anchor.description, True, f"测试证据:{anchor.task}"
    )


def _probe_level1_only(anchor: pi.CapabilityAnchor) -> pi.RequirementStatus:
    met = anchor.requirement in pi.LEVEL1_REQUIREMENTS
    return pi.RequirementStatus(
        anchor.requirement,
        anchor.task,
        anchor.description,
        met,
        f"测试证据:{anchor.task}" if met else f"无证据：{anchor.task} 未落地（测试模拟）",
    )


def _failures(n: int) -> list[dict]:
    return [
        {
            "run_id": f"run-{i}",
            "modes": ["tool_error"],
            "steps": [],
        }
        for i in range(n)
    ]


def _drive_loop_to_regression_pass(monkeypatch) -> dict:
    """Monkeypatch the heavy seams so maybe_evolve reaches a passing release gate."""
    import forgeflow.skills.candidate_compiler as cc
    import forgeflow.skills.engineering as eng

    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    async def _fake_compile(tenant, experience_ids, mode, **kwargs):
        return _candidate()

    async def _fake_engineering(*args, **kwargs):
        return SimpleNamespace(passed=True, degraded_reason=None)

    monkeypatch.setattr(el, "collect_skill_failures", _many)
    monkeypatch.setattr(cc, "compile_candidate", _fake_compile)
    monkeypatch.setattr(eng, "run_engineering_loop", _fake_engineering)

    skill = _skill()
    repos = {
        "skill_repo": _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID)]),
        "candidate_repo": _FakeCandidateRepo(),
        "experience_repo": _FakeExperienceRepo(),
        "policy_repo": _FakePolicyRepo(),
    }
    return {"skill": skill, "repos": repos}


# --------------------------------------------------------------------------- #
# A. interlock status + fail-closed probes                                     #
# --------------------------------------------------------------------------- #
def test_default_interlock_is_fully_locked_and_lists_every_missing_requirement():
    status = pi.evaluate_interlock(TENANT)

    # 时效性注释（主理人登记 TODO，留 T34/T36 收口）：本用例（及 L~612 的 HTTP 版）
    # 的 `missing`/`level1_missing` 等**时点快照**与当前实现进度耦合（R1 已 met、
    # R2–R8 未落地）。T34/T36 收口时应改为**定向构造**（monkeypatch REQUIREMENTS
    # 造确定态）以与实现进度解耦；本次保留快照为**有意的成本权衡**，非遗漏。
    #
    # INC46 T08 落地 R1（锚点 forgeflow.skills.candidate_gate，自检探针 ok）后，
    # R1 已是**真实满足**；R2–R8 仍未落地 ⇒ 联锁依旧不得解除（fail-closed）。
    assert status.released is False, "R1 已满足但 R2–R8 缺失 ⇒ 联锁仍不得解除"
    assert status.level1_open is False
    assert status.auto_publish_flag is False
    assert status.missing == [f"R{i}" for i in range(2, 9)]
    assert status.level1_missing == [f"R{i}" for i in range(2, 7)]
    payload = status.to_dict()
    assert len(payload["requirements"]) == 8
    for item in payload["requirements"]:
        met_expected = item["requirement"] == "R1"  # T08 已落地
        assert item["met"] is met_expected, item
        assert item["evidence"], "无证据必须注明，不得空证据栏"
        if not met_expected:
            assert "无证据" in item["evidence"]
        assert item["task"] and item["description"]


def test_unresolved_tenant_fails_closed_on_every_entry_point():
    with pytest.raises(GovernanceError) as exc:
        pi.evaluate_interlock(None)
    assert exc.value.status_code == 403

    with pytest.raises(GovernanceError) as exc:
        pi.auto_publish_permitted("")
    assert exc.value.status_code == 403


def test_probe_fail_closed_when_capability_module_missing():
    # R1 的锚点（forgeflow.skills.candidate_gate）已由 T08 落地；改用仍未落地的
    # R2/T13 锚点验证「模块不存在 ⇒ fail-closed 未满足」这一不变式。
    status = pi._probe_capability(pi.REQUIREMENTS[1])
    assert status.met is False
    assert "无证据" in status.evidence
    assert "T13" in status.evidence


def test_probe_met_only_when_interlock_probe_reports_ok(monkeypatch):
    module = types.ModuleType("forgeflow.skills.candidate_gate")
    module.INTERLOCK_PROBE = lambda: {"ok": True, "evidence": "golden-report#42"}
    monkeypatch.setitem(sys.modules, "forgeflow.skills.candidate_gate", module)

    status = pi._probe_capability(pi.REQUIREMENTS[0])
    assert status.met is True
    assert status.evidence == "golden-report#42"


def test_probe_fail_closed_when_probe_raises(monkeypatch):
    module = types.ModuleType("forgeflow.sandbox.real_isolation")

    def _boom():
        raise RuntimeError("sandbox probe exploded")

    module.INTERLOCK_PROBE = _boom
    monkeypatch.setitem(sys.modules, "forgeflow.sandbox.real_isolation", module)

    status = pi._probe_capability(pi.REQUIREMENTS[1])
    assert status.met is False
    assert "fail-closed" in status.evidence


def test_probe_fail_closed_when_interlock_probe_symbol_missing(monkeypatch):
    module = types.ModuleType("forgeflow.experience.outcome_labels")
    monkeypatch.setitem(sys.modules, "forgeflow.experience.outcome_labels", module)

    status = pi._probe_capability(pi.REQUIREMENTS[2])
    assert status.met is False
    assert "INTERLOCK_PROBE" in status.evidence


def test_probe_unmet_when_capability_self_reports_not_ok(monkeypatch):
    module = types.ModuleType("forgeflow.evaluation.golden_regression")
    module.INTERLOCK_PROBE = lambda: {"ok": False, "evidence": "golden 集未建"}
    monkeypatch.setitem(sys.modules, "forgeflow.evaluation.golden_regression", module)

    status = pi._probe_capability(pi.REQUIREMENTS[3])
    assert status.met is False
    assert "能力自报未满足" in status.evidence
    assert "golden 集未建" in status.evidence


def test_auto_publish_requires_flag_and_all_requirements(monkeypatch):
    settings = get_settings()

    monkeypatch.setattr(pi, "_probe_capability", _probe_all_met)
    monkeypatch.setattr(settings, "evolution_auto_publish", False)
    assert pi.auto_publish_permitted(TENANT) is False, "flag 关 ⇒ 即使 R1–R8 全满足也不自动发布"

    monkeypatch.setattr(settings, "evolution_auto_publish", True)
    assert pi.auto_publish_permitted(TENANT) is True, "flag 开 ∧ R1–R8 全满足 ⇒ Level-2 解除"

    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    assert pi.auto_publish_permitted(TENANT) is False, "R7/R8 缺失 ⇒ 联锁不解除"


# --------------------------------------------------------------------------- #
# B. maybe_evolve × interlock (阳性 + 反事实)                                   #
# --------------------------------------------------------------------------- #
async def test_regression_pass_stages_pending_approval_and_never_publishes(monkeypatch):
    """阳性：联锁未解除 + 回归通过 ⇒ pending_approval，未发布。"""
    env = _drive_loop_to_regression_pass(monkeypatch)
    skill, repos = env["skill"], env["repos"]

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)
    payload = out.to_dict()

    assert payload["triggered"] is True
    assert payload["applied"] is False, "联锁未解除 ⇒ 不得自动发布"
    assert payload["publish_state"] == "pending_approval"
    assert payload["to_version"] == "1.1.0", "候选版本已生成（bump minor）"
    assert payload["regression"]["allowed"] is True, "前提：回归确实通过"
    assert "发布联锁" in payload["reason"]

    # incumbent 保留：current_version 不动（红线 6：不覆盖历史版本）。
    assert skill.current_version == "1.0.0"

    staged = await repos["skill_repo"].get_version(TENANT, SKILL_ID, "1.1.0")
    assert staged is not None, "候选版本必须真实落库（供人工批准）"
    assert staged.release_state == "pending_approval"
    assert pi.INTERLOCK_MARKER in staged.changelog
    assert staged.approved_by is None, "未批准 ⇒ None，绝不伪造批准人"

    records = await pi.list_decisions(TENANT, SKILL_ID, "1.1.0")
    assert len(records) == 1
    assert records[0].decision == "pending"
    assert records[0].approver is None, "pending ⇒ 无批准人（未测量 ⇒ None）"
    assert records[0].reason


async def test_staging_is_idempotent_within_the_same_window(monkeypatch):
    env = _drive_loop_to_regression_pass(monkeypatch)
    repos = env["repos"]

    first = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)
    second = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)

    assert second is first, "幂等台账必须返回同一结果"
    versions = await repos["skill_repo"].list_versions(TENANT, SKILL_ID)
    staged = [v for v in versions if v.semver == "1.1.0"]
    assert len(staged) == 1, "重复触发不得重复铸造待批准版本"


async def test_gate_short_circuit_flips_the_same_path_to_published(monkeypatch):
    """反事实（套件内）：摘掉闸门 ⇒ 同一路径走向 published，证明非 vacuous。"""
    import forgeflow.skills.governance_gate as gg

    env = _drive_loop_to_regression_pass(monkeypatch)
    repos = env["repos"]

    monkeypatch.setattr(pi, "auto_publish_permitted", lambda tenant: True)

    async def _fake_promote(*args, **kwargs):
        return SkillVersionRecord(tenant_id=TENANT, skill_id=SKILL_ID, semver="1.1.0")

    monkeypatch.setattr(gg, "promote_candidate", _fake_promote)

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)
    assert out.applied is True, "闸门被短路后，回归通过 ⇒ 发布"
    assert out.publish_state == "published"
    assert out.to_version == "1.1.0"


# --------------------------------------------------------------------------- #
# C. approve_publish — Level-1 人工批准发布                                     #
# --------------------------------------------------------------------------- #
async def _stage(repo: _FakeSkillRepo, skill: SkillRecord, score: float = 0.95):
    return await pi.stage_pending_version(
        skill.tenant_id,
        skill,
        _candidate(),
        actor="evolution",
        eval_score=score,
        skill_repo=repo,
    )


async def test_approval_publishes_and_writes_the_approval_record(monkeypatch):
    """阳性后半段：Level-1 开放（R1–R6 满足）⇒ 批准后才发布且写 approval 记录。"""
    skill = _skill()
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID, score=0.80)])
    staged = await _stage(repo, skill, score=0.95)
    assert skill.current_version == "1.0.0", "批准前不得发布"

    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    # 联锁对 Level-2 仍未解除（R7/R8 缺失）——人工批准恰恰是联锁要求的通道。
    assert pi.evaluate_interlock(TENANT).released is False
    assert pi.auto_publish_permitted(TENANT) is False

    result = await pi.approve_publish(
        TENANT,
        SKILL_ID,
        staged.semver,
        approver="manager-1",
        approver_role="manager",
        reason="验收通过",
        skill_repo=repo,
    )

    assert result["state"] == "published"
    assert result["previous_version"] == "1.0.0"
    assert result["regression"]["allowed"] is True
    assert skill.current_version == staged.semver, "批准后 current_version 才切换"
    assert staged.release_state == "published"
    assert staged.approved_by == "manager-1"

    approval = result["approval"]
    assert approval["decision"] == "approved"
    assert approval["approver"] == "manager-1"
    assert approval["reason"] == "验收通过"
    assert approval["tenant_id"] == TENANT

    records = await pi.list_decisions(TENANT, SKILL_ID, staged.semver)
    assert [r.decision for r in records] == ["pending", "approved"], (
        "每次发布必须写 approval 记录，且顺序可审计"
    )


async def test_approver_without_publish_permission_is_403():
    skill = _skill()
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID)])
    staged = await _stage(repo, skill)

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            TENANT,
            SKILL_ID,
            staged.semver,
            approver="viewer-1",
            approver_role="viewer",
            skill_repo=repo,
        )
    assert exc.value.status_code == 403
    assert skill.current_version == "1.0.0", "无权限 ⇒ incumbent 保留"


async def test_cross_tenant_approval_is_404_and_changes_nothing():
    skill = _skill(tenant=TENANT)
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID)])
    staged = await _stage(repo, skill)

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            "tenant-other",
            SKILL_ID,
            staged.semver,
            approver="manager-9",
            approver_role="manager",
            skill_repo=repo,
        )
    assert exc.value.status_code == 404, "跨租户读不到 ⇒ 404（fail-closed）"
    assert skill.current_version == "1.0.0"
    # 跨租户的决定也不得写入受害租户的台账。
    assert await pi.list_decisions(TENANT, SKILL_ID, staged.semver) != []
    other = await pi.list_decisions("tenant-other", SKILL_ID, staged.semver)
    assert other == []


async def test_approving_a_regressing_candidate_is_refused_and_terminal(monkeypatch):
    """阴性：批准回归未通过的候选 ⇒ 拒绝，incumbent 保留，rejected 为终态。"""
    skill = _skill()
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID, score=0.95)])
    staged = await _stage(repo, skill, score=0.50)  # 0.50 << 0.95 ⇒ 回归

    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            TENANT,
            SKILL_ID,
            staged.semver,
            approver="manager-1",
            approver_role="manager",
            skill_repo=repo,
        )
    assert exc.value.status_code == 403
    assert "回归复核未通过" in str(exc.value)
    assert skill.current_version == "1.0.0", "拒绝 ⇒ incumbent 保留（applied=False）"

    latest = await pi.latest_decision(TENANT, SKILL_ID, staged.semver)
    assert latest == "rejected"

    with pytest.raises(GovernanceError) as exc2:
        await pi.approve_publish(
            TENANT,
            SKILL_ID,
            staged.semver,
            approver="manager-1",
            approver_role="manager",
            skill_repo=repo,
        )
    assert exc2.value.status_code == 409, "rejected 为终态，不可再批准"


async def test_approval_refused_while_level1_is_closed_and_lists_missing():
    """T08 落地 R1 后：R2–R6 未满足 ⇒ Level-1 未开放：显式 403 + 缺失项，候选保持 pending。"""
    skill = _skill()
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID)])
    staged = await _stage(repo, skill)

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            TENANT,
            SKILL_ID,
            staged.semver,
            approver="manager-1",
            approver_role="manager",
            skill_repo=repo,
        )
    assert exc.value.status_code == 403
    text = str(exc.value)
    assert "Level-1" in text
    for req in ("R2", "R6"):
        assert req in text, "缺失项必须列出"
    assert skill.current_version == "1.0.0"
    latest = await pi.latest_decision(TENANT, SKILL_ID, staged.semver)
    assert latest == "pending", "未批准 ⇒ 状态保持 pending_approval"


async def test_approving_a_published_or_unstaged_version_is_409():
    skill = _skill()
    incumbent = _incumbent(TENANT, SKILL_ID)
    repo = _FakeSkillRepo(skill, [incumbent])

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            TENANT, SKILL_ID, "1.0.0",
            approver="m", approver_role="manager", skill_repo=repo,
        )
    assert exc.value.status_code == 409, "已发布版本不可重复批准"

    ghost = SkillVersionRecord(tenant_id=TENANT, skill_id=SKILL_ID, semver="9.9.9")
    await repo.add_version(TENANT, ghost)
    with pytest.raises(GovernanceError) as exc2:
        await pi.approve_publish(
            TENANT, SKILL_ID, "9.9.9",
            approver="m", approver_role="manager", skill_repo=repo,
        )
    assert exc2.value.status_code == 409, "未经联锁 staged 的版本不在 pending_approval"


async def test_approve_publish_fails_closed_on_unresolved_tenant():
    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            None, SKILL_ID, "1.1.0", approver="m", approver_role="manager"
        )
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# D. API wiring                                                                #
# --------------------------------------------------------------------------- #
def test_interlock_routes_are_mapped_in_rbac():
    assert RBACMiddleware._resolve_permission("GET", "/evolution/interlock") == (
        "read",
        "skills",
    )
    assert RBACMiddleware._resolve_permission(
        "POST", "/skills/s1/versions/1.1.0/approve-publish"
    ) == ("write", "skills")


def test_interlock_endpoint_over_http_reports_real_unmet_state():
    client = TestClient(app)
    assert client.get("/evolution/interlock").status_code == 401

    token = create_access_token(user_id="manager-1", role="manager")
    resp = client.get("/evolution/interlock", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["released"] is False, "不得空 200 冒充已解除"
    assert body["level1_open"] is False
    assert body["auto_publish_flag"] is False
    # T08 已落地 R1；其余 R2–R8 仍缺失（真实态，非构造）。
    assert body["missing"] == [f"R{i}" for i in range(2, 9)]
    assert len(body["requirements"]) == 8
    for r in body["requirements"]:
        assert r["met"] is (r["requirement"] == "R1")
        assert r["evidence"]


def test_approve_publish_endpoint_requires_auth_and_write_grant():
    client = TestClient(app)
    url = "/skills/s1/versions/1.1.0/approve-publish"
    assert client.post(url, json={}).status_code == 401
    viewer = create_access_token(user_id="viewer-1", role="viewer")
    assert (
        client.post(url, json={}, headers={"Authorization": f"Bearer {viewer}"}).status_code
        == 403
    )


async def test_approve_publish_route_rejects_viewer_before_any_repo_access():
    with pytest.raises(HTTPException) as exc:
        await approve_publish_version(
            skill_id="s1",
            semver="1.1.0",
            request=ApprovePublishRequest(reason=None),
            user=UserContext(user_id="viewer-1", role="viewer"),
            tenant=TENANT,
        )
    assert exc.value.status_code == 403


async def test_approve_publish_route_end_to_end_on_memory_backend(
    force_memory_backend, monkeypatch
):
    """Route function + real memory repositories + interlock ledger."""
    from forgeflow.repositories import get_skill_repository
    from forgeflow.repositories.memory.skill_repo import clear_skill_store

    clear_skill_store()
    pi.reset_interlock_state()
    repo = get_skill_repository()
    tenant = get_settings().default_tenant_id
    skill = SkillRecord(
        id="skill-http-1",
        tenant_id=tenant,
        name="HTTP 联锁技能",
        current_version="1.0.0",
        status="published",
    )
    await repo.create_skill(skill)
    await repo.add_version(tenant, _incumbent(tenant, skill.id, score=0.80))
    staged = await pi.stage_pending_version(
        tenant, skill, _candidate(), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    result = await approve_publish_version(
        skill_id=skill.id,
        semver=staged.semver,
        request=ApprovePublishRequest(reason="验收通过"),
        user=UserContext(user_id="manager-1", role="manager"),
        tenant=tenant,
    )

    assert result["state"] == "published"
    assert result["approval"]["approver"] == "manager-1"
    assert result["approval"]["reason"] == "验收通过"
    reloaded = await repo.get_skill(tenant, skill.id)
    assert reloaded.current_version == staged.semver


# --------------------------------------------------------------------------- #
# E. B1/B2/B3 regression nails (INC46 T15 follow-up)                            #
# --------------------------------------------------------------------------- #
class _UniqueSkillRepo(_FakeSkillRepo):
    """Memory repo that enforces the postgres ``UNIQUE(skill_id, semver)`` rule.

    Migration 010 declares ``skill_versions UNIQUE(skill_id, semver)``; the
    in-memory store does not. This fake raises on a duplicate so the
    deterministic (no-exception) behaviour of the monotonic semver bump is
    actually exercised rather than assumed.
    """

    async def add_version(self, tenant, version):
        for existing in self.versions:
            if (
                existing.tenant_id == tenant
                and existing.skill_id == version.skill_id
                and existing.semver == version.semver
            ):
                raise ValueError(
                    "duplicate key value violates unique constraint "
                    '"skill_versions_skill_id_semver_key"'
                )
        self.versions.append(version)
        return version


def _drive_loop_with_metrics(monkeypatch, metrics: dict) -> dict:
    """Drive ``maybe_evolve`` to the release gate with a chosen candidate metric set."""
    import forgeflow.skills.candidate_compiler as cc
    import forgeflow.skills.engineering as eng

    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    async def _fake_compile(tenant, experience_ids, mode, **kwargs):
        return _candidate()

    async def _fake_engineering(*args, **kwargs):
        return SimpleNamespace(passed=True, degraded_reason=None)

    monkeypatch.setattr(el, "collect_skill_failures", _many)
    monkeypatch.setattr(cc, "compile_candidate", _fake_compile)
    monkeypatch.setattr(eng, "run_engineering_loop", _fake_engineering)

    skill = _skill()
    cand_repo = _FakeCandidateRepo()
    cand_repo.evaluation = SimpleNamespace(metrics=dict(metrics), verdict="pass")
    repos = {
        "skill_repo": _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID)]),
        "candidate_repo": cand_repo,
        "experience_repo": _FakeExperienceRepo(),
        "policy_repo": _FakePolicyRepo(),
    }
    return {"skill": skill, "repos": repos}


async def test_b1_candidate_without_evaluation_cannot_be_approved(monkeypatch):
    """① 候选无任何评估 ⇒ 批准必须 403，incumbent 保留，写 rejected（不伪造通过）。"""
    skill = _skill()
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID, score=0.80)])
    staged = await pi.stage_pending_version(
        skill.tenant_id, skill, _candidate(), actor="evolution",
        eval_score=None, skill_repo=repo,
    )
    assert staged.eval_score is None, "前提：候选确实无评估"
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            TENANT, SKILL_ID, staged.semver,
            approver="manager-1", approver_role="manager", skill_repo=repo,
        )
    assert exc.value.status_code == 403, "无评估候选不得被批准发布"
    assert "candidate has no evaluation" in str(exc.value)
    assert skill.current_version == "1.0.0", "拒绝 ⇒ incumbent 保留（applied=False）"
    decisions = [r.decision for r in await pi.list_decisions(TENANT, SKILL_ID, staged.semver)]
    assert decisions == ["pending", "rejected"], decisions


async def test_b1_incumbent_baseline_but_no_comparison_is_refused(monkeypatch):
    """② incumbent 有基线但回归结果是 no_baseline（未做比较）⇒ 拒绝，incumbent 保留。"""
    from forgeflow.skills.release_gate import ReleaseDecision

    skill = _skill()
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID, score=0.80)])
    staged = await pi.stage_pending_version(
        skill.tenant_id, skill, _candidate(), actor="evolution",
        eval_score=0.95, skill_repo=repo,
    )
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)
    # 强制「有基线但无可比指标」的诚实结论（当前数据模型下 score↔score 总可比）。
    monkeypatch.setattr(
        "forgeflow.skills.release_gate.evaluate_release",
        lambda *a, **k: ReleaseDecision(
            allowed=True, severity="no_baseline",
            reason="基线存在但无可比指标——未做比较", baseline_present=True,
        ),
    )

    with pytest.raises(GovernanceError) as exc:
        await pi.approve_publish(
            TENANT, SKILL_ID, staged.semver,
            approver="manager-1", approver_role="manager", skill_repo=repo,
        )
    assert exc.value.status_code == 403, "有基线却未做比较 ⇒ 拒绝发布"
    assert "未做比较" in str(exc.value)
    assert skill.current_version == "1.0.0"
    assert await pi.latest_decision(TENANT, SKILL_ID, staged.semver) == "rejected"


async def test_b2_restaging_after_rejection_mints_a_new_semver(monkeypatch):
    """rejected 为终态 ⇒ 再次暂存必须换新 semver，不覆盖终态（B2）。"""
    skill = _skill()
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID, score=0.95)])
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    first = await pi.stage_pending_version(
        TENANT, skill, _candidate("c1"), actor="evolution", eval_score=0.50, skill_repo=repo
    )
    with pytest.raises(GovernanceError):
        await pi.approve_publish(
            TENANT, SKILL_ID, first.semver,
            approver="manager-1", approver_role="manager", skill_repo=repo,
        )
    assert await pi.latest_decision(TENANT, SKILL_ID, first.semver) == "rejected"

    second = await pi.stage_pending_version(
        TENANT, skill, _candidate("c2"), actor="evolution", eval_score=0.99, skill_repo=repo
    )
    assert second.semver != first.semver, "rejected semver 被复用 ⇒ rejected 非终态"
    rows = [v for v in await repo.list_versions(TENANT, SKILL_ID) if v.semver == second.semver]
    assert len(rows) == 1, "重复暂存铸造了重复 semver"
    # 终态未被新 pending 覆盖
    first_decisions = [r.decision for r in await pi.list_decisions(TENANT, SKILL_ID, first.semver)]
    assert first_decisions == ["pending", "rejected"], first_decisions


async def test_b2_staging_twice_while_pending_mints_distinct_semvers(monkeypatch):
    """pending 期间再次暂存必须换新 semver（不基于未前移的 current_version）。"""
    skill = _skill()
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID, score=0.80)])
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    first = await pi.stage_pending_version(
        TENANT, skill, _candidate("c1"), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    second = await pi.stage_pending_version(
        TENANT, skill, _candidate("c2"), actor="evolution", eval_score=0.97, skill_repo=repo
    )
    assert first.semver == "1.1.0"
    assert second.semver == "1.2.0", f"第二次暂存应得 1.2.0，实得 {second.semver}"
    versions = await repo.list_versions(TENANT, SKILL_ID)
    assert [v.semver for v in versions] == ["1.0.0", "1.1.0", "1.2.0"]
    assert skill.current_version == "1.0.0", "暂存不动 current_version"


async def test_b2_staging_never_mints_a_duplicate_under_a_uniqueness_constraint(monkeypatch):
    """pg UNIQUE(skill_id, semver) 下确定性行为：重复暂存不得撞唯一约束（不靠异常兜底）。"""
    skill = _skill()
    repo = _UniqueSkillRepo(skill, [_incumbent(TENANT, SKILL_ID, score=0.80)])
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    semvers = []
    for i in range(3):
        staged = await pi.stage_pending_version(
            TENANT, skill, _candidate(f"c{i}"), actor="evolution",
            eval_score=0.95, skill_repo=repo,
        )
        semvers.append(staged.semver)
    assert semvers == ["1.1.0", "1.2.0", "1.3.0"], semvers
    assert len(set(semvers)) == 3


async def test_b2_staging_never_mints_below_an_ahead_current_version(monkeypatch):
    """B2 加固（QA p11）：current_version 指向缺失行且高于现存行最大值时，暂存不得铸出
    低于指针的 semver（否则批准会把 current_version 回退）。"""
    skill = _skill(current="2.0.0")
    # 现存版本行只有 1.0.0 —— 指针 2.0.0 指向一行**缺失**的记录（迁移遗留 / 手工改库）。
    repo = _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID, semver="1.0.0", score=0.80)])
    monkeypatch.setattr(pi, "_probe_capability", _probe_level1_only)

    staged = await pi.stage_pending_version(
        TENANT, skill, _candidate("c1"), actor="evolution", eval_score=0.95, skill_repo=repo
    )
    assert staged.semver == "2.1.0", f"不得低于指针 2.0.0，实得 {staged.semver}"

    # 批准后 current_version 只前进不回退（2.0.0 → 2.1.0）。
    result = await pi.approve_publish(
        TENANT, SKILL_ID, staged.semver,
        approver="manager-1", approver_role="manager", skill_repo=repo,
    )
    assert result["state"] == "published"
    assert skill.current_version == "2.1.0", "指针不得回退到低于原值"


async def test_b1_loop_does_not_stage_an_unscored_candidate(monkeypatch):
    """生产入口：候选无 score ⇒ 不暂存、不进入可批准态、incumbent 不动（B1 暂存侧）。"""
    env = _drive_loop_with_metrics(monkeypatch, {})  # 评估记录存在，但无可比指标
    skill, repos = env["skill"], env["repos"]

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)

    assert out.triggered is True
    assert out.publish_state != "pending_approval", "无评估候选不得进入可批准态"
    assert out.applied is False
    assert out.to_version is None, "无评估候选不得铸造待批准版本"
    assert "回归通过" not in out.reason, "未做比较不得表述为通过（B3）"
    assert "未做" in out.reason or "无评估" in out.reason
    assert skill.current_version == "1.0.0"
    versions = await repos["skill_repo"].list_versions(TENANT, SKILL_ID)
    assert [v.semver for v in versions] == ["1.0.0"], "不得新增版本行"


def test_b3_staging_reason_matches_severity():
    """B3：reason 措辞必须与真实 severity 一致，未做比较不得称「回归通过」。"""
    from forgeflow.skills.evolution_loop import _staging_reason

    ok = _staging_reason("1.1.0", SimpleNamespace(severity="ok", baseline_present=True))
    assert "回归通过" in ok

    warn = _staging_reason("1.1.0", SimpleNamespace(severity="warning", baseline_present=True))
    assert "下滑" in warn and "回归通过" not in warn

    no_base = _staging_reason(
        "1.1.0", SimpleNamespace(severity="no_baseline", baseline_present=False)
    )
    assert "回归通过" not in no_base and "未做比较" in no_base

    no_cmp = _staging_reason(
        "1.1.0", SimpleNamespace(severity="no_baseline", baseline_present=True)
    )
    assert "回归通过" not in no_cmp and "未做比较" in no_cmp


# --------------------------------------------------------------------------- #
# F. T08 DANGEROUS 端到端钉子（裁定 C）+ 真根反事实（裁定 R8 三连锁口径）          #
# --------------------------------------------------------------------------- #
def _dangerous_candidate_contract():
    """A money-movement skill that declares low risk — the DANGEROUS trigger.

    ``payment.transfer`` is in ``TOOL_PERMISSION_MAP`` (⇒ ``DANGEROUS``) **and**
    in the platform whitelist (``allowed_tool_set``), so the only high finding is
    ``dangerous_operation`` — the root-bypass counterfactual below therefore flips
    *all three* chains, not just one.
    """
    from forgeflow.skills.contracts import SkillContract

    return SkillContract(
        goal="转账付款",
        procedure=["读取金额", "发起转账"],
        tools=["payment.transfer"],
        verification=["失败则回滚"],
        risk_level="low",
    )


def test_dangerous_candidate_gate_blocks_at_the_part_level():
    """零件级（裁定 C 改写）：含 DANGEROUS 工具的候选，在 R1=met 当前态下闸门阻断自动发布。

    本用例**只驱动零件**（``evaluate_candidate_gate`` / ``blocks_auto_publish`` /
    ``INTERLOCK_PROBE``），不 monkeypatch 任何探针 —— 它们按生产代码真实执行。
    真·端到端（驱动 ``maybe_evolve`` 真实发布流程）见下方
    ``test_dangerous_candidate_nail_is_blocked_end_to_end_through_the_real_publish_interlock``；
    该 e2e 钉子的转红反事实见 ``..._removing_the_gate_wiring_turns_the_dangerous_nail_red``。
    """
    from forgeflow.skills import candidate_gate as anchor
    from forgeflow.skills import risk_escalation

    dangerous = _dangerous_candidate_contract()

    # (1) 真实发布联锁：R1 由 T08 的 DANGEROUS 闸门**真实**满足（探针未经 monkeypatch）。
    status = pi.evaluate_interlock(TENANT)
    r1 = next(r for r in status.requirements if r.requirement == "R1")
    assert r1.met is True, "R1 = candidate_gate 自检探针，必须真实 met"
    assert r1.evidence.strip(), "证据栏不得为空"
    probe = pi._probe_capability(pi.REQUIREMENTS[0])  # 真实调用 candidate_gate.INTERLOCK_PROBE()
    assert probe.met is True and probe.evidence

    # (2) 真实发布闸门：自动发布仍不得解除（R2–R8 未落地，fail-closed）。
    assert pi.auto_publish_permitted(TENANT) is False, "R2–R8 未落地 ⇒ 自动发布不得解除"
    assert status.released is False

    # (3) 零件级：DANGEROUS 候选被闸门命名阻断，且不得自动发布。
    gate = anchor.evaluate_candidate_gate(dangerous)
    assert gate.allowed is False, "DANGEROUS 候选必须被阻断"
    assert "dangerous_operation" in gate.blocking_codes
    assert gate.risk_level == "high"
    assert anchor.blocks_auto_publish(dangerous) is True
    assert risk_escalation.blocks_auto_publish(dangerous) is True


def _dangerous_candidate(label: str = "danger") -> SimpleNamespace:
    """A compiler-shaped DANGEROUS candidate — ``draft_spec`` 声明 ``payment.transfer``.

    与 :func:`_dangerous_candidate_contract` **同源**：``payment.transfer`` 位于
    ``TOOL_PERMISSION_MAP``（⇒ ``DANGEROUS``）**且**在平台工具白名单内，故闸门中
    唯一的 ``high`` 阻断项就是 ``dangerous_operation``。``draft_spec`` 采用「过闸门」
    所需的完整形态（prompt + steps + tools + io），使危险工具成为闸门触发的**真正**
    原因——若闸门接线缺失，本候选会被直接 promote（假绿暴露点）。
    """
    return SimpleNamespace(
        id=f"cand-{label}",
        status="compiled",
        name="联锁演示技能",
        domain="general",
        experience_ids=["e1", "e2", "e3"],
        draft_spec={
            "prompt": "转账付款",
            "steps": ["读取金额", "发起转账"],
            "tools": ["payment.transfer"],
            "io_schema": {"input": {"amount": "str"}, "output": {"txn_id": "str"}},
        },
    )


def _drive_loop_with_dangerous_candidate(monkeypatch) -> dict:
    """驱动 ``maybe_evolve`` 到**发布闸门**，候选含 DANGEROUS 工具（``payment.transfer``）。

    与 :func:`_drive_loop_to_regression_pass` **同一姿势**（同一条真实 ``maybe_evolve``
    路径），唯一差异是候选的 ``draft_spec`` 声明危险工具。回归与工程闭环均被 mock 为
    通过，故**若闸门接线缺失**（且 ``auto_publish_permitted`` 被放开），该候选会被真实
    promote——这正是端到端钉子要拦住的行为。
    """
    import forgeflow.skills.candidate_compiler as cc
    import forgeflow.skills.engineering as eng

    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    async def _fake_compile(tenant, experience_ids, mode, **kwargs):
        return _dangerous_candidate()

    async def _fake_engineering(*args, **kwargs):
        return SimpleNamespace(passed=True, degraded_reason=None)

    monkeypatch.setattr(el, "collect_skill_failures", _many)
    monkeypatch.setattr(cc, "compile_candidate", _fake_compile)
    monkeypatch.setattr(eng, "run_engineering_loop", _fake_engineering)

    skill = _skill()
    repos = {
        "skill_repo": _FakeSkillRepo(skill, [_incumbent(TENANT, SKILL_ID)]),
        "candidate_repo": _FakeCandidateRepo(),
        "experience_repo": _FakeExperienceRepo(),
        "policy_repo": _FakePolicyRepo(),
    }
    return {"skill": skill, "repos": repos}


def _dangerous_nail_assertions(out, skill) -> None:
    """端到端钉子的**核心断言**（钉子与反事实共用；反事实下必须真转红）。

    ``applied is False`` 是首条断言，也是「接线被撤 ⇒ 候选被 promote」时**最先**转红的
    那一条——反事实用例据此把转红点精确定位到它。
    """
    assert out.applied is False, (
        "DANGEROUS 候选被 promote（applied=True）——候选闸门接线失效，"
        "假绿：auto_publish 放开后危险候选会被自动发布"
    )
    assert out.publish_state == pi.STATE_PENDING_APPROVAL, (
        f"DANGEROUS 候选应暂存 pending_approval，实得 {out.publish_state!r}"
    )
    assert skill.current_version == "1.0.0", "incumbent 不得被 DANGEROUS 候选覆盖"


async def test_dangerous_candidate_nail_is_blocked_end_to_end_through_the_real_publish_interlock(
    monkeypatch,
):
    """裁定 C **真·端到端**钉子：DANGEROUS 候选走**真实** ``maybe_evolve`` 发布流程被挡住。

    与零件级用例的区别：本用例驱动生产发布入口 ``evolution_loop.maybe_evolve``
    （``evolution_loop.py:576``），候选经编译 → 工程闭环 → 回归 → **闸门（:779）**
    → 暂存分支（:782）——即闸门接线的**执行点**，而非直接调用闸门零件。

    假绿暴露点：``publish_interlock.auto_publish_permitted`` 被 monkeypatch 为 ``True``
    （模拟 M1 完成后 Level-2 已放开）。此时**唯一**阻止该 DANGEROUS 候选被 promote 的
    就是 :data:`candidate_gates.blocks_auto_publish` 接线；撤掉它 ⇒ 候选被 promote
    （见反事实 ``..._removing_the_gate_wiring_turns_the_dangerous_nail_red``）。
    """
    env = _drive_loop_with_dangerous_candidate(monkeypatch)
    skill, repos = env["skill"], env["repos"]

    # 模拟 M1 完成后 Level-2 已放开（否则联锁本身就会挡住，测不到闸门接线）。
    monkeypatch.setattr(pi, "auto_publish_permitted", lambda tenant: True)

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)
    payload = out.to_dict()

    # (a) 真·端到端：DANGEROUS 候选在真实发布路径中被挡住（未 promote）。
    _dangerous_nail_assertions(out, skill)

    # (b) 证据：确经真实发布入口（triggered=True）且未自动发布；被暂存为待批准版本。
    assert payload["triggered"] is True, "必须真的走到 evolution_loop 的发布分支"
    assert payload["to_version"] == "1.1.0", "DANGEROUS 候选应被暂存为待批准版本"
    assert "闸门" in payload["reason"], f"reason 须指名候选闸门阻断，实得：{payload['reason']}"

    staged = await repos["skill_repo"].get_version(TENANT, SKILL_ID, "1.1.0")
    assert staged is not None, "待批准版本必须真实落库（供人工批准）"
    assert staged.release_state == "pending_approval"
    assert pi.INTERLOCK_MARKER in (staged.changelog or "")
    assert staged.approved_by is None, "未批准 ⇒ None，绝不伪造批准人"

    # (c) incumbent 保留（红线 6：不覆盖历史版本）。
    assert skill.current_version == "1.0.0"


async def test_counterfactual_removing_the_gate_wiring_turns_the_dangerous_nail_red(
    monkeypatch,
):
    """裁定 C 端到端**反事实（真跑）**：撤掉闸门接线 ⇒ 同一钉子的断言真转红。

    接线 = ``evolution_loop.py:779`` 的 ``candidate_gates.blocks_auto_publish(...)``。
    ``auto_publish_permitted`` 仍为 ``True``（Level-2 放开态），仅旁路闸门接线，
    复用**与钉子完全相同**的断言函数 :func:`_dangerous_nail_assertions`：

    * 接线在 ⇒ 该断言成立（绿）；
    * 接线撤 ⇒ 候选被真实 promote ⇒ 首条断言 ``out.applied is False`` **真转红**
      （被 ``pytest.raises(AssertionError)`` 捕获，转红点即该断言）。
    """
    import forgeflow.skills.candidate_gates as candidate_gates
    import forgeflow.skills.governance_gate as gg

    env = _drive_loop_with_dangerous_candidate(monkeypatch)
    skill, repos = env["skill"], env["repos"]

    monkeypatch.setattr(pi, "auto_publish_permitted", lambda tenant: True)
    # 撤接线：旁路闸门调用（保持 auto_publish_permitted=True）。
    monkeypatch.setattr(candidate_gates, "blocks_auto_publish", lambda candidate: False)
    # promote 的其余前置（trust baseline / policy）与本次反事实无关，替换为固定成功，
    # 使「接线」成为唯一变量（同 test_gate_short_circuit_flips_the_same_path_to_published）。
    async def _fake_promote(*args, **kwargs):
        return SkillVersionRecord(tenant_id=TENANT, skill_id=SKILL_ID, semver="1.1.0")

    monkeypatch.setattr(gg, "promote_candidate", _fake_promote)

    out = await maybe_evolve(TENANT, SKILL_ID, actor="u1", **repos)

    # (1) 接线撤掉后：候选被 promote（applied=True）——即钉子断言应转红的根因。
    assert out.applied is True, "撤接线后 DANGEROUS 候选应被 promote（证明钉子非 vacuous）"
    assert out.publish_state == "published"

    # (2) 复用**钉子同名断言**：现在必须真抛 AssertionError，且转红点是首条
    #     ``out.applied is False``（无断言变红 ⇒ 反事实不成立）。
    with pytest.raises(AssertionError) as exc:
        _dangerous_nail_assertions(out, skill)
    assert "被 promote" in str(exc.value), (
        f"转红点必须是钉子首条断言 `out.applied is False`，实得：{exc.value}"
    )


def test_counterfactual_bypassing_the_classify_tool_root_turns_the_dangerous_nail_red(monkeypatch):
    """裁定 R8 三连锁口径（真根反事实，真跑）：旁路唯一根 ``tool_permissions.classify_tool``
    的 DANGEROUS 判定 ⇒ 链 A/B/C **同时**转红；恢复 ⇒ 复绿。

    只摘 critic（M1）只翻链 A，不是三连锁（见 ``tests/unit/test_inc46_candidate_gates.py``
    的同名反事实，仅作纵深防御补充证据）。DANGEROUS 检测的两个消费者
    （``critic._privilege_findings`` 与 ``risk_escalation``）共享上游 ``classify_tool``，
    故只有旁路该根才能让三条链同时翻。
    """
    from forgeflow.skills import candidate_gate as anchor
    from forgeflow.skills import critic, risk_escalation, tool_permissions

    dangerous = _dangerous_candidate_contract()

    # (0) 阳性基线：三链同时为正 + 端到端钉子为绿。
    assert "dangerous_operation" in [f["code"] for f in critic.critique(dangerous).findings]  # 链 A
    assert risk_escalation.effective_risk_level(dangerous) == "high"                          # 链 B
    assert anchor.evaluate_candidate_gate(dangerous).blocks_auto_publish is True              # 链 C
    assert anchor.INTERLOCK_PROBE()["ok"] is True
    assert pi._probe_capability(pi.REQUIREMENTS[0]).met is True

    # (1) 反事实：旁路唯一根 —— classify_tool 永不返回 DANGEROUS。
    _real = tool_permissions.classify_tool

    def _never_dangerous(tool: str) -> str:
        cls = _real(tool)
        return tool_permissions.READ if cls == tool_permissions.DANGEROUS else cls

    monkeypatch.setattr(tool_permissions, "classify_tool", _never_dangerous)

    # (2a) 链 A 转红：critic 不再命名 dangerous_operation。
    assert "dangerous_operation" not in [f["code"] for f in critic.critique(dangerous).findings]
    # (2b) 链 B 转红：风险不再被抬到 high。
    assert risk_escalation.effective_risk_level(dangerous) != "high"
    # (2c) 链 C 转红：闸门放行 + R1 自检探针自报未满足。
    mutated = anchor.evaluate_candidate_gate(dangerous)
    assert mutated.allowed is True and mutated.blocks_auto_publish is False
    assert anchor.INTERLOCK_PROBE()["ok"] is False
    # 端到端钉子转红：真实发布联锁的 R1 由 met 翻为 unmet。
    assert pi._probe_capability(pi.REQUIREMENTS[0]).met is False

    # (3) 恢复 ⇒ 复绿（三链复原 + R1 复原）。
    monkeypatch.undo()
    assert anchor.INTERLOCK_PROBE()["ok"] is True
    assert pi._probe_capability(pi.REQUIREMENTS[0]).met is True
    assert risk_escalation.effective_risk_level(dangerous) == "high"
