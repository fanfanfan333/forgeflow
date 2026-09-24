"""INC9 B1 — skill canary wiring (promote → canary → resolve).

Pins the *runtime* behaviour the pure layer cannot show (docs/sop/12-INC9-DESIGN.md
§10, unit-wiring):

* **default (canary off):** promotion is byte-for-byte the old behaviour — the
  new version takes ``current_version`` at once and is ``release_state="promoted"``;
* **canary on:** promotion records a ``canary`` version but leaves
  ``current_version`` on the incumbent, and writes one ``skill.canary`` audit row;
* ``resolve_canary`` applies promote / hold / rollback and audits each;
* the new route is covered by the ``/skills`` RBAC prefix (UNMAPPED stays 0).
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.api.routers.audit import _RING, clear_audit_ring
from forgeflow.config import get_settings
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
from forgeflow.repositories.memory.skill_repo import (
    MemorySkillCandidateRepository,
    MemorySkillRepository,
)
from forgeflow.skills.governance_gate import promote_candidate, resolve_canary
from forgeflow.skills.models import SkillCandidateRecord, SkillEvaluationRecord

pytestmark = pytest.mark.asyncio

_DRAFT = {
    "prompt": "分析客户流失",
    "steps": ["拉取数据", "计算概率"],
    "tools": ["data.query", "analysis.score", "report.render"],
    "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
}


@pytest.fixture(autouse=True)
def _clean_ring():
    clear_audit_ring()
    yield
    clear_audit_ring()


def _repos():
    return (
        MemorySkillCandidateRepository(),
        MemorySkillRepository(),
        MemoryPolicyRepository(),
    )


def _tenant() -> str:
    return f"t-canary-{uuid.uuid4().hex[:8]}"


async def _candidate(cand_repo, tenant: str, name: str) -> SkillCandidateRecord:
    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name=name,
        domain="数据分析",
        experience_ids=["e1", "e2"],
        draft_spec=dict(_DRAFT),
        status="draft",
    )
    await cand_repo.save_candidate(candidate)
    return candidate


async def _evaluate(cand_repo, tenant: str, candidate_id: str, score: float) -> None:
    await cand_repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant, target_id=candidate_id, metrics={"score": score}, verdict="pass"
        )
    )


async def _promote(cand_repo, skill_repo, pol_repo, tenant: str, candidate_id: str):
    return await promote_candidate(
        candidate_id,
        "manager-1",
        tenant_id=tenant,
        candidate_repo=cand_repo,
        skill_repo=skill_repo,
        policy_repo=pol_repo,
        actor_role="manager",
    )


def _canary_rows() -> list[dict]:
    return [r for r in _RING if r.get("action") == "skill.canary"]


# --------------------------------------------------------------------------- #
# default path — canary off (zero change)                                       #
# --------------------------------------------------------------------------- #

async def test_promote_without_canary_switches_over_at_once(monkeypatch):
    monkeypatch.setattr(get_settings(), "skill_canary_enabled", False)
    tenant = _tenant()
    cand_repo, skill_repo, pol_repo = _repos()
    name = f"默认即时切换-{uuid.uuid4().hex[:6]}"

    first = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, first.id, 0.80)
    v1 = await _promote(cand_repo, skill_repo, pol_repo, tenant, first.id)
    assert v1.semver == "0.1.0"
    assert v1.release_state == "promoted"

    second = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, second.id, 0.95)
    v2 = await _promote(cand_repo, skill_repo, pol_repo, tenant, second.id)
    assert v2.semver == "0.2.0"
    assert v2.release_state == "promoted"

    skill = await skill_repo.get_skill_by_name(tenant, name)
    assert skill is not None and skill.current_version == "0.2.0"
    # No canary audit is written when the feature is off.
    assert _canary_rows() == []


# --------------------------------------------------------------------------- #
# canary path — promote records a canary, keeps the incumbent                   #
# --------------------------------------------------------------------------- #

async def test_promote_with_canary_records_canary_and_keeps_incumbent(monkeypatch):
    monkeypatch.setattr(get_settings(), "skill_canary_enabled", True)
    tenant = _tenant()
    cand_repo, skill_repo, pol_repo = _repos()
    name = f"灰度发布-{uuid.uuid4().hex[:6]}"

    # First release always publishes at once (no incumbent to A/B against).
    first = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, first.id, 0.80)
    v1 = await _promote(cand_repo, skill_repo, pol_repo, tenant, first.id)
    assert v1.semver == "0.1.0"
    assert v1.release_state == "promoted"

    # The upgrade enters the canary window.
    second = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, second.id, 0.95)
    v2 = await _promote(cand_repo, skill_repo, pol_repo, tenant, second.id)
    assert v2.semver == "0.2.0"
    assert v2.release_state == "canary"

    skill = await skill_repo.get_skill_by_name(tenant, name)
    assert skill is not None
    # current_version is UNCHANGED — the canary is not the default yet.
    assert skill.current_version == "0.1.0"

    rows = _canary_rows()
    assert len(rows) == 1
    meta = rows[0]["metadata"]
    assert meta["action"] == "canary_start"
    assert meta["severity"] == "canary_start"
    assert meta["version"] == "0.2.0"


# --------------------------------------------------------------------------- #
# resolve_canary — three outcomes                                               #
# --------------------------------------------------------------------------- #

async def _canary_setup(monkeypatch):
    monkeypatch.setattr(get_settings(), "skill_canary_enabled", True)
    tenant = _tenant()
    cand_repo, skill_repo, pol_repo = _repos()
    name = f"灰度裁决-{uuid.uuid4().hex[:6]}"
    first = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, first.id, 0.80)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, first.id)
    second = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, second.id, 0.90)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, second.id)
    skill = await skill_repo.get_skill_by_name(tenant, name)
    return tenant, skill_repo, skill, name


async def test_resolve_promotes_the_canary_on_win(monkeypatch):
    tenant, skill_repo, skill, name = await _canary_setup(monkeypatch)
    result = await resolve_canary(
        skill.id,
        tenant_id=tenant,
        actor="manager-1",
        actor_role="manager",
        canary_metrics={"score": 0.95},
        incumbent_metrics={"score": 0.90},
        sample_n=20,
        skill_repo=skill_repo,
    )
    assert result["action"] == "promote"
    assert result["severity"] == "ok"
    fresh = await skill_repo.get_skill(tenant, skill.id)
    assert fresh.current_version == "0.2.0"


async def test_resolve_rolls_back_on_regression(monkeypatch):
    tenant, skill_repo, skill, name = await _canary_setup(monkeypatch)
    result = await resolve_canary(
        skill.id,
        tenant_id=tenant,
        actor="manager-1",
        actor_role="manager",
        canary_metrics={"score": 0.60},
        incumbent_metrics={"score": 0.90},
        sample_n=20,
        skill_repo=skill_repo,
    )
    assert result["action"] == "rollback"
    assert result["severity"] == "regression"
    fresh = await skill_repo.get_skill(tenant, skill.id)
    # The incumbent stays current; the canary is marked rolled_back.
    assert fresh.current_version == "0.1.0"
    canary = await skill_repo.get_version(tenant, skill.id, "0.2.0")
    assert canary is not None and canary.release_state == "rolled_back"


async def test_resolve_holds_when_samples_insufficient(monkeypatch):
    tenant, skill_repo, skill, name = await _canary_setup(monkeypatch)
    result = await resolve_canary(
        skill.id,
        tenant_id=tenant,
        actor="manager-1",
        actor_role="manager",
        canary_metrics={"score": 0.99},
        incumbent_metrics={"score": 0.50},
        sample_n=3,
        skill_repo=skill_repo,
    )
    assert result["action"] == "hold"
    assert result["severity"] == "insufficient"
    fresh = await skill_repo.get_skill(tenant, skill.id)
    assert fresh.current_version == "0.1.0"  # unchanged


async def test_resolve_audits_every_outcome(monkeypatch):
    tenant, skill_repo, skill, name = await _canary_setup(monkeypatch)
    clear_audit_ring()
    await resolve_canary(
        skill.id,
        tenant_id=tenant,
        actor="manager-1",
        actor_role="manager",
        canary_metrics={"score": 0.60},
        incumbent_metrics={"score": 0.90},
        sample_n=20,
        skill_repo=skill_repo,
    )
    rows = _canary_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "denied"
    assert row["resource"] == "skills"
    assert row["resource_id"] == name
    meta = row["metadata"]
    assert meta["action"] == "rollback"
    assert meta["severity"] == "regression"
    assert meta["version"] == "0.2.0"


async def test_resolve_missing_skill_is_404(monkeypatch):
    tenant = _tenant()
    _, skill_repo, _ = _repos()
    from forgeflow.skills.errors import GovernanceError

    with pytest.raises(GovernanceError) as exc:
        await resolve_canary(
            str(uuid.uuid4()),
            tenant_id=tenant,
            canary_metrics={"score": 0.9},
            incumbent_metrics={"score": 0.9},
            sample_n=20,
            skill_repo=skill_repo,
        )
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------- #
# RBAC coverage — the new route sits under the /skills prefix                    #
# --------------------------------------------------------------------------- #

def test_canary_route_is_covered_by_the_skills_prefix():
    assert RBACMiddleware._resolve_permission("POST", "/skills/abc/canary/resolve") == (
        "write",
        "skills",
    )


# --------------------------------------------------------------------------- #
# O4 — should_serve has a real production caller (SkillRegistry.select)          #
# --------------------------------------------------------------------------- #

async def _published_skill_with_canary(repo, tenant: str, name: str):
    from forgeflow.skills.models import SkillRecord, SkillVersionRecord

    skill = SkillRecord(
        tenant_id=tenant, name=name, domain="数据分析", status="published",
        description="给销售线索打分", current_version="1.0.0",
    )
    await repo.create_skill(skill)
    await repo.add_version(
        tenant,
        SkillVersionRecord(tenant_id=tenant, skill_id=skill.id, semver="1.0.0", spec={}),
    )
    await repo.add_version(
        tenant,
        SkillVersionRecord(
            tenant_id=tenant, skill_id=skill.id, semver="1.1.0", spec={}, release_state="canary"
        ),
    )
    return skill


async def test_select_is_unchanged_at_zero_traffic(monkeypatch):
    monkeypatch.setattr(get_settings(), "skill_canary_traffic_pct", 0)
    tenant = _tenant()
    _, skill_repo, _ = _repos()
    from forgeflow.skills.registry import SkillRegistry

    name = f"选择零流量-{uuid.uuid4().hex[:6]}"
    await _published_skill_with_canary(skill_repo, tenant, name)

    picked = await SkillRegistry(repo=skill_repo).select(tenant, name, k=3)
    assert [s.name for s in picked] == [name]
    # current_version unchanged and the stored object is not mutated.
    assert picked[0].current_version == "1.0.0"
    stored = await skill_repo.get_skill_by_name(tenant, name)
    assert stored.current_version == "1.0.0"


async def test_select_exposes_canary_at_full_traffic(monkeypatch):
    monkeypatch.setattr(get_settings(), "skill_canary_traffic_pct", 100)
    tenant = _tenant()
    _, skill_repo, _ = _repos()
    from forgeflow.skills.registry import SkillRegistry

    name = f"选择全流量-{uuid.uuid4().hex[:6]}"
    await _published_skill_with_canary(skill_repo, tenant, name)

    picked = await SkillRegistry(repo=skill_repo).select(tenant, name, k=3)
    assert picked[0].current_version == "1.1.0"  # the exposed canary view
    # The *stored* skill is untouched (the exposure is a per-request copy).
    stored = await skill_repo.get_skill_by_name(tenant, name)
    assert stored.current_version == "1.0.0"
