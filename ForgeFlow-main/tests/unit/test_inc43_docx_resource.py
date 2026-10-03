"""INC43 T04-fix — the ``.docx`` **resource seam** end-to-end nails.

Why this file exists
--------------------
``tests/unit/test_inc43_docx_edit.py`` pinned the document *plane* (the handlers
and the projection). It also honestly recorded that the resource signal was only
*forward-compatible*: ``resources.summaries`` had no ``.docx`` extension, so a
``.docx`` could not be registered as a FILE at all — an upload would be rejected,
and a bare whitelist entry would have produced the worse state "上传成功但文档平面
静默不触发". This file nails the four additive changes that close that seam so the
real path「上传 .docx → 自然语言改 → 后端走文档平面写新文件 → 返回 Artifact」is
reachable and load-bearing:

  1. ``resources.summaries`` — ``.docx`` is a supported FILE whose ``content_kind``
     is ``document`` and whose bytes are really parsed (measured structure), while
     ``.xlsx`` stays a table. (INC44 §9-2 — ``.pptx`` joined the ``document`` kind
     in INC44, so the two former "pptx is unsupported" pins below were
     intentionally updated to the new truth.)
  2. ``resources.service.resolve_task_inputs`` — a registered ``.docx`` additionally
     dereferences to ``document_paths`` (additive: ``paths`` is unchanged);
  3. ``runtime.orchestrator`` — ``_is_document_task`` keys on that real signal and
     ``_document_args`` hands it to the tools;
  4. ``runtime.tool_handlers._document_target`` — prefers ``document_paths`` (a
     content-addressed path has **no** extension and is invisible to the ``.docx``
     suffix scan) and falls back to the caller's explicit ``.docx`` path.

Honesty rules pinned here: the summary never fabricates a count (unreadable bytes
degrade to ``metadata_only``, never 5xx); the preview of a ``.docx`` is an honest
empty state; and the DOCX bytes never ride in the run payload (the artifact
carries ``content_ref`` only).
"""

from __future__ import annotations

import io
import os
from types import SimpleNamespace

import pytest

from forgeflow.repositories.memory.resource_repo import clear_resource_store
from forgeflow.resources import summaries
from forgeflow.resources.service import ResourceService, reset_resource_index
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.artifacts import ARTIFACT_KIND_DOCX, artifacts_from_invocations
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Builders / fixtures                                                          #
# --------------------------------------------------------------------------- #
def _docx_bytes(*, with_table: bool = True) -> bytes:
    """The same known DOCX used by the plane pins: 2 headings + 2 body paras + table.

    Exactly 5 paragraphs (``第一章`` body has two paragraphs), 2 headings, 2
    sections and 1 table — so the measured counts below are checkable.
    """
    from docx import Document

    document = Document()
    document.add_heading("第一章 概述", level=1)
    document.add_paragraph("总金额 40 元。")
    document.add_paragraph("备注：请核对。")
    document.add_heading("第二章 明细", level=1)
    document.add_paragraph("单价 5 元。")
    if with_table:
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "A"
        table.cell(0, 1).text = "B"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _paragraph_texts(data: bytes) -> list[str]:
    from docx import Document

    return [p.text for p in Document(io.BytesIO(data)).paragraphs]


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="run-inc43-res",
        step_id="run-inc43-res:0:0",
        tenant_id="t-inc43-res",
        user_id="u-inc43-res",
        role="manager",
        args={},
    )


@pytest.fixture
def memory_resources(force_memory_backend):
    """A clean in-memory resource repository + dereference index for one test."""
    reset_resource_index()
    clear_resource_store()
    yield
    reset_resource_index()
    clear_resource_store()


@pytest.fixture
def doc_blob_root(monkeypatch, tmp_path):
    """Point the default ``FileBlobStore`` (used by ``ResourceService()`` **and** by
    ``orchestrator._resolve_resource_inputs``) at a hermetic tmp root.

    ``FileBlobStore`` refuses a root inside the project tree, so ``tmp_path`` (the
    system temp) is the correct home: both the registered record and the planner's
    dereference then agree on the same content-addressed path.
    """
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path / "res-blobs"))
    return tmp_path / "res-blobs"


