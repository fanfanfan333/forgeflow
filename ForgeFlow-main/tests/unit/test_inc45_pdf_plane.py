"""INC45 T03 — the PDF plane: catalogue + handler chain + artifact projection.

Nails the runtime wiring for ``pdf.*`` (design §7 T03):

  * the four new tools are in ``PLATFORM_TOOL_CATALOGUE`` **and** registered, and
    **none** is in ``TOOL_PERMISSION_MAP``;
  * ``planning`` knows ``pdf.inspect`` (needs ``paths``) and ``pdf.generate``
    (needs no external input);
  * ``pdf.inspect`` reuses ``extract_pdf_text``; ``pdf.generate`` writes a real PDF
    that re-parses (non-empty pages);
  * a successful ``pdf.generate`` projects one ``pdf_document`` artifact with an
    empty ``content`` (bytes behind ``content_ref``), downloadable via
    ``_ARTIFACT_MEDIA["pdf"]``; ``artifact.save`` registers it;
  * a missing ``pypdf`` / ``fpdf2`` degrades honestly (``not_executed`` + verbatim).
"""

from __future__ import annotations

import sys

import pytest

from forgeflow.runtime import gate, planning, tool_handlers, tool_registry
from forgeflow.runtime.artifacts import ARTIFACT_KIND_PDF, artifacts_from_invocations

pytestmark = pytest.mark.asyncio

_NEW_TOOLS = ("sheet.inspect", "sheet.edit", "pdf.inspect", "pdf.generate")


def _pdf_bytes() -> bytes:
    from forgeflow.documents import PdfGenerateSpec, apply_pdf_spec

    pytest.importorskip("fpdf")
    return apply_pdf_spec(PdfGenerateSpec(title="报告", paragraphs=["关键内容 42"]))


@pytest.fixture
def pdf_file(tmp_path):
    path = tmp_path / "report.pdf"
    path.write_bytes(_pdf_bytes())
    return str(path)


@pytest.fixture
def hide_pypdf(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)


@pytest.fixture
def hide_fpdf(monkeypatch):
    monkeypatch.setitem(sys.modules, "fpdf", None)


# --------------------------------------------------------------------------- #
# 1. Catalogue / registry / permission gating                                  #
# --------------------------------------------------------------------------- #
async def test_new_tools_are_catalogued_registered_and_ungated():
    tool_registry.load_default_bindings()
    catalogue = set(gate.PLATFORM_TOOL_CATALOGUE)
    assert set(_NEW_TOOLS) <= catalogue
    assert set(gate.PLATFORM_PLAN_TOOLS) == set(tool_registry.known_ids())
    assert not (set(gate.PLATFORM_PLAN_TOOLS) & set(gate.TOOL_PERMISSION_MAP))


async def test_planning_knows_the_new_pdf_tools():
    assert "pdf.inspect" in planning.TOOL_ORDER
    assert "pdf.generate" in planning.TOOL_ORDER
    assert planning.TOOL_INPUT_CONTRACT["pdf.inspect"]["required"] == ("paths",)
    assert planning.TOOL_INPUT_CONTRACT["pdf.generate"]["required"] == ()
    _, missing = planning.resolve_inputs("pdf.inspect", planning.CapabilityContext(intent="x"))
    assert missing == ["paths"]
    _, missing_gen = planning.resolve_inputs(
        "pdf.generate", planning.CapabilityContext(intent="生成一份 PDF")
    )
    assert missing_gen == []  # no external input required


# --------------------------------------------------------------------------- #
# 2. Handler chain                                                             #
# --------------------------------------------------------------------------- #
async def test_pdf_inspect_reuses_extract_pdf_text(pdf_file):
    from forgeflow.multimodal.pdf import extract_pdf_text

    result = await tool_handlers.pdf_inspect({"pdf_paths": [pdf_file]}, None)
    assert result["ok"] is True
    assert result["provider"] == "pypdf"
    assert result["facts"]["page_count"] == extract_pdf_text(pdf_file).page_count


async def test_pdf_generate_writes_a_real_pdf_and_projects_an_artifact():
    result = await tool_handlers.pdf_generate(
        {
            "spec": {"title": "季度报告", "paragraphs": ["第一段", "第二段"]},
            "pdf_names": ["report.pdf"],
        },
        None,
    )
    assert result["ok"] is True
    assert result["format"] == "pdf"
    assert result["filename"] == "report.generated.pdf"
    assert result["provider"] == "fpdf2"
    assert "content" not in result
    assert result["artifact_ref"]

    # The produced PDF (fetched from the store by its content_ref) really
    # re-parses to a non-empty page set.
    from forgeflow.documents import DocArtifactStore
    from forgeflow.multimodal.pdf import extract_pdf_text

    blob = DocArtifactStore().get(result["artifact_ref"])
    assert blob is not None and len(blob) > 0
    document = extract_pdf_text(blob)
    assert document.page_count >= 1
    assert len(document.text) > 0

    invocation = {
        "tool": "pdf.generate",
        "status": "ok",
        "result_ref": result["result_ref"],
        "payload": result,
        "started_at": "t0",
    }
    artifacts = artifacts_from_invocations("runY", [invocation])
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["kind"] == ARTIFACT_KIND_PDF == "pdf_document"
    assert art["format"] == "pdf"
    assert art["content"] == ""
    assert art["content_ref"] == result["artifact_ref"]


async def test_pdf_artifact_is_downloadable_via_media_map():
    from forgeflow.api.routers.runs import _ARTIFACT_MEDIA

    mime, ext = _ARTIFACT_MEDIA["pdf"]
    assert mime == "application/pdf"
    assert ext == "pdf"


async def test_artifact_save_registers_the_pdf_deliverable():
    gen = await tool_handlers.pdf_generate(
        {"spec": {"title": "t", "paragraphs": ["p"]}, "pdf_names": ["deck.pdf"]}, None
    )
    assert gen["ok"]
    observations = [{"tool": "pdf.generate", "status": "ok", "payload": gen}]
    saved = await tool_handlers.artifact_save({"observations": observations}, None)
    assert saved["ok"] is True
    assert saved["kind"] == "pdf_document"
    assert saved["artifact_ref"] == gen["artifact_ref"]


# --------------------------------------------------------------------------- #
# 3. Honest degradation                                                        #
# --------------------------------------------------------------------------- #
async def test_missing_pypdf_degrades_verbatim(pdf_file, hide_pypdf):
    result = await tool_handlers.pdf_inspect({"pdf_paths": [pdf_file]}, None)
    assert result["ok"] is False
    assert result["not_executed"] is True
    assert "pdf 支持不可用" in result["reason"]


async def test_missing_fpdf_degrades_verbatim_no_empty_pdf(hide_fpdf):
    result = await tool_handlers.pdf_generate(
        {"spec": {"title": "x", "paragraphs": ["y"]}}, None
    )
    assert result["ok"] is False
    assert result["not_executed"] is True
    assert "pdf 生成支持不可用" in result["reason"]
