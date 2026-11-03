"""Timestamp-tie picks must break ties by insertion order, not by UUID.

The ``latest_*`` / earliest-pick queries ordered by a timestamp plus
``id`` where ``id`` is a random UUID: two rows sharing a timestamp
picked a winner by random-UUID order — nondeterministic. Migration 041
adds a monotonic ``seq`` column to each table and the queries order by
timestamp plus ``seq`` instead. Each test below writes two rows with an
IDENTICAL timestamp through the real store and asserts the
insertion-order winner (second-inserted for latest-picks,
first-inserted for the earliest-pick). Random tenants per test isolate
runs from each other (no cleanup dependence).
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timezone

import pytest

from forgeflow.config import get_settings
from forgeflow.lifecycle.store import (
    LifecycleEvent,
    MergeProposalRecord,
    PostgresLifecycleStore,
)
from forgeflow.metrics.store import (
    BenchmarkRunRecord,
    MetricSnapshotRecord,
    PostgresMetricsStore,
)
from forgeflow.rollout.store import PostgresRolloutStore, RolloutRecord

_T0 = datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)


def _sync_dsn() -> str:
    return get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")


def _pg_reachable() -> bool:
    dsn = _sync_dsn()
    host, _, rest = dsn.rpartition("@")[2].partition(":")
    port = int(rest.split("/")[0]) if rest.split("/")[0].isdigit() else 5432
    try:
        with socket.create_connection((host or "127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _pg_reachable(), reason="live PostgreSQL unreachable")


def _tenant() -> str:
    return f"t-tie-{uuid.uuid4().hex[:8]}"


def test_lifecycle_latest_breaks_created_at_tie_by_insertion():
    tenant = _tenant()
    store = PostgresLifecycleStore(_sync_dsn())
    store.append_event(
        LifecycleEvent(
            tenant_id=tenant, skill_id="s1", from_state="published",
            to_state="deprecated", reason="first", actor="t",
            created_at=_T0,
        )
    )
    store.append_event(
        LifecycleEvent(
            tenant_id=tenant, skill_id="s1", from_state="deprecated",
            to_state="published", reason="second", actor="t",
            created_at=_T0,
        )
    )
    latest = PostgresLifecycleStore(_sync_dsn()).latest_event(tenant, "s1")
    assert latest is not None
    assert latest.reason == "second"
    assert latest.to_state == "published"


def test_benchmark_latest_breaks_created_at_tie_by_insertion():
    tenant = _tenant()
    store = PostgresMetricsStore(_sync_dsn())
    store.add_benchmark_run(
        BenchmarkRunRecord(
            tenant_id=tenant, corpus_hash="a" * 64, total_cases=10,
            passed=10, failed=0, errors=0, skipped=0, report={},
            created_at=_T0,
        )
    )
    store.add_benchmark_run(
        BenchmarkRunRecord(
            tenant_id=tenant, corpus_hash="b" * 64, total_cases=10,
            passed=9, failed=1, errors=0, skipped=0, report={},
            created_at=_T0,
        )
    )
    latest = PostgresMetricsStore(_sync_dsn()).latest_benchmark_run(tenant)
    assert latest is not None
    assert latest.corpus_hash == "b" * 64


def test_snapshot_latest_breaks_generated_at_tie_by_insertion():
    tenant = _tenant()
    store = PostgresMetricsStore(_sync_dsn())
    store.add_snapshot(
        MetricSnapshotRecord(
            tenant_id=tenant, metrics={"a": 1}, total_tasks=5,
            generated_at=_T0,
        )
    )
    store.add_snapshot(
        MetricSnapshotRecord(
            tenant_id=tenant, metrics={"a": 2}, total_tasks=9,
            generated_at=_T0,
        )
    )
    latest = PostgresMetricsStore(_sync_dsn()).latest_snapshot(tenant)
    assert latest is not None
    assert latest.total_tasks == 9


def test_proposal_earliest_breaks_created_at_tie_by_insertion():
    tenant = _tenant()
    store = PostgresLifecycleStore(_sync_dsn())
    first = store.save_proposal(
        MergeProposalRecord(
            tenant_id=tenant, primary_skill_id="p1", duplicate_skill_id="d1",
            source_skill_ids=["p1", "d1"], created_at=_T0,
        )
    )
    store.save_proposal(
        MergeProposalRecord(
            tenant_id=tenant, primary_skill_id="p1", duplicate_skill_id="d1",
            source_skill_ids=["p1", "d1"], created_at=_T0,
        )
    )
    # Earliest pick: the FIRST-inserted row wins (seq ASC), not the one
    # whose random UUID happens to sort first. (PG returns UUIDs in dashed
    # form while the record holds hex, so compare dash-stripped.)
    found = PostgresLifecycleStore(_sync_dsn()).find_proposal(tenant, "p1", "d1")
    assert found is not None
    assert found.id.replace("-", "") == first.id.replace("-", "")


def test_rollout_latest_breaks_created_at_tie_by_insertion():
    tenant = _tenant()
    store = PostgresRolloutStore(_sync_dsn())
    store.save_rollout(
        RolloutRecord(
            tenant_id=tenant, skill_id="s1", candidate_version="v1",
            incumbent_version="v0", stage_pct=25, state="canary",
            created_at=_T0,
        )
    )
    store.save_rollout(
        RolloutRecord(
            tenant_id=tenant, skill_id="s1", candidate_version="v2",
            incumbent_version="v0", stage_pct=100, state="canary",
            created_at=_T0,
        )
    )
    latest = PostgresRolloutStore(_sync_dsn()).latest_rollout(tenant, "s1")
    assert latest is not None
    assert latest.candidate_version == "v2"
    assert latest.stage_pct == 100
