"""INC2-19 — Skill Evolution: turn usage signal into *advice* (never mutations).

User ask #12 ("Agent 变强"). The analyser proposes ``optimize`` / ``retire``
recommendations; per design verdict §7.5 nothing is applied directly — each
advice becomes an ``agent_approvals`` row (``kind="skill.optimize"`` /
``"skill.retire"``) for a human to accept.

LLM budget discipline (the 18.6 s/call constraint):

    ①  rule pass        — 0 LLM calls, filters the catalogue down to candidates
    ②  one batch call   — at most ONE call for up to ``MAX_BATCH`` candidates,
                          all packed into a single prompt; never per-skill
    ③  cache            — keyed on ``(tenant, skill_id, spec_hash)`` so a repeat
                          analysis costs 0 calls

With ``LLM_PROVIDER=mock`` (or any LLM failure) the rule-only advice is
returned and nothing is raised — the feature degrades, it does not break.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from forgeflow.config import get_settings
from forgeflow.repositories.base import new_id, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "EvolutionAdvice",
    "MAX_BATCH",
    "advise",
    "reset_advice_cache",
    "submit_advice_for_approval",
]

# --- tuning constants (rule pass) ------------------------------------------
#: A published skill untouched for this long and never used → retirement candidate.
RETIRE_IDLE_DAYS = 90
#: A skill used this many times with no recorded score still deserves a look.
OPTIMIZE_MIN_USAGE = 5
#: Below this evaluation score a skill is under-performing → optimize.
OPTIMIZE_MAX_EVAL_SCORE = 0.60
#: Hard cap on skills packed into the single LLM prompt.
MAX_BATCH = 20

ADVICE_KINDS = ("optimize", "retire")


@dataclass
class EvolutionAdvice:
    """One recommendation. ``source`` is ``"rule"`` or ``"llm"``."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    skill_id: str = ""
    skill_name: str = ""
    kind: str = "optimize"  # optimize | retire
    reason: str = ""
    confidence: float = 0.5
    source: str = "rule"
    created_at: datetime = field(default_factory=utcnow)

    @property
    def approval_kind(self) -> str:
        return f"skill.{self.kind}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "kind": self.kind,
            "reason": self.reason,
            "confidence": round(self.confidence, 3),
            "source": self.source,
            "approval_kind": self.approval_kind,
            "created_at": self.created_at.isoformat(),
        }


# (tenant, skill_id, spec_hash) → advice list. Prevents repeat LLM spend.
_CACHE: dict[tuple[str | None, str, str], list[EvolutionAdvice]] = {}


def reset_advice_cache() -> None:
    """Test helper — drop the memoised advice."""
    _CACHE.clear()


