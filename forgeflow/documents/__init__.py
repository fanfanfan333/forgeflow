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

XLSX / PDF now have their own entry points (INC45): :mod:`~forgeflow.documents.sheet_inspect`
/ ``sheet_edit`` (the XLSX plane, ``openpyxl``) and :mod:`~forgeflow.documents.pdf_inspect`
/ ``pdf_generate`` (the PDF plane, ``pypdf`` / ``fpdf2``). They mirror the DOCX/PPTX
planes' three-layer split and are additive — no existing entry point changes.
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
    compute_numbering_labels,
    document_numbers,
    heading_level,
    inspect_docx,
    open_docx,
    paragraph_numbering_ids,
    text_marker,
)
# INC46 T20 — document intent plane: deterministic locate + invariant extraction +
# structured intent (additive; the LLM layer ``resolve_intent`` above is unchanged).
from forgeflow.documents.intent import (
    EditIntent,
    NotResolved,
    resolve_intent_document,
)
from forgeflow.documents.invariants import (
    Invariant,
    InvariantSet,
    extract_invariants,
)
from forgeflow.documents.locator import (
    CONFIDENCE_THRESHOLD,
    LocatorCandidate,
    LocatorResult,
    locate,
    parse_selector,
)
from forgeflow.documents.pptx_edit import (
    SUPPORTED_OPS as PPTX_SUPPORTED_OPS,
    UNSUPPORTED_CAPABILITIES as PPTX_UNSUPPORTED_CAPABILITIES,
    EditOp as PptxEditOp,
    apply_edits as apply_pptx_edits,
    compute_diff as compute_pptx_diff,
    detect_unsupported_operation as detect_pptx_unsupported,
    resolve_intent as resolve_pptx_intent,
    unsupported_result as pptx_unsupported_result,
)
from forgeflow.documents.pptx_inspect import (
    PptxInspectionError,
    PptxStructure,
    inspect_pptx,
    open_pptx,
)
from forgeflow.documents.pdf_generate import (
    PdfGenerateSpec,
    PdfGenerationError,
    apply_spec as apply_pdf_spec,
    resolve_spec as resolve_pdf_spec,
)
from forgeflow.documents.pdf_inspect import (
    PdfFacts,
    PdfInspectionError,
    inspect_pdf,
)
from forgeflow.documents.sheet_edit import (
    SUPPORTED_OPS as SHEET_SUPPORTED_OPS,
    SheetEditOp,
    apply_edits as apply_sheet_edits,
    compute_diff as compute_sheet_diff,
    numbers_removable_by_edits as sheet_numbers_removable_by_edits,
    resolve_intent as resolve_sheet_intent,
)
from forgeflow.documents.sheet_inspect import (
    SheetInspectionError,
    SheetStructure,
    inspect_sheet,
    open_workbook,
    sheet_numbers,
)
# INC46 T27 — guarded XLSX plane (formula protection) + PDF policy + capability
# matrix. All additive: the writers stay ``sheet_edit`` / ``pdf_generate``.
from forgeflow.documents.xlsx_edit import (
    FormulaProtectionError,
    PRESERVED_FEATURES as XLSX_PRESERVED_FEATURES,
    PROTECTED_FEATURES as XLSX_PROTECTED_FEATURES,
    apply_edits as apply_xlsx_edits,
    formula_cells,
    is_formula,
)
from forgeflow.documents.pdf_policy import (
    PDF_INPLACE_UNSUPPORTED,
    PDF_STRATEGIES,
    PdfPolicyDecision,
    PdfPolicyError,
    annotate_overlay,
    classify_pdf_request,
    evaluate_pdf_request,
    rebuild_from_pdf,
)
from forgeflow.documents.capabilities import (
    CAPABILITIES,
    FORMATS,
    FORMAT_DOCX,
    FORMAT_OTHER,
    FORMAT_PDF,
    FORMAT_PPTX,
    FORMAT_XLSX,
    FormatCapability,
    capability_matrix,
    format_capability,
    render_capability_text,
    sniff_format,
    supports as format_supports,
    unsupported_reason,
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
    verify_pdf,
    verify_pptx,
    verify_sheet,
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
    # INC46 T20 — deterministic intent plane (locate + invariants + EditIntent)
    "EditIntent",
    "NotResolved",
    "resolve_intent_document",
    "Invariant",
    "InvariantSet",
    "extract_invariants",
    "CONFIDENCE_THRESHOLD",
    "LocatorCandidate",
    "LocatorResult",
    "locate",
    "parse_selector",
    "text_marker",
    "paragraph_numbering_ids",
    "compute_numbering_labels",
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
    # INC45 §1.1 — XLSX plane (openpyxl)
    "SheetStructure",
    "SheetInspectionError",
    "open_workbook",
    "inspect_sheet",
    "sheet_numbers",
    "SHEET_SUPPORTED_OPS",
    "SheetEditOp",
    "apply_sheet_edits",
    "compute_sheet_diff",
    "sheet_numbers_removable_by_edits",
    "resolve_sheet_intent",
    # INC45 §1.2 — PDF plane (pypdf / fpdf2)
    "PdfFacts",
    "PdfInspectionError",
    "inspect_pdf",
    "PdfGenerateSpec",
    "PdfGenerationError",
    "apply_pdf_spec",
    "resolve_pdf_spec",
    # verification + store
    "VerifyReport",
    "verify_docx",
    "verify_pptx",
    "verify_textfile",
    "verify_sheet",
    "verify_pdf",
    "DocArtifactStore",
    "DocStorePathError",
    # INC46 T27 — PPTX honest boundary (notes op + unsupported declaration)
    "PPTX_UNSUPPORTED_CAPABILITIES",
    "detect_pptx_unsupported",
    "pptx_unsupported_result",
    # INC46 T27 — guarded XLSX plane (formula protection)
    "FormulaProtectionError",
    "XLSX_PROTECTED_FEATURES",
    "XLSX_PRESERVED_FEATURES",
    "apply_xlsx_edits",
    "formula_cells",
    "is_formula",
    # INC46 T27 — PDF policy (never in-place)
    "PDF_STRATEGIES",
    "PDF_INPLACE_UNSUPPORTED",
    "PdfPolicyError",
    "PdfPolicyDecision",
    "classify_pdf_request",
    "evaluate_pdf_request",
    "rebuild_from_pdf",
    "annotate_overlay",
    # INC46 T27 — capability matrix (format × capability × honest degradation)
    "FORMATS",
    "FORMAT_DOCX",
    "FORMAT_PPTX",
    "FORMAT_XLSX",
    "FORMAT_PDF",
    "FORMAT_OTHER",
    "FormatCapability",
    "CAPABILITIES",
    "sniff_format",
    "capability_matrix",
    "format_capability",
    "format_supports",
    "unsupported_reason",
    "render_capability_text",
]
