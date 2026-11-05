"""Write-retry gate wiring: persisted idempotency key → step policy.

Integration coverage for the seam between the checkpoint ledger
(PR: pre-write persistence of pending writes) and the core-loop step
policy (PR: unified retry). On their own bases each side was tested
in isolation:

* the loop persisted a pending write + idempotency key BEFORE a
  write-class tool ran and blocked the write when that save failed
  (``test_checkpoint_pending_writes``);
* the retry wrapper retried a failed write only when its
  ``ToolCallContext`` carried a key with ``idempotency_persisted``
  set (``test_retry_policy``) — but nothing set the flag.

These tests pin the wiring itself, end-to-end through the react
loop: when the pre-write checkpoint save succeeds, the context the
tool executes under carries the *persisted* key and the persisted
flag, so a retryable failure is retried under the same key; read-only
tools keep the defaults (no key, flag unset).
"""

from __future__ import annotations

import pytest

from forgeflow.runtime import tool_registry
from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.orchestrator import reset_run_store
from forgeflow.runtime.tool_registry import ToolBinding

pytestmark = pytest.mark.asyncio

_FAST_RETRY = {
    "retry_initial_backoff_seconds": 0.001,
    "retry_max_backoff_seconds": 0.002,
    "retry_jitter_seconds": 0.0,
}


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


@pytest.fixture
def working_store():
    """A checkpoint store that can actually save (the pre-write
    persistence is fail-closed, so loop tests executing write tools
    need one) — the instance is handed back for ledger assertions."""
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    reset_run_store()
    yield store
    reset_checkpoint_store()
    reset_run_store()


def _register_write(handler) -> None:
    tool_registry.register(
        ToolBinding(
            tool_id="data.query",
            handler=handler,
            kind="real",
            provider="test",
            description="wiring probe write tool",
        )
    )


async def _drive(monkeypatch, script, *, run_id: str, context=None):
    """Run one react task through the inc17 scripted-model harness."""
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task
    from tests.unit.test_inc17_react_loop import (
        _patch_models,
        _patch_settings,
        _RecordingBus,
        _ScriptedModel,
    )

    _patch_settings(monkeypatch, mode="react", provider="mock")
    fake = _ScriptedModel(script)
    _patch_models(monkeypatch, fake)
    task = TaskCreate(intent="查询并记录数据", workflow_type="generic", context=context or {})
    bus = _RecordingBus()
    await run_task(
        task,
        RequestContext(tenant_id="t-inc17", user_id="u-1", role="admin"),
        run_id=run_id,
        bus=bus,
    )
    return fake, task, bus


def _write_call_script(final_text: str) -> list[dict]:
    return [
        {
            "content": "执行写入",
            "tool_calls": [{"name": "data.query", "args": {"table": "t"}, "id": "w1"}],
        },
        {"content": final_text, "tool_calls": []},
    ]


# --- the context carries the persisted key + flag ------------------------------ #


async def test_write_tool_context_carries_persisted_key_and_flag(
    monkeypatch, force_memory_backend, working_store
):
    seen: list = []

    async def _handler(args, ctx):  # noqa: ANN001, ANN202
        seen.append(ctx)
        return {"ok": True, "written": True}

    _register_write(_handler)
    _fake, task, _bus = await _drive(
        monkeypatch, _write_call_script("最终答复：写入完成"), run_id="run-wire-ctx"
    )

    assert len(seen) == 1
    ctx = seen[0]
    # The ledger entry persisted for this call carries the same key
    # the tool executed under, and it ended confirmed/ok — the flag
    # the handler saw must therefore be True, not just the key set.
    latest = working_store.latest("t-inc17", "run-wire-ctx")
    assert latest is not None
    entries = [
        e for e in latest.state.get("pending_writes") or [] if e.get("tool") == "data.query"
    ]
    assert entries, "pre-write persistence must have recorded the pending write"
    assert ctx.idempotency_key == entries[0]["idempotency_key"]
    assert ctx.idempotency_persisted is True
    assert entries[0]["confirmed"] is True
    assert entries[0]["status"] == "ok"
    record = next(r for r in task.context["tool_invocations"] if r["tool"] == "data.query")
    assert record["status"] == "ok"


# --- a retryable write failure is retried under the persisted key -------------- #


async def test_write_tool_retried_once_after_retryable_failure(
    monkeypatch, force_memory_backend, working_store
):
    seen: list = []

    async def _flaky(args, ctx):  # noqa: ANN001, ANN202
        seen.append(ctx)
        if len(seen) == 1:
            raise RuntimeError("connection reset by peer")
        return {"ok": True, "written": True}

    _register_write(_flaky)
    _fake, task, _bus = await _drive(
        monkeypatch,
        _write_call_script("最终答复：重试后写入成功"),
        run_id="run-wire-retry",
        context=dict(_FAST_RETRY),
    )

    # Exactly one retry, and both attempts ran under the SAME
    # persisted idempotency key — that sameness is what makes the
    # retry safe (a key-honoring tool dedupes the second attempt).
    assert len(seen) == 2
    assert seen[0].idempotency_key
    assert seen[0].idempotency_key == seen[1].idempotency_key
    assert seen[0].idempotency_persisted is True
    assert seen[1].idempotency_persisted is True
    record = next(r for r in task.context["tool_invocations"] if r["tool"] == "data.query")
    assert record["status"] == "ok"


# --- read-only tools keep the defaults ------------------------------------------ #


async def test_read_only_tool_context_keeps_idempotency_defaults(
    monkeypatch, force_memory_backend, working_store
):
    seen: list = []

    async def _search(args, ctx):  # noqa: ANN001, ANN202
        seen.append(ctx)
        return {"ok": True, "provider": "test", "summary": "found"}

    tool_registry.register(
        ToolBinding(
            tool_id="research.search",
            handler=_search,
            kind="real",
            provider="test",
            description="wiring probe read tool",
        )
    )
    script = [
        {
            "content": "检索",
            "tool_calls": [{"name": "research.search", "args": {"query": "q"}, "id": "r1"}],
        },
        {"content": "最终答复", "tool_calls": []},
    ]
    await _drive(monkeypatch, script, run_id="run-wire-readonly")

    assert len(seen) == 1
    # No pending write is persisted for a read tool, so its context
    # keeps the additive defaults — its retries come from the
    # executor's read-only transient retry, not the write gate.
    assert seen[0].idempotency_key is None
    assert seen[0].idempotency_persisted is False
