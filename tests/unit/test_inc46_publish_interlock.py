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


def _candidate() -> SimpleNamespace:
    return SimpleNamespace(
        id="cand-1",
        status="compiled",
        name="联锁演示技能",
        domain="general",
        experience_ids=["e1", "e2", "e3"],
        draft_spec={"prompt": "改进后的提示词", "tools": []},
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

    assert status.released is False, "R1–R8 全部未满足 ⇒ 联锁不得解除"
    assert status.level1_open is False
    assert status.auto_publish_flag is False
    assert status.missing == [f"R{i}" for i in range(1, 9)]
    assert status.level1_missing == [f"R{i}" for i in range(1, 7)]
    payload = status.to_dict()
    assert len(payload["requirements"]) == 8
    for item in payload["requirements"]:
        assert item["met"] is False
        assert item["evidence"], "无证据必须注明，不得空证据栏"
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
    status = pi._probe_capability(pi.REQUIREMENTS[0])
    assert status.met is False
    assert "无证据" in status.evidence
    assert "T08" in status.evidence


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
    """R1–R6 未满足 ⇒ Level-1 未开放：显式 403 + 缺失项，候选保持 pending。"""
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
    for req in ("R1", "R6"):
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
    assert body["missing"] == [f"R{i}" for i in range(1, 9)]
    assert len(body["requirements"]) == 8
    assert all(r["met"] is False for r in body["requirements"])
    assert all(r["evidence"] for r in body["requirements"])


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
