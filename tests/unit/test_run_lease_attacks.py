"""PR #6 review — regression tests for the three lease attacks.

Each test pins one attack the review demonstrated against the original
PR #6 head (d544cc8) and fails there:

1. Mid-handler takeover: the pre-handler fencing check was
   check-then-act.  (a) A built-in store-backed write whose handler is
   taken over mid-execution must NOT persist at the store commit;
   (b) an MCP-style external write in the same situation must come
   back ``outcome_unknown`` (never confirmed) with the run's lease
   session marked lost; (c) end-to-end, the run then terminates
   ``lease_lost`` with the pending entry unconfirmed; (d) a takeover
   observed at the round-checkpoint save means no round checkpoint is
   persisted by the stale owner.
2. Cross-process exclusion: without a Postgres backend, a
   production-like configuration must fail closed (resolution raises;
   two forked processes can no longer both mint token 1) instead of
   silently falling back to a process-local store.
3. Forged event identity: an unsigned (or invalidly signed) task event
   declaring a victim tenant + elevated role is refused; a correctly
   signed event dispatches under its declared tenant with the
   server-resolved ``service`` role; tenant-less runs still lease
   (under the fixed system tenant) and exclude each other.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import multiprocessing as mp
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.documents import DocArtifactStore
from forgeflow.documents.review_store import (
    InMemoryArtifactReviewStore,
    reset_artifact_review_store,
    set_artifact_review_store,
)
from forgeflow.events.dispatcher import EventDispatcher
from forgeflow.runtime import tool_registry
from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, reset_run_store, run_task
from forgeflow.runtime.run_lease import (
    InMemoryRunLeaseStore,
    RunLeaseSession,
    current_run_lease,
    get_run_lease_store,
    reset_run_lease_store,
    set_run_lease_store,
)
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.tool_registry import ToolBinding
from tests.unit.test_run_lease_e2e import (
    _TENANT,
    _FakeClock,
    _patch_model_factory,
    _register_write_probe,
)


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
    reset_artifact_review_store()


def _take_over(store: InMemoryRunLeaseStore, tenant: str, run_id: str, new_owner: str):
    """Force-expire the current row and acquire as ``new_owner`` —
    exactly what a peer worker does after the holder stalls."""
    row = store._rows[(tenant, run_id)]
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    taken = store.acquire(tenant, run_id, new_owner, 60.0)
    assert taken is not None
    return taken


def _tool_ctx(run_id: str, owner: str, token: int) -> ToolCallContext:
    return ToolCallContext(
        run_id=run_id,
        step_id=f"{run_id}:0:0",
        tenant_id="t-1",
        user_id="u-1",
        role="admin",
        intent="test",
        attempt=0,
        args={"table": "t"},
        idempotency_key=f"{run_id}:1:w1",
        idempotency_persisted=True,
        lease_owner=owner,
        lease_fencing_token=token,
    )


# --------------------------------------------------------------------------- #
# Attack 1a — built-in store-backed write: commit-time fencing
# --------------------------------------------------------------------------- #
async def test_mid_handler_takeover_commit_fenced_at_artifact_store(tmp_path, force_memory_backend):
    clock = _FakeClock()
    lease_store = InMemoryRunLeaseStore(now=clock)
    set_run_lease_store(lease_store)
    review_store = InMemoryArtifactReviewStore(blob_store=DocArtifactStore(root=tmp_path))
    set_artifact_review_store(review_store)

    lease_a = lease_store.acquire("t-1", "run-1", "worker-a", 30.0)
    assert lease_a is not None and lease_a.fencing_token == 1
    session = RunLeaseSession(store=lease_store, lease=lease_a, ttl_seconds=30.0)
    token = current_run_lease.set(session)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _edit_handler(args, ctx):  # noqa: ANN001, ANN202
        entered.set()
        await release.wait()
        # The commit the document/text/sheet edit tools perform,
        # through the real store, AFTER the takeover below.
        review_store.create_pending_version(
            "t-1",
            artifact_id="art-1",
            content=b"edited-bytes",
            fmt="text",
            run_id="run-1",
        )
        return {"ok": True, "written": True}

    tool_registry.register(
        ToolBinding(
            tool_id="document.edit",
            handler=_edit_handler,
            kind="real",
            provider="test",
            description="gated edit probe committing via the artifact review store",
        )
    )
    try:
        execution = asyncio.create_task(
            ToolExecutor().execute("document.edit", ctx=_tool_ctx("run-1", "worker-a", 1))
        )
        await asyncio.wait_for(entered.wait(), timeout=10)
        # Takeover lands WHILE the handler is in flight.
        clock.advance(31.0)
        lease_b = lease_store.acquire("t-1", "run-1", "worker-b", 30.0)
        assert lease_b is not None and lease_b.fencing_token == 2
        release.set()
        invocation = await asyncio.wait_for(execution, timeout=10)
    finally:
        current_run_lease.reset(token)

    # The store refused the commit: no version row was persisted, and
    # the write-class handler failure is recorded outcome_unknown —
    # never a confirmed success by the stale owner.
    assert review_store.list_versions("t-1", "art-1") == []
    assert invocation.status == "outcome_unknown"
    assert invocation.payload is None or invocation.payload.get("ok") is not True
    # The fence observation marks the session lost so the loop stops.
    assert session.lost is True


# --------------------------------------------------------------------------- #
# Attack 1b — MCP-style external write: post-completion re-validation
# --------------------------------------------------------------------------- #
async def test_mid_handler_takeover_mcp_write_becomes_outcome_unknown(force_memory_backend):
    clock = _FakeClock()
    lease_store = InMemoryRunLeaseStore(now=clock)
    set_run_lease_store(lease_store)

    lease_a = lease_store.acquire("t-1", "run-2", "worker-a", 30.0)
    assert lease_a is not None
    session = RunLeaseSession(store=lease_store, lease=lease_a, ttl_seconds=30.0)
    token = current_run_lease.set(session)

    entered = asyncio.Event()
    release = asyncio.Event()
    side_effects: list = []

    async def _mcp_handler(args, ctx):  # noqa: ANN001, ANN202
        entered.set()
        await release.wait()
        side_effects.append(dict(args))  # the external effect happens
        return {"ok": True, "written": True}

    tool_registry.register(
        ToolBinding(
            tool_id="mcp.demo.write",
            handler=_mcp_handler,
            kind="real",
            provider="mcp",
            description="external write probe (cannot be fenced atomically)",
        )
    )
    try:
        execution = asyncio.create_task(
            ToolExecutor().execute("mcp.demo.write", ctx=_tool_ctx("run-2", "worker-a", 1))
        )
        await asyncio.wait_for(entered.wait(), timeout=10)
        clock.advance(31.0)
        lease_b = lease_store.acquire("t-1", "run-2", "worker-b", 30.0)
        assert lease_b is not None and lease_b.fencing_token == 2
        release.set()
        invocation = await asyncio.wait_for(execution, timeout=10)
    finally:
        current_run_lease.reset(token)

    # The external effect may have happened — the platform cannot take
    # it back — but it is NEVER confirmed: outcome_unknown, with the
    # lease-loss marker, and the session is marked lost.
    assert len(side_effects) == 1
    assert invocation.status == "outcome_unknown"
    assert invocation.payload["lease_lost_after_write"] is True
    assert invocation.payload["ok"] is False
    assert session.lost is True


# --------------------------------------------------------------------------- #
# Attack 1c — end-to-end: post-checked write loss terminates lease_lost
# --------------------------------------------------------------------------- #
class _TakeoverDuringWriteModel:
    """One write call; the write handler itself performs the takeover
    mid-execution (as a peer worker would after expiry) and then
    reports success — the stale owner's 'ok' must not survive."""

    def __init__(self, store: InMemoryRunLeaseStore) -> None:
        self._store = store
        self.model = "fake-react-model"
        self._llm_type = "fake"
        self._sent = False

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        if not self._sent:
            self._sent = True
            return AIMessage(
                content="执行写入",
                tool_calls=[{"name": "data.query", "args": {"table": "t"}, "id": "w1"}],
                usage_metadata=usage,
            )
        return AIMessage(content="收尾", tool_calls=[], usage_metadata=usage)


