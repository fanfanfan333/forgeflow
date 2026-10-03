"""INC46 T23 — L1 structural validation (SOFTWARE structural integrity).

What L1 checks
--------------
A candidate DOCX must be a *structurally real* document before any content claim
is made about it. L1 has **required** sub-checks and **optional** ones:

Required (any failure ⇒ L1 ``fail``):

  1. ``python-docx`` can open the bytes (:func:`forgeflow.documents.docx_inspect.open_docx`);
  2. OOXML package integrity — the ZIP is readable, ``[Content_Types].xml`` and
     ``_rels/.rels`` are present, and **every** relationship target resolves to a
     real part in the package.

Optional (recorded in evidence; never gates L1):

  3. a **second engine** (a concrete example is LibreOffice ``soffice``). When
     present it is used for a smoke conversion; when absent it is reported as
     **"not measured"** — never silently claimed.

「双引擎」口径 — explicit interpretation (registered deviation)
----------------------------------------------------------------
The task book writes 「双引擎（**如** python-docx + LibreOffice）」. The 「如」
("e.g.") marks this as an *example*, not a hard two-engine mandate; requiring
LibreOffice would make the positive probe (「合规编辑 ⇒ L1 pass」) unreachable on
any machine without it (this box has no ``soffice``). L1 is therefore defined as
**python-docx open + OOXML package integrity as the required engine**, with the
second engine as an *optional, honestly-reported* extra. L1's aggregate = ``fail``
iff a **required** sub-check fails; otherwise ``pass``. This is registered in the
T23 hand-off as an explicit interpretation of the task book.

「schema 校验」口径 — capability boundary, stated honestly (原则 1.1)
---------------------------------------------------------------------
The task book's original wording groups 「关系与内容类型完整」 under an *「OOXML
schema 校验」* heading. What this module **actually** does is **package integrity**:

  * the ZIP is decompressible;
  * ``[Content_Types].xml`` and ``_rels/.rels`` are present;
  * every ``Relationship`` target resolves to a real part.

**XSD-level OOXML schema validation is NOT performed.** This repository does not
ship the OOXML schema set, and validating against it would require an external
validator or fetching the schemas over the network — neither is available here, so
claiming it would be an over-claim. If a real XSD validation is ever required it
must be wired in **explicitly** (a validator dependency + the schema files), and
until then L1's evidence is prefixed ``package-integrity:`` — never ``schema:`` —
so the distinction is visible in every verdict. No default path may claim XSD
schema validation.

This module reads bytes only — it never writes a file and never calls a model.
"""

from __future__ import annotations

import io
import os
import posixpath
import shutil
import subprocess
import tempfile
import zipfile
from xml.etree import ElementTree as ET

from forgeflow.documents.docx_inspect import DocxInspectionError, open_docx
from forgeflow.documents.validation.verdict import (
    FAIL,
    LAYER_STRUCTURAL,
    PASS,
    LayerVerdict,
)

__all__ = [
    "package_integrity",
    "probe_second_engine",
    "structural_verdict",
]

_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "[Content_Types].xml"
_ROOT_RELS = "_rels/.rels"


# --------------------------------------------------------------------------- #
# package reading / relation resolution                                        #
# --------------------------------------------------------------------------- #
def _read_parts(data: bytes) -> dict[str, bytes]:
    """Decompress a DOCX into ``{part_name: bytes}`` (raises on a bad ZIP)."""
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _rels_base_dir(rels_name: str) -> str:
    """The part-directory a ``.rels`` file's relative targets are resolved from.

    ``_rels/.rels`` (package root) → ``""``; ``word/_rels/document.xml.rels`` →
    ``"word"``.
    """
    directory = posixpath.dirname(rels_name)  # e.g. "word/_rels" / "_rels"
    if directory == "_rels":
        return ""
    return posixpath.dirname(directory)


def _resolve_target(base_dir: str, target: str) -> str:
    """Resolve a relationship ``Target`` to a package part name."""
    if target.startswith("/"):
        return target.lstrip("/")
    joined = posixpath.join(base_dir, target) if base_dir else target
    return posixpath.normpath(joined)


