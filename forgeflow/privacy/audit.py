"""INC46 T32 — scrub audit trail (溯源可用但**最小化**).

Every scrub operation records one :class:`ScrubAudit` row into ``scrub_audits``
(migration ``026``). Provenance must be *available* but *minimal*: an audit never
stores the PII it removed — only the owning tenant, the experience id, the
scrub version transition, the categories that were hit and a count. A content
``sha256`` (of the **scrubbed** text) lets a reviewer prove two states differ
without ever holding the original bytes.

Tenant discipline (红线 5): ``tenant_id`` is the first positional argument of
every read/write. A falsy tenant reads the **empty set** (fail-closed) and
refuses to write. Nothing is coerced to ``0`` / ``''`` (未测量 ⇒ NULL, 红线 4) —
``from_version`` is ``NULL`` for a record that had never been scrubbed.

Two backends mirror the rest of the hub (see ``hitl/pending.py``): an in-process
dict store for the offline profile and a ``psycopg`` sync store for the real
``scrub_audits`` table.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from forgeflow.repositories.base import new_id, utcnow

logger = logging.getLogger(__name__)

__all__ = [
    "ScrubAudit",
    "ScrubAuditStore",
    "InMemoryScrubAuditStore",
    "PostgresScrubAuditStore",
    "get_scrub_audit_store",
    "set_scrub_audit_store",
    "reset_scrub_audit_store",
]


def _now() -> datetime:
    """Timezone-aware UTC now (one clock, mirrors the repositories base)."""
    return datetime.now(timezone.utc)


@dataclass
class ScrubAudit:
    """One scrubbed experience, minimised (no raw PII ever stored)."""

    tenant_id: str
    experience_id: str
    to_version: str
    status: str
    from_version: str | None = None
    categories: list[str] = field(default_factory=list)
    match_count: int = 0
    changed: bool = False
    content_sha256: str | None = None
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "experience_id": self.experience_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "status": self.status,
            "categories": list(self.categories),
            "match_count": self.match_count,
            "changed": self.changed,
            "content_sha256": self.content_sha256,
            "created_at": self.created_at.isoformat(),
        }


# --------------------------------------------------------------------------- #
# Store — shared logic                                                          #
# --------------------------------------------------------------------------- #
class ScrubAuditStore:
    """Tenant-scoped store for :class:`ScrubAudit` rows."""

    # --- storage primitives (per backend) ----------------------------------- #
    def _insert(self, row: dict[str, Any]) -> None:
        raise NotImplementedError

    def _scan(self, tenant: str | None, experience_id: str | None) -> list[dict[str, Any]]:
        raise NotImplementedError

    # --- writes ------------------------------------------------------------- #
    def record(self, audit: ScrubAudit) -> ScrubAudit:
        """Persist one audit row.

        Raises:
            ValueError: when ``audit.tenant_id`` is falsy (an unscoped audit must
                never be written — 红线 5).
        """
        if not audit.tenant_id:
            raise ValueError("tenant required to record a scrub audit (fail closed)")
        self._insert(
            {
                "id": audit.id,
                "tenant_id": str(audit.tenant_id),
                "experience_id": str(audit.experience_id),
                "from_version": audit.from_version,
                "to_version": audit.to_version,
                "status": audit.status,
                "categories": list(audit.categories),
                "match_count": int(audit.match_count),
                "changed": bool(audit.changed),
                "content_sha256": audit.content_sha256,
                "created_at": audit.created_at or _now(),
            }
        )
        return audit

    # --- reads -------------------------------------------------------------- #
    def list(
        self,
        tenant: str | None,
        *,
        experience_id: str | None = None,
    ) -> list[ScrubAudit]:
        """List a tenant's audits (newest first). Falsy tenant ⇒ ``[]``."""
        if not tenant:
            return []
        return [self._row_to_audit(row) for row in self._scan(tenant, experience_id)]

    @staticmethod
    def _row_to_audit(row: dict[str, Any]) -> ScrubAudit:
        categories = row.get("categories")
        if isinstance(categories, str):
            try:
                categories = json.loads(categories)
            except (TypeError, ValueError):
                categories = []
        return ScrubAudit(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            experience_id=str(row["experience_id"]),
            from_version=row.get("from_version"),
            to_version=str(row["to_version"]),
            status=str(row["status"]),
            categories=list(categories or []),
            match_count=int(row.get("match_count") or 0),
            changed=bool(row.get("changed")),
            content_sha256=row.get("content_sha256"),
            created_at=row.get("created_at") or _now(),
        )


# --------------------------------------------------------------------------- #
# In-memory backend (offline profile)                                          #
# --------------------------------------------------------------------------- #
class InMemoryScrubAuditStore(ScrubAuditStore):
    """Process-local dict store (the offline profile; a restart drops it)."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def _insert(self, row: dict[str, Any]) -> None:
        self._rows.append(dict(row))

    def _scan(self, tenant: str | None, experience_id: str | None) -> list[dict[str, Any]]:
        if not tenant:
            return []
        rows = [dict(r) for r in self._rows if r.get("tenant_id") == tenant]
        if experience_id is not None:
            rows = [r for r in rows if r.get("experience_id") == experience_id]
        rows.sort(key=lambda r: r.get("created_at") or _now(), reverse=True)
        return rows


# --------------------------------------------------------------------------- #
# PostgreSQL backend (real ``scrub_audits`` table)                             #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    """Strip the SQLAlchemy driver suffix so ``psycopg`` can parse the DSN."""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


class PostgresScrubAuditStore(ScrubAuditStore):
    """The real ``scrub_audits`` table (migration ``026``) via ``psycopg``."""

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    def _insert(self, row: dict[str, Any]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scrub_audits
                    (id, tenant_id, experience_id, from_version, to_version,
                     status, categories, match_count, changed, content_sha256,
                     created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                """,
                (
                    str(row["id"]),
                    str(row["tenant_id"]),
                    str(row["experience_id"]),
                    row.get("from_version"),
                    str(row["to_version"]),
                    str(row["status"]),
                    json.dumps(list(row.get("categories") or [])),
                    int(row.get("match_count") or 0),
                    bool(row.get("changed")),
                    row.get("content_sha256"),
                    row.get("created_at") or _now(),
                ),
            )
            conn.commit()

    def _scan(self, tenant: str | None, experience_id: str | None) -> list[dict[str, Any]]:
        if not tenant:
            return []
        sql = (
            "SELECT id, tenant_id, experience_id, from_version, to_version, status, "
            "categories, match_count, changed, content_sha256, created_at "
            "FROM scrub_audits WHERE tenant_id = %s"
        )
        params: list[Any] = [tenant]
        if experience_id is not None:
            sql += " AND experience_id = %s"
            params.append(str(experience_id))
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        names = (
            "id", "tenant_id", "experience_id", "from_version", "to_version",
            "status", "categories", "match_count", "changed", "content_sha256",
            "created_at",
        )
        return [dict(zip(names, r)) for r in rows]


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_STORE: ScrubAuditStore | None = None


def get_scrub_audit_store() -> ScrubAuditStore:
    """Process-wide store: PG when a DSN is configured, else in-memory."""
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    _STORE = PostgresScrubAuditStore(dsn) if dsn else InMemoryScrubAuditStore()
    return _STORE


def set_scrub_audit_store(store: ScrubAuditStore | None) -> None:
    """Test helper — pin an explicit store."""
    global _STORE
    _STORE = store


def reset_scrub_audit_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None
