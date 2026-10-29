"""Audit #4: event log must not leak or sweep across tenants."""

from __future__ import annotations

from datetime import timedelta

from forgeflow.runtime.event_log import InProcessEventLog, sweep_run_events
from forgeflow.runtime.event_log import _now as _log_now
from forgeflow.tenant_scope import tenant_scope


def test_same_run_id_different_tenants_are_isolated():
    log = InProcessEventLog()
    with tenant_scope("tenant-a"):
        log.append("run-1", 1, "step", {"who": "a"})
    with tenant_scope("tenant-b"):
        log.append("run-1", 2, "step", {"who": "b"})
    a_rows = log.read("run-1", tenant_id="tenant-a")
    b_rows = log.read("run-1", tenant_id="tenant-b")
    assert [r["data"]["who"] for r in a_rows] == ["a"]
    assert [r["data"]["who"] for r in b_rows] == ["b"]
    assert log.max_seq("run-1", tenant_id="tenant-a") == 1
    assert log.max_seq("run-1", tenant_id="tenant-b") == 2


def test_scoped_sweep_deletes_only_that_tenant():
    log = InProcessEventLog()
    with tenant_scope("tenant-a"):
        log.append("run-1", 1, "step", {})
    with tenant_scope("tenant-b"):
        log.append("run-1", 2, "step", {})
    # Old rows for both tenants: sweep only tenant-a.
    removed = sweep_run_events(
        log, retention_days=1, now=_log_now() + timedelta(days=2), tenant_id="tenant-a"
    )
    assert removed == 1
    assert log.read("run-1", tenant_id="tenant-a") == []
    assert len(log.read("run-1", tenant_id="tenant-b")) == 1


def test_unscoped_read_keeps_old_behaviour():
    log = InProcessEventLog()
    log.append("run-1", 1, "step", {})
    assert len(log.read("run-1")) == 1