@pytest.fixture
def tmp_doc_store(tmp_path, monkeypatch):
    """Point the handler's DOCX blob store at a tmp dir (keeps the suite hermetic)."""
    from forgeflow.documents import store as store_mod

    real = store_mod.DocArtifactStore

    def _factory(root=None):
        return real(root=root if root is not None else tmp_path / "docstore")

    import forgeflow.documents as docs_pkg

    monkeypatch.setattr(store_mod, "DocArtifactStore", _factory)
    monkeypatch.setattr(docs_pkg, "DocArtifactStore", _factory)
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. resources.summaries — ``.docx`` is a real, parsed document FILE           #
# --------------------------------------------------------------------------- #
async def test_docx_is_a_supported_document_resource():
    assert summaries.content_kind("report.docx") == "document"
    assert summaries.is_supported_file("report.docx") is True
    assert ".docx" in summaries.SUPPORTED_FILE_EXTENSIONS
    # Case-insensitive suffix, like every other kind.
    assert summaries.content_kind("REPORT.DOCX") == "document"
    assert summaries.mime_for("report.docx").endswith("wordprocessingml.document")


async def test_pptx_is_a_document_and_xlsx_stays_a_table():
    """INC44 §9-2 — ``.pptx`` is now a ``document``; the fix stays narrow.

    This *supersedes* the INC43 pin ``test_pptx_stays_unsupported_and_xlsx_stays_a_table``:
    INC43 deliberately supported only DOCX, and INC44 extends the document kind
    to PPTX (user-approved). ``.xlsx`` must **still** be an *excel* (table)
    resource, never a document.
    """
    assert summaries.is_supported_file("slides.pptx") is True
    assert summaries.content_kind("slides.pptx") == "document"
    assert ".pptx" in summaries.SUPPORTED_FILE_EXTENSIONS
    assert summaries.mime_for("slides.pptx").endswith("presentationml.presentation")
    # ``.xlsx`` must remain an *excel* (table) resource, never a document.
    assert summaries.content_kind("book.xlsx") == "excel"


async def test_summarize_bytes_parses_a_real_docx_with_measured_counts():
    data = _docx_bytes()
    status, summary, detail = summaries.summarize_bytes(data, filename="report.docx")

    assert status == "parsed"
    assert detail == ""
    quality = summary.quality
    # Real, measured structure — not a fabricated placeholder.
    assert quality["paragraphs"] == 5
    assert quality["headings"] == 2
    assert quality["sections"] == 2
    assert quality["tables"] == 1
    assert summary.chars > 0


async def test_summarize_bytes_degrades_to_metadata_only_on_unreadable_docx():
    """A ``.docx`` name over non-DOCX bytes must never 5xx or fabricate a count."""
    for raw in (b"not a zip at all", b""):
        status, summary, detail = summaries.summarize_bytes(raw, filename="broken.docx")
        assert status == "metadata_only"
        assert summary.note == "unparseable docx"
        assert detail  # the verbatim reason is surfaced
        assert summary.chars is None  # unmeasured ⇒ None, never a fake 0


