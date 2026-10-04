"""INC46 T27 — the PDF **policy** plane: never promise an in-place edit.

A PDF is a fixed-layout byte stream; there is no supported path to re-flow or
rewrite its text "in place" without a full layout engine. Rather than fake it, the
platform states that plainly and offers three honest alternatives (design §1.2,
red line 17):

  1. **rebuild** — extract the text, then render a **brand-new** document, clearly
     labelled 「重建，非原位」 (a *new* file; the original is untouched);
  2. **annotate** — overlay a note / highlight as an *added* annotation layer on a
     **copy** (again non-in-place; the page content is never rewritten);
  3. **refuse** — decline with an explicit explanation and the alternatives.

:func:`evaluate_pdf_request` is the pure decision function (no bytes touched);
:func:`rebuild_from_pdf` / :func:`annotate_overlay` are the two real,
non-in-place writers. Every writer returns a fresh byte string and never mutates
its input.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PDF_STRATEGIES",
    "PDF_INPLACE_UNSUPPORTED",
    "PdfPolicyError",
    "PdfPolicyDecision",
    "classify_pdf_request",
    "evaluate_pdf_request",
    "rebuild_from_pdf",
    "annotate_overlay",
]

#: The three honest, non-in-place alternatives (in preference order).
PDF_STRATEGIES: tuple[str, ...] = ("rebuild", "annotate", "refuse")

#: The status a caller must surface when an in-place PDF edit is requested.
PDF_INPLACE_UNSUPPORTED = "unsupported_inplace"

#: Short, stable labels for each strategy (used in the user-visible reply).
_STRATEGY_LABELS: dict[str, str] = {
    "rebuild": "重建（非原位）",
    "annotate": "批注/高亮叠加（非原位）",
    "refuse": "拒绝并说明",
}


class PdfPolicyError(ValueError):
    """Raised when a PDF operation cannot be honoured (never a fake success)."""


# --------------------------------------------------------------------------- #
# Decision types                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class PdfPolicyDecision:
    """The honest outcome of a PDF editing request (pure data)."""

    operation: str
    status: str
    inplace: bool
    strategy: str | None
    label: str
    alternatives: list[str] = field(default_factory=list)
    implemented: dict[str, bool] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "status": self.status,
            "inplace": self.inplace,
            "strategy": self.strategy,
            "label": self.label,
            "alternatives": list(self.alternatives),
            "implemented": dict(self.implemented),
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Classification (deterministic keyword rules — no model needed)               #
# --------------------------------------------------------------------------- #
_INPLACE_KEYWORDS: tuple[str, ...] = (
    "原位",
    "就地",
    "in-place",
    "inplace",
    "直接编辑",
    "直接修改",
    "改写原文",
    "修改原文件",
    "编辑原文件",
    "覆盖原文件",
)
_REBUILD_KEYWORDS: tuple[str, ...] = (
    "重建",
    "重新生成",
    "重做",
    "重新做",
    "另存为",
    "生成新的",
    "新建pdf",
    "新建 pdf",
)
_ANNOTATE_KEYWORDS: tuple[str, ...] = (
    "批注",
    "注释",
    "标注",
    "高亮",
    "评论",
    "高亮叠加",
    "画圈",
)


def classify_pdf_request(instruction: str) -> str:
    """Classify a natural-language PDF request (deterministic; ``inplace`` first).

    Returns one of ``"inplace"`` / ``"rebuild"`` / ``"annotate"`` / ``"unknown"``.
    ``inplace`` wins whenever the request explicitly asks for it, because the
    honest answer to an in-place request is a refusal regardless of any other
    verb.
    """
    text = str(instruction or "")
    if any(keyword in text for keyword in _INPLACE_KEYWORDS):
        return "inplace"
    if any(keyword in text for keyword in _REBUILD_KEYWORDS):
        return "rebuild"
    if any(keyword in text for keyword in _ANNOTATE_KEYWORDS):
        return "annotate"
    return "unknown"


def _implemented_strategies() -> dict[str, bool]:
    """Which strategies are really implemented (honest, not aspirational)."""
    return {"rebuild": True, "annotate": True, "refuse": True}


def evaluate_pdf_request(instruction: str) -> PdfPolicyDecision:
    """The pure policy decision for a PDF request (touches no bytes).

    An in-place request is answered :data:`PDF_INPLACE_UNSUPPORTED` with the three
    alternatives — never a claimed success (red line 17).
    """
    kind = classify_pdf_request(instruction)
    implemented = _implemented_strategies()
    alternatives = list(PDF_STRATEGIES)
    if kind == "inplace":
        return PdfPolicyDecision(
            operation="inplace_edit",
            status=PDF_INPLACE_UNSUPPORTED,
            inplace=False,
            strategy=None,
            label="不支持原位编辑",
            alternatives=alternatives,
            implemented=implemented,
            reason=(
                "PDF 不承诺原位编辑（无版式引擎，无法安全改写原文字节）；"
                "请选择替代方案：①提取→重建新文档（重建，非原位）"
                "②批注/高亮叠加（非原位）③拒绝并说明——绝不假装原位编辑成功"
            ),
        )
    if kind == "rebuild":
        return PdfPolicyDecision(
            operation="rebuild",
            status="rebuild",
            inplace=False,
            strategy="rebuild",
            label=_STRATEGY_LABELS["rebuild"],
            alternatives=alternatives,
            implemented=implemented,
            reason="提取原文后渲染**新**文档；原文件不变（重建，非原位）",
        )
    if kind == "annotate":
        return PdfPolicyDecision(
            operation="annotate",
            status="annotate",
            inplace=False,
            strategy="annotate",
            label=_STRATEGY_LABELS["annotate"],
            alternatives=alternatives,
            implemented=implemented,
            reason="在副本上叠加批注/高亮图层；页面正文不被改写（非原位）",
        )
    return PdfPolicyDecision(
        operation="unknown",
        status="refuse",
        inplace=False,
        strategy="refuse",
        label=_STRATEGY_LABELS["refuse"],
        alternatives=alternatives,
        implemented=implemented,
        reason=(
            "无法从请求识别安全的 PDF 操作；已拒绝并列出替代方案"
            "（不猜测意图、不伪造结果）"
        ),
    )


# --------------------------------------------------------------------------- #
# Strategy ① — rebuild a NEW document (never in place)                         #
# --------------------------------------------------------------------------- #
def rebuild_from_pdf(source: bytes, *, page_size: str = "A4") -> tuple[bytes, dict[str, Any]]:
    """Extract ``source``'s text and render a **new** PDF (重建，非原位).

    The original bytes are never modified; the returned bytes are a fresh
    document. The second return element is the honest provenance record (always
    ``inplace=False``).

    Raises:
        PdfPolicyError: the source is unreadable, or the PDF extras
            (``pypdf`` / ``fpdf2``) are absent — reason carried verbatim.
    """
    from forgeflow.documents.pdf_generate import PdfGenerateSpec, PdfGenerationError, apply_spec
    from forgeflow.documents.pdf_inspect import PdfInspectionError, inspect_pdf

    if not isinstance(source, (bytes, bytearray)) or not source:
        raise PdfPolicyError("PDF 字节为空，无法重建")
    try:
        facts = inspect_pdf(bytes(source))
    except PdfInspectionError as exc:
        raise PdfPolicyError(f"无法读取源 PDF：{exc}") from exc

    lines = [line.strip() for line in (facts.text_excerpt or "").splitlines() if line.strip()]
    title = lines[0] if lines else "重建文档"
    body = lines[1:] if len(lines) > 1 else []
    spec = PdfGenerateSpec(title=title, paragraphs=body, page_size=page_size)
    try:
        new_bytes = apply_spec(spec)
    except PdfGenerationError as exc:
        raise PdfPolicyError(f"无法重建 PDF：{exc}") from exc

    provenance = {
        "inplace": False,
        "strategy": "rebuild",
        "label": _STRATEGY_LABELS["rebuild"],
        "source_pages": facts.page_count,
        "source_chars": facts.chars,
        "note": "由源 PDF 文本重新渲染的新文档；原文件未被修改（非原位）",
    }
    return new_bytes, provenance


# --------------------------------------------------------------------------- #
# Strategy ② — annotate / highlight overlay (a copy, never in place)           #
# --------------------------------------------------------------------------- #
def _overlay_pdf(width: float, height: float, note: str) -> bytes:
    """Render a transparent-sized overlay page carrying ``note`` (fpdf2)."""
    try:
        from fpdf import FPDF
    except ImportError as exc:  # pragma: no cover — extra probed by caller
        raise PdfPolicyError(f"PDF 生成支持不可用：{exc}") from exc

    width = max(float(width), 10.0)
    height = max(float(height), 10.0)
    pdf = FPDF(orientation="P", unit="pt", format=(width, height))
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    # A safe CJK-capable font when present; otherwise degrade to a core font.
    from forgeflow.documents.pdf_generate import _resolve_font_path

    font_path = _resolve_font_path()
    unicode_ok = False
    if font_path:
        try:
            pdf.add_font("cjk", "", font_path)
            pdf.set_font("cjk", size=12)
            unicode_ok = True
        except Exception:  # noqa: BLE001 — unusable font degrades to a core font
            unicode_ok = False
    if not unicode_ok:
        pdf.set_font("helvetica", size=12)
    text = note if unicode_ok else note.encode("latin-1", errors="replace").decode("latin-1")
    pdf.set_xy(18, 18)
    pdf.set_text_color(200, 30, 30)
    pdf.multi_cell(width - 36, 16, text)
    return bytes(pdf.output())


def annotate_overlay(source: bytes, note: str, *, page_index: int = 0) -> bytes:
    """Overlay ``note`` as an annotation layer on a **copy** of ``source``.

    The page content stream of the original is never rewritten — the overlay is a
    separate layer merged onto one page of a fresh copy (非原位).

    Args:
        source: the original PDF bytes (never mutated).
        note: the annotation / highlight text to overlay.
        page_index: 0-based target page (default the first page).

    Raises:
        PdfPolicyError: the source is unreadable, the page index is out of range,
            the note is empty, or the ``pypdf`` / ``fpdf2`` extras are absent.
    """
    if not str(note or "").strip():
        raise PdfPolicyError("批注内容为空，拒绝叠加（不伪造批注）")
    if not isinstance(source, (bytes, bytearray)) or not source:
        raise PdfPolicyError("PDF 字节为空，无法批注")
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise PdfPolicyError(f"pdf 支持不可用：{exc}") from exc

    try:
        reader = PdfReader(io.BytesIO(bytes(source)))
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001 — an unreadable PDF is an honest failure
        raise PdfPolicyError(f"无法解析 PDF：{exc}") from exc
    if not pages:
        raise PdfPolicyError("PDF 没有任何页面，无法批注")
    if page_index < 0 or page_index >= len(pages):
        raise PdfPolicyError(f"页码越界：{page_index}（共 {len(pages)} 页）")

    box = pages[page_index].mediabox
    overlay_bytes = _overlay_pdf(float(box.width), float(box.height), str(note))
    overlay_page = PdfReader(io.BytesIO(overlay_bytes)).pages[0]

    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)
    writer.pages[page_index].merge_page(overlay_page, over=True)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
