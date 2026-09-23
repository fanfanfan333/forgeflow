"""Multimodal attachment ingestion into the task context — INC2-24 (C4).

docs/sop/05-ARCHITECTURE-INC2.md §2.16. A ``POST /tasks`` body may carry a list
of attachments (PDF / image); this module pre-processes them and folds the
extracted text into the task **intent** so the runtime runs against the
attachment content, not just the raw instructions.

Hard constraints:
  * **Optional dependencies degrade, never raise.** pypdf / a vision model are
    optional extras; when they are missing (or the machine is memory-starved)
    the attachment is reported as *ignored* / *metadata-only* instead of
    blowing up the request.
  * **Size ceiling** comes from ``Settings.multimodal_max_bytes`` (5 MB). An
    over-limit attachment IS a client error and raises ``AttachmentTooLargeError``
    (the router maps it to HTTP 413).
  * The vision description is only attempted when a vision-capable model is
    configured (``qwen2.5vl`` style), otherwise image handling is metadata-only
    — so the offline / mock profile never pays for a real (≈18.6s) LLM call.

Reuses the existing ``forgeflow.multimodal.pdf`` / ``forgeflow.multimodal.images``
modules unchanged.
"""

from __future__ import annotations

import base64
import binascii
import logging
import mimetypes
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

SUPPORTED_KINDS = ("pdf", "image")
_VISION_HINTS = ("vl", "vision", "llava", "qwen2-vl", "qwen2.5-vl")


class AttachmentInput(BaseModel):
    """One attachment on a ``POST /tasks`` body (base64-inlined)."""

    kind: str = Field(default="pdf", description="pdf | image")
    name: str = Field(default="", description="Original file name (for MIME + label)")
    data_base64: str = Field(default="", description="Base64-encoded attachment bytes")


class AttachmentTooLargeError(ValueError):
    """Raised when an attachment exceeds ``Settings.multimodal_max_bytes``."""


@dataclass
class AttachmentResult:
    """Outcome of processing one attachment."""

    kind: str
    name: str
    status: str            # "injected" | "metadata_only" | "ignored"
    detail: str = ""
    injected_text: str = ""

    @property
    def injected(self) -> bool:
        return self.status in ("injected", "metadata_only")


@dataclass
class PreparedTask:
    """The attachment-enriched task intent + context extras."""

    intent: str
    context: dict[str, Any] = field(default_factory=dict)
    results: list[AttachmentResult] = field(default_factory=list)

    @property
    def ignored(self) -> list[str]:
        """Names of attachments that could not be used (degraded gracefully)."""
        return [r.name for r in self.results if r.status == "ignored"]


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #

