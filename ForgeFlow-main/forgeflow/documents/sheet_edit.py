"""INC45 §1.1 — the XLSX edit plane: intent → ops → bytes (分层硬约束).

Same three-layer split as every other edit plane (design §8), so the model can
**never** write a workbook:

  * :func:`resolve_intent` — the **LLM layer**. Natural language → a list of
    :class:`SheetEditOp`, and it *never* touches the workbook bytes. Untrusted
    output: an unknown ``op`` (or an unparseable reply, or a missing model)
    yields ``None`` rather than a guess.
  * :func:`apply_edits` — the **Tool layer**. The only writer. It edits cells via
    ``openpyxl`` and re-serialises the workbook; untouched cells keep their
    formulas and styles (design §1.1 P2-2).
  * :func:`compute_diff` — a **pure function** over two byte strings, computed by
    the shared :mod:`forgeflow.documents.textdiff` engine over the workbook's
    cell sequence (``Sheet!A1=value``), so every figure is recomputable.

Supported ops (exactly these; anything else raises :class:`UnknownEditOpError`)::

    set_cell      {"op": "set_cell", "sheet": "Sheet1", "cell": "B2", "value": "新值"}
    replace_text  {"op": "replace_text", "sheet": "Sheet1", "match": "旧", "replace": "新"}

Field note: the ``__init__`` class-diagram (design §3.2) lists the five fields
``op/sheet/cell/match/replace``. A ``set_cell`` value is free-form (str / int /
float / bool / formula) so a dedicated ``value`` field carries it faithfully;
:meth:`SheetEditOp.from_dict` also accepts ``replace`` as an alias for ``value``
on ``set_cell`` so both spellings parse. This extra field is **additive** — the
documented field set is a subset.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from forgeflow.documents.docx_edit import UnknownEditOpError
from forgeflow.documents.docx_inspect import numbers_in_text
from forgeflow.documents.sheet_inspect import (
    SheetInspectionError,
    SheetStructure,
    open_workbook,
)
from forgeflow.documents.textdiff import DiffReport, diff_counts

__all__ = [
    "SUPPORTED_OPS",
    "SheetEditOp",
    "UnknownEditOpError",
    "apply_edits",
    "compute_diff",
    "numbers_removable_by_edits",
    "resolve_intent",
]

#: The op kinds the platform supports. Anything else is refused explicitly.
SUPPORTED_OPS: tuple[str, ...] = ("set_cell", "replace_text")


# ``UnknownEditOpError`` is the **one** shared error class for every edit plane
# (docx / pptx / textfile / sheet): re-using ``docx_edit``'s definition keeps
# ``documents.UnknownEditOpError`` a single catch target across formats.


def _coerce_value(raw: Any) -> Any:
    """A cell value kept in kind: numbers stay numbers, everything else is text.

    ``openpyxl`` writes an ``int`` / ``float`` as a *numeric* cell (so formulas
    and number formatting keep working) and an ``str`` as text; a leading ``=``
    is written as a **formula**. We never stringify blindly — a JSON ``42``
    must land as the number 42.
    """
    if isinstance(raw, bool):  # bool must be checked before int
        return raw
    if isinstance(raw, (int, float)):
        return raw
    if raw is None:
        return None
    return str(raw)


# --------------------------------------------------------------------------- #
# SheetEditOp                                                                  #
# --------------------------------------------------------------------------- #
@dataclass
class SheetEditOp:
    """One spreadsheet edit intention (the LLM layer's only output type)."""

    op: str = ""
    sheet: str = ""
    cell: str = ""
    match: str = ""
    replace: str = ""
    #: ``set_cell`` target value — kept in kind (None clears the cell).
    value: Any = None

    def __post_init__(self) -> None:
        if self.op not in SUPPORTED_OPS:
            raise UnknownEditOpError(
                f"不支持的编辑操作：{self.op!r}（仅支持 {', '.join(SUPPORTED_OPS)}）"
            )
        if self.op == "set_cell" and not str(self.cell).strip():
            raise UnknownEditOpError("set_cell 需要非空的 cell（如 \"B2\"）")
        if self.op == "replace_text" and not self.match:
            raise UnknownEditOpError("replace_text 需要非空的 match")

    @classmethod
    def from_dict(cls, data: Any) -> "SheetEditOp":
        """Build an op from a mapping; raise :class:`UnknownEditOpError` on junk."""
        if isinstance(data, SheetEditOp):
            return data
        if not isinstance(data, dict):
            raise UnknownEditOpError(f"编辑操作必须是对象：{data!r}")
        # ``value`` is the canonical key; ``replace`` is accepted as an alias on
        # ``set_cell`` so both the design's field list and the natural spelling
        # parse without ambiguity.
        if "value" in data:
            value = _coerce_value(data.get("value"))
        elif str(data.get("op") or "").strip() == "set_cell":
            value = _coerce_value(data.get("replace"))
        else:
            value = None
        return cls(
            op=str(data.get("op") or "").strip(),
            sheet=str(data.get("sheet") or ""),
            cell=str(data.get("cell") or ""),
            match=str(data.get("match") or ""),
            replace=str(data.get("replace") or ""),
            value=value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "sheet": self.sheet,
            "cell": self.cell,
            "match": self.match,
            "replace": self.replace,
            "value": self.value,
        }


def _coerce_op(item: Any) -> SheetEditOp:
    """Accept a :class:`SheetEditOp` or a raw mapping; refuse anything else."""
    if isinstance(item, SheetEditOp):
        return item
    if isinstance(item, dict):
        return SheetEditOp.from_dict(item)
    raise UnknownEditOpError(f"编辑操作必须是对象或 SheetEditOp：{item!r}")


# --------------------------------------------------------------------------- #
# Structural helpers                                                           #
# --------------------------------------------------------------------------- #
def _select_sheet(workbook: Any, name: str) -> Any:
    """Resolve the worksheet a targeted op applies to (never guess a target)."""
    if not str(name).strip():
        return workbook.active
    if name not in workbook.sheetnames:
        raise SheetInspectionError(f"工作表不存在：{name}")
    return workbook[name]


def _set_cell(worksheet: Any, cell: str, value: Any) -> int:
    """Write ``value`` into ``cell``; return 1 when the cell actually changed."""
    try:
        target = worksheet[cell]
    except (KeyError, ValueError) as exc:
        raise SheetInspectionError(f"非法单元格坐标：{cell}") from exc
    if target.value == value:
        return 0
    target.value = value
    return 1


def _replace_in_sheet(worksheet: Any, match: str, replace: str) -> int:
    """Replace ``match`` inside every string cell of a worksheet; return count."""
    count = 0
    for row in worksheet.iter_rows():
        for cell in row:
            text = cell.value
            if isinstance(text, str) and match in text:
                count += text.count(match)
                cell.value = text.replace(match, replace)
    return count


# --------------------------------------------------------------------------- #
# Tool layer — the ONLY writer                                                 #
# --------------------------------------------------------------------------- #
def apply_edits(data: bytes, edits: list[Any]) -> tuple[bytes, int]:
    """Apply ``edits`` to ``data`` and return ``(new_bytes, n_changes)``.

    This is the layered core: the model proposed the intent, *this* function
    writes the workbook. ``n_changes`` counts the real places touched:

      * ``set_cell`` — ``1`` when the target cell's value actually changed;
      * ``replace_text`` — occurrences of ``match`` really replaced.

    Formulas and styles on untouched cells are preserved (``openpyxl`` keeps the
    loaded workbook in place; only the addressed cells are mutated).

    Raises:
        UnknownEditOpError: an edit is not one of :data:`SUPPORTED_OPS`.
        SheetInspectionError: ``data`` is not a readable workbook (or the
            ``openpyxl`` extra is absent), or an op targets a missing sheet.
    """
    workbook = open_workbook(data)
    ops = [_coerce_op(item) for item in (edits or [])]
    total = 0
    for op in ops:
        if op.op == "set_cell":
            worksheet = _select_sheet(workbook, op.sheet)
            total += _set_cell(worksheet, op.cell, op.value)
        elif op.op == "replace_text":
            if str(op.sheet).strip():
                worksheets = [_select_sheet(workbook, op.sheet)]
            else:
                worksheets = list(workbook.worksheets)
            for worksheet in worksheets:
                total += _replace_in_sheet(worksheet, op.match, op.replace)
        # ``__post_init__`` already rejected any other ``op``.

    buffer = _BytesIO()
    workbook.save(buffer)
    return buffer.getvalue(), total


def _BytesIO() -> Any:
    import io

    return io.BytesIO()


# --------------------------------------------------------------------------- #
# Diff (pure)                                                                  #
# --------------------------------------------------------------------------- #
def _cell_lines(data: bytes) -> list[str]:
    """Serialise every value-carrying cell to a stable, position-ordered line.

    The serialisation (``Sheet1!B2=新值``) is deterministic and content-addressed
    by (sheet, cell coordinate), so a change at a coordinate aligns to itself and
    reports as a *modification*, not an add + remove pair.
    """
    workbook = open_workbook(data)
    lines: list[str] = []
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                lines.append(f"{worksheet.title}!{cell.coordinate}={cell.value}")
    return lines


def compute_diff(old: bytes, new: bytes) -> DiffReport:
    """Diff two workbooks by their cell-value sequence (pure function).

    * ``modified`` — a coordinate whose cell text changed;
    * ``added`` / ``removed`` — a coordinate present in only one workbook;
    * ``numeric_changes`` — of the ``modified`` cells, how many have a different
      **numeric token set** (the 「数字变化 Z」 figure).

    Reuses the shared :func:`textdiff.diff_counts` engine and the single numeric
    rule, so the figures are recomputable and identical in kind to the other
    document planes.
    """
    return diff_counts(_cell_lines(old), _cell_lines(new), numbers_in_text)


def numbers_removable_by_edits(data: bytes, edits: list[Any]) -> set[str]:
    """Numeric tokens an explicit edit is *allowed* to remove.

    The data-consistency guard is "the original numbers must survive **except**
    where an edit deliberately touched them". For a workbook this exception is:
    every number mentioned in an edit's own text, plus — for a ``set_cell`` — the
    numbers the overwritten cell originally held. Pure and deterministic.
    """
    ops = [_coerce_op(item) for item in (edits or [])]
    allowed: set[str] = set()
    for op in ops:
        allowed.update(numbers_in_text(op.match))
        allowed.update(numbers_in_text(op.replace))
        allowed.update(numbers_in_text(op.sheet))
        allowed.update(numbers_in_text(op.cell))
        if op.value is not None:
            allowed.update(numbers_in_text(str(op.value)))
    if not ops:
        return allowed
    try:
        workbook = open_workbook(data)
    except SheetInspectionError:
        return allowed
    for op in ops:
        if op.op != "set_cell":
            continue
        try:
            worksheet = _select_sheet(workbook, op.sheet)
            original = worksheet[op.cell].value
        except (SheetInspectionError, KeyError, ValueError):
            continue
        if original is not None:
            allowed.update(numbers_in_text(str(original)))
    return allowed


# --------------------------------------------------------------------------- #
# LLM layer (intent → ops). Never writes bytes.                                #
# --------------------------------------------------------------------------- #
_EDIT_SYSTEM = (
    "你是企业多智能体平台（ForgeFlow）的 XLSX 工作簿编辑意图解析器。"
    "你的唯一职责是把自然语言编辑需求解析成结构化的编辑操作数组，"
    "绝不输出工作簿内容之外的任何内容。"
)

_EDIT_INSTRUCTION = (
    "把下面的自然语言编辑需求解析为一个编辑操作数组。\n"
    "只允许这些 op：\n"
    "1. set_cell：向指定单元格写入新值（sheet 可选，缺省用活动表）—— "
    '{{"op":"set_cell","sheet":"Sheet1","cell":"B2","value":"新值"}}\n'
    "2. replace_text：在工作表内替换子串（sheet 可选，缺省用全表）—— "
    '{{"op":"replace_text","sheet":"Sheet1","match":"原子串","replace":"新子串"}}\n'
    "只输出一个 JSON 对象，形如：\n"
    '{{"edits": [ ... ]}}\n'
    "工作簿结构（工作表名/行列数/表头）：\n{structure}\n"
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


def _structure_summary(structure: SheetStructure) -> str:
    lines = [
        f"工作表：{structure.sheets}；活动表：{structure.active}；"
        f"行数：{structure.rows}；列数：{structure.columns}；"
        f"单元格数：{structure.cells}"
    ]
    if structure.header:
        lines.append(f"表头：{structure.header[:40]}")
    return "\n".join(lines)


async def resolve_intent(
    data: bytes, intent: str, structure: SheetStructure
) -> list[SheetEditOp] | None:
    """Parse a natural-language intent into edit ops via the configured model.

    **This function never writes bytes** — it is the LLM layer. It returns
    ``None`` (an honest "no usable intent") when the model is unavailable, the
    reply cannot be parsed, or an op is not in :data:`SUPPORTED_OPS`.
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
    ops: list[SheetEditOp] = []
    for item in raw_edits:
        try:
            ops.append(_coerce_op(item))
        except UnknownEditOpError:
            return None
    return ops or None
