"""INC46 T29 — 按章节可寻址的加载（Section-Addressable Loading）。

任务书 T29 规格
---------------
「按章节可寻址的分块加载：**只加载目标区间 ± 邻接上下文 + 文档大纲**。」

本模块把这条规格落成一个**可复算**的加载计划 :class:`SectionLoadPlan`：给定一份真实
DOCX 与一个目标选择子（「第三部分」/「第 3 章」…），它

1. 用 :func:`forgeflow.documents.docx_inspect.inspect_docx` 读出**真实大纲**（标题层级 +
   每节的段落区间），用 :func:`forgeflow.documents.locator.locate` 把选择子锚定到一个
   **真实目标区间**（歧义 / 未找到如实上报，绝不猜测）；
2. 用 :func:`forgeflow.documents.invariants.extract_invariants` 抽出目标区间内的
   :class:`~forgeflow.documents.invariants.InvariantSet`（编辑必须保留的显式不变量 + 隐式基线）；
3. **只**把「目标区间 + ±邻接节 + 大纲」装进上下文（其余章节**不加载**），每块都由原文字符
   区间切片复原（:func:`forgeflow.context.chunker.assert_boundaries_recomputable`），并逐块记账。

数据来源（不臆造）
------------------
目标区间 / 大纲 / 不变量**全部来自** :mod:`forgeflow.documents`（T20）—— 本模块只做
「区间 → 字符偏移 → 分块 → 记账」的**搬运**，绝不自己发明章节结构。

回写位置可被检出（本模块的承重性质）
------------------------------------
分块保留了每块在原文字符串里的 ``[start, end)`` 偏移。:func:`verify_writeback` 用该偏移
校验「回写必须落在原位置」：位置不符 ⇒ **抛** :class:`WriteBackError`（绝不静默写错位置）。
:func:`assert_outside_unchanged` 进一步钉住「目标区间外内容零变化」（隐式基线）。

诚实纪律（INC46 §8 红线 4）
---------------------------
* 未测量的 token ⇒ ``None``（见 :mod:`forgeflow.context.budget_accounting`），绝不写 0。
* 选择子歧义 / 未找到 ⇒ 目标为 ``None`` 且 ``notes`` 说明原因，**只加载大纲**，绝不编造目标。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Sequence

from forgeflow.context.budget_accounting import (
    BudgetLedger,
    TokenCounter,
    account_chunks,
    measure_tokens,
)
from forgeflow.context.chunker import KIND_DOCUMENT, Chunk, assert_boundaries_recomputable
from forgeflow.context.compactor import (
    TAG_EDIT_TARGET,
    TAG_HITL_PENDING,
    TAG_INVARIANTS,
    TAG_USER_INSTRUCTION,
    CompactResult,
    CompactionStrategy,
    WhitelistConfig,
    compact,
)
from forgeflow.documents.docx_inspect import (
    DocxInspectionError,
    inspect_docx,
    open_docx,
)
from forgeflow.documents.invariants import extract_invariants
from forgeflow.documents.locator import locate
from forgeflow.experience.token_budget import estimate_tokens

__all__ = [
    "SectionLoadError",
    "WriteBackError",
    "TAG_VERSION_CHAIN",
    "TAG_CLARIFICATION",
    "CONVERSATION_PROTECTED_TAGS",
    "load_sections",
    "verify_writeback",
    "assert_outside_unchanged",
    "assert_required_preserved",
    "compact_plan",
]

# --- 会话压缩白名单的**新增**类别（任务书 T29：版本链 ID / 未决澄清） -----------------
#: 白名单类别：版本链 ID —— **不可压缩**（T25 提供；缺席即缺席，绝不臆造）。
TAG_VERSION_CHAIN = "version_chain"
#: 白名单类别：未决澄清 —— **不可压缩**（T21 pending ``clarify``；缺席即缺席）。
TAG_CLARIFICATION = "clarification"

#: 会话压缩白名单（任务书 T29 明确「必须保留」的类别）：InvariantSet、待批 pending_id、
#: 版本链 ID、用户明确偏好、未决澄清。默认即包含全部五类；缺席的类别**只是没有对应块**，
#: 绝不为其臆造内容。
CONVERSATION_PROTECTED_TAGS: frozenset[str] = frozenset(
    {
        TAG_INVARIANTS,       # InvariantSet
        TAG_HITL_PENDING,     # 待批 pending_id
        TAG_VERSION_CHAIN,    # 版本链 ID（T25）
        TAG_USER_INSTRUCTION, # 用户明确偏好 / 原始指令
        TAG_EDIT_TARGET,      # 当前编辑目标
        TAG_CLARIFICATION,    # 未决澄清
    }
)


class SectionLoadError(Exception):
    """Raised when a document cannot be inspected for section-addressable loading."""


class WriteBackError(Exception):
    """Raised when a write-back is aimed at the **wrong** position.

    分块保留了每块的原始 ``[start, end)``；把某个块的新文本写回原文字符串时，位置必须
    恰好是该块的 ``start``。位置不符 ⇒ **显式报错**（绝不静默写错位置 —— 会导致内容错位）。
    """


def _digest(text: str) -> str:
    """Deterministic sha256 of ``text`` (UTF-8) — identical rule to the chunker."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _paragraph_offsets(paragraph_texts: Sequence[str]) -> tuple[list[int], list[int]]:
    """Per-paragraph ``(start, end)`` char offsets inside ``"\\n".join(texts)``."""
    starts: list[int] = []
    ends: list[int] = []
    cursor = 0
    for text in paragraph_texts:
        starts.append(cursor)
        ends.append(cursor + len(text))
        cursor += len(text) + 1  # the "\n" that joins paragraphs
    return starts, ends