# --------------------------------------------------------------------------- #
# 2. resources.service — additive ``document_paths`` dereference               #
# --------------------------------------------------------------------------- #
async def test_resolve_task_inputs_adds_document_paths(memory_resources, doc_blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc43", name="report.docx", data=_docx_bytes())
    assert record.status == "parsed"

    resolved = service.resolve_task_inputs({"resources": [record.id]})

    assert "document_paths" in resolved
    document_path = resolved["document_paths"][0]
    assert os.path.isfile(document_path)
    # Content-addressed ⇒ extensionless (this is why the suffix scan misses it).
    assert os.path.splitext(document_path)[1] == ""
    # Additive: ``paths`` still carries the file path (code/analysis seams unchanged).
    assert resolved["paths"] == [document_path]


async def test_resolve_task_inputs_adds_index_aligned_document_names(
    memory_resources, doc_blob_root
):
    """The registered original name rides alongside its path, index-aligned."""
    service = ResourceService()
    record = await service.register_file("t-inc43", name="报告.docx", data=_docx_bytes())

    resolved = service.resolve_task_inputs({"resources": [record.id]})

    assert resolved["document_names"] == ["报告.docx"]
    # Same loop, same guard ⇒ the two lists are item-for-item aligned.
    assert len(resolved["document_names"]) == len(resolved["document_paths"])


async def test_resolve_task_inputs_omits_document_paths_for_a_csv(
    memory_resources, doc_blob_root
):
    service = ResourceService()
    record = await service.register_file(
        "t-inc43", name="data.csv", data=b"a,b\n1,2\n"
    )

    resolved = service.resolve_task_inputs({"resources": [record.id]})

    assert "document_paths" not in resolved
    assert "document_names" not in resolved  # counter-proof: no phantom names
    assert resolved.get("paths")  # a CSV FILE still dereferences to a path


async def test_preview_of_a_docx_is_an_honest_empty_state(memory_resources, doc_blob_root):
    service = ResourceService()
    record = await service.register_file("t-inc43", name="report.docx", data=_docx_bytes())

    preview = await service.preview("t-inc43", record.id)

    assert preview["available"] is False
    assert preview["format"] == "document"
    assert preview["content"] == ""
    assert preview["note"]  # an honest note, never invented content


async def test_limits_lists_docx_as_supported():
    extensions = ResourceService().limits()["supported_extensions"]
    assert ".docx" in extensions
    # INC44 §9-2 — ``.pptx`` is now advertised as supported (it joined the
    # document kind); the endpoint reads the same single source of truth.
    assert ".pptx" in extensions


# --------------------------------------------------------------------------- #
# 3. runtime.orchestrator — the resource signal now drives routing            #
# --------------------------------------------------------------------------- #
async def test_registered_docx_resource_drives_the_document_plane(
    memory_resources, doc_blob_root
):
    service = ResourceService()
    record = await service.register_file("t-inc43", name="report.docx", data=_docx_bytes())

    ctx = RequestContext(tenant_id="t-inc43", user_id="u", role="manager")
    task = TaskCreate(intent="把文档里的金额改一下", context={"resources": [record.id]})

    # The resource seam (document_paths) is now load-bearing, not a guess.
    assert orch._is_document_task(task, ctx) is True
    assert orch._is_analysis_task(task, ctx) is False
    assert orch._is_code_task(task, ctx) is False


async def test_registered_csv_resource_stays_analysis_not_document(
    memory_resources, doc_blob_root
):
    service = ResourceService()
    record = await service.register_file("t-inc43", name="data.csv", data=b"a,b\n1,2\n")

    ctx = RequestContext(tenant_id="t-inc43", user_id="u", role="manager")
    task = TaskCreate(intent="分析这份数据", context={"resources": [record.id]})

    assert orch._is_document_task(task, ctx) is False
    assert orch._is_analysis_task(task, ctx) is True


async def test_document_args_surface_the_resource_document_paths(
    memory_resources, doc_blob_root
):
    service = ResourceService()
    record = await service.register_file("t-inc43", name="report.docx", data=_docx_bytes())
    resolved = service.resolve_task_inputs({"resources": [record.id]})

    ctx = RequestContext(tenant_id="t-inc43", user_id="u", role="manager")
    task = TaskCreate(intent="改文档", context={"resources": [record.id]})
    args = orch._document_args(task, orch._capability_context(task, ctx))

    assert args["document_paths"] == resolved["document_paths"]


# --------------------------------------------------------------------------- #
# 4. runtime.tool_handlers._document_target — extensionless-aware target       #
# --------------------------------------------------------------------------- #
async def test_document_target_prefers_document_paths_over_paths(tmp_path):
    from forgeflow.runtime.tool_handlers import _document_target

    # A content-addressed-like target: a real DOCX at an *extensionless* path.
    target = tmp_path / "ab" / "abcdef0123456789"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_docx_bytes())

    args = {
        "document_paths": [str(target)],
        "paths": [str(tmp_path / "decoy.docx")],  # a decoy that is not a real file
    }
    assert _document_target(args) == str(target)


