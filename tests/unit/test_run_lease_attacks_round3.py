"""PR #6 review round 3 — regression tests for the three follow-ups.

Each test pins one round-3 finding against the round-2 head
(``f022374``) and fails there:

1. Event expiry was checked only at parse time: a parsed envelope
   could sit in a queue past its signed ``expires_at`` and still
   dispatch on the cached ``signature_valid``, and the replay claim
   never re-checked the time either.  Freshness must be re-validated
   at dispatch AND at claim, against the current time.
2. Single-process "confirmation" trusted configuration claims
   (``WEB_CONCURRENCY`` unset ⇒ 1), so ``uvicorn --workers 2`` still
   handed every worker a private in-memory lease store.  The
   in-memory backend now additionally requires holding an OS-level
   exclusive lock: a second live process fails closed.
3. The orchestrator's approval-checkpoint save took no fence, so a
   lease-lost (stale) owner could still persist an approval boundary
   for the new owner to resume from.  It is fenced like the round
   saves now.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

import forgeflow.events.dispatcher as dispatcher_mod
import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.events.dispatcher import EventDispatcher, sign_task_event
from forgeflow.events.processed_events import (
    InMemoryProcessedEventStore,
    reset_processed_event_store,
    set_processed_event_store,
)
from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    _save_approval_checkpoint,
)
from forgeflow.runtime.run_lease import (
    InMemoryRunLeaseStore,
    RunLeaseSession,
    current_run_lease,
    reset_run_lease_store,
    set_run_lease_store,
)

_TENANT = "t-1"


@pytest.fixture(autouse=True)
def _clean_stores():
    yield
    reset_checkpoint_store()
    reset_run_lease_store()
    reset_processed_event_store()


@pytest.fixture
def _patched_settings():
    """Patch fields on the live settings singleton and restore them
    (same pattern as the round-1/round-2 attack suites)."""
    settings = get_settings()
    saved: list = []

    def _apply(**fields) -> None:
        for name, value in fields.items():
            saved.append((name, getattr(settings, name, None)))
            object.__setattr__(settings, name, value)

    yield _apply
    for name, old in reversed(saved):
        object.__setattr__(settings, name, old)


class _FakeClock:
    """Deterministic clock: a mutable 'now' the test advances by hand."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _record_run_task(monkeypatch, calls: list):
    async def _fake_run_task(task, ctx, **kwargs):  # noqa: ANN001, ANN202
        calls.append((task, ctx))
        return SimpleNamespace(run_id="run-ev3", thread_id="thread-ev3")

    monkeypatch.setattr(orch, "run_task", _fake_run_task)


# --------------------------------------------------------------------------- #
# Item 1 — expiry re-validated at dispatch and at claim
# --------------------------------------------------------------------------- #
_EVENT_SECRET = "round3-event-secret"


def _signed_envelope(*, event_id: str, expires_at: str, tenant: str = "acme"):
    payload = {"intent": "分析数据"}
    signature = sign_task_event(
        payload,
        tenant_id=tenant,
        user_id="alice",
        role="admin",
        secret=_EVENT_SECRET,
        event_id=event_id,
        expires_at=expires_at,
    )
    return {
        "task": payload,
        "user_id": "alice",
        "role": "admin",
        "tenant_id": tenant,
        "event_id": event_id,
        "expires_at": expires_at,
        "signature": signature,
    }


@pytest.fixture
def _event_env(monkeypatch, _patched_settings):
    _patched_settings(event_signing_secret=SecretStr(_EVENT_SECRET))
    set_processed_event_store(InMemoryProcessedEventStore())
    calls: list = []
    _record_run_task(monkeypatch, calls)
    return calls


async def test_event_expired_after_parse_is_rejected_at_dispatch(monkeypatch, _event_env):
    """Parse while valid; the envelope then sits in the 'queue' past
    its signed expiry; dispatch must reject it instead of trusting
    the cached parse-time verdict."""
    calls = _event_env
    base = datetime.now(UTC)
    clock = _FakeClock(base)
    # raising=False: on the pre-fix head the dispatcher has no clock
    # seam to patch — the patch is then inert, parse/verify run on
    # real time (the envelope is genuinely unexpired), and the test
    # fails behaviorally at the dispatch assertion below.
    monkeypatch.setattr(dispatcher_mod, "_utcnow", lambda: clock.now, raising=False)

    envelope = _signed_envelope(
        event_id="evt-queue-stale",
        expires_at=(base + timedelta(minutes=5)).isoformat(),
    )
    trigger = EventDispatcher.parse_event(envelope)
    assert trigger.signature_valid is True  # valid when parsed

    # The envelope waits in the queue beyond its signed lifetime.
    clock.advance(timedelta(minutes=10))

    result = await EventDispatcher(graphs={}).dispatch(trigger)
    assert result.ok is False
    assert "expired" in (result.error or "")
    assert calls == []  # no run was created from the stale envelope