def package_integrity(data: bytes) -> tuple[bool, str, dict[str, object]]:
    """Required OOXML package checks. Returns ``(ok, evidence, detail)``."""
    try:
        parts = _read_parts(data)
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"package-integrity:NOT_A_ZIP({type(exc).__name__})", {"error": str(exc)}

    problems: list[str] = []
    if _CONTENT_TYPES not in parts:
        problems.append(f"missing:{_CONTENT_TYPES}")
    if _ROOT_RELS not in parts:
        problems.append(f"missing:{_ROOT_RELS}")

    rels_parts = sorted(
        name for name in parts if name == _ROOT_RELS or name.endswith(".rels")
    )
    rel_total = 0
    rel_external = 0
    rel_unresolved: list[str] = []
    for rels_name in rels_parts:
        blob = parts[rels_name]
        try:
            root = ET.fromstring(blob)
        except ET.ParseError as exc:
            problems.append(f"unparsable-rels:{rels_name}({exc})")
            continue
        base_dir = _rels_base_dir(rels_name)
        for rel in root.findall(f"{{{_REL_NS}}}Relationship"):
            rel_total += 1
            target = rel.get("Target") or ""
            mode = (rel.get("TargetMode") or "").lower()
            if mode == "external":
                rel_external += 1
                continue
            if not target:
                rel_unresolved.append(f"{rels_name}->(empty)")
                continue
            resolved = _resolve_target(base_dir, target)
            if resolved not in parts:
                rel_unresolved.append(f"{rels_name}->{resolved}")

    if rel_unresolved:
        problems.append(f"unresolved-rel-targets:{rel_unresolved[:5]}")

    detail: dict[str, object] = {
        "parts": len(parts),
        "rels_files": len(rels_parts),
        "rels_total": rel_total,
        "rels_external": rel_external,
        "rels_unresolved": rel_unresolved,
        "problems": problems,
    }
    ok = not problems
    evidence = (
        f"package-integrity:parts={len(parts)};"
        f"content-types={'ok' if _CONTENT_TYPES in parts else 'MISSING'};"
        f"root-rels={'ok' if _ROOT_RELS in parts else 'MISSING'};"
        f"rels={rel_total - len(rel_unresolved)}/{rel_total}"
    )
    if not ok:
        evidence = "package-integrity:FAIL[" + ";".join(problems) + "]"
    return ok, evidence, detail


# --------------------------------------------------------------------------- #
# optional second engine                                                       #
# --------------------------------------------------------------------------- #
def probe_second_engine() -> str | None:
    """Path to an optional second engine (LibreOffice ``soffice``), or ``None``.

    A **real** probe via :func:`shutil.which` — never a hard-coded ``False``.
    """
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _second_engine_note(source: bytes, engine: str | None) -> tuple[str, bool]:
    """Run a bounded conversion smoke test. Returns ``(note, ran)``.

    Optional: never gates the L1 verdict. A bounded ``timeout`` guarantees a
    wedged engine cannot hang the stack.
    """
    if not engine:
        return "not measured (soffice/libreoffice absent)", False
    try:
        with tempfile.TemporaryDirectory(prefix="ff-render-") as work:
            src = os.path.join(work, "candidate.docx")
            with open(src, "wb") as handle:
                handle.write(bytes(source))
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [engine, "--headless", "--convert-to", "pdf", "--outdir", work, src],
                capture_output=True,
                text=True,
                timeout=60,
            )
            produced = os.path.join(work, "candidate.pdf")
            if proc.returncode == 0 and os.path.exists(produced):
                return f"second-engine ok ({os.path.basename(engine)})", True
            return (
                f"second-engine attempted but produced no PDF "
                f"(rc={proc.returncode})",
                True,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"second-engine failed ({type(exc).__name__})", True


# --------------------------------------------------------------------------- #
# verdict                                                                      #
# --------------------------------------------------------------------------- #
def structural_verdict(source: bytes, *, engine: str | None = None) -> LayerVerdict:
    """L1 — structural verdict for a candidate DOCX byte string.

    Args:
        source: the candidate DOCX bytes.
        engine: optional second-engine executable override (defaults to a real
            :func:`probe_second_engine` probe).

    Returns:
        A :class:`LayerVerdict`; ``fail`` iff a *required* sub-check failed,
        otherwise ``pass`` (the optional engine is reported, never gating).
    """
    probe = engine if engine is not None else probe_second_engine()
    engine_note, _ = _second_engine_note(source, probe)

    openable = True
    open_evidence = "python-docx:open"
    open_detail: dict[str, object] = {}
    try:
        open_docx(source)
    except DocxInspectionError as exc:
        openable = False
        open_evidence = f"python-docx:OPEN_FAILED({exc})"
        open_detail = {"open_error": str(exc)}

    ok, evidence, detail = package_integrity(source)

    required_ok = openable and ok
    status = PASS if required_ok else FAIL
    combined = f"structural[{open_evidence}; {evidence}; second_engine={engine_note}]"
    merged = {**open_detail, **detail, "second_engine": engine_note}
    return LayerVerdict(LAYER_STRUCTURAL, status, combined, merged)
