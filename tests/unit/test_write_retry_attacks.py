"""Attack tests: write-tool retries must never re-execute side effects.

The pre-fix gate (``retry_policy.write_retry_allowed``) opened on a
*persisted idempotency key alone*. That was unsafe twice over:

* most handlers ignore the key entirely (the executor merely adds it
  to the handler args), so a handler that applied its side effect
  and then raised a retryable error (e.g. the response was lost —
  ``ConnectionError``) was re-executed once per retry attempt: one
  logical write, three side effects;
* two concurrent calls carrying the same key both ran the handler.

And in the react loop, a write that died mid-flight was marked
``confirmed`` on its pending-write entry (its invocation status was
a plain ``error``), so ``verify_resume`` waved the possibly-applied
write through as if its outcome were known.

The contract pinned here:

1. an UNDECLARED write tool is never retried after a failure — the
   handler runs exactly once, the side effect lands exactly once,
   and the outcome is ``outcome_unknown``;
2. a write tool that DECLARES ``idempotent=True`` and really dedupes
   by the key may be retried — the handler is invoked again, but the
   side effect is applied exactly once and the call ends ``ok``;
3. an MCP write tool is never retried (the key never reaches the
   server), even when declared;
4. two concurrent executions under one key run the handler at most
   once — the second is rejected ``blocked``/``duplicate_inflight``,
   and the guard is released afterwards;
5. end-to-end: a write raising an ordinary exception leaves its
   pending-write entry unconfirmed with status ``outcome_unknown``,
   and ``verify_resume`` refuses that checkpoint.
"""

from __future__ import annotations

import asyncio

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.checkpoints import (
    CheckpointResumeRefused,
    InMemoryCheckpointStore,
    reset_checkpoint_store,
    set_checkpoint_store,
    verify_resume,
)
from forgeflow.runtime.orchestrator import reset_run_store
from forgeflow.runtime.react_executor import ReactExecutor
from forgeflow.runtime.retry_policy import StepPolicy
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


def _ctx(**kw) -> ToolCallContext:
    base = dict(
        run_id="run-1",
        step_id="run-1:0:0",
        tenant_id="t-1",
        user_id="u-1",
        role="admin",
        intent="test",
        attempt=0,
        args={},
    )
    base.update(kw)
    return ToolCallContext(**base)


def _tiny_policy(**kw) -> StepPolicy:
    base = dict(
        max_attempts=3,
        initial_backoff_seconds=0.001,
        max_backoff_seconds=0.002,
        jitter_seconds=0.0,
        model_timeout_seconds=None,
        tool_timeout_seconds=None,
    )
    base.update(kw)
    return StepPolicy(**base)


def _register(tool_id: str, handler, *, idempotent: bool = False) -> None:
    # ``idempotent`` is passed only when True so that, against the
    # pre-fix code (which has no such field), the undeclared-tool
    # tests fail on behavior — call counts and statuses — rather
    # than on a TypeError from the registration itself.
    extra = {"idempotent": True} if idempotent else {}
    tool_registry.register(
        ToolBinding(
            tool_id=tool_id,
            handler=handler,
            kind="real",
            provider="test",
            description="attack probe",
            **extra,
        )
    )


async def _with_policy(tool: str, ctx: ToolCallContext):
    return await ReactExecutor()._execute_tool_with_policy(
        tool,
        ctx,
        policy_decision="allow",
        approval_id=None,
        blocked_reason=None,
        policy=_tiny_policy(),
        run_attempt=0,
    )


# --- 1. undeclared write: side effect, then ConnectionError ------------------- #


