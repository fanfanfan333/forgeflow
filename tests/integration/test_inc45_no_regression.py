"""INC45 T05 — ``data-testid`` zero-regression + frontend single-fact-source pins.

Design §7 T05 criterion ③ / §9-6: *前端 ``data-testid`` 只增不改不删(``REMOVED == 0``)；
本迭代未改前端*. INC45 is a **zero-frontend-change** iteration: the new
``spreadsheet_xlsx`` / ``pdf_document`` artifacts flow through the existing verbatim
passthrough (``realRun.ts::deriveArtifacts`` → ``detail.artifacts ?? []``), so the
frozen Appendix-A ``data-testid`` contract must survive byte-for-byte.

Guards over the declared scan domain ``frontend/src/**/*.{ts,tsx}``:

  1. **``data-testid`` zero-regression** — every id in the frozen contract still
     appears as a *real* ``data-testid`` attribute. Red line: ``REMOVED == 0``.
     Adding ids is allowed, so no fixed total is asserted.

  2. **single fact source** — the frontend surfaces the **runtime**
     ``/resources/limits.supported_extensions`` list (INC26's "单一事实源" posture),
     and the backend's ``SUPPORTED_FILE_EXTENSIONS`` now includes ``.xlsx`` / ``.pdf``
     (so their uploads are accepted with **no** frontend edit).

  3. **unchanged ``content_kind`` contract** (F1 red line) — INC45 must NOT move
     ``.xlsx`` / ``.pdf`` off their existing kinds (``excel`` / ``pdf``) nor touch
     ``.pptx`` → ``document``.

Like the INC43 / INC44 pins, both scans strip comments **first** (string-aware) — a
bare grep would match ``data-testid`` inside ``//`` / ``{/* … */}`` comments and pass
vacuously.

Citation discipline: ``file.py::symbol`` anchors, never ``file:line``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
SCAN_SUFFIXES = (".ts", ".tsx")
SCAN_DOMAIN = "frontend/src/**/*.{ts,tsx}"

#: The frozen ``data-testid`` contract (PRD §4.5 Appendix A — "只增不改不删").
PRESERVED_TESTIDS = frozenset(
    {
        "home-second-screen",
        "hero-attach-files",
        "hero-attach-skills",
        "hero-attach-more",
        "hero-attach-panel",
        "conv-inline-history-row",
        "history-delete-btn",
        "conv-inline-panel",
        "conv-inline-header",
        "conv-inline-stop",
        "conv-inline-close",
        "conv-inline-user",
        "conv-inline-aborted",
        "conv-inline-open-full",
        "conv-inline-error",
        "conv-inline-followup",
        "conv-followup",
        "conv-user-turn",
        "conv-run-summary",
        "conv-inline-answer",
        "conv-artifacts",
        "conv-artifact-chip",
        "conv-artifact-open",
        "conv-artifact-download",
        "conv-exec-detail",
        "conv-exec-trace",
        "workspace-live-step",
        "artifact-panel",
        "artifact-empty",
        "artifact-card",
        "artifact-preview",
        "artifact-preview-unsupported",
        "artifact-download",
        "resource-add",
        "resource-dropzone",
        "resource-list",
        "resource-card",
        "resource-delete",
        "resource-preview",
        "resource-limits-note",
        "resource-upload-row",
        "resource-upload-status",
        "resource-kind-filter",
        "resource-empty-note",
        "resource-preview-truncated",
        "skill-create",
        "skill-create-submit",
        "skill-version-editor",
        "skill-version-save",
        "model-status",
        "theme-toggle",
    }
)

# Real ``data-testid`` value forms: ="...", ='...', ={"..."}, ={'...'}, ={`...`}.
_TESTID_ATTR = re.compile(
    r"""data-testid\s*=\s*(?:\{)?\s*(?:"([^"]*)"|'([^']*)'|`([^`]*)`)""",
    re.S,
)


