"""INC2-16 — the four de-dup columns truly round-trip through the repositories.

This is the "落盘 ≠ 交付" guard the team-lead called out: the four columns
(``merged_from`` / ``conflict_with`` / ``dedup_key`` / ``confidence``) must be
persisted by **value**, not smuggled through Python object identity.

* Memory backend: proven with a **fresh instance read** — the object returned by
  ``get`` is not the object handed to ``save``, and mutating the saved input
  afterwards does not change stored state.
* PostgreSQL backend: a real INSERT + SELECT against the dev PG on :5433 when it
  is reachable (``merged_from`` / ``conflict_with`` as non-empty ``UUID[]`` plus
  a non-null ``dedup_key`` / ``confidence``). If PG is down the test SKIPS
  loudly rather than faking a pass.
"""

from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.experience.models import ExperienceRecord
from forgeflow.repositories.memory.experience_repo import (
    MemoryExperienceRepository,
    clear_memory_store,
)

# --------------------------------------------------------------------------- #
# memory backend — fresh-instance round-trip                                   #
# --------------------------------------------------------------------------- #

_A = "11111111-1111-1111-1111-111111111111"
_B = "22222222-2222-2222-2222-222222222222"
_C = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _clean():
    clear_memory_store()
    yield
    clear_memory_store()


async def test_memory_round_trip_via_new_instance():
    repo = MemoryExperienceRepository()
    tenant = f"t-rt-{uuid.uuid4().hex[:8]}"
    rec = ExperienceRecord(
        tenant_id=tenant,
        run_id="r",
        summary="round-trip",
        outcome="success",
        merged_from=[_A],
        conflict_with=[_B],
        dedup_key="key-123",
        confidence=0.75,
    )
    await repo.save(rec)

    got = await repo.get(tenant, rec.id)
    assert got is not None
    assert got is not rec                     # a NEW instance, not the same object
    assert got.merged_from == [_A]
    assert got.conflict_with == [_B]
    assert got.dedup_key == "key-123"
    assert got.confidence == pytest.approx(0.75)


async def test_memory_save_snapshots_do_not_alias_caller():
    repo = MemoryExperienceRepository()
    tenant = f"t-rt-{uuid.uuid4().hex[:8]}"
    rec = ExperienceRecord(tenant_id=tenant, run_id="r", summary="s", merged_from=[_A])
    await repo.save(rec)

    # Mutating the saved input must NOT leak into storage.
    rec.merged_from.append(_C)
    rec.confidence = 0.99

    got = await repo.get(tenant, rec.id)
    assert got.merged_from == [_A]
    assert got.confidence is None


async def test_memory_find_similar_returns_copies_with_lineage():
    repo = MemoryExperienceRepository()
    tenant = f"t-rt-{uuid.uuid4().hex[:8]}"
    await repo.save(
        ExperienceRecord(
            tenant_id=tenant,
            run_id="r",
            summary="s",
            embedding=[1.0, 0.0, 0.0],
            conflict_with=[_A],
        )
    )
    pairs = await repo.find_similar(tenant, [1.0, 0.0, 0.0], min_similarity=0.5)
    assert pairs and pairs[0][0].conflict_with == [_A]


# --------------------------------------------------------------------------- #
# postgres backend — real INSERT + SELECT when PG is reachable                 #
# --------------------------------------------------------------------------- #

_PG_DSNS = [
    os.environ.get("FF_TEST_DSN"),
    (os.environ.get("POSTGRES_URL") or "").replace("postgresql+asyncpg://", "postgresql://")
    or None,
    "postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow",
    "postgresql://postgres:postgres@127.0.0.1:5433/forgeflow",
]
_REQUIRED_COLUMNS = {"merged_from", "conflict_with", "dedup_key", "confidence"}


async def _maybe_pool():
    """Return a live asyncpg pool, or (None, reason) when PG can't be reached."""
    asyncpg = pytest.importorskip("asyncpg")
    from forgeflow.database import _init_connection  # reuse the JSONB codec setup

    last_error = "no DSN candidates"
    for dsn in [d for d in _PG_DSNS if d]:
        try:
            pool = await asyncpg.create_pool(
                dsn, min_size=1, max_size=2, command_timeout=15, init=_init_connection
            )
            return pool, None
        except Exception as exc:  # noqa: BLE001 — try the next candidate
            last_error = f"{type(exc).__name__}: {exc}"
    return None, last_error


async def test_postgres_round_trip_four_columns():
    pool, error = await _maybe_pool()
    if pool is None:
        pytest.skip(f"PG :5433 unreachable — round-trip NOT verified ({error})")

    from forgeflow.repositories.postgres.experience_repo import PgExperienceRepository

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'experiences'"
            )
            columns = {r["column_name"] for r in rows}
        if not columns:
            pytest.skip("experiences table missing — run `alembic upgrade head`")
        if not _REQUIRED_COLUMNS <= columns:
            pytest.skip(f"experiences missing 011 columns: {sorted(_REQUIRED_COLUMNS - columns)}")

        repo = PgExperienceRepository(pool=pool)
        tenant = str(uuid.uuid4())
        m1, m2, m3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        rec = ExperienceRecord(
            tenant_id=tenant,
            run_id=str(uuid.uuid4()),
            summary="pg round-trip",
            outcome="success",
            merged_from=[m1, m2],
            conflict_with=[m3],
            dedup_key="pg-key",
            confidence=0.66,
        )
        await repo.save(rec)
        got = await repo.get(tenant, rec.id)

        assert got is not None, "row must be readable back"
        assert set(got.merged_from) == {m1, m2}          # UUID[] non-empty round-trips
        assert got.conflict_with == [m3]
        assert got.dedup_key == "pg-key"                 # non-null TEXT round-trips
        assert got.confidence == pytest.approx(0.66)     # non-null DOUBLE round-trips

        # Empty arrays must persist as [] (NOT NULL DEFAULT), not NULL.
        empty = ExperienceRecord(
            tenant_id=tenant,
            run_id=str(uuid.uuid4()),
            summary="pg empty arrays",
            outcome="success",
        )
        await repo.save(empty)
        got_empty = await repo.get(tenant, empty.id)
        assert got_empty.merged_from == []
        assert got_empty.conflict_with == []
        assert got_empty.dedup_key is None

        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM experiences WHERE tenant_id = $1", uuid.UUID(tenant))
    finally:
        await pool.close()
