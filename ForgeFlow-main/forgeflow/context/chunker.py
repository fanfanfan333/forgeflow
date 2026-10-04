"""INC46 T29 — 语义单元分块（Semantic Chunking），**边界可复算**。

What this module is
-------------------
把一段上下文（一次对话、一个文档、一段自由文本）切成**语义单元**，作为预算与压缩的
最小处理单位。三类语义单元：

* ``paragraph`` — **段落**：以「一个或多个空行」为界切分自由文本。
* ``message``   — **消息**：一次对话里的每条 ``{role, content}``（一问一答各成一块）。
* ``document``  — **文档段**：Markdown 标题（``#`` / ``##`` …）为界的章节。

边界可复算（本模块的承重性质）
------------------------------
分块**绝不**依赖随机 / 时间 / 全局状态：给定同一输入，两次分块的
:func:`chunk_signature` **逐字符一致**。更强地，每个 :class:`Chunk` 都带
``start`` / ``end`` 两个**原文字符偏移**，满足 ``source[start:end] == chunk.text``
—— 即任何消费者都能用原文字符区间**独立复原**该块，从而验证分块没有偷偷改写内容。
:func:`assert_boundaries_recomputable` 把这个不变量固化成可断言函数。

诚实纪律（INC46 §8 红线 4）
---------------------------
* 分块只做**切分**，不做任何 token 计数 —— 计数的「未测量 ⇒ ``None``」纪律由
  :mod:`forgeflow.context.budget_accounting` 承担。
* 空 / 纯空白输入 ⇒ **空列表**（没有块），绝不伪造一个空块。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "Chunk",
    "KIND_PARAGRAPH",
    "KIND_DOCUMENT",
    "KIND_MESSAGE",
    "segment",
    "segment_paragraphs",
    "segment_document",
    "segment_messages",
    "canonical_message_text",
    "chunk_signature",
    "assert_boundaries_recomputable",
]

#: 语义单元类型标识（稳定字符串，进账 / 进证据）。
KIND_PARAGRAPH = "paragraph"
KIND_MESSAGE = "message"
KIND_DOCUMENT = "document"

#: 各类型块的**默认优先级**：数值越大越重要（压缩时越晚被丢弃）。
DEFAULT_PRIORITY: dict[str, int] = {
    KIND_MESSAGE: 20,
    KIND_PARAGRAPH: 10,
    KIND_DOCUMENT: 10,
}

#: 一条消息在规范化文本里渲染成 ``[role] content``。
_MESSAGE_RE = re.compile(r"\n[ \t]*\n+")
_DOC_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+\S", re.MULTILINE)


@dataclass(frozen=True)
class Chunk:
    """一个语义单元。

    Attributes:
        index: 在同一次分块中的**从 0 起**的顺序号（稳定）。
        kind: 语义单元类型（:data:`KIND_PARAGRAPH` / ``..._MESSAGE`` / ``..._DOCUMENT``）。
        text: 块的正文（**逐字节**等于原文字符区间 ``source[start:end]``）。
        start: 块在原文字符串里的**起始**偏移（含）。
        end: 块在原文字符串里的**结束**偏移（不含）。
        priority: 越大越重要；压缩时低优先块先被丢弃。
        tags: 语义标签（如 ``user_instruction``）；白名单据此判定是否受保护。
        chunk_id: 稳定标识，形如 ``"<kind>:<index>:<start>-<end>"``，**可复算**。
        sha256: ``text`` 的 UTF-8 摘要 —— 白名单保利的逐字节证据。
    """

    index: int
    kind: str
    text: str
    start: int
    end: int
    priority: int
    tags: frozenset[str] = field(default_factory=frozenset)
    chunk_id: str = ""
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe projection for accounting / evidence snapshots."""
        return {
            "chunk_id": self.chunk_id,
            "index": self.index,
            "kind": self.kind,
            "priority": self.priority,
            "tags": sorted(self.tags),
            "start": self.start,
            "end": self.end,
            "chars": len(self.text),
            "sha256": self.sha256,
        }


