"""INC44 T05 — end-to-end: a registered PPTX / text file is really edited and the
new deliverable is downloadable (memory profile, deterministic, offline).

Design §7 T05 criterion ①: *上传 pptx → 改 → 文档面写新文件 → 返回可下载 artifact；
上传 ``.py`` → 文本面编辑 → artifact；两条产物均能被 ``deriveArtifacts`` 逐字透传*.

What this nails down
--------------------
1. **Registration is load-bearing** — ``ResourceService.register_file`` really parses
   the upload (``status == "parsed"``) and the resource seam dereferences it into
   ``document_paths`` (pptx) / ``text_paths`` (``.py``), which is what drives the plane.
2. **The plane really writes a new file** — ``document.edit`` / ``textfile.edit``
   produce a fresh, content-addressed ``artifact_ref`` whose bytes are a *real*,
   *edited* deliverable (reopened with the same reader).
3. **The deliverable is downloadable** — the projected artifact (``document_pptx`` /
   ``text_file``) is served by ``api.routers.runs.download_artifact`` byte-for-byte,
   with the right media type.
4. **Verbatim passthrough contract** — the artifact dict carries exactly the fields
   ``frontend/src/views/runs/realRun.ts::deriveArtifacts`` (``return detail.artifacts
   ?? []``) reads, and is JSON-safe, so the frontend shows / downloads it with **zero
   frontend changes** (the INC44 posture — §9-6).

The execution is the deterministic offline one (explicit ``edits``): no model, no
network, no LLM. The natural-language → ops layer is pinned separately at the unit
level (``test_inc44_pptx_edit.py`` / ``test_inc44_textfile_edit.py``).

Citation discipline: ``file.py::symbol`` anchors, never ``file:line``.
"""

from __future__ import annotations

import io
import json
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

TENANT = "t-inc44-e2e"


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


