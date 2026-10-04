"""INC46 T27 — the document **format capability matrix** + honest degradation text.

This module makes the platform's real posture explicit: the platform can *read*
four formats and can *edit* only a subset of them (and, for PDF, only in ways that
are **not** in-place). Instead of leaving that implicit in which handler happens
to exist, the matrix below is the single, machine-readable statement of:

  * which operations a format really supports (:attr:`FormatCapability.supported`);
  * what is preserved but never rewritten
    (:attr:`FormatCapability.protected` / ``preserved``);
  * what is **unsupported** and must be answered honestly rather than faked
    (:attr:`FormatCapability.unsupported`);

Red line 17 (诚实降级): an unsupported format or operation must be declared as
``unsupported`` with a real reason — never dressed up as a successful in-place
edit. :func:`unsupported_reason` is the one place a caller asks "can I really do
this?" and gets an honest answer.

Format detection is by **bytes** (:func:`sniff_format`), never by file suffix:
:meth:`forgeflow.resources.storage.FileBlobStore.put` returns a content-addressed,
**extensionless** path, so the only trustworthy format signal is the content.

The capability data is derived from the real edit planes whose ops it advertises
(``docx_edit`` / ``pptx_edit`` / ``sheet_edit`` / ``xlsx_edit`` / ``pdf_policy``),
so the matrix and the writers can never silently drift apart.

:func:`render_capability_text` is a **pure function** returning the user-visible
capability reply (the same matrix, phrased for a human); the ``GET
/documents/capabilities`` route serves both ``capability_matrix()`` and that text.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from typing import Any

__all__ = [
    "FORMAT_DOCX",
    "FORMAT_PPTX",
    "FORMAT_XLSX",
    "FORMAT_PDF",
    "FORMAT_OTHER",
    "FORMATS",
    "FormatCapability",
    "CAPABILITIES",
    "sniff_format",
    "capability_matrix",
    "format_capability",
    "supports",
    "unsupported_reason",
    "render_capability_text",
]

#: Canonical format ids (the vocabulary shared by the matrix and the route).
FORMAT_DOCX = "docx"
FORMAT_PPTX = "pptx"
FORMAT_XLSX = "xlsx"
FORMAT_PDF = "pdf"
FORMAT_OTHER = "other"

#: The formats in display order (``other`` last — it is the honest fallback).
FORMATS: tuple[str, ...] = (
    FORMAT_DOCX,
    FORMAT_PPTX,
    FORMAT_XLSX,
    FORMAT_PDF,
    FORMAT_OTHER,
)


# --------------------------------------------------------------------------- #
# Byte-level format sniffing (never by suffix)                                 #
# --------------------------------------------------------------------------- #
def sniff_format(data: bytes) -> str:
    """Sniff a document's real format from its bytes.

    An OOXML package is a zip: ``word/`` ⇒ ``docx``, ``ppt/`` ⇒ ``pptx``,
    ``xl/`` ⇒ ``xlsx``. A PDF starts with ``%PDF-``. Anything else (empty bytes,
    a plain text blob, an unreadable zip) is :data:`FORMAT_OTHER` — never guessed
    from a file name.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        return FORMAT_OTHER
    raw = bytes(data)
    if raw[:5] == b"%PDF-":
        return FORMAT_PDF
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
    except Exception:  # noqa: BLE001 — a non-zip blob simply has no OOXML format
        return FORMAT_OTHER
    if any(name.startswith("word/") for name in names):
        return FORMAT_DOCX
    if any(name.startswith("ppt/") for name in names):
        return FORMAT_PPTX
    if any(name.startswith("xl/") for name in names):
        return FORMAT_XLSX
    return FORMAT_OTHER


