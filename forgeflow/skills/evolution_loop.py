"""INC46 T05 — the **Evolution** closed loop (auto-reflux + cap + idempotency).

This module closes the loop the design describes (INC46 §1.5 / §7-T05 / §4.1):
a skill that keeps failing in *real* runs auto-generates a new experience, a new
candidate, a new semver version and a regression check — **without overwriting**
the incumbent, **with a hard generation cap** and **idempotently**.

It is deliberately an **orchestration-only** module: every step reuses an
existing, already-tested piece and nothing is re-implemented here.

=======================  ===================================================
reused (already-tested)  role
=======================  ===================================================
``runtime.trace_store``  T01 — read the **real** run trace per tenant
``experience.extractor`` T01/② — the ExperienceRecord field conventions
``skills.pattern_miner`` T02 — recomputable Experience → Pattern
``skills.rule_assets``   T02 — deterministic MUST / MUST_NOT rules (zero LLM)
``candidate_compiler``   ③④ — compile a candidate from experiences
``skills.engineering``   run_engineering_loop — ④→⑤→⑥→⑦ (repair ≤3)
``governance_gate``      promote_candidate — semver bump, **never overwrites**
``release_gate``         evaluate_release — old-vs-new regression check
``versioning``           create_version (via the governance gate)
=======================  ===================================================

Closed loop (INC46 §1.5)
------------------------
``①`` read the skill's failing runs from the T01 trace
(``run_steps.status`` ∈ ``error`` / ``unavailable`` / ``refused`` **or** a
failed ``verification``) ⇒ ``failure_modes``;
``②`` fold each failing run into an ``ExperienceRecord`` (the extractor's field
shape, **additively** tagged with ``failure_mode``);
``③`` build a deterministic improvement **proposal** from T02's
pattern/rules (zero LLM);
``④`` ``compile_candidate`` (reuse);
``⑤`` ``run_engineering_loop`` + ``governance_gate.promote_candidate`` (reuse) ⇒
a new semver that does **not** overwrite the incumbent;
``⑥`` ``release_gate.evaluate_release`` (reuse) — a regression ⇒ **no publish**,
the incumbent stays, ``applied=False``.

Anti-runaway guards (INC46 §1.5)
--------------------------------
* **Trigger** — only after ``EVOLVE_TRIGGER_MIN_FAILURES`` failing runs in the
  window;
* **Cooldown** — one auto-evolution per skill per ``EVOLVE_COOLDOWN_HOURS``;
* **Cap** — at ``MAX_EVOLVE_GENERATIONS`` auto-bumps it stops and hands the
  skill to a human (``advise`` ⇒ an ``agent_approvals`` row);
* **Idempotency** — the key ``(tenant, skill_id, from_version, window_key)``
  maps to the already-decided outcome, so a repeat call mints **zero** new
  versions (the ``evolution._CACHE`` shape, keyed on the failure window).

Honesty red line (INC46 §8)
---------------------------
* unmeasured ⇒ ``None`` never ``0`` (a run with no failures has ``None``
  ``from_version`` behaviour, never a fabricated version);
* ``error`` is **never** folded into a success — a failing run becomes a
  ``failure`` experience (``outcome="failure"``), never a pass;
* tenant **fail-closed** — an unresolved tenant is a 403 *before* any read, and
  a skill the tenant does not own reports ``triggered=False``.

The generation count uses the additive ``skill_versions.created_from_runs``
column (migration ``021``) when the postgres profile is active, and an
in-process ledger otherwise (the offline/memory profile); both are monotonic and
recomputable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories import (
    get_experience_repository,
    get_policy_repository,
    get_skill_candidate_repository,
    get_skill_repository,
)
from forgeflow.repositories.base import utcnow
from forgeflow.skills.pattern_miner import (
    FAILURE_STATUSES,
    run_id_of,
    step_index,
    step_status,
    step_tool,
)
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)

__all__ = [
    "EVOLVE_TRIGGER_MIN_FAILURES",
    "EVOLVE_COOLDOWN_HOURS",
    "MAX_EVOLVE_GENERATIONS",
    "EVOLVE_WINDOW_DAYS",
    "PROVENANCE_MARKER",
    "EvolutionOutcome",
    "maybe_evolve",
    "collect_skill_failures",
    "reset_evolution_state",
    "record_generation",
    "record_evolution_time",
]

#: A skill auto-bumps only once its window holds at least this many failing runs.
EVOLVE_TRIGGER_MIN_FAILURES = 3

#: One auto-evolution per skill per this many hours (anti-thrash).
EVOLVE_COOLDOWN_HOURS = 24

#: Hard ceiling on *automatic* semver bumps per skill (INC46 §1.5). At the cap
#: the loop stops and hands the skill to a human (``advise``).
MAX_EVOLVE_GENERATIONS = 3

#: The look-back window (days) for collecting the skill's failing runs.
EVOLVE_WINDOW_DAYS = 30

#: Provenance marker written into ``skill_versions`` for an auto-evolved version
#: (belt-and-braces alongside ``created_from_runs``); never generative text.
PROVENANCE_MARKER = "inc46:auto-evolve"


# --------------------------------------------------------------------------- #
# process-level ledgers (test seams + offline/memory provenance)               #
# --------------------------------------------------------------------------- #
#: Idempotency ledger: ``(tenant, skill_id, from_version, window_key)`` → the
#: already-decided :class:`EvolutionOutcome` (mirrors ``evolution._CACHE``).
_LEDGER: dict[tuple[str, str, str, str], "EvolutionOutcome"] = {}

#: Generation ledger: ``(tenant, skill_id)`` → the semvers auto-bumped so far.
_GENERATIONS: dict[tuple[str, str], list[str]] = {}

#: Last auto-evolution clock: ``(tenant, skill_id)`` → the moment it applied.
_LAST_EVOLVE: dict[tuple[str, str], datetime] = {}


def reset_evolution_state() -> None:
    """Reset every in-process evolution ledger. Test helper only."""
    _LEDGER.clear()
    _GENERATIONS.clear()
    _LAST_EVOLVE.clear()


def record_generation(tenant_id: str, skill_id: str, semver: str) -> None:
    """Register one auto-evolved generation on the in-process ledger (test seam).

    Production calls this through :func:`_persist_provenance` (which also writes
    the postgres ``created_from_runs`` column when that profile is active).
    """
    _GENERATIONS.setdefault((tenant_id, skill_id), []).append(str(semver))


def record_evolution_time(tenant_id: str, skill_id: str, when: datetime) -> None:
    """Register the last auto-evolution moment on the in-process ledger (test seam)."""
    moment = _as_utc(when) or utcnow()
    _LAST_EVOLVE[(tenant_id, skill_id)] = moment


# --------------------------------------------------------------------------- #
# value object                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class EvolutionOutcome:
    """The full, honest record of one ``maybe_evolve`` decision (INC46 §3.2)."""

    triggered: bool = False
    reason: str = ""
    failure_modes: list[str] = field(default_factory=list)
    new_experience_id: str | None = None
    candidate_id: str | None = None
    from_version: str | None = None
    to_version: str | None = None
    regression: dict[str, Any] = field(default_factory=dict)
    applied: bool = False
    # --- additive, recomputable bookkeeping --------------------------------- #
    window_key: str = ""
    generation: int = 0
    proposal: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "reason": self.reason,
            "failure_modes": list(self.failure_modes),
            "new_experience_id": self.new_experience_id,
            "candidate_id": self.candidate_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "regression": dict(self.regression),
            "applied": self.applied,
            "window_key": self.window_key,
            "generation": self.generation,
            "proposal": dict(self.proposal),
        }


# --------------------------------------------------------------------------- #
# small helpers                                                                #
# --------------------------------------------------------------------------- #
def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an attribute-bearing object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_utc(value: Any) -> datetime | None:
    """Normalise ``value`` to a timezone-aware UTC datetime (``None`` if not one)."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _in_window(created_at: Any, window_days: int) -> bool:
    """Whether ``created_at`` (ISO-8601) is within the last ``window_days``.

    A missing/unparseable timestamp is **kept** (a missing clock reading is not
    evidence that a run is out of window) — the same discipline T02's miner uses.
    """
    if window_days <= 0:
        return True
    if not isinstance(created_at, str) or not created_at.strip():
        return True
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment >= datetime.now(timezone.utc) - timedelta(days=window_days)


