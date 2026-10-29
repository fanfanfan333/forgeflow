"""Derive cross-run / cross-project result passing from a real source run.

Before this module, ``handoff_results`` could only reach a code task if the
caller hand-crafted every item in exactly the ``validate_handoff_results``
shape (``kind`` / ``source`` / ``content``, 1-8 items, per-item and total
byte caps). There was no server-side way to say "take the result of run X
in another session / project and pass it to this task", so in practice
results were either re-typed (lossy, unaudited) or dropped entirely --
most visibly when the target was a code task on a different transport.

This module is the explicit, audited alternative, following the pattern
used by LangGraph handoffs and OpenHands delegated conversations: a
handoff is an **explicit payload with provenance**, not model-selected
arguments and not an implicit state leak. Only the fields the source run
really recorded are used; nothing is summarised, invented or re-derived
from a model. The payload is then re-validated by
:func:`forgeflow.runtime.handoff.validate_handoff_results`, so the
downstream Docker-only isolation gate for untrusted code-plane evidence
(see ``runtime/tool_handlers.py::code_execute``) still applies unchanged.

Ownership is fail-closed: a source run from another tenant, or from
another user, is never returned -- the caller gets a ``ValueError``,
never a partial payload and never a hint that distinguishes "missing"
from "not yours" beyond that generic message.
"""

from __future__ import annotations

from typing import Any

from forgeflow.runtime.handoff import validate_handoff_results

_MAX_ITEMS = 8
_MAX_CONTENT_CHARS = 12000
_MAX_TOTAL_BYTES = 32768
_MAX_SOURCE_CHARS = 512


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _source(run_id: str, label: str) -> str:
    source = f"{run_id}:{label}" if label else run_id
    return source[:_MAX_SOURCE_CHARS].strip() or run_id


_TEXT_FORMATS = {"text", "txt", "markdown", "md", "json", "csv", "yaml", "log", "diff"}


def _artifact_text(art: dict[str, Any]) -> str:
    """Resolve one artifact to text, including ``content_ref`` deliverables.

    Real deliverables deliberately carry ``content=""`` — the bytes live
    behind ``content_ref`` in the document store (see
    ``runtime/artifacts.py::_deliverable_artifact``), because a base64 body
    would be truncated away by the payload ceiling. The original builder
    only read inline ``content``, so every real DOCX/XLSX/text deliverable
    was silently skipped and a successful source run derived to ``[]`` —
    the document handoff looked "not passed" in a real stack while unit
    tests with inline content passed. Extraction mirrors the preview
    endpoint (same formats, same sheet/page caps), bounded downstream by
    ``_append``. An unreadable or unsupported artifact is skipped, never
    replaced by a placeholder.
    """
    inline = _text(art.get("content"))
    if inline:
        return inline
    ref = _text(art.get("content_ref"))
    if not ref:
        return ""
    try:
        from forgeflow.documents.store import DocArtifactStore

        data = DocArtifactStore().get(ref)
    except Exception:  # noqa: BLE001 — one bad artifact must not kill the handoff
        return ""
    if not data:
        return ""
    fmt = _text(art.get("format")).lower()
    try:
        if fmt in _TEXT_FORMATS:
            from forgeflow.resources.summaries import _decode

            text, _ = _decode(data)
            return text
        if fmt == "docx":
            import io

            from docx import Document

            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs)
            text += "\n" + "\n".join(
                " | ".join(c.text for c in row.cells) for table in doc.tables for row in table.rows
            )
            return text
        if fmt in {"xlsx", "xlsm"}:
            import io

            from openpyxl import load_workbook

            book = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            try:
                return "\n\n".join(
                    f"[{sheet.title}]\n"
                    + "\n".join(
                        " | ".join(str(v) if v is not None else "" for v in row)
                        for row in sheet.iter_rows(max_row=200, max_col=30, values_only=True)
                    )
                    for sheet in book.worksheets[:10]
                )
            finally:
                book.close()
        if fmt == "pptx":
            import io

            from pptx import Presentation

            slides = Presentation(io.BytesIO(data)).slides
            return "\n\n".join(
                f"[{i + 1}]\n"
                + "\n".join(shape.text for shape in slide.shapes if shape.has_text_frame)
                for i, slide in enumerate(slides)
            )
        if fmt == "pdf":
            import io

            from pypdf import PdfReader

            return "\n\n".join(
                page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages[:30]
            )
    except Exception:  # noqa: BLE001 — see above
        return ""
    return ""


