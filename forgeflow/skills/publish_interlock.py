"""INC46 T15 — the publish interlock (自动发布联锁).

Why this exists
---------------
T05 (``evolution_loop.maybe_evolve``) used to publish a regression-passing
candidate automatically (回归通过即自动发布). INC46 §3.3 / §六-T15 rules that
auto-publish is locked until eight guardrail capabilities (R1–R8) are measurably
in place. Until then the loop may only **stage a candidate** — a new semver that
never takes effect — and a human with publish permission approves it explicitly:

    candidate → pending_approval → approved → published;   rejected 为终态

* **Level-2（自动发布）**: R1–R8 **全部满足** 且租户显式开启
  ``evolution.auto_publish``（默认关）。
* **Level-1（人工批准发布）**: R1–R6 满足后开放；每次发布写一条
  ``publish_approvals`` 记录。
* **R1–R6 满足之前**: 候选只生成，状态 ``pending_approval``，不发布。

Fail-closed capability probes (INC46 §3.3)
------------------------------------------
R1–R8 map to capabilities delivered by later tasks (T08/T13/T16/T17/T32/T33/
T34/T36) that **do not exist yet**. Each requirement therefore has a *capability
anchor* — the module that task will land — and the probe is genuinely
fail-closed: a missing module, a missing ``INTERLOCK_PROBE`` callable, a raised
probe, or a self-reported ``ok=False`` all mean **unmet**. Nothing here is a
hard-coded ``True``; the day a capability lands its module, the probe picks it
up without a code change here.

Forward contract for the capability modules::

    def INTERLOCK_PROBE() -> dict:   # no I/O beyond local, no LLM
        return {"ok": bool, "evidence": "human-readable proof reference"}

``GET /evolution/interlock`` returns every requirement's status plus its
evidence reference — when there is no evidence the payload **says so**
(``无证据：…``), it never masquerades as satisfied.

Honesty / tenant discipline (INC46 §8)
--------------------------------------
* unmeasured ⇒ ``None`` never ``0`` / ``""``: a ``pending`` record has
  ``approver=None`` (no approval happened yet), never a fabricated approver.
* tenant **fail-closed**: :func:`require_tenant` gates every entry point; all
  ledger keys and (postgres) rows carry ``tenant_id``.
* approving a candidate whose regression re-check fails is **always refused** —
  the ``rejected`` decision is recorded, the incumbent stays, ``applied`` stays
  ``False``.

Deviation A1 (书面记录): the task book suggested ``evolution/publish_interlock.py``;
the repository has no ``evolution/`` package, so this module lives next to
``evolution_loop.py`` in ``forgeflow/skills/`` — same package, same import
discipline, zero new top-level packages.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories import get_skill_repository
from forgeflow.repositories.base import new_id, utcnow
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)

__all__ = [
    "INTERLOCK_MARKER",
    "PUBLISH_STATES",
    "STATE_CANDIDATE",
    "STATE_PENDING_APPROVAL",
    "STATE_APPROVED",
    "STATE_PUBLISHED",
    "STATE_REJECTED",
    "DECISIONS",
    "DECISION_PENDING",
    "DECISION_APPROVED",
    "DECISION_REJECTED",
    "REQUIREMENTS",
    "LEVEL1_REQUIREMENTS",
    "LEVEL2_REQUIREMENTS",
    "CapabilityAnchor",
    "RequirementStatus",
    "InterlockStatus",
    "PublishApprovalRecord",
    "evaluate_interlock",
    "auto_publish_permitted",
    "publish_state_of",
    "stage_pending_version",
    "approve_publish",
    "record_decision",
    "latest_decision",
    "list_decisions",
    "reset_interlock_state",
]

#: Changelog marker for a version staged by the interlock (additive, opaque;
#: never generative text — same discipline as ``evolution_loop.PROVENANCE_MARKER``).
INTERLOCK_MARKER = "inc46:publish-interlock"

# --------------------------------------------------------------------------- #
# publish state machine (INC46 §六-T15 规格)                                    #
# --------------------------------------------------------------------------- #
STATE_CANDIDATE = "candidate"
STATE_PENDING_APPROVAL = "pending_approval"
STATE_APPROVED = "approved"
STATE_PUBLISHED = "published"
STATE_REJECTED = "rejected"

#: candidate → pending_approval → approved → published；rejected 为终态。
PUBLISH_STATES: tuple[str, ...] = (
    STATE_CANDIDATE,
    STATE_PENDING_APPROVAL,
    STATE_APPROVED,
    STATE_PUBLISHED,
    STATE_REJECTED,
)

DECISION_PENDING = "pending"
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"

#: ``publish_approvals.decision`` 的合法取值（迁移 022 的 VARCHAR(16) 语义）。
DECISIONS: tuple[str, ...] = (DECISION_PENDING, DECISION_APPROVED, DECISION_REJECTED)


# --------------------------------------------------------------------------- #
# R1–R8 capability anchors (INC46 §3.3 联锁解除条件)                            #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CapabilityAnchor:
    """One interlock requirement → the capability module that will satisfy it."""

    requirement: str  # "R1" … "R8"
    task: str  # the INC46 task that delivers the capability ("T08" …)
    module: str  # forward-contract module exposing INTERLOCK_PROBE()
    description: str  # verbatim acceptance text from the task book (§3.3)


#: The eight fail-closed probes. Order is load-bearing: R1–R6 gate Level-1
#: (manual approval publish), R1–R8 gate Level-2 (auto publish).
REQUIREMENTS: tuple[CapabilityAnchor, ...] = (
    CapabilityAnchor(
        "R1", "T08", "forgeflow.skills.candidate_gate",
        "Candidate Gate / Critic 增强生效，DANGEROUS 候选阻断自动发布",
    ),
    CapabilityAnchor(
        "R2", "T13", "forgeflow.sandbox.real_isolation",
        "真实隔离沙箱（零生产副作用、越权不折算 pass）",
    ),
    CapabilityAnchor(
        "R3", "T16", "forgeflow.experience.outcome_labels",
        "outcome 标签闭环（UNKNOWN 不入成功样本）",
    ),
    CapabilityAnchor(
        "R4", "T17", "forgeflow.evaluation.golden_regression",
        "独立 Golden 回归集与泄漏防护",
    ),
    CapabilityAnchor(
        "R5", "T32", "forgeflow.experience.redaction",
        "经验脱敏（未审计不入 Miner）",
    ),
    CapabilityAnchor(
        "R6", "T33", "forgeflow.security.quarantine",
        "注入与污染防护（quarantine）",
    ),
    CapabilityAnchor(
        "R7", "T34", "forgeflow.skills.auto_rollback",
        "灰度与自动回滚",
    ),
    CapabilityAnchor(
        "R8", "T36", "forgeflow.evaluation.effect_benchmarks",
        "关键指标较基线未回退",
    ),
)

#: Level-1（人工批准发布）开放条件；Level-2（自动发布）需要全部八项。
LEVEL1_REQUIREMENTS: tuple[str, ...] = tuple(r.requirement for r in REQUIREMENTS[:6])
LEVEL2_REQUIREMENTS: tuple[str, ...] = tuple(r.requirement for r in REQUIREMENTS)

#: The probe seam: tests monkeypatch this attribute; production never overrides.
ProbeFn = Callable[[CapabilityAnchor], "RequirementStatus"]


@dataclass(frozen=True)
class RequirementStatus:
    """One requirement's probe outcome — always with an evidence reference."""

    requirement: str
    task: str
    description: str
    met: bool
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "task": self.task,
            "description": self.description,
            "met": self.met,
            "evidence": self.evidence,
        }