async def test_document_target_falls_back_to_a_docx_path(tmp_path):
    from forgeflow.runtime.tool_handlers import _document_target

    source = tmp_path / "report.docx"
    source.write_bytes(_docx_bytes())

    assert _document_target({"paths": [str(source)]}) == str(source)
    # Nothing readable ⇒ ``""`` (never a guess).
    assert _document_target({}) == ""
    assert _document_target({"document_paths": [str(tmp_path / "gone")]}) == ""


async def test_document_inspect_accepts_an_extensionless_document_path(tmp_path):
    from forgeflow.runtime.tool_handlers import document_inspect

    target = tmp_path / "cd" / "deadbeef"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_docx_bytes())

    result = await document_inspect({"document_paths": [str(target)]}, _ctx())

    assert result["ok"] is True
    assert result["structure"]["tables"] == 1


# --------------------------------------------------------------------------- #
# 5. End-to-end shape — upload → resolve → inspect → edit → save → project     #
# --------------------------------------------------------------------------- #
async def test_upload_edit_artifact_end_to_end(
    memory_resources, doc_blob_root, tmp_doc_store
):
    from docx import Document

    from forgeflow.documents import DocArtifactStore
    from forgeflow.runtime.tool_handlers import (
        artifact_save,
        document_edit,
        document_inspect,
    )

    # -- ① upload: a real ``.docx`` is registered as a parsed FILE resource ------
    service = ResourceService()
    record = await service.register_file("t-inc43", name="报告.docx", data=_docx_bytes())
    assert record.status == "parsed"
    assert record.summary.quality["tables"] == 1

    # -- ② resolve: the seam hands the document plane a real, readable target ----
    resolved = service.resolve_task_inputs({"resources": [record.id]})
    document_paths = resolved["document_paths"]
    assert document_paths and os.path.isfile(document_paths[0])

    # -- ③ route + assemble args (change 3) -------------------------------------
    ctx = RequestContext(tenant_id="t-inc43", user_id="u", role="manager")
    task = TaskCreate(
        intent="把文档金额从 40 改成 38",
        context={"resources": [record.id]},
    )
    assert orch._is_document_task(task, ctx) is True
    doc_args = orch._document_args(task, orch._capability_context(task, ctx))
    assert doc_args["document_paths"] == document_paths
    assert doc_args["document_names"] == ["报告.docx"]  # the original name survives
    target = doc_args["document_paths"][0]

    # -- ④ inspect: the extensionless path is honoured (change 4) ---------------
    inspected = await document_inspect({"document_paths": [target]}, _ctx())
    assert inspected["ok"] is True
    assert inspected["structure"]["tables"] == 1

    # -- ⑤ edit: the Tool layer really writes a new DOCX ------------------------
    edited = await document_edit(
        {**doc_args, "edits": [{"op": "replace_text", "match": "40", "replace": "38"}]},
        _ctx(),
    )
    assert edited["ok"] is True
    # The deliverable keeps the user's **real** file name (the registered original),
    # not the content-addressed path's hash.
    assert edited["filename"] == "报告.edited.docx"
    assert edited["artifact_ref"]
    assert "content" not in edited  # bytes never ride in the payload

    # -- ⑥ save: register the deliverable from the run's own evidence trail -----
    inspected_inv = {"tool": "document.inspect", "status": "ok", "payload": inspected}
    edit_inv = {
        "tool": "document.edit",
        "status": "ok",
        "result_ref": edited["artifact_ref"],
        "started_at": "2026-10-02T00:00:00",
        "payload": edited,
    }
    saved = await artifact_save(
        {"observations": [inspected_inv, edit_inv]}, _ctx()
    )
    assert saved["ok"] is True
    assert saved["artifact_ref"] == edited["artifact_ref"]

    # -- ⑦ project: exactly one DOCX artifact carrying ``content_ref`` ----------
    save_inv = {
        "tool": "artifact.save",
        "status": "ok",
        "result_ref": saved["artifact_ref"],
        "started_at": "2026-10-02T00:00:01",
        "payload": saved,
    }
    artifacts = artifacts_from_invocations(
        "run-inc43-e2e", [inspected_inv, edit_inv, save_inv]
    )
    assert [a["kind"] for a in artifacts] == [ARTIFACT_KIND_DOCX]
    assert artifacts[0]["content"] == ""  # the body is NOT in the payload
    assert artifacts[0]["content_ref"] == edited["artifact_ref"]

    # -- ⑧ the bytes behind ``content_ref`` really are the edited document ------
    blob = DocArtifactStore(root=tmp_doc_store / "docstore").get(
        artifacts[0]["content_ref"]
    )
    assert any("38" in text for text in _paragraph_texts(blob))
    # Sanity: the reopened edited document still parses and keeps its table.
    assert len(Document(io.BytesIO(blob)).tables) == 1


