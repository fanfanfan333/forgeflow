"""INC46 T23 — **L4** render comparison (渲染比对：转 PDF → 栅格化 → 像素 diff).

What L4 checks
--------------
The strongest fidelity check is *what a human would see*: render the document to
PDF, rasterise it, and diff the pixels. L4 does exactly that, bounded to the
**target bounding box**:

  1. convert ``before`` and ``after`` to PDF with a real engine (LibreOffice
     ``soffice``);
  2. rasterise both PDFs to a pixel grid (Poppler ``pdftoppm``);
  3. count differing pixels inside ``bbox`` and fail when the ratio exceeds the
     tolerance;
  4. **measure the page count** of both PDFs (via the existing
     :func:`forgeflow.documents.pdf_inspect.inspect_pdf` seam) and report the
     ``page_delta``. A page-count change ("页数变化") is reported as a stack-level
     ``warn`` (see :mod:`forgeflow.documents.validation.stack`), never folded into
     L4's own ``status`` — L4's ``status`` is decided by the pixel diff alone, and
     an unmeasured page count (``page_delta is None``) never raises a warning.

Unmeasured ≠ pass (red line 4 / 15)
-----------------------------------
If the engine is absent, the conversion fails, or no rasteriser is available, L4
returns ``status=None`` ("not measured") — **never** ``pass``. This box has no
``soffice`` (measured), so on it L4 is honestly ``None``; the code still probes
for the engine for real (no hard-coded ``False``) and would measure the moment an
engine appeared.

Both the engine probe and the rasteriser are **injectable** so the render path is
testable on a host without LibreOffice without ever faking a verdict: a test
supplies a deterministic engine/rasteriser, and the layer runs the real diff.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

from forgeflow.documents.validation.verdict import (
    FAIL,
    LAYER_RENDER,
    PASS,
    LayerVerdict,
)

__all__ = [
    "RasterImage",
    "probe_pdf_page_count",
    "probe_rasterizer",
    "probe_render_engine",
    "render_verdict",
]

#: A rasterised page: a list of rows, each a list of ``(r, g, b)`` tuples.
RasterImage = list[list[tuple[int, int, int]]]

#: ``bbox`` = ``(x0, y0, x1, y1)`` in pixels; ``None`` ⇒ the whole overlapping page.
BBox = tuple[int, int, int, int]

_ENGINE_NAMES: tuple[str, ...] = ("soffice", "libreoffice")
_RASTERIZER_NAMES: tuple[str, ...] = ("pdftoppm",)


# --------------------------------------------------------------------------- #
# engine / rasteriser probes (real; never hard-coded)                          #
# --------------------------------------------------------------------------- #
def probe_render_engine() -> str | None:
    """Path to the PDF conversion engine (LibreOffice), or ``None`` (real probe)."""
    for name in _ENGINE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def probe_rasterizer() -> str | None:
    """Path to the PDF rasteriser (Poppler ``pdftoppm``), or ``None`` (real probe)."""
    for name in _RASTERIZER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def probe_pdf_page_count(pdf: bytes) -> int | None:
    """Total page count of a PDF, via the existing ``inspect_pdf`` seam.

    Reuses :func:`forgeflow.documents.pdf_inspect.inspect_pdf` (which itself reuses
    :func:`forgeflow.multimodal.pdf.extract_pdf_text`) — **no** second PDF parser
    is introduced. Returns ``None`` (not measured) when the PDF cannot be read or
    the optional ``pypdf`` extra is unavailable — **never** a fabricated ``0``.
    """
    try:
        from forgeflow.documents.pdf_inspect import PdfInspectionError, inspect_pdf
    except Exception:  # noqa: BLE001 — the seam itself is missing ⇒ cannot measure
        return None
    try:
        facts = inspect_pdf(bytes(pdf))
    except PdfInspectionError:
        return None
    except Exception:  # noqa: BLE001 — any read failure is honestly unmeasured
        return None
    return facts.page_count


def _page_delta(
    pdf_before: bytes,
    pdf_after: bytes,
    counter: Callable[[bytes], int | None],
) -> tuple[int | None, int | None, int | None, str]:
    """``(page_delta, pages_before, pages_after, evidence_note)``.

    ``page_delta`` is an integer **only when both ends were measured**; if either
    side is ``None`` it is ``None`` (not measured) — never a fabricated ``0``.
    """
    before_pages = counter(pdf_before)
    after_pages = counter(pdf_after)
    if before_pages is None or after_pages is None:
        return None, before_pages, after_pages, ";pages=not measured"
    delta = int(after_pages) - int(before_pages)
    return delta, int(before_pages), int(after_pages), (
        f";pages={before_pages}->{after_pages}(page_delta={delta})"
    )


# --------------------------------------------------------------------------- #
# default (real) engine + rasteriser                                           #
# --------------------------------------------------------------------------- #
def _convert_to_pdf(source: bytes, engine: str, *, timeout: int = 90) -> bytes | None:
    """Convert DOCX bytes to PDF via ``engine`` (bounded). ``None`` on failure."""
    try:
        with tempfile.TemporaryDirectory(prefix="ff-render-") as work:
            src = os.path.join(work, "candidate.docx")
            with open(src, "wb") as handle:
                handle.write(bytes(source))
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [engine, "--headless", "--convert-to", "pdf", "--outdir", work, src],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            produced = os.path.join(work, "candidate.pdf")
            if proc.returncode == 0 and os.path.exists(produced):
                with open(produced, "rb") as handle:
                    return handle.read()
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _default_rasterizer(pdf: bytes, tool: str, *, dpi: int = 96) -> RasterImage | None:
    """Rasterise the **first** PDF page to a pixel grid via Poppler ``pdftoppm``.

    Uses ``Pillow`` to decode the produced PNG; if Pillow is unavailable the
    rasterisation is honestly unmeasured (``None``).
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — no decoder ⇒ cannot measure
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="ff-raster-") as work:
            pdf_path = os.path.join(work, "page.pdf")
            with open(pdf_path, "wb") as handle:
                handle.write(bytes(pdf))
            prefix = os.path.join(work, "out")
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [tool, "-png", "-r", str(int(dpi)), "-f", "1", "-l", "1", pdf_path, prefix],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if proc.returncode != 0:
                return None
            candidates = sorted(
                os.path.join(work, name)
                for name in os.listdir(work)
                if name.startswith("out") and name.endswith(".png")
            )
            if not candidates:
                return None
            with Image.open(candidates[0]) as image:
                rgb = image.convert("RGB")
                width, height = rgb.size
                pixels = list(rgb.getdata())
            return [
                pixels[row * width : (row + 1) * width]
                for row in range(height)
            ]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


