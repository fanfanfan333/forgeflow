"""Platform Context Builder (INC2 A5, docs/sop/05-ARCHITECTURE-INC2.md §2.5).

Every Agent's context is assembled here so the recall → dedup → compress → rank
pipeline is shared, auditable and measurably efficient. The design is
**LLM-free** (0 model calls): recall is embedding/keyword based, so it is safe
under the 18.6s-per-call Ollama constraint and works fully offline.

Pipeline::

    build_context(tenant, intent, ...)
      1) recall   memory  ← memory_store.search(tenant, intent, k=k_memory)
                  skill   ← SkillRegistry.retrieve(tenant, intent, k=k_skill)
                            (INC46 T09: Query → Hybrid → RRF → Filter → Rerank → Top-K)
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
    #: INC46 T09（裁定 V-6）—— ``None`` 表示**未测量**（红线 4：未测量 ⇒ None，
    #: 绝不写 0 冒充「相似度为 0」）。此前 skill 段一律硬编码 ``1.0``，那是谎报：
    #: 它声称每个 skill 与意图的相似度都是满分。现改为真实算出的稠密余弦。
    #: 排序（见 :func:`_score`）时未测量按 ``0.0`` 计入**组合分**，这是排序依据，
    #: 与「相似度是否测得」是两件事，不得相互冒充。
    similarity: float | None = None
    scope_weight: float = 0.0
    usage: int = 0
    scope: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ref_id": self.ref_id,
            "text": self.text,
            "score": round(self.score, 4),
            "similarity": None if self.similarity is None else round(self.similarity, 4),
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
    #: INC46 T29（加性、可选）—— 当显式请求「按章节可寻址的文档加载」时，这里带上该次
    #: 加载的**账**（目标区间 ± 邻接 + 大纲的 token 记账）；未请求 ⇒ ``None``。
    document_load: dict[str, Any] | None = None
    #: INC46 T29（加性、可选）—— 该次装配的每步 token 记账（供 T36 成本指标）。
    #: **未记账 ⇒ ``None``**（红线 4），绝不写 0。未请求文档加载 ⇒ ``None``。
    token_accounting: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sections": self.sections,
            "tokens_used": self.tokens_used,
            "tokens_raw": self.tokens_raw,
            "compression_ratio": round(self.compression_ratio, 4),
            "hit_rate": round(self.hit_rate, 4),
            "dropped": self.dropped,
            "recalled": self.recalled,
        }
        # 加性字段只在被显式请求（非 None）时出现 —— 默认路径的 to_dict 逐键不变。
        if self.document_load is not None:
            out["document_load"] = self.document_load
        if self.token_accounting is not None:
            out["token_accounting"] = self.token_accounting
        return out


# In-process metric counters (offline-safe), bumped on every build below. The
# PG-backed ``context_build_stats`` row is the **wired** companion (INC2-10):
# ``runtime.orchestrator.run_task`` awaits
# ``observability.context_stats.persist_context_build`` after each build (a no-op
# off the postgres profile).
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


def _sim(candidate: ContextSection) -> float:
    """Similarity **for ranking only** — unmeasured (``None``) counts as ``0.0``.

    这是排序分量，不是「相似度」本身：:attr:`ContextSection.similarity` 仍如实
    保留 ``None``（红线 4），二者不得相互冒充。
    """
    return 0.0 if candidate.similarity is None else candidate.similarity


def _score(candidate: ContextSection) -> float:
    usage_signal = min(candidate.usage, 100) / 100.0
    return (
        W_SIMILARITY * _sim(candidate)
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

    # --- skills (INC46 T09 — hybrid retrieval chain) ---
    try:
        from forgeflow.skills.registry import SkillRegistry

        for hit in await SkillRegistry().retrieve(tenant_id, intent, k=k_skill):
            skill = hit.skill
            text = f"{skill.name}: {skill.description}".strip(": ")
            candidates.append(
                ContextSection(
                    source="skill",
                    ref_id=skill.id,
                    text=text,
                    # 真实算出的稠密余弦（INC46 T09）；未测量 ⇒ None（红线 4），
                    # 不再是「一律 1.0」的谎报。
                    similarity=hit.similarity,
                    scope_weight=_DEFAULT_SCOPE_WEIGHT,
                    usage=int(getattr(skill, "usage_count", 0) or 0),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("context recall: skill retrieve failed: %s", exc)

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
            victim = min(episodic, key=lambda c: (c.score, _sim(c)))
            reason = "episodic_pruned"
        else:
            victim = min(working, key=lambda c: (c.score, _sim(c)))
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
    document: bytes | None = None,
    document_selector: str | None = None,
    document_adjacency: int = 1,
    preferences: list[Any] | None = None,
) -> ContextBundle:
    """Assemble a budget-bounded context bundle for ``intent``.

    ``user_id`` / ``team_id`` are accepted for scope-aware recall and future
    filtering; the default pipeline is tenant-scoped. ``budget_tokens`` is the
    hard ceiling; each item is capped at ``budget × per_item_ratio`` (0.4).

    INC46 T29 (加性、可选): 传入 ``document``（真实 DOCX 字节）时，本函数在既有三源召回
    之外**追加**一段「按章节可寻址的文档上下文」—— 只加载**目标区间 ± 邻接 + 文档大纲**
    （见 :func:`forgeflow.context.section_loader.load_sections`），并把该次加载的 token 记账
    写入 ``bundle.document_load`` / ``bundle.token_accounting``（供 T36）。``document`` 为
    ``None``（默认）时，本函数的**默认行为逐字节不变**：既不导入也未触碰新库。

    INC46 T31 (加性、可选): 传入 ``preferences``（T31 解析出的**生效**偏好列表，
    见 :func:`forgeflow.memory.preferences.resolve_preferences`）时，**追加**一段
    ``source="preference"`` 的上下文（写作风格 / 术语表 / 禁用词 / 文档约定）。调用方只传
    **生效**项（``explicit`` / ``confirmed_suggestion``）；未确认的建议不在此列。``preferences``
    为 ``None``（默认）时默认行为逐字节不变。
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

    # INC9 B2 — the single runtime source of the memory reuse signal: bump the
    # reuse counter for the memory entries that actually made it into the bundle.
    # Additive (a build with no memory sections is a no-op) and best-effort — a
    # store hiccup must never break context assembly.
    await _mark_memory_reused(tenant_id, selected)

    sections = [c.to_dict() for c in selected]
    tokens_used = sum(estimate_tokens(c.text) for c in selected)
    compression_ratio = (tokens_used / tokens_raw) if tokens_raw > 0 else 1.0
    hit_rate = (len(selected) / recalled_count) if recalled_count > 0 else 0.0

    # INC46 T29 — 按章节可寻址的文档加载（加性、**仅在被请求时**执行）。
    document_load: dict[str, Any] | None = None
    token_accounting: dict[str, Any] | None = None
    if document is not None:
        document_load, token_accounting, tokens_used = _append_document_context(
            document=document,
            selector=document_selector,
            adjacency=document_adjacency,
            budget=budget,
            sections=sections,
            tokens_used=tokens_used,
        )

    # INC46 T31 — 记忆偏好的**加性、可选**注入（默认 ``None`` ⇒ 逐字节不变）。
    if preferences:
        tokens_used = _append_preference_context(
            preferences=preferences, sections=sections, tokens_used=tokens_used
        )

    bundle = ContextBundle(
        sections=sections,
        tokens_used=tokens_used,
        tokens_raw=tokens_raw,
        compression_ratio=compression_ratio,
        hit_rate=hit_rate,
        dropped=dropped,
        recalled=recalled_count,
        document_load=document_load,
        token_accounting=token_accounting,
    )

    _record_stats(bundle)
    return bundle


