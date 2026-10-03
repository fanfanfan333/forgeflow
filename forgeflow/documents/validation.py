"""INC43 S4 — DOCX validation (design §4.4 "校验；失败重试，耗尽诚实报错").

``verify_docx`` measures a candidate document against a small constraint bag and
reports each check as a tri-state:

  * ``True`` — measured and satisfied;
  * ``False`` — measured and violated;
  * ``None`` — **not measured** (no constraint supplied for it).

``None`` is the load-bearing honesty rule: an unmeasured dimension is never
dressed up as a pass or a failure. The edit handler treats a not-``False`` value
as "no violation reported", so a caller who supplies no numeric / structural
constraint still gets an honest, partial report instead of a fabricated verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from forgeflow.documents.docx_inspect import (
    DocxInspectionError,
    heading_level,
    open_docx,
)

__all__ = [
    "VerifyReport",
    "verify_docx",
    "verify_pptx",
    "verify_textfile",
    "verify_sheet",
    "verify_pdf",
]

_WS_RE = re.compile(r"\s+")
_NUMERIC_RE = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass
class VerifyReport:
    """The outcome of :func:`verify_docx` (tri-state per dimension)."""

    openable: bool = False
    structure_ok: bool | None = None
    data_ok: bool | None = None
    requirement_ok: bool | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "openable": self.openable,
            "structure_ok": self.structure_ok,
            "data_ok": self.data_ok,
            "requirement_ok": self.requirement_ok,
            "notes": self.notes,
        }


def _count_sections(paragraphs: list[Any]) -> int:
    """Number of heading paragraphs — the same rule ``inspect_docx`` groups by."""
    return sum(
        1
        for paragraph in paragraphs
        if heading_level(paragraph) is not None and paragraph.text.strip()
    )


def verify_docx(data: bytes, constraints: dict[str, Any] | None = None) -> VerifyReport:
    """Verify a candidate DOCX against ``constraints``.

    Recognised constraint keys (all optional):

      * ``expected_sections`` / ``expected_tables`` — structural preservation
        (drive ``structure_ok``);
      * ``original_numbers`` (+ optional ``allowed_missing_numbers``) — the
        original numeric tokens that must survive unless an edit legitimately
        liberated them (drives ``data_ok``);
      * ``max_chars`` / ``min_chars`` — a "≤N 字" style requirement (drives
        ``requirement_ok``).

    A byte string that cannot be opened yields ``openable=False`` with every
    other dimension ``None`` (nothing else could be measured — never ``False``
    masquerading as "checked and bad").
    """
    options = dict(constraints or {})
    try:
        document = open_docx(data)
    except DocxInspectionError as exc:
        return VerifyReport(openable=False, notes=f"文档无法打开：{exc}")

    paragraphs = list(document.paragraphs)
    notes: list[str] = []

    structure_ok: bool | None = None
    if "expected_sections" in options or "expected_tables" in options:
        ok = True
        if "expected_sections" in options:
            want = int(options["expected_sections"])
            got = _count_sections(paragraphs)
            if want != got:
                ok = False
                notes.append(f"章节数应为 {want}，实为 {got}")
        if "expected_tables" in options:
            want = int(options["expected_tables"])
            got = len(document.tables)
            if want != got:
                ok = False
                notes.append(f"表格数应为 {want}，实为 {got}")
        structure_ok = ok

    data_ok: bool | None = None
    if "original_numbers" in options:
        original = [str(n) for n in (options.get("original_numbers") or [])]
        allowed = {str(n) for n in (options.get("allowed_missing_numbers") or [])}
        now = [n for p in paragraphs for n in _NUMERIC_RE.findall(p.text)]
        counts_now: dict[str, int] = {}
        for token in now:
            counts_now[token] = counts_now.get(token, 0) + 1
        missing: list[str] = []
        for token in original:
            if token in allowed:
                continue
            if counts_now.get(token, 0) > 0:
                counts_now[token] -= 1
            else:
                missing.append(token)
        data_ok = not missing
        if missing:
            notes.append(f"数字被静默修改（原数字在新文档缺失且编辑未声明）：{sorted(set(missing))}")

    requirement_ok: bool | None = None
    if "max_chars" in options or "min_chars" in options:
        char_count = len(_WS_RE.sub("", "\n".join(p.text for p in paragraphs)))
        ok = True
        if "max_chars" in options:
            limit = int(options["max_chars"])
            if char_count > limit:
                ok = False
                notes.append(f"字数 {char_count} 超过上限 {limit}")
        if "min_chars" in options:
            limit = int(options["min_chars"])
            if char_count < limit:
                ok = False
                notes.append(f"字数 {char_count} 低于下限 {limit}")
        requirement_ok = ok

    return VerifyReport(
        openable=True,
        structure_ok=structure_ok,
        data_ok=data_ok,
        requirement_ok=requirement_ok,
        notes="；".join(notes),
    )


# --------------------------------------------------------------------------- #
# INC44 §2.2 — PPTX + textfile verification (same tri-state contract)          #
# --------------------------------------------------------------------------- #
def _pptx_numbers(texts: list[str]) -> list[str]:
    out: list[str] = []
    for text in texts:
        out.extend(_NUMERIC_RE.findall(text))
    return out


def _pptx_texts(document: Any) -> list[str]:
    """Every non-empty text-frame paragraph text across the presentation."""
    from forgeflow.documents.pptx_inspect import shape_paragraph_texts

    return shape_paragraph_texts(document)


def verify_pptx(data: bytes, constraints: dict[str, Any] | None = None) -> VerifyReport:
    """Verify a candidate PPTX against ``constraints`` (tri-state, like DOCX).

    Recognised constraint keys (all optional):

      * ``expected_slides`` — structural preservation (drives ``structure_ok``);
      * ``original_numbers`` (+ optional ``allowed_missing_numbers``) — the
        original numeric tokens that must survive unless an edit legitimately
        liberated them (drives ``data_ok``);
      * ``max_chars`` / ``min_chars`` — a "≤N 字" style requirement.

    A byte string that cannot be opened yields ``openable=False`` with every
    other dimension ``None`` (nothing else could be measured — never ``False``
    masquerading as "checked and bad").
    """
    from forgeflow.documents.pptx_inspect import PptxInspectionError, open_pptx

    options = dict(constraints or {})
    try:
        presentation = open_pptx(data)
    except PptxInspectionError as exc:
        return VerifyReport(openable=False, notes=f"演示文稿无法打开：{exc}")

    texts = _pptx_texts(presentation)
    notes: list[str] = []

    structure_ok: bool | None = None
    if "expected_slides" in options:
        want = int(options["expected_slides"])
        got = len(list(presentation.slides))
        structure_ok = want == got
        if not structure_ok:
            notes.append(f"幻灯片数应为 {want}，实为 {got}")

    data_ok: bool | None = None
    if "original_numbers" in options:
        original = [str(n) for n in (options.get("original_numbers") or [])]
        allowed = {str(n) for n in (options.get("allowed_missing_numbers") or [])}
        now = _pptx_numbers(texts)
        counts_now: dict[str, int] = {}
        for token in now:
            counts_now[token] = counts_now.get(token, 0) + 1
        missing: list[str] = []
        for token in original:
            if token in allowed:
                continue
            if counts_now.get(token, 0) > 0:
                counts_now[token] -= 1
            else:
                missing.append(token)
        data_ok = not missing
        if missing:
            notes.append(
                f"数字被静默修改（原数字在新演示文稿缺失且编辑未声明）：{sorted(set(missing))}"
            )

    requirement_ok: bool | None = None
    if "max_chars" in options or "min_chars" in options:
        char_count = len(_WS_RE.sub("", "\n".join(texts)))
        ok = True
        if "max_chars" in options:
            limit = int(options["max_chars"])
            if char_count > limit:
                ok = False
                notes.append(f"字数 {char_count} 超过上限 {limit}")
        if "min_chars" in options:
            limit = int(options["min_chars"])
            if char_count < limit:
                ok = False
                notes.append(f"字数 {char_count} 低于下限 {limit}")
        requirement_ok = ok

    return VerifyReport(
        openable=True,
        structure_ok=structure_ok,
        data_ok=data_ok,
        requirement_ok=requirement_ok,
        notes="；".join(notes),
    )


def verify_textfile(
    data: bytes, constraints: dict[str, Any] | None = None
) -> VerifyReport:
    """Verify a candidate text / code file against ``constraints`` (tri-state).

    Recognised constraint keys (all optional):

      * ``expect_compilable_python`` — when truthy, run ``ast.parse`` and record
        the outcome on ``structure_ok`` (``True`` / ``False``); when the flag is
        absent the dimension stays ``None`` ("not measured");
      * ``must_contain`` — every listed substring must be present (drives
        ``data_ok``);
      * ``max_chars`` / ``min_chars`` — a "≤N 字" style requirement (drives
        ``requirement_ok``).

    A byte string that cannot be decoded *at all* yields ``openable=False``; the
    decoder is total (Latin-1 last resort), so in practice only empty / non-bytes
    input reaches that branch.
    """
    options = dict(constraints or {})
    try:
        from forgeflow.documents.textfile_inspect import inspect_textfile

        structure = inspect_textfile(data)
        text, _encoding = _decode(data)
    except Exception as exc:  # noqa: BLE001 — an unreadable file is honestly unopenable
        return VerifyReport(openable=False, notes=f"文本文件无法打开：{exc}")

    notes: list[str] = []

    structure_ok: bool | None = None
    if options.get("expect_compilable_python"):
        import ast

        try:
            ast.parse(text)
            structure_ok = True
        except SyntaxError as exc:
            structure_ok = False
            notes.append(f"Python 语法错误：{exc.msg}")

    data_ok: bool | None = None
    must_contain = options.get("must_contain")
    if isinstance(must_contain, (list, tuple)) and must_contain:
        missing = [str(token) for token in must_contain if str(token) not in text]
        data_ok = not missing
        if missing:
            notes.append(f"缺少必需内容：{missing}")

    requirement_ok: bool | None = None
    if "max_chars" in options or "min_chars" in options:
        ok = True
        if "max_chars" in options:
            limit = int(options["max_chars"])
            if structure.chars > limit:
                ok = False
                notes.append(f"字数 {structure.chars} 超过上限 {limit}")
        if "min_chars" in options:
            limit = int(options["min_chars"])
            if structure.chars < limit:
                ok = False
                notes.append(f"字数 {structure.chars} 低于下限 {limit}")
        requirement_ok = ok

    return VerifyReport(
        openable=True,
        structure_ok=structure_ok,
        data_ok=data_ok,
        requirement_ok=requirement_ok,
        notes="；".join(notes),
    )


def _decode(data: bytes) -> tuple[str, str]:
    """Re-use the resource seam's single honest decoder (local import)."""
    from forgeflow.resources.summaries import _decode as _resource_decode

    return _resource_decode(data)


