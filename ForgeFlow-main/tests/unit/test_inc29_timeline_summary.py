"""INC29 T02 —— §6 进展摘要词表 + 清死代码。

## 留存经验（为什么要这条钉子）

设计 §6 要求「进展摘要词表」（修改 N 个文件 / 执行 pytest / M passed · N failed /
自动修复 R 轮），而此前 ``codeplane/events.py`` 只有**工具级粗标签**
（``_TOOL_LABELS`` / ``_LABELS``：「正在运行命令」「正在修改文件」）——
逐文件变更计数、失败数、修复轮次**一概没有**。同时 ``adapt_stream`` 是规划期留下、
全仓零调用的**死代码**（只有 ``__init__.py`` re-export 它）。

修法（INC29 §4.B）：

* ``events.py::summarize_timeline`` —— 从 timeline+diff 产出结构化摘要，
  键至少含 ``files_changed`` / ``test_command`` / ``passed`` / ``failed`` /
  ``repair_rounds``；**全部来自真实证据**，拿不到就是 ``None``（未测量），
  **绝不写 0 冒充**；
* 状态一律走 ``protocol.py::STEP_STATUSES``（不引入第二套状态词表）;
* ``adapt_stream`` **删除**（``events.py`` / ``codeplane/__init__.py`` 一并清）。

引文一律 ``file.py::symbol``，不用行号。
"""

from __future__ import annotations

import pytest

from forgeflow.codeplane import events as ev
from forgeflow.codeplane.events import summarize_timeline
from forgeflow.codeplane.protocol import STEP_STATUSES

#: A unified diff touching **three** files.
_DIFF_3_FILES = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n+ x\n"
    "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n+ y\n"
    "diff --git a/c.py b/c.py\n--- a/c.py\n+++ b/c.py\n+ z\n"
)


def _test_event(status: str, raw_stdout: str, exit_code: int, command: str = "python -m pytest -q") -> dict:
    return {
        "seq": 9,
        "phase": "test",
        "kind": "test",
        "status": status,
        "detail": command,
        "data": {"raw_stdout": raw_stdout, "exit_code": exit_code},
    }


def _diff_event(diff_text: str) -> dict:
    return {"seq": 8, "phase": "diff", "kind": "diff", "status": "ok", "data": {"diff": diff_text}}


# --------------------------------------------------------------------------- #
# 1. 主口径 —— 3 改文件 / `5 passed, 1 failed` ⇒ files_changed=3, failed=1       #
# --------------------------------------------------------------------------- #
def test_summary_reads_files_and_counts_from_the_timeline():
    timeline = [
        _diff_event(_DIFF_3_FILES),
        _test_event("error", "5 passed, 1 failed in 2.3s\n", 1),
    ]
    summary = summarize_timeline(timeline)

    assert summary["files_changed"] == 3
    assert summary["passed"] == 5
    assert summary["failed"] == 1
    assert summary["test_command"] == "python -m pytest -q"
    # One real test execution ⇒ no re-run ⇒ 0 repair rounds (measured, not None).
    assert summary["repair_rounds"] == 0


def test_summary_uses_a_supplied_diff_and_reviewed_tests():
    """An explicit diff + a reviewed TestResult are authoritative over the timeline."""
    timeline = [_test_event("ok", "should be ignored", 0)]
    summary = summarize_timeline(
        timeline,
        diff=_DIFF_3_FILES,
        tests={"measured": True, "passed": 2, "failed": 0, "command": "pytest -q"},
    )
    assert summary["files_changed"] == 3
    assert summary["passed"] == 2
    assert summary["failed"] == 0
    assert summary["test_command"] == "pytest -q"


def test_summary_counts_files_from_file_edit_events_when_no_diff_exists():
    timeline = [
        {"kind": "action", "status": "ok", "tool": "file_editor", "data": {"path": "a.py"}},
        {"kind": "action", "status": "ok", "tool": "file_editor", "data": {"path": "b.py"}},
        {"kind": "action", "status": "ok", "tool": "str_replace_editor", "data": {"path": "c.py"}},
    ]
    summary = summarize_timeline(timeline)
    assert summary["files_changed"] == 3


# --------------------------------------------------------------------------- #
# 2. 诚实红线 —— 证据缺失 ⇒ None（不是 0）                                       #
# --------------------------------------------------------------------------- #
def test_missing_evidence_is_none_not_zero():
    summary = summarize_timeline([])
    assert summary["files_changed"] is None
    assert summary["test_command"] is None
    assert summary["passed"] is None
    assert summary["failed"] is None
    assert summary["repair_rounds"] is None


def test_unmeasured_tests_never_become_zero():
    """A test event with no parseable output ⇒ counts stay ``None`` (未测量 ≠ 0)."""
    timeline = [_test_event("error", "collection error, no counts here", 2)]
    summary = summarize_timeline(timeline)
    assert summary["passed"] is None
    assert summary["failed"] is None
    assert summary["test_command"] == "python -m pytest -q"  # command is real evidence


def test_counterfactual_zero_would_be_a_lie():
    """反证：把未测量当成 0，会把「没测」谎报成「0 失败」。"""

    def old_naive(timeline):
        # 旧式粗口径：直接 `dict.get(..., 0)` —— 未测量被写成 0。
        return {"passed": 0, "failed": 0}

    summary = summarize_timeline([_test_event("error", "boom", 2)])
    assert summary["failed"] != 0 and summary["passed"] != 0
    assert summary["failed"] is None and summary["passed"] is None
    # 反事实的旧口径确实会给出 0（证明「None ≠ 0」这条纪律是承重的）。
    assert old_naive([])["failed"] == 0


# --------------------------------------------------------------------------- #
# 3. 自动修复轮次 —— 失败后的重跑                                          #
# --------------------------------------------------------------------------- #
def test_repair_rounds_counts_reruns_after_a_failure():
    timeline = [
        _test_event("error", "1 failed\n", 1),  # first run fails
        _test_event("ok", "1 passed\n", 0),     # re-run after failure ⇒ 1 repair round
    ]
    summary = summarize_timeline(timeline)
    assert summary["repair_rounds"] == 1


def test_no_repair_rounds_without_a_failing_run():
    timeline = [
        _test_event("ok", "3 passed\n", 0),
        _test_event("ok", "3 passed\n", 0),
    ]
    summary = summarize_timeline(timeline)
    assert summary["repair_rounds"] == 0


# --------------------------------------------------------------------------- #
# 4. 单一状态词表 —— 不引入第二套                                          #
# --------------------------------------------------------------------------- #
def test_executed_test_statuses_stay_inside_the_shared_vocabulary():
    assert ev._EXECUTED_TEST_STATUSES <= set(STEP_STATUSES)
    assert ev._EXECUTED_TEST_STATUSES, "词表交集为空 —— 分类将永远不命中"


# --------------------------------------------------------------------------- #
# 5. 死代码清除 —— adapt_stream 已不存在                                    #
# --------------------------------------------------------------------------- #
def test_adapt_stream_is_deleted_everywhere():
    assert not hasattr(ev, "adapt_stream"), "events.adapt_stream 仍在（死代码未清）"
    with pytest.raises(ImportError):
        from forgeflow.codeplane.events import adapt_stream  # noqa: F401

    import forgeflow.codeplane as cp

    assert not hasattr(cp, "adapt_stream"), "codeplane.adapt_stream re-export 仍在"
    assert "adapt_stream" not in ev.__all__
    assert "summarize_timeline" in ev.__all__
