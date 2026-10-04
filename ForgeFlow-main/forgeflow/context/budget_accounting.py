"""INC46 T29 — 上下文记账（Budget Accounting）：每块的 token 数与「已测量 / 未测量」。

诚实纪律（本模块的承重性质，INC46 §8 红线 4）
--------------------------------------------
* **未测量 ⇒ ``None``，禁止写 ``0``。** 当没有可用的计数器（``counter is None``）
  或块正文为 ``None`` 时，记账字段 :attr:`ChunkAccount.estimated_tokens` 必须是
  ``None`` —— 它表示「**不知道**多少 token」，而不是「0 个 token」。
  :func:`measure_tokens` 把这条纪律固化在唯一的测量入口。
* 「已测量的 0」与「未测量」是两件事：空串 ``""`` 走真计数器得到**已测量的 0**；
  而 ``None`` 输入得到 **未测量**（``None``）。二者不得相互冒充。
* :meth:`BudgetLedger.measured_tokens` 只累加**已测量**的块；未测量的块**不计入**
  总量（它们既不是 0，也不是已知值），并由 :meth:`BudgetLedger.unmeasured_accounts`
  单独点名，**绝不**被静默吞掉。

token 计数：**非精确 tokenizer**（诚实声明）
--------------------------------------------
本模块**复用**既有的 :func:`forgeflow.experience.token_budget.estimate_tokens`
（字符启发式：CJK ≈ 1.7 chars/token，其余 ≈ 4 chars/token）。**它不是 tiktoken 那样的
精确 BPE 计数**，因此所有记账字段一律以 ``estimated_`` 前缀命名（``estimated_tokens``），
**绝不**把估算值冒充精确 token 数。需要精确计数时，调用方可注入自定义 ``counter``。

与 T30 同构
-----------
本账本刻意与 T30 :class:`forgeflow.skills.progressive_loader.LoadLedger` 的
``measured_bytes`` / ``unmeasured_records`` 同构（此处为 ``measured_tokens`` /
``unmeasured_accounts``），便于 T28 Agent 主循环用同一套「已测量 / 未测量」语义消费。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from forgeflow.context.chunker import Chunk
from forgeflow.experience.token_budget import estimate_tokens

__all__ = [
    "TokenCounter",
    "ChunkAccount",
    "BudgetLedger",
    "ContextBudgetError",
    "measure_tokens",
    "account_chunks",
    "budget_violations",
    "assert_within_budget",
]

#: 计数器签名：``text -> token 数``。默认 = 复用既有字符启发式估算器。
TokenCounter = Callable[[str], int]


@dataclass
class ChunkAccount:
    """One accounted chunk: its (estimated) token count and kept/dropped state.

    Attributes:
        chunk_id: 对应 :class:`~forgeflow.context.chunker.Chunk` 的稳定标识。
        kind: 语义单元类型（段落 / 消息 / 文档段）。
        sha256: 块正文的 UTF-8 摘要（压缩前后的逐字节比对证据）。
        estimated_tokens: **估算**的 token 数；**未测量时为 ``None``**（红线 4），
            绝不写 0 冒充未测量。
        protected: 是否被白名单保护（受保护块不可压缩）。
        kept: 本块最终是否被保留。
        reason: 被丢弃 / 被压缩的原因（保留块为 ``None``）。
    """

    chunk_id: str
    kind: str
    sha256: str
    estimated_tokens: int | None = None
    protected: bool = False
    kept: bool = True
    reason: str | None = None

    @property
    def measured(self) -> bool:
        """True when this chunk's token count was actually measured."""
        return self.estimated_tokens is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection for the evidence snapshot."""
        return {
            "chunk_id": self.chunk_id,
            "kind": self.kind,
            "sha256": self.sha256,
            "estimated_tokens": self.estimated_tokens,  # None ⇒ 未测量（≠ 0）
            "protected": self.protected,
            "kept": self.kept,
            "reason": self.reason,
        }


def measure_tokens(
    text: str | None, *, counter: TokenCounter | None = estimate_tokens
) -> int | None:
    """Measure the token count of ``text`` — the **only** honest measurement入口.

    Args:
        text: 待测正文。``None`` ⇒ **未测量**（返回 ``None``）。
        counter: 计数器；``None`` ⇒ **未测量**（返回 ``None``，表示「无法测量」）。
            默认 = 既有字符启发式 :func:`estimate_tokens`（非精确 tokenizer）。

    Returns:
        ``int``（含**已测量的 0**，如空串）或 ``None``（未测量）。**绝不**用 0 冒充未测量。
    """
    if text is None or counter is None:
        return None
    return int(counter(text))


@dataclass
class BudgetLedger:
    """The append-only accounting of every chunk under a token budget.

    与 T30 ``LoadLedger`` 同构：``measured_tokens`` ≈ ``measured_bytes``，
    ``unmeasured_accounts`` ≈ ``unmeasured_records``。
    """

    budget_tokens: int
    accounts: list[ChunkAccount] = field(default_factory=list)

    # -- 查询 ---------------------------------------------------------------- #
    def measured_tokens(self) -> int:
        """Sum of the **measured** token counts.

        未测量块（``estimated_tokens is None``）**不计入**该总量 —— 它们不是 0，而是
        「不知道多少」；由 :meth:`unmeasured_accounts` 单独点名，不被静默吞掉。
        """
        return sum(
            a.estimated_tokens
            for a in self.accounts
            if a.estimated_tokens is not None
        )

    def kept_tokens(self) -> int:
        """Sum of the **measured** token counts of the chunks that were **kept**."""
        return sum(
            a.estimated_tokens
            for a in self.accounts
            if a.kept and a.estimated_tokens is not None
        )

    def unmeasured_accounts(self) -> list[ChunkAccount]:
        """Accounts whose token count was **not measured** (``None``)."""
        return [a for a in self.accounts if a.estimated_tokens is None]

    def protected_chunks(self) -> list[ChunkAccount]:
        """Accounts flagged as whitelist-protected."""
        return [a for a in self.accounts if a.protected]

    def by_id(self, chunk_id: str) -> ChunkAccount | None:
        """Look up an account by its chunk id (or ``None``)."""
        for a in self.accounts:
            if a.chunk_id == chunk_id:
                return a
        return None

    def snapshot(self) -> dict[str, Any]:
        """A reproducible evidence snapshot (measured / kept / unmeasured / per-chunk)."""
        unmeasured = self.unmeasured_accounts()
        return {
            "budget_tokens": self.budget_tokens,
            "measured_tokens": self.measured_tokens(),
            "kept_tokens": self.kept_tokens(),
            "account_count": len(self.accounts),
            "unmeasured_count": len(unmeasured),
            "unmeasured_chunk_ids": [a.chunk_id for a in unmeasured],
            "accounts": [a.to_dict() for a in self.accounts],
        }


class ContextBudgetError(Exception):
    """Raised when a context ledger breaches its token budget.

    This is the *explicit* failure mode demanded by T29 —— 压缩后仍超预算必须**显式**
    失败，绝不静默丢弃关键信息（红线 16）。消息里给出**实测**总量与预算，便于复算。
    """


def account_chunks(
    chunks: Sequence[Chunk],
    *,
    budget_tokens: int,
    counter: TokenCounter | None = estimate_tokens,
    protected_ids: frozenset[str] | None = None,
    kept_ids: frozenset[str] | None = None,
    reasons: dict[str, str] | None = None,
) -> BudgetLedger:
    """Build a :class:`BudgetLedger` for ``chunks``.

    Args:
        chunks: 待记账的块（顺序保留）。
        budget_tokens: 预算上限（写进账本，供 :func:`assert_within_budget` 复算）。
        counter: 计数器；``None`` ⇒ 全部块**未测量**（``estimated_tokens=None``）。
        protected_ids: 被白名单保护的 ``chunk_id`` 集合。
        kept_ids: 最终保留的 ``chunk_id`` 集合（``None`` ⇒ 视为全部保留）。
        reasons: ``chunk_id -> 原因``（如 ``"drop_low_priority"`` / ``"truncated"``）。
    """
    protected_ids = protected_ids or frozenset()
    reasons = reasons or {}
    ledger = BudgetLedger(budget_tokens=int(budget_tokens))
    for chunk in chunks:
        kept = True if kept_ids is None else (chunk.chunk_id in kept_ids)
        ledger.accounts.append(
            ChunkAccount(
                chunk_id=chunk.chunk_id,
                kind=chunk.kind,
                sha256=chunk.sha256,
                estimated_tokens=measure_tokens(chunk.text, counter=counter),
                protected=chunk.chunk_id in protected_ids,
                kept=kept,
                reason=reasons.get(chunk.chunk_id),
            )
        )
    return ledger


def budget_violations(ledger: BudgetLedger) -> list[str]:
    """Structured budget violations — empty list means "within budget".

    预算以**保留块**的已测量总量为准（:meth:`BudgetLedger.kept_tokens`）—— 被丢弃 / 被
    压缩掉的块不再占用上下文预算。

    * 保留块已测量总量超预算 ⇒ 一条含**实测**总量的违规；
    * 存在未测量块 ⇒ 一条点名块数的提示（未测量不计入总量，但不被吞掉）。
    """
    violations: list[str] = []
    total = ledger.kept_tokens()
    if total > ledger.budget_tokens:
        violations.append(
            f"上下文 token 总量 {total} 超过预算 {ledger.budget_tokens}"
            f"（超出 {total - ledger.budget_tokens}）"
        )
    unmeasured = ledger.unmeasured_accounts()
    if unmeasured:
        named = "；".join(a.chunk_id for a in unmeasured[:5])
        violations.append(
            f"{len(unmeasured)} 个上下文块 token 未测量（未测量 ⇒ None，不计入总量）：{named}"
        )
    return violations


def assert_within_budget(ledger: BudgetLedger) -> None:
    """Assert the ledger's **kept** measured tokens are within its budget.

    Raises:
        ContextBudgetError: 保留块已测量总量超过预算时显式抛错（绝不静默通过 —— 红线 16）。
            未测量块**不**触发本异常（它们由 :func:`budget_violations` 单独提示），
            但**绝不**被当作 0。
    """
    over = [v for v in budget_violations(ledger) if "超过预算" in v]
    if over:
        raise ContextBudgetError("；".join(over))
