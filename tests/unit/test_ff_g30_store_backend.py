"""FF-G30 — checkpoint and lease stores share ONE backend decision.

The bug: ``get_checkpoint_store`` chose Postgres whenever a sync
DSN was merely *present* in the environment, while
``get_run_lease_store`` also required the postgres *profile*. With
a DSN configured but ``storage_backend=memory``, the first
checkpoint write landed in Postgres, found no ``run_leases`` row,
and the run died ``lease_lost`` instead of running (24 loop/react
tests failed under the ``run_tests`` wrapper for exactly this).

The rule now: ``storage_backend`` is authoritative and both stores
resolve through :func:`forgeflow.runtime.store_backend.select_store_backend`:

* memory profile → both stores memory, even with a DSN set;
* postgres profile → both stores Postgres;
* postgres profile without a DSN → a clear configuration error
  (which never contains a URL or a secret value).

The DSN below is an obvious fake and is never dialled: the memory
assertions prove nothing connects, and the Postgres store
constructors are lazy (they only keep the DSN).
"""

from __future__ import annotations

import pytest

from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    PostgresCheckpointStore,
    get_checkpoint_store,
    reset_checkpoint_store,
)
from forgeflow.runtime.run_lease import (
    InMemoryRunLeaseStore,
    PostgresRunLeaseStore,
    get_run_lease_store,
    reset_run_lease_store,
)
from forgeflow.runtime.store_backend import (
    StoreBackendConfigurationError,
    require_postgres_dsn,
    select_store_backend,
)

FAKE_DSN = "postgresql://ff-g30-fake:ff-g30-fake@127.0.0.1:1/ff_g30_fake"


@pytest.fixture()
def fresh_stores():
    reset_checkpoint_store()
    reset_run_lease_store()
    yield
    reset_checkpoint_store()
    reset_run_lease_store()


def test_memory_profile_wins_over_a_configured_dsn(monkeypatch, force_memory_backend, fresh_stores):
    monkeypatch.setenv("POSTGRES_SYNC_URL", FAKE_DSN)
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    assert select_store_backend() == "memory"
    assert isinstance(get_checkpoint_store(), InMemoryCheckpointStore)
    assert isinstance(get_run_lease_store(), InMemoryRunLeaseStore)


def _load_inc17():
    """inc17's drive harness, loaded by path (test modules are not a package)."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).with_name("test_inc17_react_loop.py")
    spec = importlib.util.spec_from_file_location("inc17_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_react_run_ends_on_max_iterations_not_lease_lost(
    monkeypatch, force_memory_backend, fresh_stores
):
    """The Codex repro, driven end to end (inc17's cutoff scenario)."""
    from forgeflow.runtime.orchestrator import reset_run_store
    from forgeflow.runtime.tool_registry import (
        ToolBinding,
        load_default_bindings,
        register,
        reset_registry,
    )

    inc17 = _load_inc17()
    monkeypatch.setenv("POSTGRES_SYNC_URL", FAKE_DSN)
    monkeypatch.setenv("FORGEFLOW_SINGLE_PROCESS_DEV", "1")
    reset_checkpoint_store()
    reset_run_lease_store()
    reset_run_store()
    reset_registry()
    # inc17's fake_research fixture, inlined: a deterministic search binding.
    load_default_bindings()
    register(
        ToolBinding(
            tool_id="research.search",
            handler=inc17._fake_research,
            kind="real",
            provider="test",
            description="scripted test search",
        )
    )
    script = [
        {
            "content": "继续查",
            "tool_calls": [{"name": "research.search", "args": {"query": "q"}, "id": "c"}],
        }
    ]
    try:
        _fake, task, _bus, handle = await inc17._drive(
            monkeypatch, script, context={"max_react_iterations": 2}
        )
    finally:
        reset_run_store()
        reset_registry()
    meta = task.context["llm_runtime"]
    assert meta["terminated_by"] == "max_iterations"
    assert handle.status != "completed"
    assert "lease_lost" not in str(task.context)


def test_postgres_profile_selects_postgres_for_both_stores(
    monkeypatch, force_memory_backend, fresh_stores
):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    monkeypatch.setenv("POSTGRES_SYNC_URL", FAKE_DSN)
    assert select_store_backend() == "postgres"
    assert isinstance(get_checkpoint_store(), PostgresCheckpointStore)
    assert isinstance(get_run_lease_store(), PostgresRunLeaseStore)


def test_postgres_profile_without_a_dsn_is_a_clear_configuration_error(
    monkeypatch, force_memory_backend, fresh_stores
):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    monkeypatch.delenv("POSTGRES_SYNC_URL", raising=False)
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("FORGEFLOW_SINGLE_PROCESS_DEV", raising=False)
    assert select_store_backend() == "postgres"  # the profile still decides
    with pytest.raises(StoreBackendConfigurationError) as excinfo:
        require_postgres_dsn()
    message = str(excinfo.value)
    assert "POSTGRES_SYNC_URL" in message
    assert "://" not in message  # the error never carries a URL or a secret
    with pytest.raises(StoreBackendConfigurationError):
        get_checkpoint_store()
    # And the lease store fails closed for the same configuration
    # (its own pinned error, outside single-process dev mode).
    with pytest.raises(RuntimeError, match="run leases require the Postgres backend"):
        get_run_lease_store()