def _probe_capability(anchor: CapabilityAnchor) -> RequirementStatus:
    """Fail-closed capability probe for one requirement.

    Unmet unless the anchor module exists **and** exposes ``INTERLOCK_PROBE()``
    **and** the probe returns ``{"ok": True}``. Every failure mode (import
    error, missing callable, raised probe, falsy ok) is reported as unmet with
    an explicit evidence note — 无证据则注明，never an empty ``met=True``.
    """
    try:
        module = importlib.import_module(anchor.module)
    except Exception:  # noqa: BLE001 — any import failure ⇒ capability absent
        return RequirementStatus(
            anchor.requirement,
            anchor.task,
            anchor.description,
            False,
            f"无证据：能力模块 {anchor.module} 不存在（{anchor.task} 未落地）",
        )
    probe = getattr(module, "INTERLOCK_PROBE", None)
    if not callable(probe):
        return RequirementStatus(
            anchor.requirement,
            anchor.task,
            anchor.description,
            False,
            f"无证据：{anchor.module}.INTERLOCK_PROBE 未实现（{anchor.task} 未落地）",
        )
    try:
        result = probe()
    except Exception as exc:  # noqa: BLE001 — a broken probe fails closed
        return RequirementStatus(
            anchor.requirement,
            anchor.task,
            anchor.description,
            False,
            f"探针执行失败（fail-closed 视为未满足）：{exc}",
        )
    ok = bool(result.get("ok")) if isinstance(result, dict) else False
    if not ok:
        note = result.get("evidence") if isinstance(result, dict) else None
        return RequirementStatus(
            anchor.requirement,
            anchor.task,
            anchor.description,
            False,
            f"能力自报未满足：{note or '未附说明'}",
        )
    evidence = str(result.get("evidence") or "").strip()
    return RequirementStatus(
        anchor.requirement,
        anchor.task,
        anchor.description,
        True,
        evidence or "能力自报满足（未附证据引用，待补齐）",
    )