@dataclass
class SectionLoadPlan:
    """The outcome of a section-addressable load (outline + target range ± adjacency).

    Attributes:
        source: 规范化原文（``"\\n".join(段落文本)``）；每个加载块都可由它切片复原。
        source_sha256: 原文摘要（复算证据）。
        full_document_tokens: **全文**的（估算）token 数；无法测量 ⇒ ``None``。
        outline: 大纲行（``{index, level, title, char_offset}``），按文档顺序。
        target_chunk: 命中的目标块（:class:`~forgeflow.context.chunker.Chunk`）；未命中 ⇒ ``None``。
        adjacent_chunks: ±邻接节块（文档顺序）。
        outline_chunk: 大纲块（派生，非原文切片）。
        invariants_chunk: InvariantSet 块（白名单保护）；无目标 ⇒ ``None``。
        loaded_chunks: 目标 + 邻接块（**原文切片**，可复算），文档顺序。
        ledger: 逐块 token 账（未测量 ⇒ ``None``）。
        whitelist: 本次加载使用的**会话压缩白名单**。
        located: 是否唯一命中目标。
        notes: 诚实说明（未命中 / 歧义 / 正常）。
    """

    source: str
    source_sha256: str
    full_document_tokens: int | None
    outline: list[dict[str, Any]] = field(default_factory=list)
    target_chunk: Chunk | None = None
    adjacent_chunks: list[Chunk] = field(default_factory=list)
    outline_chunk: Chunk | None = None
    invariants_chunk: Chunk | None = None
    loaded_chunks: list[Chunk] = field(default_factory=list)
    ledger: BudgetLedger | None = None
    whitelist: WhitelistConfig = field(default_factory=WhitelistConfig)
    located: bool = False
    notes: str = ""

    # -- 复算 / 查询 --------------------------------------------------------- #
    def measured_tokens(self, chunk: Chunk) -> int | None:
        """``chunk`` 的（估算）token 数；无法测量 ⇒ ``None``（红线 4）。"""
        return measure_tokens(chunk.text)

    @property
    def loaded_tokens(self) -> int:
        """目标 + 邻接块的**已测量** token 之和（未测量块不计入）。"""
        return sum(
            (measure_tokens(c.text) or 0) for c in self.loaded_chunks
        )

    @property
    def outline_tokens(self) -> int:
        """大纲块的**已测量** token 数（无大纲 ⇒ 0）。"""
        if self.outline_chunk is None:
            return 0
        return measure_tokens(self.outline_chunk.text) or 0

    @property
    def total_loaded_tokens(self) -> int:
        """本次**实际加载**的 token 总量 = 目标 + 邻接 + 大纲。"""
        return self.loaded_tokens + self.outline_tokens

    def budget_ratio(self) -> float | None:
        """``total_loaded_tokens / full_document_tokens``；无法测量 ⇒ ``None``（绝不写 0）。"""
        if self.full_document_tokens is None or self.full_document_tokens <= 0:
            return None
        return self.total_loaded_tokens / self.full_document_tokens

    def loaded_text(self) -> str:
        """目标 + 邻接块的正文（文档顺序，以空行相连）—— 真正装进上下文的部分。"""
        ordered = sorted(
            [c for c in [self.target_chunk, *self.adjacent_chunks] if c is not None],
            key=lambda c: c.start,
        )
        return "\n\n".join(c.text for c in ordered)

    def all_chunks(self) -> list[Chunk]:
        """目标 + 邻接 + 大纲 + 不变量（记账 / 压缩的统一集合）。"""
        chunks: list[Chunk] = list(self.loaded_chunks)
        if self.outline_chunk is not None:
            chunks.append(self.outline_chunk)
        if self.invariants_chunk is not None:
            chunks.append(self.invariants_chunk)
        return chunks

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe evidence snapshot (``None`` preserved, never coerced to 0)."""
        return {
            "source_sha256": self.source_sha256,
            "located": self.located,
            "notes": self.notes,
            "full_document_tokens": self.full_document_tokens,
            "loaded_tokens": self.loaded_tokens,
            "outline_tokens": self.outline_tokens,
            "total_loaded_tokens": self.total_loaded_tokens,
            "budget_ratio": self.budget_ratio(),
            "outline_count": len(self.outline),
            "target_chunk_id": self.target_chunk.chunk_id if self.target_chunk else None,
            "loaded_chunk_ids": [c.chunk_id for c in self.loaded_chunks],
            "loaded_sha256": [c.sha256 for c in self.loaded_chunks],
            "invariant_chunk_id": (
                self.invariants_chunk.chunk_id if self.invariants_chunk else None
            ),
            "protected_tags": sorted(self.whitelist.protected_tags),
        }


def _top_level_sections(
    headings: Sequence[dict[str, Any]],
    starts: Sequence[int],
    ends: Sequence[int],
) -> list[dict[str, Any]]:
    """Disjoint *top-level* sections — a heading nested under a shallower one is skipped.

    Returns dicts ``{head_index, level, title, para_start, para_end, char_start, char_end}``
    in document order. Top-level = no earlier heading with ``level <=`` its level is still
    "open"; this yields the outermost sections, so ± adjacency never points at a section's
    own child.
    """
    sections: list[dict[str, Any]] = []
    min_level: int | None = None
    for heading in headings:
        level = int(heading["level"])
        if min_level is None or level <= min_level:
            min_level = level
            head_index = int(heading["index"])
            para_end = int(heading["section_end_index"])
            end_para = max(head_index, para_end - 1)
            sections.append(
                {
                    "head_index": head_index,
                    "level": level,
                    "title": str(heading.get("text") or ""),
                    "para_start": head_index,
                    "para_end": para_end,
                    "char_start": starts[head_index],
                    "char_end": ends[min(end_para, len(ends) - 1)] if ends else 0,
                }
            )
    return sections


def _make_chunk(
    source: str,
    *,
    index: int,
    char_start: int,
    char_end: int,
    tags: frozenset[str],
    chunk_id: str | None = None,
) -> Chunk:
    """Build a source-slice :class:`Chunk` (``source[start:end] == text``)."""
    text = source[char_start:char_end]
    return Chunk(
        index=index,
        kind=KIND_DOCUMENT,
        text=text,
        start=char_start,
        end=char_end,
        priority=10,
        tags=tags,
        chunk_id=chunk_id or f"{KIND_DOCUMENT}:{index}:{char_start}-{char_end}",
        sha256=_digest(text),
    )


def _render_outline(headings: Sequence[dict[str, Any]]) -> str:
    """Render the document outline as indented ``- title`` lines (deterministic)."""
    lines: list[str] = []
    for heading in headings:
        level = max(0, int(heading["level"]))
        lines.append("  " * level + "- " + str(heading.get("text") or ""))
    return "\n".join(lines)


def _invariants_chunk(invariants: Any, *, index: int) -> Chunk:
    """A protected chunk carrying the InvariantSet (tagged ``invariants``)."""
    import json

    payload = invariants.to_dict() if hasattr(invariants, "to_dict") else invariants
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return Chunk(
        index=index,
        kind=KIND_DOCUMENT,
        text=text,
        start=0,
        end=0,
        priority=99,
        tags=frozenset({TAG_INVARIANTS}),
        chunk_id="invariants:0:0-0",
        sha256=_digest(text),
    )


def load_sections(
    document: bytes,
    *,
    selector: str | None = None,
    target_range: tuple[int, int] | None = None,
    adjacency: int = 1,
    budget_tokens: int | None = None,
    counter: TokenCounter | None = estimate_tokens,
    extra_protected_tags: frozenset[str] | None = None,
) -> SectionLoadPlan:
    """Load **only** the target range ± ``adjacency`` neighbours + the outline.

    Args:
        document: DOCX 字节（真实文档，非臆造）。
        selector: 目标选择子（如 ``"第三部分"``）；与 ``target_range`` 二选一。
        target_range: 显式段落区间 ``[start, end)``（可跨章节边界）；优先于 ``selector``。
        adjacency: 目标两侧各加载几节（默认 1；0 ⇒ 只加载目标节）。
        budget_tokens: 记账预算；``None`` ⇒ 取全文 token 数（即「全放得下」）。
        counter: 计数器；``None`` ⇒ 全部块**未测量**（``estimated_tokens=None``）。
        extra_protected_tags: 追加到会话白名单的受保护标签。

    Returns:
        :class:`SectionLoadPlan`。目标歧义 / 未找到 ⇒ ``located=False``、``target_chunk=None``，
        只加载大纲并在 ``notes`` 里如实说明（绝不猜测目标）。

    Raises:
        SectionLoadError: ``document`` 不是可读 DOCX。
    """
    try:
        structure = inspect_docx(document)
    except DocxInspectionError as exc:
        raise SectionLoadError(f"无法解析文档：{exc}") from exc

    doc = open_docx(document)
    paragraph_texts = [p.text for p in doc.paragraphs]
    source = "\n".join(paragraph_texts)
    starts, ends = _paragraph_offsets(paragraph_texts)

    outline = [
        {
            "index": int(h["index"]),
            "level": int(h["level"]),
            "title": str(h.get("text") or ""),
            "char_offset": starts[int(h["index"])] if starts else 0,
        }
        for h in structure.headings
    ]

    # --- resolve the target range ------------------------------------------------- #
    located = False
    notes = ""
    target_char: tuple[int, int] | None = None
    target_heading_index: int | None = None
    sections = _top_level_sections(structure.headings, starts, ends)

    if target_range is not None:
        lo, hi = int(target_range[0]), int(target_range[1])
        lo = max(0, min(lo, len(paragraph_texts) - 1)) if paragraph_texts else 0
        hi = max(lo + 1, min(hi, len(paragraph_texts)))
        target_char = (starts[lo], ends[hi - 1])
        target_heading_index = lo
        located = True
        notes = f"显式目标区间 paragraphs[{lo}:{hi}]"
    elif selector is not None:
        result = locate(structure, selector)
        if result.chosen is not None:
            target_heading_index = int(result.chosen.index)
            hit = next(
                (s for s in sections
                 if s["para_start"] <= target_heading_index < s["para_end"]),
                None,
            )
            if hit is None:
                target_char = (
                    starts[target_heading_index],
                    ends[max(target_heading_index, int(result.chosen.section_end_index) - 1)],
                )
            else:
                target_char = (hit["char_start"], hit["char_end"])
            located = True
            notes = f"唯一定位：{result.chosen.label!r}（confidence={result.chosen.confidence}）"
        else:
            notes = "目标选择子歧义或未找到：只加载大纲，不猜测目标"
    else:
        notes = "未给出选择子：只加载大纲"

    # --- assemble adjacency (disjoint sibling sections) --------------------------- #
    adjacent: list[Chunk] = []
    if located and target_char is not None:
        ts, te = target_char
        before = [s for s in sections if s["char_end"] <= ts]
        after = [s for s in sections if s["char_start"] >= te and s["char_start"] != ts]
        before = before[-adjacency:] if adjacency > 0 else []
        after = after[:adjacency] if adjacency > 0 else []
        chosen_sections = sorted(before + after, key=lambda s: s["char_start"])
    else:
        chosen_sections = []

    # --- build chunks ------------------------------------------------------------- #
    loaded_chunks: list[Chunk] = []
    idx = 0
    target_chunk: Chunk | None = None
    if located and target_char is not None:
        target_chunk = _make_chunk(
            source, index=idx, char_start=target_char[0], char_end=target_char[1],
            tags=frozenset({TAG_EDIT_TARGET}),
        )
        loaded_chunks.append(target_chunk)
        idx += 1
    for section in chosen_sections:
        chunk = _make_chunk(
            source, index=idx, char_start=section["char_start"],
            char_end=section["char_end"], tags=frozenset(),
        )
        loaded_chunks.append(chunk)
        adjacent.append(chunk)
        idx += 1

    # 边界可复算：每个加载块都必须是原文的精确切片。
    if loaded_chunks:
        assert_boundaries_recomputable(source, loaded_chunks)

    outline_text = _render_outline(structure.headings)
    outline_chunk = Chunk(
        index=idx,
        kind=KIND_DOCUMENT,
        text=outline_text,
        start=0,
        end=0,
        priority=50,
        tags=frozenset(),
        chunk_id="outline:0:0-0",
        sha256=_digest(outline_text),
    )
    idx += 1

    # InvariantSet —— 目标区间内的显式不变量 + 隐式基线（白名单保护）。
    invariants_chunk: Chunk | None = None
    if located and target_heading_index is not None:
        hit_section = next(
            (s for s in sections
             if s["para_start"] <= target_heading_index < s["para_end"]),
            None,
        )
        para_hi = hit_section["para_end"] if hit_section else len(paragraph_texts)
        para_lo = (
            target_range[0] if target_range is not None else target_heading_index
        )
        invariants = extract_invariants(document, start=para_lo, end=para_hi)
        invariants_chunk = _invariants_chunk(invariants, index=idx)
        idx += 1

    whitelist = WhitelistConfig(
        protected_tags=CONVERSATION_PROTECTED_TAGS
        | (extra_protected_tags or frozenset())
    )

    all_chunks = list(loaded_chunks) + [outline_chunk]
    if invariants_chunk is not None:
        all_chunks.append(invariants_chunk)
    protected_ids = whitelist.protected_ids(all_chunks)

    full_tokens = measure_tokens(source, counter=counter)
    budget = (
        int(budget_tokens)
        if budget_tokens is not None
        else int(full_tokens if full_tokens is not None else 0)
    )
    ledger = account_chunks(
        all_chunks,
        budget_tokens=budget,
        counter=counter,
        protected_ids=protected_ids,
    )

    return SectionLoadPlan(
        source=source,
        source_sha256=_digest(source),
        full_document_tokens=full_tokens,
        outline=outline,
        target_chunk=target_chunk,
        adjacent_chunks=adjacent,
        outline_chunk=outline_chunk,
        invariants_chunk=invariants_chunk,
        loaded_chunks=loaded_chunks,
        ledger=ledger,
        whitelist=whitelist,
        located=located,
        notes=notes,
    )


def compact_plan(
    plan: SectionLoadPlan,
    *,
    budget_tokens: int,
    strategy: CompactionStrategy = CompactionStrategy.DROP_LOW_PRIORITY,
) -> CompactResult:
    """Compact a plan's chunks under its **own** conversation whitelist.

    受保护内容（InvariantSet / 编辑目标 / 待批 …）在任何策略下**逐字节保留**；压缩后仍超
    预算 ⇒ 由 :func:`forgeflow.context.compactor.compact` 显式抛 :class:`ContextBudgetError`。
    """
    return compact(
        plan.all_chunks(),
        budget_tokens=budget_tokens,
        policy=plan.whitelist,
        strategy=strategy,
    )


def verify_writeback(
    source: str,
    chunk: Chunk,
    new_text: str,
    *,
    at_offset: int,
) -> str:
    """Write ``new_text`` back **only** at ``chunk``'s original ``start``.

    Args:
        source: 分块时的原文（``chunk`` 是它的一个切片）。
        chunk: 要回写的块（携带原始 ``[start, end)``）。
        new_text: 该块的新正文。
        at_offset: 调用方给出的回写起点。

    Returns:
        回写后的新原文。

    Raises:
        WriteBackError: 回写位置 ``at_offset != chunk.start``（错位回写）；或该块的边界
            在原样原文上不可复算（``source[start:end] != text``）。
    """
    if at_offset != chunk.start:
        raise WriteBackError(
            f"回写位置错误：块 {chunk.chunk_id} 的原始起点是 {chunk.start}，"
            f"实际回写位置 {at_offset}（差值 {at_offset - chunk.start}）—— 拒绝错位写入"
        )
    if source[chunk.start:chunk.end] != chunk.text:
        raise WriteBackError(
            f"块 {chunk.chunk_id} 边界不可复算：source[{chunk.start}:{chunk.end}] != text"
        )
    return source[:chunk.start] + new_text + source[chunk.end:]


def assert_outside_unchanged(before: str, after: str, chunk: Chunk) -> None:
    """Assert the write-back changed **only** ``chunk``'s range (隐式基线：区间外零变化）。

    Raises:
        AssertionError: 目标区间之外的内容发生了变化。
    """
    delta = len(after) - len(before)
    prefix_ok = after[:chunk.start] == before[:chunk.start]
    suffix_ok = after[chunk.end + delta:] == before[chunk.end:]
    assert prefix_ok and suffix_ok, (
        f"目标区间外内容发生变化（块 {chunk.chunk_id}）：区间外必须零变化"
    )


def assert_required_preserved(
    result: CompactResult, required: Sequence[Chunk]
) -> None:
    """Hard-fail unless every ``required`` chunk survived **byte-identical**.

    这是任务书要求的「必含字段检查」：压缩输出若缺 InvariantSet（或任何必保留块），本函数
    **抛** :class:`AssertionError`（**硬失败**，不是告警）。

    Raises:
        AssertionError: 任一必保留块被丢弃（缺失）或其 ``sha256`` 发生变化。
    """
    kept = {c.chunk_id: c.sha256 for c in result.kept}
    missing = [c.chunk_id for c in required if c.chunk_id not in kept]
    changed = [
        c.chunk_id for c in required
        if c.chunk_id in kept and kept[c.chunk_id] != c.sha256
    ]
    assert not missing and not changed, (
        "压缩未保留关键状态："
        f"缺失={missing}，被改写={changed}"
    )