# --------------------------------------------------------------------------- #
# Capability descriptor                                                        #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FormatCapability:
    """One format's honest capability record (format × capability × reason)."""

    format: str
    label: str
    #: ``True`` when the platform can really write this format.
    editable: bool
    #: ``True`` only when editing happens **in place**; PDF is ``False``.
    inplace: bool
    #: The edit operations this format really supports.
    supported: tuple[str, ...]
    #: Capabilities that are intentionally **not** rewritten (kept intact).
    protected: tuple[str, ...]
    #: Capabilities the platform cannot serve — answered ``unsupported``.
    unsupported: tuple[str, ...]
    #: Document features preserved verbatim on every edit.
    preserved: tuple[str, ...]
    #: Non-in-place alternatives offered (PDF only; empty otherwise).
    strategies: tuple[str, ...]
    #: The concrete tool / library that does the work.
    engine: str
    #: The honest, human-readable degradation note.
    notes: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe capability record."""
        return {
            "format": self.format,
            "label": self.label,
            "editable": self.editable,
            "inplace": self.inplace,
            "supported": list(self.supported),
            "protected": list(self.protected),
            "unsupported": list(self.unsupported),
            "preserved": list(self.preserved),
            "strategies": list(self.strategies),
            "engine": self.engine,
            "notes": self.notes,
        }


def _build_capabilities() -> dict[str, FormatCapability]:
    """Derive the matrix from the real edit planes (single source of truth)."""
    # Imported lazily so this module stays import-light and cycle-free: every
    # name below is imported from a *submodule* (never the package ``__init__``).
    from forgeflow.documents.docx_edit import SUPPORTED_OPS as _DOCX_OPS
    from forgeflow.documents.pdf_policy import PDF_STRATEGIES as _PDF_STRATEGIES
    from forgeflow.documents.pptx_edit import (
        SUPPORTED_OPS as _PPTX_OPS,
        UNSUPPORTED_CAPABILITIES as _PPTX_UNSUPPORTED,
    )
    from forgeflow.documents.sheet_edit import SUPPORTED_OPS as _XLSX_OPS
    from forgeflow.documents.xlsx_edit import (
        PRESERVED_FEATURES as _XLSX_PRESERVED,
        PROTECTED_FEATURES as _XLSX_PROTECTED,
    )

    return {
        FORMAT_DOCX: FormatCapability(
            format=FORMAT_DOCX,
            label="Word 文档（.docx）",
            editable=True,
            inplace=True,
            supported=tuple(_DOCX_OPS),
            protected=(),
            unsupported=(),
            preserved=("run_style", "table", "numbering", "heading"),
            strategies=(),
            engine="python-docx",
            notes="全量支持（本任务书基线）：段落 / 文本 / 样式真实字节编辑",
        ),
        FORMAT_PPTX: FormatCapability(
            format=FORMAT_PPTX,
            label="PowerPoint 演示文稿（.pptx）",
            editable=True,
            inplace=True,
            supported=tuple(_PPTX_OPS),
            # 文本可改，但这些结构刻意保持不动。
            protected=("layout", "master", "image"),
            unsupported=tuple(_PPTX_UNSUPPORTED),
            preserved=("run_style", "shape_geometry", "slide_structure"),
            strategies=(),
            engine="python-pptx",
            notes=(
                "支持文本替换 / 改写（保留 run 样式）与备注；"
                "不改版式/母版/图片；动画/复杂形状 ⇒ unsupported"
            ),
        ),
        FORMAT_XLSX: FormatCapability(
            format=FORMAT_XLSX,
            label="Excel 工作簿（.xlsx / .xlsm）",
            editable=True,
            inplace=True,
            supported=tuple(_XLSX_OPS),
            # 公式单元格默认受保护（不得用常量覆盖公式；改公式须显式意图）。
            protected=tuple(_XLSX_PROTECTED),
            unsupported=(),
            preserved=tuple(_XLSX_PRESERVED),
            strategies=(),
            engine="openpyxl",
            notes=(
                "支持单元格值与文本修改；公式单元格默认受保护"
                "（不得用常量覆盖公式；改公式须显式意图）；"
                "保留格式/数据验证/条件格式/图表引用"
            ),
        ),
        FORMAT_PDF: FormatCapability(
            format=FORMAT_PDF,
            label="PDF 文档（.pdf）",
            editable=False,
            # PDF 不承诺原位编辑。
            inplace=False,
            supported=(),
            protected=(),
            unsupported=("inplace_edit",),
            preserved=(),
            strategies=tuple(_PDF_STRATEGIES),
            engine="pypdf / fpdf2",
            notes=(
                "不承诺原位编辑；可选：①提取→重建新文档（标注「重建，非原位」）"
                "②批注/高亮叠加 ③拒绝并说明"
            ),
        ),
        FORMAT_OTHER: FormatCapability(
            format=FORMAT_OTHER,
            label="其他格式",
            editable=False,
            inplace=False,
            supported=(),
            protected=(),
            unsupported=("*",),
            preserved=(),
            strategies=(),
            engine="",
            notes="不支持：返回 unsupported + 原因（不猜测、不伪装成功）",
        ),
    }


#: The capability matrix, keyed by format id (built once at import — pure).
CAPABILITIES: dict[str, FormatCapability] = _build_capabilities()


# --------------------------------------------------------------------------- #
# Matrix accessors (pure)                                                      #
# --------------------------------------------------------------------------- #
def capability_matrix() -> dict[str, dict[str, Any]]:
    """The full matrix as a JSON-safe ``{format: capability}`` mapping."""
    return {fmt: CAPABILITIES[fmt].to_dict() for fmt in FORMATS}


def format_capability(fmt: str) -> FormatCapability:
    """The capability record for ``fmt`` (unknown formats map to ``other``)."""
    token = str(fmt or "").strip().lower()
    return CAPABILITIES.get(token, CAPABILITIES[FORMAT_OTHER])


def supports(fmt: str, operation: str) -> bool:
    """Whether ``fmt`` really supports ``operation`` (exact, never a guess)."""
    cap = format_capability(fmt)
    return str(operation or "").strip() in cap.supported


def unsupported_reason(fmt: str, operation: str = "") -> str:
    """An honest ``unsupported`` reason, or ``""`` when the request is supported.

    Red line 17: this is the single gate a caller uses before promising an edit.
    A non-empty return **must** be surfaced verbatim (never swallowed into a fake
    success).
    """
    token = str(fmt or "").strip().lower()
    cap = CAPABILITIES.get(token)
    if cap is None:
        return (
            f"不支持的格式：{fmt!r}（unsupported）——"
            "本平台仅支持 docx / pptx / xlsx（可编辑）与 pdf（非原位）；"
            "其他格式请先转换或另选工具"
        )
    if not cap.editable:
        if cap.format == FORMAT_PDF:
            return (
                "PDF 不承诺原位编辑（unsupported_inplace）："
                "可选 ①提取→重建新文档（重建，非原位）"
                "②批注/高亮叠加 ③拒绝并说明——绝不假装原位编辑成功"
            )
        return f"不支持的格式：{cap.label}（unsupported）——{cap.notes}"
    op = str(operation or "").strip()
    if not op:
        return ""
    if op in cap.supported:
        return ""
    return (
        f"不支持的编辑操作：{op!r}（unsupported）——"
        f"{cap.label} 仅支持 {', '.join(cap.supported) or '（无）'}；"
        f"其中 {', '.join(cap.protected) or '（无）'} 保持不变，"
        f"{', '.join(cap.unsupported) or '（无）'} 无法处理"
    )


# --------------------------------------------------------------------------- #
# User-visible reply (pure)                                                    #
# --------------------------------------------------------------------------- #
def render_capability_text(fmt: str | None = None) -> str:
    """The user-visible capability reply (pure; identical to the API matrix).

    ``fmt`` restricts the reply to one format; ``None`` renders the whole matrix.
    The text always states each format's honest limits and degradation (red line
    17), so a user is never led to believe an in-place edit happened when it did
    not.
    """
    formats = [str(fmt).strip().lower()] if fmt else list(FORMATS)
    lines: list[str] = ["文档格式能力矩阵（诚实声明）："]
    for token in formats:
        cap = CAPABILITIES.get(token)
        if cap is None:
            lines.append(f"- {token}：不支持（unsupported）——未识别的格式，不猜测")
            continue
        if cap.supported:
            supported = "、".join(cap.supported)
        else:
            supported = "（无原位编辑操作）"
        parts = [f"- {cap.label}：可编辑={cap.editable}，原位={cap.inplace}"]
        parts.append(f"支持操作：{supported}")
        if cap.protected:
            parts.append(f"保持不变：{'、'.join(cap.protected)}")
        if cap.unsupported:
            parts.append(f"无法处理（unsupported）：{'、'.join(cap.unsupported)}")
        if cap.strategies:
            parts.append(f"替代方案：{'、'.join(cap.strategies)}")
        parts.append(cap.notes)
        lines.append("；".join(parts))
    return "\n".join(lines)
