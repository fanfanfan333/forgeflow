"""INC45 §1.2 — the PDF *generation* plane (a brand-new PDF, fpdf2).

Same three-layer split as the other file planes (design §8):

  * :func:`resolve_spec` — the **LLM layer**. Natural language → a
    :class:`PdfGenerateSpec`, and it *never* writes bytes. Untrusted output: an
    unparseable reply (or a missing model) yields ``None`` rather than a guess.
  * :func:`apply_spec` — the **Tool layer**. The only writer. It renders a simple
    one-or-more-page text PDF via ``fpdf2`` and returns the bytes. A missing
    ``fpdf2`` extra raises :class:`PdfGenerationError` (verbatim) — it never
    fabricates an empty PDF.

Scope (design §1.2, D6): generate a **new** PDF from an instruction. No in-place
byte editing, no OCR, no page-level re-flow — those are explicitly out of scope.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

__all__ = ["PdfGenerateSpec", "PdfGenerationError", "apply_spec", "resolve_spec"]

#: Page formats fpdf2 accepts directly (kept small and explicit so a bad request
#: is refused rather than silently defaulted).
_PAGE_FORMATS: tuple[str, ...] = ("A4", "A3", "A5", "Letter", "Legal")

#: Candidate Unicode (CJK-capable) TrueType fonts, searched in order. fpdf2's
#: built-in core fonts are Latin-1 only, so a CJK body needs a real TTF; the
#: search is best-effort and never fatal (a missing font degrades to Latin-1).
_CJK_FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\simkai.ttf",
    r"/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    r"/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


class PdfGenerationError(ValueError):
    """Raised when a PDF cannot be generated (never yields a fake empty PDF)."""


@dataclass
class PdfGenerateSpec:
    """The content spec for a generated PDF (the LLM layer's output type)."""

    title: str = ""
    paragraphs: list[str] = field(default_factory=list)
    page_size: str = "A4"

    @classmethod
    def from_dict(cls, data: Any) -> "PdfGenerateSpec":
        """Build a spec from a mapping; tolerant of missing / extra keys."""
        if isinstance(data, PdfGenerateSpec):
            return data
        if not isinstance(data, dict):
            raise PdfGenerationError(f"PDF 规格必须是对象：{data!r}")
        raw_paragraphs = data.get("paragraphs")
        paragraphs: list[str] = []
        if isinstance(raw_paragraphs, (list, tuple)):
            paragraphs = [str(item) for item in raw_paragraphs]
        elif raw_paragraphs is not None:
            raise PdfGenerationError(f"paragraphs 必须是数组：{raw_paragraphs!r}")
        return cls(
            title=str(data.get("title") or ""),
            paragraphs=paragraphs,
            page_size=str(data.get("page_size") or "A4"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "paragraphs": list(self.paragraphs),
            "page_size": self.page_size,
        }


def _resolve_font_path() -> str | None:
    """First existing CJK-capable TTF, or ``None`` (never fatal)."""
    for path in _CJK_FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _page_format(page_size: str) -> str:
    """Validate / normalise a page-size token, refusing anything unknown."""
    token = str(page_size or "A4").strip()
    for fmt in _PAGE_FORMATS:
        if token.lower() == fmt.lower():
            return fmt
    raise PdfGenerationError(f"不支持的页面尺寸：{page_size!r}（仅支持 {', '.join(_PAGE_FORMATS)}）")


def _safe_text(text: str, *, unicode_ok: bool) -> str:
    """Make ``text`` renderable by the selected font.

    A Unicode font renders anything; a Latin-1 core font degrades per-character
    (``errors="replace"``) rather than raising — so a PDF is still produced, just
    with replacement glyphs, which is an honest visible degradation.
    """
    if unicode_ok:
        return text
    return text.encode("latin-1", errors="replace").decode("latin-1")


def apply_spec(spec: PdfGenerateSpec | Any) -> bytes:
    """Render ``spec`` into a new PDF and return its bytes.

    This is the layered core: the model proposed the content spec, *this*
    function writes the PDF. Every paragraph is emitted with ``multi_cell`` (so
    long text wraps); a page break is inserted automatically when content
    overflows.

    Raises:
        PdfGenerationError: the ``fpdf2`` extra is absent (reason verbatim), the
            spec is malformed, or the page size is unsupported.
    """
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise PdfGenerationError(f"pdf 生成支持不可用：{exc}") from exc

    options = spec if isinstance(spec, PdfGenerateSpec) else PdfGenerateSpec.from_dict(spec)
    fmt = _page_format(options.page_size)

    try:
        pdf = FPDF(orientation="P", unit="mm", format=fmt)
    except Exception as exc:  # noqa: BLE001 — a bad format is an honest refusal
        raise PdfGenerationError(f"无法初始化 PDF：{exc}") from exc

    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    font_path = _resolve_font_path()
    if font_path:
        try:
            pdf.add_font("cjk", "", font_path)
            pdf.set_font("cjk", size=11)
            unicode_ok = True
        except Exception:  # noqa: BLE001 — an unusable font degrades to core fonts
            unicode_ok = False
    else:
        unicode_ok = False
    if not unicode_ok:
        pdf.set_font("helvetica", size=11)

    if options.title:
        pdf.set_font_size(16)
        pdf.multi_cell(0, 10, _safe_text(options.title, unicode_ok=unicode_ok))
        pdf.ln(2)
        pdf.set_font_size(11)
    for paragraph in options.paragraphs:
        pdf.multi_cell(0, 8, _safe_text(paragraph, unicode_ok=unicode_ok))
        pdf.ln(1)

    out = pdf.output()
    return bytes(out)


# --------------------------------------------------------------------------- #
# LLM layer (intent → spec). Never writes bytes.                               #
# --------------------------------------------------------------------------- #
_SPEC_SYSTEM = (
    "你是企业多智能体平台（ForgeFlow）的 PDF 生成意图解析器。"
    "你的唯一职责是把自然语言需求解析成结构化的 PDF 内容规格，"
    "绝不输出内容之外的任何说明文字。"
)

_SPEC_INSTRUCTION = (
    "把下面的自然语言需求解析为一个 PDF 内容规格。\n"
    "只输出一个 JSON 对象，形如：\n"
    '{{"title":"标题","paragraphs":["第一段","第二段"],"page_size":"A4"}}\n'
    "page_size 仅支持 A4/A3/A5/Letter/Legal，缺省 A4。\n"
    "需求：{intent}"
)


def _extract_json(text: str | None) -> dict[str, Any] | None:
    """Lenient first-JSON-object parse (tolerates ``` fences and prose)."""
    if not text:
        return None
    body = str(text).strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body[:4].lower() == "json":
            body = body[4:]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(body[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def resolve_spec(intent: str) -> PdfGenerateSpec | None:
    """Parse a natural-language intent into a PDF content spec via the model.

    **This function never writes bytes** — it is the LLM layer. It returns
    ``None`` (an honest "no usable intent") when the model is unavailable or the
    reply cannot be parsed; it never fabricates a spec.
    """
    if not str(intent or "").strip():
        return None
    try:
        from forgeflow.models.provider import get_model

        model = get_model()
    except Exception:  # noqa: BLE001 — a provider hiccup is an honest "unavailable"
        return None
    if model is None:
        return None

    try:
        if hasattr(model, "reasoning"):
            model = model.model_copy(update={"reasoning": False})
    except Exception:  # noqa: BLE001 — an unsupported flag must not break the call
        pass

    prompt = _SPEC_INSTRUCTION.format(intent=str(intent)[:1200])
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        message = await model.ainvoke(
            [SystemMessage(content=_SPEC_SYSTEM), HumanMessage(content=prompt)]
        )
    except Exception:  # noqa: BLE001 — any call failure degrades to None
        return None

    payload = _extract_json(getattr(message, "content", ""))
    if not isinstance(payload, dict):
        return None
    try:
        spec = PdfGenerateSpec.from_dict(payload)
    except PdfGenerationError:
        return None
    if not spec.title and not spec.paragraphs:
        return None
    return spec
