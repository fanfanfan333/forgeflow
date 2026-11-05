"""PR #6 review round 2 — regression tests for the six follow-up items.

Each test pins one round-2 finding against the round-1 head
(``0c78f97``) and fails there:

1. Commit-time fencing must be TRULY atomic: (a) with the in-memory
   stores, a lease takeover cannot interleave between the fenced
   validation and the write (they share the lease store's lock);
   (b) a refused/failed commit leaves no orphaned blob behind.
   (The Postgres half — ``SELECT ... FOR UPDATE`` in the commit
   transaction — cannot execute in this VM and is pinned
   structurally.)
2. Read-only classification is a whitelist: ``research.verify`` and
   ``research.read`` WRITE (they retain sources / write report
   blobs), so they are write-class — fenced, never auto-retried as
   read-only; unknown tool ids default to write-class; genuinely
   read-only ids stay read-only.
3. Event signatures carry replay protection: the signed content
   includes ``event_id`` + ``expires_at``; a duplicate delivery is
   rejected from the persistent processed-event registry, an expired
   event is rejected, and an id tampered after signing is rejected.
4. Reserved tenants: an external event declaring
   ``__forgeflow_system__`` is rejected signed or not, in any mode;
   an unsigned dev event claiming an admin role is rejected; an
   ordinary unsigned dev event still proceeds.
5. Secure defaults: single-process dev mode requires the explicit
   ``FORGEFLOW_SINGLE_PROCESS_DEV=1`` opt-in (``APP_ENV=dev`` no
   longer implies it) and is refused under a multi-worker
   configuration.
6. The round-checkpoint save validates INSIDE the save: a takeover
   pinned into the check→save window can no longer let a stale
   owner's round checkpoint persist.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import forgeflow.runtime.orchestrator as orch
import forgeflow.runtime.tool_executor as tool_executor_mod
from forgeflow.config import get_settings
from forgeflow.documents import DocArtifactStore
from forgeflow.documents.review_store import (
    InMemoryArtifactReviewStore,
    reset_artifact_review_store,
    set_artifact_review_store,
)
from forgeflow.events.dispatcher import EventDispatcher, sign_task_event
from forgeflow.events.processed_events import (
    InMemoryProcessedEventStore,
    reset_processed_event_store,
    set_processed_event_store,
)
from forgeflow.runtime import tool_registry
from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.orchestrator import TaskCreate
from forgeflow.runtime.react_executor import _save_round_checkpoint
from forgeflow.runtime.run_lease import (
    InMemoryRunLeaseStore,
    LeaseFencedError,
    RunLeaseSession,
    current_run_lease,
    get_run_lease_store,
    reset_run_lease_store,
    set_run_lease_store,
)
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor, _is_read_only_tool
from forgeflow.runtime.tool_registry import ToolBinding
from tests.unit.test_run_lease_e2e import _FakeClock

_TENANT = "t-1"


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()


@pytest.fixture(autouse=True)
def _clean_stores():
    reset_processed_event_store()
    yield
    reset_checkpoint_store()
    reset_run_lease_store()
    reset_artifact_review_store()
    reset_processed_event_store()


@pytest.fixture
def _patched_settings():
    """Patch fields on the live settings singleton and restore them
    (same pattern as the round-1 attacks file)."""
    settings = get_settings()
    saved: list = []

    def _apply(**fields) -> None:
        for name, value in fields.items():
            saved.append((name, getattr(settings, name, None)))
            object.__setattr__(settings, name, value)

    yield _apply
    for name, old in reversed(saved):
        object.__setattr__(settings, name, old)


def _tool_ctx(run_id: str, owner: str, token: int, tool_args=None) -> ToolCallContext:
    return ToolCallContext(
        run_id=run_id,
        step_id=f"{run_id}:0:0",
        tenant_id=_TENANT,
        user_id="u-1",
        role="admin",
        intent="test",
        attempt=0,
        args=tool_args or {"table": "t"},
        idempotency_key=f"{run_id}:1:w1",
        idempotency_persisted=True,
        lease_owner=owner,
        lease_fencing_token=token,
    )


# --------------------------------------------------------------------------- #
# Item 1a — the fenced commit and the takeover share one lock
# --------------------------------------------------------------------------- #
def test_lease_fenced_commit_section_blocks_takeover():
    """The coupling itself: while a fenced commit section is held, a
    takeover (acquire after expiry) cannot proceed — it contends on
    the same lock.  On the round-1 code the store exposes no such
    section, so this fails at the API surface."""
    clock = _FakeClock()
    store = InMemoryRunLeaseStore(now=clock)
    lease_a = store.acquire(_TENANT, "run-lock", "worker-a", 30.0)
    assert lease_a is not None
    clock.advance(31.0)  # lease A is now expired; a takeover may proceed

    acquired: list = []
    takeover_attempted = threading.Event()

    def _take_over() -> None:
        takeover_attempted.set()
        acquired.append(store.acquire(_TENANT, "run-lock", "worker-b", 30.0))

    with store.fenced_commit():
        thread = threading.Thread(target=_take_over)
        thread.start()
        assert takeover_attempted.wait(timeout=10)
        thread.join(timeout=2)
        # The takeover is BLOCKED on the commit section's lock — it
        # cannot slip between a validation and its write.
        assert thread.is_alive()
        assert acquired == []
    thread.join(timeout=10)
    assert acquired and acquired[0] is not None and acquired[0].fencing_token == 2


class _GatedReviewStore(InMemoryArtifactReviewStore):
    """Pauses inside the version write so a test can pin the exact
    validate→write window open and attempt a takeover inside it."""

    def __init__(self, *args, commit_entered: threading.Event, allow_write: threading.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self._commit_entered = commit_entered
        self._allow_write = allow_write

    def _save_version(self, row):  # noqa: ANN001, ANN202
        self._commit_entered.set()
        self._allow_write.wait(timeout=30)
        super()._save_version(row)


def test_inmemory_commit_takeover_cannot_interleave(tmp_path):
    clock = _FakeClock()
    lease_store = InMemoryRunLeaseStore(now=clock)
    set_run_lease_store(lease_store)
    commit_entered = threading.Event()
    allow_write = threading.Event()
    review_store = _GatedReviewStore(
        blob_store=DocArtifactStore(root=tmp_path),
        commit_entered=commit_entered,
        allow_write=allow_write,
    )
    set_artifact_review_store(review_store)

    lease_a = lease_store.acquire(_TENANT, "run-race", "worker-a", 30.0)
    assert lease_a is not None and lease_a.fencing_token == 1
    session = RunLeaseSession(store=lease_store, lease=lease_a, ttl_seconds=30.0)

    commit_outcome: dict = {}

    def _commit() -> None:
        token = current_run_lease.set(session)
        try:
            version, _review = review_store.create_pending_version(
                _TENANT,
                artifact_id="art-race",
                content=b"owner-a-bytes",
                fmt="text",
                run_id="run-race",
            )
            commit_outcome["version"] = version
        except Exception as exc:  # noqa: BLE001 — asserted below
            commit_outcome["error"] = exc
        finally:
            current_run_lease.reset(token)

    commit_thread = threading.Thread(target=_commit)
    commit_thread.start()
    # The commit is now pinned INSIDE the write, after validation.
    assert commit_entered.wait(timeout=10)

    # The lease expires and a peer worker attempts the takeover NOW —
    # inside the validate→write window.
    clock.advance(31.0)
    takeover: dict = {}
    takeover_attempted = threading.Event()

    def _take_over() -> None:
        takeover_attempted.set()
        takeover["lease"] = lease_store.acquire(_TENANT, "run-race", "worker-b", 30.0)

    takeover_thread = threading.Thread(target=_take_over)
    takeover_thread.start()
    assert takeover_attempted.wait(timeout=10)
    takeover_thread.join(timeout=2)
    # Post-fix: the takeover CANNOT complete while the fenced commit
    # is in flight — it blocks on the lease store's lock until the
    # commit finishes.  (Pre-fix it completed immediately, and the
    # stale owner's write then landed on top of it.)
    assert takeover_thread.is_alive(), "takeover interleaved with the fenced commit"
    assert "lease" not in takeover

    allow_write.set()
    commit_thread.join(timeout=10)
    takeover_thread.join(timeout=10)

    # The commit was fenced-valid for its whole section, so it landed
    # legitimately as worker-a's version — BEFORE the takeover.
    assert "error" not in commit_outcome, commit_outcome
    assert takeover["lease"] is not None and takeover["lease"].fencing_token == 2
    versions = review_store.list_versions(_TENANT, "art-race")
    assert len(versions) == 1

    # And the now-stale owner's NEXT commit is refused outright.
    token = current_run_lease.set(session)
    try:
        with pytest.raises(LeaseFencedError):
            review_store.create_pending_version(
                _TENANT,
                artifact_id="art-race",
                content=b"stale-bytes",
                fmt="text",
                run_id="run-race",
            )
    finally:
        current_run_lease.reset(token)
    assert len(review_store.list_versions(_TENANT, "art-race")) == 1


# --------------------------------------------------------------------------- #
# Item 1b — no orphaned blobs from refused/failed commits
# --------------------------------------------------------------------------- #
def _blob_files(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_refused_commit_leaves_no_blob_behind(tmp_path):
    clock = _FakeClock()
    lease_store = InMemoryRunLeaseStore(now=clock)
    set_run_lease_store(lease_store)
    blob_store = DocArtifactStore(root=tmp_path)
    review_store = InMemoryArtifactReviewStore(blob_store=blob_store)
    set_artifact_review_store(review_store)

    lease_a = lease_store.acquire(_TENANT, "run-blob", "worker-a", 30.0)
    assert lease_a is not None
    session = RunLeaseSession(store=lease_store, lease=lease_a, ttl_seconds=30.0)
    # Takeover BEFORE the commit attempt: the commit is refused.
    clock.advance(31.0)
    lease_b = lease_store.acquire(_TENANT, "run-blob", "worker-b", 30.0)
    assert lease_b is not None and lease_b.fencing_token == 2

    token = current_run_lease.set(session)
    try:
        with pytest.raises(LeaseFencedError):
            review_store.create_pending_version(
                _TENANT,
                artifact_id="art-blob",
                content=b"refused-bytes",
                fmt="text",
                run_id="run-blob",
            )
    finally:
        current_run_lease.reset(token)
    assert review_store.list_versions(_TENANT, "art-blob") == []
    assert _blob_files(tmp_path) == []


def test_failed_commit_removes_its_created_blob(tmp_path):
    """The commit fails AFTER its blob was written (the Postgres
    shape: the fence inside the save transaction is what fires after
    the blob put).  The blob this call created must be removed again,
    not orphaned."""
    blob_store = DocArtifactStore(root=tmp_path)

    class _FailingCommitStore(InMemoryArtifactReviewStore):
        def _save_version_and_review(self, version_row, review_row):  # noqa: ANN001, ANN202
            raise RuntimeError("commit failed after blob write")

    review_store = _FailingCommitStore(blob_store=blob_store)
    set_artifact_review_store(review_store)
    ref = blob_store.ref_for(b"doomed-bytes", suffix=".docx")
    with pytest.raises(RuntimeError, match="commit failed"):
        review_store.create_pending_version(
            _TENANT,
            artifact_id="art-doomed",
            content=b"doomed-bytes",
            fmt="docx",
            run_id=None,
        )
    assert not blob_store.exists(ref)
    assert _blob_files(tmp_path) == []


def test_partial_commit_failure_leaves_neither_version_nor_blob(tmp_path):
    """The second half of the commit fails after the first half
    landed: no half-committed version row and no orphaned blob may
    remain (the in-memory compensation; Postgres gets the same
    guarantee from its single transaction)."""
    blob_store = DocArtifactStore(root=tmp_path)

    class _ReviewSaveFailsStore(InMemoryArtifactReviewStore):
        def _save_review(self, row):  # noqa: ANN001, ANN202
            raise RuntimeError("review insert failed")

    review_store = _ReviewSaveFailsStore(blob_store=blob_store)
    set_artifact_review_store(review_store)
    with pytest.raises(RuntimeError, match="review insert failed"):
        review_store.create_pending_version(
            _TENANT,
            artifact_id="art-partial",
            content=b"partial-bytes",
            fmt="docx",
            run_id=None,
        )
    assert review_store.list_versions(_TENANT, "art-partial") == []
    assert _blob_files(tmp_path) == []


def test_pg_fence_paths_lock_the_lease_row():
    """Structural pin for the Postgres half (not executable in this
    VM): both fenced PG commit paths lock the lease row FOR UPDATE
    inside the write's transaction, and the review store commits
    version + review in ONE transaction."""
    root = pathlib.Path(__file__).resolve().parents[2]
    review_src = (root / "forgeflow" / "documents" / "review_store.py").read_text("utf-8")
    checkpoint_src = (root / "forgeflow" / "runtime" / "checkpoints.py").read_text("utf-8")
    assert "FOR UPDATE" in review_src
    assert "_save_version_and_review" in review_src
    assert "FOR UPDATE" in checkpoint_src


# --------------------------------------------------------------------------- #
# Item 2 — read-only classification is an exact-id whitelist
# --------------------------------------------------------------------------- #
def test_research_writers_are_write_class_and_unknown_defaults_write():
    assert _is_read_only_tool("research.verify") is False
    assert _is_read_only_tool("research.read") is False
    # Unknown / future ids default to write-class.
    assert _is_read_only_tool("research.some_future_tool") is False
    assert _is_read_only_tool("totally.unknown") is False
    assert _is_read_only_tool("mcp.demo.write") is False
    # Genuinely read-only, individually verified ids stay read-only.
    assert _is_read_only_tool("research.search") is True
    assert _is_read_only_tool("evidence.read") is True
    assert _is_read_only_tool("code.lint") is True
    assert _is_read_only_tool("textfile.inspect") is True
    assert _is_read_only_tool("analysis.profile") is True


async def test_research_verify_is_lease_fenced_as_write_class(force_memory_backend):
    lease_store = InMemoryRunLeaseStore()
    set_run_lease_store(lease_store)
    lease = lease_store.acquire(_TENANT, "run-fence", "worker-a", 60.0)
    assert lease is not None and lease.fencing_token == 1
    calls: list = []

    async def _verify_handler(args, ctx):  # noqa: ANN001, ANN202
        calls.append(dict(args))
        return {"ok": True, "verified": True}

    tool_registry.register(
        ToolBinding(
            tool_id="research.verify",
            handler=_verify_handler,
            kind="real",
            provider="test",
            description="verify probe",
        )
    )
    # Stamped with a STALE fencing token (99 != current 1): a
    # write-class call is refused by the lease guard before the
    # handler runs.  (While research.* was prefix-classified
    # read-only, the guard skipped it entirely.)
    invocation = await ToolExecutor().execute(
        "research.verify", ctx=_tool_ctx("run-fence", "worker-a", 99)
    )
    assert calls == []
    assert invocation.status == "blocked"
    assert invocation.provider == "lease_guard"


async def test_research_read_not_auto_retried_but_whitelisted_read_is(monkeypatch):
    async def _no_sleep(_seconds):  # noqa: ANN001
        return None

    monkeypatch.setattr(tool_executor_mod, "_sleep", _no_sleep)
    attempts = {"research.read": 0, "evidence.read": 0}

    async def _flaky(tool):  # noqa: ANN001, ANN202
        async def _handler(args, ctx):  # noqa: ANN001, ANN202
            attempts[tool] += 1
            if attempts[tool] == 1:
                raise RuntimeError("503 connection reset by peer")
            return {"ok": True, "retried": True}

        return _handler

    for tool in ("research.read", "evidence.read"):
        tool_registry.register(
            ToolBinding(
                tool_id=tool,
                handler=await _flaky(tool),
                kind="real",
                provider="test",
                description="flaky probe",
            )
        )
    def _unstamped_ctx(run_id: str) -> ToolCallContext:
        # No lease stamps: these runs are not leased, so the lease
        # guard is out of the picture and only the classification /
        # retry behavior is under test.
        return ToolCallContext(
            run_id=run_id,
            step_id=f"{run_id}:0:0",
            tenant_id=_TENANT,
            user_id="u-1",
            role="admin",
            intent="test",
            attempt=0,
            args={"table": "t"},
            idempotency_key=f"{run_id}:1:w1",
            idempotency_persisted=True,
            lease_owner=None,
            lease_fencing_token=None,
        )

    # research.read is write-class: its handler exception is
    # outcome_unknown (the write may have landed) and it is NEVER
    # auto-retried as if it were read-only.
    inv_write = await ToolExecutor().execute("research.read", ctx=_unstamped_ctx("run-r"))
    assert attempts["research.read"] == 1
    assert inv_write.status == "outcome_unknown"
    # A whitelisted read-only tool gets the transient retry.
    inv_read = await ToolExecutor().execute("evidence.read", ctx=_unstamped_ctx("run-e"))
    assert attempts["evidence.read"] == 2
    assert inv_read.status == "ok"


# --------------------------------------------------------------------------- #
# Item 3 — signature replay protection
# --------------------------------------------------------------------------- #
_EVENT_SECRET = "round2-event-secret"


def _record_run_task(monkeypatch, calls: list):
    async def _fake_run_task(task, ctx, **kwargs):  # noqa: ANN001, ANN202
        calls.append((task, ctx))
        return SimpleNamespace(run_id="run-ev2", thread_id="thread-ev2")

    monkeypatch.setattr(orch, "run_task", _fake_run_task)


def _signed_envelope(*, event_id: str, expires_at: str, tenant: str = "acme", role: str = "admin"):
    payload = {"intent": "分析数据"}
    signature = sign_task_event(
        payload,
        tenant_id=tenant,
        user_id="alice",
        role=role,
        secret=_EVENT_SECRET,
        event_id=event_id,
        expires_at=expires_at,
    )
    return {
        "task": payload,
        "user_id": "alice",
        "role": role,
        "tenant_id": tenant,
        "event_id": event_id,
        "expires_at": expires_at,
        "signature": signature,
    }


def _future_expiry(minutes: int = 10) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def _event_env(monkeypatch, _patched_settings):
    _patched_settings(event_signing_secret=SecretStr(_EVENT_SECRET))
    set_processed_event_store(InMemoryProcessedEventStore())
    calls: list = []
    _record_run_task(monkeypatch, calls)
    return calls


async def test_signed_event_replay_is_rejected_as_duplicate(_event_env):
    calls = _event_env
    dispatcher = EventDispatcher(graphs={})
    envelope = _signed_envelope(event_id="evt-replay-1", expires_at=_future_expiry())

    first = await dispatcher.dispatch(EventDispatcher.parse_event(envelope))
    assert first.ok is True
    assert len(calls) == 1

    # The identical, still-validly-signed envelope delivered again:
    # rejected as a duplicate BEFORE any dispatch work.
    second = await dispatcher.dispatch(EventDispatcher.parse_event(envelope))
    assert second.ok is False
    assert "duplicate" in (second.error or "")
    assert len(calls) == 1


async def test_expired_signed_event_is_rejected(_event_env):
    calls = _event_env
    dispatcher = EventDispatcher(graphs={})
    envelope = _signed_envelope(
        event_id="evt-expired-1",
        expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    trigger = EventDispatcher.parse_event(envelope)
    assert trigger.signature_valid is False
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert calls == []


async def test_event_id_tampered_after_signing_is_rejected(_event_env):
    calls = _event_env
    dispatcher = EventDispatcher(graphs={})
    envelope = _signed_envelope(event_id="evt-signed-id", expires_at=_future_expiry())
    envelope["event_id"] = "evt-attacker-id"  # re-labelled after signing
    trigger = EventDispatcher.parse_event(envelope)
    assert trigger.signature_valid is False
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert calls == []


# --------------------------------------------------------------------------- #
# Item 4 — reserved tenants + dev-mode privilege claims
# --------------------------------------------------------------------------- #
async def test_signed_event_declaring_system_tenant_is_rejected(_event_env):
    calls = _event_env
    dispatcher = EventDispatcher(graphs={})
    envelope = _signed_envelope(
        event_id="evt-reserved-1",
        expires_at=_future_expiry(),
        tenant="__forgeflow_system__",
    )
    trigger = EventDispatcher.parse_event(envelope)
    assert trigger.signature_valid is True  # properly signed — still refused
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert "reserved" in (result.error or "")
    assert calls == []


async def test_unsigned_dev_event_declaring_system_tenant_is_rejected(
    monkeypatch, _patched_settings
):
    _patched_settings(event_signing_secret=SecretStr(""))
    set_processed_event_store(InMemoryProcessedEventStore())
    calls: list = []
    _record_run_task(monkeypatch, calls)
    dispatcher = EventDispatcher(graphs={})
    trigger = EventDispatcher.parse_event(
        {
            "task": {"intent": "冒充系统任务"},
            "user_id": "mallory",
            "tenant_id": "__forgeflow_system__",
        }
    )
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert "reserved" in (result.error or "")
    assert calls == []


async def test_unsigned_dev_event_claiming_admin_role_is_rejected(monkeypatch, _patched_settings):
    _patched_settings(event_signing_secret=SecretStr(""))
    set_processed_event_store(InMemoryProcessedEventStore())
    calls: list = []
    _record_run_task(monkeypatch, calls)
    dispatcher = EventDispatcher(graphs={})
    trigger = EventDispatcher.parse_event(
        {
            "task": {"intent": "提权"},
            "user_id": "mallory",
            "role": "admin",
            "tenant_id": "acme",
        }
    )
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert "privileged role" in (result.error or "")
    assert calls == []


async def test_unsigned_dev_event_with_ordinary_identity_proceeds(monkeypatch, _patched_settings):
    _patched_settings(event_signing_secret=SecretStr(""))
    set_processed_event_store(InMemoryProcessedEventStore())
    calls: list = []
    _record_run_task(monkeypatch, calls)
    dispatcher = EventDispatcher(graphs={})
    trigger = EventDispatcher.parse_event(
        {
            "task": {"intent": "普通本地事件"},
            "user_id": "dev-user",
            "role": "manager",
            "tenant_id": "acme",
        },
        source_event_id="dev-msg-1",
    )
    result = await dispatcher.dispatch(trigger)
    assert result.ok is True
    assert len(calls) == 1
    _task, ctx = calls[0]
    assert ctx.tenant_id == "acme"
    assert ctx.role == "manager"


# --------------------------------------------------------------------------- #
# Item 5 — explicit opt-in, never inferred; multi-worker refused
# --------------------------------------------------------------------------- #
def test_dev_mode_requires_explicit_flag_not_app_env(monkeypatch):
    from forgeflow.runtime.run_lease import single_process_dev_mode

    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "dev")  # the old inference source
    monkeypatch.delenv("FORGEFLOW_SINGLE_PROCESS_DEV", raising=False)
    assert single_process_dev_mode() is False

    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    assert single_process_dev_mode() is True


def test_dev_mode_refused_for_multi_worker(monkeypatch):
    from forgeflow.runtime.run_lease import single_process_dev_mode

    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    assert single_process_dev_mode() is False
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    assert single_process_dev_mode() is True


def test_lease_resolution_fails_closed_with_app_env_dev_but_no_flag(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "dev")
    monkeypatch.setattr(settings, "storage_backend", "memory")
    monkeypatch.delenv("FORGEFLOW_SINGLE_PROCESS_DEV", raising=False)
    reset_run_lease_store()
    try:
        with pytest.raises(RuntimeError, match="Postgres backend"):
            get_run_lease_store()
    finally:
        reset_run_lease_store()


def test_lease_resolution_flag_allows_inmemory_even_when_app_env_prod(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "storage_backend", "memory")
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    reset_run_lease_store()
    try:
        store = get_run_lease_store()
        assert isinstance(store, InMemoryRunLeaseStore)
    finally:
        reset_run_lease_store()


def test_lease_resolution_flag_with_multi_worker_fails_closed(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "storage_backend", "memory")
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    monkeypatch.setenv("WEB_CONCURRENCY", "3")
    reset_run_lease_store()
    try:
        with pytest.raises(RuntimeError, match="Postgres backend"):
            get_run_lease_store()
    finally:
        reset_run_lease_store()


# --------------------------------------------------------------------------- #
# Item 6 — the round-checkpoint save validates inside the save
# --------------------------------------------------------------------------- #
class _GatedCheckpointStore(InMemoryCheckpointStore):
    """Pauses at save() entry so a test can pin the check→save window
    open and land a takeover inside it."""

    def __init__(self, save_entered: threading.Event, allow_save: threading.Event) -> None:
        super().__init__()
        self._save_entered = save_entered
        self._allow_save = allow_save

    def save(self, tenant, **kwargs):  # noqa: ANN001, ANN202
        self._save_entered.set()
        self._allow_save.wait(timeout=30)
        return super().save(tenant, **kwargs)


def test_round_save_takeover_in_check_save_window_is_refused():
    clock = _FakeClock()
    lease_store = InMemoryRunLeaseStore(now=clock)
    set_run_lease_store(lease_store)
    save_entered = threading.Event()
    allow_save = threading.Event()
    checkpoint_store = _GatedCheckpointStore(save_entered, allow_save)
    set_checkpoint_store(checkpoint_store)

    lease_a = lease_store.acquire(_TENANT, "run-round-race", "worker-a", 30.0)
    assert lease_a is not None and lease_a.fencing_token == 1
    session = RunLeaseSession(store=lease_store, lease=lease_a, ttl_seconds=30.0)

    task = TaskCreate(intent="回合检查点竞态", workflow_type="generic", context={})
    ctx = SimpleNamespace(tenant_id=_TENANT, user_id="u-1", role="admin")
    outcome: dict = {}

    def _do_save() -> None:
        token = current_run_lease.set(session)
        try:
            outcome["saved"] = _save_round_checkpoint(
                task, ctx, "run-round-race", [], {}, 1, [], None
            )
        finally:
            current_run_lease.reset(token)

    save_thread = threading.Thread(target=_do_save)
    save_thread.start()
    # The save is pinned at the store boundary — exactly where the
    # round-1 loop-seam check had already passed and the old code
    # would now persist unconditionally.
    assert save_entered.wait(timeout=10)

    # Takeover lands inside the check→save window.
    clock.advance(31.0)
    lease_b = lease_store.acquire(_TENANT, "run-round-race", "worker-b", 30.0)
    assert lease_b is not None and lease_b.fencing_token == 2

    allow_save.set()
    save_thread.join(timeout=10)

    # Post-fix: the fenced save validates inside the store and is
    # refused — nothing persisted, the write reports failure, and the
    # session is marked lost so the loop terminates as lease_lost.
    assert outcome["saved"] is False
    assert checkpoint_store.latest(_TENANT, "run-round-race") is None
    assert session.lost is True


# --------------------------------------------------------------------------- #
# Item 3 (structural) — migration 045
# --------------------------------------------------------------------------- #
def test_migration_045_processed_event_ids():
    migrations = pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions"
    path = migrations / "045_processed_event_ids.py"
    spec = importlib.util.spec_from_file_location("mig_045_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "045"
    assert module.down_revision == "044"
    source = path.read_text(encoding="utf-8")
    assert "processed_event_ids" in source
    assert "event_id" in source and "PRIMARY KEY" in source
