"""2026-11-03 9cc942d round follow-ups: read-only enforcement, verb
table, fingerprint budget exception, per-round acceptance history.

Four gaps the round exposed, each pinned at the decision layer
(the react loop wiring itself mirrors the existing (2b) refusal and
runtime_meta appends, and is observed in the next real round rather
than claimed unit-proven here):

* A bare "modify" task produced NO edit requirement, so neither
  satisfaction nor scope could engage.
* A read-only request's trajectory carried successful edits —
  nothing enforced read-only at the tool layer.
* A negative-fingerprint run that looped to the round cap failed
  with budget_exceeded only, never the named fingerprint error.
* The record carried only the terminal assess, so a loop's cause
  could not be reconstructed from it.
"""

from __future__ import annotations

from types import SimpleNamespace

from forgeflow.runtime.acceptance import (
    acceptance_snapshot,
    is_read_only_task,
    requirement_list,
)
from forgeflow.runtime.orchestrator import _missing_continuation_error


def _rows(intent: str):
    return requirement_list(intent, {"resources": ["calc.py"]}, None)


def _ids(intent: str):
    return [row["id"] for row in _rows(intent)]


# --------------------------------------------------------------------------- #
# 1. Verb table: modify + unambiguous Chinese compounds gain edit rows          #
# --------------------------------------------------------------------------- #
def test_modify_and_compounds_create_edit_requirements() -> None:
    assert _ids("modify calc.py") == ["edit"]
    assert _ids("请改动 add 函数") == ["edit"]
    assert _ids("请更改文件") == ["edit"]
    assert _ids("请改正这段代码") == ["edit"]
    assert _ids("请改写这一节") == ["edit"]
    assert _ids("请调整 add 函数") == ["edit"]


def test_ambiguous_update_and_bare_change_do_not() -> None:
    # "update me on X" is a status request, not an edit; "change" is
    # deliberately still out (see the verb-table comment). Bare 改
    # stays out too ("改用" = switch to, "改天" = another day).
    assert _ids("update me on the project status") == []
    assert _ids("请改用新方案") == []


# --------------------------------------------------------------------------- #
# 2. Read-only detection                                                       #
# --------------------------------------------------------------------------- #
def test_read_only_needs_phrase_and_no_edit_or_return_row() -> None:
    assert is_read_only_task("只读这份文件并总结", []) is True
    assert is_read_only_task("do not edit, summarize only", []) is True
    assert is_read_only_task("研究这份资料", []) is False
    edit_row = {"id": "edit", "description": "x", "status": "unverified", "evidence": []}
    assert is_read_only_task("只读这份文件并总结", [edit_row]) is False
    assert is_read_only_task("先只读分析，再修改文件", [edit_row]) is False
    return_row = {"id": "return", "description": "x", "status": "unverified", "evidence": []}
    assert is_read_only_task("只读并返回文件", [return_row]) is False


# --------------------------------------------------------------------------- #
# 3. Per-round acceptance snapshot                                             #
# --------------------------------------------------------------------------- #
def test_acceptance_snapshot_shape() -> None:
    snapshot = acceptance_snapshot(
        2,
        0,
        {"complete": False, "requirements": [{"id": "edit", "status": "unverified"}]},
    )
    assert snapshot["round"] == 2
    assert snapshot["attempt"] == 0
    assert snapshot["complete"] is False
    assert snapshot["requirements"][0]["id"] == "edit"


# --------------------------------------------------------------------------- #
# 4. Fingerprint error: budget-only exception                                  #
# --------------------------------------------------------------------------- #
def _missing_scope():
    return SimpleNamespace(fingerprint_missing=True, inherited_inputs=None)


def _task():
    return SimpleNamespace(
        intent="请修改文件",
        context={
            "continued_from_artifact_ref": "ref-missing",
            "tool_invocations": [],
        },
    )


def test_budget_only_errors_gain_the_named_fingerprint_error() -> None:
    errors = _missing_continuation_error(
        _task(),
        _missing_scope(),
        ["budget_exceeded: 模型达到 6 轮执行上限，未完成任务、未产出最终答案"],
    )
    assert len(errors) == 2
    assert errors[0].startswith("budget_exceeded")
    assert "ref-missing" in errors[1]


def test_mixed_and_substantive_errors_keep_existing_only() -> None:
    tool_error = "工具 'textfile.edit' 未成功执行：写入失败"
    budget = "budget_exceeded: 模型达到 6 轮执行上限"
    assert _missing_continuation_error(_task(), _missing_scope(), [tool_error]) == [
        tool_error
    ]
    assert _missing_continuation_error(_task(), _missing_scope(), [budget, tool_error]) == [
        budget,
        tool_error,
    ]
