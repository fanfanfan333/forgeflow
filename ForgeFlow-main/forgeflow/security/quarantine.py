"""INC46 T33 — 经验隔离区（quarantine）。

红线 14 的**第三道**防线，也是规格 ④「被标记的 Experience 进 quarantine，人工
复核后才可入 Miner」的落点。

工作方式
--------
写路径（:mod:`forgeflow.repositories.memory.experience_repo` /
``postgres.experience_repo``）在 ``scrub_in_place``（T32 脱敏）之后调用
:func:`inspect_and_quarantine`：命中指令性文本（见
:mod:`forgeflow.security.injection_detector`）的 Experience **不写入**
``experiences`` 表，而是整条塞进 ``experience_quarantine``（迁移 ``030``）。
因此污染的文本既不入经验 / 记忆，也天然不进 Miner（Miner 读的是 ``experiences``）。

人工复核后调用 :func:`release_quarantine` 放行：隔离行被标记 ``released``，其
完整 payload 被重新写回 ``experiences``（此时才可能被 Miner 读到）。**隔离区的
存在本身不阻断正常编辑** —— 干净内容照常落库。

最小化（同 ``privacy.audit`` 的溯源纪律）
------------------------------------------
隔离行**不保存**命中的敏感原文，只保存：租户、experience id、run id、状态、
命中标签与**片段**（excerpt，最多 48 字）、一个内容 ``sha256``，以及放行所需的
Experience payload。``schema`` 的 ``findings`` 只含标签 / 片段，不含整篇文档。

Tenant discipline (红线 5): ``tenant_id`` 是每个读写的**第一个位置参数**；未解析
租户读空集、拒绝写入（fail-closed）。无 ``0`` / ``''`` 冒充（红线 4）。

双后端沿用 ``privacy.audit`` / ``hitl.pending`` 的同构：进程内 dict store（离线档）
+ ``psycopg`` 同步 store（真 ``experience_quarantine`` 表）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from forgeflow.repositories.base import new_id, utcnow
from forgeflow.security.injection_detector import (
    INJECTION_VERSION,
    InjectionReport,
    detect_experience_injection,
)

logger = logging.getLogger(__name__)

__all__ = [
    "QUARANTINE_STATUS_QUARANTINED",
    "QUARANTINE_STATUS_RELEASED",
    "QUARANTINE_STATUSES",
    "QuarantineError",
    "QuarantineEntry",
    "QuarantineDecision",
    "QuarantineStore",
    "InMemoryQuarantineStore",
    "PostgresQuarantineStore",
    "get_quarantine_store",
    "set_quarantine_store",
    "reset_quarantine_store",
    "inspect_and_quarantine",
    "is_quarantined",
    "quarantined_ids",
    "list_quarantine",
    "release_quarantine",
]

QUARANTINE_STATUS_QUARANTINED = "quarantined"
QUARANTINE_STATUS_RELEASED = "released"
QUARANTINE_STATUSES: tuple[str, ...] = (
    QUARANTINE_STATUS_QUARANTINED,
    QUARANTINE_STATUS_RELEASED,
)


def _now() -> datetime:
    """Timezone-aware UTC now (one clock, mirrors the repositories base)."""
    return datetime.now(timezone.utc)


class QuarantineError(Exception):
    """A quarantine write could not be made safely (e.g. no tenant — 红线 5).

    Fail-closed: the caller must **refuse** to persist the flagged experience
    rather than let the polluted content through unquarantined.
    """


@dataclass
class QuarantineEntry:
    """One quarantined Experience (minimised provenance + release payload)."""

    tenant_id: str
    experience_id: str
    run_id: str = ""
    status: str = QUARANTINE_STATUS_QUARANTINED
    reason: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    content_sha256: str | None = None
    detector_version: str = INJECTION_VERSION
    id: str = field(default_factory=new_id)
    created_at: datetime = field(default_factory=utcnow)
    released_at: datetime | None = None
    released_by: str = ""

    @property
    def released(self) -> bool:
        return self.status == QUARANTINE_STATUS_RELEASED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "experience_id": self.experience_id,
            "run_id": self.run_id,
            "status": self.status,
            "reason": self.reason,
            # Only tags / excerpts — never the raw flagged text (红线 14 / 最小化).
            "findings": [dict(f) for f in self.findings],
            "content_sha256": self.content_sha256,
            "detector_version": self.detector_version,
            "created_at": self.created_at.isoformat(),
            "released_at": self.released_at.isoformat() if self.released_at else None,
            "released_by": self.released_by,
        }


@dataclass
class QuarantineDecision:
    """The outcome of the write-path guard."""

    quarantined: bool
    entry: QuarantineEntry | None = None
    report: InjectionReport | None = None


# --------------------------------------------------------------------------- #
# Store — shared logic                                                         #
# --------------------------------------------------------------------------- #
class QuarantineStore:
    """Tenant-scoped store for :class:`QuarantineEntry` rows (upsert by exp id)."""

    # --- storage primitives (per backend) ----------------------------------- #
    def _upsert(self, row: dict[str, Any]) -> None:
        raise NotImplementedError

    def _scan(self, tenant: str | None, status: str | None) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _get(self, tenant: str | None, entry_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def _mark_released(
        self, tenant: str | None, entry_id: str, released_at: datetime, released_by: str
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    # --- writes ------------------------------------------------------------- #
    def record(self, entry: QuarantineEntry) -> QuarantineEntry:
        """Persist one quarantine row (upsert by ``(tenant, experience_id)``).

        Raises:
            QuarantineError: when ``entry.tenant_id`` is falsy (an unscoped
                quarantine must never be written — 红线 5).
        """
        if not entry.tenant_id:
            raise QuarantineError(
                "tenant required to quarantine an experience (fail closed)"
            )
        self._upsert(
            {
                "id": entry.id,
                "tenant_id": str(entry.tenant_id),
                "experience_id": str(entry.experience_id),
                "run_id": entry.run_id or "",
                "status": entry.status,
                "reason": entry.reason,
                "findings": list(entry.findings),
                "payload": dict(entry.payload),
                "content_sha256": entry.content_sha256,
                "detector_version": entry.detector_version,
                "created_at": entry.created_at or _now(),
                "released_at": entry.released_at,
                "released_by": entry.released_by,
            }
        )
        return entry

    # --- reads -------------------------------------------------------------- #
    def list(self, tenant: str | None, *, status: str | None = None) -> list[QuarantineEntry]:
        """List a tenant's quarantine rows (newest first). Falsy tenant ⇒ ``[]``."""
        if not tenant:
            return []
        return [self._row_to_entry(row) for row in self._scan(tenant, status)]

    def get(self, tenant: str | None, entry_id: str) -> QuarantineEntry | None:
        """Fetch one row, tenant-scoped. Falsy tenant / cross-tenant ⇒ ``None``."""
        if not tenant:
            return None
        row = self._get(tenant, entry_id)
        return self._row_to_entry(row) if row is not None else None

    def mark_released(
        self, tenant: str | None, entry_id: str, *, released_by: str = ""
    ) -> QuarantineEntry | None:
        """Flip one row to ``released`` (tenant-scoped). ``None`` when absent."""
        if not tenant:
            return None
        row = self._mark_released(tenant, entry_id, _now(), released_by or "")
        return self._row_to_entry(row) if row is not None else None

    @staticmethod
    def _row_to_entry(row: dict[str, Any]) -> QuarantineEntry:
        findings = row.get("findings")
        if isinstance(findings, str):
            try:
                findings = json.loads(findings)
            except (TypeError, ValueError):
                findings = []
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        return QuarantineEntry(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            experience_id=str(row["experience_id"]),
            run_id=str(row.get("run_id") or ""),
            status=str(row.get("status") or QUARANTINE_STATUS_QUARANTINED),
            reason=str(row.get("reason") or ""),
            findings=list(findings or []),
            payload=dict(payload or {}),
            content_sha256=row.get("content_sha256"),
            detector_version=str(row.get("detector_version") or INJECTION_VERSION),
            created_at=row.get("created_at") or _now(),
            released_at=row.get("released_at"),
            released_by=str(row.get("released_by") or ""),
        )


