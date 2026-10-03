"""INC45 T02 — the resource seam: PDF signal keys + honest preview (offline).

Nails the additive resource change (design §1.3 / §7 T02):

  * a registered ``.pdf`` dereferences to ``pdf_paths`` / ``pdf_names``
    (index-aligned) while ``paths`` stays **item-for-item unchanged**;
  * the signal comes from ``content_kind`` (the extension), so ``pdf_paths`` is
    produced in **both** the ``parsed`` and the ``metadata_only`` registration
    states (independent of whether ``pypdf`` is installed);
  * ``preview`` reports an **honest empty state** for a PDF (``format == "pdf"``).
"""

from __future__ import annotations

import os

import pytest

from forgeflow.repositories.memory.resource_repo import clear_resource_store
from forgeflow.resources import summaries
from forgeflow.resources.service import ResourceService, reset_resource_index

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Builders / fixtures                                                          #
# --------------------------------------------------------------------------- #
def _real_pdf_bytes() -> bytes:
    from forgeflow.documents import PdfGenerateSpec, apply_pdf_spec

    pytest.importorskip("fpdf")
    return apply_pdf_spec(PdfGenerateSpec(title="报告", paragraphs=["关键内容 42"]))


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
# 1. Classification                                                           #
# --------------------------------------------------------------------------- #
async def test_pdf_is_a_supported_pdf_kind():
    assert summaries.content_kind("report.pdf") == "pdf"
    assert summaries.content_kind("REPORT.PDF") == "pdf"
    assert summaries.is_supported_file("report.pdf") is True
    assert ".pdf" in summaries.SUPPORTED_FILE_EXTENSIONS


# --------------------------------------------------------------------------- #
# 2. Dereference is additive and index-aligned                                 #
# --------------------------------------------------------------------------- #
async def test_registered_parsed_pdf_dereferences_to_pdf_paths(memory_resources, blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="report.pdf", data=_real_pdf_bytes())
    assert record.status == "parsed"

    resolved = service.resolve_task_inputs({"resources": [record.id]})

    assert "pdf_paths" in resolved
    path = resolved["pdf_paths"][0]
    assert os.path.isfile(path)
    assert os.path.splitext(path)[1] == ""  # content-addressed ⇒ extensionless
    assert resolved["pdf_names"] == ["report.pdf"]
    assert resolved["paths"] == [path]
    assert "document_paths" not in resolved
    assert "sheet_paths" not in resolved
    assert "text_paths" not in resolved


async def test_metadata_only_pdf_still_yields_pdf_paths(memory_resources, blob_root):
    """The signal comes from the extension, not from a successful parse."""
    service = ResourceService()
    record = await service.register_file("t-inc45", name="scan.pdf", data=b"not really a pdf")
    assert record.status == "metadata_only"  # parsed-independent

    resolved = service.resolve_task_inputs({"resources": [record.id]})

    assert "pdf_paths" in resolved
    assert resolved["pdf_names"] == ["scan.pdf"]
    assert resolved["paths"] == resolved["pdf_paths"]


async def test_resolve_task_inputs_is_empty_without_a_declaration(memory_resources):
    assert ResourceService().resolve_task_inputs({}) == {}


# --------------------------------------------------------------------------- #
# 3. Preview keeps the honest empty state                                      #
# --------------------------------------------------------------------------- #
async def test_preview_of_a_pdf_is_an_honest_empty_state(memory_resources, blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc45", name="report.pdf", data=_real_pdf_bytes())

    preview = await service.preview("t-inc45", record.id)

    assert preview["available"] is False
    assert preview["format"] == "pdf"
    assert preview["content"] == "" and preview["rows"] == []
    assert preview["note"]