def _digest(text: str) -> str:
    """Deterministic sha256 hex digest of ``text`` (UTF-8)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _make_chunk(
    *,
    index: int,
    kind: str,
    text: str,
    start: int,
    end: int,
    priority: int | None,
    tags: Iterable[str],
) -> Chunk:
    """Assemble a :class:`Chunk`, computing the stable id + digest."""
    return Chunk(
        index=index,
        kind=kind,
        text=text,
        start=start,
        end=end,
        priority=DEFAULT_PRIORITY.get(kind, 10) if priority is None else int(priority),
        tags=frozenset(str(t) for t in tags),
        chunk_id=f"{kind}:{index}:{start}-{end}",
        sha256=_digest(text),
    )


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Shrink ``[start, end)`` inward past leading/trailing whitespace."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of blank-line-delimited paragraphs (deterministic)."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _MESSAGE_RE.finditer(text):
        s, e = _trim_span(text, cursor, match.start())
        if e > s:
            spans.append((s, e))
        cursor = match.end()
    s, e = _trim_span(text, cursor, len(text))
    if e > s:
        spans.append((s, e))
    return spans


def _document_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of Markdown sections (heading-delimited, deterministic).

    The text before the first heading (if non-empty) becomes a leading chunk; each
    heading starts a new chunk that runs up to the next heading.
    """
    spans: list[tuple[int, int]] = []
    heads = [m.start() for m in _DOC_HEADING_RE.finditer(text)]
    if not heads:
        s, e = _trim_span(text, 0, len(text))
        return [(s, e)] if e > s else []
    # preamble
    s, e = _trim_span(text, 0, heads[0])
    if e > s:
        spans.append((s, e))
    for i, hs in enumerate(heads):
        he = heads[i + 1] if i + 1 < len(heads) else len(text)
        s, e = _trim_span(text, hs, he)
        if e > s:
            spans.append((s, e))
    return spans


def _spans_for(text: str, kind: str) -> list[tuple[int, int]]:
    if kind == KIND_DOCUMENT:
        return _document_spans(text)
    # paragraph / message both split on blank lines.
    return _paragraph_spans(text)


def _norm_tags(tags: Mapping[int, Iterable[str]] | Sequence[Iterable[str]] | None,
               index: int) -> tuple[str, ...]:
    """Look up the tag set for chunk ``index`` from either a mapping or a sequence."""
    if tags is None:
        return ()
    if isinstance(tags, Mapping):
        return tuple(tags.get(index, ()))
    seq = list(tags)
    return tuple(seq[index]) if index < len(seq) else ()


def _norm_priority(priorities: Mapping[int, int] | Sequence[int] | None,
                   index: int) -> int | None:
    if priorities is None:
        return None
    if isinstance(priorities, Mapping):
        return priorities.get(index)
    seq = list(priorities)
    return int(seq[index]) if index < len(seq) else None


def segment(
    source: str,
    *,
    kind: str = KIND_PARAGRAPH,
    priorities: Mapping[int, int] | Sequence[int] | None = None,
    tags: Mapping[int, Iterable[str]] | Sequence[Iterable[str]] | None = None,
) -> list[Chunk]:
    """Split ``source`` into semantic chunks. Pure + deterministic.

    Args:
        source: 原文（``None`` / 空 ⇒ 返回 ``[]``）。
        kind: :data:`KIND_PARAGRAPH`（默认）或 :data:`KIND_DOCUMENT`。``KIND_MESSAGE``
            请改走 :func:`segment_messages`（它需要结构化的消息列表）。
        priorities: 可选的按块索引优先级覆盖（越大越重要）。
        tags: 可选的按块索引标签（白名单据此判定保护，见
            :class:`forgeflow.context.compactor.WhitelistConfig`）。

    Returns:
        稳定排序的 :class:`Chunk` 列表；每个块的 ``source[start:end] == text``。
    """
    if not source:
        return []
    spans = _spans_for(source, kind)
    chunks: list[Chunk] = []
    for index, (start, end) in enumerate(spans):
        chunks.append(
            _make_chunk(
                index=index,
                kind=kind,
                text=source[start:end],
                start=start,
                end=end,
                priority=_norm_priority(priorities, index),
                tags=_norm_tags(tags, index),
            )
        )
    return chunks