# --------------------------------------------------------------------------- #
# In-memory backend (offline profile)                                          #
# --------------------------------------------------------------------------- #
class InMemoryQuarantineStore(QuarantineStore):
    """Process-local dict store (the offline profile; a restart drops it)."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def _upsert(self, row: dict[str, Any]) -> None:
        # Upsert by (tenant, experience_id): a re-quarantine replaces the row.
        self._rows = [
            r
            for r in self._rows
            if not (
                r.get("tenant_id") == row["tenant_id"]
                and r.get("experience_id") == row["experience_id"]
            )
        ]
        self._rows.append(dict(row))

    def _scan(self, tenant: str | None, status: str | None) -> list[dict[str, Any]]:
        if not tenant:
            return []
        rows = [dict(r) for r in self._rows if r.get("tenant_id") == tenant]
        if status is not None:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: r.get("created_at") or _now(), reverse=True)
        return rows

    def _get(self, tenant: str | None, entry_id: str) -> dict[str, Any] | None:
        if not tenant:
            return None
        for row in self._rows:
            if row.get("tenant_id") == tenant and str(row.get("id")) == entry_id:
                return dict(row)
        return None

    def _mark_released(
        self, tenant: str | None, entry_id: str, released_at: datetime, released_by: str
    ) -> dict[str, Any] | None:
        if not tenant:
            return None
        for row in self._rows:
            if row.get("tenant_id") == tenant and str(row.get("id")) == entry_id:
                row["status"] = QUARANTINE_STATUS_RELEASED
                row["released_at"] = released_at
                row["released_by"] = released_by
                return dict(row)
        return None


# --------------------------------------------------------------------------- #
# PostgreSQL backend (real ``experience_quarantine`` table)                    #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    """Strip the SQLAlchemy driver suffix so ``psycopg`` can parse the DSN."""
    return dsn.replace("postgresql+psycopg://", "postgresql://")


class PostgresQuarantineStore(QuarantineStore):
    """The real ``experience_quarantine`` table (migration ``030``) via ``psycopg``."""

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    def _upsert(self, row: dict[str, Any]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO experience_quarantine
                    (id, tenant_id, experience_id, run_id, status, reason,
                     findings, payload, content_sha256, detector_version,
                     created_at, released_at, released_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s,
                        %s, %s, %s)
                ON CONFLICT (tenant_id, experience_id) DO UPDATE SET
                    id = EXCLUDED.id,
                    run_id = EXCLUDED.run_id,
                    status = EXCLUDED.status,
                    reason = EXCLUDED.reason,
                    findings = EXCLUDED.findings,
                    payload = EXCLUDED.payload,
                    content_sha256 = EXCLUDED.content_sha256,
                    detector_version = EXCLUDED.detector_version,
                    created_at = EXCLUDED.created_at,
                    released_at = EXCLUDED.released_at,
                    released_by = EXCLUDED.released_by
                """,
                (
                    str(row["id"]),
                    str(row["tenant_id"]),
                    str(row["experience_id"]),
                    row.get("run_id") or "",
                    str(row["status"]),
                    str(row.get("reason") or ""),
                    json.dumps(list(row.get("findings") or [])),
                    json.dumps(dict(row.get("payload") or {})),
                    row.get("content_sha256"),
                    str(row.get("detector_version") or INJECTION_VERSION),
                    row.get("created_at") or _now(),
                    row.get("released_at"),
                    str(row.get("released_by") or ""),
                ),
            )
            conn.commit()

    def _scan(self, tenant: str | None, status: str | None) -> list[dict[str, Any]]:
        if not tenant:
            return []
        sql = (
            "SELECT id, tenant_id, experience_id, run_id, status, reason, findings, "
            "payload, content_sha256, detector_version, created_at, released_at, "
            "released_by FROM experience_quarantine WHERE tenant_id = %s"
        )
        params: list[Any] = [tenant]
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        names = (
            "id", "tenant_id", "experience_id", "run_id", "status", "reason",
            "findings", "payload", "content_sha256", "detector_version",
            "created_at", "released_at", "released_by",
        )
        return [dict(zip(names, r)) for r in rows]

    def _get(self, tenant: str | None, entry_id: str) -> dict[str, Any] | None:
        if not tenant:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, tenant_id, experience_id, run_id, status, reason, findings, "
                "payload, content_sha256, detector_version, created_at, released_at, "
                "released_by FROM experience_quarantine "
                "WHERE tenant_id = %s AND id = %s",
                (tenant, entry_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        names = (
            "id", "tenant_id", "experience_id", "run_id", "status", "reason",
            "findings", "payload", "content_sha256", "detector_version",
            "created_at", "released_at", "released_by",
        )
        return dict(zip(names, row))

    def _mark_released(
        self, tenant: str | None, entry_id: str, released_at: datetime, released_by: str
    ) -> dict[str, Any] | None:
        if not tenant:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE experience_quarantine SET status = %s, released_at = %s, "
                "released_by = %s WHERE tenant_id = %s AND id = %s",
                (QUARANTINE_STATUS_RELEASED, released_at, released_by, tenant, entry_id),
            )
            conn.commit()
        return self._get(tenant, entry_id)


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_STORE: QuarantineStore | None = None