async def test_post_checked_write_loss_terminates_run_lease_lost(monkeypatch, force_memory_backend):
    from tests.unit.test_checkpoint_resume import _RecordingBus

    checkpoint_store = InMemoryCheckpointStore()
    set_checkpoint_store(checkpoint_store)
    lease_store = InMemoryRunLeaseStore()
    set_run_lease_store(lease_store)
    writes: list = []

    async def _takeover_handler(args, ctx):  # noqa: ANN001, ANN202
        writes.append(dict(args))
        _take_over(lease_store, _TENANT, "run-mcp-loss", "worker-b")
        return {"ok": True, "written": True}

    tool_registry.register(
        ToolBinding(
            tool_id="data.query",
            handler=_takeover_handler,
            kind="real",
            provider="test",
            description="external-style write that loses its lease mid-flight",
        )
    )
    model = _TakeoverDuringWriteModel(lease_store)

    def _factory():
        return (model, model, [{"slot": "strong", "class": "_TakeoverModel", "model": model.model}])

    _patch_model_factory(monkeypatch, _factory)
    bus = _RecordingBus()
    task = TaskCreate(
        intent="查询并记录数据",
        workflow_type="generic",
        context={"run_lease_ttl_seconds": 30.0, "run_lease_renew_interval_seconds": 0.1},
    )
    handle = await run_task(
        task,
        RequestContext(tenant_id=_TENANT, user_id="u-1", role="admin"),
        run_id="run-mcp-loss",
        bus=bus,
        lease_owner="worker-a",
    )

    assert len(writes) == 1
    runtime = task.context.get("llm_runtime") or {}
    assert runtime.get("terminated_by") == "lease_lost"
    warnings = [d for t, d in bus.events if t == "run.warning" and d.get("reason") == "lease_lost"]
    assert warnings, "a run.warning with reason=lease_lost must be emitted"
    assert handle.status == "failed"
    # The pending entry for the lost write stays UNconfirmed in the
    # persisted ledger — verify_resume will refuse a blind replay.
    # (The loop flips its in-memory copy to outcome_unknown, but the
    # only later save — the round checkpoint — is itself lease-guarded
    # and skipped, so the durable record is the pre-write snapshot:
    # status "pending", confirmed False.  What must never appear is a
    # confirmed/ok entry for the lost write.)
    latest = checkpoint_store.latest(_TENANT, "run-mcp-loss")
    assert latest is not None
    entries = [e for e in latest.state.get("pending_writes") or [] if e.get("tool") == "data.query"]
    assert entries, "the write must have a pending-ledger entry"
    assert all(e.get("confirmed") is not True for e in entries)
    assert all(e.get("status") != "ok" for e in entries)


