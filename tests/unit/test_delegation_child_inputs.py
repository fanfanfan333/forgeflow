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
