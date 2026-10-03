"""INC43 S4 (BE-1) — real DOCX structural inspection via ``python-docx``.

Layer contract (design §1.1 / §1.3): this module **only reads** bytes. It never
writes a file and never calls a model — it is the honest "inspect" layer that
reports the document's *measured* structure (paragraphs / headings / words /
tables / images / section grouping). Nothing is guessed: a byte string that
``python-docx`` cannot open raises :class:`DocxInspectionError`, which the
handler converts into an honest failure.

Why a dedicated types module
----------------------------
``DocStructure`` is shared by ``docx_edit`` (to address sections) and by the
runtime handler (to build the small inspect payload). Keeping the heading-level
rule and the numeric-token rule here means there is exactly **one** definition
of "what is a heading" and "what is a number" across the whole document domain.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DocStructure",
    "DocxInspectionError",
    "heading_level",
    "inspect_docx",
    "numbers_in_text",
    "document_numbers",
    "open_docx",
    # INC46 T20 — additive structure helpers (numPr labels / division markers)
    "chinese_number_to_int",
    "text_marker",
    "paragraph_numbering_ids",
    "compute_numbering_labels",
]

#: ``Heading 1`` / ``标题 1`` … → an outline level 1..9.
_HEADING_RE = re.compile(r"^(?:heading|标题|標題|見出し)\s*([1-9])$", re.IGNORECASE)
#: ``Title`` / ``标题`` → the outline level 0 (a document title).
_TITLE_RE = re.compile(r"^(?:title|标题|標題|タイトル)$", re.IGNORECASE)

#: CJK ideographs (BMP + Ext-A + compatibility + Ext-B) counted per character.
_CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ffff]"
)

#: A numeric token: an integer or a decimal / thousands-separated number.
_NUMERIC_RE = re.compile(r"\d+(?:[.,]\d+)*")

_TITLE_LEVEL = 0


class DocxInspectionError(ValueError):
    """Raised when ``bytes`` are not a readable DOCX (never silently tolerated)."""


def open_docx(data: bytes) -> Any:
    """Open ``data`` as a ``python-docx`` ``Document`` (or raise honestly).

    This is the single open path used by inspect / edit / validation, so an
    unreadable byte string is reported identically everywhere.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise DocxInspectionError("DOCX 字节为空")
    try:  # imported lazily so the module stays import-safe without the extra
        from docx import Document

        return Document(io.BytesIO(bytes(data)))
    except Exception as exc:  # noqa: BLE001 — any parse failure is the point
        raise DocxInspectionError(f"无法解析 DOCX：{exc}") from exc


def heading_level(paragraph: Any) -> int | None:
    """The heading level of ``paragraph`` (``0`` = Title, ``1..9`` = Heading n).

    Returns ``None`` for a body paragraph. Detection is by the paragraph's real
    style name (English ``Heading n`` / ``Title`` and the Chinese ``标题 n`` /
    ``标题`` variants), never by text shape — so a bold line that merely *looks*
    like a heading is honestly reported as body text.
    """
    style = getattr(paragraph, "style", None)
    name = str(getattr(style, "name", "") or "").strip()
    if not name:
        return None
    match = _HEADING_RE.match(name)
    if match:
        return int(match.group(1))
    if _TITLE_RE.match(name):
        return _TITLE_LEVEL
    return None


def numbers_in_text(text: str | None) -> list[str]:
    """Every numeric token in ``text``, in order (document order preserved)."""
    return _NUMERIC_RE.findall(str(text or ""))


# --------------------------------------------------------------------------- #
# INC46 T20 — Chinese numerals, leading division markers, numPr labels         #
# --------------------------------------------------------------------------- #
#: A Chinese numeral digit character → its value (both 小写 and 大写 forms).
_CN_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7, "捌": 8, "玖": 9,
}
#: A Chinese numeral unit character → its multiplier.
_CN_UNITS: dict[str, int] = {
    "十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000,
    "万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000,
}
_CN_NUM_CHARS = set(_CN_DIGITS) | set(_CN_UNITS)
_CN_DIGIT_CHARS = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]

#: ``第三部分`` / ``第一章`` … → (ordinal text, division-kind word).
_MARKER_CN_KIND_RE = re.compile(
    r"^\s*第\s*([一二三四五六七八九十百零〇两壹贰叁肆伍陆柒捌玖拾佰千仟万萬亿億]+)\s*"
    r"(部分|章|节|条|篇|编)"
)
_MARKER_AR_KIND_RE = re.compile(r"^\s*第\s*(\d+)\s*(部分|章|节|条|篇|编)")
#: ``三、`` / ``三.`` / ``三)`` … (an enumerated-item marker, no kind word).
_MARKER_CN_PUNCT_RE = re.compile(r"^\s*([一二三四五六七八九十百零〇]+)\s*[、.．)）]")
_MARKER_AR_PUNCT_RE = re.compile(r"^\s*(\d+)\s*[、.．)）]")