# --------------------------------------------------------------------------- #
# Attack 1d — round-checkpoint save is lease-guarded (minor fix)
# --------------------------------------------------------------------------- #
class _TakeoverDuringReadModel:
    """Round 1 is a two-read batch; the second read performs the
    takeover.  The batch's round checkpoint (saved after the batch)
    must NOT be persisted by the now-stale owner."""

    def __init__(self) -> None:
        self.model = "fake-react-model"
        self._llm_type = "fake"
        self._sent = False

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        if not self._sent:
            self._sent = True
            return AIMessage(
                content="读取两项",
                tool_calls=[
                    {"name": "research.read", "args": {"url": "https://a.example"}, "id": "r1"},
                    {"name": "research.read", "args": {"url": "https://b.example"}, "id": "r2"},
                ],
                usage_metadata=usage,
            )
        return AIMessage(
            content="尝试写入",
            tool_calls=[{"name": "data.query", "args": {"table": "t"}, "id": "w1"}],
            usage_metadata=usage,
        )


async def test_round_checkpoint_not_saved_after_lease_loss(monkeypatch, force_memory_backend):
    from tests.unit.test_checkpoint_resume import _RecordingBus

    checkpoint_store = InMemoryCheckpointStore()
    set_checkpoint_store(checkpoint_store)
    lease_store = InMemoryRunLeaseStore()
    set_run_lease_store(lease_store)
    writes: list = []
    _register_write_probe(writes)
    reads: list = []

    async def _read_handler(args, ctx):  # noqa: ANN001, ANN202
        reads.append(dict(args))
        if len(reads) == 2:
            _take_over(lease_store, _TENANT, "run-round-save", "worker-b")
        return {"ok": True, "read": True}

    tool_registry.register(
        ToolBinding(
            tool_id="research.read",
            handler=_read_handler,
            kind="real",
            provider="test",
            description="read probe; second call takes the lease over",
        )
    )
    model = _TakeoverDuringReadModel()

    def _factory():
        return (model, model, [{"slot": "strong", "class": "_RoundModel", "model": model.model}])

    _patch_model_factory(monkeypatch, _factory)
    task = TaskCreate(
        intent="读取并记录",
        workflow_type="generic",
        context={"run_lease_ttl_seconds": 30.0, "run_lease_renew_interval_seconds": 0.1},
    )
    handle = await run_task(
        task,
        RequestContext(tenant_id=_TENANT, user_id="u-1", role="admin"),
        run_id="run-round-save",
        bus=_RecordingBus(),
        lease_owner="worker-a",
    )

    assert len(reads) == 2
    # The stale owner never executed the follow-up write...
    assert writes == []
    # ...the run terminated as lease_lost...
    runtime = task.context.get("llm_runtime") or {}
    assert runtime.get("terminated_by") == "lease_lost"
    assert handle.status == "failed"
    # ...and NO round checkpoint was persisted for the run at all:
    # there were no write-class calls (so no pre-write saves) and the
    # post-batch round save was refused by the lease guard.
    assert checkpoint_store.history(_TENANT, "run-round-save", limit=50) == []


