"""INC44 T04 — runtime routing pins (mutual exclusion + tool wiring).

Design §1.4 / §7 T04: the routing predicates are mutually exclusive in the order
``code > document > textfile > analysis``, and the two new ``textfile.*`` tools
must live in the platform catalogue (⇒ ``PLATFORM_PLAN_TOOLS``) **with** a
registry binding and **without** a narrow grant. A CSV must still route to the
analysis plane (the INC26 Q5 regression must not reappear).
"""

from __future__ import annotations

import pytest

from forgeflow.resources.service import ResourceService, reset_resource_index
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime import tool_registry
from forgeflow.runtime.gate import PLATFORM_PLAN_TOOLS, PLATFORM_TOOL_CATALOGUE, TOOL_PERMISSION_MAP
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

pytestmark = pytest.mark.asyncio

_CTX = RequestContext(tenant_id="t-inc44", user_id="u", role="manager")


@pytest.fixture(autouse=True)
def _clean_resource_index():
    """Keep the in-process resource index hermetic for the CSV pin below."""
    reset_resource_index()
    yield
    reset_resource_index()


# --------------------------------------------------------------------------- #
# 1. Predicates: mutual exclusion across the four planes                       #
# --------------------------------------------------------------------------- #
async def test_pptx_routes_to_the_document_plane():
    task = TaskCreate(intent="编辑幻灯片", context={"paths": ["/srv/a/deck.pptx"]})
    assert orch._is_document_task(task, _CTX) is True
    assert orch._is_textfile_task(task, _CTX) is False
    assert orch._is_analysis_task(task, _CTX) is False
    assert orch._is_code_task(task, _CTX) is False


async def test_py_routes_to_the_textfile_plane_not_analysis():
    task = TaskCreate(intent="改代码", context={"paths": ["/srv/a/app.py"]})
    assert orch._is_textfile_task(task, _CTX) is True
    assert orch._is_analysis_task(task, _CTX) is False
    assert orch._is_document_task(task, _CTX) is False
    assert orch._is_code_task(task, _CTX) is False


async def test_csv_still_routes_to_the_analysis_plane(force_memory_backend):
    """No regression: a registered CSV FILE stays on the analysis plane.

    The CSV analysis signal is a **dereferenced FILE resource** (design §8 —
    ``resolve_task_inputs`` derives ``paths`` for every FILE, and
    ``TABLE_EXTENSIONS`` ⇒ ``analysis``). This mirrors
    ``test_inc26_analysis_route.py::_file_task``'s real registration: a bare
    ``context["paths"]`` entry was **never** an analysis signal (the analysis
    predicate keys on the dereferenced resource, not a caller path), so using one
    would not exercise the text/code exclusion this pin guards.
    """
    service = ResourceService()
    record = await service.register_file(
        "t-inc44",
        name="leads.csv",
        data=b"lead_id,amount\n1,10\n2,20\n",
        created_by="u",
    )
    task = TaskCreate(intent="分析数据", context={"resources": [record.id]})

    assert orch._is_analysis_task(task, _CTX) is True
    assert orch._is_textfile_task(task, _CTX) is False
    assert orch._is_document_task(task, _CTX) is False
    assert orch._is_code_task(task, _CTX) is False


async def test_code_wins_over_document_and_textfile(monkeypatch):
    """The order is ``code > document > textfile``: a code source short-circuits."""
    monkeypatch.setattr(
        orch,
        "_resolve_resource_inputs",
        lambda _c: {
            "repo_path": "/srv/repo",
            "paths": ["/srv/repo/app.py"],
            "document_paths": ["/srv/b/x"],
        },
    )
    task = TaskCreate(intent="改代码仓库", context={"resources": ["r1"]})
    assert orch._is_code_task(task, _CTX) is True
    assert orch._is_document_task(task, _CTX) is False
    assert orch._is_textfile_task(task, _CTX) is False
    assert orch._is_analysis_task(task, _CTX) is False


async def test_document_wins_over_textfile(monkeypatch):
    monkeypatch.setattr(
        orch,
        "_resolve_resource_inputs",
        lambda _c: {"document_paths": ["/srv/b/x"], "paths": ["/srv/b/x"]},
    )
    task = TaskCreate(intent="改文档", context={"resources": ["r1"]})
    assert orch._is_document_task(task, _CTX) is True
    assert orch._is_textfile_task(task, _CTX) is False


async def test_dereferenced_text_paths_drive_the_textfile_plane(monkeypatch):
    """A registered text / code FILE (content-addressed, extensionless) is a signal."""
    monkeypatch.setattr(
        orch, "_resolve_resource_inputs", lambda _c: {"text_paths": ["/srv/t/x"], "paths": ["/srv/t/x"]}
    )
    task = TaskCreate(intent="改文件", context={"resources": ["r1"]})
    assert orch._is_textfile_task(task, _CTX) is True
    assert orch._is_analysis_task(task, _CTX) is False


async def test_declared_textfile_tool_is_a_signal():
    task = TaskCreate(intent="改文件", context={"declared_tools": ["textfile.edit"]})
    assert orch._is_textfile_task(task, _CTX) is True


async def test_md_and_json_are_text_not_analysis(monkeypatch):
    for name in ("notes.md", "config.json"):
        task = TaskCreate(intent="改文件", context={"paths": [f"/srv/a/{name}"]})
        assert orch._is_textfile_task(task, _CTX) is True, name
        assert orch._is_analysis_task(task, _CTX) is False, name


# --------------------------------------------------------------------------- #
# 2. Plan injection + order                                                    #
# --------------------------------------------------------------------------- #
async def test_textfile_plane_is_injected_before_report(): 
    task = TaskCreate(intent="改文件", context={"paths": ["/srv/a/app.py"]})
    tools = [c["tool"] for c in orch._candidates_for(task, _CTX)]

    for tool in orch._TEXTFILE_TOOLS:
        assert tool in tools
    assert tools.index("textfile.inspect") < tools.index("textfile.edit")
    assert tools.index("textfile.edit") < tools.index("artifact.save")
    assert tools[-1] == "report.render"

    injected = orch._platform_injected_tools(task, _CTX)
    assert injected == [*orch._TEXTFILE_TOOLS, "artifact.save"]


async def test_document_plane_injection_is_unchanged_for_pptx():
    task = TaskCreate(intent="改文档", context={"paths": ["/srv/a/deck.pptx"]})
    injected = orch._platform_injected_tools(task, _CTX)
    assert injected == list(orch._DOCUMENT_TOOLS)
    assert orch._is_document_task(task, _CTX) is True


# --------------------------------------------------------------------------- #
# 3. Tool wiring: catalogue ⇔ registry ⇔ grants                                #
# --------------------------------------------------------------------------- #
async def test_textfile_tools_are_catalogued_and_bound_without_a_narrow_grant():
    for tool in ("textfile.inspect", "textfile.edit"):
        assert tool in PLATFORM_TOOL_CATALOGUE
        assert tool in PLATFORM_PLAN_TOOLS
        assert tool not in TOOL_PERMISSION_MAP
        assert tool_registry.resolve(tool) is not None


async def test_no_orphan_or_dead_binding():
    """Criterion ①: the catalogue and the registry are exactly equal."""
    assert set(PLATFORM_PLAN_TOOLS) == set(tool_registry.known_ids())


async def test_non_document_target_edit_still_fails_with_docx_reason(tmp_path):
    """A non-docx/pptx target keeps the INC43 honest failure (reason names docx)."""
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