def _pptx_bytes() -> bytes:
    """A real PPTX: one title-and-content slide carrying a number."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only layout
    slide.shapes.title.text = "季度汇报 2026"
    box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
    box.text_frame.text = "营收 40 万元"
    buffer = io.BytesIO()
    prs.save(buffer)
    return buffer.getvalue()


def _py_bytes() -> bytes:
    """A real Python source file."""
    return b"def add(a, b):\n    return a + b\n\n\nprint(add(1, 2))\n"


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
        created_at="2026-10-04T00:00:00+00:00",
        completed_at="2026-10-04T00:00:01+00:00",
        session_id=run_id,
    )
    record.artifacts = list(artifacts)
    get_run_store().save(record)


# --------------------------------------------------------------------------- #
# 1. PPTX — upload → edit → downloadable deliverable                           #
# --------------------------------------------------------------------------- #
async def test_pptx_registered_edited_and_downloaded(force_memory_backend, tmp_doc_store):
    from forgeflow.resources.summaries import content_kind

    record = await _register("deck.pptx", _pptx_bytes())
    assert record.status == "parsed"
    assert content_kind("deck.pptx") == "document"

    ctx = RequestContext(tenant_id=TENANT, user_id="u", role="manager")
    task = TaskCreate(
        intent="把营收 40 万元改成 42 万元",
        context={
            "resources": [record.id],
            "declared_tools": ["document.edit"],
            "edits": [{"op": "replace_text", "match": "40", "replace": "42"}],
        },
    )
    run_id = "run-inc44-pptx-e2e"
    steps, errors = await orch._default_executor(task, ctx, _Bus(), run_id)

    assert errors == []
    by_tool = {s["tool"]: s["status"] for s in steps}
    assert by_tool["document.inspect"] == "ok"
    assert by_tool["document.edit"] == "ok"
    assert by_tool["artifact.save"] == "ok"

    artifacts = artifacts_from_invocations(
        run_id, list(task.context.get("tool_invocations") or []), fallback_created_at="t"
    )
    pptx = [a for a in artifacts if a["kind"] == "document_pptx"]
    assert len(pptx) == 1, [a["kind"] for a in artifacts]
    art = pptx[0]
    assert art["format"] == "pptx"
    assert art["title"] == "deck.edited.pptx"
    assert art["content"] == ""  # the bytes are NOT in the payload
    assert art["content_ref"]
    assert art["diff"]["numeric_changes"] == 1

    # Downloadable, byte-for-byte, as a real edited PPTX. The id embeds the
    # content-addressed ``content_ref`` (which contains a ``/``), so it is sent
    # percent-encoded exactly as the frontend does (``artifactDownloadUrl``).
    _save_run_with_artifacts(run_id, artifacts)
    resp = _client().get(f"/runs/{run_id}/artifacts/{quote(art['id'], safe='')}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml"
    )
    assert "attachment" in resp.headers.get("content-disposition", "")

    from forgeflow.documents.pptx_inspect import open_pptx, shape_paragraph_texts

    texts = "\n".join(shape_paragraph_texts(open_pptx(resp.content)))
    assert "42" in texts and "40" not in texts


# --------------------------------------------------------------------------- #
# 2. Text / code — upload → edit → downloadable deliverable                    #
# --------------------------------------------------------------------------- #
async def test_textfile_registered_edited_and_downloaded(force_memory_backend, tmp_doc_store):
    from forgeflow.resources.summaries import content_kind

    record = await _register("app.py", _py_bytes())
    assert record.status == "parsed"
    assert content_kind("app.py") == "text"

    ctx = RequestContext(tenant_id=TENANT, user_id="u", role="manager")
    task = TaskCreate(
        intent="把函数名 add 改成 plus",
        context={
            "resources": [record.id],
            "declared_tools": ["textfile.edit"],
            "edits": [{"op": "replace_text", "match": "add", "replace": "plus"}],
        },
    )
    run_id = "run-inc44-text-e2e"
    steps, errors = await orch._default_executor(task, ctx, _Bus(), run_id)

    assert errors == []
    by_tool = {s["tool"]: s["status"] for s in steps}
    assert by_tool["textfile.inspect"] == "ok"
    assert by_tool["textfile.edit"] == "ok"
    assert by_tool["artifact.save"] == "ok"

    artifacts = artifacts_from_invocations(
        run_id, list(task.context.get("tool_invocations") or []), fallback_created_at="t"
    )
    text = [a for a in artifacts if a["kind"] == "text_file"]
    assert len(text) == 1, [a["kind"] for a in artifacts]
    art = text[0]
    assert art["format"] == "text"
    assert art["title"] == "app.edited.py"
    assert art["content_ref"]

    _save_run_with_artifacts(run_id, artifacts)
    resp = _client().get(f"/runs/{run_id}/artifacts/{quote(art['id'], safe='')}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")

    body = resp.content.decode("utf-8")
    assert "def plus(" in body and "def add(" not in body


# --------------------------------------------------------------------------- #
# 3. Verbatim passthrough contract (what ``deriveArtifacts`` reads)            #
# --------------------------------------------------------------------------- #
def test_deliverable_artifact_carries_the_fields_the_frontend_reads():
    """Both deliverables are small, JSON-safe dicts riding ``detail.artifacts``.

    ``frontend/src/views/runs/realRun.ts::deriveArtifacts`` returns ``detail.artifacts
    ?? []`` (verbatim), and ``ArtifactPanel`` / ``ChatArtifactChip`` read
    ``title || kind`` + download via ``content_ref``. So the backend contract that
    makes INC44 a zero-frontend-change iteration is exactly: a JSON-safe dict with
    those keys, and an empty ``content`` (the bytes live behind ``content_ref``).
    """
    trail = [
        {
            "tool": "document.edit",
            "status": "ok",
            "result_ref": "ab/x.pptx",
            "started_at": "t",
            "payload": {
                "ok": True,
                "format": "pptx",
                "filename": "d.edited.pptx",
                "artifact_ref": "ab/x.pptx",
                "modified": 1,
            },
        },
        {
            "tool": "textfile.edit",
            "status": "ok",
            "result_ref": "cd/y.txt",
            "started_at": "t",
            "payload": {
                "ok": True,
                "format": "text",
                "filename": "a.edited.py",
                "artifact_ref": "cd/y.txt",
                "modified": 1,
            },
        },
    ]
    arts = artifacts_from_invocations("run-x", trail, fallback_created_at="t")
    assert {a["kind"] for a in arts} == {"document_pptx", "text_file"}

    for art in arts:
        for key in ("id", "kind", "title", "format", "content", "content_ref", "diff"):
            assert key in art, key
        assert art["content"] == ""  # bytes are NEVER in the payload
        assert art["content_ref"]
        json.dumps(art)  # ⇒ rides ``detail.artifacts`` verbatim over JSON