# --------------------------------------------------------------------------- #
# INC45 §3.2 — XLSX + PDF verification (same tri-state contract)              #
# --------------------------------------------------------------------------- #
def _sheet_char_count(workbook: Any) -> int:
    """Total character count across every value-carrying cell of a workbook."""
    total = 0
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows(values_only=True):
            for value in row:
                if value is None or (isinstance(value, str) and value == ""):
                    continue
                total += len(str(value))
    return total


def verify_sheet(data: bytes, constraints: dict[str, Any] | None = None) -> VerifyReport:
    """Verify a candidate XLSX against ``constraints`` (tri-state, like DOCX).

    Recognised constraint keys (all optional):

      * ``expected_sheets`` — worksheet-count preservation (drives ``structure_ok``);
      * ``original_numbers`` (+ optional ``allowed_missing_numbers``) — the
        original numeric tokens that must survive unless an edit legitimately
        liberated them (drives ``data_ok``);
      * ``max_chars`` / ``min_chars`` — a "≤N 字" style requirement (drives
        ``requirement_ok``).

    A byte string that cannot be opened yields ``openable=False`` with every
    other dimension ``None`` (nothing else could be measured — never ``False``
    masquerading as "checked and bad").
    """
    from forgeflow.documents.sheet_inspect import (
        SheetInspectionError,
        open_workbook,
        sheet_numbers,
    )

    options = dict(constraints or {})
    try:
        workbook = open_workbook(data)
    except SheetInspectionError as exc:
        return VerifyReport(openable=False, notes=f"工作簿无法打开：{exc}")

    notes: list[str] = []

    structure_ok: bool | None = None
    if "expected_sheets" in options:
        want = int(options["expected_sheets"])
        got = len(list(workbook.sheetnames))
        structure_ok = want == got
        if not structure_ok:
            notes.append(f"工作表数应为 {want}，实为 {got}")

    data_ok: bool | None = None
    if "original_numbers" in options:
        original = [str(n) for n in (options.get("original_numbers") or [])]
        allowed = {str(n) for n in (options.get("allowed_missing_numbers") or [])}
        now = sheet_numbers(data)
        counts_now: dict[str, int] = {}
        for token in now:
            counts_now[token] = counts_now.get(token, 0) + 1
        missing: list[str] = []
        for token in original:
            if token in allowed:
                continue
            if counts_now.get(token, 0) > 0:
                counts_now[token] -= 1
            else:
                missing.append(token)
        data_ok = not missing
        if missing:
            notes.append(f"数字被静默修改（原数字在新工作簿缺失且编辑未声明）：{sorted(set(missing))}")

    requirement_ok: bool | None = None
    if "max_chars" in options or "min_chars" in options:
        char_count = _sheet_char_count(workbook)
        ok = True
        if "max_chars" in options:
            limit = int(options["max_chars"])
            if char_count > limit:
                ok = False
                notes.append(f"字数 {char_count} 超过上限 {limit}")
        if "min_chars" in options:
            limit = int(options["min_chars"])
            if char_count < limit:
                ok = False
                notes.append(f"字数 {char_count} 低于下限 {limit}")
        requirement_ok = ok

    return VerifyReport(
        openable=True,
        structure_ok=structure_ok,
        data_ok=data_ok,
        requirement_ok=requirement_ok,
        notes="；".join(notes),
    )


