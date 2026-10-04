"""INC46 T29 — 上下文管理与压缩（Context Budget & Compaction）。

Scope (阳性 / 阴性 / 反事实 / 红线 4 + 16):

* **分块（阳性）** — 按语义单元（段落 / 消息 / 文档段）分块；**边界可复算**：同一输入
  两次分块的 ``chunk_signature`` 一致，且每块可由原文字符区间切片复原。
* **压缩（阳性）** — 超预算上下文经压缩后落在预算内，且**白名单块逐字节仍在**
  （贴出 sha256 前后比对）。
* **记账** — 每块的（估算）token 数；压缩前后可复算（压缩后总量 = 各保留块之和）。
* **红线 4（阴性）** — token **未测量 ⇒ ``None``**，绝不写 ``0``；「已测量的 0」与
  「未测量」被区分开。
* **红线 16（阴性）** — 压缩后仍超预算 ⇒ 显式报错；白名单被压缩 ⇒ 显式拒绝，绝不静默。
* **反事实（真跑）** — 把「白名单保护」去掉 ⇒ 白名单保利用例**必须转红**。

所有断言都驱动 ``forgeflow.context`` 的**真实函数**，复用仓库既有 token 估算器
``forgeflow.experience.token_budget.estimate_tokens``（**非精确 tokenizer** —— 记账字段
因此名为 ``estimated_tokens``，不冒充精确值）。
"""

from __future__ import annotations

import pytest

from forgeflow.context import (
    BudgetLedger,
    Chunk,
    ContextBudgetError,
    ProtectedContentError,
    WhitelistConfig,
    assert_boundaries_recomputable,
    assert_within_budget,
    budget_violations,
    chunk_signature,
    compact,
    compact_one,
    measure_tokens,
    segment,
    segment_document,
    segment_messages,
    segment_paragraphs,
)
from forgeflow.context.budget_accounting import account_chunks
from forgeflow.context.compactor import (
    DEFAULT_PROTECTED_TAGS,
    TAG_EDIT_TARGET,
    TAG_USER_INSTRUCTION,
    CompactionStrategy,
)
from forgeflow.experience.token_budget import estimate_tokens

