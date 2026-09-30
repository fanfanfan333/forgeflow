"""INC14/INC15 regression — the report must not fabricate a per-tool latency.

Why this file exists
--------------------
``report.render`` renders each prior observation as a Markdown row, and INC14
surfaces that body verbatim as the run's **first-visual-layer** deliverable
(``GET /runs/{id}.artifacts[0].content``). QA found that the ``延迟(ms)`` cell
was emitted as a bare ``0`` for every step — a millisecond-precise measurement
that was never taken (PRD §4.5).

INC14 made the cell show nothing it cannot trust. INC15 finished the job on the
*report reader's* side (main裁定 §3.1): the cell now shows the literal Chinese
``未测量`` (not the ``—`` glyph — a reader parses ``—`` as "this cell is broken",
which was the direct source of the complaint) for anything that is not a real
measurement, and ``str(value)`` for a real one (sub-millisecond precision
preserved, e.g. ``0.062``).

These tests pin exactly that behaviour, at two levels:

* :func:`_format_latency_ms` / :func:`_render_markdown` — the pure formatting
  seam, exercised directly (the legacy call shape);
* :func:`report_render` — the public handler that actually produces the
  artifact body, so the regression cannot reappear behind the handler.

Every case declares its storage tier explicitly via ``force_memory_backend``
(the formatting seam is pure, but the tier must never be inherited from the
environment — see ``tests/conftest.py``).
"""

from __future__ import annotations

from typing import Any

from forgeflow.runtime.tool_handlers import (
    _format_latency_ms,
    _render_markdown,
    report_render,
)

#: The agreed "no data" text — the literal Chinese 未测量 (INC15).
_NO_DATA = "未测量"

#: The rendered table's column order; the latency cell is addressed by name so
#: the test survives a column being added/reordered elsewhere.
_COLUMNS = ["#", "工具", "状态", "已执行", "provider", "延迟(ms)", "摘要"]
_LATENCY_COL = _COLUMNS.index("延迟(ms)")


def _observation(**overrides: Any) -> dict[str, Any]:
    """A single observation shaped as the executor records it."""
    obs: dict[str, Any] = {
        "tool": "research.search",
        "status": "ok",
        "executed": True,
        "provider": "development-stub",
        "summary": "检索到 1 条结果",
    }
    obs.update(overrides)
    return obs


def _data_rows(content: str) -> list[list[str]]:
    """Split a rendered Markdown table into data rows of stripped cells.

    Lines 0 and 1 of the table are the header and the ``|---|`` separator;
    the rest are the per-observation rows.
    """
    table_lines = [ln for ln in content.splitlines() if ln.startswith("|")]
    return [
        [cell.strip() for cell in ln.strip("|").split("|")]
        for ln in table_lines[2:]
    ]


class _Ctx:
    """Minimal ``ToolCallContext`` stand-in — only ``intent`` is read."""

    def __init__(self, intent: str = "") -> None:
        self.intent = intent


# --------------------------------------------------------------------------- #
# 1. The pure formatter                                                        #
# --------------------------------------------------------------------------- #
def test_formatter_renders_only_real_positive_numbers(force_memory_backend):
    assert _format_latency_ms(123) == "123"
    assert _format_latency_ms(812.5) == "812.5"
    # INC15 — a real sub-millisecond measurement is preserved, not truncated.
    assert _format_latency_ms(0.062) == "0.062"


def test_formatter_refuses_everything_that_is_not_a_real_measurement(
    force_memory_backend,
):
    for value in (None, 0, 0.0, -1, -0.5, "", "42", [], {}, True, False):
        assert _format_latency_ms(value) == _NO_DATA, repr(value)


# --------------------------------------------------------------------------- #
# 2. The rendered table cell                                                   #
# --------------------------------------------------------------------------- #
def test_missing_latency_renders_no_data_and_never_zero(force_memory_backend):
    content = _render_markdown([_observation()], "为 Acme 整理销售线索")

    assert "延迟(ms)" in content  # column name retained
    row = _data_rows(content)[0]
    assert row[_LATENCY_COL] == _NO_DATA
    assert "0" not in row, "未计时却渲染出 0 —— 伪造精度"


def test_none_latency_renders_no_data_and_never_zero(force_memory_backend):
    content = _render_markdown([_observation(latency_ms=None)], "intent")
    row = _data_rows(content)[0]
    assert row[_LATENCY_COL] == _NO_DATA
    assert "0" not in row


def test_zero_latency_renders_no_data_and_never_zero(force_memory_backend):
    content = _render_markdown([_observation(latency_ms=0)], "intent")
    row = _data_rows(content)[0]
    assert row[_LATENCY_COL] == _NO_DATA
    assert "0" not in row


def test_positive_latency_is_rendered_as_the_number(force_memory_backend):
    content = _render_markdown([_observation(latency_ms=123)], "intent")
    row = _data_rows(content)[0]
    assert row[_LATENCY_COL] == "123"


def test_sub_millisecond_latency_is_rendered_verbatim(force_memory_backend):
    content = _render_markdown([_observation(latency_ms=0.062)], "intent")
    row = _data_rows(content)[0]
    assert row[_LATENCY_COL] == "0.062"


def test_row_count_and_reference_still_intact(force_memory_backend):
    """The footer/prefix around the table must survive the cell change."""
    observations = [
        _observation(tool="research.search", latency_ms=0),
        _observation(tool="data.query", status="blocked", executed=False),
    ]
    content = _render_markdown(observations, "为 Acme 整理销售线索")

    assert content.startswith("# 运行报告")
    assert "**意图**：为 Acme 整理销售线索" in content
    assert content.rstrip().endswith("共 2 步，已执行 1，成功 1。")
    assert len(_data_rows(content)) == 2


# --------------------------------------------------------------------------- #
# 3. The public handler that produces the artifact body                        #
# --------------------------------------------------------------------------- #
async def test_report_render_body_shows_no_data_not_a_fabricated_zero(
    force_memory_backend,
):
    observations = [
        _observation(tool="research.search", latency_ms=0),
        _observation(
            tool="data.query",
            status="blocked",
            executed=False,
            summary="未提供表名，未执行",
        ),
    ]

    result = await report_render(
        {"observations": observations, "intent": "为 Acme 整理销售线索"},
        _Ctx("为 Acme 整理销售线索"),
    )

    assert result["ok"] is True
    content = result["content"]
    assert "延迟(ms)" in content

    rows = _data_rows(content)
    assert len(rows) == 2
    assert all(row[_LATENCY_COL] == _NO_DATA for row in rows)
    assert "0" not in [cell for row in rows for cell in row], (
        "产物正文出现伪造的 0 ms 精度"
    )