def verify_pdf(data: bytes, constraints: dict[str, Any] | None = None) -> VerifyReport:
    """Verify a candidate PDF against ``constraints`` (tri-state, like DOCX).

    Recognised constraint keys (all optional):

      * ``expected_pages`` — page-count preservation (drives ``structure_ok``);
      * ``must_contain`` — every listed substring must appear in the extracted
        text (drives ``data_ok``);
      * ``max_chars`` / ``min_chars`` — a "≤N 字" style requirement (drives
        ``requirement_ok``).

    A byte string that cannot be opened yields ``openable=False`` with every
    other dimension ``None`` (nothing else could be measured — never ``False``
    masquerading as "checked and bad").
    """
    from forgeflow.documents.pdf_inspect import PdfInspectionError, inspect_pdf

    options = dict(constraints or {})
    try:
        facts = inspect_pdf(data)
    except PdfInspectionError as exc:
        return VerifyReport(openable=False, notes=f"PDF 无法打开：{exc}")

    notes: list[str] = []

    structure_ok: bool | None = None
    if "expected_pages" in options:
        want = int(options["expected_pages"])
        got = facts.page_count
        if got is None:
            structure_ok = None
        else:
            structure_ok = want == got
            if not structure_ok:
                notes.append(f"页数应为 {want}，实为 {got}")

    data_ok: bool | None = None
    must_contain = options.get("must_contain")
    if isinstance(must_contain, (list, tuple)) and must_contain:
        from forgeflow.multimodal.pdf import extract_pdf_text

        try:
            text = extract_pdf_text(bytes(data)).text
        except Exception as exc:  # noqa: BLE001 — cannot read ⇒ cannot assert
            return VerifyReport(openable=False, notes=f"PDF 无法打开：{exc}")
        missing = [str(token) for token in must_contain if str(token) not in text]
        data_ok = not missing
        if missing:
            notes.append(f"缺少必需内容：{missing}")

    requirement_ok: bool | None = None
    if "max_chars" in options or "min_chars" in options:
        char_count = facts.chars if facts.chars is not None else 0
        ok = True
        if "max_chars" in options:
            limit = int(options["max_chars"])
            if char_count > limit:
                ok = False
                notes.append(f"字数 {char_count} 超过上限 {limit}")
        if "min_chars" in options:
            limit = int(options["min_chars"])
            if char_count < limit:
                ok = False
                notes.append(f"字数 {char_count} 低于下限 {limit}")
        requirement_ok = ok

    return VerifyReport(
        openable=True,
        structure_ok=structure_ok,
        data_ok=data_ok,
        requirement_ok=requirement_ok,
        notes="；".join(notes),
    )