def _step_verification(step: Any) -> dict[str, Any] | None:
    value = _attr(step, "verification")
    return value if isinstance(value, dict) else None


def _verification_failed(verification: dict[str, Any] | None) -> bool:
    """Whether a step's ``verification`` JSONB evidences a failure (never guesses)."""
    if not isinstance(verification, dict):
        return False
    if verification.get("passed") is False or verification.get("ok") is False:
        return True
    status = str(verification.get("status", "")).lower()
    return status in ("fail", "failed", "error")


def _is_failing_step(step: Any) -> bool:
    """A step fails when its status is a hard-failure status OR its verification did."""
    return step_status(step) in FAILURE_STATUSES or _verification_failed(
        _step_verification(step)
    )


def _step_failure_reason(step: Any) -> str | None:
    """The verbatim failure mode for one failing step (``None`` when not failing)."""
    tool = step_tool(step)
    status = step_status(step)
    if status in FAILURE_STATUSES:
        return f"{tool} 执行失败（status={status}）"
    verification = _step_verification(step)
    if _verification_failed(verification):
        detail = str(
            (verification or {}).get("reason")
            or (verification or {}).get("detail")
            or (verification or {}).get("status")
            or "verification"
        )
        return f"{tool} 校验未通过（{detail}）"
    return None


