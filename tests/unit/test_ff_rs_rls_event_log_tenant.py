"""FF-RS-RLS fix 1 — PostgresRunEventLog.append must carry the run's tenant.

On the real stack (restricted runtime role + forced RLS) an append
made without an ambient tenant wrote ``tenant_id = NULL`` and
``run_events``' RLS policy rejected every row. The append path only
knows the run_id, so the tenant must be resolved from the run itself.
These tests fake the psycopg connection (no database involved) and
pin what reaches the INSERT and the ``app.tenant_id`` session setting.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from forgeflow.runtime.event_log import PostgresRunEventLog
from forgeflow.tenant_scope import tenant_scope


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.statements.append((sql, params))


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.committed = False

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.statements.append((sql, params))

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed = True


@pytest.fixture()
def fake_pg(monkeypatch):
    conn = _FakeConn()
    module = types.ModuleType("psycopg")
    module.connect = lambda dsn: conn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", module)
    return conn


@pytest.fixture()
def run_store(monkeypatch):
    import forgeflow.runtime.orchestrator as orch

    store = SimpleNamespace(get=lambda run_id: None)
    monkeypatch.setattr(orch, "get_run_store", lambda: store)
    return store


def _insert_params(conn: _FakeConn) -> tuple:
    for sql, params in conn.statements:
        if "INSERT INTO run_events" in sql:
            return params
    raise AssertionError(f"no INSERT recorded: {conn.statements!r}")


def _set_config_tenant(conn: _FakeConn) -> str:
    for sql, params in conn.statements:
        if "set_config('app.tenant_id'" in sql:
            return params[0]
    raise AssertionError(f"no set_config recorded: {conn.statements!r}")


def test_append_without_ambient_tenant_uses_the_runs_tenant(fake_pg, run_store):
    run_store.get = lambda run_id: SimpleNamespace(tenant_id="t-rs-1")
    log = PostgresRunEventLog("postgresql://runtime@127.0.0.1/db")
    log.append("run-1", 1, "run.started", {"intent": "x"})
    params = _insert_params(fake_pg)
    assert params[4] == "t-rs-1", f"tenant_id column must be the run's tenant, got {params[4]!r}"
    assert _set_config_tenant(fake_pg) == "t-rs-1"
    assert fake_pg.committed


def test_append_with_ambient_tenant_keeps_ambient(fake_pg, run_store):
    run_store.get = lambda run_id: SimpleNamespace(tenant_id="t-rs-1")
    log = PostgresRunEventLog("postgresql://runtime@127.0.0.1/db")
    with tenant_scope("t-ambient"):
        log.append("run-1", 1, "run.started", {})
    assert _insert_params(fake_pg)[4] == "t-ambient"


def test_append_unknown_run_without_ambient_stays_null(fake_pg, run_store):
    log = PostgresRunEventLog("postgresql://runtime@127.0.0.1/db")
    log.append("run-ghost", 1, "run.started", {})
    assert _insert_params(fake_pg)[4] is None
