"""Release-gate audit trail — the caller-layer trace review finding #11 asked for.

``promote_candidate`` used to *discard* the ``ReleaseDecision`` from
``evaluate_release``: it 403'd on a regression, but a caller could not tell a
``no_baseline`` pass (a first release, or a scoreless baseline) from a
verified-clean pass — so "why was this version released?" had no answer after the
fact. These tests pin that **every** decision is recorded on the platform audit
trail — the same sink ``/audit/search`` + ``/audit/export`` read — carrying the
skill id and version, and that no sensitive evaluation internals leak into it.

Three paths, one test each:
  * ``no_baseline`` → first release → allowed
  * ``ok``         → clean upgrade → allowed
  * ``regression`` → blocked upgrade → 403 (denied) path
plus a consumer read-back and a "no leak" guard.
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.api.routers.audit import _RING, clear_audit_ring, search_audit_log
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
from forgeflow.repositories.memory.skill_repo import (
    MemorySkillCandidateRepository,
    MemorySkillRepository,
)
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.governance_gate import (
    _RELEASE_AUDIT_ACTION,
    promote_candidate,
)
from forgeflow.skills.models import SkillCandidateRecord, SkillEvaluationRecord

_DRAFT = {
    "prompt": "分析客户流失",
    "steps": ["拉取数据", "计算概率"],
    "tools": ["data.query", "analysis.score", "report.render"],
    "io_schema": {"input": {"intent": "string"}, "output": {"summary": "string"}},
}


@pytest.fixture(autouse=True)
def _clean_ring():
    """Isolate the shared offline audit ring per test."""
    clear_audit_ring()
    yield
    clear_audit_ring()


def _repos():
    return (
        MemorySkillCandidateRepository(),
        MemorySkillRepository(),
        MemoryPolicyRepository(),
    )


async def _candidate(repo, tenant: str, name: str) -> SkillCandidateRecord:
    candidate = SkillCandidateRecord(
        tenant_id=tenant,
        name=name,
        domain="数据分析",
        experience_ids=["e1", "e2"],
        draft_spec=dict(_DRAFT),
        status="draft",
    )
    await repo.save_candidate(candidate)
    return candidate


async def _evaluate(repo, tenant: str, candidate_id: str, score: float) -> None:
    await repo.save_evaluation(
        SkillEvaluationRecord(
            tenant_id=tenant,
            target_id=candidate_id,
            metrics={"score": score},
            verdict="pass",
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


def _release_rows() -> list[dict]:
    return [r for r in _RING if r.get("action") == _RELEASE_AUDIT_ACTION]


# --------------------------------------------------------------------------- #
# ① no_baseline — a first release is recorded (allowed), not silently clean      #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_first_release_is_audited_as_no_baseline():
    tenant = f"t-rel-audit-{uuid.uuid4().hex[:8]}"
    cand_repo, skill_repo, pol_repo = _repos()
    name = f"审计无基线-{uuid.uuid4().hex[:6]}"
    candidate = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, candidate.id, 0.9)

    version = await _promote(cand_repo, skill_repo, pol_repo, tenant, candidate.id)
    assert version.semver == "0.1.0"

    rows = _release_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "allowed"
    assert row["resource"] == "skills"
    assert row["resource_id"] == name
    assert row["role"] == "manager"
    meta = row["metadata"]
    assert meta["skill"] == name
    assert meta["severity"] == "no_baseline"
    assert meta["allowed"] is True
    assert meta["version"] == "0.1.0"


# --------------------------------------------------------------------------- #
# ② ok — a clean upgrade is recorded as ok (distinct from no_baseline)          #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_clean_upgrade_is_audited_as_ok():
    tenant = f"t-rel-audit-{uuid.uuid4().hex[:8]}"
    cand_repo, skill_repo, pol_repo = _repos()
    name = f"审计干净通过-{uuid.uuid4().hex[:6]}"

    first = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, first.id, 0.80)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, first.id)

    second = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, second.id, 0.95)
    version = await _promote(cand_repo, skill_repo, pol_repo, tenant, second.id)
    assert version.semver == "0.2.0"

    rows = _release_rows()
    # First release (no_baseline) then this clean upgrade (ok) — the two allow
    # reasons are distinguishable, which is the whole point of the finding.
    assert [r["metadata"]["severity"] for r in rows] == ["no_baseline", "ok"]
    latest = rows[-1]
    assert latest["outcome"] == "allowed"
    assert latest["metadata"]["skill"] == name
    assert latest["metadata"]["allowed"] is True
    assert latest["metadata"]["version"] == "0.2.0"


# --------------------------------------------------------------------------- #
# ③ regression — the 403 path is recorded as denied with the version it hit     #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_regressing_upgrade_is_audited_as_denied():
    tenant = f"t-rel-audit-{uuid.uuid4().hex[:8]}"
    cand_repo, skill_repo, pol_repo = _repos()
    name = f"审计回归拒绝-{uuid.uuid4().hex[:6]}"

    first = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, first.id, 0.90)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, first.id)

    second = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, second.id, 0.60)
    with pytest.raises(GovernanceError) as excinfo:
        await _promote(cand_repo, skill_repo, pol_repo, tenant, second.id)
    assert excinfo.value.status_code == 403

    denied = _release_rows()[-1]
    assert denied["outcome"] == "denied"
    meta = denied["metadata"]
    assert meta["skill"] == name
    assert meta["severity"] == "regression"
    assert meta["allowed"] is False
    # The version that *stays* (the one the blocked upgrade targeted).
    assert meta["version"] == "0.1.0"


# --------------------------------------------------------------------------- #
# Consumer read-back + no-leak guard                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_release_audit_is_readable_through_the_search_consumer():
    # A UUID tenant so the entry carries a real workspace_id and the consumer's
    # tenant scoping can be exercised (a non-UUID slug is stored as NULL, exactly
    # like the request-audit path — see middleware/audit ``_uuid_or_none``).
    tenant = str(uuid.uuid4())
    cand_repo, skill_repo, pol_repo = _repos()
    name = f"审计可检索-{uuid.uuid4().hex[:6]}"
    candidate = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, candidate.id, 0.9)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, candidate.id)

    # The offline branch of the very function /audit/search wraps.
    result = await search_audit_log(
        pool=None, action=_RELEASE_AUDIT_ACTION, workspace_id=tenant
    )
    assert result["total"] == 1
    assert result["items"][0]["metadata"]["severity"] == "no_baseline"
    assert result["items"][0]["resource_id"] == name


@pytest.mark.asyncio
async def test_release_audit_does_not_leak_evaluation_internals():
    tenant = f"t-rel-audit-{uuid.uuid4().hex[:8]}"
    cand_repo, skill_repo, pol_repo = _repos()
    name = f"审计不泄露-{uuid.uuid4().hex[:6]}"
    candidate = await _candidate(cand_repo, tenant, name)
    await _evaluate(cand_repo, tenant, candidate.id, 0.9)
    await _promote(cand_repo, skill_repo, pol_repo, tenant, candidate.id)

    meta = _release_rows()[-1]["metadata"]
    # Exactly the explainable conclusion + identifiers — nothing else.
    assert set(meta) == {
        "skill",
        "candidate_id",
        "version",
        "severity",
        "allowed",
        "reason",
    }
    # No raw baseline / per-metric findings / evaluation internals surfaced.
    assert "findings" not in meta
    assert "baseline" not in meta
    assert "metrics" not in meta
    assert "draft_spec" not in meta
