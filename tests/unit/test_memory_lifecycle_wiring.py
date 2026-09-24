"""INC9 B2/B3 — memory lifecycle + two-dimensional model *wiring*.

Pins the runtime behaviour docs/sop/12-INC9-DESIGN.md §10 (unit-wiring):
archive exclusion in the read paths, `lifecycle_sweep` idempotency (and the
default no-op), the reuse signal, the promote type-transition, and — crucially —
that ``/memory/lifecycle`` is NOT swallowed by ``/{memory_id}`` (the route-order
trap the design calls out).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.routers import memory as memory_router
from forgeflow.experience.memory_store import (
    archive_memory,
    clear_memory_entries,
    get_memory,
    lifecycle_sweep,
    lifecycle_summary,
    list_memories,
    mark_reused,
    save_memory,
    search,
)
from forgeflow.experience.promotion import clear_promotion_audit, list_promotion_audit, promote_memory
from forgeflow.experience.scopes import list_scopes

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"t-life-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _memory_backend(force_memory_backend):
    """Declare the backend this suite means to exercise (INC8 lesson: never
    inherit it from the environment)."""
    clear_memory_entries()
    yield force_memory_backend
    clear_memory_entries()


# --------------------------------------------------------------------------- #
# read-path archive exclusion (default => no change)                            #
# --------------------------------------------------------------------------- #

async def test_archived_entries_are_excluded_from_list_and_search():
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "archivable fact")
    await save_memory(tenant, "semantic", "kept fact")

    assert await archive_memory(tenant, entry.id, reason="test") is not None

    listed = await list_memories(tenant)
    assert entry.id not in [r.id for r in listed]

    listed_all = await list_memories(tenant, include_archived=True)
    assert entry.id in [r.id for r in listed_all]

    # Search honours the same default.
    default_hits = await search(tenant, "archivable fact", k=10)
    assert entry.id not in [e.id for e, _ in default_hits]
    all_hits = await search(tenant, "archivable fact", k=10, include_archived=True)
    assert entry.id in [e.id for e, _ in all_hits]


async def test_archive_is_a_marker_not_a_delete():
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "marker only")
    await archive_memory(tenant, entry.id, reason="why")
    stored = await get_memory(tenant, entry.id)
    assert stored is not None  # still present
    assert stored.archived is True
    assert stored.metadata.get("archived") is True
    assert "archived_at" in stored.metadata


# --------------------------------------------------------------------------- #
# sweep — default no-op + idempotency                                           #
# --------------------------------------------------------------------------- #

async def test_sweep_is_a_noop_when_decay_is_disabled(monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "memory_decay_enabled", False)
    tenant = _tenant()
    await save_memory(tenant, "semantic", "x")
    result = await lifecycle_sweep(tenant)
    assert result == {"enabled": False, "scored": 0, "archived": 0, "skipped": 1}
    assert await list_memories(tenant) != []  # nothing was archived


async def test_sweep_with_default_thresholds_never_archives_and_is_idempotent(monkeypatch):
    from forgeflow.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "memory_decay_enabled", True)
    monkeypatch.setattr(settings, "memory_archive_min_score", 0.0)
    monkeypatch.setattr(settings, "memory_archive_max_age_days", 0.0)

    tenant = _tenant()
    await save_memory(tenant, "semantic", "a")
    await save_memory(tenant, "user", "b", actor_id="u1")

    first = await lifecycle_sweep(tenant)
    second = await lifecycle_sweep(tenant)
    assert first == second == {"enabled": True, "scored": 2, "archived": 0, "skipped": 0}


async def test_sweep_archives_below_threshold_then_is_idempotent(monkeypatch):
    from forgeflow.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "memory_decay_enabled", True)
    # Every entry scores well below 0.9 (no reuse, not promoted) ⇒ all archived.
    monkeypatch.setattr(settings, "memory_archive_min_score", 0.9)
    monkeypatch.setattr(settings, "memory_archive_max_age_days", 0.0)

    tenant = _tenant()
    await save_memory(tenant, "semantic", "a")
    await save_memory(tenant, "semantic", "b")

    first = await lifecycle_sweep(tenant)
    assert first["archived"] == 2
    # Second run: the two are already archived ⇒ nothing new archived.
    second = await lifecycle_sweep(tenant)
    assert second["archived"] == 0
    assert second["skipped"] == 2


async def test_lifecycle_summary_reports_counts():
    tenant = _tenant()
    e1 = await save_memory(tenant, "semantic", "a")
    await save_memory(tenant, "semantic", "b")
    await archive_memory(tenant, e1.id)
    summary = await lifecycle_summary(tenant)
    assert summary["total"] == 2
    assert summary["archived"] == 1
    assert summary["active"] == 1
    assert summary["avg_score"] is not None


async def test_lifecycle_summary_avg_is_none_when_empty():
    summary = await lifecycle_summary(_tenant())
    assert summary["avg_score"] is None  # no data, never a fabricated 0


# --------------------------------------------------------------------------- #
# reuse signal                                                                  #
# --------------------------------------------------------------------------- #

async def test_mark_reused_bumps_the_counter():
    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "reusable")
    bumped = await mark_reused(tenant, [entry.id, "missing-id"])
    assert bumped == 1
    assert (await get_memory(tenant, entry.id)).reuse_count == 1


async def test_context_builder_marks_selected_memory_reused():
    from forgeflow.experience.context_builder import build_context

    tenant = _tenant()
    entry = await save_memory(tenant, "semantic", "销售线索评分规则：结合互动频次。")
    await build_context(tenant, "销售线索评分", budget_tokens=200, k_memory=5)
    fresh = await get_memory(tenant, entry.id)
    assert fresh.reuse_count >= 1


# --------------------------------------------------------------------------- #
# two-dimensional model — type normalisation + promote transition               #
# --------------------------------------------------------------------------- #

async def test_save_normalises_an_invalid_type():
    tenant = _tenant()
    entry = await save_memory(tenant, "episodic", "trace", memory_type="bogus")
    assert entry.memory_type == "episodic"  # default_type_for_scope("episodic")


async def test_save_respects_a_valid_explicit_type():
    tenant = _tenant()
    entry = await save_memory(tenant, "user", "x", memory_type="procedural")
    assert entry.memory_type == "procedural"


async def test_promote_advances_the_type_ladder_and_audits_it():
    tenant = _tenant()
    clear_promotion_audit()
    entry = await save_memory(tenant, "user", "个人经验", actor_id="u1")
    assert entry.memory_type == "working"

    moved = await promote_memory(
        tenant, entry.id, "user", "team", actor_id="u1", role="member"
    )
    assert moved.memory_type == "episodic"  # working → episodic

    event = list_promotion_audit()[-1]
    assert event.metadata["from_type"] == "working"
    assert event.metadata["to_type"] == "episodic"


def test_scope_catalogue_exposes_the_type_dimension():
    scopes = list_scopes()
    assert all("memory_type" in s for s in scopes)
    by_scope = {s["scope"]: s["memory_type"] for s in scopes}
    assert by_scope["user"] == "working"
    assert by_scope["semantic"] == "semantic"


# --------------------------------------------------------------------------- #
# route order — lifecycle must not be captured as {memory_id}                    #
# --------------------------------------------------------------------------- #

def _memory_client() -> TestClient:
    minimal = FastAPI()
    minimal.include_router(memory_router.router, prefix="/memory")
    return TestClient(minimal)


def test_lifecycle_get_route_is_not_swallowed_by_memory_id():
    response = _memory_client().get("/memory/lifecycle")
    assert response.status_code == 200
    body = response.json()
    assert "active" in body and "archived" in body


def test_lifecycle_sweep_post_route_is_not_swallowed_by_memory_id():
    response = _memory_client().post("/memory/lifecycle/sweep")
    assert response.status_code == 200
    assert "scored" in response.json()
