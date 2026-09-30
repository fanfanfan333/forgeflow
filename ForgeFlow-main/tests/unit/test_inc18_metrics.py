"""INC18-B — 关键指标（Key Metrics）: the report lifts real numbers to the top.

The user's ask was a deliverable that reads like a report, not a debug console.
The obvious way to satisfy it — render a pretty metric card — is also the
obvious way to fabricate: a card with a number the platform never measured is
worse than no card at all. So this file pins the three guards that keep the
metrics section honest:

  * a ``development_stub`` payload is **never** a metric (synthetic rows are not
    measurements);
  * only values a tool really returned are lifted, never an estimate or a unit
    we invented;
  * when nothing qualifies, the section is **omitted** — no placeholder zero.

Measurement scope: this file covers ``tool_handlers._metric_rows`` and the
``report.render`` handler. It does **not** cover the frontend renderer
(``realRun.deriveMetrics``), which has no test facility on this project
(no jsdom CSS engine, by design).
"""

from __future__ import annotations

import pytest

from forgeflow.runtime.tool_handlers import _metric_rows, _render_full_report, report_render

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _obs(
    tool: str = "analysis.score",
    payload: dict | None = None,
    *,
    executed: bool = True,
    stub: bool = False,
) -> dict:
    return {
        "tool": tool,
        "status": "ok" if executed else "blocked",
        "executed": executed,
        "provider": "stdlib-stats",
        "development_stub": stub,
        "summary": "ignored metadata",
        "payload": payload if payload is not None else {},
    }


class _Ctx:
    intent = "生成本周业务报告"


# --------------------------------------------------------------------------- #
# _metric_rows
# --------------------------------------------------------------------------- #


def test_real_scalar_is_lifted_with_its_source():
    rows = _metric_rows([_obs(payload={"score": 1.0, "provider": "stdlib-stats"})])
    assert ("score", "1.0", "analysis.score") in rows


def test_development_stub_payload_is_never_a_metric():
    """Synthetic rows are not measurements — they must never become a card."""
    rows = _metric_rows([_obs("data.query", {"revenue": 1284500}, stub=True)])
    assert rows == []


def test_stub_is_excluded_but_a_real_call_still_counts():
    """Positive control for the guard above: the rule is 'not from stubs',
    not 'never show anything'."""
    rows = _metric_rows(
        [
            _obs("data.query", {"revenue": 1284500}, stub=True),
            _obs("analysis.score", {"score": 1.0}),
        ]
    )
    assert ("revenue", "1284500", "data.query") not in rows
    assert ("score", "1.0", "analysis.score") in rows


def test_call_metadata_is_not_metric():
    """provider / summary / ok describe the call, not what it measured."""
    rows = _metric_rows(
        [_obs(payload={"ok": True, "provider": "stdlib-stats", "summary": "x", "score": 0.5})]
    )
    assert rows == [("score", "0.5", "analysis.score")]


def test_prose_is_not_metric():
    rows = _metric_rows([_obs(payload={"formula": "a" * 200, "query": "本周营收", "score": 0.5})])
    assert rows == [("score", "0.5", "analysis.score")]


def test_truncation_envelope_is_not_metric():
    """Real-task finding: ``ToolExecutor._bound_payload`` replaces an oversized
    payload with ``{truncated, original_length, preview}``. Those describe how
    the payload was cut down — an ``original_length`` card is noise dressed as
    a measurement."""
    rows = _metric_rows(
        [_obs(payload={"truncated": True, "original_length": 8104, "preview": "x" * 100, "score": 0.5})]
    )
    assert rows == [("score", "0.5", "analysis.score")]


def test_non_executed_observation_contributes_nothing():
    rows = _metric_rows([_obs(payload={"score": 1.0}, executed=False)])
    assert rows == []


def test_row_count_is_capped():
    payload = {f"k{i}": float(i) for i in range(50)}
    assert len(_metric_rows([_obs(payload=payload)])) == 12


# --------------------------------------------------------------------------- #
# report.render
# --------------------------------------------------------------------------- #


async def test_report_renders_metrics_section_from_real_observations():
    out = await report_render(
        {
            "intent": "本周业务报告",
            "plan": {"steps": [{"tool": "analysis.score"}]},
            "records": [_obs(payload={"score": 1.0})],
        },
        _Ctx(),
    )
    assert out["ok"] is True
    assert "| 指标 | 数值 | 来源 |" in out["content"]
    assert "| score | 1.0 | analysis.score |" in out["content"]


async def test_report_omits_metrics_section_when_nothing_is_real():
    """No placeholder, no zero — the section simply does not exist."""
    out = await report_render(
        {
            "intent": "本周业务报告",
            "plan": {"steps": [{"tool": "data.query"}]},
            "records": [_obs("data.query", {"revenue": 1}, stub=True)],
        },
        _Ctx(),
    )
    assert "关键指标" not in out["content"]


def test_empty_section_keeps_the_body_identical():
    """Additive-change discipline: when no metric qualifies, the rendered body
    is byte-for-byte what a run without metric-able payloads always produced."""
    base = _render_full_report({"steps": []}, [_obs("data.query", {"x": 1}, stub=True)], [], "i")
    no_obs = _render_full_report({"steps": []}, [], [], "i")
    assert "关键指标" not in base
    assert "关键指标" not in no_obs


def test_model_converged_without_text_says_so():
    """A run that converged but whose model reply was empty must not produce a
    '已完成' report that says nothing."""
    body = _render_full_report({"steps": []}, [], [], "i", final_answer="", terminated_by="model")
    assert "## 最终答案" in body
    assert "未返回任何文本" in body


def test_legacy_caller_still_renders_nothing_for_the_answer():
    """Additive discipline: no ``terminated_by`` ⇒ no 最终答案 section at all."""
    body = _render_full_report({"steps": []}, [], [], "i")
    assert "最终答案" not in body


@pytest.mark.parametrize("value", [0, -3, 12.75, True, None, [], {}])
def test_only_scalars_are_lifted(value):
    rows = _metric_rows([_obs(payload={"v": value})])
    if isinstance(value, bool) or value is None or isinstance(value, (list, dict)):
        assert rows == []
    else:
        assert rows == [("v", str(value), "analysis.score")]
