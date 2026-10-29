"""Cross-project result passing: server-derived handoff, not hand-crafted.

A new task in another session / project names only ``handoff_from_run_id``;
the payload must come from the source run's real recorded results, be
tenant/user-scoped, and stay inside the existing validator's caps.
"""

from types import SimpleNamespace

import pytest

from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.orchestrator import TaskCreate, _codeplane_args, _continued_context_text
from forgeflow.runtime.result_handoff import build_handoff_results


def _record(**overrides):
    base = {
        "run_id": "src-1",
        "tenant_id": "t1",
        "actor_user_id": "u1",
        "intent": "写一份调研",
        "outcome": "success",
        "artifacts": [
            {"title": "报告", "kind": "document", "result_ref": "ref-1", "content": "报告正文"},
            {"title": "数据", "kind": "document", "result_ref": "ref-2", "content": "数据正文"},
        ],
        "llm": {"final_answer": {"text": "最终结论"}},
        "codeplane": {"diff": "diff --git a/x b/x"},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_handoff_uses_real_results_and_validates():
    items = build_handoff_results(_record(), tenant_id="t1", user_id="u1")
    assert len(items) == 4  # 2 artifacts + final answer + diff
    assert items[0]["kind"] == "document" and items[0]["content"] == "报告正文"
    assert "src-1" in items[0]["source"]
    assert items[2]["kind"] == "research" and items[2]["content"] == "最终结论"
    assert items[3]["content"].startswith("diff --git")


def test_build_handoff_empty_source_is_honestly_empty():
    assert build_handoff_results(_record(artifacts=[], llm={}, codeplane={})) == []


def test_build_handoff_foreign_tenant_or_user_fails_closed():
    with pytest.raises(ValueError):
        build_handoff_results(_record(), tenant_id="other", user_id="u1")
    with pytest.raises(ValueError):
        build_handoff_results(_record(), tenant_id="t1", user_id="other")


def test_build_handoff_truncates_and_marks_truncated():
    items = build_handoff_results(_record(artifacts=[{"title": "长", "content": "x" * 20000}]))
    assert items[0]["content"] and len(items[0]["content"]) <= 12000
    assert items[0]["truncated"] is True


def test_codeplane_args_derives_handoff_from_source_run(monkeypatch):
    record = _record()
    monkeypatch.setattr(orch, "_scoped_parent", lambda run_id: record)
    task = TaskCreate(intent="用上一个项目的结果改代码", context={"handoff_from_run_id": "src-1"})
    ctx = orch.RequestContext(tenant_id="t1", user_id="u1", role="admin")
    args = _codeplane_args(task, ctx)
    assert args["handoff_results"][0]["content"] == "报告正文"


def test_codeplane_args_explicit_handoff_wins_over_derived(monkeypatch):
    monkeypatch.setattr(orch, "_scoped_parent", lambda run_id: _record())
    explicit = [{"kind": "research", "source": "manual", "content": "手动"}]
    task = TaskCreate(
        intent="x",
        context={"handoff_results": explicit, "handoff_from_run_id": "src-1"},
    )
    assert _codeplane_args(task)["handoff_results"] == explicit


def test_codeplane_args_missing_source_fails_closed(monkeypatch):
    monkeypatch.setattr(orch, "_scoped_parent", lambda run_id: None)
    task = TaskCreate(intent="x", context={"handoff_from_run_id": "nope"})
    with pytest.raises(ValueError):
        _codeplane_args(task)


def test_continued_context_carries_more_than_a_snippet_and_diff():
    text = _continued_context_text(_record(
        artifacts=[{"title": "报告", "result_ref": "ref-1", "content": "y" * 1000}],
        llm={"final_answer": {"text": "z" * 3000}},
    ))
    # The old 240-char artifact snippet and the missing diff were the
    # cross-project loss; both must now be visible.
    assert "y" * 1000 in text
    assert "diff --git" in text
    assert "ref-1" in text
