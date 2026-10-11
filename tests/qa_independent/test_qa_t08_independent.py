"""QA independent verification — INC46 T08 (frozen: evolution_loop sha256 acf396…).

Owned by QA (严过关). NOT the engineer's file; added only, never weakening any
existing assertion. Every probe here was written to *falsify*, not to re-run the
engineer's command.

Contents
--------
* V1'  — an **independent** end-to-end probe that drives the real
  ``evolution_loop.maybe_evolve`` publish entry point with a DANGEROUS candidate,
  using QA's own fakes and QA's own observation surface (it records whether
  ``governance_gate.promote_candidate`` was actually reached). This is the
  green-baseline for the external **source-mutation** counterfactual (see
  ``qa_tmp/qa_t08_source_mutation_*``), which removes ``gate_blocked or`` from
  ``evolution_loop.py:782`` and must turn this file red.
* V3   — three-chain counterfactual at the single upstream root. QA's injection
  point is **the data source** ``forgeflow.runtime.gate.TOOL_PERMISSION_MAP``
  (emptied) — a *different mechanism* than the engineer's ``classify_tool``
  monkeypatch, but the same唯一根. Chains A/B/C must all flip; restoring flips back.
* V4   — ``engineering.version_and_publish`` fail-closed 403 on a DANGEROUS
  candidate, and no new version minted.

Self-attest: v1 2026-10-03 — initial QA independent probe (never edited by the
engineer; QA owns this file).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.skills import publish_interlock as pi
from forgeflow.skills import evolution_loop as el
from forgeflow.skills.evolution_loop import EVOLVE_TRIGGER_MIN_FAILURES
from forgeflow.skills.evolution_loop import maybe_evolve
from forgeflow.skills.evolution_loop import reset_evolution_state
from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.contracts import SkillContract

TENANT = "qa-tenant-t08"
SKILL_ID = "qa-skill-t08"


# --------------------------------------------------------------------------- #
# QA-owned fakes (deliberately minimal, mirroring only the seams touched)      #
# --------------------------------------------------------------------------- #
class _SkillRepo:
    def __init__(self, skill, versions):
        self.skill = skill
        self.versions = list(versions)

    async def get_skill(self, tenant, skill_id):
        if self.skill.tenant_id != tenant or self.skill.id != skill_id:
            return None
        return self.skill

    async def get_version(self, tenant, skill_id, semver):
        for v in self.versions:
            if v.tenant_id == tenant and v.skill_id == skill_id and v.semver == semver:
                return v
        return None

    async def list_versions(self, tenant, skill_id):
        return [v for v in self.versions if v.tenant_id == tenant and v.skill_id == skill_id]

    async def add_version(self, tenant, version):
        self.versions.append(version)
        return version

    async def update_skill(self, skill):
        self.skill = skill
        return skill


class _CandidateRepo:
    def __init__(self):
        self.saved = []
        self.evaluation = SimpleNamespace(metrics={"score": 0.95}, verdict="pass")

    async def save_candidate(self, candidate):
        self.saved.append(candidate)
        return candidate

    async def get_candidate(self, *args):
        wanted = args[-1] if args else None
        for c in self.saved:
            if getattr(c, "id", None) == wanted:
                return c
        return None

    async def get_evaluation_for(self, *args):
        return self.evaluation


class _ExperienceRepo:
    def __init__(self):
        self.saved = []

    async def save(self, record):
        self.saved.append(record)
        return record

    async def get(self, tenant, exp_id):
        return None


class _PolicyRepo:
    async def save_approval(self, record):
        return record


def _dangerous_candidate(label: str = "qa-danger") -> SimpleNamespace:
    """A compiler-shaped candidate whose draft_spec declares ``payment.transfer``.

    ``payment.transfer`` is DANGEROUS *and* whitelisted ⇒ the only gate trigger is
    ``dangerous_operation`` (so the root-bypass counterfactual flips the wiring).
    """
    return SimpleNamespace(
        id=f"cand-{label}",
        status="compiled",
        name="QA 联锁演示技能",
        domain="general",
        experience_ids=["e1", "e2", "e3"],
        draft_spec={
            "prompt": "转账付款",
            "steps": ["读取金额", "发起转账"],
            "tools": ["payment.transfer"],
            "io_schema": {"input": {"amount": "str"}, "output": {"txn_id": "str"}},
        },
    )


def _skill() -> SkillRecord:
    return SkillRecord(
        id=SKILL_ID,
        tenant_id=TENANT,
        name="QA 联锁演示技能",
        current_version="1.0.0",
        status="published",
    )


def _incumbent() -> SkillVersionRecord:
    return SkillVersionRecord(
        tenant_id=TENANT, skill_id=SKILL_ID, semver="1.0.0", eval_score=0.80
    )


def _failures(n: int) -> list[dict]:
    return [{"run_id": f"run-{i}", "modes": ["tool_error"], "steps": []} for i in range(n)]


@pytest.fixture(autouse=True)
def _clean():
    pi.reset_interlock_state()
    reset_evolution_state()
    yield
    pi.reset_interlock_state()
    reset_evolution_state()


# --------------------------------------------------------------------------- #
# V1' — independent e2e probe (green baseline for the source-mutation red)     #
# --------------------------------------------------------------------------- #
async def test_qa_probe_dangerous_candidate_blocked_at_real_publish_entry(monkeypatch):
    """Drive REAL ``maybe_evolve``; DANGEROUS candidate must NOT reach promote.

    Observation surface is QA's own: ``governance_gate.promote_candidate`` is
    replaced with a spy so we can assert it was **never reached** (a stronger,
    independent observation than inspecting the outcome object alone).
    """
    import forgeflow.skills.candidate_compiler as cc
    import forgeflow.skills.engineering as eng
    import forgeflow.skills.governance_gate as gg

    reached: list[str] = []

    async def _many(tenant, *, skill_tools, window_days=30):
        return _failures(EVOLVE_TRIGGER_MIN_FAILURES + 1)

    async def _fake_compile(tenant, experience_ids, mode, **kwargs):
        return _dangerous_candidate()

    async def _fake_engineering(*args, **kwargs):
        return SimpleNamespace(passed=True, degraded_reason=None)

    async def _spy_promote(candidate_id, actor, **kwargs):
        reached.append(candidate_id)
        return SkillVersionRecord(tenant_id=TENANT, skill_id=SKILL_ID, semver="9.9.9")

    monkeypatch.setattr(el, "collect_skill_failures", _many)
    monkeypatch.setattr(cc, "compile_candidate", _fake_compile)
    monkeypatch.setattr(eng, "run_engineering_loop", _fake_engineering)
    monkeypatch.setattr(gg, "promote_candidate", _spy_promote)
    # Simulate M1 complete: Level-2 auto-publish released. The ONLY remaining
    # blocker must be the T08 gate wiring at evolution_loop.py:779/782.
    monkeypatch.setattr(pi, "auto_publish_permitted", lambda tenant: True)

    skill = _skill()
    repos = {
        "skill_repo": _SkillRepo(skill, [_incumbent()]),
        "candidate_repo": _CandidateRepo(),
        "experience_repo": _ExperienceRepo(),
        "policy_repo": _PolicyRepo(),
    }

    out = await maybe_evolve(TENANT, SKILL_ID, actor="qa", **repos)

    assert out.triggered is True, "must really reach the publish branch of maybe_evolve"
    assert reached == [], "DANGEROUS candidate must never reach promote_candidate"
    assert out.applied is False
    assert out.publish_state == pi.STATE_PENDING_APPROVAL
    assert skill.current_version == "1.0.0", "incumbent must not move"
    assert "闸门" in out.reason, out.reason

    staged = await repos["skill_repo"].get_version(TENANT, SKILL_ID, out.to_version)
    assert staged is not None and staged.release_state == "pending_approval"
    assert staged.approved_by is None


# --------------------------------------------------------------------------- #
# V3 — three-chain counterfactual at the single upstream root                  #
# --------------------------------------------------------------------------- #
def test_qa_three_chain_counterfactual_root_is_the_single_bypass_point(monkeypatch):
    """Chain A/B/C all flip when the *root* denies DANGEROUS.

    QA injection: disable the single DANGEROUS determination at its root —
    ``tool_permissions._dangerous_tools`` → empty — a *different mechanism* than
    the engineer's ``classify_tool`` monkeypatch, same唯一根. (A coarser injection
    that empties ``gate.TOOL_PERMISSION_MAP`` was tried first and did NOT isolate
    the DANGEROUS judgment: that table also backs ``allowed_tool_set`` ⇒ a new
    ``tools_not_whitelisted`` high finding appears — see the QA report.)
    """
    from forgeflow.skills import candidate_gate as anchor
    from forgeflow.skills import critic, risk_escalation, tool_permissions

    danger = SkillContract(
        goal="转账付款",
        procedure=["读取金额", "发起转账"],
        tools=["payment.transfer"],
        verification=["失败则回滚"],
        risk_level="low",
    )

    # (0) positive baseline — three chains + interlock probe all positive.
    assert "dangerous_operation" in [f["code"] for f in critic.critique(danger).findings]
    assert risk_escalation.effective_risk_level(danger) == "high"
    assert anchor.evaluate_candidate_gate(danger).blocks_auto_publish is True
    assert anchor.INTERLOCK_PROBE()["ok"] is True
    assert pi._probe_capability(pi.REQUIREMENTS[0]).met is True

    # (1) inject at the root: no tool is ever classified DANGEROUS.
    monkeypatch.setattr(tool_permissions, "_dangerous_tools", lambda: frozenset())

    # (2) chain A red — critic no longer names dangerous_operation.
    assert "dangerous_operation" not in [f["code"] for f in critic.critique(danger).findings]
    # (2) chain B red — risk no longer escalated to high.
    assert risk_escalation.effective_risk_level(danger) != "high"
    # (2) chain C red — gate allows + interlock probe self-reports unmet.
    mut = anchor.evaluate_candidate_gate(danger)
    assert mut.allowed is True and mut.blocks_auto_publish is False
    assert anchor.INTERLOCK_PROBE()["ok"] is False
    assert pi._probe_capability(pi.REQUIREMENTS[0]).met is False
    assert pi.auto_publish_permitted(TENANT) is False

    # (3) restore ⇒ all chains green again (bidirectional flip).
    monkeypatch.undo()
    assert anchor.INTERLOCK_PROBE()["ok"] is True
    assert pi._probe_capability(pi.REQUIREMENTS[0]).met is True
    assert risk_escalation.effective_risk_level(danger) == "high"


def test_qa_only_critic_branch_does_not_flip_chain_b_or_c(monkeypatch):
    """M1 contrast (裁定 R8): removing ONLY the critic rule must NOT flip B/C."""
    from forgeflow.skills import candidate_gate as anchor
    from forgeflow.skills import critic, risk_escalation

    danger = SkillContract(
        goal="转账付款",
        procedure=["读取金额", "发起转账"],
        tools=["payment.transfer"],
        verification=["失败则回滚"],
        risk_level="low",
    )
    original = critic._privilege_findings

    def _without_dangerous(contract):
        return [f for f in original(contract) if f.get("code") != "dangerous_operation"]

    monkeypatch.setattr(critic, "_privilege_findings", _without_dangerous)

    # chain A flips ...
    assert "dangerous_operation" not in [f["code"] for f in critic.critique(danger).findings]
    # ... but B/C do NOT (depth-in-defence still holds) — so it is NOT a three-chain.
    assert risk_escalation.effective_risk_level(danger) == "high"
    assert anchor.evaluate_candidate_gate(danger).blocks_auto_publish is True
    assert anchor.INTERLOCK_PROBE()["ok"] is True
    assert pi._probe_capability(pi.REQUIREMENTS[0]).met is True


# --------------------------------------------------------------------------- #
# V4 — engineering.version_and_publish fail-closed 403                         #
# --------------------------------------------------------------------------- #
async def test_qa_engineering_version_and_publish_403_on_dangerous(monkeypatch):
    """Independent V4: construct a DANGEROUS candidate, call version_and_publish,
    assert 403 and NO new version minted (promote never reached)."""
    from forgeflow.skills import engineering
    from forgeflow.skills import governance_gate as gg
    from forgeflow.skills.errors import GovernanceError

    cand_repo = _CandidateRepo()
    danger = _dangerous_candidate("qa-v4")
    cand_repo.saved.append(danger)

    skill = _skill()
    skill_repo = _SkillRepo(skill, [_incumbent()])
    before_version = skill.current_version

    promote_calls: list[str] = []

    async def _spy_promote(candidate_id, actor, **kwargs):  # pragma: no cover
        promote_calls.append(candidate_id)
        return SkillVersionRecord(tenant_id=TENANT, skill_id=SKILL_ID, semver="9.9.9")

    monkeypatch.setattr(gg, "promote_candidate", _spy_promote)

    with pytest.raises(GovernanceError) as exc:
        await engineering.version_and_publish(
            TENANT,
            danger.id,
            actor="qa",
            actor_role="manager",
            candidate_repo=cand_repo,
            skill_repo=skill_repo,
            policy_repo=_PolicyRepo(),
        )

    assert exc.value.status_code == 403, exc.value
    assert promote_calls == [], "must fail-closed BEFORE promote"
    assert skill.current_version == before_version, "no new version minted"
    assert len(skill_repo.versions) == 1, "version list unchanged"