# --------------------------------------------------------------------------- #
# 6. Deliverable name — the original name wins, sanitized + honest fallback     #
# --------------------------------------------------------------------------- #
async def test_safe_docx_stem_is_traversal_safe():
    from forgeflow.runtime.tool_handlers import _safe_docx_stem

    assert _safe_docx_stem("报告.docx") == "报告"
    assert _safe_docx_stem("a/b/报告.docx") == "报告"
    assert _safe_docx_stem("..\\..\\etc\\passwd.docx") == "passwd"
    assert _safe_docx_stem("../../etc/passwd.docx") == "passwd"
    # Nothing safe remains ⇒ "" (the caller then falls back honestly).
    for bad in ("", ".", "..", "...", "/", "///", "..\\.."):
        assert _safe_docx_stem(bad) == ""


async def test_document_edit_uses_the_registered_original_name(tmp_path, tmp_doc_store):
    from forgeflow.runtime.tool_handlers import document_edit

    target = tmp_path / "ab" / "cafebabe0123"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_docx_bytes())

    result = await document_edit(
        {
            "document_paths": [str(target)],
            "document_names": ["报告.docx"],
            "edits": [{"op": "replace_text", "match": "40", "replace": "38"}],
        },
        _ctx(),
    )

    assert result["ok"] is True
    assert result["filename"] == "报告.edited.docx"


async def test_document_edit_falls_back_to_path_name_without_document_names(
    tmp_path, tmp_doc_store
):
    """A caller-supplied ``.docx`` path (no seam name) keeps the old behaviour."""
    from forgeflow.runtime.tool_handlers import document_edit

    source = tmp_path / "report.docx"
    source.write_bytes(_docx_bytes())

    result = await document_edit(
        {
            "paths": [str(source)],
            "edits": [{"op": "replace_text", "match": "40", "replace": "38"}],
        },
        _ctx(),
    )

    assert result["ok"] is True
    assert result["filename"] == "report.edited.docx"
    assert result["filename"].endswith(".edited.docx")


async def test_document_edit_sanitizes_a_traversal_document_name(tmp_path, tmp_doc_store):
    """A hostile ``document_names`` entry must never leak a directory into the name."""
    from forgeflow.runtime.tool_handlers import document_edit

    target = tmp_path / "ab" / "feedface4567"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_docx_bytes())

    result = await document_edit(
        {
            "document_paths": [str(target)],
            "document_names": ["../../etc/passwd.docx"],
            "edits": [{"op": "replace_text", "match": "40", "replace": "38"}],
        },
        _ctx(),
    )

    assert result["ok"] is True
    assert result["filename"] == "passwd.edited.docx"
    assert "/" not in result["filename"]
    assert "\\" not in result["filename"]
    assert ".." not in result["filename"]