def get_quarantine_store() -> QuarantineStore:
    """Process-wide store: PG when a DSN is configured, else in-memory."""
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    _STORE = PostgresQuarantineStore(dsn) if dsn else InMemoryQuarantineStore()
    return _STORE


def set_quarantine_store(store: QuarantineStore | None) -> None:
    """Test helper — pin an explicit store."""
    global _STORE
    _STORE = store


def reset_quarantine_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None


# --------------------------------------------------------------------------- #
# Write-path guard + review surface                                            #
# --------------------------------------------------------------------------- #
def _content_sha256(record: Any) -> str:
    """SHA-256 over the experience's free-text fields (provenance, not the bytes)."""
    parts: list[str] = []
    summary = getattr(record, "summary", "") or ""
    if summary:
        parts.append(str(summary))
    for step in getattr(record, "reusable_steps", None) or []:
        if isinstance(step, dict) and isinstance(step.get("note"), str):
            parts.append(step["note"])
    for decision in getattr(record, "decisions", None) or []:
        if isinstance(decision, dict):
            for key in ("reason", "detail", "decision", "note", "text"):
                value = decision.get(key)
                if isinstance(value, str) and value:
                    parts.append(value)
        elif isinstance(decision, str):
            parts.append(decision)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


#: Transient attestation set by :func:`release_quarantine` on the record it is
#: re-admitting. It is deliberately a private, code-set attribute (never a wire
#: field, never round-tripped) so the ONLY way a flagged record re-enters
#: ``experiences`` is through the admin-only release path — a caller cannot
#: forge it via ``ExperienceRecord.from_dict`` (``asdict`` never emits it).
_RELEASE_ATTESTATION = "_fg_quarantine_released"