async def test_event_expiring_between_gate_and_claim_is_rejected_and_id_not_poisoned(
    monkeypatch, _event_env
):
    """Expiry lands in the gap between the dispatch trust gate and
    the replay claim: the claim must refuse too, must not run the
    task, and must NOT record the id — a later legitimate redelivery
    of the same id under a fresh signature/expiry still dispatches."""
    calls = _event_env
    store = InMemoryProcessedEventStore()
    set_processed_event_store(store)
    base = datetime.now(UTC)
    clock = _FakeClock(base)
    monkeypatch.setattr(dispatcher_mod, "_utcnow", lambda: clock.now, raising=False)

    envelope = _signed_envelope(
        event_id="evt-gate-claim-gap",
        expires_at=(base + timedelta(minutes=5)).isoformat(),
    )
    trigger = EventDispatcher.parse_event(envelope)
    assert trigger.signature_valid is True

    # Gate the claim boundary: time jumps past the expiry exactly
    # when dispatch reaches the claim (after the trust gate passed).
    # One-shot: the redelivery below passes through the same wrapper
    # without a second jump (its fresh expiry is far enough out).
    original_claim = EventDispatcher._claim_event_once
    gate = {"armed": True}

    def _gated_claim(trig, tenant_id):  # noqa: ANN001, ANN202
        if gate["armed"]:
            gate["armed"] = False
            clock.advance(timedelta(minutes=10))
        return original_claim(trig, tenant_id)

    monkeypatch.setattr(EventDispatcher, "_claim_event_once", staticmethod(_gated_claim))

    dispatcher = EventDispatcher(graphs={})
    result = await dispatcher.dispatch(trigger)
    assert result.ok is False
    assert "expired" in (result.error or "")
    assert calls == []  # run_task never called
    # The expired event was NOT recorded: its id must not be burned
    # (a recorded claim would live until the old expiry + grace and
    # misread a fresh redelivery as a duplicate).
    assert "evt-gate-claim-gap" not in store._claims

    # Legitimate redelivery: same event id, FRESH signature + expiry.
    fresh = _signed_envelope(
        event_id="evt-gate-claim-gap",
        expires_at=(base + timedelta(minutes=30)).isoformat(),
    )
    fresh_trigger = EventDispatcher.parse_event(fresh)
    assert fresh_trigger.signature_valid is True
    redelivered = await dispatcher.dispatch(fresh_trigger)
    assert redelivered.ok is True
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Item 2 — single-process must be positively confirmed (OS lock)
# --------------------------------------------------------------------------- #
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

_CHILD_SCRIPT = r"""
import os, sys, time

mode = sys.argv[1]
from forgeflow.runtime.run_lease import get_run_lease_store, reset_run_lease_store

reset_run_lease_store()
try:
    store = get_run_lease_store()
except Exception as exc:
    print(f"RESOLUTION-FAILED {type(exc).__name__}: {exc}", flush=True)
    sys.exit(3)
print(f"RESOLVED {type(store).__name__}", flush=True)
if mode == "hold":
    ready, stop = sys.argv[2], sys.argv[3]
    with open(ready, "w") as fh:
        fh.write("ready")
    deadline = time.time() + 120
    while time.time() < deadline and not os.path.exists(stop):
        time.sleep(0.05)
    print("HOLDER-EXIT", flush=True)
"""