def _group_by_run(steps: list[Any]) -> dict[str, list[Any]]:
    """Regroup tenant steps by ``run_id`` (ascending ``step_index``)."""
    by_run: dict[str, list[Any]] = {}
    for step in steps:
        by_run.setdefault(run_id_of([step]), []).append(step)
    for run in by_run.values():
        run.sort(key=step_index)
    return by_run


def _skill_tools_from_spec(spec: Any) -> list[str]:
    tools = spec.get("tools", []) if isinstance(spec, dict) else []
    return [str(t) for t in (tools or []) if t]


async def _current_skill_tools(skill_repo: Any, tenant: str, skill: Any) -> list[str]:
    """The tools declared by the skill's **current** version (may be empty)."""
    semver = getattr(skill, "current_version", None) or ""
    version = await skill_repo.get_version(tenant, getattr(skill, "id", ""), semver)
    return _skill_tools_from_spec(getattr(version, "spec", None) if version else None)


def _window_key(failing: list[dict[str, Any]], window_days: int) -> str:
    """Deterministic key over the *set* of failing run ids in the window."""
    ids = "|".join(f["run_id"] for f in sorted(failing, key=lambda f: f["run_id"]))
    digest = hashlib.sha1(f"{ids}|w{int(window_days)}".encode("utf-8")).hexdigest()
    return digest[:16]