def _append_document_context(
    *,
    document: bytes,
    selector: str | None,
    adjacency: int,
    budget: int,
    sections: list[dict[str, Any]],
    tokens_used: int,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    """Append a section-addressable document section — additive, opt-in (INC46 T29).

    Lazily imports :mod:`forgeflow.context.section_loader` so the default
    (document-less) path neither imports nor touches the new library. Returns
    ``(document_load, token_accounting, tokens_used)``. Best-effort: a document
    that cannot be inspected is reported honestly (``document_load`` carries the
    reason) and never breaks the recall-stage bundle.
    """
    from forgeflow.context.section_loader import SectionLoadError, load_sections
    from forgeflow.context.token_accounting import account_step, run_step_token_payload

    try:
        plan = load_sections(
            document,
            selector=selector,
            adjacency=adjacency,
            budget_tokens=budget,
            counter=estimate_tokens,
        )
    except SectionLoadError as exc:
        # Honest degradation: report the failure, add nothing, keep the bundle valid.
        return (
            {"located": False, "error": str(exc)},
            run_step_token_payload(None),
            tokens_used,
        )

    text = plan.loaded_text()
    if text:
        sections.append(
            {
                "source": "document",
                "ref_id": (
                    plan.target_chunk.chunk_id if plan.target_chunk else "outline"
                ),
                "text": text,
                "score": 1.0,
                "similarity": None,  # 未测量（红线 4）；文档段靠「目标命中」而非相似度入选
                "scope": "document",
            }
        )
        tokens_used += plan.total_loaded_tokens

    # token 记账：context 有测量（估算），prompt/completion 本步未测量 ⇒ None。
    accounting = account_step(context=text or None)
    return plan.to_dict(), run_step_token_payload(accounting), tokens_used



def _append_preference_context(
    *,
    preferences: list[Any],
    sections: list[dict[str, Any]],
    tokens_used: int,
) -> int:
    """Append one ``source="preference"`` section — additive, opt-in (INC46 T31).

    ``preferences`` are the caller's **effective** items (each a
    :class:`forgeflow.memory.preferences.Preference` or a duck-typed mapping with
    ``as_instruction`` / ``value``). Only items that expose an instruction line
    are injected. ``similarity`` is ``None`` (未测量 ⇒ None, 红线 4) — a
    preference is authoritative, not similarity-ranked. Returns the new
    ``tokens_used``; an empty list is a no-op.
    """
    lines: list[str] = []
    for pref in preferences or []:
        as_instruction = getattr(pref, "as_instruction", None)
        if callable(as_instruction):
            lines.append(str(as_instruction()))
        else:  # duck-typed mapping — fall back to its value
            value = (pref.get("value") if isinstance(pref, dict) else None) or ""
            if value:
                lines.append(str(value))
    if not lines:
        return tokens_used

    text = "\n".join(lines)
    sections.append(
        {
            "source": "preference",
            "ref_id": "memory-preferences",
            "text": text,
            "score": 1.0,
            "similarity": None,  # 未测量（红线 4）；偏好靠「显式设定」而非相似度入选
            "scope": "preference",
        }
    )
    return tokens_used + estimate_tokens(text)


def _record_stats(bundle: ContextBundle) -> None:
    _STATS["builds"] += 1
    _STATS["tokens_raw"] += bundle.tokens_raw
    _STATS["tokens_used"] += bundle.tokens_used
    _STATS["selected"] += len(bundle.sections)
    _STATS["recalled"] += bundle.recalled


async def _mark_memory_reused(
    tenant_id: str | None, selected: list[ContextSection]
) -> None:
    """Bump ``reuse_count`` for the memory sections that were actually selected.

    Lazy import + broad guard: the memory store is an in-process, offline-safe
    dependency, and a failure here must never break context assembly (the same
    rule the recall stage follows). When no memory section is selected this is a
    no-op, so a build that recalls nothing new leaves ``reuse_count`` untouched.
    """
    memory_ids = [c.ref_id for c in selected if c.source == "memory" and c.ref_id]
    if not memory_ids:
        return
    try:
        from forgeflow.experience.memory_store import mark_reused

        await mark_reused(tenant_id, memory_ids)
    except Exception as exc:  # noqa: BLE001 — reuse bookkeeping is best-effort
        logger.warning("context build: mark_reused failed: %s", exc)
