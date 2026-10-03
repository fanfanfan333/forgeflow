"""INC45 T03 — the XLSX plane: catalogue + handler chain + artifact projection.

Nails the runtime wiring for ``sheet.*`` (design §7 T03):

  * the four new tools are in ``PLATFORM_TOOL_CATALOGUE`` **and** registered (no
    orphan / dead binding), and **none** is in ``TOOL_PERMISSION_MAP``;
  * ``planning`` knows ``sheet.edit`` and reports the missing order
    ``["paths", "edits"]``;
  * ``sheet.inspect`` / ``sheet.edit`` really read / write the workbook;
  * a successful ``sheet.edit`` persists bytes to ``DocArtifactStore`` (``.xlsx``)
    and projects one ``spreadsheet_xlsx`` artifact whose ``content`` is empty
    (bytes behind ``content_ref``), downloadable via ``_ARTIFACT_MEDIA["xlsx"]``;
  * ``artifact.save`` registers the ``sheet.edit`` deliverable; a missing extra
    degrades honestly (``not_executed`` + verbatim).
"""

from __future__ import annotations

import io
import sys

import pytest

from forgeflow.runtime import gate, planning, tool_handlers, tool_registry
from forgeflow.runtime.artifacts import ARTIFACT_KIND_SHEET, artifacts_from_invocations

pytestmark = pytest.mark.asyncio

_NEW_TOOLS = ("sheet.inspect", "sheet.edit", "pdf.inspect", "pdf.generate")


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


# Built once at import so the "missing openpyxl" pin still has a real workbook.
_SAMPLE_WB = _wb_bytes()


@pytest.fixture
def wb_file(tmp_path):
    path = tmp_path / "book.xlsx"
    path.write_bytes(_SAMPLE_WB)
    return str(path)


@pytest.fixture
def hide_openpyxl(monkeypatch):
    monkeypatch.setitem(sys.modules, "openpyxl", None)


# --------------------------------------------------------------------------- #
# 1. Catalogue / registry / permission gating                                  #
# --------------------------------------------------------------------------- #
async def test_new_tools_are_catalogued_registered_and_ungated():
    tool_registry.load_default_bindings()
    catalogue = set(gate.PLATFORM_TOOL_CATALOGUE)
    assert set(_NEW_TOOLS) <= catalogue
    # The single-source-of-truth equality (no orphan / dead binding).
    assert set(gate.PLATFORM_PLAN_TOOLS) == set(tool_registry.known_ids())
    # New tools inherit ``execute:workflows`` — never a narrow grant.
    assert not (set(gate.PLATFORM_PLAN_TOOLS) & set(gate.TOOL_PERMISSION_MAP))
    for tool in _NEW_TOOLS:
        assert tool not in gate.TOOL_PERMISSION_MAP


async def test_planning_knows_the_new_tools():
    assert "sheet.inspect" in planning.TOOL_ORDER
    assert "sheet.edit" in planning.TOOL_ORDER
    assert planning.TOOL_INPUT_CONTRACT["sheet.edit"]["required"] == ("paths", "edits")
    _, missing = planning.resolve_inputs(
        "sheet.edit", planning.CapabilityContext(intent="改一下")
    )
    assert missing == ["paths", "edits"]  # paths first, edits second


# --------------------------------------------------------------------------- #
# 2. Handler chain                                                             #
# --------------------------------------------------------------------------- #
async def test_sheet_inspect_reads_the_real_structure(wb_file):
    result = await tool_handlers.sheet_inspect({"sheet_paths": [wb_file]}, None)
    assert result["ok"] is True
    assert result["provider"] == "openpyxl"
    assert result["structure"]["sheets"] == ["Sheet1"]
    assert result["structure"]["cells"] == 4


async def test_sheet_edit_writes_and_projects_an_artifact(wb_file):
    result = await tool_handlers.sheet_edit(
        {"sheet_paths": [wb_file], "edits": [{"op": "set_cell", "cell": "B2", "value": 5}]},
        None,
    )
    assert result["ok"] is True
    assert result["format"] == "xlsx"
    assert result["filename"] == "book.edited.xlsx"
    assert result["provider"] == "openpyxl"
    assert result["modified"] == 1
    assert "content" not in result  # bytes never ride the payload
    assert result["artifact_ref"]

    invocation = {
        "tool": "sheet.edit",
        "status": "ok",
        "result_ref": result["result_ref"],
        "payload": result,
        "started_at": "t0",
    }
    artifacts = artifacts_from_invocations("runX", [invocation])
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art["kind"] == ARTIFACT_KIND_SHEET == "spreadsheet_xlsx"
    assert art["format"] == "xlsx"
    assert art["content"] == ""
    assert art["content_ref"] == result["artifact_ref"]


async def test_sheet_artifact_is_downloadable_via_media_map():
    from forgeflow.api.routers.runs import _ARTIFACT_MEDIA

    mime, ext = _ARTIFACT_MEDIA["xlsx"]
    assert ext == "xlsx"
    assert "spreadsheetml" in mime


async def test_artifact_save_registers_the_sheet_deliverable(wb_file):
    edit = await tool_handlers.sheet_edit(
        {"sheet_paths": [wb_file], "edits": [{"op": "set_cell", "cell": "B2", "value": 7}]},
        None,
    )
    assert edit["ok"]
    observations = [{"tool": "sheet.edit", "status": "ok", "payload": edit}]
    saved = await tool_handlers.artifact_save({"observations": observations}, None)
    assert saved["ok"] is True
    assert saved["kind"] == "spreadsheet_xlsx"
    assert saved["artifact_ref"] == edit["artifact_ref"]


# --------------------------------------------------------------------------- #
# 3. Honest degradation                                                        #
# --------------------------------------------------------------------------- #
async def test_missing_openpyxl_degrades_verbatim(tmp_path, hide_openpyxl):
    path = tmp_path / "book.xlsx"
    path.write_bytes(_SAMPLE_WB)
    result = await tool_handlers.sheet_inspect({"sheet_paths": [str(path)]}, None)
    assert result["ok"] is False
    assert result["not_executed"] is True
    assert "xlsx 支持不可用" in result["reason"]


async def test_no_workbook_path_is_not_executed():
    result = await tool_handlers.sheet_inspect({}, None)
    assert result["ok"] is False and result["not_executed"] is True
