"""PostgreSQL Resource Center repository (INC25 W1) — asyncpg, lazy pool.

Talks to the ``resources`` table created by migration ``015``. ``tenant_id`` is
an opaque ``TEXT`` (matching the post-``013`` hub convention): the tenant id is
stored exactly as the application hands it, normalised only through
``scope_key`` so a missing tenant lands in the well-defined default bucket rather
than a shared ``NULL`` one.

``summary`` / ``locator`` are JSONB; asyncpg returns them as ``str`` unless a
codec is registered, so the reader ``json.loads`` them defensively (the same
pattern as ``repositories/postgres/eval_sample_repo.py``).

Importing this module opens no connection — asyncpg is imported lazily inside
``_get_pool`` via ``forgeflow.database``.
"""

from __future__ import annotations

import json
from typing import Any

from forgeflow.resources.models import (
    ApiLocator,
    CodeLocator,
    DatabaseLocator,
    FileLocator,
    KbLocator,
    ResourceKind,
    ResourceRecord,
    ResourceSummary,
)
from forgeflow.repositories.base import TenantScopedRepository, utcnow

_LOCATOR_TYPES = {
    ResourceKind.FILE.value: FileLocator,
    ResourceKind.DATABASE.value: DatabaseLocator,
    ResourceKind.GIT_REPO.value: CodeLocator,
    ResourceKind.KNOWLEDGE_BASE.value: KbLocator,
    ResourceKind.API.value: ApiLocator,
}


def _as_dict(value: Any) -> dict[str, Any]:
    """Decode a JSONB column that may arrive as ``dict`` or JSON text."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _locator_to_dict(value: Any) -> dict[str, Any]:
    """Serialise a locator (typed dataclass with ``to_dict`` or a mapping) to JSON.

    A freshly-registered record carries a typed locator dataclass
    (:class:`~forgeflow.resources.models.FileLocator` etc.), while a
    round-tripped one carries a plain ``dict`` — both must serialise here.
    """
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    if isinstance(value, dict):
        return dict(value)
    return {}


class PgResourceRepository(TenantScopedRepository):
    """asyncpg-backed ``ResourceRepository``."""

    def __init__(self, default_tenant: str = "default", pool: Any | None = None) -> None:
        super().__init__(default_tenant)
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    @staticmethod
    def _to_record(row: Any) -> ResourceRecord:
        d = dict(row)
        kind = str(d.get("kind") or ResourceKind.FILE.value)
        summary_raw = _as_dict(d.get("summary"))
        locator_raw = _as_dict(d.get("locator"))
        locator_type = _LOCATOR_TYPES.get(kind)
        locator: Any = dict(locator_raw)
        if locator_type is not None:
            # Rebuild the typed locator so a round-tripped record exposes the
            # same attribute surface as a freshly-registered one; unknown keys
            # are dropped rather than smuggled into an unrelated locator.
            allowed = set(locator_type.__dataclass_fields__)  # type: ignore[attr-defined]
            locator = locator_type(**{k: v for k, v in locator_raw.items() if k in allowed})
        return ResourceRecord(
            id=str(d.get("id") or ""),
            tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
            kind=kind,
            name=str(d.get("name") or ""),
            created_by=str(d.get("created_by") or ""),
            created_at=str(d.get("created_at") or ""),
            status=str(d.get("status") or "registered"),
            detail=str(d.get("detail") or ""),
            summary=ResourceSummary.from_dict(summary_raw),
            locator=locator,
        )

    async def save(self, record: ResourceRecord) -> ResourceRecord:
        pool = await self._get_pool()
        created_at = record.created_at or utcnow().isoformat()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resources
                  (id, tenant_id, kind, name, created_by, created_at, status,
                   detail, summary, locator)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                  kind=EXCLUDED.kind, name=EXCLUDED.name,
                  created_by=EXCLUDED.created_by, status=EXCLUDED.status,
                  detail=EXCLUDED.detail, summary=EXCLUDED.summary,
                  locator=EXCLUDED.locator
                """,
                record.id,
                self.scope_key(record.tenant_id),
                str(record.kind),
                record.name,
                record.created_by,
                created_at,
                record.status,
                record.detail,
                json.dumps(record.summary.to_dict()),
                json.dumps(_locator_to_dict(record.locator)),
            )
        return record

    async def get(self, tenant_id: str | None, resource_id: str) -> ResourceRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM resources WHERE id=$1 AND tenant_id IS NOT DISTINCT FROM $2",
                resource_id,
                self.scope_key(tenant_id),
            )
        return self._to_record(row) if row else None

    async def list(
        self,
        tenant_id: str | None,
        *,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ResourceRecord]:
        pool = await self._get_pool()
        clauses = ["tenant_id IS NOT DISTINCT FROM $1"]
        args: list[Any] = [self.scope_key(tenant_id)]
        if kind:
            args.append(str(kind))
            clauses.append(f"kind = ${len(args)}")
        args += [limit, offset]
        sql = (
            "SELECT * FROM resources WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY created_at DESC LIMIT ${len(args) - 1} OFFSET ${len(args)}"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [self._to_record(r) for r in rows]

    async def delete(self, tenant_id: str | None, resource_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM resources WHERE id=$1 AND tenant_id IS NOT DISTINCT FROM $2",
                resource_id,
                self.scope_key(tenant_id),
            )
