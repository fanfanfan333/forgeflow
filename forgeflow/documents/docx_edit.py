"""INC43 S4 (BE-2) — the DOCX edit plane: intent → ops → bytes (分层硬约束).

Three layers, deliberately separated so the model can **never** write a file:

  * :func:`resolve_intent` — the **LLM layer**. It turns a natural-language
    intent into a list of :class:`EditOp` and *never* touches the document
    bytes. The model output is untrusted: an unknown ``op`` (or an unparseable
    reply, or a missing model) yields ``None`` rather than a guess.
  * :func:`apply_edits` — the **Tool layer**. This is the only function here
    that writes bytes (via ``python-docx``). It rewrites whole paragraphs and
    therefore preserves paragraph styles, heading levels and the table
    structure; the model can never reach this layer directly.
  * :func:`compute_diff` — a **pure function** over two byte strings. It
    re-derives 「修改 N 处 / 新增 X / 删除 Y / 数字变化 Z」 from a paragraph-level
    LCS alignment, so every figure is recomputable and unit-testable.

Supported ops (exactly these; anything else raises :class:`UnknownEditOpError`)::

    replace_text      {"op": "replace_text", "match": "原", "replace": "新"}
    set_paragraph     {"op": "set_paragraph", "index": 0, "text": "整段新文本"}
    set_section_text  {"op": "set_section_text", "section": "标题", "text": "新正文"}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from forgeflow.documents.docx_inspect import (
    DocStructure,
    DocxInspectionError,
    heading_level,
    numbers_in_text,
    open_docx,
)
from forgeflow.documents.textdiff import DiffReport, diff_counts

__all__ = [
    "SUPPORTED_OPS",
    "EditOp",
    "UnknownEditOpError",
    "DiffReport",
    "apply_edits",
    "compute_diff",
    "numbers_removable_by_edits",
    "resolve_intent",
]

#: The op kinds the platform supports. Anything else is refused explicitly.
SUPPORTED_OPS: tuple[str, ...] = ("replace_text", "set_paragraph", "set_section_text")

_WS_RE = re.compile(r"\s+")


class UnknownEditOpError(ValueError):
    """Raised for an ``op`` outside :data:`SUPPORTED_OPS` (never silently ignored)."""


def _normalise_key(text: str | None) -> str:
    """Whitespace-collapsed, case-folded key for heading / section matching."""
    return _WS_RE.sub(" ", str(text or "").strip()).casefold()


# --------------------------------------------------------------------------- #
# EditOp                                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class EditOp:
    """One document edit intention (the LLM layer's only output type)."""

    op: str = ""
    match: str = ""
    replace: str = ""
    index: int | None = None
    text: str = ""
    section: str = ""

    def __post_init__(self) -> None:
        if self.op not in SUPPORTED_OPS:
            raise UnknownEditOpError(
                f"不支持的编辑操作：{self.op!r}（仅支持 {', '.join(SUPPORTED_OPS)}）"
            )
        if self.op == "replace_text" and not self.match:
            raise UnknownEditOpError("replace_text 需要非空的 match")
        if self.op == "set_paragraph" and self.index is None:
            raise UnknownEditOpError("set_paragraph 需要 index")
        if self.op == "set_section_text" and not self.section.strip():
            raise UnknownEditOpError("set_section_text 需要 section")

    @classmethod
    def from_dict(cls, data: Any) -> EditOp:
        """Build an op from a mapping; raise :class:`UnknownEditOpError` on junk."""
        if isinstance(data, EditOp):
            return data
        if not isinstance(data, dict):
            raise UnknownEditOpError(f"编辑操作必须是对象：{data!r}")
        raw_index = data.get("index")
        index: int | None = None
        if raw_index is not None:
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise UnknownEditOpError(f"index 非法：{raw_index!r}") from exc
        return cls(
            op=str(data.get("op") or "").strip(),
            match=str(data.get("match") or ""),
            replace=str(data.get("replace") or ""),
            index=index,
            text=str(data.get("text") or ""),
            section=str(data.get("section") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "match": self.match,
            "replace": self.replace,
            "index": self.index,
            "text": self.text,
            "section": self.section,
        }


def _coerce_op(item: Any) -> EditOp:
    """Accept an :class:`EditOp` or a raw mapping; refuse anything else."""
    if isinstance(item, EditOp):
        return item
    if isinstance(item, dict):
        return EditOp.from_dict(item)
    raise UnknownEditOpError(f"编辑操作必须是对象或 EditOp：{item!r}")


# --------------------------------------------------------------------------- #
# Structural helpers (paragraph-level; tables are never touched)               #
# --------------------------------------------------------------------------- #
def _set_text_preserving(paragraph: Any, text: str) -> None:
    """Replace a paragraph's whole text, keeping its run formatting + style.

    Writes into the first existing run (so character formatting survives) and
    blanks the remaining runs; a run-less paragraph gets one new run. The
    paragraph's own style (e.g. ``Heading 1``) is never changed.
    """
    runs = list(paragraph.runs)
    if runs:
        runs[0].text = text
        for run in runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def _insert_paragraph_after(reference: Any, text: str, style: Any = None) -> Any:
    """Insert a new body paragraph directly after ``reference`` (stdlib recipe)."""
    from docx.oxml import OxmlElement
    from docx.text.paragraph import Paragraph

    new_element = OxmlElement("w:p")
    reference._p.addnext(new_element)
    paragraph = Paragraph(new_element, reference._parent)
    if style is not None:
        paragraph.style = style
    if text:
        paragraph.add_run(text)
    return paragraph


def _remove_paragraph(paragraph: Any) -> None:
    element = paragraph._p
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def _find_section(paragraphs: list[Any], section: str) -> tuple[int, int] | None:
    """Return ``(heading_index, body_end)`` for a section title, or ``None``.

    The section runs from the matching heading up to (but excluding) the next
    heading of the same or a higher level — the same grouping rule
    :func:`docx_inspect.inspect_docx` uses, so edit targets and reported sections
    can never disagree.
    """
    key = _normalise_key(section)
    if not key:
        return None
    for index, paragraph in enumerate(paragraphs):
        level = heading_level(paragraph)
        if level is None or not paragraph.text.strip():
            continue
        if _normalise_key(paragraph.text) != key:
            continue
        end = len(paragraphs)
        for j in range(index + 1, len(paragraphs)):
            other = paragraphs[j]
            other_level = heading_level(other)
            if other_level is not None and other.text.strip() and other_level <= level:
                end = j
                break
        return (index, end)
    return None


# --------------------------------------------------------------------------- #
# Tool layer — the ONLY writer                                                #
# --------------------------------------------------------------------------- #
def _apply_replace(paragraphs: list[Any], match: str, replace: str) -> int:
    if not match:
        return 0
    count = 0
    for paragraph in paragraphs:
        text = paragraph.text
        if match in text:
            count += text.count(match)
            _set_text_preserving(paragraph, text.replace(match, replace))
    return count


def _apply_set_paragraph(paragraphs: list[Any], index: int | None, text: str) -> int:
    if index is None or index < 0 or index >= len(paragraphs):
        return 0
    paragraph = paragraphs[index]
    if paragraph.text == text:
        return 0
    _set_text_preserving(paragraph, text)
    return 1


def _apply_set_section(paragraphs: list[Any], section: str, text: str) -> int:
    span = _find_section(paragraphs, section)
    if span is None:
        return 0
    start, end = span
    body = [paragraphs[k] for k in range(start + 1, end)]
    if "\n".join(p.text for p in body) == text:
        return 0
    new_lines = text.split("\n") if text != "" else []
    body_style = body[-1].style if body else None
    reference = paragraphs[start]
    for i, line in enumerate(new_lines):
        if i < len(body):
            _set_text_preserving(body[i], line)
            reference = body[i]
        else:
            reference = _insert_paragraph_after(reference, line, style=body_style)
    for extra in body[len(new_lines):]:
        _remove_paragraph(extra)
    return max(len(new_lines), len(body))


def apply_edits(data: bytes, edits: list[Any]) -> tuple[bytes, int]:
    """Apply ``edits`` to ``data`` and return ``(new_bytes, n_changes)``.

    This is the layered core: the model proposed the intent, *this* function
    writes the file. ``n_changes`` counts the actual places touched:

      * ``replace_text`` — occurrences of ``match`` really replaced;
      * ``set_paragraph`` — ``1`` when the target paragraph's text changed;
      * ``set_section_text`` — paragraph positions re-written (>= 1 per section).

    Raises:
        UnknownEditOpError: an edit is not one of :data:`SUPPORTED_OPS`.
        DocxInspectionError: ``data`` is not a readable DOCX.
    """
    document = open_docx(data)
    ops = [_coerce_op(item) for item in (edits or [])]
    paragraphs = list(document.paragraphs)
    total = 0
    for op in ops:
        if op.op == "replace_text":
            total += _apply_replace(paragraphs, op.match, op.replace)
        elif op.op == "set_paragraph":
            total += _apply_set_paragraph(paragraphs, op.index, op.text)
        elif op.op == "set_section_text":
            total += _apply_set_section(paragraphs, op.section, op.text)
        # ``__post_init__`` already rejected any other ``op``.

    import io as _io

    buffer = _io.BytesIO()
    document.save(buffer)
    return buffer.getvalue(), total


# --------------------------------------------------------------------------- #
# Diff (pure)                                                                  #
# --------------------------------------------------------------------------- #
# ``DiffReport`` is now defined once in :mod:`forgeflow.documents.textdiff`
# (INC44 §2.2) and re-exported here so ``from forgeflow.documents import
# DiffReport`` keeps working. The historical dataclass lived in this module;
# moving it to the shared engine is a pure refactor — its shape (``modified`` /
# ``added`` / ``removed`` / ``numeric_changes`` + ``to_dict``) is unchanged.


def _paragraph_texts(data: bytes) -> list[str]:
    document = open_docx(data)
    return [p.text for p in document.paragraphs]


def compute_diff(old: bytes, new: bytes) -> DiffReport:
    """Diff two documents by their body-paragraph text sequence (pure function).

    * ``modified`` — paragraphs whose position keeps an (aligned) counterpart but
      whose text changed;
    * ``added`` / ``removed`` — paragraphs with no counterpart;
    * ``numeric_changes`` — of the ``modified`` paragraphs, how many have a
      different **numeric token set** (the 「数字变化 Z」 figure).

    Deterministic and side-effect-free: it only reads the two byte strings. The
    alignment + counting now delegate to :func:`textdiff.diff_counts` (the one
    shared engine), so the output is byte-for-byte identical to the historical
    implementation — pinned by the existing ``test_compute_diff_*`` nails.
    """
    old_paras = _paragraph_texts(old)
    new_paras = _paragraph_texts(new)
    return diff_counts(old_paras, new_paras, numbers_in_text)


def numbers_removable_by_edits(old_data: bytes, edits: list[Any]) -> set[str]:
    """Numeric tokens an explicit edit is *allowed* to remove.

    The data-consistency guard is "the original numbers must survive **except**
    where an edit deliberately touched them". This computes exactly that
    exception: every number mentioned in an edit's own text, plus the numbers in
    the original paragraph / section a ``set_paragraph`` / ``set_section_text``
    rewrites wholesale. Pure and deterministic.
    """
    ops = [_coerce_op(item) for item in (edits or [])]
    allowed: set[str] = set()
    for op in ops:
        allowed.update(numbers_in_text(op.match))
        allowed.update(numbers_in_text(op.replace))
        allowed.update(numbers_in_text(op.text))
        allowed.update(numbers_in_text(op.section))
    if not ops:
        return allowed
    try:
        paragraphs = list(open_docx(old_data).paragraphs)
    except DocxInspectionError:
        return allowed
    for op in ops:
        if op.op == "set_paragraph" and op.index is not None and 0 <= op.index < len(paragraphs):
            allowed.update(numbers_in_text(paragraphs[op.index].text))
        elif op.op == "set_section_text":
            span = _find_section(paragraphs, op.section)
            if span is not None:
                for k in range(span[0] + 1, span[1]):
                    allowed.update(numbers_in_text(paragraphs[k].text))
    return allowed


# --------------------------------------------------------------------------- #
# LLM layer (intent → ops). Never writes bytes.                                #
# --------------------------------------------------------------------------- #
_EDIT_SYSTEM = (
    "你是企业多智能体平台（ForgeFlow）的 DOCX 文档编辑意图解析器。"
    "你的唯一职责是把自然语言编辑需求解析成结构化的编辑操作数组，"
    "绝不输出文档正文之外的任何内容。"
)

_EDIT_INSTRUCTION = (
    "把下面的自然语言编辑需求解析为一个编辑操作数组。\n"
    "只允许这些 op：\n"
    "1. replace_text：逐段替换子串 —— "
    '{{"op":"replace_text","match":"原文子串","replace":"新子串"}}\n'
    "2. set_paragraph：整段替换（index 为 0 基段落序号）—— "
    '{{"op":"set_paragraph","index":0,"text":"新的整段文本"}}\n'
    "3. set_section_text：按标题文字整体替换某小节正文 —— "
    '{{"op":"set_section_text","section":"标题文字","text":"新的小节正文"}}\n'
    "只输出一个 JSON 对象，形如：\n"
    '{{"edits": [ ... ]}}\n'
    "文档结构（标题及段落序号）：\n{structure}\n"
    "编辑需求：{intent}"
)


def _extract_json(text: str | None) -> dict[str, Any] | None:
    """Lenient first-JSON-object parse (tolerates ``` fences and prose)."""
    if not text:
        return None
    body = str(text).strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body[:4].lower() == "json":
            body = body[4:]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _structure_summary(structure: DocStructure) -> str:
    lines = [
        f"段落数：{structure.paragraphs}；表格数：{structure.tables}；"
        f"标题数：{len(structure.headings)}"
    ]
    for heading in structure.headings[:60]:
        lines.append(f"#{heading['level']} [段落 {heading['index']}] {heading['text']}")
    if not structure.headings:
        lines.append("（文档没有标题样式段落）")
    return "\n".join(lines)


async def resolve_intent(
    data: bytes, intent: str, structure: DocStructure
) -> list[EditOp] | None:
    """Parse a natural-language intent into edit ops via the configured model.

    **This function never writes bytes** — it is the LLM layer. It returns
    ``None`` (an honest "no usable intent") when the model is unavailable, the
    reply cannot be parsed, or an op is not in :data:`SUPPORTED_OPS`; it never
    fabricates an operation.
    """
    if not str(intent or "").strip():
        return None
    try:
        from forgeflow.models.provider import get_model

        model = get_model()
    except Exception:  # noqa: BLE001 — a provider hiccup is an honest "unavailable"
        return None
    if model is None:
        return None

    # Classification / extraction must not spend the budget on a hidden trace:
    # a reasoning model with thinking ON returns empty content (measured on this
    # box). Force ``reasoning=False`` on a copy when the model supports it.
    try:
        if hasattr(model, "reasoning"):
            model = model.model_copy(update={"reasoning": False})
    except Exception:  # noqa: BLE001 — an unsupported flag must not break the call
        pass

    prompt = _EDIT_INSTRUCTION.format(
        structure=_structure_summary(structure), intent=str(intent)[:1200]
    )
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        message = await model.ainvoke(
            [SystemMessage(content=_EDIT_SYSTEM), HumanMessage(content=prompt)]
        )
    except Exception:  # noqa: BLE001 — any call failure degrades to None
        return None

    payload = _extract_json(getattr(message, "content", ""))
    if not isinstance(payload, dict):
        return None
    raw_edits = payload.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        return None
    ops: list[EditOp] = []
    for item in raw_edits:
        try:
            ops.append(_coerce_op(item))
        except UnknownEditOpError:
            return None
    return ops or None
