"""Run session leases — end-to-end behaviour.

Four scenarios through the real seams (``run_task``, the react loop,
``ToolExecutor``, ``resume_run_from_checkpoint``):

a. two workers race the same run → exactly one executes, the other is
   refused with the lease error, and the write tool runs exactly once;
b. takeover after expiry: worker B resumes the run to completion and a
   write worker A already confirmed in the pending-write ledger is NOT
   re-executed;
c. a stale owner's delayed write through the real ``ToolExecutor``
   path is fenced off (blocked, did-not-execute);
d. a lease taken over mid-execution stops the run safely: no further
   writes, explicit ``terminated_by="lease_lost"`` termination, and no
   pending entry confirmed after the loss.

Every test fails on unmodified main: the lease module, the
``lease_owner`` plumbing, the fencing fields and the lease-lost
termination do not exist there.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, ToolMessage

import forgeflow.config as config_mod
import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime import tool_registry
from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    pending_write_entry,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, reset_run_store, run_task
from forgeflow.runtime.run_lease import (
    InMemoryRunLeaseStore,
    RunLease,
    RunLeaseHeldError,
    reset_run_lease_store,
    set_run_lease_store,
)
from forgeflow.runtime.tool_registry import ToolBinding

pytestmark = pytest.mark.asyncio

_TENANT = "t-inc17"


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


@pytest.fixture(autouse=True)
def _clean_stores():
    reset_run_store()
    yield
    reset_run_store()
    reset_checkpoint_store()
    reset_run_lease_store()


def _register_write_probe(writes: list) -> None:
    async def _handler(args, ctx):  # noqa: ANN001, ANN202
        writes.append(dict(args))
        return {"ok": True, "written": True}

    tool_registry.register(
        ToolBinding(
            tool_id="data.query",
            handler=_handler,
            kind="real",
            provider="test",
            description="counting write probe",
        )
    )


def _patch_model_factory(monkeypatch, factory) -> None:  # noqa: ANN001
    """test_react_wiring settings patch + a per-call model factory."""
    from tests.unit.test_react_wiring import _SettingsProxy

    proxy = _SettingsProxy(
        get_settings(),
        agent_runtime_mode="react",
        llm_provider="mock",
        agent_tool_loading="direct",
    )
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_mod, "get_settings", lambda: proxy)
    monkeypatch.setattr(orch, "_build_planner_models", factory)


class _ScriptedAsyncModel:
    """_FakeReactModel-style scripted model (ainvoke only, no bind_tools).

    ``gate`` (optional) is an (entered, release) event pair: the first
    ainvoke signals ``entered`` and waits for ``release`` — used to hold
    worker A inside its run while worker B attempts the same run.
    """

    def __init__(self, script: list[dict], gate=None) -> None:  # noqa: ANN001
        self._script = list(script)
        self._gate = gate
        self.calls: list[list] = []
        self.model = "fake-react-model"
        self._llm_type = "fake"

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(list(messages))
        if self._gate is not None:
            entered, release = self._gate
            if not entered.is_set():
                entered.set()
                await release.wait()
        item = self._script.pop(0) if self._script else {"content": "收尾", "tool_calls": []}
        return AIMessage(
            content=item.get("content", ""),
            tool_calls=item.get("tool_calls", []),
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )


def _write_script() -> list[dict]:
    return [
        {
            "content": "执行写入",
            "tool_calls": [{"name": "data.query", "args": {"table": "t"}, "id": "w1"}],
        },
        {"content": "最终答复", "tool_calls": []},
    ]


# --------------------------------------------------------------------------- #
# (a) Two workers, one run: exactly one executes
# --------------------------------------------------------------------------- #
async def test_two_workers_race_same_run_exactly_one_executes(monkeypatch, force_memory_backend):
    from tests.unit.test_checkpoint_resume import _RecordingBus

    set_checkpoint_store(InMemoryCheckpointStore())
    set_run_lease_store(InMemoryRunLeaseStore())
    writes: list = []
    _register_write_probe(writes)

    entered = asyncio.Event()
    release = asyncio.Event()
    created: list[_ScriptedAsyncModel] = []

    def _factory():
        gate = (entered, release) if not created else None
        fake = _ScriptedAsyncModel(_write_script(), gate=gate)
        created.append(fake)
        return (
            fake,
            fake,
            [{"slot": "strong", "class": "_ScriptedAsyncModel", "model": fake.model}],
        )

    _patch_model_factory(monkeypatch, _factory)
    ctx = RequestContext(tenant_id=_TENANT, user_id="u-1", role="admin")

    task_a = TaskCreate(intent="查询并记录数据", workflow_type="generic", context={})
    run_a = asyncio.create_task(
        run_task(task_a, ctx, run_id="run-race", bus=_RecordingBus(), lease_owner="worker-a")
    )
    # Worker A is inside its run (lease held) before B even attempts.
    await asyncio.wait_for(entered.wait(), timeout=10)

    task_b = TaskCreate(intent="查询并记录数据", workflow_type="generic", context={})
    with pytest.raises(RunLeaseHeldError) as excinfo:
        await run_task(task_b, ctx, run_id="run-race", bus=_RecordingBus(), lease_owner="worker-b")
    err = excinfo.value
    assert err.owner == "worker-a"
    assert err.expires_at is not None
    assert "worker-a" in str(err) and "leased" in str(err)
    assert writes == []  # B executed nothing; A's write hasn't run yet either

    release.set()
    handle = await asyncio.wait_for(run_a, timeout=15)
    assert handle.run_id == "run-race"
    # The run's write tool executed exactly once overall.
    assert len(writes) == 1


# --------------------------------------------------------------------------- #
# (b) Takeover after expiry + pending-write ledger is not replayed
# --------------------------------------------------------------------------- #
class _FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class _RecordingLeaseStore(InMemoryRunLeaseStore):
    def __init__(self, now) -> None:  # noqa: ANN001
        super().__init__(now=now)
        self.acquired: list[RunLease] = []

    def acquire(self, *args):  # noqa: ANN002, ANN202
        lease = super().acquire(*args)
        if lease is not None:
            self.acquired.append(lease)
        return lease


async def test_takeover_resume_does_not_replay_confirmed_write(monkeypatch, force_memory_backend):
    from forgeflow.runtime.checkpoint_resume import resume_run_from_checkpoint
    from forgeflow.runtime.checkpoints import (
        CheckpointResumeRefused,
        build_state,
    )
    from forgeflow.runtime.orchestrator import RunRecord, get_run_store
    from tests.unit.test_checkpoint_resume import (
        _FakeReactModel,
        _patch_react,
        _wait_terminal,
    )

    clock = _FakeClock()
    lease_store = _RecordingLeaseStore(clock)
    set_run_lease_store(lease_store)
    checkpoint_store = InMemoryCheckpointStore()
    set_checkpoint_store(checkpoint_store)
    writes: list = []
    _register_write_probe(writes)

    # Worker A holds the source run's lease with a short TTL and stalls.
    lease_a = lease_store.acquire("t-react", "src-1", "worker-a", 30.0)
    assert lease_a is not None and lease_a.fencing_token == 1

    # A's run already executed + confirmed one write before stalling:
    # the ledger entry is confirmed/ok and the invocation is on record.
    entry = pending_write_entry("src-1", 1, "w1", "data.query")
    entry["confirmed"] = True
    entry["status"] = "ok"
    state = build_state(
        intent="查询并记录数据",
        requirements=[],
        acceptance={"round": 1, "complete": False, "requirements": []},
        next_step={"kind": "model_call"},
        codeplane_ref=None,
        breaker_summary={},
        stuck_summary={},
        approval=None,
        pending_writes=[entry],
        trajectory_cursor=1,
    )
    checkpoint_store.save("t-react", run_id="src-1", round=1, kind="round", state=state)
    get_run_store().save(
        RunRecord(
            run_id="src-1",
            thread_id="src-1",
            tenant_id="t-react",
            agent_id=None,
            intent="查询并记录数据",
            status="interrupted",
            outcome="",
            steps=[],
            errors=[],
            created_at="2026-11-04T00:00:00+00:00",
            tool_invocations=[
                {"tool": "data.query", "status": "ok", "payload": {"ok": True}, "executed": True}
            ],
            declared_inputs={},
            workflow_type="generic",
            session_id="",
            actor_user_id="u-1",
            actor_role="admin",
        )
    )

    # While A holds the lease, B's resume is refused with the lease error.
    with pytest.raises(CheckpointResumeRefused, match="leased by worker-a"):
        await resume_run_from_checkpoint(
            tenant="t-react", user_id="u-1", role="admin", run_id="src-1", lease_owner="worker-b"
        )

    # After expiry, B takes over (token strictly greater) and resumes.
    clock.advance(31.0)
    fake = _FakeReactModel([])
    _patch_react(monkeypatch, fake)
    handle = await resume_run_from_checkpoint(
        tenant="t-react", user_id="u-1", role="admin", run_id="src-1", lease_owner="worker-b"
    )
    assert handle.parent_run_id == "src-1"
    record = await _wait_terminal(handle.run_id)
    assert record.status == "completed"

    tokens = [(lease.owner, lease.fencing_token) for lease in lease_store.acquired]
    assert ("worker-a", 1) in tokens
    assert ("worker-b", 2) in tokens  # takeover bumped the fencing token
    # A already confirmed the write in the ledger; B did NOT re-execute it.
    assert writes == []


# --------------------------------------------------------------------------- #
# (c) Stale owner's delayed write is fenced at the ToolExecutor seam
# --------------------------------------------------------------------------- #
async def test_stale_owner_write_fenced_after_takeover(force_memory_backend):
    from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor

    clock = _FakeClock()
    store = InMemoryRunLeaseStore(now=clock)
    set_run_lease_store(store)
    writes: list = []
    _register_write_probe(writes)

    lease_a = store.acquire("t-1", "run-1", "worker-a", 30.0)
    assert lease_a is not None
    clock.advance(31.0)
    lease_b = store.acquire("t-1", "run-1", "worker-b", 30.0)
    assert lease_b is not None and lease_b.fencing_token > lease_a.fencing_token

    def _ctx(owner: str, token: int) -> ToolCallContext:
        return ToolCallContext(
            run_id="run-1",
            step_id="run-1:0:0",
            tenant_id="t-1",
            user_id="u-1",
            role="admin",
            intent="test",
            attempt=0,
            args={"table": "t"},
            idempotency_key="run-1:1:w1",
            idempotency_persisted=True,
            lease_owner=owner,
            lease_fencing_token=token,
        )

    stale = await ToolExecutor().execute("data.query", ctx=_ctx("worker-a", lease_a.fencing_token))
    assert stale.status == "blocked"
    assert stale.executed is False and stale.invoked is False
    assert stale.payload["lease_fenced"] is True
    assert writes == []  # the handler never ran: side-effect count unchanged

    fresh = await ToolExecutor().execute("data.query", ctx=_ctx("worker-b", lease_b.fencing_token))
    assert fresh.status == "ok"
    assert len(writes) == 1


# --------------------------------------------------------------------------- #
# (d) Renewal failure mid-execution → safe lease-lost exit
# --------------------------------------------------------------------------- #
class _TakeoverModel:
    """First loop call writes W1; the call after W1's result takes the
    lease over (as another worker would after expiry), waits for the
    renewal task to notice, then tries a second write W2."""

    def __init__(self, store: InMemoryRunLeaseStore) -> None:
        self._store = store
        self.calls: list[list] = []
        self.model = "fake-react-model"
        self._llm_type = "fake"
        self._w1_sent = False
        self._took_over = False

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append(list(messages))
        saw_w1 = any(isinstance(m, ToolMessage) and m.tool_call_id == "w1" for m in messages)
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        if saw_w1 and not self._took_over:
            self._took_over = True
            # Force expiry, then take over exactly as a peer worker does:
            # a fresh acquire that bumps the fencing token.
            row = self._store._rows[(_TENANT, "run-lease-lost")]
            row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            taken = self._store.acquire(_TENANT, "run-lease-lost", "worker-b", 60.0)
            assert taken is not None and taken.fencing_token == 2
            # The renewal interval is pinned to 0.1s via task context;
            # give the renewal task a few ticks to observe the takeover
            # (token mismatch → renew fails) and mark the lease lost.
            await asyncio.sleep(0.5)
            return AIMessage(
                content="继续写入",
                tool_calls=[{"name": "data.query", "args": {"table": "t2"}, "id": "w2"}],
                usage_metadata=usage,
            )
        if not self._w1_sent:
            self._w1_sent = True
            return AIMessage(
                content="执行写入",
                tool_calls=[{"name": "data.query", "args": {"table": "t1"}, "id": "w1"}],
                usage_metadata=usage,
            )
        return AIMessage(content="收尾", tool_calls=[], usage_metadata=usage)


async def test_renewal_failure_stops_run_with_lease_lost(monkeypatch, force_memory_backend):
    from tests.unit.test_checkpoint_resume import _RecordingBus

    checkpoint_store = InMemoryCheckpointStore()
    set_checkpoint_store(checkpoint_store)
    lease_store = InMemoryRunLeaseStore()
    set_run_lease_store(lease_store)
    writes: list = []
    _register_write_probe(writes)

    model = _TakeoverModel(lease_store)

    def _factory():
        return (model, model, [{"slot": "strong", "class": "_TakeoverModel", "model": model.model}])

    _patch_model_factory(monkeypatch, _factory)
    bus = _RecordingBus()
    task = TaskCreate(
        intent="查询并记录数据",
        workflow_type="generic",
        # Long TTL (the lease can only be lost via the takeover below,
        # never by the clock) + fast renewal cadence, both via the
        # task-context overrides — deterministic, no expiry races.
        context={"run_lease_ttl_seconds": 30.0, "run_lease_renew_interval_seconds": 0.1},
    )
    handle = await run_task(
        task,
        RequestContext(tenant_id=_TENANT, user_id="u-1", role="admin"),
        run_id="run-lease-lost",
        bus=bus,
        lease_owner="worker-a",
    )

    # W1 executed exactly once; W2 (after the loss) never reached the
    # handler. (The recorded args are the executor's prepared payload,
    # so the count is the assertion — exactly one write happened.)
    assert len(writes) == 1
    # Explicit lease-lost termination on the run's runtime meta + warning.
    runtime = task.context.get("llm_runtime") or {}
    assert runtime.get("terminated_by") == "lease_lost"
    warnings = [d for t, d in bus.events if t == "run.warning" and d.get("reason") == "lease_lost"]
    assert warnings, "a run.warning with reason=lease_lost must be emitted"
    # No pending entry was confirmed after the loss: the only ledger
    # entry is W1's (confirmed BEFORE the loss); W2 never got one.
    latest = checkpoint_store.latest(_TENANT, "run-lease-lost")
    assert latest is not None
    entries = [e for e in latest.state.get("pending_writes") or [] if e.get("tool") == "data.query"]
    assert entries and all(e.get("confirmed") is True for e in entries)
    assert handle.run_id == "run-lease-lost"
    # The run is terminally failed — a lease-lost attempt must not be
    # replanned into a "completed" the stale owner no longer owns.
    assert handle.status == "failed"
