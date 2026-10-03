"""INC45 T02 — the resource seam: XLSX signal keys + honest preview (offline).

Nails the additive resource change (design §1.3 / §7 T02):

  * a registered ``.xlsx`` / ``.xlsm`` dereferences to ``sheet_paths`` /
    ``sheet_names`` (index-aligned) while ``paths`` stays **item-for-item
    unchanged** (the code / analysis seams keep seeing every FILE path verbatim);
  * ``content_kind`` is untouched (``.xlsx`` stays ``excel``, ``.pptx`` stays
    ``document`` — the two load-bearing nails);
  * ``preview`` reports an **honest empty state** for a workbook (``format ==
    "sheet"``) instead of decoding binary bytes into garbage lines;
  * ``GET /resources/limits`` still reads the one source of truth.
"""

from __future__ import annotations

import io
import os

import pytest

from forgeflow.repositories.memory.resource_repo import clear_resource_store
from forgeflow.resources import summaries
from forgeflow.resources.service import ResourceService, reset_resource_index

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Builders / fixtures                                                          #
# --------------------------------------------------------------------------- #
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
# 1. Classification is untouched                                               #
# --------------------------------------------------------------------------- #
async def test_content_kind_nails_are_unchanged():
    assert summaries.content_kind("book.xlsx") == "excel"
    assert summaries.content_kind("macro.xlsm") == "excel"
    assert summaries.content_kind("slides.pptx") == "document"
    assert summaries.content_kind("report.docx") == "document"
    assert summaries.content_kind("data.csv") == "table"


async def test_limits_reads_the_single_source_of_truth(memory_resources):
    extensions = ResourceService().limits()["supported_extensions"]
    assert extensions == list(summaries.SUPPORTED_FILE_EXTENSIONS)
    assert ".xlsx" in extensions


# --------------------------------------------------------------------------- #
# 2. Dereference is additive and index-aligned                                 #
# --------------------------------------------------------------------------- #
async def test_registered_xlsx_dereferences_to_sheet_paths_additively(
    memory_resources, blob_root
):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="book.xlsx", data=_xlsx_bytes())
    assert record.status == "parsed"

    resolved = service.resolve_task_inputs({"resources": [record.id]})

    assert "sheet_paths" in resolved
    path = resolved["sheet_paths"][0]
    assert os.path.isfile(path)
    assert os.path.splitext(path)[1] == ""  # content-addressed ⇒ extensionless
    assert resolved["sheet_names"] == ["book.xlsx"]
    # Additive: ``paths`` still carries the file path verbatim; a workbook is
    # neither a document nor a text file.
    assert resolved["paths"] == [path]
    assert "document_paths" not in resolved
    assert "text_paths" not in resolved


async def test_mixed_registration_keeps_every_list_aligned(memory_resources, blob_root):
    service = ResourceService()
    csv = await service.register_file("t-inc45", name="data.csv", data=b"a,b\n1,2\n")
    xlsx = await service.register_file("t-inc45", name="book.xlsx", data=_xlsx_bytes())
    py = await service.register_file("t-inc45", name="tool.py", data=b"x = 1\n")

    resolved = service.resolve_task_inputs({"resources": [csv.id, xlsx.id, py.id]})

    assert len(resolved["paths"]) == 3
    assert len(resolved["sheet_paths"]) == len(resolved["sheet_names"]) == 1
    assert len(resolved["text_paths"]) == len(resolved["text_names"]) == 1
    assert resolved["sheet_names"] == ["book.xlsx"]
    assert "document_paths" not in resolved  # no document among the three


async def test_resolve_task_inputs_is_empty_without_a_declaration(memory_resources):
    assert ResourceService().resolve_task_inputs({}) == {}


# --------------------------------------------------------------------------- #
# 3. Preview keeps the honest empty state                                      #
# --------------------------------------------------------------------------- #
async def test_preview_of_a_workbook_is_an_honest_empty_state(memory_resources, blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="book.xlsx", data=_xlsx_bytes())

    preview = await service.preview("t-inc45", record.id)

    assert preview["available"] is False
    assert preview["format"] == "sheet"
    assert preview["content"] == "" and preview["rows"] == []
    assert preview["note"]


async def test_preview_of_a_table_is_still_verbatim(memory_resources, blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="data.csv", data=b"a,b\n1,2\n")

    preview = await service.preview("t-inc45", record.id)

    assert preview["available"] is True
    assert preview["format"] == "table"
    assert preview["columns"] == ["a", "b"]