def inspect_and_quarantine(record: Any, *, store: QuarantineStore | None = None) -> QuarantineDecision:
    """Write-path guard: detect instruction text in ``record`` and quarantine it.

    Returns a :class:`QuarantineDecision`. When the record is flagged, the whole
    Experience is stored in the quarantine (its free-text never reaches
    ``experiences`` / the miner) and ``quarantined=True``. A clean record returns
    ``quarantined=False`` and is persisted by the caller as usual.

    A record carrying the :data:`_RELEASE_ATTESTATION` (set only by
    :func:`release_quarantine` after a human review) bypasses the guard so the
    release write itself is not re-quarantined.

    Raises:
        QuarantineError: a flagged record with no tenant cannot be quarantined
            safely — fail-closed rather than persist it unquarantined (红线 5).
    """
    if getattr(record, _RELEASE_ATTESTATION, False):
        return QuarantineDecision(quarantined=False, report=None)

    report = detect_experience_injection(record)
    if not report.flagged:
        return QuarantineDecision(quarantined=False, report=report)

    tenant = getattr(record, "tenant_id", None)
    if not tenant:
        raise QuarantineError(
            "flagged experience has no tenant_id — refusing an unquarantined write "
            "(fail closed, 红线 5)"
        )

    entry = QuarantineEntry(
        tenant_id=str(tenant),
        experience_id=str(getattr(record, "id", "") or ""),
        run_id=str(getattr(record, "run_id", "") or ""),
        reason=",".join(report.reasons) or "injection_detected",
        findings=[f.to_dict() for f in report.findings],
        payload=record.to_dict() if hasattr(record, "to_dict") else {},
        content_sha256=_content_sha256(record),
    )
    (store or get_quarantine_store()).record(entry)
    logger.warning(
        "experience quarantined | tenant=%s experience=%s reasons=%s",
        entry.tenant_id,
        entry.experience_id,
        entry.reason,
    )
    return QuarantineDecision(quarantined=True, entry=entry, report=report)


