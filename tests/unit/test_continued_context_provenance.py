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


def _rich_parent(artifacts, answer_text=""):
    return SimpleNamespace(
        run_id="parent-1",
        intent="写规范文档",
        outcome="success",
        llm={"final_answer": {"text": answer_text}} if answer_text else {},
        artifacts=artifacts,
        codeplane={},
    )


def test_missing_fingerprint_paragraph_carries_no_parent_answer_text():
    # 2026-11-01 fingerprint inversion (missing case): the paragraph
    # always carried the parent's final-answer excerpt, which quoted
    # the very document the missing gate had just withheld — so a
    # "missing" fingerprint still yielded the document text.
    parent = _rich_parent(
        [_art("规范", "ref-a")], answer_text="父轮答复引用了机密文档正文XYZ"
    )
    text = orch._continued_context_text(parent, artifact_ref="ref-missing")
    assert "未在上一轮找到" in text
    assert "机密文档正文XYZ" not in text
    assert "正文" not in text


def test_matched_fingerprint_keeps_answer_and_snippet():
    parent = _rich_parent(
        [_art("规范", "ref-a"), _art("其他", "ref-b")], answer_text="父轮摘要"
    )
    text = orch._continued_context_text(parent, artifact_ref="ref-b")
    assert "父轮摘要" in text
    assert "正文" in text
    assert "未在上一轮找到" not in text


def test_scoped_chain_artifact_in_another_parent_has_no_not_found_line():
    # Provenance aggregates the chain (included if ANY parent holds the
    # artifact); the paragraph must not contradict it with a per-parent
    # "未找到" line from a parent that merely lacks it.
    from forgeflow.runtime.input_scope import InputScope, current_input_scope

    direct = _rich_parent([_art("规范", "ref-a")])
    direct.run_id = "parent-1"
    older = _rich_parent([_art("目标", "ref-x")])
    older.run_id = "parent-0"
    token = current_input_scope.set(
        InputScope("t1", {}, [direct, older], {})
    )
    try:
        text = orch._resolve_continued_context(
            {"continued_from_run_id": "parent-1", "continued_from_artifact_ref": "ref-x"}
        )
    finally:
        current_input_scope.reset(token)
    assert "未在上一轮找到" not in text
    assert "正文" in text


def test_artifact_text_falls_back_to_content_ref_extension(tmp_path, monkeypatch):
    # Format metadata absent while the bytes are intact: the
    # content-addressed ref itself carries the real extension.
    from forgeflow.config import get_settings
    from forgeflow.documents.store import DocArtifactStore

    monkeypatch.setattr(get_settings(), "resource_store_root", str(tmp_path))
    ref = DocArtifactStore().put("扩展名推断正文".encode(), ".txt")
    parent = _rich_parent(
        [{"title": "报告", "result_ref": "ref-t", "content_ref": ref, "format": ""}]
    )
    text = orch._continued_context_text(parent, artifact_ref="ref-t")
    assert "扩展名推断正文" in text
    assert "（无正文）" not in text
