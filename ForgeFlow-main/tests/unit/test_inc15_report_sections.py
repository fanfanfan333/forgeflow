"""INC15 ③/④ — the report renders four distinct layers, honestly.

Why this file exists
--------------------
INC14 made ``report.render``'s Markdown body the run's first-visual-layer
deliverable. QA found the report padded itself: it rendered a single flat table
whose per-row ``0`` claimed a latency that was never measured, and whose step
list was whatever the executor happened to hold. There was no way to tell a
*planned* step from an *executed* one, nor a step that **did not apply** from one
that was **blocked** from one that **failed**.

INC15 gives the report four sections, each reading its own layer:

* **执行记录** (L2) — one row per real execution record, status ``executed`` /
  provider / latency verbatim; the latency cell is ``未测量`` for an unmeasured
  record and ``str(value)`` for a real one.
* **任务计划** (L1) — the plan's steps and their applicability.
* **未适用** (L1) — the candidate steps that did not apply, with the reason.
* **受阻** (L2) — the blocked records (needed but missing input).
* **失败** (L2) — the error / unavailable / refused records.

The footer counts are **recomputed from the records** (never summed over a mixed
collection), and the body states plainly that the deliverable step itself is not
in the execution list.

Every case declares its storage tier via ``force_memory_backend``.
"""

from __future__ import annotations

import hashlib

from forgeflow.runtime.tool_handlers import (
    _render_full_report,
    _render_markdown,
    report_render,
)


class _Ctx:
    def __init__(self, intent: str = "") -> None:
        self.intent = intent


#: Both render paths emit the same table column order; the status cell is
#: addressed by name so this test survives a column being added/reordered.
_COLUMNS = ["#", "工具", "状态", "已执行", "provider", "延迟(ms)", "摘要"]
_STATUS_COL = _COLUMNS.index("状态")


def _data_rows(content: str) -> list[list[str]]:
    """Split the first Markdown table into data rows of stripped cells.

    Lines 0 and 1 of a table are the header and the ``|---|`` separator; the
    rest are the data rows. Only used on the single-table legacy renderer, or
    on the first (执行记录) table of the modern renderer via its ``| 1 |`` row.
    """
    table_lines = [ln for ln in content.splitlines() if ln.startswith("|")]
    return [
        [cell.strip() for cell in ln.strip("|").split("|")]
        for ln in table_lines[2:]
    ]


def _record(**kw) -> dict:
    base = {
        "tool": "research.search",
        "status": "ok",
        "executed": True,
        "provider": "development-stub",
        "latency_ms": 12.5,
        "summary": "检索到 1 条结果",
    }
    base.update(kw)
    return base


def _plan() -> dict:
    return {
        "run_id": "r-inc15",
        "attempt": 0,
        "source": "deterministic",
        "steps": [
            {"index": 0, "tool": "research.search", "applicability": "required", "note": "研究"},
            {"index": 1, "tool": "report.render", "applicability": "required", "note": "生成报告"},
        ],
        "not_applicable": [
            {"tool": "data.query", "reason": "与当前任务无关：未提供 表名(table)"},
            {"tool": "code.run", "reason": "与当前任务无关：未提供 路径(paths/repo_path)"},
        ],
    }


# --------------------------------------------------------------------------- #
# 1. All five sections are present and read their own layer                     #
# --------------------------------------------------------------------------- #
def test_full_report_has_all_five_sections(force_memory_backend):
    content = _render_full_report(_plan(), [_record()], [], "为 Acme 整理线索")
    assert "## 一、执行记录" in content
    assert "## 二、任务计划" in content
    assert "## 三、未适用" in content
    assert "## 四、受阻" in content
    assert "## 五、失败" in content
    assert "**意图**：为 Acme 整理线索" in content


def test_not_applicable_section_lists_trimmed_candidates(force_memory_backend):
    content = _render_full_report(_plan(), [_record()], [], "intent")
    assert "data.query" in content
    assert "code.run" in content
    # The reason is carried verbatim, not invented.
    assert "未提供 表名(table)" in content


def test_task_plan_section_lists_steps_with_applicability(force_memory_backend):
    content = _render_full_report(_plan(), [_record()], [], "intent")
    plan_block = content.split("## 二、任务计划")[1].split("## 三、")[0]
    assert "research.search" in plan_block
    assert "report.render" in plan_block
    assert "适用性：required" in plan_block


