"""INC45 T05 — end-to-end: a registered ``.xlsx`` is really edited (new workbook,
original untouched) and a declared ``pdf.generate`` really produces a new PDF; both
deliverables are downloadable (memory profile, deterministic, offline).

Design §7 T05 criterion ①: *上传 ``.xlsx`` → 改一个单元格 → 文档面写新工作簿（原文件
不变）；``pdf.generate`` → 产出可再次解析的新 PDF；两条产物均能被 ``deriveArtifacts``
逐字透传*.

What this nails down
--------------------
1. **Registration is load-bearing** — ``ResourceService.register_file`` really parses
   the upload (``status == "parsed"``) and the resource seam dereferences it into
   ``sheet_paths`` (``content_kind == "excel"``) / ``pdf_paths`` (``content_kind ==
   "pdf"``), which is exactly what drives each plane (INC45 §1.3).
2. **The plane really writes a new file** — ``sheet.edit`` produces a fresh,
   content-addressed ``artifact_ref`` whose bytes are a *real, edited* workbook
   (reopened with the same reader, original on disk byte-for-byte unchanged), and
   ``pdf.generate`` produces a *real* PDF that re-parses to a non-empty page set.
3. **The deliverables are downloadable** — the projected artifacts
   (``spreadsheet_xlsx`` / ``pdf_document``) are served by
   ``api.routers.runs.download_artifact`` byte-for-byte, with the right media type.
4. **Verbatim passthrough contract** — each artifact dict carries exactly the fields
   ``frontend/src/views/runs/realRun.ts::deriveArtifacts`` reads (``return
   detail.artifacts ?? []``) and is JSON-safe, so the UI shows / downloads it with
   **zero frontend changes** (the INC45 posture — §9-6).

The execution is the deterministic offline one (explicit ``edits`` / ``spec``): no
model, no network, no LLM. The natural-language → ops layer is pinned separately at
the unit level (``test_inc45_sheet_edit.py`` / ``test_inc45_pdf_domain.py``).

Citation discipline: ``file.py::symbol`` anchors, never ``file:line``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.routers import runs as runs_router
from forgeflow.resources.service import ResourceService, reset_resource_index
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.artifacts import artifacts_from_invocations
from forgeflow.runtime.orchestrator import (
    RequestContext,
    RunRecord,
    TaskCreate,
    get_run_store,
    reset_run_store,
)

TENANT = "t-inc45-e2e"


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_state():
    """Deterministic under any backend: clean run store + resource index."""
    reset_run_store()
    reset_resource_index()
    yield
    reset_run_store()
    reset_resource_index()


@pytest.fixture
def tmp_doc_store(tmp_path, monkeypatch):
    """Point the deliverable blob store at a tmp dir (keeps the suite hermetic)."""
    from forgeflow.documents import store as store_mod

    real = store_mod.DocArtifactStore

    def _factory(root=None):
        return real(root=root if root is not None else tmp_path / "docstore")

    import forgeflow.documents as docs_pkg

    monkeypatch.setattr(store_mod, "DocArtifactStore", _factory)
    monkeypatch.setattr(docs_pkg, "DocArtifactStore", _factory)
    return tmp_path


@pytest.fixture
def blob_root(monkeypatch, tmp_path):
    """Point the *resource* blob store at a tmp dir (the dereferenced source path)."""
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path / "res-blobs"))
    return tmp_path / "res-blobs"


def _xlsx_bytes(value_b2: object = 3) -> bytes:
    """A real XLSX: one sheet with a header row and one data row."""
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
    """A real, parseable PDF via the INC45 generator (the single truth)."""
    pytest.importorskip("fpdf")
    from forgeflow.documents import PdfGenerateSpec, apply_pdf_spec

    return apply_pdf_spec(
        PdfGenerateSpec(title="来源报告", paragraphs=["原始内容 10 万元"])
    )


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def emit(self, run_id, event_type, data):
        self.events.append((event_type, data))


async def _register(name: str, data: bytes):
    return await ResourceService().register_file(
        TENANT, name=name, data=data, created_by="u"
    )


def _client(tenant: str = TENANT) -> TestClient:
    """A minimal app exposing only the runs router (tenant pinned, RBAC off)."""
    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


def _save_run_with_artifacts(run_id: str, artifacts, tenant: str = TENANT) -> None:
    record = RunRecord(
        run_id=run_id,
        thread_id=f"{run_id}-th",
        tenant_id=tenant,
        agent_id=None,
        intent="编辑文件",
        status="completed",
        outcome="success",
        steps=[],
        errors=[],
        created_at="2026-10-05T00:00:00+00:00",
        completed_at="2026-10-05T00:00:01+00:00",
        session_id=run_id,
    )
    record.artifacts = list(artifacts)
    get_run_store().save(record)


# --------------------------------------------------------------------------- #
# 1. XLSX — upload → edit one cell → downloadable new workbook, original intact #
# --------------------------------------------------------------------------- #
async def test_xlsx_registered_edited_and_downloaded(
    force_memory_backend, tmp_doc_store, blob_root
):
    from forgeflow.resources.summaries import content_kind

    # Built ONCE and reused: an ``openpyxl`` save embeds a DOS timestamp, so two
    # independent ``_xlsx_bytes()`` calls are NOT byte-equal. Comparing the
    # registered bytes against a *second* fresh generation is a flaky fixture
    # bug, not a product fact — the real assertion is "the bytes we uploaded are
    # the bytes on the dereferenced source path".
    uploaded = _xlsx_bytes(value_b2=3)
    record = await _register("book.xlsx", uploaded)
    assert record.status == "parsed"
    assert content_kind("book.xlsx") == "excel"

    ctx = RequestContext(tenant_id=TENANT, user_id="u", role="manager")
    task = TaskCreate(
        intent="把库存数量 B2 改成 5",
        context={
            "resources": [record.id],
            "declared_tools": ["sheet.edit"],
            "edits": [{"op": "set_cell", "cell": "B2", "value": 5}],
        },
    )

    # The dereferenced source path (content-addressed, extensionless) — captured
    # BEFORE the run so we can prove the plane never mutates the original in place.
    resolved = orch._resolve_resource_inputs(task.context)
    assert resolved.get("sheet_paths"), resolved
    source = resolved["sheet_paths"][0]
    original_bytes = Path(source).read_bytes()
    assert original_bytes == uploaded

    run_id = "run-inc45-xlsx-e2e"
    steps, errors = await orch._default_executor(task, ctx, _Bus(), run_id)

    assert errors == []
    by_tool = {s["tool"]: s["status"] for s in steps}
    assert by_tool["sheet.inspect"] == "ok"
    assert by_tool["sheet.edit"] == "ok"
    assert by_tool["artifact.save"] == "ok"

    # The original workbook on the source path is byte-for-byte unchanged.
    assert Path(source).read_bytes() == original_bytes

    artifacts = artifacts_from_invocations(
        run_id, list(task.context.get("tool_invocations") or []), fallback_created_at="t"
    )
    sheets = [a for a in artifacts if a["kind"] == "spreadsheet_xlsx"]
    assert len(sheets) == 1, [a["kind"] for a in artifacts]
    art = sheets[0]
    assert art["format"] == "xlsx"
    assert art["title"] == "book.edited.xlsx"
    assert art["content"] == ""  # the bytes are NOT in the payload
    assert art["content_ref"]
    assert art["diff"]["modified"] >= 1
    assert art["diff"]["numeric_changes"] == 1  # 3 → 5 is one numeric change

    # Downloadable, byte-for-byte, as a real edited XLSX. The id embeds the
    # content-addressed ``content_ref`` (which contains a ``/``), so it is sent
    # percent-encoded exactly as the frontend does (``artifactDownloadUrl``).
    _save_run_with_artifacts(run_id, artifacts)
    resp = _client().get(f"/runs/{run_id}/artifacts/{quote(art['id'], safe='')}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml"
    )
    assert "attachment" in resp.headers.get("content-disposition", "")

    import openpyxl

    edited = openpyxl.load_workbook(io.BytesIO(resp.content))
    assert edited.active["B2"].value == 5
    assert edited.active["A2"].value == "apple"


# --------------------------------------------------------------------------- #
# 2. PDF — declared pdf.generate → re-parseable downloadable new PDF            #
# --------------------------------------------------------------------------- #
async def test_declared_pdf_generate_produces_a_parseable_downloadable_pdf(
    force_memory_backend, tmp_doc_store, blob_root
):
    pytest.importorskip("pypdf")
    from forgeflow.resources.summaries import content_kind

    record = await _register("report.pdf", _pdf_bytes())
    assert record.status == "parsed"
    assert content_kind("report.pdf") == "pdf"

    ctx = RequestContext(tenant_id=TENANT, user_id="u", role="manager")
    task = TaskCreate(
        intent="生成一份 PDF",
        context={
            "resources": [record.id],
            "declared_tools": ["pdf.generate"],
            "spec": {"title": "季度报告", "paragraphs": ["营收 42 万元", "成本 10 万元"]},
        },
    )
    run_id = "run-inc45-pdf-e2e"
    steps, errors = await orch._default_executor(task, ctx, _Bus(), run_id)

    assert errors == []
    by_tool = {s["tool"]: s["status"] for s in steps}
    assert by_tool["pdf.inspect"] == "ok"
    assert by_tool["pdf.generate"] == "ok"
    assert by_tool["artifact.save"] == "ok"

    artifacts = artifacts_from_invocations(
        run_id, list(task.context.get("tool_invocations") or []), fallback_created_at="t"
    )
    pdfs = [a for a in artifacts if a["kind"] == "pdf_document"]
    assert len(pdfs) == 1, [a["kind"] for a in artifacts]
    art = pdfs[0]
    assert art["format"] == "pdf"
    assert art["title"] == "report.generated.pdf"
    assert art["content"] == ""  # the bytes are NOT in the payload
    assert art["content_ref"]

    _save_run_with_artifacts(run_id, artifacts)
    resp = _client().get(f"/runs/{run_id}/artifacts/{quote(art['id'], safe='')}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert "attachment" in resp.headers.get("content-disposition", "")

    # The delivered PDF really re-parses to a non-empty page set (no empty PDF).
    from forgeflow.multimodal.pdf import extract_pdf_text

    document = extract_pdf_text(resp.content)
    assert document.page_count >= 1
    assert len(document.text) > 0


# --------------------------------------------------------------------------- #
# 3. Verbatim passthrough contract (what ``deriveArtifacts`` reads)            #
# --------------------------------------------------------------------------- #
def test_deliverable_artifacts_carry_the_fields_the_frontend_reads():
    """Both deliverables are small, JSON-safe dicts riding ``detail.artifacts``.

    ``frontend/src/views/runs/realRun.ts::deriveArtifacts`` returns ``detail.artifacts
    ?? []`` (verbatim), and ``ArtifactPanel`` / ``ChatArtifactChip`` read
    ``title || kind`` + download via ``content_ref``. So the backend contract that
    makes INC45 a zero-frontend-change iteration is exactly: a JSON-safe dict with
    those keys, and an empty ``content`` (the bytes live behind ``content_ref``).
    """
    trail = [
        {
            "tool": "sheet.edit",
            "status": "ok",
            "result_ref": "ab/x.xlsx",
            "started_at": "t",
            "payload": {
                "ok": True,
                "format": "xlsx",
                "filename": "book.edited.xlsx",
                "artifact_ref": "ab/x.xlsx",
                "modified": 1,
            },
        },
        {
            "tool": "pdf.generate",
            "status": "ok",
            "result_ref": "cd/y.pdf",
            "started_at": "t",
            "payload": {
                "ok": True,
                "format": "pdf",
                "filename": "report.generated.pdf",
                "artifact_ref": "cd/y.pdf",
            },
        },
    ]
    arts = artifacts_from_invocations("run-x", trail, fallback_created_at="t")
    assert {a["kind"] for a in arts} == {"spreadsheet_xlsx", "pdf_document"}

    for art in arts:
        for key in ("id", "kind", "title", "format", "content", "content_ref", "diff"):
            assert key in art, key
        assert art["content"] == ""  # bytes are NEVER in the payload
        assert art["content_ref"]
        json.dumps(art)  # ⇒ rides ``detail.artifacts`` verbatim over JSON
