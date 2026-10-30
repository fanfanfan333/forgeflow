"""Typed HandoffPayload: by-reference documents, explicit status, hashes."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from forgeflow.config import get_settings
from forgeflow.documents.store import DocArtifactStore
from forgeflow.runtime.handoff_payload import build_handoff_payload


def _record(**overrides):
    base = {
        "run_id": "src-1",
        "thread_id": "th-1",
        "tenant_id": "t1",
        "actor_user_id": "u1",
        "status": "completed",
        "outcome": "success",
        "errors": [],
        "artifacts": [],
        "llm": {"final_answer": {"text": "最终结论"}},
        "codeplane": {"diff": "diff --git a/x b/x"},
        "created_at": "2026-10-29T00:00:00Z",
        "completed_at": "2026-10-29T01:00:00Z",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_completed_payload_carries_refs_and_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path))
    data = "真实文档正文".encode()
    ref = DocArtifactStore().put(data, ".txt")
    record = _record(
        artifacts=[
            {
                "id": "a1",
                "title": "报告",
                "format": "text",
                "content": "",
                "content_ref": ref,
                "result_ref": "r1",
            }
        ]
    )
    payload = build_handoff_payload(
        record, tenant_id="t1", user_id="u1", target_project_id="proj-b"
    )
    assert payload.status == "completed"
    assert payload.source_run_id == "src-1"
    assert payload.source_thread_id == "th-1"
    assert payload.target_project_id == "proj-b"
    assert len(payload.documents) == 1
    assert payload.documents[0].content_ref == ref
    assert payload.documents[0].sha256 == hashlib.sha256(data).hexdigest()
    assert payload.diff is not None
    assert payload.diff.sha256 == hashlib.sha256(b"diff --git a/x b/x").hexdigest()
    assert payload.payload_hash
    audit = payload.audit_record(target_run_id="tgt-1")
    assert audit["target_run_id"] == "tgt-1"
    assert audit["payload_hash"] == payload.payload_hash


def test_partial_source_is_labelled_partial_not_completed():
    payload = build_handoff_payload(_record(status="failed", outcome="failed"))
    # Usable outputs exist, so it is partial (inspection only), not completed.
    assert payload.status == "partial"
    assert payload.final_answer == "最终结论"


def test_failed_source_carries_only_errors():
    payload = build_handoff_payload(
        _record(
            status="failed",
            outcome="failed",
            artifacts=[],
            llm={},
            codeplane={},
            errors=["搜索失败"],
        )
    )
    assert payload.status == "failed"
    assert payload.documents == []
    assert payload.final_answer == ""
    assert payload.diff is None
    assert payload.errors == ["搜索失败"]


def test_foreign_tenant_fails_closed():
    with pytest.raises(ValueError):
        build_handoff_payload(_record(), tenant_id="other", user_id="u1")


def test_explicit_payload_is_partial_not_completed():
    from forgeflow.runtime.handoff_payload import build_explicit_payload

    payload = build_explicit_payload(
        [{"kind": "document", "source": "caller", "content": "手写正文"}],
        tenant_id="t1",
    )
    assert payload.derived_from == "explicit"
    assert payload.status == "partial"
    assert payload.source_run_id == ""
    assert payload.documents[0].sha256 == hashlib.sha256("手写正文".encode()).hexdigest()


def test_completed_payload_with_missing_document_downgrades_to_partial():
    """A completed source run with an unreadable artifact must not be
    labelled completed — only completed may flow to a commit gate."""
    from types import SimpleNamespace

    from forgeflow.runtime.handoff_payload import build_handoff_payload

    record = SimpleNamespace(
        run_id="r",
        thread_id="th",
        tenant_id="t",
        actor_user_id="u",
        status="completed",
        outcome="success",
        errors=[],
        artifacts=[
            {"title": "坏.docx", "content_ref": "missing-ref", "content": "", "format": "docx"}
        ],
        llm={"final_answer": {"text": "done"}},
        codeplane={},
        completed_at="2026-10-30T00:00:00Z",
        created_at="2026-10-30T00:00:00Z",
    )
    payload = build_handoff_payload(record, tenant_id="t", user_id="u")
    assert payload.status == "partial"
    assert "坏.docx" in payload.missing


def test_failed_payload_names_missing_documents():
    """A failed run with only an unreadable artifact: status failed,
    no documents presented, and the missing artifact is named."""
    from types import SimpleNamespace

    from forgeflow.runtime.handoff_payload import build_handoff_payload

    record = SimpleNamespace(
        run_id="r",
        thread_id="th",
        tenant_id="t",
        actor_user_id="u",
        status="failed",
        outcome="failure",
        errors=["boom"],
        artifacts=[
            {"title": "坏.docx", "content_ref": "missing-ref", "content": "", "format": "docx"}
        ],
        llm={},
        codeplane={},
        completed_at="2026-10-30T00:00:00Z",
        created_at="2026-10-30T00:00:00Z",
    )
    payload = build_handoff_payload(record, tenant_id="t", user_id="u")
    assert payload.status == "failed"
    assert payload.documents == []
    assert payload.errors == ["boom"]
    assert "坏.docx" in payload.missing