def _child_env(data_dir: pathlib.Path, *, web_concurrency: str | None = None) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "WEB_CONCURRENCY",
            "POSTGRES_SYNC_URL",
            "POSTGRES_DSN",
            "FORGEFLOW_DATA_DIR",
        }
    }
    env["FORGEFLOW_SINGLE_PROCESS_DEV"] = "1"
    env["STORAGE_BACKEND"] = "memory"
    env["FORGEFLOW_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if web_concurrency is not None:
        env["WEB_CONCURRENCY"] = web_concurrency
    return env


def _run_child(script: pathlib.Path, args: list, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_second_live_process_cannot_resolve_inmemory_lease_store(tmp_path):
    """Process A holds the deployment's single-process lock; process
    B — a real subprocess, dev flag set, WEB_CONCURRENCY unset — must
    fail closed instead of minting its own private lease store.
    Controls: a lone process resolves fine, and WEB_CONCURRENCY=2 is
    refused even with nobody else running."""
    script = tmp_path / "lease_child.py"
    script.write_text(_CHILD_SCRIPT)
    data_dir = tmp_path / "data"
    ready = tmp_path / "ready"
    stop = tmp_path / "stop"
    env = _child_env(data_dir)

    holder = subprocess.Popen(
        [sys.executable, str(script), "hold", str(ready), str(stop)],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # Deterministic handshake: B only starts once A has resolved
        # (and therefore holds the lock).
        deadline = 120
        waited = 0.0
        while not ready.exists() and waited < deadline:
            assert holder.poll() is None, "holder exited before signalling readiness"
            time.sleep(0.05)
            waited += 0.05
        assert ready.exists(), "holder never signalled readiness"

        contender = _run_child(script, ["resolve"], env)
        assert contender.returncode == 3, contender.stdout
        assert "RESOLUTION-FAILED" in contender.stdout
        assert "RunLeaseConfigurationError" in contender.stdout
    finally:
        stop.write_text("stop")
        holder_out, _ = holder.communicate(timeout=120)
    assert holder.returncode == 0
    assert "RESOLVED InMemoryRunLeaseStore" in holder_out

    # Control: a single live process under the flag resolves fine
    # (the lock is free again once the holder exited).
    single = _run_child(script, ["resolve"], _child_env(tmp_path / "data-single"))
    assert single.returncode == 0, single.stdout
    assert "RESOLVED InMemoryRunLeaseStore" in single.stdout

    # Control: positive multi-worker evidence refuses even a lone
    # process, flag or not.
    multi = _run_child(
        script, ["resolve"], _child_env(tmp_path / "data-multi", web_concurrency="2")
    )
    assert multi.returncode == 3, multi.stdout
    assert "RunLeaseConfigurationError" in multi.stdout


def test_confirm_single_process_is_idempotent_per_process():
    """The pytest process itself can confirm repeatedly (the record
    is keyed by pid); this is what lets the whole suite resolve the
    in-memory store under the conftest-pinned flag."""
    from forgeflow.runtime.run_lease import confirm_single_process

    assert confirm_single_process() is True
    assert confirm_single_process() is True


# --------------------------------------------------------------------------- #
# Item 3 — approval-checkpoint save is fenced
# --------------------------------------------------------------------------- #
class _LeaseClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _approval_task() -> TaskCreate:
    return TaskCreate(
        intent="修改代码并等待审批",
        context={
            "tool_invocations": [
                {
                    "status": "awaiting_approval",
                    "tool": "code.execute",
                    "payload": {"pending_id": "pend-r3"},
                    "arguments_hash": "hash-r3",
                }
            ],
            "llm_runtime": {},
        },
    )


def _approval_setup():
    clock = _LeaseClock()
    checkpoint_store = InMemoryCheckpointStore()
    set_checkpoint_store(checkpoint_store)
    lease_store = InMemoryRunLeaseStore(now=clock)
    set_run_lease_store(lease_store)
    lease = lease_store.acquire(_TENANT, "run-approval-r3", "worker-a", 30.0)
    assert lease is not None and lease.fencing_token == 1
    session = RunLeaseSession(store=lease_store, lease=lease, ttl_seconds=30.0)
    return clock, checkpoint_store, lease_store, session


def test_approval_checkpoint_saved_by_current_owner():
    _clock, checkpoint_store, _lease_store, session = _approval_setup()
    token = current_run_lease.set(session)
    try:
        _save_approval_checkpoint(
            _approval_task(),
            RequestContext(tenant_id=_TENANT, user_id="u-1", role="admin"),
            "run-approval-r3",
            None,
        )
    finally:
        current_run_lease.reset(token)
    history = checkpoint_store.history(_TENANT, "run-approval-r3", limit=50)
    assert len(history) == 1
    assert history[0].kind == "approval"
    assert session.lost is False


def test_approval_checkpoint_refused_after_takeover():
    clock, checkpoint_store, lease_store, session = _approval_setup()
    token = current_run_lease.set(session)
    try:
        # Takeover lands before the (stale) owner persists its
        # approval boundary at the end of its run.
        clock.advance(31.0)
        taken = lease_store.acquire(_TENANT, "run-approval-r3", "worker-b", 30.0)
        assert taken is not None and taken.fencing_token == 2

        # The save path is best-effort: the refusal must not escape
        # as an exception into terminal bookkeeping...
        _save_approval_checkpoint(
            _approval_task(),
            RequestContext(tenant_id=_TENANT, user_id="u-1", role="admin"),
            "run-approval-r3",
            None,
        )
    finally:
        current_run_lease.reset(token)
    # ...but NOTHING may be persisted for the new owner to resume
    # from, and the session is marked lost.
    assert checkpoint_store.history(_TENANT, "run-approval-r3", limit=50) == []
    assert session.lost is True