def chinese_number_to_int(text: str | None) -> int | None:
    """Parse a Chinese numeral (``三`` / ``十`` / ``二十三`` / ``壹佰万``) to ``int``.

    Returns ``None`` when ``text`` is not a well-formed Chinese numeral — an
    unparsable token is honestly unmeasurable, never a silent ``0``.
    """
    s = str(text or "").strip()
    if not s or any(ch not in _CN_NUM_CHARS for ch in s):
        return None
    total = 0
    section = 0
    number = 0
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        else:
            unit = _CN_UNITS[ch]
            if unit < 10000:
                section += (number or 1) * unit
            else:
                total += (section + number) * unit
                section = 0
            number = 0
    return total + section + number


def _to_chinese_number(value: int) -> str:
    """Format ``value`` as a Chinese numeral (``1→一`` … ``23→二十三`` … ``105→一百零五``)."""
    if value <= 0:
        return str(value)
    if value < 10:
        return _CN_DIGIT_CHARS[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        head = "" if tens == 1 else _CN_DIGIT_CHARS[tens]
        return head + "十" + (_CN_DIGIT_CHARS[ones] if ones else "")
    if value < 10000:
        out = ""
        rem = value
        for unit, label in ((1000, "千"), (100, "百"), (10, "十")):
            digit, rem = divmod(rem, unit)
            if digit:
                out += _CN_DIGIT_CHARS[digit] + label
            elif out and rem:
                out += "零"
        return out + (_CN_DIGIT_CHARS[rem] if rem else "")
    return str(value)


def _format_label_number(value: int, fmt: str | None) -> str:
    """Render an auto-numbering counter ``value`` under its ``w:numFmt`` rule."""
    f = fmt or "decimal"
    if f in (
        "chineseCounting",
        "chineseCountingThousand",
        "chineseLegalSimplified",
        "japaneseCounting",
        "japaneseLegal",
        "ideographTraditional",
        "ideographDigital",
        "taiwaneseCounting",
    ):
        return _to_chinese_number(value)
    if f == "lowerLetter":
        return _to_latin_letters(value).lower()
    if f == "upperLetter":
        return _to_latin_letters(value)
    return str(value)


def _to_latin_letters(value: int) -> str:
    """``1→A`` … ``26→Z`` … ``27→AA`` (bijective base-26, the OOXML rule)."""
    if value <= 0:
        return str(value)
    out = ""
    n = value
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def text_marker(text: str | None) -> dict[str, Any]:
    """The leading division marker of ``text`` (``第三章`` / ``三、`` / ``3.``).

    Returns ``{"marker": str|None, "ordinal": int|None, "kind": str|None}`` — the
    literal surface form, its parsed ordinal number, and the division-kind word
    (``部分``/``章``/… ) or ``"、"`` for a bare enumerated-item marker.
    """
    s = str(text or "").strip()
    match = _MARKER_CN_KIND_RE.match(s)
    if match:
        return {
            "marker": match.group(0).strip(),
            "ordinal": chinese_number_to_int(match.group(1)),
            "kind": match.group(2),
        }
    match = _MARKER_AR_KIND_RE.match(s)
    if match:
        return {"marker": match.group(0).strip(), "ordinal": int(match.group(1)), "kind": match.group(2)}
    match = _MARKER_CN_PUNCT_RE.match(s)
    if match:
        return {"marker": match.group(0).strip(), "ordinal": chinese_number_to_int(match.group(1)), "kind": "、"}
    match = _MARKER_AR_PUNCT_RE.match(s)
    if match:
        return {"marker": match.group(0).strip(), "ordinal": int(match.group(1)), "kind": "、"}
    return {"marker": None, "ordinal": None, "kind": None}


def paragraph_numbering_ids(paragraph: Any) -> tuple[int, int] | None:
    """``(numId, ilvl)`` for an auto-numbered paragraph, else ``None``.

    ``numId == 0`` means "numbering removed" in OOXML, so it is reported as
    un-numbered. Read at the XML level (``w:pPr/w:numPr``) because ``python-docx``
    exposes no high-level numbering API.
    """
    from docx.oxml.ns import qn

    pPr = paragraph._p.find(qn("w:pPr"))
    if pPr is None:
        return None
    numPr = pPr.find(qn("w:numPr"))
    if numPr is None:
        return None
    num_id_el = numPr.find(qn("w:numId"))
    if num_id_el is None or num_id_el.get(qn("w:val")) is None:
        return None
    try:
        num_id = int(num_id_el.get(qn("w:val")))
    except (TypeError, ValueError):
        return None
    if num_id == 0:
        return None
    ilvl_el = numPr.find(qn("w:ilvl"))
    try:
        ilvl = int(ilvl_el.get(qn("w:val"))) if ilvl_el is not None and ilvl_el.get(qn("w:val")) is not None else 0
    except (TypeError, ValueError):
        ilvl = 0
    return num_id, ilvl


def _numbering_tables(document: Any) -> tuple[dict[int, int], dict[int, dict[int, dict[str, Any]]]]:
    """``(numId→abstractNumId, abstractNumId→{ilvl→{fmt,text,start}})`` from ``numbering.xml``."""
    from docx.oxml.ns import qn

    try:
        numbering = document.part.numbering_part.element
    except Exception:  # noqa: BLE001 — a document without a numbering part has no auto labels
        return {}, {}
    num_map: dict[int, int] = {}
    for num in numbering.findall(qn("w:num")):
        try:
            num_id = int(num.get(qn("w:numId")))
        except (TypeError, ValueError):
            continue
        abs_el = num.find(qn("w:abstractNumId"))
        if abs_el is not None and abs_el.get(qn("w:val")) is not None:
            try:
                num_map[num_id] = int(abs_el.get(qn("w:val")))
            except (TypeError, ValueError):
                continue
    abs_map: dict[int, dict[int, dict[str, Any]]] = {}
    for abstract in numbering.findall(qn("w:abstractNum")):
        try:
            abs_id = int(abstract.get(qn("w:abstractNumId")))
        except (TypeError, ValueError):
            continue
        levels: dict[int, dict[str, Any]] = {}
        for lvl in abstract.findall(qn("w:lvl")):
            try:
                ilvl = int(lvl.get(qn("w:ilvl")))
            except (TypeError, ValueError):
                ilvl = 0
            fmt_el = lvl.find(qn("w:numFmt"))
            text_el = lvl.find(qn("w:lvlText"))
            start_el = lvl.find(qn("w:start"))
            start = 1
            if start_el is not None and start_el.get(qn("w:val")) is not None:
                try:
                    start = int(start_el.get(qn("w:val")))
                except (TypeError, ValueError):
                    start = 1
            levels[ilvl] = {
                "fmt": (fmt_el.get(qn("w:val")) if fmt_el is not None else None) or "decimal",
                "text": (text_el.get(qn("w:val")) if text_el is not None else None) or "%1",
                "start": start,
            }
        abs_map[abs_id] = levels
    return num_map, abs_map


def compute_numbering_labels(document: Any, paragraphs: list[Any]) -> list[str | None]:
    """The rendered auto-numbering label for each paragraph (``三、`` / ``3.`` …).

    Simulates the per-``numId`` level counters over the body paragraph sequence
    and substitutes ``%1``…``%9`` in each level's ``lvlText``, so a paragraph with
    ``w:numPr`` gets the label Word would actually display. A paragraph without
    numbering (or whose level definition is absent) yields ``None``.
    """
    num_map, abs_map = _numbering_tables(document)
    if not num_map:
        return [None] * len(paragraphs)
    counters: dict[int, dict[int, int]] = {}
    labels: list[str | None] = []
    for paragraph in paragraphs:
        ids = paragraph_numbering_ids(paragraph)
        if ids is None:
            labels.append(None)
            continue
        num_id, ilvl = ids
        levels = abs_map.get(num_map.get(num_id), {})
        counter = counters.setdefault(num_id, {})
        start = levels.get(ilvl, {}).get("start", 1)
        counter[ilvl] = counter.get(ilvl, start - 1) + 1
        for deeper in [k for k in list(counter) if k > ilvl]:
            del counter[deeper]
        level_def = levels.get(ilvl)
        if level_def is None:
            labels.append(None)
            continue

        def _substitute(match: re.Match[str]) -> str:
            idx = int(match.group(1)) - 1
            value = counter.get(idx, levels.get(idx, {}).get("start", 1))
            return _format_label_number(value, levels.get(idx, {}).get("fmt"))

        labels.append(re.sub(r"%([1-9])", _substitute, str(level_def["text"])))
    return labels


def document_numbers(data: bytes) -> list[str]:
    """Every numeric token in the document's body paragraphs, in order.

    Used by the edit handler to build the "numbers that must survive" guard: the
    original numbers minus the ones an explicit edit legitimately liberates.
    """
    document = open_docx(data)
    out: list[str] = []
    for paragraph in document.paragraphs:
        out.extend(_NUMERIC_RE.findall(paragraph.text))
    return out


def _text_stats(texts: list[str]) -> tuple[int, int]:
    """Return ``(words, char_count)`` for a paragraph-text sequence.

    * ``words`` — CJK ideographs (one per character) plus whitespace-delimited
      Latin/numeric tokens (one per token);
    * ``char_count`` — non-whitespace characters (the "字数" a ``≤N 字`` rule
      measures).
    """
    joined = "\n".join(texts)
    cjk = len(_CJK_RE.findall(joined))
    tokens = 0
    for raw in re.split(r"\s+", joined):
        token = raw.strip()
        if not token:
            continue
        if any(ch.isalnum() for ch in _CJK_RE.sub("", token)):
            tokens += 1
    return cjk + tokens, len(re.sub(r"\s+", "", joined))


@dataclass
class DocStructure:
    """The measured structure of a DOCX document."""

    paragraphs: int = 0
    headings: list[dict[str, Any]] = field(default_factory=list)
    sections: list[dict[str, Any]] = field(default_factory=list)
    words: int = 0
    char_count: int = 0
    tables: int = 0
    images: int = 0
    # INC46 T20 — additive, document-level id lists (stable, ordered). They never
    # change the meaning of the existing fields above and are ``[]`` when the
    # document has none, so every existing consumer is byte-for-byte unchanged.
    table_ids: list[str] = field(default_factory=list)
    image_ids: list[str] = field(default_factory=list)

    def to_dict(self, limit: int | None = None) -> dict[str, Any]:
        """JSON-safe structure summary.

        ``limit`` bounds ``headings`` / ``sections`` so a very large document
        cannot blow past the executor's payload ceiling; when it bites, the two
        totals are reported verbatim alongside ``"truncated": True`` so the cap
        is explicit rather than a silent loss.
        """
        headings = list(self.headings)
        sections = list(self.sections)
        truncated = False
        if limit is not None and limit >= 0:
            if len(headings) > limit:
                headings = headings[:limit]
                truncated = True
            if len(sections) > limit:
                sections = sections[:limit]
                truncated = True
        out: dict[str, Any] = {
            "paragraphs": self.paragraphs,
            "heading_count": len(self.headings),
            "headings": headings,
            "section_count": len(self.sections),
            "sections": sections,
            "words": self.words,
            "char_count": self.char_count,
            "tables": self.tables,
            "images": self.images,
            # INC46 T20 — additive keys; existing keys above are unchanged.
            "table_ids": list(self.table_ids),
            "image_ids": list(self.image_ids),
        }
        if truncated:
            out["truncated"] = True
        return out


def inspect_docx(data: bytes) -> DocStructure:
    """Read the **real** structure of a DOCX byte string.

    Raises:
        DocxInspectionError: ``data`` is empty or is not a readable DOCX.
    """
    document = open_docx(data)
    paragraphs = list(document.paragraphs)
    texts = [p.text for p in paragraphs]

    numbering_labels = compute_numbering_labels(document, paragraphs)

    headings: list[dict[str, Any]] = []
    for index, paragraph in enumerate(paragraphs):
        level = heading_level(paragraph)
        if level is not None and paragraph.text.strip():
            headings.append(
                {
                    "index": index,
                    "level": level,
                    "text": paragraph.text.strip(),
                    # INC46 T20 — additive per-heading fields (index/level/text
                    # above are unchanged; consumers read them by key).
                    "numbering_label": numbering_labels[index],
                    "section_end_index": len(paragraphs),  # filled in below
                }
            )

    sections: list[dict[str, Any]] = []
    for heading in headings:
        start = int(heading["index"])
        level = int(heading["level"])
        end = len(paragraphs)
        for j in range(start + 1, len(paragraphs)):
            other = paragraphs[j]
            other_level = heading_level(other)
            if other_level is not None and other.text.strip() and other_level <= level:
                end = j
                break
        heading["section_end_index"] = end
        body = [
            paragraphs[k].text
            for k in range(start + 1, end)
            if paragraphs[k].text.strip()
        ]
        sections.append(
            {
                "title": heading["text"],
                "level": level,
                "index": start,
                "paragraphs": len(body),
            }
        )

    words, char_count = _text_stats(texts)
    try:
        images = len(document.inline_shapes)
    except Exception:  # noqa: BLE001 — a shape-part quirk must not break inspection
        images = 0

    return DocStructure(
        paragraphs=len(paragraphs),
        headings=headings,
        sections=sections,
        words=words,
        char_count=char_count,
        tables=len(document.tables),
        images=images,
        table_ids=[f"table[{i}]" for i in range(len(document.tables))],
        image_ids=[f"image[{i}]" for i in range(images)],
    )
