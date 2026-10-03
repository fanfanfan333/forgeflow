"""INC45 §1.2 — real PDF inspection (read-only), reusing ``extract_pdf_text``.

Layer contract (design §1.2 / §8): this module **only reads** bytes. It is the
honest "inspect" layer for PDFs — it reports a document's *measured* facts
(page count, per-page character counts, metadata, a text excerpt) and never
writes a file and never calls a model.

Why reuse :func:`forgeflow.multimodal.pdf.extract_pdf_text`
----------------------------------------------------------
That function is the existing, tested, single source of truth for PDF text +
metadata. Re-implementing extraction here would create a second, drifting
"truth". So ``inspect_pdf`` is a thin, honest projection of its result: the
``.pdf`` byte string goes in, the measured facts come out.

Dependency posture: ``pypdf`` is an optional extra (``pdf``). When it is absent
``extract_pdf_text`` raises ``ImportError``; we convert that — verbatim — into
:class:`PdfInspectionError`, which the handler turns into an honest failure
(``not_executed``, HTTP < 500). Nothing is guessed or fabricated.

Honesty rules:
  * an unmeasured numeric fact is ``None`` — never a fake ``0``;
  * a scanned PDF with no embedded text returns empty pages, verbatim (the same
    posture as ``multimodal.pdf``; OCR is a roadmap item, not a silent guess).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["PdfFacts", "PdfInspectionError", "inspect_pdf"]

#: How much of the extracted text an inspect returns as an excerpt. Kept modest
#: so an inspect never blows past the executor's payload ceiling; the full text
#: is never needed to answer "what kind of PDF is this".
_EXCERPT_CHARS = 2000


class PdfInspectionError(ValueError):
    """Raised when bytes are not a readable PDF (or the extra is missing)."""


@dataclass
class PdfFacts:
    """The measured facts of a PDF document."""

    #: Total page count of the document — ``None`` only when unmeasurable.
    page_count: int | None = None
    #: Character count of each **extracted** page (scanned pages yield ``0``
    #: honestly — the page really has no extractable text).
    page_chars: list[int] = field(default_factory=list)
    #: Total extracted characters across the extracted pages.
    chars: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    text_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe facts summary."""
        return {
            "page_count": self.page_count,
            "pages_extracted": len(self.page_chars),
            "page_chars": list(self.page_chars),
            "chars": self.chars,
            "metadata": dict(self.metadata),
            "text_excerpt": self.text_excerpt,
        }


def inspect_pdf(data: bytes, *, max_pages: int | None = None) -> PdfFacts:
    """Read the **real** facts of a PDF byte string.

    ``max_pages`` bounds how many pages are extracted for the excerpt / per-page
    counts (``None`` = whole document); ``page_count`` always reports the
    document's total page count regardless of that bound.

    Raises:
        PdfInspectionError: ``data`` is empty / unreadable, or the ``pypdf``
            extra is absent (reason carried verbatim from ``extract_pdf_text``).
    """
    from forgeflow.multimodal.pdf import extract_pdf_text

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PdfInspectionError("PDF 字节为空")

    try:
        document = extract_pdf_text(bytes(data), max_pages=max_pages)
    except ImportError as exc:
        raise PdfInspectionError(f"pdf 支持不可用：{exc}") from exc
    except ValueError as exc:
        raise PdfInspectionError(f"无法解析 PDF：{exc}") from exc

    page_chars = [len(page) for page in document.pages]
    return PdfFacts(
        page_count=int(document.page_count),
        page_chars=page_chars,
        chars=sum(page_chars),
        metadata=dict(document.metadata),
        text_excerpt=document.text[:_EXCERPT_CHARS],
    )