@dataclass
class InterlockStatus:
    """The full, honest interlock snapshot returned by ``GET /evolution/interlock``."""

    tenant_id: str
    auto_publish_flag: bool
    requirements: list[RequirementStatus] = field(default_factory=list)

    @property
    def level1_missing(self) -> list[str]:
        """Unmet requirement ids among R1–R6 (Level-1 开放条件的缺失项)."""
        return [
            r.requirement
            for r in self.requirements
            if r.requirement in LEVEL1_REQUIREMENTS and not r.met
        ]

    @property
    def missing(self) -> list[str]:
        """Unmet requirement ids among R1–R8 (Level-2 解除条件的缺失项)."""
        return [r.requirement for r in self.requirements if not r.met]

    @property
    def level1_open(self) -> bool:
        """人工批准发布通道是否开放（R1–R6 全满足）。"""
        return not self.level1_missing

    @property
    def released(self) -> bool:
        """联锁是否解除 = R1–R8 全满足 **且** 租户显式开启 evolution.auto_publish。"""
        return self.auto_publish_flag and not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "auto_publish_flag": self.auto_publish_flag,
            "level1_open": self.level1_open,
            "level1_missing": self.level1_missing,
            "released": self.released,
            "missing": self.missing,
            "requirements": [r.to_dict() for r in self.requirements],
        }


def evaluate_interlock(
    tenant_id: str | None,
    *,
    probe: ProbeFn | None = None,
) -> InterlockStatus:
    """Snapshot the tenant's publish-interlock status (read-only, no I/O).

    Tenant **fail-closed**: an unresolved tenant is a 403, never an empty 200
    masquerading as a lifted interlock. Every requirement is probed live; the
    flag is read from ``Settings.evolution_auto_publish`` (default False).
    """
    tenant = require_tenant(tenant_id)
    probe_fn: ProbeFn = probe or _probe_capability
    flag = bool(getattr(get_settings(), "evolution_auto_publish", False))
    return InterlockStatus(
        tenant_id=tenant,
        auto_publish_flag=flag,
        requirements=[probe_fn(anchor) for anchor in REQUIREMENTS],
    )


def auto_publish_permitted(tenant_id: str | None) -> bool:
    """Whether the evolution loop may publish WITHOUT a human (Level-2).

    True only when the tenant explicitly enabled ``evolution.auto_publish``
    **and** every R1–R8 probe reports satisfied. Default posture: False.
    """
    return evaluate_interlock(tenant_id).released


# --------------------------------------------------------------------------- #
# publish_approvals ledger (memory profile) + best-effort postgres writes      #
# --------------------------------------------------------------------------- #
@dataclass
class PublishApprovalRecord:
    """One ``publish_approvals`` row (迁移 022).

    ``approver=None`` while the decision is ``pending`` — 未发生的批准绝不伪造
    批准人（INC46 §8：未测量 ⇒ None/NULL，绝不写 0/空串）。
    """

    id: str = field(default_factory=new_id)
    tenant_id: str = ""
    skill_id: str = ""
    version: str = ""
    approver: str | None = None
    decision: str = DECISION_PENDING
    reason: str | None = None
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "skill_id": self.skill_id,
            "version": self.version,
            "approver": self.approver,
            "decision": self.decision,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
        }