def _append(items: list[dict[str, Any]], *, kind: str, source: str, content: str) -> None:
    """Append one bounded item, honouring the validator's total byte cap."""
    text = content.strip()
    if not text or len(items) >= _MAX_ITEMS:
        return
    # Reserve room for the JSON envelope around this item; the final
    # validate() call is the authority, this is only a pre-trim so a long
    # item does not silently evict every later item.
    used = sum(len(str(i.get("content") or "").encode("utf-8")) for i in items)
    remaining = _MAX_TOTAL_BYTES - used
    if remaining <= 0:
        return
    encoded = text.encode("utf-8")
    truncated = False
    if len(encoded) > _MAX_CONTENT_CHARS:
        text = encoded[:_MAX_CONTENT_CHARS].decode("utf-8", errors="ignore").rstrip()
        encoded = text.encode("utf-8")
        truncated = True
    if len(encoded) > remaining:
        text = encoded[:remaining].decode("utf-8", errors="ignore").rstrip()
        truncated = True
    if not text:
        return
    item: dict[str, Any] = {"kind": kind, "source": source, "content": text}
    if truncated:
        item["truncated"] = True
    items.append(item)


def build_handoff_results(
    record: Any,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build validated ``handoff_results`` from one real source run.

    Returns ``[]`` (honest "nothing usable was recorded") when the source
    run carries no artifact content, final answer or codeplane diff -- the
    caller must then decide whether to proceed without a handoff, never
    to receive a fabricated placeholder item.
    """
    if record is None:
        raise ValueError("跨项目结果来源运行不存在或无权访问")
    if tenant_id is not None and getattr(record, "tenant_id", None) not in (None, tenant_id):
        raise ValueError("跨项目结果来源运行不存在或无权访问")
    if user_id is not None:
        actor = getattr(record, "actor_user_id", None)
        if actor not in (None, "", user_id):
            raise ValueError("跨项目结果来源运行不存在或无权访问")

    run_id = _text(getattr(record, "run_id", "")) or "unknown-run"
    items: list[dict[str, Any]] = []

    # 1. Delivered artifacts first: they are the run's real outputs.
    artifacts = [a for a in (getattr(record, "artifacts", None) or []) if isinstance(a, dict)]
    for art in artifacts:
        content = _artifact_text(art)
        if not content:
            continue
        title = _text(art.get("title") or art.get("kind") or "artifact")
        ref = _text(art.get("result_ref"))
        label = f"artifact:{title}" + (f"#{ref}" if ref else "")
        _append(items, kind="document", source=_source(run_id, label), content=content)
        if len(items) >= _MAX_ITEMS:
            break

    # 2. The run's final answer, as research evidence.
    llm = getattr(record, "llm", None) or {}
    answer = llm.get("final_answer") or {} if isinstance(llm, dict) else {}
    answer_text = _text(answer.get("text")) if isinstance(answer, dict) else ""
    if answer_text:
        _append(items, kind="research", source=_source(run_id, "final_answer"), content=answer_text)

    # 3. Codeplane diff, when the source run was a code task. It is data
    # (a document), never an instruction to the target agent.
    codeplane = getattr(record, "codeplane", None) or {}
    if isinstance(codeplane, dict):
        diff = _text(codeplane.get("diff"))
        if diff:
            _append(items, kind="document", source=_source(run_id, "codeplane-diff"), content=diff)

    if not items:
        return []
    return validate_handoff_results(items)
