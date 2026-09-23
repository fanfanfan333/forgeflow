"""Platform Context Builder (INC2 A5, docs/sop/05-ARCHITECTURE-INC2.md §2.5).

Every Agent's context is assembled here so the recall → dedup → compress → rank
pipeline is shared, auditable and measurably efficient. The design is
**LLM-free** (0 model calls): recall is embedding/keyword based, so it is safe
under the 18.6s-per-call Ollama constraint and works fully offline.

Pipeline::

    build_context(tenant, intent, ...)
      1) recall   memory  ← memory_store.search(tenant, intent, k=k_memory)
                  skill   ← SkillRegistry.select(tenant, intent, k=k_skill)
                  exp     ← ExperienceRepository.find_similar(tenant, embed(intent), k=k_exp)
      2) dedup    by (source, sha1(text[:200])); cross-source collisions keep the
                  higher-priority item
      3) compress per-item cap ≤ budget*0.4 ; over-budget tiers: drop episodic
                  first, then the lowest-similarity item
      4) rank     score = w1*similarity + w2*scope_weight + w3*usage → fill to budget
      5) metrics  tokens_used / compression_ratio / hit_rate → in-process counters
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from forgeflow.config import get_settings
from forgeflow.experience.embedding import embed_text
from forgeflow.experience.token_budget import estimate_tokens, per_item_cap, truncate_to_tokens

logger = logging.getLogger(__name__)

__all__ = [
    "ContextSection",
    "ContextBundle",
    "build_context",
    "get_context_stats",
    "reset_context_stats",
]

# Ranking weights (architecture §2.5). Kept module-level so tests can reference
# them and a future config override is trivial.
W_SIMILARITY = 0.6
W_SCOPE = 0.3
W_USAGE = 0.1

# Memory-scope priority weights (higher = more authoritative context).
_SCOPE_WEIGHTS: dict[str, float] = {
    "user": 1.0,
    "org": 1.0,
    "semantic": 0.9,
    "team": 0.85,
    "episodic": 0.2,
}
_DEFAULT_SCOPE_WEIGHT = 0.7


@dataclass
class ContextSection:
    """One selected context item. ``source`` ∈ {memory, skill, experience}."""

    source: str
    ref_id: str
    text: str
    score: float = 0.0
    similarity: float = 0.0
    scope_weight: float = 0.0
    usage: int = 0
    scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ref_id": self.ref_id,
            "text": self.text,
            "score": round(self.score, 4),
            "similarity": round(self.similarity, 4),
            "scope": self.scope,
        }


@dataclass
class ContextBundle:
    """The assembled context plus the efficiency metrics the UI/ops page shows."""

    sections: list[dict[str, Any]] = field(default_factory=list)
    tokens_used: int = 0
    tokens_raw: int = 0
    compression_ratio: float = 1.0
    hit_rate: float = 0.0
    dropped: list[dict[str, Any]] = field(default_factory=list)
    recalled: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "sections": self.sections,
            "tokens_used": self.tokens_used,
            "tokens_raw": self.tokens_raw,
            "compression_ratio": round(self.compression_ratio, 4),
            "hit_rate": round(self.hit_rate, 4),
            "dropped": self.dropped,
            "recalled": self.recalled,
        }


# In-process metric counters (offline-safe). The PG-backed
# ``context_build_stats`` write is a follow-up hook (INC2-10 observability).
_STATS: dict[str, float] = {
    "builds": 0.0,
    "tokens_raw": 0.0,
    "tokens_used": 0.0,
    "selected": 0.0,
    "recalled": 0.0,
}


def get_context_stats() -> dict[str, float]:
    """Return a copy of the in-process context-build counters."""
    return dict(_STATS)


def reset_context_stats() -> None:
    """Test helper: zero the counters."""
    for key in _STATS:
        _STATS[key] = 0.0


def _content_key(text: str) -> str:
    return hashlib.sha1((text or "")[:200].encode("utf-8")).hexdigest()


def _score(candidate: ContextSection) -> float:
    usage_signal = min(candidate.usage, 100) / 100.0
    return (
        W_SIMILARITY * candidate.similarity
        + W_SCOPE * candidate.scope_weight
        + W_USAGE * usage_signal
    )


async def _recall(
    tenant_id: str | None,
    intent: str,
    *,
    k_memory: int,
    k_skill: int,
    k_exp: int,
    min_similarity: float,
) -> list[ContextSection]:
    """Stage 1 — gather raw candidates from the three sources (0 LLM calls)."""
    candidates: list[ContextSection] = []

    # --- memory (five-layer scoped store) ---
    try:
        from forgeflow.experience.memory_store import search as memory_search

        for entry, similarity in await memory_search(tenant_id, intent, k=k_memory):
            candidates.append(
                ContextSection(
                    source="memory",
                    ref_id=entry.id,
                    text=entry.content,
                    similarity=float(similarity),
                    scope_weight=_SCOPE_WEIGHTS.get(entry.scope, _DEFAULT_SCOPE_WEIGHT),
                    scope=entry.scope,
                )
            )
    except Exception as exc:  # noqa: BLE001 — recall must never crash the build
        logger.warning("context recall: memory search failed: %s", exc)

    # --- skills (registry keyword select) ---
    try:
        from forgeflow.skills.registry import SkillRegistry

        for skill in await SkillRegistry().select(tenant_id, intent, k=k_skill):
            text = f"{skill.name}: {skill.description}".strip(": ")
            candidates.append(
                ContextSection(
                    source="skill",
                    ref_id=skill.id,
                    text=text,
                    similarity=1.0,
                    scope_weight=_DEFAULT_SCOPE_WEIGHT,
                    usage=int(getattr(skill, "usage_count", 0) or 0),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("context recall: skill select failed: %s", exc)

    # --- experiences (embedding similarity) ---
    try:
        from forgeflow.repositories import get_experience_repository

        repo = get_experience_repository()
        pairs = await repo.find_similar(
            tenant_id, embed_text(intent), k=k_exp, min_similarity=min_similarity
        )
        for record, similarity in pairs:
            candidates.append(
                ContextSection(
                    source="experience",
                    ref_id=record.id,
                    text=getattr(record, "summary", "") or "",
                    similarity=float(similarity),
                    scope_weight=_DEFAULT_SCOPE_WEIGHT,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("context recall: experience search failed: %s", exc)

    return candidates


def _dedup(candidates: list[ContextSection]) -> list[ContextSection]:
    """Stage 2 — drop duplicates by (source, content-hash) and cross-source twins.

    Candidates are pre-sorted by descending score so the first occurrence of a
    key is always the highest-priority one.
    """
    seen_source_key: set[tuple[str, str]] = set()
    seen_content: set[str] = set()
    kept: list[ContextSection] = []
    for cand in candidates:
        source_key = (cand.source, _content_key(cand.text))
        content_key = _content_key(cand.text)
        if source_key in seen_source_key or content_key in seen_content:
            continue
        seen_source_key.add(source_key)
        seen_content.add(content_key)
        kept.append(cand)
    return kept


def _select_within_budget(
    candidates: list[ContextSection],
    budget_tokens: int,
    *,
    cap: int,
) -> tuple[list[ContextSection], list[dict[str, Any]]]:
    """Stage 3+4 — cap per item, then drop (episodic first) until it fits."""
    selected: list[ContextSection] = []
    dropped: list[dict[str, Any]] = []

    # Per-item cap: clip each text so no single item hogs the budget.
    for cand in candidates:
        clipped = truncate_to_tokens(cand.text, cap)
        if clipped != cand.text:
            cand.text = clipped

    # Fill by descending score.
    ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
    total = sum(estimate_tokens(c.text) for c in ordered)
    working = list(ordered)

    while working and total > budget_tokens:
        # Tiered pruning: episodic first (lowest score first), then the lowest
        # similarity item overall.
        episodic = [c for c in working if c.scope == "episodic"]
        if episodic:
            victim = min(episodic, key=lambda c: (c.score, c.similarity))
            reason = "episodic_pruned"
        else:
            victim = min(working, key=lambda c: (c.score, c.similarity))
            reason = "over_budget"
        working.remove(victim)
        total -= estimate_tokens(victim.text)
        dropped.append(
            {"source": victim.source, "ref_id": victim.ref_id, "reason": reason}
        )

    selected = working
    return selected, dropped


async def build_context(
    tenant_id: str | None,
    intent: str,
    *,
    user_id: str | None = None,
    team_id: str | None = None,
    budget_tokens: int = 2000,
    k_memory: int = 5,
    k_skill: int = 3,
    k_exp: int = 3,
    min_similarity: float = 0.6,
    per_item_ratio: float | None = None,
) -> ContextBundle:
    """Assemble a budget-bounded context bundle for ``intent``.

    ``user_id`` / ``team_id`` are accepted for scope-aware recall and future
    filtering; the default pipeline is tenant-scoped. ``budget_tokens`` is the
    hard ceiling; each item is capped at ``budget × per_item_ratio`` (0.4).
    """
    settings = get_settings()
    budget = int(budget_tokens or settings.context_budget_tokens)
    ratio = (
        per_item_ratio
        if per_item_ratio is not None
        else settings.context_per_item_ratio
    )
    cap = per_item_cap(budget, ratio)

    recalled = await _recall(
        tenant_id,
        intent,
        k_memory=k_memory,
        k_skill=k_skill,
        k_exp=k_exp,
        min_similarity=min_similarity,
    )
    recalled_count = len(recalled)
    tokens_raw = sum(estimate_tokens(c.text) for c in recalled)

    # Score before dedup so the highest-priority duplicate wins.
    for cand in recalled:
        cand.score = _score(cand)
    deduped = _dedup(sorted(recalled, key=lambda c: c.score, reverse=True))

    selected, dropped = _select_within_budget(deduped, budget, cap=cap)

    sections = [c.to_dict() for c in selected]
    tokens_used = sum(estimate_tokens(c.text) for c in selected)
    compression_ratio = (tokens_used / tokens_raw) if tokens_raw > 0 else 1.0
    hit_rate = (len(selected) / recalled_count) if recalled_count > 0 else 0.0

    bundle = ContextBundle(
        sections=sections,
        tokens_used=tokens_used,
        tokens_raw=tokens_raw,
        compression_ratio=compression_ratio,
        hit_rate=hit_rate,
        dropped=dropped,
        recalled=recalled_count,
    )

    _record_stats(bundle)
    return bundle


def _record_stats(bundle: ContextBundle) -> None:
    _STATS["builds"] += 1
    _STATS["tokens_raw"] += bundle.tokens_raw
    _STATS["tokens_used"] += bundle.tokens_used
    _STATS["selected"] += len(bundle.sections)
    _STATS["recalled"] += bundle.recalled
