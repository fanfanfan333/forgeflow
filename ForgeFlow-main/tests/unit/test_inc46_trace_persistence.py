"""INC46 T01 — trace materialisation: honest, best-effort, tenant fail-closed.

These tests pin the *contract* of :mod:`forgeflow.runtime.trace_store` and the
:meth:`forgeflow.runtime.tool_executor.ToolExecutor.execute` seam, without a
database: an in-process recording sink stands in for the postgres store (see
:func:`forgeflow.runtime.trace_store.set_trace_sink`).

What is pinned
--------------
* **unmeasured latency is ``None``, never a fabricated ``0``** (the data-honesty
  red line that migration ``019`` exists to make storable);
* ``execute`` returns the **byte-identical** invocation ``_execute`` produced —
  the trace is an enhancement, never a gate;
* a write failure never disturbs the run;
* a missing ``tenant_id`` and a non-UUID ``run_id`` each **refuse** to write
  (fail-closed; ``run_steps.run_id`` is ``UUID NOT NULL`` and is never coerced);
* the ``memory`` profile is an honest no-op (persistence disabled).
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.config import get_settings
from forgeflow.runtime import tool_registry, trace_store
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.trace_store import (
    TraceStep,
    list_for_run,
    persist_invocation,
    reset_trace_sink,
    set_trace_sink,
    to_row,
    trace_persistence_enabled,
)

pytestmark = pytest.mark.asyncio


class _RecordingSink:
    """A minimal in-process :class:`TraceStore` for offline assertions."""

    def __init__(self, *, fail: bool = False) -> None:
        self.steps: list[TraceStep] = []
        self.fail = fail

    async def insert_step(self, step: TraceStep) -> None:
        if self.fail:
            raise RuntimeError("sink exploded")
        self.steps.append(step)

    async def list_for_run(
        self, tenant_id: str | None, run_id: str, *, limit: int = 500
    ) -> list[TraceStep]:
        return [
            s for s in self.steps if s.run_id == run_id and s.tenant_id == tenant_id
        ][:limit]

    async def list_for_tenant(
        self, tenant_id: str | None, *, limit: int = 1000
    ) -> list[TraceStep]:
        return [s for s in self.steps if s.tenant_id == tenant_id][:limit]


@pytest.fixture(autouse=True)
def _clean_state():
    """Start every test from default bindings and no injected sink."""
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    reset_trace_sink()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    reset_trace_sink()


def _run_id() -> str:
    return str(uuid.uuid4())


def _ctx(**kw) -> ToolCallContext:
    base = dict(
        run_id=_run_id(),
        step_id="",
        tenant_id="t-inc46",
        user_id="u-1",
        role="admin",
        intent="分析数据",
        attempt=0,
        args={},
    )
    base.update(kw)
    base["step_id"] = base["step_id"] or f"{base['run_id']}:{base['attempt']}:0"
    return ToolCallContext(**base)


# --------------------------------------------------------------------------- #
# 1. Honest latency — unmeasured is NULL, never 0                              #
# --------------------------------------------------------------------------- #
async def test_unmeasured_latency_is_none_never_zero():
    inv = await ToolExecutor().execute(
        "docs.parse", ctx=_ctx(args={}, intent=""), blocked_reason="缺少文本输入"
    )
    assert inv.status == "blocked"
    assert inv.latency_ms is None  # never measured
    assert inv.latency_ms != 0  # explicitly NOT a fabricated 0

    step = to_row(
        inv.to_dict(), args={}, step_index=0, tenant_id="t-inc46"
    )
    assert step.latency_ms is None
    assert step.status == "blocked"


async def test_measured_latency_is_a_real_positive_float():
    inv = await ToolExecutor().execute(
        "docs.parse", ctx=_ctx(args={"text": "第一段\n\n第二段"})
    )
    assert inv.status == "ok"
    assert inv.latency_ms is not None and inv.latency_ms > 0
    step = to_row(inv.to_dict(), args={"text": "第一段\n\n第二段"}, step_index=0, tenant_id="t-inc46")
    assert step.latency_ms is not None and step.latency_ms > 0


# --------------------------------------------------------------------------- #
# 2. execute() persists the *exact* invocation it returns (byte-identical)     #
# --------------------------------------------------------------------------- #
async def test_execute_persists_the_byte_identical_invocation():
    sink = _RecordingSink()
    set_trace_sink(sink)

    ctx = _ctx(args={"text": "第一段\n\n第二段"})
    inv = await ToolExecutor().execute("docs.parse", ctx=ctx)

    assert inv.status == "ok"
    assert len(sink.steps) == 1
    row = sink.steps[0]
    # The row's ``output`` is precisely what the caller received.
    assert row.output == inv.to_dict()
    assert row.status == "ok"
    assert row.tool == "docs.parse"
    assert row.run_id == ctx.run_id
    assert row.tenant_id == "t-inc46"
    assert row.attempt == 0
    assert row.actor_user_id == "u-1"
    assert row.input["args"] == {"text": "第一段\n\n第二段"}
    assert row.input["arguments_hash"] == inv.arguments_hash


async def test_step_index_is_parsed_from_step_id():
    sink = _RecordingSink()
    set_trace_sink(sink)
    run = _run_id()
    ctx = _ctx(run_id=run, step_id=f"{run}:0:7", args={"text": "x"})
    await ToolExecutor().execute("docs.parse", ctx=ctx)
    assert sink.steps[0].step_index == 7


async def test_step_index_falls_back_to_arrival_order():
    sink = _RecordingSink()
    set_trace_sink(sink)
    run = _run_id()
    for i in range(3):
        ctx = ToolCallContext(
            run_id=run,
            step_id="not-parsable",  # no ":" ⇒ arrival counter
            tenant_id="t-inc46",
            user_id="u-1",
            role="admin",
            intent="x",
            attempt=0,
            args={"text": "line"},
        )
        await ToolExecutor().execute("docs.parse", ctx=ctx)
    assert [s.step_index for s in sink.steps] == [0, 1, 2]


# --------------------------------------------------------------------------- #
# 3. Gate + fail-closed                                                        #
# --------------------------------------------------------------------------- #
async def test_memory_backend_without_sink_is_a_noop(force_memory_backend):
    assert trace_persistence_enabled() is False
    # No sink, memory backend ⇒ the write is a silent, honest no-op.
    result = await persist_invocation(
        {"run_id": _run_id(), "tool": "docs.parse", "status": "ok"},
        args={},
        run_id=_run_id(),
        tenant_id="t-inc46",
        step_id="r:0:0",
    )
    assert result is None


async def test_missing_tenant_writes_nothing():
    sink = _RecordingSink()
    set_trace_sink(sink)
    run = _run_id()
    await persist_invocation(
        {"run_id": run, "tool": "docs.parse", "status": "ok", "started_at": ""},
        args={},
        run_id=run,
        tenant_id=None,  # fail-closed
        step_id=f"{run}:0:0",
    )
    assert sink.steps == []


async def test_non_uuid_run_id_writes_nothing_and_does_not_raise():
    sink = _RecordingSink()
    set_trace_sink(sink)
    await persist_invocation(
        {"run_id": "hub-abc", "tool": "docs.parse", "status": "ok", "started_at": ""},
        args={},
        run_id="hub-abc",  # not a UUID ⇒ never coerced into the UUID column
        tenant_id="t-inc46",
        step_id="hub-abc:0:0",
    )
    assert sink.steps == []


async def test_write_failure_does_not_break_the_run():
    set_trace_sink(_RecordingSink(fail=True))
    inv = await ToolExecutor().execute(
        "docs.parse", ctx=_ctx(args={"text": "第一段"})
    )
    # The run is unaffected even though persistence raised internally.
    assert inv.status == "ok"
    assert inv.latency_ms is not None


# --------------------------------------------------------------------------- #
# 4. Field mapping                                                             #
# --------------------------------------------------------------------------- #
async def test_artifact_ref_prefers_payload_then_result_ref():
    inv = {
        "run_id": _run_id(),
        "tool": "document.write",
        "status": "ok",
        "result_ref": "deadbeef",
        "payload": {"artifact_ref": "artifact://real"},
        "latency_ms": 12.5,
        "attempt": 2,
        "actor_user_id": "u-9",
        "arguments_hash": "h",
        "started_at": "2026-10-03T00:00:00+00:00",
    }
    step = to_row(inv, args={"k": "v"}, step_index=1, tenant_id="t-inc46")
    assert step.artifact_ref == "artifact://real"  # payload wins
    assert step.attempt == 2
    assert step.actor_user_id == "u-9"
    assert step.created_at == "2026-10-03T00:00:00+00:00"
    assert step.output == inv

    # Without a payload artifact_ref, result_ref is used; with neither ⇒ None.
    inv2 = dict(inv, payload={}, result_ref="cafef00d")
    assert to_row(inv2, args={}, step_index=1, tenant_id="t").artifact_ref == "cafef00d"
    inv3 = dict(inv, payload={}, result_ref=None)
    assert to_row(inv3, args={}, step_index=1, tenant_id="t").artifact_ref is None


async def test_verification_is_carried_only_when_a_dict():
    base = {
        "run_id": _run_id(),
        "tool": "x",
        "status": "ok",
        "result_ref": None,
        "latency_ms": None,
        "attempt": 0,
        "actor_user_id": "u",
        "arguments_hash": "h",
        "started_at": "",
    }
    with_v = dict(base, payload={"verification": {"passed": True}})
    assert to_row(with_v, args={}, step_index=0, tenant_id="t").verification == {
        "passed": True
    }
    without_v = dict(base, payload={"verification": "not-a-dict"})
    assert to_row(without_v, args={}, step_index=0, tenant_id="t").verification is None


# --------------------------------------------------------------------------- #
# 5. Reads are tenant fail-closed                                              #
# --------------------------------------------------------------------------- #
async def test_list_for_run_is_tenant_fail_closed():
    sink = _RecordingSink()
    set_trace_sink(sink)
    run = _run_id()
    await persist_invocation(
        {"run_id": run, "tool": "docs.parse", "status": "ok", "started_at": ""},
        args={},
        run_id=run,
        tenant_id="t-inc46",
        step_id=f"{run}:0:0",
    )
    # Owned tenant ⇒ the row is visible; unresolved tenant ⇒ nothing.
    assert len(await list_for_run("t-inc46", run)) == 1
    assert await list_for_run(None, run) == []
    assert await list_for_run("t-other", run) == []
    # A non-UUID run id can never exist in the UUID column.
    assert await list_for_run("t-inc46", "hub-abc") == []