#: In-process ledger — authoritative for the memory/offline profile and the
#: unit-test seam (mirrors ``evolution_loop._LEDGER``'s discipline).
_APPROVALS: list[PublishApprovalRecord] = []


def reset_interlock_state() -> None:
    """Reset the in-process approvals ledger. Test helper only."""
    _APPROVALS.clear()


def _backend() -> str:
    try:
        return str(getattr(get_settings(), "storage_backend", "") or "").lower()
    except Exception:  # noqa: BLE001 — a gate read must never break the interlock
        return ""


async def _pg_insert_decision(record: PublishApprovalRecord) -> None:
    """Best-effort insert into ``publish_approvals`` (postgres profile only).

    A failed write never blocks or fakes a decision — the in-process ledger
    stays authoritative within the process (same discipline as
    ``evolution_loop._persist_provenance``).
    """
    if _backend() != "postgres":
        return
    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO publish_approvals
                    (tenant_id, skill_id, version, approver, decision, reason, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                record.tenant_id,
                record.skill_id,
                record.version,
                record.approver,
                record.decision,
                record.reason,
                record.created_at,
            )
    except Exception:  # noqa: BLE001 — audit persistence is best-effort here
        logger.debug("INC46 interlock: publish_approvals insert skipped", exc_info=True)