# --------------------------------------------------------------------------- #
# Attack 2 — cross-process exclusion must fail closed without PG
# --------------------------------------------------------------------------- #
def test_lease_resolution_fails_closed_prod_memory_profile(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "storage_backend", "memory")
    reset_run_lease_store()
    try:
        with pytest.raises(RuntimeError, match="run leases require the Postgres backend"):
            get_run_lease_store()
    finally:
        reset_run_lease_store()


def test_lease_resolution_fails_closed_staging_without_dsn(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "staging")
    monkeypatch.setattr(settings, "storage_backend", "postgres")
    monkeypatch.delenv("POSTGRES_SYNC_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    reset_run_lease_store()
    try:
        with pytest.raises(RuntimeError, match="run leases require the Postgres backend"):
            get_run_lease_store()
    finally:
        reset_run_lease_store()


def test_lease_resolution_dev_mode_allows_inmemory(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "storage_backend", "memory")
    reset_run_lease_store()
    try:
        store = get_run_lease_store()
        assert isinstance(store, InMemoryRunLeaseStore)
        lease = store.acquire("t", "run-dev", "worker-a", 60.0)
        assert lease is not None and lease.fencing_token == 1
    finally:
        reset_run_lease_store()


def test_lease_resolution_prod_with_postgres_backend_ok(monkeypatch):
    from forgeflow.runtime.run_lease import PostgresRunLeaseStore

    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "storage_backend", "postgres")
    # POSTGRES_SYNC_URL is pinned by conftest; construction is lazy
    # (no dial), so resolution alone must succeed.
    reset_run_lease_store()
    try:
        assert isinstance(get_run_lease_store(), PostgresRunLeaseStore)
    finally:
        reset_run_lease_store()


