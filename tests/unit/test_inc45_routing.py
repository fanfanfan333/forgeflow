"""INC45 T04 — runtime routing pins (mutual exclusion code>document>sheet>pdf>textfile>analysis).

Design §1.3 / §7 T04: a registered ``.xlsx`` / ``.pdf`` routes to its own plane and
**never** to the CSV profiler (the defect this iteration fixes). The signal comes
from the extension (``content_kind``), so a ``.pdf`` routes identically in the
``parsed`` and the ``metadata_only`` registration states. A CSV must still route to
the analysis plane (the INC26 Q5 regression must not reappear).
"""

from __future__ import annotations

import io

import pytest

from forgeflow.repositories.memory.resource_repo import clear_resource_store
from forgeflow.resources.service import ResourceService, reset_resource_index
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

pytestmark = pytest.mark.asyncio

_CTX = RequestContext(tenant_id="t-inc45", user_id="u", role="manager")


def _xlsx_bytes() -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet["A1"] = "name"
    sheet["B1"] = "qty"
    sheet["A2"] = "apple"
    sheet["B2"] = 3
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pdf_bytes() -> bytes:
    from forgeflow.documents import PdfGenerateSpec, apply_pdf_spec

    pytest.importorskip("fpdf")
    return apply_pdf_spec(PdfGenerateSpec(title="报告", paragraphs=["关键内容 42"]))


@pytest.fixture(autouse=True)
def _clean_resource_index():
    reset_resource_index()
    clear_resource_store()
    yield
    reset_resource_index()
    clear_resource_store()


@pytest.fixture
def memory_resources(force_memory_backend):
    reset_resource_index()
    clear_resource_store()
    yield
    reset_resource_index()
    clear_resource_store()


@pytest.fixture
def blob_root(monkeypatch, tmp_path):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path / "res-blobs"))
    return tmp_path / "res-blobs"


# --------------------------------------------------------------------------- #
# 1. Registered resources route to their own plane (and never to analysis)      #
# --------------------------------------------------------------------------- #
async def test_registered_xlsx_routes_to_the_sheet_plane(memory_resources, blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="book.xlsx", data=_xlsx_bytes())
    task = TaskCreate(intent="改表格", context={"resources": [record.id]})

    assert orch._is_sheet_task(task, _CTX) is True
    assert orch._is_analysis_task(task, _CTX) is False
    assert orch._is_textfile_task(task, _CTX) is False
    assert orch._is_pdf_task(task, _CTX) is False
    assert orch._is_document_task(task, _CTX) is False


async def test_registered_pdf_routes_to_the_pdf_plane_parsed(memory_resources, blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="report.pdf", data=_pdf_bytes())
    assert record.status == "parsed"
    task = TaskCreate(intent="看 PDF", context={"resources": [record.id]})

    assert orch._is_pdf_task(task, _CTX) is True
    assert orch._is_analysis_task(task, _CTX) is False
    assert orch._is_sheet_task(task, _CTX) is False


async def test_registered_pdf_routes_to_the_pdf_plane_metadata_only(
    memory_resources, blob_root
):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="scan.pdf", data=b"not a real pdf")
    assert record.status == "metadata_only"
    task = TaskCreate(intent="看 PDF", context={"resources": [record.id]})

    assert orch._is_pdf_task(task, _CTX) is True
    assert orch._is_analysis_task(task, _CTX) is False


# --------------------------------------------------------------------------- #
# 2. Existing planes are unchanged                                             #
# --------------------------------------------------------------------------- #
async def test_csv_still_routes_to_the_analysis_plane(memory_resources, blob_root):
    service = ResourceService()
    record = await service.register_file(
        "t-inc45", name="leads.csv", data=b"lead_id,amount\n1,10\n"
    )
    task = TaskCreate(intent="分析数据", context={"resources": [record.id]})

    assert orch._is_analysis_task(task, _CTX) is True
    assert orch._is_sheet_task(task, _CTX) is False
    assert orch._is_pdf_task(task, _CTX) is False


async def test_pptx_routes_to_document_and_py_to_textfile():
    deck = TaskCreate(intent="改幻灯片", context={"paths": ["/srv/a/deck.pptx"]})
    assert orch._is_document_task(deck, _CTX) is True
    assert orch._is_sheet_task(deck, _CTX) is False
    assert orch._is_pdf_task(deck, _CTX) is False

    code = TaskCreate(intent="改代码", context={"paths": ["/srv/a/app.py"]})
    assert orch._is_textfile_task(code, _CTX) is True
    assert orch._is_sheet_task(code, _CTX) is False
    assert orch._is_analysis_task(code, _CTX) is False


