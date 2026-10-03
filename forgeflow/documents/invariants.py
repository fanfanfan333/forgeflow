"""INC46 T20 — invariant extraction (显式不变量 + 隐式基线).

An :class:`InvariantSet` is everything that must **survive** an edit of a target
range. It is derived from the *instruction* (which kinds to hold) and the
*document* (the concrete values actually inside the range) — **never** from a
fixed checklist:

* **explicit** — every amount / date / table / image / auto-number / reference
  that really occurs inside the located paragraph range;
* **implicit baseline** — three rules that are always in force regardless of
  whether the instruction mentions them:

  1. 目标区间外内容零变化  2. 章节数不变  3. 不新增数字.

Recognition deliberately does **not** touch ``docx_inspect``'s ASCII-only
``_NUMERIC_RE`` / ``numbers_in_text`` / ``document_numbers`` (the edit guard
``numbers_removable_by_edits`` depends on their exact semantics): every money /
date recogniser below is new and local. Coverage is honest — the recognised
reference forms are declared in ``coverage["covered"]`` and the ones this module
does **not** reach are declared in ``coverage["uncovered"]`` (never faked).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from forgeflow.documents.docx_inspect import (
    compute_numbering_labels,
    open_docx,
    paragraph_numbering_ids,
)

__all__ = [
    "AMOUNT_KIND",
    "BASELINE_KIND",
    "DATE_KIND",
    "IMAGE_KIND",
    "Invariant",
    "InvariantSet",
    "NUMBERING_KIND",
    "REFERENCE_KIND",
    "TABLE_KIND",
    "extract_invariants",
]

AMOUNT_KIND = "amount"
DATE_KIND = "date"
TABLE_KIND = "table"
IMAGE_KIND = "image"
NUMBERING_KIND = "numbering"
REFERENCE_KIND = "reference"
BASELINE_KIND = "baseline"

# --------------------------------------------------------------------------- #
# Money / date recognisers (new + local — see module docstring)                #
# --------------------------------------------------------------------------- #
_CN_NUM_CHARS = "零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟萬億"
_CURRENCY_TOKENS: tuple[str, ...] = (
    "¥", "￥", "$", "人民币", "CNY", "RMB", "USD", "美元", "元", "圆",
)

#: Chinese-numeral amount: a numeral run with a 万/亿 magnitude **or** a 元/圆 unit.
#: A bare ``十`` (as in ``十月三日``) never matches — a magnitude/currency is required.
_CN_AMOUNT_RE = re.compile(
    rf"[{_CN_NUM_CHARS}]+(?:万|亿)(?:元|圆)?"
    rf"|[{_CN_NUM_CHARS}]+(?:元|圆)"
)
#: Arabic amount: optional leading currency, then thousands-separated / decimal
#: / unit-suffixed number, then optional trailing currency. A bare integer with
#: neither separator, fraction nor unit (e.g. a year ``2026``) never matches.
_ARABIC_AMOUNT_RE = re.compile(
    r"(?:[¥￥$]|人民币|USD|CNY|RMB)?\s*"
    r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\d+\s*(?:万元|亿元|万|亿|元))"
    r"\s*(?:元|人民币)?"
)

#: The date forms the probe requires: ``2026年10月3日`` / ``2026-10-03`` / ``十月三日``.
_DATE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"),
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}/\d{1,2}/\d{1,2}"),
    re.compile(r"[一二三四五六七八九十]{1,3}\s*月\s*[一二三四五六七八九十]{1,3}\s*日"),
)


def _find_chinese_amounts(text: str | None) -> list[str]:
    """Chinese-numeral amounts in ``text`` (surface forms, whitespace-collapsed)."""
    return [re.sub(r"\s+", "", m) for m in _CN_AMOUNT_RE.findall(str(text or ""))]


def _find_arabic_amounts(text: str | None) -> list[str]:
    """Arabic amounts in ``text`` (surface forms, whitespace-collapsed)."""
    out: list[str] = []
    for match in _ARABIC_AMOUNT_RE.finditer(str(text or "")):
        surface = re.sub(r"\s+", "", match.group(0))
        if surface and any(ch.isdigit() for ch in surface):
            out.append(surface)
    return out


def _find_amounts(text: str | None) -> list[str]:
    """Every amount in ``text`` (Chinese + Arabic), in order."""
    return _find_chinese_amounts(text) + _find_arabic_amounts(text)


def _find_dates(text: str | None) -> list[str]:
    """Every date in ``text`` (the recognised multi-formats), in order."""
    body = str(text or "")
    out: list[str] = []
    for pattern in _DATE_RES:
        out.extend(re.sub(r"\s+", "", m.group(0)) for m in pattern.finditer(body))
    return out


def _currency_of(surface: str) -> str | None:
    """The currency token embedded in an amount surface, if any."""
    for token in _CURRENCY_TOKENS:
        if token in surface:
            return token
    return None


# --------------------------------------------------------------------------- #
# Data types                                                                   #
# --------------------------------------------------------------------------- #
@dataclass
class Invariant:
    """One thing that must survive the edit."""

    kind: str
    value: str
    location: str
    evidence: str
    explicit: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "location": self.location,
            "evidence": self.evidence,
            "explicit": self.explicit,
        }


@dataclass
class InvariantSet:
    """Explicit invariants (from the located range) + the implicit baseline."""

    items: list[Invariant] = field(default_factory=list)
    coverage: dict[str, list[str]] = field(default_factory=dict)

    def explicit_items(self, kind: str | None = None) -> list[Invariant]:
        return [i for i in self.items if i.explicit and (kind is None or i.kind == kind)]

    def baseline_items(self) -> list[Invariant]:
        return [i for i in self.items if i.kind == BASELINE_KIND]

    def values(self, kind: str | None = None) -> list[str]:
        return [i.value for i in self.items if i.explicit and (kind is None or i.kind == kind)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.to_dict() for i in self.items],
            "coverage": {k: list(v) for k, v in self.coverage.items()},
        }


#: Coverage declaration — what this module really recognises today.
_COVERAGE: dict[str, list[str]] = {
    "covered": [
        "amount:chinese-numeral",
        "amount:arabic-thousands",
        "amount:arabic-unit",
        "amount:currency",
        "date:ymd-cjk",
        "date:ymd-iso",
        "date:ymd-slash",
        "date:md-cjk",
        "table",
        "image",
        "numbering",
        "reference:hyperlink",
        "reference:bookmark",
    ],
    "uncovered": [
        "reference:footnote",
        "reference:endnote",
        "reference:cross-ref-field",
    ],
}


def _baseline_items() -> list[Invariant]:
    """The implicit baseline — always in force, independent of the instruction."""
    return [
        Invariant(BASELINE_KIND, "目标区间外内容零变化", "document", "隐式基线：始终生效", False),
        Invariant(BASELINE_KIND, "章节数不变", "document", "隐式基线：始终生效", False),
        Invariant(BASELINE_KIND, "不新增数字", "document", "隐式基线：始终生效", False),
    ]


# --------------------------------------------------------------------------- #
# Range scanners (XML-level; python-docx exposes no high-level helpers)        #
# --------------------------------------------------------------------------- #
def _in_range_tables(document: Any, start: int, end: int) -> list[tuple[int, int]]:
    """``[(table_ordinal, preceding_paragraph_index)]`` for tables inside ``[start, end)``.

    Tables are body-level (their own paragraphs are not in ``document.paragraphs``);
    walking the body element order attributes each table to the paragraph index it
    follows, so a table right after the section heading (``start``) counts as
    in-range while one before the heading does not.
    """
    from docx.oxml.ns import qn

    body = document.element.body
    para_index = -1
    table_ordinal = -1
    out: list[tuple[int, int]] = []
    for child in body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            para_index += 1
        elif tag == qn("w:tbl"):
            table_ordinal += 1
            if start <= para_index < end:
                out.append((table_ordinal, para_index))
    return out


def _paragraph_has(paragraph: Any, tag_local: str) -> bool:
    from docx.oxml.ns import qn

    return paragraph._p.find(f".//{qn(tag_local)}") is not None


def extract_invariants(
    source: bytes,
    *,
    start: int,
    end: int,
) -> InvariantSet:
    """Extract the explicit invariants inside ``[start, end)`` plus the baseline.

    Args:
        source: the DOCX bytes.
        start: the target heading's paragraph index (inclusive).
        end: the target range's exclusive end paragraph index.

    Returns:
        An :class:`InvariantSet`. Every explicit value is *measured* from the
        range — nothing is invented and nothing outside the range is included.
    """
    document = open_docx(source)
    paragraphs = list(document.paragraphs)
    lo = max(0, int(start))
    hi = min(len(paragraphs), int(end)) if end is not None else len(paragraphs)
    labels = compute_numbering_labels(document, paragraphs)

    items: list[Invariant] = []
    for index in range(lo, hi):
        paragraph = paragraphs[index]
        text = paragraph.text
        location = f"paragraph[{index}]"

        for surface in _find_amounts(text):
            currency = _currency_of(surface)
            items.append(
                Invariant(
                    AMOUNT_KIND,
                    surface,
                    location,
                    f"金额识别：{surface}（币种={currency or '未标注'}）",
                )
            )
        for surface in _find_dates(text):
            items.append(Invariant(DATE_KIND, surface, location, f"日期识别：{surface}"))

        if paragraph_numbering_ids(paragraph) is not None and labels[index]:
            items.append(
                Invariant(NUMBERING_KIND, str(labels[index]), location, "自动编号域（numPr）")
            )
        if _paragraph_has(paragraph, "w:drawing") or _paragraph_has(paragraph, "w:pict"):
            items.append(Invariant(IMAGE_KIND, f"image@{index}", location, "内联/浮动图片"))
        if _paragraph_has(paragraph, "w:hyperlink"):
            items.append(Invariant(REFERENCE_KIND, f"hyperlink@{index}", location, "超链接引用"))
        if _paragraph_has(paragraph, "w:bookmarkStart"):
            items.append(Invariant(REFERENCE_KIND, f"bookmark@{index}", location, "书签引用"))

    for table_ordinal, preceding in _in_range_tables(document, lo, hi):
        items.append(
            Invariant(TABLE_KIND, f"table[{table_ordinal}]", f"paragraph[{preceding}]", "目标区间内表格")
        )

    items.extend(_baseline_items())
    return InvariantSet(items=items, coverage={k: list(v) for k, v in _COVERAGE.items()})