# --------------------------------------------------------------------------- #
# 2. The latency cell is honest                                                 #
# --------------------------------------------------------------------------- #
def test_latency_cell_is_no_data_for_an_unmeasured_record(force_memory_backend):
    rec = _record(latency_ms=None, status="blocked", executed=False)
    content = _render_full_report(_plan(), [rec], [], "intent")
    row = next(ln for ln in content.splitlines() if ln.startswith("| 1 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[5] == "未测量"


def test_latency_cell_renders_a_real_sub_ms_value(force_memory_backend):
    rec = _record(latency_ms=0.062)
    content = _render_full_report(_plan(), [rec], [], "intent")
    row = next(ln for ln in content.splitlines() if ln.startswith("| 1 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[5] == "0.062"


# --------------------------------------------------------------------------- #
# 3. Blocked / failed sections are driven by the records, not the plan          #
# --------------------------------------------------------------------------- #
def test_blocked_and_failed_sections(force_memory_backend):
    records = [
        _record(tool="research.search", status="ok", executed=True),
        _record(tool="data.query", status="blocked", executed=False, latency_ms=None,
                summary="缺少必需输入：表名(table)"),
        _record(tool="code.run", status="unavailable", executed=False, latency_ms=None,
                error="未绑定任何实现", summary="未绑定任何实现"),
    ]
    content = _render_full_report(_plan(), records, [], "intent")
    blocked_block = content.split("## 四、受阻")[1].split("## 五、")[0]
    failed_block = content.split("## 五、失败")[1]
    assert "data.query" in blocked_block
    assert "缺少必需输入：表名(table)" in blocked_block
    assert "code.run" in failed_block
    assert "unavailable" in failed_block


def test_legacy_skipped_record_renders_as_blocked(force_memory_backend):
    """A historical ``skipped`` record renders under 受阻, not as a failure."""
    rec = _record(tool="data.query", status="skipped", executed=False, latency_ms=None)
    content = _render_full_report(_plan(), [rec], [], "intent")
    blocked_block = content.split("## 四、受阻")[1].split("## 五、")[0]
    assert "data.query" in blocked_block
    assert "skipped" not in content  # the reader aliases it to blocked
    assert "| data.query | blocked " in content


def test_legacy_markdown_reader_also_normalizes_skipped_to_blocked(
    force_memory_backend,
):
    """INC15 R1 — *both* render paths speak one vocabulary for a legacy record.

    The ``_render_full_report`` path already routed its status cell through
    ``normalize_status``; the legacy ``_render_markdown`` path did not, so a
    historical/exported observation whose stored status is the retired
    ``skipped`` leaked the literal 'skipped' out of one path while the other
    honestly said 'blocked' — a promise↔mechanism divergence. Both readers must
    now render the same cell for the same record.
    """
    obs = {
        "tool": "data.query",
        "status": "skipped",
        "executed": False,
        "summary": "旧记录",
    }

    # legacy reader (the shape with only ``observations``).
    legacy = _render_markdown([obs], "intent")
    assert "skipped" not in legacy
    assert _data_rows(legacy)[0][_STATUS_COL] == "blocked"

    # modern reader — same record, same cell, same vocabulary.
    modern = _render_full_report(_plan(), [obs], [], "intent")
    assert "skipped" not in modern
    modern_row = next(ln for ln in modern.splitlines() if ln.startswith("| 1 |"))
    modern_cells = [c.strip() for c in modern_row.strip("|").split("|")]
    assert modern_cells[_STATUS_COL] == "blocked"


# --------------------------------------------------------------------------- #
# 4. Footer counts are recomputed from the records                              #
# --------------------------------------------------------------------------- #
def test_footer_counts_come_from_records(force_memory_backend):
    records = [
        _record(tool="research.search", status="ok", executed=True),
        _record(tool="data.query", status="blocked", executed=False, latency_ms=None),
        _record(tool="code.run", status="error", executed=True, latency_ms=3.0,
                error="校验失败", summary="校验失败"),
    ]
    content = _render_full_report(_plan(), records, [], "intent")
    footer = next(ln for ln in content.splitlines() if ln.startswith("共 "))
    assert "已执行 2" in footer
    assert "成功 1" in footer
    assert "受阻 1" in footer
    assert "失败 1" in footer
    assert "未适用 2" in footer


def test_body_states_the_product_step_is_not_in_the_records(force_memory_backend):
    content = _render_full_report(_plan(), [_record()], [], "intent")
    assert "report.render" in content
    assert "自身不在上方的执行记录中" in content


# --------------------------------------------------------------------------- #
# 5. The public handler (modern vs legacy call shape)                           #
# --------------------------------------------------------------------------- #
async def test_report_render_modern_path(force_memory_backend):
    records = [_record(tool="research.search", status="ok", executed=True)]
    result = await report_render(
        {
            "intent": "为 Acme 整理线索",
            "plan": _plan(),
            "records": records,
            "observations": [records[0]],
        },
        _Ctx("为 Acme 整理线索"),
    )
    assert result["ok"] is True
    content = result["content"]
    assert "## 一、执行记录" in content
    assert "## 三、未适用" in content
    assert result["result_ref"] == hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
    assert result["record_count"] == 1


async def test_report_render_modern_path_is_always_renderable(force_memory_backend):
    """A run where nothing executed still yields an honest report of the plan."""
    result = await report_render(
        {"intent": "intent", "plan": _plan(), "records": [], "observations": []},
        _Ctx("intent"),
    )
    assert result["ok"] is True
    assert "## 二、任务计划" in result["content"]
    assert "（本产物步之前没有真正执行的步骤）" in result["content"]


async def test_report_render_legacy_path_still_writes_no_data(force_memory_backend):
    observations = [
        {"tool": "research.search", "status": "ok", "executed": True,
         "provider": "development-stub", "summary": "1 条"}
    ]
    result = await report_render({"observations": observations, "intent": "intent"}, _Ctx("intent"))
    assert result["ok"] is True
    assert "延迟(ms)" in result["content"]
    assert "未测量" in result["content"]