async def _pg_latest_decision(tenant: str, skill_id: str, version: str) -> str | None:
    """Newest decision for (tenant, skill, version) from postgres, if reachable."""
    if _backend() != "postgres":
        return None
    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT decision FROM publish_approvals
                WHERE tenant_id = $1 AND skill_id = $2 AND version = $3
                ORDER BY created_at DESC LIMIT 1
                """,
                tenant,
                skill_id,
                version,
            )
        return str(value) if value else None
    except Exception:  # noqa: BLE001 — unreadable audit ⇒ fall back to the ledger
        logger.debug("INC46 interlock: publish_approvals read skipped", exc_info=True)
        return None


async def record_decision(
    tenant_id: str | None,
    skill_id: str,
    version: str,
    approver: str | None,
    decision: str,
    reason: str | None,
) -> PublishApprovalRecord:
    """Append one publish decision to the ledger (+ best-effort postgres).

    Tenant fail-closed; ``decision`` must be one of :data:`DECISIONS` — an
    unknown decision is a programming error, not a state.
    """
    tenant = require_tenant(tenant_id)
    if decision not in DECISIONS:
        raise ValueError(f"unknown publish decision: {decision!r}")
    record = PublishApprovalRecord(
        tenant_id=tenant,
        skill_id=str(skill_id),
        version=str(version),
        approver=approver,
        decision=decision,
        reason=reason,
    )
    _APPROVALS.append(record)
    await _pg_insert_decision(record)
    return record


async def list_decisions(
    tenant_id: str | None,
    skill_id: str | None = None,
    version: str | None = None,
) -> list[PublishApprovalRecord]:
    """Ledger rows for a tenant (optionally narrowed), oldest first."""
    tenant = require_tenant(tenant_id)
    rows = [r for r in _APPROVALS if r.tenant_id == tenant]
    if skill_id is not None:
        rows = [r for r in rows if r.skill_id == str(skill_id)]
    if version is not None:
        rows = [r for r in rows if r.version == str(version)]
    return sorted(rows, key=lambda r: r.created_at)


async def latest_decision(tenant_id: str | None, skill_id: str, version: str) -> str | None:
    """The newest decision for (tenant, skill, version), ``None`` if never decided.

    In-process ledger ⊕ best-effort postgres read; the newer ``created_at``
    wins so a decision written by another process is not silently ignored.
    """
    tenant = require_tenant(tenant_id)
    rows = await list_decisions(tenant, skill_id, version)
    decision = rows[-1].decision if rows else None
    pg = await _pg_latest_decision(tenant, str(skill_id), str(version))
    if pg in DECISIONS:
        # The pg row carries no clock here; prefer it only when the ledger has
        # nothing — within one process the ledger is always at least as fresh.
        decision = decision or pg
    return decision


# --------------------------------------------------------------------------- #
# state derivation + staging + approval                                       #
# --------------------------------------------------------------------------- #
def publish_state_of(skill: Any, version: Any, latest: str | None) -> str:
    """Derive a version's publish state — the state machine, never a guess.

    ``published`` is derived from the skill's ``current_version`` pointer (the
    one observable fact of "took effect"), the rest from the newest recorded
    decision. A version with no decision and no effect is a plain ``candidate``.
    """
    current = str(getattr(skill, "current_version", "") or "")
    if current and str(getattr(version, "semver", "") or "") == current:
        return STATE_PUBLISHED
    if latest == DECISION_REJECTED:
        return STATE_REJECTED
    if latest == DECISION_APPROVED:
        return STATE_APPROVED
    if latest == DECISION_PENDING:
        return STATE_PENDING_APPROVAL
    return STATE_CANDIDATE


async def stage_pending_version(
    tenant_id: str | None,
    skill: Any,
    candidate: Any,
    *,
    actor: str,
    eval_score: float | None,
    skill_repo: Any,
) -> Any:
    """Mint the regression-passing candidate as a **pending_approval** version.

    This is the T15 replacement for T05's automatic ``promote_candidate``: the
    new semver is recorded (``publish=False`` — ``current_version`` does **not**
    move), additively tagged with :data:`INTERLOCK_MARKER`, and a ``pending``
    decision row is written so ``approve_publish`` has an auditable trail.

    Honesty: ``approved_by=None`` — nothing was approved yet. The incumbent is
    never touched here (红线 6：不覆盖历史版本).

    Version identity (INC46 T15 B2): the new semver is bumped from the skill's
    **maximum existing** semver — pending and rejected rows included — not from
    ``skill.current_version``. ``current_version`` does not move while a
    candidate is pending/rejected, so basing the bump on it minted the *same*
    semver on every restage: a duplicate ``(skill_id, semver)`` that the memory
    store mis-resolves and the postgres ``UNIQUE(skill_id, semver)`` rejects,
    which would leave the skill permanently un-stageable. Anchoring on the
    maximum makes restaging monotonic: a rejected semver is terminal and can
    never be revived or overwritten, and the deterministic loop below pre-empts
    any collision instead of relying on a uniqueness exception.
    """
    from forgeflow.skills.versioning import bump_semver, create_version, parse_semver

    tenant = require_tenant(tenant_id)
    candidate_id = str(getattr(candidate, "id", "") or "")
    skill_id = str(getattr(skill, "id", "") or "")
    existing = await skill_repo.list_versions(tenant, skill_id)
    taken = [str(getattr(v, "semver", "") or "") for v in existing]
    # B2 基准（含 QA p11 加固）：max(现存 semver ∪ {current_version})。
    # 正常 API 流下 current_version 总指向现存行，但迁移遗留 / 手工改库可能令指针
    # 指向**缺失**的版本行且其 semver 高于现存行最大值——若只取现存行最大值，会铸出
    # 低于指针的 semver，批准时把 current_version 回退。并入 current_version 保证
    # 单调不回退，同时仍以现存行最大值为准避免复用（pending/rejected 计入）。
    current = str(getattr(skill, "current_version", "") or "")
    baseline_candidates = taken + ([current] if current else [])
    base = max(baseline_candidates, key=parse_semver) if baseline_candidates else "0.0.0"
    next_semver = bump_semver(base, "minor")
    taken_set = set(taken)
    while next_semver in taken_set:  # deterministic: never silently reuse a semver
        next_semver = bump_semver(next_semver, "minor")
    version = await create_version(
        skill_repo,
        tenant,
        skill,
        dict(getattr(candidate, "draft_spec", None) or {}),
        bump="minor",
        actor=actor,
        summary=f"由候选 {candidate_id[:8]} 固化（发布联锁：待人工批准）",
        approved_by=None,  # 未批准 ⇒ None，绝不伪造批准人
        eval_score=eval_score,
        source_experience_ids=list(getattr(candidate, "experience_ids", None) or []),
        publish=False,
        target_semver=next_semver,
    )
    version.release_state = STATE_PENDING_APPROVAL
    version.changelog = (version.changelog or "") + f"\n- {INTERLOCK_MARKER} pending_approval"
    await record_decision(
        tenant,
        skill_id,
        version.semver,
        None,  # pending ⇒ 无批准人
        DECISION_PENDING,
        "回归通过；发布联锁未解除，候选版本进入 pending_approval，等待人工批准",
    )
    return version


async def _audit_publish_decision(
    *,
    tenant_id: str,
    actor: str,
    actor_role: str,
    skill_name: str,
    version: str,
    decision: str,
    reason: str,
) -> None:
    """Record one interlock decision on the **existing** audit sink (best-effort).

    Same single sink (``middleware.audit.write_audit_entry``) the release gate
    and canary lifecycle use, so ``/audit`` answers for publish approvals
    identically — no new audit component (可审计, INC46 §六-T15 目标).
    """
    from forgeflow.middleware.audit import write_audit_entry

    await write_audit_entry(
        {
            "user_id": actor,
            "role": actor_role,
            "action": "skill.publish_interlock",
            "resource": "skills",
            "resource_id": skill_name,
            "outcome": "allowed" if decision == DECISION_APPROVED else "denied",
            "workspace_id": tenant_id,
            "metadata": {
                "skill": skill_name,
                "version": version,
                "decision": decision,
                "reason": reason,
            },
        }
    )


async def approve_publish(
    tenant_id: str | None,
    skill_id: str,
    semver: str,
    *,
    approver: str,
    approver_role: str = "manager",
    reason: str | None = None,
    skill_repo: Any | None = None,
    probe: ProbeFn | None = None,
) -> dict[str, Any]:
    """Level-1: a human with publish permission approves a **pending** version.

    Gates, in order (every refusal is explicit — 失败表现必须是显式 403/原因):

    1. tenant fail-closed (403);
    2. approver must hold ``approve:skills`` (403);
    3. skill / version must exist **for this tenant** (404 — cross-tenant reads
       nothing);
    4. the version must be in ``pending_approval`` (409 otherwise; ``rejected``
       是终态);
    5. Level-1 开放条件：R1–R6 全满足（403，列出缺失项）;
    6. regression re-check against the incumbent's recorded score — 批准一个
       回归未通过的候选一律拒绝，写 ``rejected`` 记录，incumbent 保留 (403)。

    On success the decision is recorded (``approved``), ``current_version``
    moves to the approved semver and the version is marked ``promoted``.

    Raises:
        GovernanceError(403/404/409): every refusal, with the concrete reason.
    """
    from forgeflow.rbac.enforcer import RBACEnforcer
    from forgeflow.skills.release_gate import baseline_from_version, evaluate_release

    tenant = require_tenant(tenant_id)

    # 2. 批准人须有发布权限（approve:skills）。
    if not RBACEnforcer().check(approver_role, "approve", "skills"):
        raise GovernanceError(
            f"角色 '{approver_role}' 无发布权限（approve:skills），批准被拒绝",
            status_code=403,
        )

    sk_repo = skill_repo or get_skill_repository()

    # 3. 跨租户 fail-closed：读不到 ⇒ 404，绝不跨租户放行。
    skill = await sk_repo.get_skill(tenant, skill_id)
    if skill is None:
        raise GovernanceError("skill not found", status_code=404)
    version = await sk_repo.get_version(tenant, skill_id, semver)
    if version is None:
        raise GovernanceError(f"version {semver} not found", status_code=404)

    # 4. 状态机：只允许从 pending_approval 迁移；rejected 为终态。
    latest = await latest_decision(tenant, skill_id, semver)
    state = publish_state_of(skill, version, latest)
    if state == STATE_PUBLISHED:
        raise GovernanceError(
            f"版本 {semver} 已是当前发布版本，无需重复批准", status_code=409
        )
    if state == STATE_REJECTED:
        raise GovernanceError(
            f"版本 {semver} 已被拒绝（rejected 为终态），不可再批准", status_code=409
        )
    if state != STATE_PENDING_APPROVAL:
        raise GovernanceError(
            f"版本 {semver} 不在待批准状态（state={state}），仅 pending_approval 可批准",
            status_code=409,
        )

    # 5. Level-1 开放条件（R1–R6）。未开放 ⇒ 显式 403 + 缺失项，绝不空放行。
    interlock = evaluate_interlock(tenant, probe=probe)
    if not interlock.level1_open:
        raise GovernanceError(
            "发布联锁未解除（Level-1 未开放）：缺失 "
            + "、".join(interlock.level1_missing)
            + "；候选保持 pending_approval，incumbent 保留",
            status_code=403,
        )

    # 6. 回归复核（B1 fail-closed）：批准一个回归未通过的候选一律拒绝，incumbent 保留。
    #    两道显式闸门，绝不把「未做比较」当作「无基线放行」：
    #      ① 候选版本必须带真实评估 —— 无评估 ⇒ 无任何回归证据 ⇒ 拒绝；
    #      ② incumbent 有基线时，回归结果不得是 no_baseline（必须真的比较过）。
    incumbent = await sk_repo.get_version(tenant, skill_id, skill.current_version or "")
    baseline = baseline_from_version(incumbent)

    if version.eval_score is None:
        refusal = "候选无评估（candidate has no evaluation），无回归证据，拒绝发布"
        await record_decision(
            tenant, skill_id, semver, approver, DECISION_REJECTED, refusal
        )
        await _audit_publish_decision(
            tenant_id=tenant,
            actor=approver,
            actor_role=approver_role,
            skill_name=str(getattr(skill, "name", "") or ""),
            version=semver,
            decision=DECISION_REJECTED,
            reason=refusal,
        )
        raise GovernanceError(
            f"候选版本 {semver} 无任何评估，拒绝发布（candidate has no evaluation；"
            "incumbent 保留，applied=False）",
            status_code=403,
        )

    new_metrics = {"score": float(version.eval_score)}
    release = evaluate_release(new_metrics, baseline)

    if release.baseline_present and release.severity == "no_baseline":
        refusal = "无基线可比，未做比较，拒绝发布"
        await record_decision(
            tenant, skill_id, semver, approver, DECISION_REJECTED, refusal
        )
        await _audit_publish_decision(
            tenant_id=tenant,
            actor=approver,
            actor_role=approver_role,
            skill_name=str(getattr(skill, "name", "") or ""),
            version=semver,
            decision=DECISION_REJECTED,
            reason=refusal,
        )
        raise GovernanceError(
            "incumbent 存在基线但候选无可比评估指标：未做比较，拒绝发布"
            "（incumbent 保留，applied=False）",
            status_code=403,
        )

    if not release.allowed:
        await record_decision(
            tenant,
            skill_id,
            semver,
            approver,
            DECISION_REJECTED,
            f"回归复核未通过：{release.reason}",
        )
        await _audit_publish_decision(
            tenant_id=tenant,
            actor=approver,
            actor_role=approver_role,
            skill_name=str(getattr(skill, "name", "") or ""),
            version=semver,
            decision=DECISION_REJECTED,
            reason=release.reason,
        )
        raise GovernanceError(
            f"回归复核未通过，拒绝发布（incumbent 保留，applied=False）：{release.reason}",
            status_code=403,
        )

    # 批准：先落记录（每次发布写 approval 记录），再生效。
    approval = await record_decision(
        tenant,
        skill_id,
        semver,
        approver,
        DECISION_APPROVED,
        reason or "人工批准发布（Level-1）",
    )
    previous_version = str(getattr(skill, "current_version", "") or "") or None
    skill.current_version = semver
    skill.updated_at = utcnow()
    await sk_repo.update_skill(skill)
    version.release_state = STATE_PUBLISHED
    version.approved_by = approver
    await _audit_publish_decision(
        tenant_id=tenant,
        actor=approver,
        actor_role=approver_role,
        skill_name=str(getattr(skill, "name", "") or ""),
        version=semver,
        decision=DECISION_APPROVED,
        reason=approval.reason or "",
    )
    return {
        "tenant_id": tenant,
        "skill_id": skill_id,
        "version": semver,
        "state": STATE_PUBLISHED,
        "previous_version": previous_version,
        "approval": approval.to_dict(),
        "regression": release.to_dict(),
        "reason": f"人工批准发布：{previous_version or '(none)'} → {semver}",
    }