# --------------------------------------------------------------------------- #
# ① read the skill's failing runs from the T01 trace                           #
# --------------------------------------------------------------------------- #
async def collect_skill_failures(
    tenant_id: str,
    *,
    skill_tools: list[str],
    window_days: int = EVOLVE_WINDOW_DAYS,
) -> list[dict[str, Any]]:
    """Group the tenant's trace into this skill's **failing** runs.

    Tenant **fail-closed**: an unresolved tenant reads nothing. Each returned
    entry is ``{"run_id", "steps", "modes"}`` where ``modes`` is the sorted,
    de-duplicated set of verbatim failure reasons. A run counts only when it
    carries at least one failing step; when the skill declares no tools we cannot
    attribute runs to it, so every failing run is considered (documented).
    """
    if not tenant_id:
        return []

    from forgeflow.runtime.trace_store import list_for_tenant

    steps = await list_for_tenant(tenant_id)
    failing: list[dict[str, Any]] = []
    for run_id, run in _group_by_run(steps).items():
        if not any(_in_window(_attr(s, "created_at"), window_days) for s in run):
            continue
        if skill_tools and not any(step_tool(s) in skill_tools for s in run):
            continue
        modes: list[str] = []
        for step in run:
            if skill_tools and step_tool(step) not in skill_tools:
                continue
            reason = _step_failure_reason(step)
            if reason:
                modes.append(reason)
        if modes:
            failing.append({"run_id": run_id, "steps": run, "modes": sorted(set(modes))})
    failing.sort(key=lambda f: f["run_id"])
    return failing


# --------------------------------------------------------------------------- #
# ② fold failing runs into experiences, ③ build the improvement proposal       #
# --------------------------------------------------------------------------- #
def _fold_failure_experience(tenant: str, skill: Any, failure: dict[str, Any]) -> Any:
    """Fold one failing run into an ``ExperienceRecord`` (extractor field shape).

    Reuses ``forgeflow.experience.extractor``'s field conventions
    (``reusable_steps`` = ``{index, tool, step_type, note, outcome}``, a
    non-empty ``outcome``) and **additively** tags the failure: each step
    carries a ``failure_mode`` key and the record's ``tags`` gain a
    ``failure_mode:<mode>`` entry. The outcome is ``"failure"`` — an ``error``
    is never folded into a success (INC46 §8).
    """
    from forgeflow.experience.embedding import deterministic_embedding
    from forgeflow.experience.models import ExperienceRecord

    modes: list[str] = list(failure.get("modes", []))
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(failure.get("steps", [])):
        tool = step_tool(step)
        mode = next((m for m in modes if m.startswith(tool)), "")
        steps.append(
            {
                "index": index,
                "tool": tool,
                "step_type": str(_attr(step, "step_type", "tool") or "tool"),
                "note": f"失败模式：{mode}" if mode else "",
                "outcome": "failure",
                "failure_mode": mode,
            }
        )
    run_id = str(failure.get("run_id", ""))
    domain = str(getattr(skill, "domain", "") or "general")
    summary = f"{getattr(skill, 'name', '')} 运行 {run_id[:8]} 失败：" + "；".join(modes)
    tags = [domain, PROVENANCE_MARKER, *[f"failure_mode:{m}" for m in modes]]
    return ExperienceRecord(
        tenant_id=tenant,
        run_id=run_id,
        summary=summary,
        outcome="failure",
        reusable_steps=steps,
        tags=tags,
        embedding=deterministic_embedding(f"{summary} {' '.join(tags)}"),
    )


def _build_proposal(failing: list[dict[str, Any]]) -> dict[str, Any]:
    """③ deterministic improvement proposal from T02's pattern / rules (no LLM)."""
    from forgeflow.skills.pattern_miner import mine_patterns
    from forgeflow.skills.rule_assets import (
        RULE_MIN_CONFIDENCE,
        RULE_MIN_SUPPORT,
        extract_rules,
    )

    runs = [list(f.get("steps", [])) for f in failing]
    patterns = mine_patterns(runs, min_support=1)
    rules = extract_rules(
        patterns, runs, min_support=RULE_MIN_SUPPORT, min_confidence=RULE_MIN_CONFIDENCE
    )
    best = patterns[0] if patterns else None
    return {
        "pattern_key": best.key if best else "",
        "tools": list(best.tools) if best else [],
        "pattern_count": len(patterns),
        "rules": [r.to_dict() for r in rules],
        "rule_count": len(rules),
        "source_run_ids": [f["run_id"] for f in failing],
    }


