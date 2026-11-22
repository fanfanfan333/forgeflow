"""BIZ-14 — a run's body must survive a restart, within a bounded budget.

Before this, ``workspace_runs`` stored only the header, so a hydrated run came
back with ``steps=[]`` and ``detail_retained=False``: the product forgot what it
did and every "重启后任务详情不完整" report was accurate.

The projection is bounded on purpose — a live run can carry megabytes of tool
output — so the tests pin both halves: the body really comes back, and a huge
body is truncated *visibly* (``truncated``/``omitted``) rather than silently
dropped or silently kept at full size.
"""

from __future__ import annotations

import json

from forgeflow.runtime.dispatcher import _record_from_header
from forgeflow.runtime.orchestrator import RunRecord
from forgeflow.workspace.detail import MAX_DETAIL_BYTES, build_detail, restore_detail
from forgeflow.workspace.models import WorkspaceRunRecord


def _record(**overrides) -> RunRecord:
    base = dict(
        run_id="run-detail-1",
        thread_id="th-1",
        tenant_id="t-1",
        agent_id=None,
        intent="核对季度报价",
        status="completed",
        outcome="success",
        steps=[
            {"step": 1, "tool": "document.inspect", "status": "ok", "latency_ms": 12},
            {"step": 2, "tool": "textfile.edit", "status": "ok", "latency_ms": 30},
        ],
        errors=[],
        created_at="2026-11-22T00:00:00+00:00",
        actor_user_id="u-1",
        actor_role="sales_rep",
    )
    base.update(overrides)
    return RunRecord(**base)


def test_detail_survives_the_header_round_trip():
    """record → persisted header → hydrated record: the body is there."""
    record = _record(
        tool_invocations=[
            {"tool": "document.inspect", "status": "ok", "latency_ms": 12, "arguments": "{}"}
        ],
        plan={"source": "react", "steps": [{"tool": "document.inspect"}]},
        plan_source="react",
        persistence={"status": "saved"},
    )

    header = WorkspaceRunRecord.from_run_record(record)
    assert header.detail["steps"], "the projection wrote no steps"
    assert header.detail["tool_invocations"], "the projection wrote no invocations"

    hydrated = _record_from_header(header)
    assert hydrated.detail_retained is True
    assert [s["tool"] for s in hydrated.steps] == ["document.inspect", "textfile.edit"]
    assert hydrated.tool_invocations[0]["tool"] == "document.inspect"
    assert hydrated.plan["source"] == "react"
    assert hydrated.plan_source == "react"


def test_a_pre_042_header_is_honest_about_having_no_detail():
    """An old row (empty projection) must not pretend it was restored."""
    header = WorkspaceRunRecord(
        run_id="run-old",
        tenant_id="t-1",
        session_id="run-old",
        intent="旧运行",
        status="completed",
        outcome="success",
        actor_user_id="u-1",
    )
    hydrated = _record_from_header(header)
    assert hydrated.detail_retained is False
    assert hydrated.steps == []


def test_a_huge_detail_is_truncated_visibly_not_silently():
    """Oversized bodies are cut down and say so; nothing is passed off as whole."""
    steps = [
        {"step": i, "tool": "code.execute", "status": "ok", "note": "x" * 5000} for i in range(400)
    ]
    payload = build_detail(_record(steps=steps), max_bytes=64 * 1024)

    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert size <= 64 * 1024, f"projection exceeded its own budget: {size}"
    assert payload["truncated"] is True
    assert payload["omitted"], "truncation must name what it dropped"
    assert 0 < len(payload["steps"]) < 400


def test_a_normal_run_is_not_marked_truncated():
    """``truncated=False`` has to mean "this really is the whole detail"."""
    payload = build_detail(_record())
    assert payload["truncated"] is False
    assert payload["omitted"] == []
    assert len(payload["steps"]) == 2


def test_string_fields_are_clipped_but_never_dropped():
    """A long free-text field keeps its head plus an explicit marker."""
    record = _record(steps=[{"step": 1, "tool": "t", "note": "y" * 5000}])
    payload = build_detail(record)
    note = payload["steps"][0]["note"]
    assert note.startswith("y" * 100)
    assert note.endswith("chars]")
    assert len(note) < 5000


def test_restore_does_not_overwrite_real_errors_with_stored_ones():
    """Restoration fills gaps; it never rewrites facts the live record holds."""
    record = _record(errors=["live error"])
    payload = build_detail(_record(errors=["stored error", "stored too"]))
    restore_detail(record, payload)
    assert record.errors == ["live error"]


def test_default_budget_is_bounded():
    assert MAX_DETAIL_BYTES == 128 * 1024
