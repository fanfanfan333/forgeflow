"""INC44 §2.2 — the PPTX edit plane: intent → ops → bytes (分层硬约束).

Three layers, deliberately separated so the model can **never** write a file:

  * :func:`resolve_intent` — the **LLM layer**. It turns a natural-language
    intent into a list of :class:`EditOp` and *never* touches the presentation
    bytes. The model output is untrusted: an unknown ``op`` (or an unparseable
    reply, or a missing model) yields ``None`` rather than a guess.
  * :func:`apply_edits` — the **Tool layer**. This is the only function here that
    writes bytes (via ``python-pptx``). It rewrites whole text-frame paragraphs
    and therefore preserves the surrounding shape / slide structure; the model
    can never reach this layer directly.
  * :func:`compute_diff` — a **pure function** over two byte strings. It
    re-derives 「修改 N 处 / 新增 X / 删除 Y / 数字变化 Z」 over the slides' text
    via the shared :mod:`forgeflow.documents.textdiff` engine, so every figure is
    recomputable and unit-testable.

Supported ops (exactly these; anything else raises :class:`UnknownEditOpError`)::

    replace_text     {"op": "replace_text", "match": "原", "replace": "新"}
    set_slide_title  {"op": "set_slide_title", "index": 0, "text": "新标题"}
    set_shape_text   {"op": "set_shape_text", "index": 0, "shape": "标题 1", "text": "新文本"}

``index`` is a **0-based slide index**; ``shape`` matches a shape by its real
``name`` (or, when numeric, by its position in the slide). Missing-target ops
never guess — they change nothing and report ``0``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from forgeflow.documents.docx_edit import UnknownEditOpError
from forgeflow.documents.docx_inspect import numbers_in_text
from forgeflow.documents.pptx_inspect import (
    PptxInspectionError,
    PptxStructure,
    open_pptx,
    shape_paragraph_texts,
)
from forgeflow.documents.textdiff import DiffReport, diff_counts

__all__ = [
    "SUPPORTED_OPS",
    "EditOp",
    "UnknownEditOpError",
    "apply_edits",
    "compute_diff",
    "numbers_removable_by_edits",
    "resolve_intent",
]

#: The op kinds the platform supports. Anything else is refused explicitly.
SUPPORTED_OPS: tuple[str, ...] = ("replace_text", "set_slide_title", "set_shape_text")

_WS_RE = re.compile(r"\s+")


# ``UnknownEditOpError`` is the **one** shared error class for every edit plane
# (docx / pptx / textfile): re-using ``docx_edit``'s definition (imported above)
# rather than a second, independent class means ``documents.UnknownEditOpError``
# catches an illegal op no matter which format raised it.


# --------------------------------------------------------------------------- #
# EditOp                                                                       #
# --------------------------------------------------------------------------- #
@dataclass
class EditOp:
    """One presentation edit intention (the LLM layer's only output type)."""

    op: str = ""
    match: str = ""
    replace: str = ""
    index: int | None = None
    text: str = ""
    shape: str = ""

    def __post_init__(self) -> None:
        if self.op not in SUPPORTED_OPS:
            raise UnknownEditOpError(
                f"不支持的编辑操作：{self.op!r}（仅支持 {', '.join(SUPPORTED_OPS)}）"
            )
        if self.op == "replace_text" and not self.match:
            raise UnknownEditOpError("replace_text 需要非空的 match")
        if self.op == "set_slide_title" and self.index is None:
            raise UnknownEditOpError("set_slide_title 需要 index")
        if self.op == "set_shape_text" and (self.index is None or not self.shape.strip()):
            raise UnknownEditOpError("set_shape_text 需要 index 与非空 shape")

    @classmethod
    def from_dict(cls, data: Any) -> "EditOp":
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
            shape=str(data.get("shape") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "match": self.match,
            "replace": self.replace,
            "index": self.index,
            "text": self.text,
            "shape": self.shape,
        }


def _coerce_op(item: Any) -> EditOp:
    """Accept an :class:`EditOp` or a raw mapping; refuse anything else."""
    if isinstance(item, EditOp):
        return item
    if isinstance(item, dict):
        return EditOp.from_dict(item)
    raise UnknownEditOpError(f"编辑操作必须是对象或 EditOp：{item!r}")


# --------------------------------------------------------------------------- #
# Structural helpers                                                           #
# --------------------------------------------------------------------------- #
def _set_paragraph_text(paragraph: Any, text: str) -> None:
    """Replace a paragraph's whole text, keeping its first run's formatting."""
    runs = list(getattr(paragraph, "runs", []) or [])
    if runs:
        runs[0].text = text
        for run in runs[1:]:
            run.text = ""
    else:
        run = paragraph.add_run()
        run.text = text


def _set_frame_text(frame: Any, text: str) -> None:
    """Replace a text frame's content with a single paragraph of ``text``."""
    paragraphs = list(getattr(frame, "paragraphs", []) or [])
    if paragraphs:
        _set_paragraph_text(paragraphs[0], text)
        for extra in paragraphs[1:]:
            element = getattr(extra, "_p", None)
            parent = element.getparent() if element is not None else None
            if parent is not None:
                parent.remove(element)
    else:
        _set_paragraph_text(frame.add_paragraph(), text)


def _find_shape(slide: Any, selector: str) -> Any | None:
    """Find a shape by its real ``name`` (or by numeric position). Never guesses."""
    wanted = str(selector or "").strip()
    if not wanted:
        return None
    shapes = list(slide.shapes)
    for shape in shapes:
        if str(getattr(shape, "name", "") or "") == wanted:
            return shape
    if wanted.isdigit():
        idx = int(wanted)
        if 0 <= idx < len(shapes):
            return shapes[idx]
    return None


# --------------------------------------------------------------------------- #
# Tool layer — the ONLY writer                                                #
# --------------------------------------------------------------------------- #
def _apply_replace(presentation: Any, match: str, replace: str) -> int:
    if not match:
        return 0
    count = 0
    for slide in presentation.slides:
        for shape in slide.shapes:
            frame = getattr(shape, "text_frame", None)
            if frame is None or not getattr(shape, "has_text_frame", False):
                continue
            for paragraph in frame.paragraphs:
                text = str(getattr(paragraph, "text", "") or "")
                if match in text:
                    count += text.count(match)
                    _set_paragraph_text(paragraph, text.replace(match, replace))
    return count


def _apply_set_slide_title(presentation: Any, index: int | None, text: str) -> int:
    slides = list(presentation.slides)
    if index is None or index < 0 or index >= len(slides):
        return 0
    try:
        title = slides[index].shapes.title
    except Exception:  # noqa: BLE001 — a placeholder quirk means "no title"
        title = None
    if title is None:
        return 0
    frame = getattr(title, "text_frame", None)
    if frame is None:
        return 0
    if str(getattr(title, "text", "") or "") == text:
        return 0
    _set_frame_text(frame, text)
    return 1


def _apply_set_shape_text(
    presentation: Any, index: int | None, shape: str, text: str
) -> int:
    slides = list(presentation.slides)
    if index is None or index < 0 or index >= len(slides):
        return 0
    target = _find_shape(slides[index], shape)
    if target is None:
        return 0
    frame = getattr(target, "text_frame", None)
    if frame is None or not getattr(target, "has_text_frame", False):
        return 0
    if str(getattr(target, "text", "") or "") == text:
        return 0
    _set_frame_text(frame, text)
    return 1


def apply_edits(data: bytes, edits: list[Any]) -> tuple[bytes, int]:
    """Apply ``edits`` to ``data`` and return ``(new_bytes, n_changes)``.

    This is the layered core: the model proposed the intent, *this* function
    writes the file. ``n_changes`` counts the actual places touched:

      * ``replace_text`` — occurrences of ``match`` really replaced;
      * ``set_slide_title`` — ``1`` when the slide's title text changed;
      * ``set_shape_text`` — ``1`` when the addressed shape's text changed.

    Raises:
        UnknownEditOpError: an edit is not one of :data:`SUPPORTED_OPS`.
        PptxInspectionError: ``data`` is not a readable PPTX (or the extra is
            missing).
    """
    import io as _io

    presentation = open_pptx(data)
    ops = [_coerce_op(item) for item in (edits or [])]
    total = 0
    for op in ops:
        if op.op == "replace_text":
            total += _apply_replace(presentation, op.match, op.replace)
        elif op.op == "set_slide_title":
            total += _apply_set_slide_title(presentation, op.index, op.text)
        elif op.op == "set_shape_text":
            total += _apply_set_shape_text(presentation, op.index, op.shape, op.text)
        # ``__post_init__`` already rejected any other ``op``.

    buffer = _io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue(), total


# --------------------------------------------------------------------------- #
# Diff (pure)                                                                  #
# --------------------------------------------------------------------------- #
def compute_diff(old: bytes, new: bytes) -> DiffReport:
    """Diff two presentations by their text-frame paragraph sequence (pure).

    Reuses the shared :func:`textdiff.diff_counts` engine and the single numeric
    rule (``docx_inspect.numbers_in_text``), so a PPTX diff is defined exactly
    like a DOCX diff. Deterministic and side-effect-free.
    """
    old_paras = shape_paragraph_texts(open_pptx(old))
    new_paras = shape_paragraph_texts(open_pptx(new))
    return diff_counts(old_paras, new_paras, numbers_in_text)


def numbers_removable_by_edits(old_data: bytes, edits: list[Any]) -> set[str]:
    """Numeric tokens an explicit edit is *allowed* to remove.

    Mirrors ``docx_edit.numbers_removable_by_edits``: every number mentioned in an
    edit's own text, plus the numbers in the original text the op rewrites
    wholesale (a ``set_slide_title`` / ``set_shape_text`` target). Pure.
    """
    ops = [_coerce_op(item) for item in (edits or [])]
    allowed: set[str] = set()
    for op in ops:
        allowed.update(numbers_in_text(op.match))
        allowed.update(numbers_in_text(op.replace))
        allowed.update(numbers_in_text(op.text))
        allowed.update(numbers_in_text(op.shape))
    if not ops:
        return allowed
    try:
        presentation = open_pptx(old_data)
    except PptxInspectionError:
        return allowed
    slides = list(presentation.slides)
    for op in ops:
        if op.index is None or op.index < 0 or op.index >= len(slides):
            continue
        slide = slides[op.index]
        if op.op == "set_slide_title":
            try:
                title = slide.shapes.title
            except Exception:  # noqa: BLE001
                title = None
            if title is not None:
                allowed.update(numbers_in_text(str(getattr(title, "text", "") or "")))
        elif op.op == "set_shape_text":
            target = _find_shape(slide, op.shape)
            if target is not None:
                allowed.update(numbers_in_text(str(getattr(target, "text", "") or "")))
    return allowed


# --------------------------------------------------------------------------- #
# LLM layer (intent → ops). Never writes bytes.                                #
# --------------------------------------------------------------------------- #
_EDIT_SYSTEM = (
    "你是企业多智能体平台（ForgeFlow）的 PPTX 演示文稿编辑意图解析器。"
    "你的唯一职责是把自然语言编辑需求解析成结构化的编辑操作数组，"
    "绝不输出演示文稿正文之外的任何内容。"
)

_EDIT_INSTRUCTION = (
    "把下面的自然语言编辑需求解析为一个编辑操作数组。\n"
    "只允许这些 op：\n"
    "1. replace_text：逐段替换子串 —— "
    '{{"op":"replace_text","match":"原文子串","replace":"新子串"}}\n'
    "2. set_slide_title：整张幻灯片标题替换（index 为 0 基幻灯片序号）—— "
    '{{"op":"set_slide_title","index":0,"text":"新的标题"}}\n'
    "3. set_shape_text：替换某一形状文本（index 为 0 基幻灯片序号，shape 为形状名）—— "
    '{{"op":"set_shape_text","index":0,"shape":"标题 1","text":"新的文本"}}\n'
    "只输出一个 JSON 对象，形如：\n"
    '{{"edits": [ ... ]}}\n'
    "演示文稿结构（幻灯片序号及标题）：\n{structure}\n"
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


def _structure_summary(structure: PptxStructure) -> str:
    lines = [
        f"幻灯片数：{structure.slides}；文本框：{structure.text_frames}；"
        f"段落：{structure.paragraphs}；图表：{structure.charts}；备注：{structure.notes}"
    ]
    for index, title in enumerate(structure.titles[:60]):
        lines.append(f"[幻灯片 {index}] {title or '（无标题）'}")
    if not structure.titles:
        lines.append("（演示文稿没有幻灯片）")
    return "\n".join(lines)


async def resolve_intent(
    data: bytes, intent: str, structure: PptxStructure
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