async def test_undeclared_write_is_not_retried_and_ends_outcome_unknown():
    calls = {"n": 0}
    effects: list[str] = []

    async def _apply_then_drop(args, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        effects.append("write")  # the side effect LANDS…
        raise ConnectionError("connection reset by peer")  # …then the response is lost

    _register("mock.write", _apply_then_drop)  # NOT declared idempotent
    inv = await _with_policy(
        "mock.write",
        _ctx(idempotency_key="run-1:0:call-1", idempotency_persisted=True),
    )
    # Pre-fix this ran the handler 3 times (3 side effects) and
    # reported a plain "error". Now: exactly one execution, and the
    # outcome is honestly unknown.
    assert calls["n"] == 1
    assert effects == ["write"]
    assert inv.status == "outcome_unknown"


# --- 2. declared + deduping write: retried, side effect applied once ---------- #


async def test_declared_idempotent_write_retries_but_applies_once():
    calls = {"n": 0}
    effects: list[str] = []
    stored: dict = {}

    async def _deduping(args, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        key = ctx.idempotency_key
        if key in stored:
            return stored[key]  # replay under a known key: no re-apply
        effects.append("write")
        result = {"ok": True, "written": True}
        stored[key] = result
        if calls["n"] == 1:
            raise ConnectionError("connection reset by peer")
        return result

    _register("mock.write", _deduping, idempotent=True)
    inv = await _with_policy(
        "mock.write",
        _ctx(idempotency_key="run-1:0:call-1", idempotency_persisted=True),
    )
    assert calls["n"] == 2  # one retry happened…
    assert effects == ["write"]  # …but the write was applied exactly once
    assert inv.status == "ok"


# --- 3. MCP write: never retried, even when declared ---------------------------- #


async def test_mcp_write_is_never_retried_even_when_declared():
    calls = {"n": 0}
    effects: list[str] = []

    async def _apply_then_drop(args, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        effects.append("write")
        raise ConnectionError("connection reset by peer")

    # The key is never sent to an MCP server, so a declaration cannot
    # make an MCP write dedupe — the gate must stay shut regardless.
    _register("mcp.internal.sample_write", _apply_then_drop, idempotent=True)
    inv = await _with_policy(
        "mcp.internal.sample_write",
        _ctx(idempotency_key="run-1:0:call-1", idempotency_persisted=True),
    )
    assert calls["n"] == 1
    assert effects == ["write"]
    assert inv.status == "outcome_unknown"


# --- 4. concurrency: same key executes the handler at most once ----------------- #


async def test_concurrent_same_key_write_executes_handler_once():
    executions: list[str] = []

    async def _slow_write(args, ctx):  # noqa: ANN001, ANN202
        executions.append("write")
        await asyncio.sleep(0.05)
        return {"ok": True, "written": True}

    _register("mock.write", _slow_write)
    ctx = _ctx(idempotency_key="run-1:0:call-1", idempotency_persisted=True)
    first, second = await asyncio.gather(
        ToolExecutor().execute("mock.write", ctx=ctx),
        ToolExecutor().execute("mock.write", ctx=ctx),
    )
    assert executions == ["write"]  # the side effect ran at most once
    assert sorted([first.status, second.status]) == ["blocked", "ok"]
    rejected = first if first.status == "blocked" else second
    assert rejected.invoked is False
    assert rejected.payload["duplicate_inflight"] is True

    # The guard is released when the execution finishes: a later,
    # sequential call under the same key (e.g. a resume) runs again.
    third = await ToolExecutor().execute("mock.write", ctx=ctx)
    assert third.status == "ok"
    assert executions == ["write", "write"]


async def test_concurrent_distinct_keys_do_not_interfere():
    executions: list[str] = []

    async def _slow_write(args, ctx):  # noqa: ANN001, ANN202
        executions.append(ctx.idempotency_key)
        await asyncio.sleep(0.05)
        return {"ok": True, "written": True}

    _register("mock.write", _slow_write)
    first, second = await asyncio.gather(
        ToolExecutor().execute(
            "mock.write", ctx=_ctx(idempotency_key="run-1:0:call-1", idempotency_persisted=True)
        ),
        ToolExecutor().execute(
            "mock.write",
            ctx=_ctx(
                step_id="run-1:0:1",
                idempotency_key="run-1:0:call-2",
                idempotency_persisted=True,
            ),
        ),
    )
    assert first.status == "ok"
    assert second.status == "ok"
    assert sorted(executions) == ["run-1:0:call-1", "run-1:0:call-2"]


# --- executor status mapping (fix 2 at the executor seam) ----------------------- #


async def test_executor_maps_write_exception_to_outcome_unknown_not_error():
    async def _explode(args, ctx):  # noqa: ANN001, ANN202
        raise RuntimeError("handler died mid-write")

    _register("mock.write", _explode)
    inv = await ToolExecutor().execute("mock.write", ctx=_ctx())
    assert inv.status == "outcome_unknown"
    assert inv.invoked is True
    assert inv.executed is False


async def test_executor_keeps_read_only_exception_as_plain_error():
    async def _explode(args, ctx):  # noqa: ANN001, ANN202
        raise RuntimeError("handler died mid-read")

    # Probe id is a whitelisted read-only tool (round 2: read-only
    # classification is an exact-id whitelist — the old synthetic
    # ``research.flaky`` id was read-only only via the removed
    # ``research.`` prefix rule, and ``research.*`` writers exist).
    _register("evidence.read", _explode)
    inv = await ToolExecutor().execute("evidence.read", ctx=_ctx())
    assert inv.status == "error"
    assert inv.invoked is True
    assert inv.executed is True


# --- 5. loop level: pending entry stays unconfirmed / outcome_unknown ----------- #


@pytest.fixture
def working_store():
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    reset_run_store()
    yield store
    reset_checkpoint_store()
    reset_run_store()


async def test_loop_write_exception_leaves_pending_entry_outcome_unknown(
    monkeypatch, force_memory_backend, working_store
):
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task
    from tests.unit.test_inc17_react_loop import (
        _patch_models,
        _patch_settings,
        _RecordingBus,
        _ScriptedModel,
    )

    calls = {"n": 0}

    async def _explode(args, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        raise RuntimeError("write exploded mid-flight")

    _register("data.query", _explode)
    _patch_settings(monkeypatch, mode="react", provider="mock")
    fake = _ScriptedModel(
        [
            {
                "content": "执行写入",
                "tool_calls": [{"name": "data.query", "args": {"table": "t"}, "id": "w1"}],
            },
            {"content": "最终答复", "tool_calls": []},
        ]
    )
    _patch_models(monkeypatch, fake)
    task = TaskCreate(intent="查询并记录数据", workflow_type="generic", context={})
    bus = _RecordingBus()
    await run_task(
        task,
        RequestContext(tenant_id="t-inc17", user_id="u-1", role="admin"),
        run_id="run-attack-unknown",
        bus=bus,
    )

    assert calls["n"] == 1
    record = next(r for r in task.context["tool_invocations"] if r["tool"] == "data.query")
    assert record["status"] == "outcome_unknown"

    history = working_store.history("t-inc17", "run-attack-unknown", limit=50)
    entries = [
        entry
        for checkpoint in history
        for entry in (checkpoint.state.get("pending_writes") or [])
        if entry.get("tool") == "data.query"
    ]
    assert entries, "the pre-write ledger entry must have been persisted"
    # The post-execution checkpoint carries the honest verdict:
    # unconfirmed, outcome_unknown — never confirmed/"error".
    unknown_entries = [e for e in entries if e.get("status") == "outcome_unknown"]
    assert unknown_entries, f"no outcome_unknown pending entry in {entries}"
    assert all(e.get("confirmed") is False for e in unknown_entries)
    assert not any(e.get("confirmed") is True and e.get("status") == "error" for e in entries)
    # …and resume verification refuses that checkpoint instead of
    # waving the possibly-applied write through.
    unknown_checkpoint = next(
        checkpoint
        for checkpoint in history
        if any(
            entry.get("tool") == "data.query" and entry.get("status") == "outcome_unknown"
            for entry in (checkpoint.state.get("pending_writes") or [])
        )
    )
    with pytest.raises(CheckpointResumeRefused):
        verify_resume(unknown_checkpoint, run_status="interrupted")
