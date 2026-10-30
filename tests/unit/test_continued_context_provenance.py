"""Continued-context provenance must be visible in the run record.

2026-10-30 handoff retest: follow-up runs were partial and the run
detail did not prove what prior context the follow-up saw, or whether
a named artifact fingerprint was missing — the paragraph only entered
the planner prompt. ``llm["continued_context"]`` now records it.
"""

from types import SimpleNamespace

from forgeflow.runtime import orchestrator as orch


def _parent(artifacts):
    return SimpleNamespace(
        run_id="parent-1",
        intent="写规范文档",
        outcome="success",
        llm={"final_answer": {"text": "add 规范"}},
        artifacts=artifacts,
        codeplane={},
    )


def _art(title, ref):
    return {"title": title, "result_ref": ref, "content_ref": "", "content": "正文"}


def test_provenance_includes_matched_artifact_only(monkeypatch):
    parent = _parent([_art("规范", "ref-a"), _art("其他", "ref-b")])
    monkeypatch.setattr(orch, "_continued_parent", lambda ctx: parent)
    monkeypatch.setattr(orch, "_resolve_continued_context", lambda ctx: "prior")
    prov = orch._continued_context_provenance(
        {"continued_from_run_id": "parent-1", "continued_from_artifact_ref": "ref-b"}
    )
    assert prov is not None
    assert prov["parent_run_id"] == "parent-1"
    assert prov["artifact_ref"] == "ref-b"
    assert prov["missing_artifact_ref"] is False
    assert [i["result_ref"] for i in prov["included"]] == ["ref-b"]
    assert prov["parent_artifact_count"] == 2


def test_provenance_reports_missing_fingerprint(monkeypatch):
    parent = _parent([_art("规范", "ref-a")])
    monkeypatch.setattr(orch, "_continued_parent", lambda ctx: parent)
    monkeypatch.setattr(orch, "_resolve_continued_context", lambda ctx: "prior")
    prov = orch._continued_context_provenance(
        {"continued_from_run_id": "parent-1", "continued_from_artifact_ref": "ref-missing"}
    )
    assert prov is not None
    assert prov["missing_artifact_ref"] is True
    assert prov["included"] == []


def test_provenance_none_for_plain_task():
    assert orch._continued_context_provenance({}) is None
    assert orch._continued_context_provenance({"continued_from_run_id": ""}) is None
