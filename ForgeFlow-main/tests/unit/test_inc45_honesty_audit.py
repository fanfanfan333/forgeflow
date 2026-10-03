"""INC45 T05 — honesty discipline audit for the XLSX editing + PDF planes.

Design §7 T05 criterion ② / §8 诚实口径: *假计数扫描 / ``0`` 兜底扫描 / 未测量⇒``None``
/ 缺 extra 诚实降级 / 耗尽诚实报错，全绿*.

Every pin here is a **falsifiable** honesty claim, not a smoke test:

  * **tri-state** — an unmeasured verification dimension is ``None`` (never a fake
    ``True`` / ``False``) for both the XLSX and the PDF plane;
  * **no fabricated counts** — the delivered 「修改 N 处」 figures are, in source,
    *either* the recomputed ``diff`` value *or* an honest ``0`` on the failure path
    — never a hardcoded positive;
  * **exhaustion is honest** — an unsatisfiable constraint ends in ``ok=False``
    with ``modified == 0`` / no ``artifact_ref`` (no fake deliverable);
  * **missing extra degrades, never 5xx** — a ``.xlsx`` / ``.pdf`` whose optional
    extra (``openpyxl`` / ``fpdf2``) is absent lands ``not_executed`` with a
    **verbatim** reason, and the up-front upload still never claims a fabricated
    measurement (an unparseable ``.xlsx`` is ``metadata_only``, never ``parsed``).

Citation discipline: ``file.py::symbol`` anchors, never ``file:line``.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.documents import compute_sheet_diff, verify_pdf, verify_sheet
from forgeflow.documents.sheet_edit import apply_edits as apply_sheet_edits

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_HANDLERS = REPO_ROOT / "forgeflow" / "runtime" / "tool_handlers.py"

# NOTE: no module-level ``pytest.mark.asyncio`` here on purpose — this module mixes
# sync honesty pins (source scans / pure-function checks) with ``async`` handler
# tests. ``pyproject.toml`` sets ``asyncio_mode = "auto"``, so the async tests are
# collected without an explicit mark, and the sync ones stay unmarked (a blanket
# mark would raise a PytestWarning on each sync test).


# --------------------------------------------------------------------------- #
# Builders — built at import so the "missing extra" pins still have real bytes  #
# --------------------------------------------------------------------------- #
def _wb_bytes(value_b2: object = 3) -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["A1"] = "name"
    sheet["B1"] = "qty"
    sheet["A2"] = "apple"
    sheet["B2"] = value_b2
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pdf_bytes() -> bytes:
    from forgeflow.documents import PdfGenerateSpec, apply_pdf_spec

    pytest.importorskip("fpdf")
    return apply_pdf_spec(PdfGenerateSpec(title="报告", paragraphs=["关键内容 42"]))


_SAMPLE_WB = _wb_bytes()  # built BEFORE any monkeypatch hides openpyxl


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-inc45-honesty",
        step_id="run-inc45-honesty:0:0",
        tenant_id="t-inc45",
        user_id="u-inc45",
        role="manager",
        args={},
    )


@pytest.fixture
def tmp_doc_store(tmp_path, monkeypatch):
    from forgeflow.documents import store as store_mod

    real = store_mod.DocArtifactStore

    def _factory(root=None):
        return real(root=root if root is not None else tmp_path / "docstore")

    import forgeflow.documents as docs_pkg

    monkeypatch.setattr(store_mod, "DocArtifactStore", _factory)
    monkeypatch.setattr(docs_pkg, "DocArtifactStore", _factory)
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. Tri-state: unmeasured ⇒ None (never a fake True/False)                    #
# --------------------------------------------------------------------------- #
async def test_verify_sheet_unmeasured_dimensions_are_none():
    report = verify_sheet(_SAMPLE_WB, {})
    assert report.openable is True
    assert report.structure_ok is None
    assert report.data_ok is None
    assert report.requirement_ok is None


async def test_verify_pdf_unmeasured_dimensions_are_none():
    report = verify_pdf(_pdf_bytes(), {})
    assert report.openable is True
    assert report.structure_ok is None
    assert report.data_ok is None
    assert report.requirement_ok is None


async def test_verify_sheet_unopenable_bytes_are_honest():
    report = verify_sheet(b"not a workbook", {"max_chars": 10})
    assert report.openable is False
    assert report.structure_ok is None
    assert report.data_ok is None
    assert report.requirement_ok is None


async def test_verify_pdf_unopenable_bytes_are_honest():
    report = verify_pdf(b"not a pdf", {"expected_pages": 2})
    assert report.openable is False
    assert report.structure_ok is None
    assert report.data_ok is None
    assert report.requirement_ok is None


# --------------------------------------------------------------------------- #
# 2. No fabricated counts — the figures are ``diff``-derived or an honest 0     #
# --------------------------------------------------------------------------- #
def test_delivered_counts_are_never_hardcoded_positives():
    """Every ``modified`` / ``added`` / ``removed`` / ``numeric_changes`` in the
    document / text / sheet handlers is **either** the recomputed ``diff.<field>``
    **or** an honest ``0`` (the failure path) — never a fabricated positive literal.
    """
    source = TOOL_HANDLERS.read_text(encoding="utf-8")
    for field in ("modified", "added", "removed", "numeric_changes"):
        values = re.findall(rf'"{field}":\s*([^,\n]+)', source)
        assert values, f"{field} not found — scan would be vacuous"
        for raw in values:
            token = raw.strip()
            assert token in (f"diff.{field}", "0"), (field, token)


async def test_sheet_success_counts_equal_the_recomputed_diff(tmp_path, tmp_doc_store):
    """The reported figures are exactly the pure ``compute_sheet_diff`` result."""
    from forgeflow.runtime.tool_handlers import sheet_edit

    old = _SAMPLE_WB
    ops = [{"op": "set_cell", "cell": "B2", "value": 5}]
    new, _changes = apply_sheet_edits(old, ops)
    expected = compute_sheet_diff(old, new)

    target = tmp_path / "book.xlsx"
    target.write_bytes(old)
    result = await sheet_edit({"sheet_paths": [str(target)], "edits": ops}, _ctx())
    assert result["ok"] is True
    assert result["modified"] == expected.modified
    assert result["added"] == expected.added
    assert result["removed"] == expected.removed
    assert result["numeric_changes"] == expected.numeric_changes


# --------------------------------------------------------------------------- #
# 3. Exhaustion is honest — ok=False, count 0, NO artifact_ref                 #
# --------------------------------------------------------------------------- #
async def test_sheet_edit_exhaustion_is_honest(tmp_path, tmp_doc_store):
    from forgeflow.runtime.tool_handlers import sheet_edit

    target = tmp_path / "book.xlsx"
    target.write_bytes(_SAMPLE_WB)
    result = await sheet_edit(
        {
            "sheet_paths": [str(target)],
            "edits": [{"op": "set_cell", "cell": "B2", "value": 5}],
            "max_chars": 1,  # unsatisfiable ⇒ honest failure
        },
        _ctx(),
    )
    assert result["ok"] is False
    assert "校验未通过" in result["reason"]
    assert result["modified"] == 0
    assert result["validation"]["requirement_ok"] is False
    assert "artifact_ref" not in result


async def test_pdf_generate_exhaustion_is_honest(tmp_doc_store):
    pytest.importorskip("fpdf")
    from forgeflow.runtime.tool_handlers import pdf_generate

    result = await pdf_generate(
        {
            "spec": {"title": "报告", "paragraphs": ["关键内容 42"]},
            "expected_pages": 99,  # unsatisfiable ⇒ honest failure
        },
        _ctx(),
    )
    assert result["ok"] is False
    assert "校验未通过" in result["reason"]
    assert result["validation"]["structure_ok"] is False
    assert "artifact_ref" not in result


# --------------------------------------------------------------------------- #
# 4. Missing extra / unparseable ⇒ not_executed / metadata_only + verbatim      #
# --------------------------------------------------------------------------- #
async def test_missing_openpyxl_degrades_verbatim_no_fake_deliverable(
    tmp_path, monkeypatch, tmp_doc_store
):
    from forgeflow.runtime.tool_handlers import sheet_edit, sheet_inspect

    target = tmp_path / "book.xlsx"
    target.write_bytes(_SAMPLE_WB)
    monkeypatch.setitem(sys.modules, "openpyxl", None)

    inspected = await sheet_inspect({"sheet_paths": [str(target)]}, _ctx())
    assert inspected["ok"] is False
    assert inspected["not_executed"] is True
    assert "xlsx 支持不可用" in inspected["reason"]

    edited = await sheet_edit(
        {"sheet_paths": [str(target)], "edits": [{"op": "set_cell", "cell": "B2", "value": 5}]},
        _ctx(),
    )
    assert edited["ok"] is False
    assert edited["not_executed"] is True
    assert "xlsx 支持不可用" in edited["reason"]
    assert "artifact_ref" not in edited  # never a fake deliverable


async def test_missing_fpdf_degrades_verbatim_no_empty_pdf(monkeypatch, tmp_doc_store):
    from forgeflow.runtime.tool_handlers import pdf_generate

    monkeypatch.setitem(sys.modules, "fpdf", None)
    result = await pdf_generate(
        {"spec": {"title": "x", "paragraphs": ["y"]}}, _ctx()
    )
    assert result["ok"] is False
    assert result["not_executed"] is True
    assert "PDF 生成不可用" in result["reason"]
    assert "artifact_ref" not in result  # never a fabricated empty PDF


def test_unparseable_xlsx_upload_is_never_parsed():
    """An unparseable ``.xlsx`` degrades to ``metadata_only`` (verbatim), never
    ``parsed`` — the upload never claims a measurement it did not make."""
    from forgeflow.resources import summaries

    status, _summary, detail = summaries.summarize_bytes(
        b"not a zip container", filename="broken.xlsx"
    )
    assert status == "metadata_only"
    assert "unreadable xlsx" in detail  # the verbatim reason


async def test_unparseable_xlsx_upload_status_is_metadata_only(force_memory_backend):
    from forgeflow.resources.service import ResourceService, reset_resource_index

    reset_resource_index()
    try:
        record = await ResourceService().register_file(
            "t-inc45", name="broken.xlsx", data=b"not a zip", created_by="u"
        )
        assert record.status == "metadata_only"
        assert record.detail  # verbatim reason present
    finally:
        reset_resource_index()


def test_pdf_is_routed_even_without_pypdf_installed(monkeypatch):
    """D9 — the routing signal is the extension, so a ``.pdf`` stays in the PDF
    plane whether or not ``pypdf`` is installed (never a fake success, never a
    silent fall-through to the analysis plane)."""
    from forgeflow.runtime import orchestrator as orch
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

    ctx = RequestContext(tenant_id="t-inc45", user_id="u", role="manager")
    # Route deterministically with ``pypdf`` unavailable: the extension is the
    # signal, so the verdict must not depend on the optional extra.
    monkeypatch.setitem(sys.modules, "pypdf", None)
    task = TaskCreate(intent="看 PDF", context={"paths": ["/srv/a/a.pdf"]})
    assert orch._is_pdf_task(task, ctx) is True
    assert orch._is_analysis_task(task, ctx) is False