def _spec_hash(spec: Any) -> str:
    """Stable hash of a skill's spec/version signature."""
    try:
        blob = json.dumps(spec, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(spec)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _age_days(value: Any, now: datetime) -> float | None:
    """Days since ``value`` (datetime or ISO string); ``None`` if unusable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (now - moment).total_seconds() / 86400.0)


def _rule_advice(
    skills: list[Any],
    *,
    tenant_id: str | None,
    now: datetime,
) -> list[EvolutionAdvice]:
    """Stage ① — deterministic, zero-LLM candidate selection."""
    advice: list[EvolutionAdvice] = []
    for skill in skills:
        skill_id = str(getattr(skill, "id", "") or "")
        if not skill_id:
            continue
        name = str(getattr(skill, "name", "") or skill_id)
        status = str(getattr(skill, "status", "") or "")
        if status == "retired":
            continue

        usage = int(getattr(skill, "usage_count", 0) or 0)
        suggested = bool(getattr(skill, "retire_suggested", False))
        eval_score = getattr(skill, "eval_score", None)
        last_used = getattr(skill, "last_used_at", None) or getattr(skill, "updated_at", None)
        idle_days = _age_days(last_used, now)

        if suggested:
            advice.append(
                EvolutionAdvice(
                    tenant_id=tenant_id,
                    skill_id=skill_id,
                    skill_name=name,
                    kind="retire",
                    reason="已被标记为建议退役（retire_suggested）",
                    confidence=0.9,
                )
            )
            continue
        if usage == 0 and idle_days is not None and idle_days >= RETIRE_IDLE_DAYS:
            advice.append(
                EvolutionAdvice(
                    tenant_id=tenant_id,
                    skill_id=skill_id,
                    skill_name=name,
                    kind="retire",
                    reason=f"连续 {int(idle_days)} 天无调用且使用次数为 0",
                    confidence=0.7,
                )
            )
            continue

        if eval_score is not None:
            try:
                score = float(eval_score)
            except (TypeError, ValueError):
                score = 1.0
            if score < OPTIMIZE_MAX_EVAL_SCORE:
                advice.append(
                    EvolutionAdvice(
                        tenant_id=tenant_id,
                        skill_id=skill_id,
                        skill_name=name,
                        kind="optimize",
                        reason=f"评估得分 {score:.2f} 低于 {OPTIMIZE_MAX_EVAL_SCORE:.2f}",
                        confidence=0.75,
                    )
                )
        elif usage >= OPTIMIZE_MIN_USAGE:
            advice.append(
                EvolutionAdvice(
                    tenant_id=tenant_id,
                    skill_id=skill_id,
                    skill_name=name,
                    kind="optimize",
                    reason=f"已被调用 {usage} 次但缺少评估分数，建议补齐评测并优化",
                    confidence=0.5,
                )
            )
    return advice


async def _llm_batch(skills: list[Any], advice: list[EvolutionAdvice]) -> dict[str, str]:
    """Stage ② — ONE batched LLM call for up to ``MAX_BATCH`` candidates.

    Returns ``{skill_id: reason}``. Returns ``{}`` under ``LLM_PROVIDER=mock``
    or on any failure, so callers keep the rule-only advice.
    """
    settings = get_settings()
    if settings.llm_provider.lower() in ("", "mock"):
        return {}
    if not advice:
        return {}

    batch = advice[:MAX_BATCH]
    lines = []
    for item in batch:
        lines.append(f"- id={item.skill_id} name={item.skill_name} kind={item.kind} rule={item.reason}")
    prompt = (
        "你是技能治理助手。下面是一批候选技能建议（每行一个）。\n"
        "请仅输出 JSON 数组，每项形如 {\"id\": \"<skill_id>\", \"reason\": \"<一句话中文理由>\"}。\n"
        "不要输出解释文字。\n\n" + "\n".join(lines)
    )

    try:
        from forgeflow.models import get_model

        model = get_model(strong=True)
        message = await model.ainvoke(prompt)
        content = getattr(message, "content", "") or ""
    except Exception as exc:  # noqa: BLE001 — degrade to rule-only, never raise
        logger.debug("skill evolution LLM batch skipped: %s", exc)
        return {}

    return _parse_llm_reasons(content, {item.skill_id for item in batch})


def _parse_llm_reasons(content: str, valid_ids: set[str]) -> dict[str, str]:
    """Leniently pull ``{"id": ..., "reason": ...}`` entries out of a reply."""
    start = content.find("[")
    end = content.rfind("]")
    blob = content[start : end + 1] if start != -1 and end > start else content
    try:
        parsed = json.loads(blob)
    except (TypeError, ValueError):
        return {}
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return {}
    out: dict[str, str] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id", ""))
        reason = str(item.get("reason", "")).strip()
        if sid in valid_ids and reason:
            out[sid] = reason[:400]
    return out


async def advise(
    tenant_id: str | None,
    *,
    skills: list[Any] | None = None,
    skill_repo: Any | None = None,
    use_llm: bool = True,
    limit: int = 200,
) -> list[EvolutionAdvice]:
    """Produce evolution advice for a tenant.

    ``skills`` may be supplied directly (tests, callers that already loaded
    them); otherwise they are read from the skill repository.
    """
    now = datetime.now(timezone.utc)
    if skills is None:
        repo = skill_repo
        if repo is None:
            from forgeflow.repositories import get_skill_repository

            repo = get_skill_repository()
        try:
            rows, _total = await repo.list_skills(tenant_id, limit=limit)
        except Exception as exc:  # noqa: BLE001 — no catalogue ⇒ no advice
            logger.debug("skill evolution: listing skills failed: %s", exc)
            rows = []
        skills = list(rows)
    else:
        skills = list(skills)

    rule_advice = _rule_advice(skills, tenant_id=tenant_id, now=now)

    # --- cache: skip the LLM entirely when nothing changed -----------------
    spec_hashes = {
        str(getattr(s, "id", "")): _spec_hash(
            {
                "version": getattr(s, "current_version", None),
                "usage": getattr(s, "usage_count", None),
                "score": getattr(s, "eval_score", None),
                "retire": getattr(s, "retire_suggested", None),
                "last_used": str(getattr(s, "last_used_at", None)),
            }
        )
        for s in skills
    }

    cached: list[EvolutionAdvice] = []
    pending: list[EvolutionAdvice] = []
    for item in rule_advice:
        key = (tenant_id, item.skill_id, spec_hashes.get(item.skill_id, ""))
        hit = _CACHE.get(key)
        if hit is not None:
            cached.extend(hit)
        else:
            pending.append(item)

    if use_llm and pending:
        reasons = await _llm_batch(skills, pending)
        for item in pending:
            if item.skill_id in reasons:
                item.reason = reasons[item.skill_id]
                item.source = "llm"
                item.confidence = min(1.0, item.confidence + 0.15)

    for item in pending:
        key = (tenant_id, item.skill_id, spec_hashes.get(item.skill_id, ""))
        _CACHE[key] = [item]

    merged = cached + pending
    merged.sort(key=lambda a: (a.kind != "retire", -a.confidence, a.skill_id))
    return merged


async def submit_advice_for_approval(
    tenant_id: str | None,
    advices: list[EvolutionAdvice],
    *,
    requester: str = "system",
    policy_repo: Any | None = None,
) -> list[Any]:
    """§7.5 — every advice becomes a pending approval; nothing is auto-applied.

    Returns the persisted ``ApprovalRecord``s. Persistence failures are logged
    and skipped so a broken policy repo cannot lose the whole analysis.
    """
    from forgeflow.governance.models import ApprovalRecord

    repo = policy_repo
    if repo is None:
        from forgeflow.repositories import get_policy_repository

        repo = get_policy_repository()

    saved: list[Any] = []
    for item in advices:
        approval = ApprovalRecord(
            tenant_id=tenant_id,
            risk_level="medium" if item.kind == "retire" else "low",
            requested_action=f"skill.{item.kind}:{item.skill_id}",
            requester=requester,
            status="pending",
            note=f"{item.skill_name} — {item.reason}",
            kind=item.approval_kind,
        )
        try:
            saved.append(await repo.save_approval(approval))
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill evolution: approval persist failed: %s", exc)
    return saved
