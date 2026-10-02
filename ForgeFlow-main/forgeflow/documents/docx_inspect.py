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

    headings: list[dict[str, Any]] = []
    for index, paragraph in enumerate(paragraphs):
        level = heading_level(paragraph)
        if level is not None and paragraph.text.strip():
            headings.append(
                {"index": index, "level": level, "text": paragraph.text.strip()}
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
    )