# --------------------------------------------------------------------------- #
# helpers — real chunks with real tags / priorities                            #
# --------------------------------------------------------------------------- #
def _filler(tag: str, *, words: int = 80) -> str:
    """A deterministic ASCII paragraph (~4 chars/token)."""
    return f"{tag} " + " ".join(["lorem", "ipsum", "dolor", "sit", "amet"] * (words // 5))


def _six_paragraph_source() -> str:
    """6 paragraphs: p0=user_instruction, p4=edit_target, the rest are filler."""
    paras = [
        _filler("USER-ORIGINAL-INSTRUCTION"),        # 0 — 白名单
        _filler("filler-one"),                       # 1 — 可压缩
        _filler("filler-two"),                       # 2 — 可压缩
        _filler("filler-three"),                     # 3 — 可压缩
        _filler("CURRENT-EDIT-TARGET"),              # 4 — 白名单
        _filler("filler-five"),                      # 5 — 可压缩
    ]
    return "\n\n".join(paras)


def _tagged_chunks() -> list[Chunk]:
    """The 6-paragraph world, with whitelist tags on p0/p4 (low priority on purpose)."""
    source = _six_paragraph_source()
    return segment_paragraphs(
        source,
        tags={0: [TAG_USER_INSTRUCTION], 4: [TAG_EDIT_TARGET]},
        # 白名单块优先级也刻意设低 —— 证明保护来自**白名单**，而非优先级。
        priorities={0: 1, 4: 1, 1: 10, 2: 10, 3: 10, 5: 10},
    )


def _tokens(chunk: Chunk) -> int:
    return estimate_tokens(chunk.text)


def _assert_whitelist_preserved(result, declared_protected: list[Chunk]) -> None:
    """The load-bearing 白名单保利 assertion (must turn red without the whitelist)."""
    before = {c.chunk_id: c.sha256 for c in declared_protected}
    after = {c.chunk_id: c.sha256 for c in result.kept}
    for chunk_id, digest in before.items():
        assert after.get(chunk_id) == digest, (
            f"白名单块 {chunk_id} 未被逐字节保留：压缩前 sha256={digest}，"
            f"压缩后={after.get(chunk_id)}"
        )


# --------------------------------------------------------------------------- #
# 1. 分块 — 边界可复算 & 确定性                                                  #
# --------------------------------------------------------------------------- #
def test_paragraph_chunking_is_deterministic_and_recomputable() -> None:
    source = _six_paragraph_source()
    a = segment_paragraphs(source)
    b = segment_paragraphs(source)
    # 确定性：同一输入两次分块，签名逐字符一致。
    assert chunk_signature(a) == chunk_signature(b)
    # 边界可复算：每块都能由原文字符区间切片复原。
    assert_boundaries_recomputable(source, a)
    assert len(a) == 6
    assert [c.index for c in a] == list(range(6))


def test_message_chunking_deterministic_and_offsets() -> None:
    messages = [
        {"role": "user", "content": "请修改第三章标题"},
        {"role": "assistant", "content": "好的，以下是建议"},
        {"role": "user", "content": "再调整字号"},
    ]
    a = segment_messages(messages)
    b = segment_messages(messages)
    assert chunk_signature(a) == chunk_signature(b)
    assert len(a) == 3
    assert all(c.kind == "message" for c in a)
    # 单条消息 = 一条消息段；content 逐字在内。
    assert "请修改第三章标题" in a[0].text
    # 偏移自洽：相邻块的 start 差恒为 len(text)+2（"\n\n" 分隔符）。
    assert a[1].start == a[0].end + 2


def test_document_chunking_splits_on_markdown_headings() -> None:
    doc = "# 标题一\n正文甲\n\n## 标题二\n正文乙\n\n正文丙"
    chunks = segment_document(doc)
    assert len(chunks) == 2
    assert chunks[0].text.startswith("# 标题一")
    assert chunks[1].text.startswith("## 标题二")
    assert_boundaries_recomputable(doc, chunks)
    # 复算：同一输入两次一致。
    assert chunk_signature(chunks) == chunk_signature(segment_document(doc))


def test_chunk_signature_changes_when_content_changes() -> None:
    """阳性对照：内容变了签名必须变（否则上面的「一致」是恒真）。"""
    a = segment_paragraphs("alpha beta")
    b = segment_paragraphs("alpha gamma")
    assert chunk_signature(a) != chunk_signature(b)


def test_empty_source_yields_no_chunks() -> None:
    assert segment("") == []
    assert segment_paragraphs(None) == []  # type: ignore[arg-type]
    assert segment_messages([]) == []


# --------------------------------------------------------------------------- #
# 2. 记账 — 红线 4：未测量 ⇒ None，禁止写 0                                       #
# --------------------------------------------------------------------------- #
def test_measure_tokens_none_when_unmeasured_and_zero_is_measured() -> None:
    # 未测量：无正文 / 无计数器 ⇒ None。
    assert measure_tokens(None) is None
    assert measure_tokens("hello", counter=None) is None
    # 已测量的 0：空串走真计数器得到 0 —— 这是「已测量的 0」，不是「未测量」。
    zero = measure_tokens("", counter=estimate_tokens)
    assert zero == 0 and zero is not None
    # 一致性：非空正文得到正整数。
    pos = measure_tokens("hello world", counter=estimate_tokens)
    assert isinstance(pos, int) and pos > 0


def test_ledger_unmeasured_is_none_not_zero() -> None:
    chunks = _tagged_chunks()
    # counter=None ⇒ 全部未测量：字段必须是 None，且**明确不是 0**。
    ledger = account_chunks(chunks, budget_tokens=10_000, counter=None)
    assert all(a.estimated_tokens is None for a in ledger.accounts)
    for a in ledger.accounts:
        assert a.estimated_tokens != 0  # None ≠ 0，禁止冒充
    assert ledger.measured_tokens() == 0  # 未测量不计入总量
    assert len(ledger.unmeasured_accounts()) == len(chunks)  # 但被单独点名，不被吞掉
    assert ledger.snapshot()["unmeasured_count"] == len(chunks)


def test_ledger_measures_and_accounts_kept_vs_dropped() -> None:
    chunks = _tagged_chunks()
    ledger = account_chunks(chunks, budget_tokens=350)
    assert all(a.estimated_tokens is not None and a.estimated_tokens > 0 for a in ledger.accounts)
    # 未显式标记 ⇒ 默认全保留；总量 = 各块之和。
    assert ledger.kept_tokens() == sum(_tokens(c) for c in chunks)
    assert ledger.measured_tokens() == ledger.kept_tokens()


# --------------------------------------------------------------------------- #
# 3. 阳性 — 超预算 ⇒ 压缩后落在预算内，且白名单逐字节仍在                          #
# --------------------------------------------------------------------------- #
def test_positive_over_budget_compaction_fits_and_whitelist_byte_identical() -> None:
    chunks = _tagged_chunks()
    declared_protected = [c for c in chunks if c.index in (0, 4)]
    total = sum(_tokens(c) for c in chunks)
    budget = 350
    assert total > budget, f"前置条件：应超预算（total={total}, budget={budget}）"

    result = compact(chunks, budget_tokens=budget)

    # 压缩后落在预算内。
    assert result.within_budget is True
    assert sum(_tokens(c) for c in result.kept) <= budget
    # 白名单块逐字节仍在（贴出 sha256 前后比对）。
    kept_index = {k.chunk_id: k for k in result.kept}
    print(
        "[T29 whitelist sha256 before->after] "
        + " | ".join(
            f"{c.chunk_id}:{c.sha256[:12]}->"
            + (kept_index[c.chunk_id].sha256[:12] if c.chunk_id in kept_index else "MISSING")
            for c in declared_protected
        )
    )
    for c in declared_protected:
        kept = kept_index.get(c.chunk_id)
        assert kept is not None, f"白名单块 {c.chunk_id} 被丢弃了"
        assert kept.text == c.text  # 逐字节
        assert kept.sha256 == c.sha256
    assert result.protected_preserved(WhitelistConfig()) is True
    # 确有非白名单块被丢弃（证明真的压缩了，不是恒等通过）。
    assert result.dropped, "应有低优先块被丢弃"


def test_compaction_accounting_before_after_is_recomputable() -> None:
    chunks = _tagged_chunks()
    budget = 350
    result = compact(chunks, budget_tokens=budget)

    after = result.ledger_after
    assert after is not None
    # 压缩后总量恒等于「各保留块估算值之和」——独立复算。
    kept_by_id = {c.chunk_id: c for c in result.kept}
    recomputed = sum(_tokens(c) for c in kept_by_id.values())
    assert after.kept_tokens() == recomputed
    # 账本口径：kept 标记与真实保留集一致。
    assert {a.chunk_id for a in after.accounts if a.kept} == set(kept_by_id)
    # 压缩前记账覆盖全部块，且不含未测量。
    assert result.ledger_before is not None
    assert len(result.ledger_before.accounts) == len(chunks)
    assert result.ledger_before.unmeasured_accounts() == []


def test_no_compaction_when_already_within_budget() -> None:
    chunks = _tagged_chunks()
    total = sum(_tokens(c) for c in chunks)
    result = compact(chunks, budget_tokens=total + 1)
    assert len(result.kept) == len(chunks)
    assert result.dropped == [] and result.compacted == []


# --------------------------------------------------------------------------- #
# 4. 阴性 — 白名单被压缩 ⇒ 显式拒绝（不得静默）                                    #
# --------------------------------------------------------------------------- #
def test_negative_compacting_whitelist_chunk_raises() -> None:
    chunks = _tagged_chunks()
    protected = chunks[0]  # user_instruction
    with pytest.raises(ProtectedContentError) as exc:
        compact_one(protected, policy=WhitelistConfig(), max_tokens=1)
    msg = str(exc.value)
    assert protected.chunk_id in msg and "白名单" in msg

    # 对照：非白名单块可正常压缩（证明拒绝来自白名单，而非「谁都拒绝」）。
    filler = chunks[1]
    reduced = compact_one(filler, policy=WhitelistConfig(), max_tokens=5,
                          strategy=CompactionStrategy.TRUNCATE)
    assert _tokens(reduced) <= 5
    assert reduced.sha256 != filler.sha256


def test_negative_protected_alone_over_budget_raises_explicitly() -> None:
    """受保护内容本身超预算 ⇒ 无法在不丢关键信息下满足预算 ⇒ 显式失败（红线 16）。"""
    chunks = _tagged_chunks()
    protected_tokens = sum(
        _tokens(c) for c in chunks if c.index in (0, 4)
    )
    budget = protected_tokens - 1  # 白名单都放不下
    with pytest.raises(ContextBudgetError) as exc:
        compact(chunks, budget_tokens=budget)
    msg = str(exc.value)
    assert "超过预算" in msg
    assert str(protected_tokens) in msg, "错误消息必须给出实测总量（可复算）"


def test_negative_assert_within_budget_raises_with_measured_total() -> None:
    chunks = _tagged_chunks()
    total = sum(_tokens(c) for c in chunks)
    ledger = account_chunks(chunks, budget_tokens=total - 1)
    violations = budget_violations(ledger)
    assert any("超过预算" in v for v in violations)
    with pytest.raises(ContextBudgetError) as exc:
        assert_within_budget(ledger)
    assert str(total) in str(exc.value)


def test_unmeasured_ledger_does_not_raise_but_is_flagged() -> None:
    """未测量**不**触发超预算异常（它既不是 0 也不是已知值），但必须被点名。"""
    chunks = _tagged_chunks()
    ledger = account_chunks(chunks, budget_tokens=1, counter=None)
    assert_within_budget(ledger)  # 不抛：未测量不计入总量
    assert any("未测量" in v for v in budget_violations(ledger))


# --------------------------------------------------------------------------- #
# 5. 反事实（真跑）— 去掉「白名单保护」⇒ 保利断言转红                              #
# --------------------------------------------------------------------------- #
def test_counterfactual_removing_whitelist_turns_preservation_red() -> None:
    chunks = _tagged_chunks()
    declared_protected = [c for c in chunks if c.index in (0, 4)]
    budget = 350

    # (a) 默认白名单：保利断言通过。
    guarded = compact(chunks, budget_tokens=budget)
    _assert_whitelist_preserved(guarded, declared_protected)  # must NOT raise

    # (b) 反事实（真跑）：把白名单清空 ⇒ 「白名单块」被当作普通块丢弃。
    unguarded = compact(
        chunks,
        budget_tokens=budget,
        policy=WhitelistConfig(
            protected_tags=frozenset(),
            protected_kinds=frozenset(),
            protected_chunk_ids=frozenset(),
        ),
    )
    with pytest.raises(AssertionError) as exc:
        _assert_whitelist_preserved(unguarded, declared_protected)
    assert "未被逐字节保留" in str(exc.value)
    # 可观测证据：无白名单时确实有「白名单块」被丢。
    kept_ids = {c.chunk_id for c in unguarded.kept}
    assert any(c.chunk_id not in kept_ids for c in declared_protected)


# --------------------------------------------------------------------------- #
# 6. 其他策略 — 截断 / 摘要均不碰白名单                                          #
# --------------------------------------------------------------------------- #
def test_truncate_strategy_never_touches_protected() -> None:
    chunks = _tagged_chunks()
    declared_protected = [c for c in chunks if c.index in (0, 4)]
    result = compact(
        chunks, budget_tokens=350, strategy=CompactionStrategy.TRUNCATE
    )
    for c in declared_protected:
        kept = next(k for k in result.kept if k.chunk_id == c.chunk_id)
        assert kept.text == c.text and kept.sha256 == c.sha256
    assert sum(_tokens(c) for c in result.kept) <= 350


def test_summarize_strategy_produces_deterministic_summary_and_keeps_protected() -> None:
    chunks = _tagged_chunks()
    declared_protected = [c for c in chunks if c.index in (0, 4)]
    a = compact(chunks, budget_tokens=350, strategy=CompactionStrategy.SUMMARIZE)
    b = compact(chunks, budget_tokens=350, strategy=CompactionStrategy.SUMMARIZE)
    # 确定性：两次摘要结果一致。
    assert [c.text for c in a.kept] == [c.text for c in b.kept]
    # 白名单逐字节不变。
    for c in declared_protected:
        kept = next(k for k in a.kept if k.chunk_id == c.chunk_id)
        assert kept.sha256 == c.sha256
    assert sum(_tokens(c) for c in a.kept) <= 350


# --------------------------------------------------------------------------- #
# 7. 记账快照（证据用）                                                          #
# --------------------------------------------------------------------------- #
def test_compaction_snapshot_evidence() -> None:
    chunks = _tagged_chunks()
    declared_protected = [c for c in chunks if c.index in (0, 4)]
    result = compact(chunks, budget_tokens=350)
    before = result.ledger_before.snapshot()
    after = result.ledger_after.snapshot()
    print(
        "[T29 snapshot] "
        f"before: measured={before['measured_tokens']} accounts={before['account_count']} | "
        f"after: kept={after['kept_tokens']} kept_count="
        f"{sum(1 for a in after['accounts'] if a['kept'])} | "
        f"sha256 protected="
        + ",".join(f"{c.chunk_id}:{c.sha256[:10]}" for c in declared_protected)
    )
    # 压缩后 kept 总量 ≤ 预算；且 = 各保留块估算值之和。
    assert after["kept_tokens"] <= 350
    assert after["kept_tokens"] == sum(
        a["estimated_tokens"] for a in after["accounts"]
        if a["kept"] and a["estimated_tokens"] is not None
    )
    assert DEFAULT_PROTECTED_TAGS  # 白名单默认不可为空


def test_whitelist_config_is_configurable() -> None:
    """白名单**可配置**：新增受保护类型 / 具体块 id 均生效。"""
    doc = "# 不可动标题\n正文甲"
    chunks = segment_document(doc)
    assert not WhitelistConfig().is_protected(chunks[0])
    by_kind = WhitelistConfig(protected_kinds=frozenset({"document"}))
    assert by_kind.is_protected(chunks[0])
    by_id = WhitelistConfig(protected_chunk_ids=frozenset({chunks[0].chunk_id}))
    assert by_id.is_protected(chunks[0])