# --------------------------------------------------------------------------- #
# generation / cooldown provenance                                             #
# --------------------------------------------------------------------------- #
def _backend() -> str:
    try:
        return str(getattr(get_settings(), "storage_backend", "") or "").lower()
    except Exception:  # noqa: BLE001 — a gate read must never break evolution
        return ""


async def _pg_generation_count(tenant: str, skill_id: str) -> int:
    """Count auto-evolved versions via the ``created_from_runs`` column (pg only).

    Best-effort: any error (no pool, non-UUID tenant on a UUID column, …) yields
    ``0`` — the in-process ledger still carries the count, so the cap can never
    silently disappear.
    """
    if _backend() != "postgres":
        return 0
    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT count(*) FROM skill_versions
                WHERE tenant_id IS NOT DISTINCT FROM $1::uuid
                  AND skill_id = $2::uuid
                  AND created_from_runs IS NOT NULL
                  AND jsonb_typeof(created_from_runs) = 'array'
                  AND jsonb_array_length(created_from_runs) > 0
                """,
                tenant,
                skill_id,
            )
        return int(value or 0)
    except Exception:  # noqa: BLE001 — provenance is best-effort, never a gate
        logger.debug("INC46 evolve: pg generation count skipped", exc_info=True)
        return 0


async def _generation_count(tenant: str, skill_id: str) -> int:
    """Auto-evolved generations for a skill (in-process ledger ⊕ pg provenance)."""
    ledger = len(_GENERATIONS.get((tenant, skill_id), []))
    return max(ledger, await _pg_generation_count(tenant, skill_id))


async def _persist_provenance(
    tenant: str, skill_id: str, semver: str, run_ids: list[str], when: datetime
) -> None:
    """Record an auto-evolved version: ledger + (best-effort) pg column."""
    record_generation(tenant, skill_id, semver)
    _LAST_EVOLVE[(tenant, skill_id)] = _as_utc(when) or utcnow()
    if _backend() != "postgres":
        return
    try:
        from forgeflow.database import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE skill_versions SET created_from_runs = $1::jsonb
                WHERE tenant_id IS NOT DISTINCT FROM $2::uuid
                  AND skill_id = $3::uuid AND semver = $4
                """,
                json.dumps(list(run_ids), ensure_ascii=False),
                tenant,
                skill_id,
                semver,
            )
    except Exception:  # noqa: BLE001 — provenance is additive, never load-bearing
        logger.debug("INC46 evolve: provenance column write skipped", exc_info=True)


def _is_evolved_version(version: Any) -> bool:
    if getattr(version, "created_from_runs", None):
        return True
    return PROVENANCE_MARKER in str(getattr(version, "changelog", "") or "")


async def _last_evolution_at(skill_repo: Any, tenant: str, skill_id: str) -> datetime | None:
    """The moment of the skill's most recent auto-evolution, if any."""
    moments: list[datetime] = []
    ledger = _LAST_EVOLVE.get((tenant, skill_id))
    if ledger is not None:
        moments.append(ledger)
    try:
        versions = await skill_repo.list_versions(tenant, skill_id)
    except Exception:  # noqa: BLE001 — a provenance read must never break the loop
        versions = []
    for version in versions:
        if _is_evolved_version(version):
            moment = _as_utc(getattr(version, "created_at", None))
            if moment is not None:
                moments.append(moment)
    return max(moments) if moments else None


async def _advise_cap_reached(
    tenant: str, skill_id: str, skill_name: str, policy_repo: Any
) -> None:
    """Hand a capped skill to a human via the existing approval queue (reuse §1.5)."""
    from forgeflow.skills.evolution import EvolutionAdvice, submit_advice_for_approval

    advice = EvolutionAdvice(
        tenant_id=tenant,
        skill_id=skill_id,
        skill_name=skill_name,
        kind="optimize",
        reason=(
            f"已达自动升版代际上限（{MAX_EVOLVE_GENERATIONS}），"
            "停止自动升版并转人工审批"
        ),
        confidence=0.6,
        source="rule",
    )
    await submit_advice_for_approval(tenant, [advice], policy_repo=policy_repo)