def _mp_lease_attempt(result_queue) -> None:  # noqa: ANN001
    """Child process: production-like config, no Postgres profile.

    Both children must fail closed; before the fix each silently built
    its own in-memory store and acquired fencing token 1.
    """
    os.environ["APP_ENV"] = "prod"
    os.environ["STORAGE_BACKEND"] = "memory"
    from forgeflow.config import get_settings as _get_settings

    _get_settings.cache_clear()
    from forgeflow.runtime.run_lease import (
        get_run_lease_store as _get_store,
    )
    from forgeflow.runtime.run_lease import (
        reset_run_lease_store as _reset_store,
    )

    _reset_store()
    try:
        store = _get_store()
        lease = store.acquire("t-mp", "run-mp", "worker", 60.0)
        result_queue.put(("acquired", lease.fencing_token if lease is not None else None))
    except Exception as exc:  # noqa: BLE001 — report, don't crash the queue
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def test_two_processes_cannot_both_mint_token_one():
    ctx = mp.get_context("fork")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_mp_lease_attempt, args=(queue,)) for _ in range(2)]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0
    # Neither process obtained a lease store: both failed closed with
    # the configuration error instead of each minting token 1 in a
    # private in-memory store.
    assert all(kind == "error" for kind, _ in results), results
    assert all("Postgres backend" in str(detail) for _, detail in results), results


# --------------------------------------------------------------------------- #
# Attack 3 — forged event identity
# --------------------------------------------------------------------------- #
_EVENT_SECRET = "test-event-secret"


@pytest.fixture
def _patched_settings():
    """Patch fields on the live settings singleton and restore them.

    ``object.__setattr__`` is used so the patch also lands on a
    pre-fix Settings (which has no ``event_signing_secret`` field):
    the value is then simply never read, and the attack assertions
    fail behaviorally instead of erroring at setup.  (Rebuilding the
    singleton from env is avoided on purpose: other fixtures rebuild
    it during their teardown, while env patches are still active,
    which poisoned later tests with a stale prod singleton.)
    """
    settings = get_settings()
    saved: list = []

    def _apply(**fields) -> None:
        for name, value in fields.items():
            saved.append((name, getattr(settings, name, None)))
            object.__setattr__(settings, name, value)

    yield _apply
    for name, old in reversed(saved):
        object.__setattr__(settings, name, old)


def _sign(payload, *, tenant_id, user_id, role, secret) -> str:
    canonical = {"payload": payload, "role": role, "tenant_id": tenant_id, "user_id": user_id}
    data = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()


def _record_run_task(monkeypatch, calls: list):
    async def _fake_run_task(task, ctx, **kwargs):  # noqa: ANN001, ANN202
        calls.append((task, ctx))
        return SimpleNamespace(run_id="run-ev", thread_id="thread-ev")

    monkeypatch.setattr(orch, "run_task", _fake_run_task)


async def test_unsigned_event_rejected_when_secret_configured(monkeypatch, _patched_settings):
    _patched_settings(event_signing_secret=SecretStr(_EVENT_SECRET))
    calls: list = []
    _record_run_task(monkeypatch, calls)
    dispatcher = EventDispatcher(graphs={})
    trigger = EventDispatcher.parse_event(
        {
            "task": {"intent": "窃取数据"},
            "user_id": "mallory",
            "role": "admin",
            "tenant_id": "victim",
        }
    )
    assert getattr(trigger, "signature_valid", False) is False
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert "signature" in (result.error or "")
    assert calls == []  # no run was created under the forged identity


async def test_invalid_signature_event_rejected(monkeypatch, _patched_settings):
    _patched_settings(event_signing_secret=SecretStr(_EVENT_SECRET))
    calls: list = []
    _record_run_task(monkeypatch, calls)
    dispatcher = EventDispatcher(graphs={})
    trigger = EventDispatcher.parse_event(
        {
            "task": {"intent": "窃取数据"},
            "user_id": "mallory",
            "role": "admin",
            "tenant_id": "victim",
            "signature": "0" * 64,
        }
    )
    assert getattr(trigger, "signature_valid", False) is False
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert calls == []