def _strip_comments(src: str) -> str:
    """Remove JS/TS ``//`` and ``/* */`` comments, preserving string literals.

    A plain regex strip would corrupt ``"https://…"`` (the ``//`` inside it); a
    bare grep would count commented-out ids. This walks the source so quoted /
    template runs are copied verbatim and only real comments are dropped.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        two = src[i : i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if two == "/*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        ch = src[i]
        if ch in ("'", '"', "`"):
            out.append(ch)
            i += 1
            while i < n:
                if src[i] == "\\":
                    out.append(src[i])
                    i += 1
                    if i < n:
                        out.append(src[i])
                        i += 1
                    continue
                out.append(src[i])
                if src[i] == ch:
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _iter_sources(root: Path):
    for path in sorted(root.rglob("*")):
        if path.suffix in SCAN_SUFFIXES:
            yield path.relative_to(root).as_posix(), _strip_comments(
                path.read_text(encoding="utf-8")
            )


def _scan_testids(root: Path) -> set[str]:
    """Static ``data-testid`` ids in ``root`` (comment-stripped)."""
    found: set[str] = set()
    for _rel, code in _iter_sources(root):
        for m in _TESTID_ATTR.finditer(code):
            value = next((g for g in m.groups() if g is not None), "")
            if "${" not in value:  # dynamic templates carry no pinnable literal
                found.add(value)
    return found


# --------------------------------------------------------------------------- #
# 1. data-testid zero-regression (REMOVED == 0)                                #
# --------------------------------------------------------------------------- #
def test_appendix_a_testids_survive_data_testid_zero_regression():
    """REMOVED == 0: every frozen id is still a real attribute.

    Scan domain: ``frontend/src/**/*.{ts,tsx}``. Adding ids is allowed, so this
    asserts nothing about the total; only the frozen contract must survive.
    """
    found = _scan_testids(FRONTEND_SRC)
    removed = PRESERVED_TESTIDS - found
    assert removed == set(), (
        f"data-testid contract broken (REMOVED={len(removed)}): {sorted(removed)}; "
        f"scan={SCAN_DOMAIN} total={len(found)} added={len(found - PRESERVED_TESTIDS)}"
    )


def test_positive_control_scanner_flags_a_removed_testid(tmp_path):
    """Control: the pin *can* go red — rename an id ⟹ REMOVED."""
    comment_only = tmp_path / "comments"
    comment_only.mkdir()
    (comment_only / "x.tsx").write_text(
        '// data-testid="conv-followup"\n/* data-testid="conv-followup" */\n',
        encoding="utf-8",
    )
    assert _scan_testids(comment_only) == set(), "comments must never count"

    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "y.tsx").write_text(
        '<span data-testid="resource-list" />\n', encoding="utf-8"
    )
    assert "resource-list" in _scan_testids(copy)
    (copy / "y.tsx").write_text(
        '<span data-testid="resource-list-renamed" />\n', encoding="utf-8"
    )
    assert "resource-list" not in _scan_testids(copy)


# --------------------------------------------------------------------------- #
# 2. supported_extensions is a single runtime fact source                      #
# --------------------------------------------------------------------------- #
def test_supported_extensions_is_a_single_runtime_fact_source():
    """``.xlsx`` / ``.pdf`` are accepted with **no** frontend edit.

    The backend is the one source of truth (``summaries.SUPPORTED_FILE_EXTENSIONS``);
    the frontend surfaces it verbatim from ``/resources/limits`` (no hardcoded list).
    """
    from forgeflow.resources import summaries

    assert ".xlsx" in summaries.SUPPORTED_FILE_EXTENSIONS
    assert ".pdf" in summaries.SUPPORTED_FILE_EXTENSIONS

    # The frontend reads the runtime list (never a hardcoded enumeration).
    client_src = (FRONTEND_SRC / "api" / "client.ts").read_text(encoding="utf-8")
    assert "supported_extensions" in client_src

    picker_src = (FRONTEND_SRC / "views" / "runs" / "ResourcePicker.tsx").read_text(
        encoding="utf-8"
    )
    assert "limitsData.supported_extensions" in picker_src


def test_derive_artifacts_is_a_verbatim_passthrough():
    """The new artifact kinds are visible/downloadable with zero frontend change.

    ``realRun.ts::deriveArtifacts`` returns ``detail.artifacts ?? []`` verbatim, so
    a ``spreadsheet_xlsx`` / ``pdf_document`` artifact appears without touching the UI.
    """
    src = (FRONTEND_SRC / "views" / "runs" / "realRun.ts").read_text(encoding="utf-8")
    assert "return detail.artifacts ?? []" in src


# --------------------------------------------------------------------------- #
# 3. content_kind contract is unchanged (F1 red line)                          #
# --------------------------------------------------------------------------- #
def test_content_kind_contract_is_unchanged():
    """INC45 is additive: the existing ``content_kind`` mapping must not move.

    ``book.xlsx`` stays ``excel`` and ``slides.pptx`` stays ``document`` — the
    routing is fixed by the *planes keying on the kind*, never by rewriting the
    kind (D5).
    """
    from forgeflow.resources.summaries import content_kind

    assert content_kind("book.xlsx") == "excel"
    assert content_kind("slides.pptx") == "document"
    assert content_kind("report.pdf") == "pdf"
    assert content_kind("x.doc.docx") == "document"
