"""INC44 §2.2 — real PPTX structural inspection via ``python-pptx``.

Layer contract (design §1.2 / §8): this module **only reads** bytes. It never
writes a file and never calls a model — it is the honest "inspect" layer that
reports a presentation's *measured* structure (slides / shapes / text frames /
paragraphs / charts / notes / char count). Nothing is guessed: a byte string
that ``python-pptx`` cannot open raises :class:`PptxInspectionError`, which the
handler converts into an honest failure.

Why a dedicated types module
----------------------------
``PptxStructure`` is shared by ``pptx_edit`` (to address slides / shapes) and by
the runtime handler (to build the small inspect payload). The numeric-token rule
is re-used from :mod:`forgeflow.documents.docx_inspect` (``numbers_in_text``), so
there is exactly **one** definition of "what is a number" across the whole
document domain — docx and pptx can never disagree on the 「数字变化」 figure.

Dependency posture: ``python-pptx`` is an optional extra. ``open_pptx`` imports
it lazily; when it is absent the call raises :class:`PptxInspectionError` (the
caller degrades honestly — ``metadata_only`` + a verbatim reason, HTTP < 500),
mirroring the ``.docx`` / ``python-docx`` posture exactly.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PptxStructure",
    "PptxInspectionError",
    "open_pptx",
    "inspect_pptx",
    "slide_texts",
    "presentation_numbers",
    "shape_paragraph_texts",
]

_WS_RE = re.compile(r"\s+")


class PptxInspectionError(ValueError):
    """Raised when bytes are not a readable PPTX (never silently tolerated)."""


def open_pptx(data: bytes) -> Any:
    """Open ``data`` as a ``python-pptx`` ``Presentation`` (or raise honestly).

    This is the single open path used by inspect / edit / validation, so an
    unreadable byte string (or a missing ``python-pptx`` extra) is reported
    identically everywhere.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PptxInspectionError("PPTX 字节为空")
    try:  # imported lazily so the module stays import-safe without the extra
        from pptx import Presentation

        return Presentation(io.BytesIO(bytes(data)))
    except ImportError as exc:
        raise PptxInspectionError(f"python-pptx 不可用：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 — any parse failure is the point
        raise PptxInspectionError(f"无法解析 PPTX：{exc}") from exc


def _text_frame_of(shape: Any) -> Any | None:
    """The shape's text frame when it really has one (charts/tables excluded)."""
    has_tf = getattr(shape, "has_text_frame", False)
    if callable(has_tf):
        try:
            has_tf = has_tf()
        except Exception:  # noqa: BLE001 — a quirky shape must not break inspection
            return None
    if not has_tf:
        return None
    return getattr(shape, "text_frame", None)


def _shape_has_chart(shape: Any) -> bool:
    has_chart = getattr(shape, "has_chart", False)
    if callable(has_chart):
        try:
            return bool(has_chart())
        except Exception:  # noqa: BLE001
            return False
    return bool(has_chart)


def _notes_text(slide: Any) -> str:
    """The slide's real notes text (``""`` when it has none)."""
    try:
        if not slide.has_notes_slide:
            return ""
        frame = slide.notes_slide.notes_text_frame
        return str(getattr(frame, "text", "") or "")
    except Exception:  # noqa: BLE001 — a notes quirk must not break inspection
        return ""


def _slide_title(slide: Any) -> str:
    """The slide's title text via its title placeholder (``""`` when none)."""
    try:
        placeholder = slide.shapes.title
    except Exception:  # noqa: BLE001
        placeholder = None
    if placeholder is None:
        return ""
    return str(getattr(placeholder, "text", "") or "").strip()


def shape_paragraph_texts(presentation: Any) -> list[str]:
    """Every non-empty paragraph text across all slides' text frames, in order.

    Used by the pure diff engine so a PPTX diff is computed over exactly the
    textual content the reader would see (slides → shapes → paragraphs).
    """
    out: list[str] = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            frame = _text_frame_of(shape)
            if frame is None:
                continue
            for paragraph in frame.paragraphs:
                text = str(getattr(paragraph, "text", "") or "")
                if text.strip():
                    out.append(text)
    return out


def slide_texts(presentation: Any) -> list[str]:
    """One concatenated text blob per slide (used for ``char_count`` / numbers)."""
    blobs: list[str] = []
    for slide in presentation.slides:
        parts: list[str] = []
        for shape in slide.shapes:
            frame = _text_frame_of(shape)
            if frame is None:
                continue
            text = str(getattr(frame, "text", "") or "")
            if text.strip():
                parts.append(text)
        blobs.append("\n".join(parts))
    return blobs


def presentation_numbers(data: bytes) -> list[str]:
    """Every numeric token in a presentation's text, in slide/shape order."""
    from forgeflow.documents.docx_inspect import numbers_in_text

    presentation = open_pptx(data)
    out: list[str] = []
    for text in shape_paragraph_texts(presentation):
        out.extend(numbers_in_text(text))
    return out


@dataclass
class PptxStructure:
    """The measured structure of a PPTX presentation."""

    slides: int = 0
    shapes: int = 0
    text_frames: int = 0
    paragraphs: int = 0
    charts: int = 0
    notes: int = 0
    char_count: int = 0
    titles: list[str] = field(default_factory=list)

    def to_dict(self, limit: int | None = None) -> dict[str, Any]:
        """JSON-safe structure summary.

        ``limit`` bounds ``titles`` so a very large deck cannot blow past the
        executor's payload ceiling; when it bites, ``title_count`` is reported
        verbatim alongside ``"truncated": True`` so the cap is explicit rather
        than a silent loss.
        """
        titles = list(self.titles)
        truncated = False
        if limit is not None and limit >= 0 and len(titles) > limit:
            titles = titles[:limit]
            truncated = True
        out: dict[str, Any] = {
            "slides": self.slides,
            "shapes": self.shapes,
            "text_frames": self.text_frames,
            "paragraphs": self.paragraphs,
            "charts": self.charts,
            "notes": self.notes,
            "char_count": self.char_count,
            "title_count": len(self.titles),
            "titles": titles,
        }
        if truncated:
            out["truncated"] = True
        return out


def inspect_pptx(data: bytes) -> PptxStructure:
    """Read the **real** structure of a PPTX byte string.

    Raises:
        PptxInspectionError: ``data`` is empty or is not a readable PPTX.
    """
    presentation = open_pptx(data)
    slides = list(presentation.slides)
    shapes = text_frames = paragraphs = charts = notes = 0
    titles: list[str] = []
    blobs: list[str] = []
    for slide in slides:
        title = _slide_title(slide)
        titles.append(title)
        if _notes_text(slide).strip():
            notes += 1
        parts: list[str] = []
        for shape in slide.shapes:
            shapes += 1
            if _shape_has_chart(shape):
                charts += 1
            frame = _text_frame_of(shape)
            if frame is None:
                continue
            text_frames += 1
            for paragraph in frame.paragraphs:
                paragraphs += 1
                text = str(getattr(paragraph, "text", "") or "")
                if text.strip():
                    parts.append(text)
        blobs.append("\n".join(parts))
    joined = "\n".join(blobs)
    char_count = len(_WS_RE.sub("", joined))
    return PptxStructure(
        slides=len(slides),
        shapes=shapes,
        text_frames=text_frames,
        paragraphs=paragraphs,
        charts=charts,
        notes=notes,
        char_count=char_count,
        titles=titles,
    )
