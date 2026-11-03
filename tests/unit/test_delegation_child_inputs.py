"""Delegation children must actually be able to read parent files.

2026-10-31, b32c891 round: research experts failed because they had
no file-plane tool and were never told a file_index existed.
"""

from types import SimpleNamespace

from forgeflow.runtime.delegation import ROLE_TOOLS
from forgeflow.runtime.react_executor import ReactExecutor


def test_research_role_can_read_uploaded_files():
    assert "textfile.inspect" in ROLE_TOOLS["research"]
    assert "document.inspect" in ROLE_TOOLS["research"]


def test_resource_block_discloses_child_context_text_paths():
    task = SimpleNamespace(
        context={
            "text_paths": ["/tmp/specification.txt"],
            "text_names": ["specification.txt"],
            "paths": ["/tmp/specification.txt"],
        }
    )
    block = ReactExecutor._resource_context(task)
    assert "specification.txt" in block
    assert "textfile.inspect" in block
    assert "file_index=0" in block


def test_repeated_successful_delegation_returns_cache_without_cap():
    """cee5814 round: a repeat of the same role on the same files
    must not start a child or consume a delegation slot."""
    import asyncio
    from types import SimpleNamespace

    from forgeflow.runtime import delegation as d
    from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget

    parent_task = SimpleNamespace(context={"resources": ["res1"]})
    ctx = SimpleNamespace(task_context=parent_task, runtime_context=SimpleNamespace())
    d._store_delegation(
        parent_task,
        "research",
        ["res1"],
        {"ok": True, "summary": "add confirmed", "delegation_run_id": "r1"},
    )
    budget = ExecutionBudget()
    budget.delegations = 4  # cap already exhausted
    token = current_budget.set(budget)
    try:
        result = asyncio.run(
            d.delegate(
                {"role": "research", "instruction": "reworded brief", "resources": ["res1"]}, ctx
            )
        )
    finally:
        current_budget.reset(token)
    assert result["ok"] is True
    assert result["already_completed"] is True
    assert result["summary"] == "add confirmed"
    assert budget.delegations == 4  # no new slot consumed


def test_failed_delegation_is_not_cached():
    from types import SimpleNamespace

    from forgeflow.runtime import delegation as d

    parent_task = SimpleNamespace(context={})
    d._store_delegation(parent_task, "research", ["res1"], {"ok": False, "summary": "x"})
    assert d._cached_delegation(parent_task, "research", ["res1"]) is None


def test_child_sees_no_skills_when_brief_names_none():
    """2026-11-02 parent 924858e2: a research child inherited the whole
    parent skill catalogue and burned 3 of its 4 rounds on skills.read
    calls for irrelevant business skills, then died on budget_exceeded
    without answering. A brief that names no skill gives the child none."""
    from forgeflow.runtime.delegation import _brief_skills

    catalogue = [
        {"id": "sk1", "name": "项目复盘总结", "description": "d1", "version": "1", "content_hash": "h1"},
        {"id": "sk2", "name": "客户流失分析", "description": "d2", "version": "1", "content_hash": "h2"},
        {"id": "sk3", "name": "合同审查", "description": "d3", "version": "1", "content_hash": "h3"},
    ]
    assert _brief_skills(catalogue, "请阅读规格文件并总结加法需求") == []


def test_child_keeps_only_brief_named_skill_by_name_or_id():
    from forgeflow.runtime.delegation import _brief_skills

    catalogue = [
        {"id": "sk1", "name": "项目复盘总结", "description": "d1", "version": "1", "content_hash": "h1"},
        {"id": "sk2", "name": "客户流失分析", "description": "d2", "version": "1", "content_hash": "h2"},
        {"id": "sk3", "name": "合同审查", "description": "d3", "version": "1", "content_hash": "h3", "extra": "dropped"},
    ]
    by_name = _brief_skills(catalogue, "请先读客户流失分析，再总结规格")
    assert [s["id"] for s in by_name] == ["sk2"]
    by_id = _brief_skills(catalogue, "请读 sk3 后回答")
    assert [s["id"] for s in by_id] == ["sk3"]
    assert "extra" not in by_id[0]  # only the five carried keys survive
    # A descriptive mention is not an exact name/id match: no fuzzy leak.
    assert _brief_skills(catalogue, "请参考复盘类技能") == []


def test_child_keeps_keyword_matched_skill():
    """Keyword skills: an author-declared keyword in the brief inherits
    the skill, under the same exact-substring discipline as id/name."""
    from forgeflow.runtime.delegation import _brief_skills

    catalogue = [
        {"id": "sk1", "name": "项目复盘总结", "description": "d1", "version": "1", "content_hash": "h1"},
        {"id": "sk2", "name": "客户流失分析", "description": "d2", "version": "1", "content_hash": "h2",
         "keywords": ["客户流失", "churn"]},
        {"id": "sk3", "name": "合同审查", "description": "d3", "version": "1", "content_hash": "h3"},
    ]
    # The full name "客户流失分析" is NOT in the brief; keyword "客户流失" is.
    matched = _brief_skills(catalogue, "请分析客户流失原因")
    assert [s["id"] for s in matched] == ["sk2"]
    assert matched[0]["keywords"] == ["客户流失", "churn"]
    # A descriptive mention still matches nothing without a declared keyword.
    assert _brief_skills(catalogue, "请参考复盘类技能") == []
    # A keyword longer than the mention is not contained: no fuzzy leak.
    assert _brief_skills(catalogue, "请做复盘") == []


def test_keyword_matching_ignores_non_list_and_empty_keywords():
    from forgeflow.runtime.delegation import _brief_skills

    catalogue = [
        {"id": "sk1", "name": "A", "keywords": "客户流失"},  # bare string, not a list
        {"id": "sk2", "name": "B", "keywords": ["", "  "]},
        {"id": "sk3", "name": "C", "keywords": ["客户流失", "", "  "]},
    ]
    assert _brief_skills(catalogue, "请分析客户流失原因") == [
        {"id": "sk3", "name": "C", "keywords": ["客户流失"]}
    ]
