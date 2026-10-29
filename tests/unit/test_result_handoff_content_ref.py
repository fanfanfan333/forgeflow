"""Cross-project handoff must resolve ``content_ref`` deliverables.

Regression for the real-stack failure at c9015cb: a source run that
delivered a document carried ``content=""`` with the bytes behind
``content_ref`` (see ``runtime/artifacts.py::_deliverable_artifact``),
so the original builder skipped every real deliverable and derived
``[]`` — the handoff looked "not passed" while unit tests with inline
content passed. These tests store real bytes in the document store
and assert they reach the handoff payload, in both the codeplane
builder and the continued-context paragraph.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from forgeflow.config import get_settings
from forgeflow.documents.store import DocArtifactStore
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.result_handoff import build_handoff_results


def _record_with_ref(ref: str, *, fmt: str = "text", title: str = "报告") -> SimpleNamespace:
    return SimpleNamespace(
        run_id="src-ref",
        tenant_id="t1",
        actor_user_id="u1",
        intent="写一份报告",
        outcome="success",
        artifacts=[
            {
                "title": title,
                "kind": "document",
                "format": fmt,
                "content": "",
                "content_ref": ref,
                "result_ref": "ref-1",
            }
        ],
        llm={},
        codeplane={},
    )


def test_content_ref_text_deliverable_reaches_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path))
    ref = DocArtifactStore().put("真实文档正文".encode(), ".txt")
    items = build_handoff_results(_record_with_ref(ref), tenant_id="t1", user_id="u1")
    assert len(items) == 1
    assert items[0]["kind"] == "document"
    assert items[0]["content"] == "真实文档正文"
    assert "src-ref" in items[0]["source"]


def test_content_ref_docx_deliverable_reaches_handoff(tmp_path, monkeypatch):
    from docx import Document

    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path))
    doc = Document()
    doc.add_paragraph("Word 段落正文")
    buffer = io.BytesIO()
    doc.save(buffer)
    ref = DocArtifactStore().put(buffer.getvalue(), ".docx")
    items = build_handoff_results(
        _record_with_ref(ref, fmt="docx", title="报告.docx"), tenant_id="t1", user_id="u1"
    )
    assert len(items) == 1
    assert "Word 段落正文" in items[0]["content"]


def test_missing_or_unsupported_content_ref_is_honestly_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path))
    # A ref that was never stored, and a format with no extractor.
    assert build_handoff_results(_record_with_ref("aa/nonexistent.txt")) == []
    ref = DocArtifactStore().put(b"\x00\x01\x02", ".bin")
    assert build_handoff_results(_record_with_ref(ref, fmt="bin")) == []


def test_continued_context_resolves_content_ref_too(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path))
    ref = DocArtifactStore().put("续聊文档正文".encode(), ".txt")
    text = orch._continued_context_text(_record_with_ref(ref))
    assert "续聊文档正文" in text
    assert "（无正文）" not in text