async def test_document_wins_over_sheet_and_pdf(monkeypatch):
    monkeypatch.setattr(
        orch,
        "_resolve_resource_inputs",
        lambda _c: {"document_paths": ["/srv/b/x"], "paths": ["/srv/b/x"]},
    )
    task = TaskCreate(intent="改文档", context={"resources": ["r1"]})
    assert orch._is_document_task(task, _CTX) is True
    assert orch._is_sheet_task(task, _CTX) is False
    assert orch._is_pdf_task(task, _CTX) is False


# --------------------------------------------------------------------------- #
# 3. Plan injection / order / args                                             #
# --------------------------------------------------------------------------- #
async def test_sheet_plane_is_injected_before_report(memory_resources, blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="book.xlsx", data=_xlsx_bytes())
    task = TaskCreate(intent="改表格", context={"resources": [record.id]})

    tools = [c["tool"] for c in orch._candidates_for(task, _CTX)]
    assert tools.index("sheet.inspect") < tools.index("sheet.edit") < tools.index("artifact.save")
    assert tools[-1] == "report.render"
    assert orch._platform_injected_tools(task, _CTX) == [*orch._SHEET_TOOLS, "artifact.save"]


async def test_pdf_generate_is_only_injected_when_declared():
    viewing = TaskCreate(intent="看 PDF", context={"paths": ["/srv/a/a.pdf"]})
    assert orch._platform_injected_tools(viewing, _CTX) == ["pdf.inspect", "artifact.save"]

    generating = TaskCreate(
        intent="生成 PDF",
        context={"paths": ["/srv/a/a.pdf"], "declared_tools": ["pdf.generate"]},
    )
    assert orch._platform_injected_tools(generating, _CTX) == [
        "pdf.inspect",
        "pdf.generate",
        "artifact.save",
    ]
    tools = [c["tool"] for c in orch._candidates_for(generating, _CTX)]
    assert tools.index("pdf.inspect") < tools.index("pdf.generate") < tools.index("artifact.save")


async def test_execution_args_use_sheet_and_pdf_arg_builders(monkeypatch):
    from forgeflow.runtime import planning as _planning

    # Direct unit on the arg builders (the capability context is the real source).
    cap = _planning.CapabilityContext(
        explicit_inputs={
            "sheet_paths": ["/x/book"],
            "sheet_names": ["book.xlsx"],
            "edits": [{"op": "set_cell", "cell": "A1", "value": 1}],
        }
    )
    args = orch._sheet_args(None, cap)
    assert args["sheet_paths"] == ["/x/book"]
    assert args["sheet_names"] == ["book.xlsx"]
    assert args["edits"] == [{"op": "set_cell", "cell": "A1", "value": 1}]

    pdf_task = TaskCreate(
        intent="生成",
        context={"spec": {"title": "t", "paragraphs": ["p"]}},
    )
    pcap = _planning.CapabilityContext(
        explicit_inputs={"pdf_paths": ["/y/a"], "pdf_names": ["report.pdf"]}
    )
    pargs = orch._pdf_args(pdf_task, pcap)
    assert pargs["pdf_paths"] == ["/y/a"]
    assert pargs["pdf_names"] == ["report.pdf"]
    assert pargs["spec"] == {"title": "t", "paragraphs": ["p"]}

    # ``_execution_args`` dispatches ``sheet.*`` / ``pdf.*`` through the builders.
    monkeypatch.setattr(orch, "_sheet_args", lambda task, cap: {"sheet_paths": ["/sentinel"]})
    monkeypatch.setattr(orch, "_pdf_args", lambda task, cap: {"pdf_paths": ["/sentinel2"]})
    sheet_task = TaskCreate(intent="改表格", context={})
    assert orch._execution_args("sheet.edit", sheet_task, _CTX, None, [])["sheet_paths"] == [
        "/sentinel"
    ]
    assert orch._execution_args("pdf.inspect", sheet_task, _CTX, None, [])["pdf_paths"] == [
        "/sentinel2"
    ]


async def test_non_document_target_edit_still_fails_with_docx_reason(tmp_path):
    from forgeflow.runtime.tool_handlers import document_edit

    csv = tmp_path / "data.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    result = await document_edit(
        {"paths": [str(csv)], "edits": [{"op": "replace_text", "match": "1", "replace": "2"}]},
        _CTX,
    )
    assert result["ok"] is False
    assert result["not_executed"] is True
    assert "docx" in result["reason"]
