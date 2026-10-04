"""INC46 T29 — 上下文预算、分块与压缩（Context Budget & Compaction）。

本包为 **Agent 主循环（T28）** 提供「有预算、可压缩、压缩不丢关键信息」的上下文
装配接缝。三层职责互相解耦、各自可复算：

* :mod:`forgeflow.context.chunker` — 按**语义单元**（段落 / 消息 / 文档段）分块，
  **边界可复算**（同一输入两次分块结果逐字节一致，且每个块可由原文字符区间切片复原）。
* :mod:`forgeflow.context.budget_accounting` — 每块的 token 记账。**未测量 ⇒ ``None``，
  禁止写 0**（红线 4）。token 计数复用既有的
  :func:`forgeflow.experience.token_budget.estimate_tokens`（**非精确 tokenizer**：字符
  启发式），记账字段因此一律以 ``estimated_`` 前缀命名，**不冒充精确 token 数**。
* :mod:`forgeflow.context.compactor` — 超预算 ⇒ 按策略压缩（摘要 / 截断 / 丢弃低优先块），
  **白名单必保留且可配置**；压缩后仍超预算 ⇒ **显式报错**，绝不静默丢弃关键信息（红线 16）。

设计上与 T30 :mod:`forgeflow.skills.progressive_loader` 的记账同构（``measured_*`` /
``unmeasured_*`` 语义一致），便于 T28 统一消费。

本包**不**修改任何既有模块（含 :func:`forgeflow.experience.context_builder.build_context`
—— 三源召回唯一路径），只**消费**其输出形状。
"""

from __future__ import annotations

from forgeflow.context.budget_accounting import (
    BudgetLedger,
    ChunkAccount,
    ContextBudgetError,
    assert_within_budget,
    budget_violations,
    measure_tokens,
)
from forgeflow.context.chunker import (
    KIND_DOCUMENT,
    KIND_MESSAGE,
    KIND_PARAGRAPH,
    Chunk,
    assert_boundaries_recomputable,
    chunk_signature,
    segment,
    segment_document,
    segment_messages,
    segment_paragraphs,
)
from forgeflow.context.compactor import (
    DEFAULT_PROTECTED_TAGS,
    TAG_EDIT_TARGET,
    TAG_HITL_PENDING,
    TAG_INVARIANTS,
    TAG_USER_INSTRUCTION,
    CompactResult,
    CompactionStrategy,
    ProtectedContentError,
    WhitelistConfig,
    compact,
    compact_one,
)

__all__ = [
    # chunker
    "Chunk",
    "KIND_PARAGRAPH",
    "KIND_DOCUMENT",
    "KIND_MESSAGE",
    "segment",
    "segment_paragraphs",
    "segment_document",
    "segment_messages",
    "chunk_signature",
    "assert_boundaries_recomputable",
    # budget_accounting
    "ChunkAccount",
    "BudgetLedger",
    "ContextBudgetError",
    "measure_tokens",
    "budget_violations",
    "assert_within_budget",
    # compactor
    "WhitelistConfig",
    "CompactionStrategy",
    "CompactResult",
    "ProtectedContentError",
    "compact",
    "compact_one",
    "DEFAULT_PROTECTED_TAGS",
    "TAG_USER_INSTRUCTION",
    "TAG_EDIT_TARGET",
    "TAG_INVARIANTS",
    "TAG_HITL_PENDING",
]