def is_quarantined(
    tenant: str | None, experience_id: str, *, store: QuarantineStore | None = None
) -> bool:
    """Whether ``experience_id`` currently sits in the tenant's quarantine."""
    if not tenant:
        return False
    store = store or get_quarantine_store()
    for row in store.list(tenant, status=QUARANTINE_STATUS_QUARANTINED):
        if row.experience_id == experience_id:
            return True
    return False


def quarantined_ids(tenant: str | None, *, store: QuarantineStore | None = None) -> set[str]:
    """The set of still-quarantined experience ids for a tenant (falsy ⇒ empty)."""
    if not tenant:
        return set()
    store = store or get_quarantine_store()
    return {
        row.experience_id
        for row in store.list(tenant, status=QUARANTINE_STATUS_QUARANTINED)
    }


def list_quarantine(
    tenant: str | None,
    *,
    status: str | None = None,
    store: QuarantineStore | None = None,
) -> list[QuarantineEntry]:
    """List a tenant's quarantine rows (newest first). Falsy tenant ⇒ ``[]``."""
    if not tenant:
        return []
    return (store or get_quarantine_store()).list(tenant, status=status)


async def release_quarantine(
    tenant: str | None,
    entry_id: str,
    *,
    released_by: str = "",
    store: QuarantineStore | None = None,
    repo: Any | None = None,
) -> QuarantineEntry | None:
    """Human-review release: mark the row ``released`` and re-admit the Experience.

    Tenant fail-closed: an unresolved tenant reads / writes nothing (``None``).
    Cross-tenant / unknown id ⇒ ``None`` (no existence leak). The stored payload
    is re-persisted into the ``experiences`` store so the record only becomes
    miner-visible **after** a human release (规格 ④).

    Idempotent: releasing an already-released row returns it without a second
    write of the experience.
    """
    if not tenant:
        return None
    store = store or get_quarantine_store()
    existing = store.get(tenant, entry_id)
    if existing is None:
        return None
    if existing.released:
        return existing

    released = store.mark_released(tenant, entry_id, released_by=released_by)
    if released is None:
        return None

    payload = dict(released.payload or {})
    if payload:
        from forgeflow.experience.models import ExperienceRecord

        if repo is None:
            from forgeflow.repositories import get_experience_repository

            repo = get_experience_repository()
        record = ExperienceRecord.from_dict(payload)
        # Keep the release honest: the experience is admitted under its own
        # tenant only (never a cross-tenant bucket — 红线 5).
        record.tenant_id = str(tenant)
        # Attest the human review so the guarded write path does not immediately
        # re-quarantine the very record we are admitting (see the sentinel note).
        setattr(record, _RELEASE_ATTESTATION, True)
        await repo.save(record)
    return released
