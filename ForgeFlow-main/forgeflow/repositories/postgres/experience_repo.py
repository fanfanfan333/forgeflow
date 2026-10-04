"""PostgreSQL/pgvector Experience repository.

Importing this module does **not** import asyncpg — the pool is resolved
lazily from ``forgeflow.database`` on first use, so the module imports cleanly
in the offline (memory) profile too.

Tenant scoping uses the single-yardstick partition key (docs §11 R7): the
``tenant_id`` / ``team_id`` columns are opaque ``TEXT`` (since migration
``013``), so the value is stored exactly as the application hands it —
``tenant_id`` normalised through ``scope_key`` (``None`` → the ``"default"``
bucket). There is no UUID coercion: a non-UUID tenant slug is a first-class
tenant, never silently collapsed into a shared ``NULL`` bucket.
"""

from __future__ import annotations

import uuid
from typing import Any

from forgeflow.experience.models import ExperienceRecord
from forgeflow.repositories.base import (
    TenantScopedRepository,
    utcnow,
)


def _vector_literal(embedding: list[float] | None) -> str | None:
    """Render a Python list as a pgvector literal, e.g. ``[0.1,0.2,...]``."""
    if embedding is None:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _uuid_array(values: list[str] | None) -> list[uuid.UUID]:
    """Coerce a lineage id list into a ``uuid.UUID`` list for a ``UUID[]`` column.

    The ``merged_from`` / ``conflict_with`` columns are ``UUID[] NOT NULL DEFAULT
    '{}'`` — we always pass a real (possibly empty) list, never ``None``. Legacy
    rows whose arrays are NULL are normalised to ``[]`` on read (see below).
    """
    return [uuid.UUID(str(v)) for v in (values or [])]


def _read_uuid_array(value: Any) -> list[str]:
    """Normalise a ``UUID[]`` column value to ``list[str]`` (NULL/None → [])."""
    if value is None:
        return []
    return [str(item) for item in value]


def _read_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _row_to_record(row: Any, memory_ids: list[str] | None = None) -> ExperienceRecord:
    data = dict(row)
    return ExperienceRecord(
        id=str(data["id"]),
        tenant_id=str(data["tenant_id"]) if data.get("tenant_id") else None,
        team_id=str(data["team_id"]) if data.get("team_id") else None,
        run_id=str(data.get("run_id") or ""),
        summary=data.get("summary") or "",
        decisions=list(data.get("decisions") or []),
        outcome=data.get("outcome") or "success",
        reusable_steps=list(data.get("reusable_steps") or []),
        tags=list(data.get("tags") or []),
        embedding=None,  # vectors are not round-tripped by default (avoid large reads)
        merged_from=_read_uuid_array(data.get("merged_from")),
        conflict_with=_read_uuid_array(data.get("conflict_with")),
        dedup_key=data.get("dedup_key"),
        confidence=_read_float(data.get("confidence")),
        scrub_status=data.get("scrub_status"),
        scrub_version=data.get("scrub_version"),
        created_at=data.get("created_at") or utcnow(),
        memory_ids=list(memory_ids or []),
    )