# --------------------------------------------------------------------------- #
# pixel diff                                                                   #
# --------------------------------------------------------------------------- #
def _bbox_bounds(
    a: RasterImage, b: RasterImage, bbox: BBox | None
) -> tuple[int, int, int, int]:
    height = min(len(a), len(b))
    width = min(len(a[0]) if a else 0, len(b[0]) if b else 0)
    if bbox is None:
        return 0, 0, width, height
    x0, y0, x1, y1 = bbox
    return max(0, x0), max(0, y0), min(width, x1), min(height, y1)


def diff_ratio(a: RasterImage, b: RasterImage, bbox: BBox | None) -> float:
    """Fraction of differing pixels inside ``bbox`` (``0.0`` when identical).

    A size mismatch is the maximal difference (``1.0``) — a different page size is
    a real, visible change, never silently ignored.
    """
    if len(a) != len(b) or (a and b and len(a[0]) != len(b[0])):
        return 1.0
    x0, y0, x1, y1 = _bbox_bounds(a, b, bbox)
    total = 0
    differing = 0
    for row in range(y0, y1):
        ra, rb = a[row], b[row]
        for col in range(x0, x1):
            total += 1
            if ra[col] != rb[col]:
                differing += 1
    if total == 0:
        return 0.0
    return differing / total