def _decode(data_base64: str) -> bytes:
    """Decode a base64 payload, raising ValueError on malformed input."""
    try:
        return base64.b64decode(data_base64 or "", validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 attachment payload: {exc}") from exc


def _guess_image_mime(name: str) -> str | None:
    mime, _ = mimetypes.guess_type(name or "")
    return mime


def vision_available() -> bool:
    """True when a vision-capable model looks reachable from the settings.

    Deliberately conservative: the offline (mock) profile returns False, so the
    acceptance tests and the offline demo never trigger a real vision call.
    """
    settings = get_settings()
    provider = (settings.llm_provider or "").lower()
    if provider in ("openai", "anthropic"):
        return True
    if provider == "ollama":
        candidates = f"{settings.ollama_model} {settings.ollama_model_strong}".lower()
        return any(hint in candidates for hint in _VISION_HINTS)
    return False


def _ingest_pdf(data: bytes, name: str) -> AttachmentResult:
    """Extract PDF text; degrade to *ignored* when pypdf is unavailable."""
    from forgeflow.multimodal.pdf import extract_pdf_text

    try:
        document = extract_pdf_text(data)
    except ImportError as exc:  # optional dependency missing
        logger.warning("PDF support unavailable — attachment '%s' ignored: %s", name, exc)
        return AttachmentResult("pdf", name, "ignored", "pdf support unavailable")
    except ValueError as exc:
        return AttachmentResult("pdf", name, "ignored", f"unparseable pdf: {exc}")
    except Exception as exc:  # noqa: BLE001 — never let parsing break the request
        logger.warning("PDF parse failed for '%s': %s", name, exc)
        return AttachmentResult("pdf", name, "ignored", f"pdf parse failed: {exc}")

    text = (document.text or "").strip()
    if not text:
        return AttachmentResult("pdf", name, "ignored", "no extractable text")
    return AttachmentResult(
        "pdf", name, "injected", f"{document.page_count} page(s)", injected_text=text
    )


async def _ingest_image(data: bytes, name: str, *, describe: bool) -> AttachmentResult:
    """Encode an image for the vision path; degrade to metadata-only otherwise."""
    from forgeflow.multimodal.images import describe_image, image_to_data_url

    mime = _guess_image_mime(name)
    try:
        # Encoding also validates the MIME allowlist without an LLM call.
        image_to_data_url(data, mime_type=mime)
    except ValueError as exc:
        return AttachmentResult("image", name, "ignored", f"unsupported image: {exc}")

    if describe and vision_available():
        try:
            description = await describe_image(data, model=None)
            text = (description.description or "").strip()
            if text:
                return AttachmentResult("image", name, "injected", "vision description", text)
        except Exception as exc:  # noqa: BLE001 — vision is best-effort only
            logger.warning("vision description failed for '%s': %s", name, exc)

    marker = f"[图片附件 {name}（{len(data)} bytes）]"
    return AttachmentResult("image", name, "metadata_only", "vision model unavailable", marker)


# --------------------------------------------------------------------------- #
# public API                                                                   #
# --------------------------------------------------------------------------- #

async def prepare_attachments(
    attachments: list[Any] | None,
    *,
    intent: str = "",
    max_bytes: int | None = None,
    describe_images: bool | None = None,
) -> PreparedTask:
    """Pre-process attachments and inject their text into the task intent.

    Args:
        attachments: ``AttachmentInput`` objects (or equivalent dicts).
        intent: the raw task intent; extracted text is appended to it.
        max_bytes: size ceiling override (defaults to ``multimodal_max_bytes``).
        describe_images: force/skip the vision path. ``None`` (default) enables
            it only when a vision-capable model is configured.

    Returns:
        A ``PreparedTask`` with the enriched intent, context extras
        (``{"attachments": [...]}``) and the per-attachment results.

    Raises:
        AttachmentTooLargeError: an attachment exceeded the byte ceiling.
        ValueError: a payload was not valid base64.
    """
    settings = get_settings()
    limit = max_bytes if max_bytes is not None else int(settings.multimodal_max_bytes)
    should_describe = describe_images if describe_images is not None else vision_available()

    blocks: list[str] = []
    context_attachments: list[dict[str, Any]] = []
    results: list[AttachmentResult] = []

    for raw in attachments or []:
        item = raw if isinstance(raw, AttachmentInput) else AttachmentInput(**(raw or {}))
        name = item.name or f"attachment-{len(results) + 1}"
        kind = (item.kind or "").lower()

        if kind not in SUPPORTED_KINDS:
            result = AttachmentResult(kind or "unknown", name, "ignored", f"unsupported kind: {kind}")
            results.append(result)
            context_attachments.append(_context_entry(result))
            continue

        try:
            data = _decode(item.data_base64)
        except ValueError as exc:
            result = AttachmentResult(kind, name, "ignored", str(exc))
            results.append(result)
            context_attachments.append(_context_entry(result))
            continue

        if len(data) > limit:
            raise AttachmentTooLargeError(
                f"attachment '{name}' is {len(data)} bytes; the limit is {limit}"
            )

        if kind == "pdf":
            result = _ingest_pdf(data, name)
        else:
            result = await _ingest_image(data, name, describe=should_describe)

        results.append(result)
        context_attachments.append(_context_entry(result))
        if result.injected and result.injected_text:
            blocks.append(f"[附件 {name}]\n{result.injected_text}")

    enriched = intent
    if blocks:
        joined = "\n\n".join(blocks)
        enriched = f"{intent}\n\n{joined}" if intent else joined

    context: dict[str, Any] = {}
    if context_attachments:
        context["attachments"] = context_attachments

    return PreparedTask(intent=enriched, context=context, results=results)


def _context_entry(result: AttachmentResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "kind": result.kind,
        "status": result.status,
        "detail": result.detail,
    }