async def test_signed_event_dispatches_with_server_resolved_role(monkeypatch, _patched_settings):
    _patched_settings(event_signing_secret=SecretStr(_EVENT_SECRET))
    calls: list = []
    _record_run_task(monkeypatch, calls)
    dispatcher = EventDispatcher(graphs={})
    payload = {"intent": "分析数据", "context": {"k": "v"}}
    signature = _sign(
        payload, tenant_id="acme", user_id="alice", role="admin", secret=_EVENT_SECRET
    )
    trigger = EventDispatcher.parse_event(
        {
            "task": payload,
            "user_id": "alice",
            "role": "admin",  # declared — must NOT be honored as the run role
            "tenant_id": "acme",
            "signature": signature,
        }
    )
    assert getattr(trigger, "signature_valid", False) is True
    result = await dispatcher.dispatch(trigger)
    assert result.ok is True
    assert result.workflow_id == "run-ev"
    task, ctx = calls[0]
    assert task.intent == "分析数据"
    assert ctx.tenant_id == "acme"
    assert ctx.user_id == "alice"
    # Role resolved server-side to the fixed least-privilege service
    # role — the envelope's "admin" claim is not honored.
    assert ctx.role == "service"


async def test_unsigned_event_rejected_outside_dev_mode(monkeypatch, _patched_settings):
    _patched_settings(event_signing_secret=SecretStr(""), app_env="prod")
    calls: list = []
    _record_run_task(monkeypatch, calls)
    dispatcher = EventDispatcher(graphs={})
    trigger = EventDispatcher.parse_event(
        {
            "task": {"intent": "窃取数据"},
            "user_id": "mallory",
            "role": "admin",
            "tenant_id": "victim",
        }
    )
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert "EVENT_SIGNING_SECRET" in (result.error or "")
    assert calls == []


# --------------------------------------------------------------------------- #
# Attack 3b — tenant-less runs still lease (system tenant), no bypass
# --------------------------------------------------------------------------- #
async def test_tenant_less_runs_lease_under_system_tenant_and_exclude(
    monkeypatch, force_memory_backend
):
    from forgeflow.runtime import run_lease as run_lease_mod
    from forgeflow.runtime.run_lease import RunLeaseHeldError

    store = InMemoryRunLeaseStore()
    set_run_lease_store(store)

    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list = []

    async def _fake_run_task(task, ctx, **kwargs):  # noqa: ANN001, ANN202
        calls.append(task)
        if len(calls) == 1:
            # Only the first execution blocks (holding its lease);
            # a second one — if the lease wrongly lets it through —
            # returns at once so the raises-assertion below fails
            # fast instead of deadlocking on the gate.
            entered.set()
            await release.wait()
        return "completed"

    async def _fake_prepare(task, ctx):  # noqa: ANN001, ANN202
        return None

    # Same stubbing pattern as the budget unit tests: input scoping is
    # not what is under test, and it requires a real tenant context.
    monkeypatch.setattr("forgeflow.runtime.input_scope.prepare_input_scope", _fake_prepare)
    monkeypatch.setattr(orch, "_run_task", _fake_run_task)

    first = asyncio.create_task(
        run_task(
            TaskCreate(intent="系统任务一"),
            SimpleNamespace(),  # bare namespace: no tenant at all
            run_id="run-system",
            lease_owner="worker-a",
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=10)

    # The run IS leased — under the fixed system tenant — so a second
    # tenant-less execution of the same run is refused, exactly like a
    # tenant-scoped one.  (Before the fix, tenant-less runs skipped
    # the lease and both proceeded.)
    with pytest.raises(RunLeaseHeldError) as excinfo:
        await run_task(
            TaskCreate(intent="系统任务二"),
            SimpleNamespace(),
            run_id="run-system",
            lease_owner="worker-b",
        )
    err = excinfo.value
    assert err.tenant_id == run_lease_mod.SYSTEM_LEASE_TENANT
    holder = store.peek(err.tenant_id, "run-system")
    assert holder is not None and holder.owner == "worker-a"

    release.set()
    assert await asyncio.wait_for(first, timeout=10) == "completed"
