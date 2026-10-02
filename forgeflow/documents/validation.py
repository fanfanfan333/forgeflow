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

__all__ = ["VerifyReport", "verify_docx"]

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
