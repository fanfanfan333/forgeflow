"""INC18 — 「只跑了一步就宣称已完成」必须结束：引入 ``partial`` 结论。

根因（用户用真实任务抓到，不是单测抓到）：``validation.validator.validate`` 的
规则 3 是「**只要至少一步跑了** ⇒ success」。于是「计划 2 步、`report.render`
根本没执行」的运行，仍然把 `outcome` 写成 `success`、页面显示「已完成」。

本次把两件事**分开**（这是本文件最重要的断言）：

* ``success``（布尔）= 「这次运行需不需要 replan / 升级人工？」—— 部分完成
  **不需要**（重试也变不出缺失的输入），所以它保持 ``True``，replan 循环与
  HITL 升级**完全不受影响**；
* ``outcome``（结论）= 「交付有多诚实？」—— 这才是落库、前端徽章读的字段。

把两者合并会走向两个不可接受的极端：要么每个部分完成的运行都去打扰人工，
要么把没做完的交付物说成做完了。

测量范围
--------
本文件只钉 ``validate`` 这一个纯函数（无 IO、无 provider、无存储档位依赖）。
它**不**声称前端如何渲染、也不声称其它档位的行为。
"""

from __future__ import annotations

from forgeflow.validation.validator import validate


def test_all_steps_ran_is_success():
    verdict = validate(
        {
            "status": "completed",
            "steps": [
                {"tool": "research.search", "status": "ok"},
                {"tool": "report.render", "status": "ok"},
            ],
        }
    )
    assert verdict.outcome == "success"
    assert verdict.success is True
    assert verdict.score == 1.0


def test_unrun_closing_step_is_partial_not_success():
    """The exact defect: the report step never ran, yet nothing failed."""
    verdict = validate(
        {
            "status": "completed",
            "steps": [
                {"tool": "research.search", "status": "ok"},
                {"tool": "report.render", "status": "blocked"},
            ],
        }
    )
    assert verdict.outcome == "partial", "单步跑完但收尾步未执行，不得宣称 success"
    # …but it must NOT be escalated to a replan / human approval either.
    assert verdict.success is True
    assert verdict.score < 1.0
    assert verdict.metrics["unrun_steps"] == ["report.render"]
    assert any("report.render" in r for r in verdict.reasons)


def test_partial_covers_every_unrun_vocabulary():
    for status in ("blocked", "pending", "awaiting_approval", "paused"):
        verdict = validate(
            {"status": "completed", "steps": [{"tool": "t", "status": status}]}
        )
        assert verdict.outcome == "partial", status
        assert verdict.success is True, status


def test_failure_vocabulary_is_not_double_counted_as_unrun():
    """裁决边界：`unavailable` / `refused` 已属既有失败-不可用口径，
    把它们也算成「未执行」会让 partial 与 failed 双重计数。"""
    for status in ("unavailable", "refused"):
        verdict = validate(
            {"status": "completed", "steps": [{"tool": "t", "status": status}]}
        )
        assert verdict.metrics.get("unrun_steps") is None, status
        assert verdict.outcome == "success", status


def test_not_applicable_is_not_a_shortfall():
    """反向对照：本就不适用的步骤不算「没做完」，否则是另一种撒谎。"""
    verdict = validate(
        {
            "status": "completed",
            "steps": [
                {"tool": "research.search", "status": "ok"},
                {"tool": "data.query", "status": "not_applicable"},
            ],
        }
    )
    assert verdict.outcome == "success"
    assert verdict.metrics.get("unrun_steps") is None


def test_unknown_status_is_not_claimed_either_way():
    """保守读数：payload 没给出证据时不主张「未执行」（否则会误伤既有判定）。"""
    verdict = validate({"status": "completed", "steps": [{"tool": "x"}]})
    assert verdict.outcome == "success"


def test_error_steps_still_fail():
    """partial 不得吞掉真正的失败。"""
    verdict = validate(
        {
            "status": "completed",
            "steps": [
                {"tool": "research.search", "status": "ok"},
                {"tool": "report.render", "status": "blocked"},
            ],
            "errors": ["boom"],
        }
    )
    assert verdict.outcome == "failure"
    assert verdict.success is False


def test_aborted_is_untouched():
    verdict = validate({"status": "aborted", "steps": [{"tool": "x", "status": "ok"}]})
    assert verdict.outcome == "aborted"
    assert verdict.success is False


def test_implicit_success_path_is_also_honest():
    """No terminal status + steps ⇒ the implicit-success branch, same rule."""
    verdict = validate(
        {"steps": [{"tool": "a", "status": "ok"}, {"tool": "b", "status": "blocked"}]}
    )
    assert verdict.outcome == "partial"
    assert verdict.success is True
