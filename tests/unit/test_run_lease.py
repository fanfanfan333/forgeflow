"""Run session leases — store semantics (unit) + migration 044 structure.

The InMemory store is driven by an injected fake clock so expiry and
takeover are deterministic.  Every test here fails on unmodified main:
the module ``forgeflow.runtime.run_lease`` and migration ``044`` do
not exist there.
"""

from __future__ import annotations

import importlib.util
import pathlib
import threading
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest

from forgeflow.runtime.run_lease import (
    InMemoryRunLeaseStore,
    PostgresRunLeaseStore,
    RunLease,
    RunLeaseHeldError,
    get_run_lease_store,
    reset_run_lease_store,
    resolve_lease_ttl_seconds,
    set_run_lease_store,
)

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


class _FakeClock:
    def __init__(self) -> None:
        self.now = _T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _store(clock: _FakeClock) -> InMemoryRunLeaseStore:
    return InMemoryRunLeaseStore(now=clock)


# --------------------------------------------------------------------------- #
# Acquire / race / takeover
# --------------------------------------------------------------------------- #
def test_second_acquire_while_held_returns_none():
    clock = _FakeClock()
    store = _store(clock)
    first = store.acquire("t", "run-1", "worker-a", 60.0)
    assert first is not None
    assert first.fencing_token == 1
    assert store.acquire("t", "run-1", "worker-b", 60.0) is None
    # A different run is unaffected.
    assert store.acquire("t", "run-2", "worker-b", 60.0) is not None
    # A different tenant is unaffected.
    assert store.acquire("t2", "run-1", "worker-b", 60.0) is not None


