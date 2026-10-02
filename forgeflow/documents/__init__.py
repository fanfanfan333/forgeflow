"""INC43 S4 — the DOCX document domain (一期仅 DOCX).

The package is deliberately layered (design §1.1 / §1.3):

  * :mod:`~forgeflow.documents.docx_inspect` — read the real structure;
  * :mod:`~forgeflow.documents.docx_edit` — the LLM intent layer
    (:func:`resolve_intent`, never writes) plus the Tool layer
    (:func:`apply_edits`, the only writer) and the pure :func:`compute_diff`;
  * :mod:`~forgeflow.documents.validation` — honest, tri-state verification;
  * :mod:`~forgeflow.documents.store` — content-addressed blob storage for the
    deliverable bytes (kept out of the bounded run payload).

XLSX / PPTX / PDF are intentionally **not** implemented in this iteration (no
entry point, no fake request).
"""

from __future__ import annotations

from forgeflow.documents.docx_edit import (
    SUPPORTED_OPS,
    DiffReport,
    EditOp,
    UnknownEditOpError,
    apply_edits,
    compute_diff,
    numbers_removable_by_edits,
    resolve_intent,
)
from forgeflow.documents.docx_inspect import (
    DocStructure,
    DocxInspectionError,
    document_numbers,
    heading_level,
    inspect_docx,
    open_docx,
)
from forgeflow.documents.store import DocArtifactStore, DocStorePathError
from forgeflow.documents.validation import VerifyReport, verify_docx

__all__ = [
    "DocStructure",
    "DocxInspectionError",
    "inspect_docx",
    "open_docx",
    "heading_level",
    "document_numbers",
    "SUPPORTED_OPS",
    "EditOp",
    "UnknownEditOpError",
    "apply_edits",
    "compute_diff",
    "DiffReport",
    "numbers_removable_by_edits",
    "resolve_intent",
    "VerifyReport",
    "verify_docx",
    "DocArtifactStore",
    "DocStorePathError",
]