# --------------------------------------------------------------------------- #
# the loop                                                                     #
# --------------------------------------------------------------------------- #
async def maybe_evolve(
    tenant_id: str | None,
    skill_id: str,
    *,
    actor: str,
    actor_role: str = "manager",
    now: datetime | None = None,
    **repos: Any,
) -> EvolutionOutcome:
    """Run the Evolution closed loop for one skill — guarded and idempotent.

    Tenant **fail-closed** (403 on an unresolved tenant). ``repos`` may carry any
    of ``skill_repo`` / ``candidate_repo`` / ``experience_repo`` / ``policy_repo``
    (tests, callers that already hold them) and, additively, ``window_days``.

    Returns an :class:`EvolutionOutcome` describing exactly what happened —
    never a fabricated success. A ``could-not-apply`` (regression / insufficient
    experiences / engineering degradation) is reported with ``applied=False`` and
    the incumbent version untouched.
    """
    tenant = require_tenant(tenant_id)  # BE-5 — fail closed before any read
    moment = _as_utc(now) or utcnow()

    skill_repo = repos.get("skill_repo") or get_skill_repository()
    cand_repo = repos.get("candidate_repo") or get_skill_candidate_repository()
    exp_repo = repos.get("experience_repo") or get_experience_repository()
    pol_repo = repos.get("policy_repo") or get_policy_repository()
    window_days = int(repos.get("window_days", EVOLVE_WINDOW_DAYS))

    skill = await skill_repo.get_skill(tenant, skill_id)
    if skill is None:
        return EvolutionOutcome(
            triggered=False,
            reason="该租户下不存在该 skill，无失败轨迹可回流",
        )

    from_version = str(getattr(skill, "current_version", "") or "")
    skill_tools = await _current_skill_tools(skill_repo, tenant, skill)

    failing = await collect_skill_failures(
        tenant, skill_tools=skill_tools, window_days=window_days
    )
    failure_modes = sorted({mode for f in failing for mode in f["modes"]})
    window_key = _window_key(failing, window_days)

    # --- idempotency: same (tenant, skill, from_version, window) ⇒ same outcome #
    idem_key = (tenant, skill_id, from_version, window_key)
    cached = _LEDGER.get(idem_key)
    if cached is not None:
        return cached

    generations = await _generation_count(tenant, skill_id)

    def _remember(outcome: EvolutionOutcome) -> EvolutionOutcome:
        _LEDGER[idem_key] = outcome
        return outcome

    # --- cap: stop auto-bumping, hand to a human (INC46 §1.5) ---------------- #
    if generations >= MAX_EVOLVE_GENERATIONS:
        await _advise_cap_reached(tenant, skill_id, str(getattr(skill, "name", "")), pol_repo)
        return _remember(
            EvolutionOutcome(
                triggered=True,
                reason=(
                    f"已达自动升版代际上限（{generations}/{MAX_EVOLVE_GENERATIONS}），"
                    "停止自动升版并转人工审批"
                ),
                failure_modes=failure_modes,
                from_version=from_version,
                window_key=window_key,
                generation=generations,
            )
        )

    # --- cooldown: one auto-evolution per skill per window (not cached) ------- #
    last = await _last_evolution_at(skill_repo, tenant, skill_id)
    if last is not None and (moment - last) < timedelta(hours=EVOLVE_COOLDOWN_HOURS):
        return EvolutionOutcome(
            triggered=False,
            reason=(
                f"冷却期内（{EVOLVE_COOLDOWN_HOURS}h）不重复自动升版，"
                "保留当前 versions"
            ),
            failure_modes=failure_modes,
            from_version=from_version,
            window_key=window_key,
            generation=generations,
        )

    # --- trigger threshold --------------------------------------------------- #
    if len(failing) < EVOLVE_TRIGGER_MIN_FAILURES:
        return _remember(
            EvolutionOutcome(
                triggered=False,
                reason=(
                    f"窗口内失败 run {len(failing)} < "
                    f"{EVOLVE_TRIGGER_MIN_FAILURES}，未触发自动回流"
                ),
                failure_modes=failure_modes,
                from_version=from_version,
                window_key=window_key,
                generation=generations,
            )
        )

    # --- ② fold failing runs into new experiences (additive failure_mode) ----- #
    proposal = _build_proposal(failing)  # ③ deterministic, zero LLM
    records: list[Any] = []
    for failure in failing:
        record = _fold_failure_experience(tenant, skill, failure)
        await exp_repo.save(record)
        records.append(record)
    new_experience_id = records[0].id if records else None

    base = EvolutionOutcome(
        triggered=True,
        failure_modes=failure_modes,
        new_experience_id=new_experience_id,
        from_version=from_version,
        window_key=window_key,
        generation=generations,
        proposal=proposal,
    )

    # --- ④ compile a candidate (reuse) --------------------------------------- #
    from forgeflow.skills.candidate_compiler import compile_candidate

    candidate = await compile_candidate(
        tenant,
        [r.id for r in records],
        "manual",
        candidate_repo=cand_repo,
        experience_repo=exp_repo,
    )
    base.candidate_id = candidate.id
    if candidate.status == "insufficient":
        base.reason = (
            f"失败经验不足，无法编译候选"
            f"（{len(candidate.experience_ids)} < 最小经验数）"
        )
        return _remember(base)

    # The evolution of an *existing* skill must carry that skill's name so the
    # governance gate resolves (and bumps) the incumbent rather than minting a
    # second skill.
    candidate.name = str(getattr(skill, "name", "") or candidate.name)
    await cand_repo.save_candidate(candidate)

    # --- ⑤ engineering loop (reuse) → candidate reaches REVIEW ---------------- #
    from forgeflow.skills.engineering import run_engineering_loop

    result = await run_engineering_loop(
        tenant,
        candidate.id,
        actor,
        actor_role,
        candidate_repo=cand_repo,
        skill_repo=skill_repo,
        policy_repo=pol_repo,
    )
    if not result.passed:
        base.reason = result.degraded_reason or "工程闭环未通过，未生成新版本"
        return _remember(base)

    # --- ⑥ release gate (reuse): old-vs-new regression ----------------------- #
    from forgeflow.skills.governance_gate import promote_candidate
    from forgeflow.skills.release_gate import baseline_from_version, evaluate_release

    evaluation = await cand_repo.get_evaluation_for(tenant, candidate.id)
    new_metrics = dict(getattr(evaluation, "metrics", {}) or {})
    superseded = await skill_repo.get_version(tenant, skill_id, from_version)
    release = evaluate_release(new_metrics, baseline_from_version(superseded))
    base.regression = release.to_dict()
    if not release.allowed:
        base.reason = f"回归未过，未发布（保留 incumbent）：{release.reason}"
        return _remember(base)

    # --- promote → a NEW semver, never overwriting the incumbent (reuse) ------ #
    try:
        version = await promote_candidate(
            candidate.id,
            actor,
            tenant_id=tenant,
            candidate_repo=cand_repo,
            skill_repo=skill_repo,
            policy_repo=pol_repo,
            actor_role=actor_role,
        )
    except Exception as exc:  # noqa: BLE001 — degrade honestly, never fabricate
        base.reason = f"发布未通过（保留 incumbent）：{exc}"
        return _remember(base)

    base.to_version = version.semver
    base.applied = True
    base.generation = generations + 1
    base.reason = f"自动升版成功：{from_version or '(none)'} → {version.semver}"
    await _persist_provenance(
        tenant, skill_id, version.semver, [f["run_id"] for f in failing], moment
    )
    return _remember(base)