def test_concurrent_acquire_race_has_exactly_one_winner():
    clock = _FakeClock()
    store = _store(clock)
    barrier = threading.Barrier(8)
    results: list[RunLease | None] = []
    results_lock = threading.Lock()

    def _race(owner: str) -> None:
        barrier.wait(timeout=10)
        lease = store.acquire("t", "run-race", owner, 60.0)
        with results_lock:
            results.append(lease)

    threads = [threading.Thread(target=_race, args=(f"worker-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    winners = [lease for lease in results if lease is not None]
    assert len(results) == 8
    assert len(winners) == 1
    assert winners[0].fencing_token == 1


def test_acquire_after_expiry_succeeds_with_strictly_greater_token():
    clock = _FakeClock()
    store = _store(clock)
    first = store.acquire("t", "run-1", "worker-a", 30.0)
    assert first is not None
    clock.advance(29.0)
    assert store.acquire("t", "run-1", "worker-b", 30.0) is None  # still held
    clock.advance(2.0)  # past expiry
    second = store.acquire("t", "run-1", "worker-b", 30.0)
    assert second is not None
    assert second.fencing_token > first.fencing_token
    assert second.owner == "worker-b"


# --------------------------------------------------------------------------- #
# Renew / validate / release
# --------------------------------------------------------------------------- #
def test_renew_by_holder_extends_expiry():
    clock = _FakeClock()
    store = _store(clock)
    lease = store.acquire("t", "run-1", "worker-a", 30.0)
    assert lease is not None
    clock.advance(20.0)
    assert store.renew(lease, 30.0) is True  # now expires at t+50
    clock.advance(25.0)  # t+45: past the ORIGINAL expiry, within the renewed one
    assert store.validate("t", "run-1", "worker-a", lease.fencing_token) is True
    assert store.acquire("t", "run-1", "worker-b", 30.0) is None


def test_renew_with_stale_token_after_takeover_fails():
    clock = _FakeClock()
    store = _store(clock)
    stale = store.acquire("t", "run-1", "worker-a", 30.0)
    assert stale is not None
    clock.advance(31.0)
    current = store.acquire("t", "run-1", "worker-b", 30.0)
    assert current is not None
    assert store.renew(stale, 30.0) is False
    assert store.renew(current, 30.0) is True


def test_validate_holder_stale_and_expired():
    clock = _FakeClock()
    store = _store(clock)
    lease = store.acquire("t", "run-1", "worker-a", 30.0)
    assert lease is not None
    assert store.validate("t", "run-1", "worker-a", lease.fencing_token) is True
    assert store.validate("t", "run-1", "worker-a", lease.fencing_token + 1) is False
    assert store.validate("t", "run-1", "worker-b", lease.fencing_token) is False
    assert store.validate("t", "run-unknown", "worker-a", lease.fencing_token) is False
    clock.advance(31.0)  # expired (nobody took over, but validate is time-bound)
    assert store.validate("t", "run-1", "worker-a", lease.fencing_token) is False


def test_release_by_holder_frees_and_by_non_holder_does_not():
    clock = _FakeClock()
    store = _store(clock)
    lease = store.acquire("t", "run-1", "worker-a", 60.0)
    assert lease is not None
    impostor = RunLease(
        tenant_id="t",
        run_id="run-1",
        owner="worker-b",
        fencing_token=lease.fencing_token,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
    )
    assert store.release(impostor) is False
    assert store.acquire("t", "run-1", "worker-b", 60.0) is None  # still held
    assert store.release(lease) is True
    freed = store.acquire("t", "run-1", "worker-b", 60.0)
    assert freed is not None
    assert freed.fencing_token > lease.fencing_token


def test_held_error_names_holder_and_expiry():
    clock = _FakeClock()
    store = _store(clock)
    set_run_lease_store(store)
    try:
        held = store.acquire("t", "run-1", "worker-a", 60.0)
        assert held is not None
        from forgeflow.runtime.run_lease import acquire_run_lease

        with pytest.raises(RunLeaseHeldError) as excinfo:
            acquire_run_lease("t", "run-1", "worker-b", 60.0)
        err = excinfo.value
        assert err.owner == "worker-a"
        assert err.expires_at == held.expires_at
        assert "worker-a" in str(err) and "leased" in str(err)
    finally:
        reset_run_lease_store()


# --------------------------------------------------------------------------- #
# Config resolution + registry default
# --------------------------------------------------------------------------- #
def test_ttl_resolution_defaults_and_overrides():
    class _Task:
        def __init__(self, context):
            self.context = context

    assert resolve_lease_ttl_seconds(_Task({})) == 120.0
    assert resolve_lease_ttl_seconds(_Task({"run_lease_ttl_seconds": 5})) == 5.0
    # Out-of-range / wrong-type overrides fall back to the default.
    assert resolve_lease_ttl_seconds(_Task({"run_lease_ttl_seconds": 0})) == 120.0
    assert resolve_lease_ttl_seconds(_Task({"run_lease_ttl_seconds": "5"})) == 120.0
    assert resolve_lease_ttl_seconds(_Task({"run_lease_ttl_seconds": True})) == 120.0


def test_registry_default_is_inmemory_under_memory_profile(monkeypatch):
    """The offline/memory profile must never dial Postgres for a lease:
    the registry combines the checkpoint DSN rule with the repository
    profile rule (STORAGE_BACKEND is pinned to memory in the test env,
    while POSTGRES_SYNC_URL is always set by conftest)."""
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_backend", "memory")
    reset_run_lease_store()
    try:
        assert isinstance(get_run_lease_store(), InMemoryRunLeaseStore)
    finally:
        reset_run_lease_store()


def test_registry_picks_postgres_under_postgres_profile(monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    reset_run_lease_store()
    try:
        # Construction only — the store dials lazily per operation.
        assert isinstance(get_run_lease_store(), PostgresRunLeaseStore)
    finally:
        reset_run_lease_store()


# --------------------------------------------------------------------------- #
# Migration 044 structure (style of test_migration_043)
# --------------------------------------------------------------------------- #
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "alembic" / "versions"


def _load(path: pathlib.Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_source(filename: str) -> str:
    return (MIGRATIONS / filename).read_text(encoding="utf-8")


def test_migration_044_revision_chain():
    mig = _load(MIGRATIONS / "044_run_leases.py", "mig_044_under_test")
    assert mig.revision == "044"
    assert mig.down_revision == "043"
    assert callable(mig.upgrade)
    assert callable(mig.downgrade)


def test_migration_044_creates_run_leases_with_pk_and_rls():
    source = _read_source("044_run_leases.py")
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "CREATE TABLE IF NOT EXISTS run_leases" in upgrade
    assert "PRIMARY KEY (tenant_id, run_id)" in upgrade
    assert "fencing_token BIGINT NOT NULL" in upgrade
    assert "expires_at    TIMESTAMPTZ NOT NULL" in upgrade
    assert "released      BOOLEAN NOT NULL DEFAULT false" in upgrade
    assert "ALTER TABLE run_leases ENABLE ROW LEVEL SECURITY" in upgrade
    assert "ALTER TABLE run_leases FORCE ROW LEVEL SECURITY" in upgrade
    assert "DROP POLICY IF EXISTS tenant_isolation ON run_leases" in upgrade
    assert "CREATE POLICY tenant_isolation ON run_leases" in upgrade
    assert "current_setting('app.tenant_id', true)" in source
    assert "NULLIF" in source
    assert "USING" in upgrade and "WITH CHECK" in upgrade


def test_migration_044_check_constraints():
    """The invariants the lease store relies on are database-enforced:
    fencing tokens are positive, owners are non-empty, and a lease
    never expires before it was acquired."""
    source = _read_source("044_run_leases.py")
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert "CHECK (fencing_token > 0)" in upgrade
    assert "CHECK (btrim(owner) <> '')" in upgrade
    assert "CHECK (expires_at >= acquired_at)" in upgrade


def test_migration_044_downgrade_drops_policy_and_table():
    source = _read_source("044_run_leases.py")
    downgrade = source.split("def downgrade()", 1)[1]
    assert "DROP POLICY IF EXISTS tenant_isolation ON run_leases" in downgrade
    assert "ALTER TABLE run_leases NO FORCE ROW LEVEL SECURITY" in downgrade
    assert "ALTER TABLE run_leases DISABLE ROW LEVEL SECURITY" in downgrade
    assert "DROP TABLE IF EXISTS run_leases" in downgrade
