"""INC46 T29 — 上下文压缩（Compaction）：超预算可压缩，**白名单必保留**。

What this module is
-------------------
当上下文超过 token 预算时，按策略把它压进预算：

* :attr:`CompactionStrategy.DROP_LOW_PRIORITY`（默认）— 先丢弃**低优先**的非白名单块；
* :attr:`CompactionStrategy.TRUNCATE` — 截断非白名单块；
* :attr:`CompactionStrategy.SUMMARIZE` — 摘要非白名单块。

**白名单（Whitelist）** 明确声明哪些内容**不可压缩** —— 例如：用户原始指令
（``user_instruction``）、当前编辑目标（``edit_target``）、不变量清单（``invariants``）、
HITL 待决项（``hitl_pending``）。白名单**可配置**：见 :class:`WhitelistConfig`（可增删受保护
标签 / 类型 / 具体块）。

承重不变量（红线 16 + 白名单保利）
----------------------------------
* **白名单块逐字节不变**：任何策略下，受保护块的 ``text`` / ``sha256`` 都与压缩前**完全
  一致**；它们既不被截断、不被摘要，也不被丢弃。
* **白名单被压缩 ⇒ 显式报错**：若有人试图把受保护块送进压缩，:func:`compact_one`
  **抛** :class:`ProtectedContentError`，绝不静默。
* **压缩后仍超预算 ⇒ 显式报错**：:func:`compact` 结束时调用
  :func:`forgeflow.context.budget_accounting.assert_within_budget`，超限即抛
  :class:`ContextBudgetError` —— 例如受保护内容本身就超过预算（无法在不丢关键信息的
  前提下满足预算）时**必须失败**，绝不静默丢弃关键信息（红线 16）。
* **记账可复算**：压缩前后的 :class:`BudgetLedger` 都可复算；压缩后总量恒等于
  *各保留块估算值之和*。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Sequence

from forgeflow.context.budget_accounting import (
    BudgetLedger,
    TokenCounter,
    account_chunks,
    assert_within_budget,
    measure_tokens,
)
from forgeflow.context.chunker import Chunk
from forgeflow.experience.token_budget import estimate_tokens, truncate_to_tokens

__all__ = [
    "TAG_USER_INSTRUCTION",
    "TAG_EDIT_TARGET",
    "TAG_INVARIANTS",
    "TAG_HITL_PENDING",
    "DEFAULT_PROTECTED_TAGS",
    "WhitelistConfig",
    "CompactionStrategy",
    "CompactResult",
    "ProtectedContentError",
    "compact",
    "compact_one",
]

#: 白名单类别：用户原始指令 —— **不可压缩**。
TAG_USER_INSTRUCTION = "user_instruction"
#: 白名单类别：当前编辑目标 —— **不可压缩**。
TAG_EDIT_TARGET = "edit_target"
#: 白名单类别：不变量清单 —— **不可压缩**。
TAG_INVARIANTS = "invariants"
#: 白名单类别：HITL 待决项 —— **不可压缩**。
TAG_HITL_PENDING = "hitl_pending"

#: 默认受保护标签集合（可经 :class:`WhitelistConfig` 覆盖 / 增删）。
DEFAULT_PROTECTED_TAGS: frozenset[str] = frozenset(
    {TAG_USER_INSTRUCTION, TAG_EDIT_TARGET, TAG_INVARIANTS, TAG_HITL_PENDING}
)

#: 摘要器的签名：``text -> summary``。
Summarizer = Callable[[str], str]

#: 默认摘要的最大字符数（确定性、离线）。
_DEFAULT_SUMMARY_MAX = 160


@dataclass(frozen=True)
class WhitelistConfig:
    """Declarative, **configurable** whitelist of content that must survive compaction.

    Attributes:
        protected_tags: 受保护标签集合（默认 = :data:`DEFAULT_PROTECTED_TAGS`）。
        protected_kinds: 受保护的语义单元类型（如 ``{"document"}``）。
        protected_chunk_ids: 精确到具体块的受保护 id 集合。
    """

    protected_tags: frozenset[str] = DEFAULT_PROTECTED_TAGS
    protected_kinds: frozenset[str] = frozenset()
    protected_chunk_ids: frozenset[str] = frozenset()

    def is_protected(self, chunk: Chunk) -> bool:
        """True when ``chunk`` matches any whitelist rule (⇒ **不可压缩**)."""
        if chunk.chunk_id in self.protected_chunk_ids:
            return True
        if chunk.kind in self.protected_kinds:
            return True
        return bool(self.protected_tags & set(chunk.tags))

    def protected_ids(self, chunks: Iterable[Chunk]) -> frozenset[str]:
        """The subset of ``chunks`` ids flagged as protected."""
        return frozenset(c.chunk_id for c in chunks if self.is_protected(c))


class CompactionStrategy(str, Enum):
    """How an over-budget, non-whitelisted context is reduced."""

    DROP_LOW_PRIORITY = "drop_low_priority"
    TRUNCATE = "truncate"
    SUMMARIZE = "summarize"


class ProtectedContentError(Exception):
    """Raised when a whitelist-protected chunk would be compacted.

    这是白名单的**显式**拒绝（红线 16 的姊妹条款）：受保护内容**绝不**被静默压缩 / 丢弃。
    """


@dataclass
class CompactResult:
    """The outcome of a compaction pass, plus before/after accounting.

    Attributes:
        kept: 压缩后保留的块（受保护块**逐字节原样**在内）。
        dropped: 被丢弃的块（低优先 / 超出预算）。
        compacted: 被截断 / 摘要的块（**仅**非受保护块；其 ``sha256`` 已变化）。
        ledger_before: 压缩前的账本。
        ledger_after: 压缩后的账本。
        strategy: 实际使用的策略。
        within_budget: 压缩后是否落在预算内（构造时已保证为 ``True``，否则会抛错）。
    """

    kept: list[Chunk] = field(default_factory=list)
    dropped: list[Chunk] = field(default_factory=list)
    compacted: list[Chunk] = field(default_factory=list)
    ledger_before: BudgetLedger | None = None
    ledger_after: BudgetLedger | None = None
    strategy: CompactionStrategy = CompactionStrategy.DROP_LOW_PRIORITY
    within_budget: bool = True

    def protected_preserved(self, policy: WhitelistConfig) -> bool:
        """True when every whitelist-protected block survived **byte-identical**.

        Compares the ``sha256`` of the pre-compaction protected chunks against the
        post-compaction kept set — the load-bearing 白名单保利 check.
        """
        assert self.ledger_before is not None and self.ledger_after is not None
        before = {
            a.chunk_id: a.sha256
            for a in self.ledger_before.protected_chunks()
        }
        after = {c.chunk_id: c.sha256 for c in self.kept}
        for chunk_id, digest in before.items():
            if after.get(chunk_id) != digest:
                return False
        return True


def _default_summarizer(text: str) -> str:
    """Deterministic, offline summariser: first sentence, length-bounded.

    取第一句（到 ``。`` / ``！`` / ``？`` / ``. `` 之一），压掉多余空白并截断到
    :data:`_DEFAULT_SUMMARY_MAX`。**不是** LLM 摘要 —— 只是可控、可复算的压缩。
    """
    raw = " ".join((text or "").split())
    if not raw:
        return ""
    for sep in ("。", "！", "？", ". ", "!", "?"):
        idx = raw.find(sep)
        if idx != -1:
            raw = raw[: idx] + sep.strip()
            break
    return raw[:_DEFAULT_SUMMARY_MAX]


def _rehash(text: str) -> str:
    """Local sha256 (kept private so the chunker stays the single hashing authority)."""
    import hashlib

    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _build_chunk(chunk: Chunk, new_text: str) -> Chunk:
    """Clone ``chunk`` with a new ``text`` (and matching digest), preserving identity."""
    return Chunk(
        index=chunk.index,
        kind=chunk.kind,
        text=new_text,
        start=chunk.start,
        end=chunk.end,
        priority=chunk.priority,
        tags=chunk.tags,
        chunk_id=chunk.chunk_id,
        sha256=_rehash(new_text),
    )


def compact_one(
    chunk: Chunk,
    *,
    policy: WhitelistConfig,
    max_tokens: int,
    strategy: CompactionStrategy = CompactionStrategy.TRUNCATE,
    summarizer: Summarizer | None = None,
) -> Chunk:
    """Compact a **single** chunk, **refusing** to touch whitelist-protected content.

    Raises:
        ProtectedContentError: ``chunk`` 命中白名单 ⇒ **拒绝压缩**（不静默）。
    """
    if policy.is_protected(chunk):
        raise ProtectedContentError(
            f"块 {chunk.chunk_id} 命中白名单（tags={sorted(chunk.tags)}）—— "
            "受保护内容不可压缩，拒绝静默压缩 / 丢弃"
        )

    if strategy is CompactionStrategy.SUMMARIZE:
        summarize = summarizer or _default_summarizer
        summary = summarize(chunk.text)
        if measure_tokens(summary) is not None and estimate_tokens(summary) > max_tokens:
            summary = truncate_to_tokens(summary, max_tokens)
        new_text = summary
    else:  # DROP_LOW_PRIORITY 的单块形态 == TRUNCATE（丢弃在批量层做）
        new_text = truncate_to_tokens(chunk.text, max_tokens)

    return _build_chunk(chunk, new_text)


def _drop_low_priority(
    chunks: Sequence[Chunk],
    *,
    budget_tokens: int,
    protected_ids: frozenset[str],
    counter: TokenCounter | None,
) -> tuple[list[Chunk], list[Chunk]]:
    """Greedily drop the least-important non-protected chunks until within budget."""
    total = sum(
        (measure_tokens(c.text, counter=counter) or 0) for c in chunks
    )
    if total <= budget_tokens:
        return list(chunks), []

    reducible = [c for c in chunks if c.chunk_id not in protected_ids]
    # priority asc (least important first), then later index first (stable tie-break).
    ordered = sorted(reducible, key=lambda c: (c.priority, -c.index))
    keep = list(chunks)
    dropped: list[Chunk] = []
    running = total
    for victim in ordered:
        if running <= budget_tokens:
            break
        running -= measure_tokens(victim.text, counter=counter) or 0
        keep.remove(victim)
        dropped.append(victim)
    return keep, dropped


def _reduce_reducible(
    chunks: Sequence[Chunk],
    *,
    budget_tokens: int,
    protected_ids: frozenset[str],
    strategy: CompactionStrategy,
    summarizer: Summarizer | None,
    counter: TokenCounter | None,
) -> tuple[list[Chunk], list[Chunk], list[Chunk]]:
    """Truncate / summarise non-protected chunks to fit the budget (greedy, deterministic).

    Returns ``(kept, dropped, compacted)``.
    """
    protected = [c for c in chunks if c.chunk_id in protected_ids]
    reducible = [c for c in chunks if c.chunk_id not in protected_ids]
    protected_tokens = sum(
        (measure_tokens(c.text, counter=counter) or 0) for c in protected
    )
    available = budget_tokens - protected_tokens  # 可分配给非受保护块的额度

    # Most-important reducible chunks first; least-important are the ones we compress.
    ordered = sorted(reducible, key=lambda c: (c.priority, -c.index))
    kept: list[Chunk] = list(protected)
    dropped: list[Chunk] = []
    compacted: list[Chunk] = []
    remaining = available
    for chunk in ordered:
        cost = measure_tokens(chunk.text, counter=counter) or 0
        if remaining <= 0:
            dropped.append(chunk)
            continue
        if cost <= remaining:
            kept.append(chunk)
            remaining -= cost
            continue
        # Doesn't fit whole ⇒ compress this one to the remaining budget.
        reduced = compact_one(
            chunk,
            policy=_allow_all(),
            max_tokens=max(1, remaining),
            strategy=strategy,
            summarizer=summarizer,
        )
        reduced_cost = measure_tokens(reduced.text, counter=counter) or 0
        if reduced_cost <= 0 or reduced_cost > remaining:
            dropped.append(chunk)
            continue
        kept.append(reduced)
        compacted.append(reduced)
        remaining -= reduced_cost
    return kept, dropped, compacted


def _allow_all() -> WhitelistConfig:
    """A permissive policy used *internally* on already-filtered reducible chunks.

    Safe by construction: :func:`_reduce_reducible` only ever passes chunks that were
    already proven non-protected, so this never weakens the whitelist.
    """
    return WhitelistConfig(
        protected_tags=frozenset(),
        protected_kinds=frozenset(),
        protected_chunk_ids=frozenset(),
    )


def compact(
    chunks: Sequence[Chunk],
    *,
    budget_tokens: int,
    policy: WhitelistConfig | None = None,
    strategy: CompactionStrategy = CompactionStrategy.DROP_LOW_PRIORITY,
    counter: TokenCounter | None = estimate_tokens,
    summarizer: Summarizer | None = None,
) -> CompactResult:
    """Compact ``chunks`` to fit ``budget_tokens``, **never touching** whitelist content.

    Args:
        chunks: 已分块的上下文。
        budget_tokens: 目标预算（token，估算口径 —— 见 ``counter``）。
        policy: 白名单配置（默认保护 :data:`DEFAULT_PROTECTED_TAGS`）。
        strategy: 压缩策略。
        counter: 计数器（默认 = 既有字符启发式估算器，**非精确 tokenizer**）。
        summarizer: 摘要器（仅 :attr:`CompactionStrategy.SUMMARIZE` 用）；``None`` ⇒
            :func:`_default_summarizer`。

    Returns:
        :class:`CompactResult`；``within_budget`` 恒为 ``True``（否则已抛错）。

    Raises:
        ContextBudgetError: 压缩后**仍**超预算（例如受保护内容本身超预算）⇒ 显式失败，
            绝不静默丢弃关键信息（红线 16）。
    """
    policy = policy or WhitelistConfig()
    all_chunks = list(chunks)
    protected_ids = policy.protected_ids(all_chunks)

    before = account_chunks(
        all_chunks, budget_tokens=budget_tokens, counter=counter,
        protected_ids=protected_ids,
    )

    if strategy is CompactionStrategy.DROP_LOW_PRIORITY:
        kept, dropped = _drop_low_priority(
            all_chunks,
            budget_tokens=budget_tokens,
            protected_ids=protected_ids,
            counter=counter,
        )
        compacted: list[Chunk] = []
    else:
        kept, dropped, compacted = _reduce_reducible(
            all_chunks,
            budget_tokens=budget_tokens,
            protected_ids=protected_ids,
            strategy=strategy,
            summarizer=summarizer,
            counter=counter,
        )

    reasons: dict[str, str] = {}
    for chunk in dropped:
        reasons[chunk.chunk_id] = (
            "protected_over_budget"
            if chunk.chunk_id in protected_ids
            else "drop_low_priority"
        )
    for chunk in compacted:
        reasons[chunk.chunk_id] = (
            "summarized" if strategy is CompactionStrategy.SUMMARIZE else "truncated"
        )

    kept_ids = frozenset(c.chunk_id for c in kept)
    # The **post-compaction** ledger must measure the *final* chunk bodies (a truncated /
    # summarised chunk has new text), so feed it the post-state chunks — in original order.
    final_by_id: dict[str, Chunk] = {c.chunk_id: c for c in kept}
    final_by_id.update({c.chunk_id: c for c in dropped})
    post_chunks = [final_by_id.get(c.chunk_id, c) for c in all_chunks]
    after = account_chunks(
        post_chunks,
        budget_tokens=budget_tokens,
        counter=counter,
        protected_ids=protected_ids,
        kept_ids=kept_ids,
        reasons=reasons,
    )

    # 显式把关：压缩后仍超预算 ⇒ 抛错（例如受保护内容本身超预算）。
    assert_within_budget(after)

    return CompactResult(
        kept=list(kept),
        dropped=list(dropped),
        compacted=list(compacted),
        ledger_before=before,
        ledger_after=after,
        strategy=strategy,
        within_budget=True,
    )
