"""INC43 S4 / INC44 — the document domain (DOCX + PPTX + text/code files).

The package is deliberately layered (design §1.1 / §1.3 / INC44 §1.2):

  * :mod:`~forgeflow.documents.textdiff` — the **one** pure LCS diff engine
    shared by every format (docx / pptx / textfile);
  * :mod:`~forgeflow.documents.docx_inspect` — read a real DOCX structure;
  * :mod:`~forgeflow.documents.docx_edit` — the DOCX LLM intent layer
    (:func:`resolve_intent`, never writes) + the Tool layer (:func:`apply_edits`,
    the only writer) + the pure :func:`compute_diff`;
  * :mod:`~forgeflow.documents.pptx_inspect` / :mod:`~forgeflow.documents.pptx_edit`
    — the PPTX mirror (``python-pptx``);
  * :mod:`~forgeflow.documents.textfile_inspect` / ``textfile_edit`` — the text /
    code plane (stdlib; EOL & encoding preserving);
  * :mod:`~forgeflow.documents.validation` — honest, tri-state verification;
  * :mod:`~forgeflow.documents.store` — content-addressed blob storage for the
    deliverable bytes (kept out of the bounded run payload).

XLSX / PDF are intentionally **not** implemented here (no entry point, no fake
request).
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
from forgeflow.documents.pptx_edit import (
    SUPPORTED_OPS as PPTX_SUPPORTED_OPS,
    EditOp as PptxEditOp,
    apply_edits as apply_pptx_edits,
    compute_diff as compute_pptx_diff,
    resolve_intent as resolve_pptx_intent,
)
from forgeflow.documents.pptx_inspect import (
    PptxInspectionError,
    PptxStructure,
    inspect_pptx,
    open_pptx,
)
from forgeflow.documents.store import DocArtifactStore, DocStorePathError
from forgeflow.documents.textdiff import (
    align_lines,
    diff_counts,
)
from forgeflow.documents.textfile_edit import (
    SUPPORTED_OPS as TEXTFILE_SUPPORTED_OPS,
    TextEditOp,
    apply_edits as apply_textfile_edits,
    compute_diff as compute_textfile_diff,
    resolve_intent as resolve_textfile_intent,
)
from forgeflow.documents.textfile_inspect import (
    TextFileStructure,
    TextInspectionError,
    inspect_textfile,
)
from forgeflow.documents.validation import (
    VerifyReport,
    verify_docx,
    verify_pptx,
    verify_textfile,
)

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
    # INC44 §2.2 — shared diff engine
    "align_lines",
    "diff_counts",
    # INC44 §1.2 — PPTX plane
    "PptxStructure",
    "PptxInspectionError",
    "inspect_pptx",
    "open_pptx",
    "PPTX_SUPPORTED_OPS",
    "PptxEditOp",
    "apply_pptx_edits",
    "compute_pptx_diff",
    "resolve_pptx_intent",
    # INC44 §1.3 — text / code plane
    "TextFileStructure",
    "TextInspectionError",
    "inspect_textfile",
    "TEXTFILE_SUPPORTED_OPS",
    "TextEditOp",
    "apply_textfile_edits",
    "compute_textfile_diff",
    "resolve_textfile_intent",
    # verification + store
    "VerifyReport",
    "verify_docx",
    "verify_pptx",
    "verify_textfile",
    "DocArtifactStore",
    "DocStorePathError",
]