class PgExperienceRepository(TenantScopedRepository):
    """asyncpg-backed ``ExperienceRepository`` (identical surface to memory)."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool  # lazy — keeps asyncpg out of import graph

        return await get_pool()

    async def save(self, record: ExperienceRecord) -> ExperienceRecord:
        # Red line 13 — scrub user-document content *before* it is persisted;
        # shared with the memory backend so both stamp identically.
        from forgeflow.repositories.memory.experience_repo import scrub_in_place

        scrub_in_place(record)
        # INC46 T33 (红线 14) — a flagged experience is diverted to the
        # quarantine and never INSERTed here, so the miner cannot read it.
        from forgeflow.security.quarantine import inspect_and_quarantine

        if inspect_and_quarantine(record).quarantined:
            return record
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO experiences
                  (id, tenant_id, team_id, run_id, summary, decisions, outcome,
                   reusable_steps, tags, embedding,
                   merged_from, conflict_with, dedup_key, confidence, created_at,
                   scrub_status, scrub_version)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::vector,
                        $11, $12, $13, $14, $15, $16, $17)
                ON CONFLICT (id) DO UPDATE SET
                  summary = EXCLUDED.summary,
                  decisions = EXCLUDED.decisions,
                  outcome = EXCLUDED.outcome,
                  reusable_steps = EXCLUDED.reusable_steps,
                  tags = EXCLUDED.tags,
                  embedding = EXCLUDED.embedding,
                  merged_from = EXCLUDED.merged_from,
                  conflict_with = EXCLUDED.conflict_with,
                  dedup_key = EXCLUDED.dedup_key,
                  confidence = EXCLUDED.confidence,
                  scrub_status = EXCLUDED.scrub_status,
                  scrub_version = EXCLUDED.scrub_version
                """,
                record.id,
                self.scope_key(record.tenant_id),
                record.team_id,
                # ``experiences.run_id`` is opaque TEXT (migration 014): the hub
                # run id is stored verbatim. ``or None`` keeps the historical
                # meaning of an empty id ("not linked to a run") as NULL.
                record.run_id or None,
                record.summary,
                record.decisions,
                record.outcome,
                record.reusable_steps,
                record.tags,
                _vector_literal(record.embedding),
                _uuid_array(record.merged_from),
                _uuid_array(record.conflict_with),
                record.dedup_key,
                record.confidence,
                record.created_at,
                record.scrub_status,
                record.scrub_version,
            )
        return record

    async def get(self, tenant_id: str | None, experience_id: str) -> ExperienceRecord | None:
        # INC43 §3.4 / BE-5 — fail-closed: an unresolved tenant reads NOTHING
        # (never the shared "default" bucket, never every tenant's rows).
        if not tenant_id:
            return None
        pool = await self._get_pool()
        tenant = self.scope_key(tenant_id)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM experiences "
                "WHERE id = $1 AND tenant_id IS NOT DISTINCT FROM $2",
                experience_id,
                tenant,
            )
            if row is None:
                return None
            mem_rows = await conn.fetch(
                "SELECT memory_id FROM experience_memory WHERE experience_id = $1",
                experience_id,
            )
        return _row_to_record(row, [str(m["memory_id"]) for m in mem_rows])

    async def list(
        self,
        tenant_id: str | None,
        *,
        outcome: str | None = None,
        tag: str | None = None,
        run_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExperienceRecord]:
        if not tenant_id:  # BE-5 fail-closed (discovery returns empty, not all)
            return []
        pool = await self._get_pool()
        clauses = ["tenant_id IS NOT DISTINCT FROM $1"]
        args: list[Any] = [self.scope_key(tenant_id)]
        if outcome:
            args.append(outcome)
            clauses.append(f"outcome = ${len(args)}")
        if tag:
            args.append(tag)
            clauses.append(f"${len(args)} = ANY(tags)")
        if run_id:
            # No coercion: the column is TEXT (014), so a non-UUID hub run id is
            # compared literally. The old ``uuid_or_none`` turned it into
            # ``run_id = NULL`` — a predicate that is never true, i.e. a
            # permanently empty result with no error.
            args.append(run_id)
            clauses.append(f"run_id = ${len(args)}")
        args.append(limit)
        limit_idx = len(args)
        args.append(offset)
        offset_idx = len(args)
        sql = (
            "SELECT * FROM experiences WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY created_at DESC LIMIT ${limit_idx} OFFSET ${offset_idx}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [_row_to_record(r) for r in rows]

    async def find_similar(
        self,
        tenant_id: str | None,
        embedding: list[float] | None,
        *,
        k: int = 10,
        min_similarity: float = 0.85,
        tags: list[str] | None = None,
    ) -> list[tuple[ExperienceRecord, float]]:
        if not tenant_id:  # BE-5 fail-closed
            return []
        pool = await self._get_pool()
        tenant = self.scope_key(tenant_id)
        if embedding is None:
            # Degrade to tag overlap (docs R6) — no embedding ⇒ no vector search.
            if not tags:
                return []
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM experiences "
                    "WHERE tenant_id IS NOT DISTINCT FROM $1 AND tags && $2 "
                    "ORDER BY created_at DESC LIMIT $3",
                    tenant,
                    list(tags),
                    k,
                )
            return [(_row_to_record(r), float(min_similarity)) for r in rows]

        lit = _vector_literal(embedding)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT *, 1 - (embedding <=> $1::vector) AS sim
                FROM experiences
                WHERE tenant_id IS NOT DISTINCT FROM $2
                  AND embedding IS NOT NULL
                  AND 1 - (embedding <=> $1::vector) >= $3
                ORDER BY embedding <=> $1::vector
                LIMIT $4
                """,
                lit,
                tenant,
                float(min_similarity),
                k,
            )
        return [(_row_to_record(r), float(r["sim"])) for r in rows]

    async def link_memory(
        self,
        tenant_id: str | None,
        experience_id: str,
        memory_id: str,
        relation: str = "source",
    ) -> None:
        # INC43 §3.4 / BE-5 — fail-closed write: an unresolved tenant links
        # nothing (never into the shared "default" bucket).
        if not tenant_id:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO experience_memory (experience_id, memory_id, relation)
                VALUES ($1, $2, $3)
                ON CONFLICT (experience_id, memory_id) DO NOTHING
                """,
                experience_id,
                memory_id,
                relation,
            )

    async def list_memories(self, tenant_id: str | None, experience_id: str) -> list[str]:
        if not tenant_id:  # BE-5 fail-closed
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT memory_id FROM experience_memory WHERE experience_id = $1",
                experience_id,
            )
        return [str(r["memory_id"]) for r in rows]

    async def count(self, tenant_id: str | None) -> int:
        if not tenant_id:  # BE-5 fail-closed: an unresolved tenant counts zero
            return 0
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS c FROM experiences "
                "WHERE tenant_id IS NOT DISTINCT FROM $1",
                self.scope_key(tenant_id),
            )
        return int(row["c"]) if row else 0
