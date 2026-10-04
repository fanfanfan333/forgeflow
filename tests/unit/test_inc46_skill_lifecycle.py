"""INC46 T35 — Skill 生命周期治理（去重 / 合并 / 冲突 / 淘汰）。

范围（任务书 T35 §测试）
------------------------
* **阳性**：构造两个近重复 Skill ⇒ 生成 merge proposal，链含两个源 ID；
  构造冲突对 ⇒ 产生 conflict finding。
* **阴性**：非法跃迁（``archived → published``）⇒ 拒绝；无权限审批 ⇒ 403；
  ``deprecated`` 在检索中**不可见**，但历史 run 的 ``skill_id`` 仍可解析；
  描述相似但工具序列不同 ⇒ **不误合并**。
* **承重语义（反事实支撑）**：
  ① 去掉状态机校验 ⇒ 非法跃迁用例转红；
  ② 去掉 ``deprecated`` 过滤（T09 ``build_pool``）⇒ 检索用例转红。
* **诚实纪律**：成功率 ``UNKNOWN`` 不进分母；样本不足 ⇒ ``None``（不写 0）；
  生命周期事件**只追加**（红线 6）；租户 fail-closed（红线 5）。

Every test drives the real functions (never a re-implementation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.auth.jwt import create_access_token
from forgeflow.lifecycle import conflicts as cf
from forgeflow.lifecycle import retirement as rt
from forgeflow.lifecycle import similarity as sim
from forgeflow.lifecycle import state_machine as sm
from forgeflow.lifecycle.store import (
    InMemoryLifecycleStore,
    PROPOSAL_APPROVED,
    get_lifecycle_store,
    reset_lifecycle_store,
    set_lifecycle_store,
)
from forgeflow.skills.contracts import SkillContract
from forgeflow.skills.models import SkillRecord
from forgeflow.skills.retrieval import build_pool, retrieve_skills

TENANT = "t-t35"
OTHER = "t-t35-other"
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_lifecycle(force_memory_backend):
    """Pin the process-wide lifecycle store to a fresh in-memory one per test."""
    set_lifecycle_store(InMemoryLifecycleStore())
    yield
    reset_lifecycle_store()


def _store() -> InMemoryLifecycleStore:
    return get_lifecycle_store()  # type: ignore[return-value]


def _skill(skill_id: str, *, status: str = "published", description: str = "",
           tools: list[str] | None = None, tenant: str = TENANT, name: str = "") -> SkillRecord:
    record = SkillRecord(
        id=skill_id,
        tenant_id=tenant,
        name=name or skill_id,
        domain="数据分析",
        description=description or f"{skill_id} 的能力描述",
        status=status,
    )
    # Attach the declared tools so similarity/conflict helpers can read them.
    record.tools = list(tools or [])  # type: ignore[attr-defined]
    return record


# --------------------------------------------------------------------------- #
# 1 · state machine                                                             #
# --------------------------------------------------------------------------- #
def test_legal_chain_draft_candidate_published_deprecated_archived():
    store = _store()
    assert sm.current_state(store, TENANT, "s1", default="draft") == "draft"
    sm.apply_transition(store, TENANT, "s1", "candidate", default_from="draft")
    sm.apply_transition(store, TENANT, "s1", "published")
    sm.apply_transition(store, TENANT, "s1", "deprecated")
    assert sm.current_state(store, TENANT, "s1") == "deprecated"
    assert sm.retrieval_excluded(store, TENANT, "s1") is True
    sm.apply_transition(store, TENANT, "s1", "archived")
    assert sm.current_state(store, TENANT, "s1") == "archived"
    assert sm.retrieval_excluded(store, TENANT, "s1") is True


def test_illegal_archived_to_published_is_rejected():
    """阴性探针：非法跃迁（archived → published）⇒ 拒绝（反事实承重点）。"""
    store = _store()
    sm.apply_transition(store, TENANT, "s1", "archived", default_from="deprecated")
    before = len(store.list_events(TENANT, "s1"))
    with pytest.raises(sm.IllegalTransition):
        sm.apply_transition(store, TENANT, "s1", "published")
    # 拒得干净：不写入任何事件。
    assert len(store.list_events(TENANT, "s1")) == before
    assert sm.current_state(store, TENANT, "s1") == "archived"


def test_illegal_shortcut_and_unknown_target_are_rejected():
    store = _store()
    # draft → published 不允许（必须先经 candidate）。
    with pytest.raises(sm.IllegalTransition):
        sm.apply_transition(store, TENANT, "s2", "published", default_from="draft")
    # 未知目标状态也不允许。
    with pytest.raises(sm.IllegalTransition):
        sm.apply_transition(store, TENANT, "s2", "banana", default_from="published")


def test_transitions_are_append_only_and_audited():
    store = _store()
    sm.apply_transition(store, TENANT, "s1", "deprecated", reason="闲置 90 天", actor="alice")
    sm.apply_transition(store, TENANT, "s1", "published", reason="宽限期内恢复", actor="bob")
    events = store.list_events(TENANT, "s1")
    assert len(events) == 2
    assert [e.to_state for e in events] == ["deprecated", "published"]
    assert events[0].reason == "闲置 90 天" and events[0].actor == "alice"
    # 第一条事件一字未改（只追加）。
    assert events[0].from_state == "published"


def test_unresolved_tenant_migration_is_refused():
    store = _store()
    with pytest.raises(sm.LifecycleError):
        sm.apply_transition(store, None, "s1", "deprecated")


def test_events_are_tenant_scoped():
    store = _store()
    sm.apply_transition(store, TENANT, "s1", "deprecated")
    assert len(store.list_events(TENANT, "s1")) == 1
    assert store.list_events(OTHER, "s1") == []


# --------------------------------------------------------------------------- #
# 2 · retrieval exclusion (the T09 gate T35 turns on)                           #
# --------------------------------------------------------------------------- #
def test_build_pool_excludes_deprecated_and_archived():
    """反事实承重点②：去掉 T09 的 deprecated/archived 过滤 ⇒ 本用例必须转红。"""
    published = _skill("pub", status="published", description="合同风险审查")
    deprecated = _skill("dep", status="deprecated", description="合同风险审查")
    archived = _skill("arc", status="archived", description="合同风险审查")
    pool = build_pool(TENANT, [published, deprecated, archived])
    assert [s.id for s in pool] == ["pub"]


def test_retrieve_skills_hides_deprecated_but_keeps_history_resolvable():
    """``deprecated`` 在检索中不可见，但其对象 / 状态仍可解析（可读、可审计）。"""
    live = _skill("live", status="published", description="合同风险审查 tools docs.parse")
    dead = _skill("dead", status="deprecated", description="合同风险审查 tools docs.parse")
    hits = retrieve_skills(TENANT, "合同风险审查", [live, dead], k=5)
    ids = [h.skill.id for h in hits]
    assert "live" in ids and "dead" not in ids  # 阳性对照 + 阴性
    # 历史可解析：被剔除的 skill 对象本体仍在，状态 / 记录仍可读。
    assert dead.status == "deprecated"
    assert _store().list_events(TENANT, "dead") == []  # 无迁移事件也不影响可读


def test_archived_is_still_readable_and_auditable():
    store = _store()
    sm.apply_transition(store, TENANT, "s1", "archived", default_from="deprecated")
    # archived 仍可读：状态可解析、事件流可审计。
    assert sm.current_state(store, TENANT, "s1") == "archived"
    assert len(store.list_events(TENANT, "s1")) == 1
    # 但不可被检索。
    assert build_pool(TENANT, [_skill("s1", status="archived")]) == []


# --------------------------------------------------------------------------- #
# 3 · dedup / merge proposals                                                   #
# --------------------------------------------------------------------------- #
_DESC = "自动识别合同风险点，生成审查报告。"
_DESC_VARIANT = "自动识别合同风险点并生成审查报告。"
_TOOLS = ["docs.parse", "policy.check"]
_OTHER_TOOLS = ["git.diff", "code.lint"]


def test_near_duplicate_pair_generates_proposal_with_source_chain():
    """阳性探针：两个近重复 Skill ⇒ 生成 merge proposal，链含两个源 ID。"""
    a = _skill("a", description=_DESC, tools=_TOOLS)
    b = _skill("b", description=_DESC_VARIANT, tools=_TOOLS)
    records = sim.propose_merges(_store(), TENANT, [a, b])
    assert len(records) == 1
    rec = records[0]
    assert sorted(rec.source_skill_ids) == ["a", "b"]  # 来源链含两个源 ID
    assert rec.status == "proposed"  # 不自动合并
    assert rec.description_cosine is not None and rec.description_cosine >= sim.MERGE_COSINE_THRESHOLD
    assert rec.tool_jaccard >= sim.MERGE_JACCARD_THRESHOLD


def test_similar_description_different_tools_is_not_merged():
    """阴性探针：描述相似但工具序列不同 ⇒ 不误合并。"""
    a = _skill("a", description=_DESC, tools=_TOOLS)
    c = _skill("c", description=_DESC_VARIANT, tools=_OTHER_TOOLS)
    assert sim.description_cosine(a.description, c.description) >= sim.MERGE_COSINE_THRESHOLD
    assert sim.tool_sequence_jaccard(a.tools, c.tools) == 0.0
    assert sim.find_merge_candidates([a, c]) == []
    assert sim.propose_merges(_store(), TENANT, [a, c]) == []


def test_merge_requires_both_thresholds():
    a = _skill("a", description=_DESC, tools=_TOOLS)
    b = _skill("b", description="完全无关的另一件事：客户流失分析", tools=_TOOLS)
    # 工具相同但描述不相似 ⇒ 余弦不过。
    assert sim.find_merge_candidates([a, b]) == []


def test_merge_proposals_are_idempotent():
    a = _skill("a", description=_DESC, tools=_TOOLS)
    b = _skill("b", description=_DESC_VARIANT, tools=_TOOLS)
    sim.propose_merges(_store(), TENANT, [a, b])
    sim.propose_merges(_store(), TENANT, [a, b])
    assert len(_store().list_proposals(TENANT)) == 1


def test_approve_proposal_records_decision_and_preserves_chain():
    a = _skill("a", description=_DESC, tools=_TOOLS)
    b = _skill("b", description=_DESC_VARIANT, tools=_TOOLS)
    rec = sim.propose_merges(_store(), TENANT, [a, b])[0]
    approved = sim.approve_proposal(
        _store(), TENANT, rec.id, actor="alice", merged_skill_id="m1", now=NOW
    )
    assert approved.status == PROPOSAL_APPROVED
    assert approved.decided_by == "alice" and approved.merged_skill_id == "m1"
    assert sorted(approved.source_skill_ids) == ["a", "b"]
    # 重复 approve ⇒ fail-closed。
    with pytest.raises(sm.LifecycleError):
        sim.approve_proposal(_store(), TENANT, rec.id, actor="alice")


def test_plan_merged_skill_carries_merged_from_chain():
    a = _skill("a", description=_DESC, tools=_TOOLS, name="合同审查")
    b = _skill("b", description=_DESC_VARIANT, tools=_TOOLS, name="合同风险审查")
    rec = sim.propose_merges(_store(), TENANT, [a, b])[0]
    plan = sim.plan_merged_skill(rec, a, b)
    assert plan["version"]["spec"]["merged_from"] == sorted(["a", "b"])
    assert plan["skill"]["merged_from"] == sorted(["a", "b"])


def test_proposals_are_tenant_scoped():
    a = _skill("a", description=_DESC, tools=_TOOLS)
    b = _skill("b", description=_DESC_VARIANT, tools=_TOOLS)
    sim.propose_merges(_store(), TENANT, [a, b])
    assert _store().list_proposals(OTHER) == []


# --------------------------------------------------------------------------- #
# 4 · cross-skill conflicts (reusing T08)                                       #
# --------------------------------------------------------------------------- #
def test_conflict_detection_reuses_t08_io_type_conflict():
    left = SkillContract(goal="生成报告", inputs={"doc": "string"}, outputs={"r": "pdf"})
    right = SkillContract(goal="生成报告", inputs={"doc": "int"}, outputs={"r": "pdf"})
    findings = cf.detect_skill_conflict(left, right)
    assert any(f["code"] == "io_type_conflict" for f in findings)


def test_overlapping_trigger_conflict():
    left = SkillContract(goal="生成周报", applicable_when={"domain": "报表"})
    right = SkillContract(goal="生成月报", applicable_when={"domain": "报表"})
    findings = cf.detect_skill_conflict(left, right)
    assert any(f["code"] == "overlapping_trigger" for f in findings)


def test_scan_conflicts_is_deterministic_and_finds_pair():
    a = _skill("a", tools=_TOOLS)
    b = _skill("b", tools=_TOOLS)
    a.spec = {"prompt": "生成报告", "io_schema": {"input": {"doc": "string"}, "output": {}}}  # type: ignore[attr-defined]
    b.spec = {"prompt": "生成报告", "io_schema": {"input": {"doc": "int"}, "output": {}}}  # type: ignore[attr-defined]
    pairs = cf.scan_conflicts([a, b])
    assert len(pairs) == 1
    assert pairs[0].skill_ids == ["a", "b"]
    assert "io_type_conflict" in pairs[0].codes
    # 确定性：同输入两次结果一致。
    assert [p.to_dict() for p in cf.scan_conflicts([b, a])] == [p.to_dict() for p in pairs]


def test_conflict_downweighting_adjusts_score_and_emits_notice():
    a = _skill("a", tools=_TOOLS)
    b = _skill("b", tools=_TOOLS)
    other = _skill("z", tools=_TOOLS)
    a.spec = {"goal": "g", "inputs": {"doc": "string"}}  # type: ignore[attr-defined]
    b.spec = {"goal": "g", "inputs": {"doc": "int"}}  # type: ignore[attr-defined]
    pairs = cf.scan_conflicts([a, b])

    from forgeflow.skills.retrieval import RetrievedSkill

    hits = [
        RetrievedSkill(skill=a, score=1.0, similarity=0.9, lexical_score=1.0, fused_score=1.0),
        RetrievedSkill(skill=other, score=1.0, similarity=0.5, lexical_score=0.5, fused_score=0.5),
    ]
    adjusted, notices = cf.apply_conflict_downweighting(hits, pairs, factor=0.5)
    # 冲突项被降权。
    assert adjusted[0].score == pytest.approx(0.5)
    # 非冲突项不动。
    assert adjusted[1].score == pytest.approx(1.0)
    # 提示不静默。
    assert len(notices) == 1 and "a" in notices[0] and "io_type_conflict" in notices[0]


# --------------------------------------------------------------------------- #
# 5 · retirement (90d idle / last-30 success < 50% / 14d grace)                 #
# --------------------------------------------------------------------------- #
def test_retirement_idle_90_days():
    d = rt.evaluate_retirement(now=NOW, last_used_at=NOW - timedelta(days=91))
    assert d.should_retire and d.trigger == rt.TRIGGER_IDLE
    assert d.idle_days is not None and d.idle_days >= 90


def test_retirement_recent_activity_is_safe():
    d = rt.evaluate_retirement(now=NOW, last_used_at=NOW - timedelta(days=10))
    assert d.should_retire is False


def test_retirement_low_success_over_last_30():
    d = rt.evaluate_retirement(now=NOW, recent_labels=["REJECTED"] * 30)
    assert d.should_retire and d.trigger == rt.TRIGGER_LOW_SUCCESS
    assert d.success_rate == 0.0 and d.labeled_runs == 30


def test_retirement_needs_min_sample_and_ignores_unknown():
    # 只有 10 条有标签 run（< 30）⇒ 不触发（样本不足 ≠ 表现差）。
    d = rt.evaluate_retirement(now=NOW, recent_labels=["REJECTED"] * 10)
    assert d.should_retire is False and d.labeled_runs == 10
    # 全 UNKNOWN ⇒ success_rate 为 None（不写 0），且不触发。
    d2 = rt.evaluate_retirement(now=NOW, recent_labels=["UNKNOWN"] * 40)
    assert d2.should_retire is False and d2.success_rate is None and d2.labeled_runs == 0


def test_retirement_unknown_not_in_denominator():
    # 30 REJECTED + 30 UNKNOWN ⇒ 分母只算 30（成功率 0.0，而非 0.5）。
    labels = ["REJECTED"] * 30 + ["UNKNOWN"] * 30
    d = rt.evaluate_retirement(now=NOW, recent_labels=labels)
    assert d.labeled_runs == 30 and d.success_rate == 0.0 and d.should_retire


def test_grace_period_start_revoke_and_finalize():
    store = _store()
    notice = rt.start_grace(store, TENANT, "s1", owner="alice", now=NOW)
    assert sm.current_state(store, TENANT, "s1") == "deprecated"
    assert notice["owner"] == "alice" and notice["kind"] == "skill_retirement_grace"
    deadline = datetime.fromisoformat(notice["grace_deadline"])
    assert (deadline - NOW).days == rt.GRACE_DAYS

    # 宽限期内 ⇒ True；恢复则撤销（deprecated → published）。
    assert rt.within_grace(store, TENANT, "s1", now=NOW + timedelta(days=3)) is True
    rt.revoke_grace(store, TENANT, "s1", now=NOW + timedelta(days=3))
    assert sm.current_state(store, TENANT, "s1") == "published"
    assert rt.within_grace(store, TENANT, "s1", now=NOW + timedelta(days=3)) is False


def test_grace_finalize_archives():
    store = _store()
    rt.start_grace(store, TENANT, "s1", now=NOW)
    assert rt.within_grace(store, TENANT, "s1", now=NOW + timedelta(days=20)) is False
    rt.finalize_grace(store, TENANT, "s1", now=NOW + timedelta(days=20))
    assert sm.current_state(store, TENANT, "s1") == "archived"
    assert build_pool(TENANT, [_skill("s1", status="archived")]) == []


# --------------------------------------------------------------------------- #
# 6 · API wiring                                                                #
# --------------------------------------------------------------------------- #
def test_lifecycle_routes_are_mapped_in_rbac():
    from forgeflow.middleware.auth import RBACMiddleware

    assert RBACMiddleware._resolve_permission(
        "GET", "/skills/lifecycle/proposals"
    ) == ("read", "skills")
    assert RBACMiddleware._resolve_permission(
        "POST", "/skills/lifecycle/proposals/p1/approve"
    ) == ("write", "skills")
    assert RBACMiddleware._resolve_permission("GET", "/skills/s1/lifecycle") == ("read", "skills")


def test_list_proposals_requires_auth_then_lists():
    client = TestClient(app)
    assert client.get("/skills/lifecycle/proposals").status_code == 401
    token = create_access_token(user_id="manager-1", role="manager")
    resp = client.get(
        "/skills/lifecycle/proposals", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code in (200, 403), resp.text
    if resp.status_code == 200:
        assert resp.json()["proposals"] == []


def test_approve_without_permission_is_403():
    store = _store()
    a = _skill("a", description=_DESC, tools=_TOOLS)
    b = _skill("b", description=_DESC_VARIANT, tools=_TOOLS)
    rec = sim.propose_merges(store, TENANT, [a, b])[0]

    client = TestClient(app)
    viewer = create_access_token(user_id="viewer-1", role="viewer")
    resp = client.post(
        f"/skills/lifecycle/proposals/{rec.id}/approve",
        json={"note": "试试"},
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert resp.status_code == 403, resp.text
    # 未越权执行：提案仍是 proposed。
    assert store.get_proposal(TENANT, rec.id).status == "proposed"