# --------------------------------------------------------------------------- #
# verdict                                                                      #
# --------------------------------------------------------------------------- #
def render_verdict(
    before: bytes,
    after: bytes,
    *,
    engine: str | None = None,
    converter: Callable[[bytes, str], bytes | None] | None = None,
    rasterizer: Callable[[bytes, str, int], RasterImage | None] | None = None,
    rasterizer_name: str | None = None,
    page_counter: Callable[[bytes], int | None] | None = None,
    bbox: BBox | None = None,
    dpi: int = 96,
    tolerance: float = 0.0,
) -> LayerVerdict:
    """L4 — render-comparison verdict between ``before`` and ``after``.

    Args:
        before: original DOCX bytes.
        after: edited DOCX bytes.
        engine: conversion-engine path override (defaults to a real probe).
        converter: injectable ``(docx_bytes, engine) -> pdf_bytes | None``.
        rasterizer: injectable ``(pdf, tool, dpi) -> RasterImage | None``.
        rasterizer_name: tool path passed to the rasteriser (defaults to a probe).
        page_counter: injectable ``(pdf) -> int | None`` page-count reader
            (defaults to :func:`probe_pdf_page_count`).
        bbox: ``(x0, y0, x1, y1)`` pixel box to compare (``None`` ⇒ whole page).
        dpi: rasterisation resolution.
        tolerance: allowed differing-pixel ratio below which the page still passes.

    Returns:
        A :class:`LayerVerdict`; ``fail`` iff the rendered pages differ beyond
        ``tolerance`` **inside ``bbox``**; ``None`` (not measured) when the engine,
        conversion or rasteriser is unavailable — never a fabricated ``pass``.

        ``detail["page_delta"]`` is an integer (``after - before``) **only when
        both** page counts were measured, else ``None``; it never changes this
        layer's ``status`` (a page-count change is surfaced as a stack-level
        ``warn`` by :func:`forgeflow.documents.validation.stack.validate_document`).
    """
    resolved_engine = engine if engine is not None else probe_render_engine()
    if resolved_engine is None:
        return LayerVerdict(
            LAYER_RENDER,
            None,
            "render:not measured (no soffice/libreoffice engine);pages=not measured",
            {"engine": None, "page_delta": None, "pages_before": None, "pages_after": None},
        )

    convert = converter if converter is not None else _convert_to_pdf
    pdf_before = convert(before, resolved_engine)
    pdf_after = convert(after, resolved_engine)
    if pdf_before is None or pdf_after is None:
        return LayerVerdict(
            LAYER_RENDER,
            None,
            "render:not measured (PDF conversion produced no output);pages=not measured",
            {"engine": resolved_engine, "page_delta": None, "pages_before": None, "pages_after": None},
        )

    counter: Callable[[bytes], int | None] = (
        page_counter if page_counter is not None else probe_pdf_page_count
    )
    page_delta, pages_before, pages_after, page_note = _page_delta(
        pdf_before, pdf_after, counter
    )
    page_detail: dict[str, Any] = {
        "page_delta": page_delta,
        "pages_before": pages_before,
        "pages_after": pages_after,
    }

    tool = rasterizer_name if rasterizer_name is not None else probe_rasterizer()
    raster = rasterizer
    if raster is None:
        if tool is None:
            return LayerVerdict(
                LAYER_RENDER,
                None,
                "render:not measured (no Pdftoppm rasteriser)" + page_note,
                {"engine": resolved_engine, **page_detail},
            )

        def _default(pdf: bytes, _tool: str, _dpi: int) -> RasterImage | None:
            return _default_rasterizer(pdf, _tool, dpi=_dpi)

        raster = _default

    image_before = raster(pdf_before, tool or "", dpi)
    image_after = raster(pdf_after, tool or "", dpi)
    if image_before is None or image_after is None:
        return LayerVerdict(
            LAYER_RENDER,
            None,
            "render:not measured (rasterisation unavailable)" + page_note,
            {"engine": resolved_engine, **page_detail},
        )

    ratio = diff_ratio(image_before, image_after, bbox)
    status = PASS if ratio <= tolerance else FAIL
    evidence = (
        f"render:engine={os.path.basename(resolved_engine)};bbox={bbox};"
        f"diff_ratio={ratio:.4f};tolerance={tolerance}" + page_note
    )
    detail: dict[str, Any] = {
        "engine": resolved_engine,
        "bbox": list(bbox) if bbox is not None else None,
        "diff_ratio": ratio,
        "tolerance": tolerance,
        **page_detail,
    }
    return LayerVerdict(LAYER_RENDER, status, evidence, detail)
