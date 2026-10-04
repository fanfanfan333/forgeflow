"""INC46 T27 — the XLSX *guarded* edit plane: formula cells are protected.

This is a thin, additive policy layer over :mod:`forgeflow.documents.sheet_edit`
(INC45). The **byte writer stays** ``sheet_edit.apply_edits`` (single writer — no
duplicated truth); this module adds exactly one hard rule and re-exports the
inherited op grammar / honest degradation:

Formula protection
------------------
``set_cell`` on a cell that **currently holds a formula** (a string starting with
``=``) is refused by default, because silently replacing a formula with a constant
destroys a computed relationship the user cannot see in a cached value. To change
a formula cell — whether to a new formula or (rarely) to a constant — the caller
must **explicitly express a formula intent**:

  * per-op: ``{"op": "set_cell", "cell": "C1", "value": "=A1+B1", "intent": "formula"}``;
  * or globally: ``apply_edits(data, edits, allow_formula_overwrite=True)``.

Everything else is inherited verbatim from ``sheet_edit``:

  * the same ``set_cell`` / ``replace_text`` op grammar and ``SheetEditOp``
    validation (``UnknownEditOpError``);
  * the same recomputable :func:`~forgeflow.documents.sheet_edit.compute_diff`;
  * untouched cells keep their formulas, number / date formats, **data
    validation**, **conditional formatting** and **chart references** (openpyxl
    round-trips the loaded workbook in place; only the addressed cells mutate).

Honesty (red line 17 / 14): a refusal raises :class:`FormulaProtectionError` with
the real cell + formula; it never silently succeeds and never reports a fake "已修改
N 处".
"""

from __future__ import annotations

from typing import Any

from forgeflow.documents.docx_edit import UnknownEditOpError
from forgeflow.documents.sheet_edit import (
    SUPPORTED_OPS,
    SheetEditOp,
    _coerce_op,
    _select_sheet,
    apply_edits as _apply_sheet_edits,
)
from forgeflow.documents.sheet_inspect import (
    SheetInspectionError,
    open_workbook,
)

__all__ = [
    "SUPPORTED_OPS",
    "PROTECTED_FEATURES",
    "PRESERVED_FEATURES",
    "FORMULA_INTENT_TOKENS",
    "FormulaProtectionError",
    "SheetEditOp",
    "UnknownEditOpError",
    "SheetInspectionError",
    "is_formula",
    "formula_cells",
    "apply_edits",
]

#: The capability this plane protects by default.
PROTECTED_FEATURES: tuple[str, ...] = ("formula",)

#: Document features preserved verbatim on an edit (declared in the matrix).
PRESERVED_FEATURES: tuple[str, ...] = (
    "cell_format",
    "number_format",
    "data_validation",
    "conditional_formatting",
    "chart_reference",
)

#: Per-op ``intent`` tokens that count as an *explicit* formula intent.
FORMULA_INTENT_TOKENS: frozenset[str] = frozenset(
    {"formula", "set_formula", "replace_formula", "overwrite_formula", "explicit"}
)


class FormulaProtectionError(ValueError):
    """Raised when a constant would silently overwrite a formula cell.

    This is a *policy* refusal (distinct from :class:`UnknownEditOpError`, which is
    a grammar error): the op is well-formed, but overwriting the formula requires
    an explicit formula intent.
    """


def is_formula(value: Any) -> bool:
    """Whether a cell value is a formula (openpyxl stores it as ``=...``)."""
    return isinstance(value, str) and value.startswith("=")


def formula_cells(data: bytes) -> list[tuple[str, str, str]]:
    """Every formula cell as ``(sheet_title, coordinate, formula)`` tuples.

    Deterministic and read-only; used by the guard and by tests to prove a
    workbook's formulas survive an edit.
    """
    workbook = open_workbook(data)
    out: list[tuple[str, str, str]] = []
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if is_formula(cell.value):
                    out.append((str(worksheet.title), str(cell.coordinate), str(cell.value)))
    return out


def _explicit_formula_intent(item: Any) -> bool:
    """Whether a raw op item explicitly declares a formula intent."""
    if isinstance(item, dict):
        token = str(item.get("intent") or "").strip().lower()
    else:
        token = str(getattr(item, "intent", "") or "").strip().lower()
    return token in FORMULA_INTENT_TOKENS


def _guard_formula_overwrite(
    data: bytes, raw_edits: list[Any], ops: list[SheetEditOp], *, allow: bool
) -> None:
    """Refuse (honestly) any ``set_cell`` that would clobber a formula cell.

    Raises :class:`FormulaProtectionError` naming the real sheet / cell / formula.
    A missing sheet or coordinate is left for ``sheet_edit`` to report in its own
    words (we do not invent a second error path for the same condition).
    """
    if allow:
        return
    workbook = open_workbook(data)
    for item, op in zip(raw_edits, ops):
        if op.op != "set_cell":
            continue
        if _explicit_formula_intent(item):
            continue
        try:
            worksheet = _select_sheet(workbook, op.sheet)
            current = worksheet[op.cell].value
        except (SheetInspectionError, KeyError, ValueError):
            continue  # let the real writer raise the authoritative error
        if is_formula(current):
            where = f"{op.sheet or worksheet.title}!{op.cell}"
            raise FormulaProtectionError(
                f"拒绝用常量覆盖公式单元格 {where}（原公式 {current!r}）："
                "公式单元格默认受保护；如确需修改公式，请显式表达意图"
                "（op 加 intent=\"formula\"，或 allow_formula_overwrite=True）"
            )


def apply_edits(
    data: bytes, edits: list[Any], *, allow_formula_overwrite: bool = False
) -> tuple[bytes, int]:
    """Apply ``edits`` with formula protection, then delegate the write.

    Args:
        data: the original ``.xlsx`` / ``.xlsm`` bytes.
        edits: the op list (mappings or :class:`SheetEditOp`).
        allow_formula_overwrite: when ``True``, a per-op formula intent is not
            required — the caller has explicitly accepted clobbering formulas.

    Returns:
        ``(new_bytes, n_changes)`` — identical semantics to
        ``sheet_edit.apply_edits`` (untouched cells keep formulas / formats /
        validation / conditional formatting / chart references).

    Raises:
        FormulaProtectionError: a ``set_cell`` would overwrite a formula without
            an explicit formula intent.
        UnknownEditOpError: an op is not one of :data:`SUPPORTED_OPS`.
        SheetInspectionError: ``data`` is not a readable workbook (or the
            ``openpyxl`` extra is absent), or an op targets a missing sheet.
    """
    raw_edits = list(edits or [])
    # Coerce first so op-legality errors (UnknownEditOpError) surface with the
    # same message as the shared plane; the guard then only inspects legal ops.
    ops = [_coerce_op(item) for item in raw_edits]
    _guard_formula_overwrite(
        data, raw_edits, ops, allow=bool(allow_formula_overwrite)
    )
    # The ONE writer stays ``sheet_edit`` — no duplicated byte-writing logic.
    return _apply_sheet_edits(data, ops)