def segment_paragraphs(
    source: str,
    *,
    priorities: Mapping[int, int] | Sequence[int] | None = None,
    tags: Mapping[int, Iterable[str]] | Sequence[Iterable[str]] | None = None,
) -> list[Chunk]:
    """Convenience wrapper: paragraph segmentation."""
    return segment(source, kind=KIND_PARAGRAPH, priorities=priorities, tags=tags)


def segment_document(
    source: str,
    *,
    priorities: Mapping[int, int] | Sequence[int] | None = None,
    tags: Mapping[int, Iterable[str]] | Sequence[Iterable[str]] | None = None,
) -> list[Chunk]:
    """Convenience wrapper: Markdown-heading document segmentation."""
    return segment(source, kind=KIND_DOCUMENT, priorities=priorities, tags=tags)


def canonical_message_text(messages: Sequence[Mapping[str, Any]]) -> str:
    """Render ``messages`` into the canonical text whose spans back the chunks.

    每条消息渲染成 ``"[role] content"``，消息之间以空行（``"\\n\\n"``）相连 —— 这正是
    :func:`segment_messages` 计算偏移所依据的原文，故 ``canonical[start:end] == text``。
    """
    parts = [
        f"[{str(m.get('role', 'user'))}] {str(m.get('content', ''))}"
        for m in messages
    ]
    return "\n\n".join(parts)


def segment_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    priorities: Mapping[int, int] | Sequence[int] | None = None,
    tags: Mapping[int, Iterable[str]] | Sequence[Iterable[str]] | None = None,
) -> list[Chunk]:
    """Split a conversation into one chunk per message (deterministic).

    Offsets index into the canonical text from :func:`canonical_message_text`, so a
    consumer can re-derive ``canonical[start:end]`` for every message chunk.
    """
    if not messages:
        return []
    parts = [
        f"[{str(m.get('role', 'user'))}] {str(m.get('content', ''))}"
        for m in messages
    ]
    chunks: list[Chunk] = []
    cursor = 0
    for index, part in enumerate(parts):
        start = cursor
        end = start + len(part)
        chunks.append(
            _make_chunk(
                index=index,
                kind=KIND_MESSAGE,
                text=part,
                start=start,
                end=end,
                priority=_norm_priority(priorities, index),
                tags=_norm_tags(tags, index),
            )
        )
        cursor = end + len("\n\n")  # separator between messages
    return chunks


def chunk_signature(chunks: Sequence[Chunk]) -> str:
    """A stable digest of a segmentation — equal inputs ⇒ equal signatures.

    摘要只覆盖 **结构**（``index`` / ``kind`` / ``start`` / ``end`` / ``sha256``），
    不含任何时间 / 随机量，因此「同一输入两次分块结果一致」可被直接断言。
    """
    payload = [
        [c.index, c.kind, c.start, c.end, c.sha256]
        for c in chunks
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def assert_boundaries_recomputable(source: str, chunks: Sequence[Chunk]) -> None:
    """Assert every chunk can be recovered by slicing ``source``.

    Raises:
        AssertionError: 当存在 ``source[c.start:c.end] != c.text`` 的块（说明分块偷偷
            改写了原文，边界不可复算）。
    """
    for c in chunks:
        assert source[c.start:c.end] == c.text, (
            f"块 {c.chunk_id} 边界不可复算：source[{c.start}:{c.end}] != text"
        )
