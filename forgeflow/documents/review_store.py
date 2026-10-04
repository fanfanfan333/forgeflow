"""INC46 T22 — 文档产物的版本 + 评审存储（迁移 028）。

Why this module exists（书面差异）
--------------------------------
任务书把 T22 的代码落点写成 ``documents/diff.py`` / ``documents/tracked_changes.py``
/ ``api/routers/artifact_review.py``，并把落库写成「``artifact_reviews``；``artifact_versions``
增加 ``state`` 列」。经核对仓库实际结构：本仓此前**没有** ``artifact_versions`` 表，
也没有任何「文档产物版本 / 评审」的持久层。要真正实现「先产出 ``pending`` 版本 + diff，
确认后才 ``committed``」这条状态机，必须有一个持久层，而它不属于「纯 diff 引擎」或
「OOXML 生成器」。故新增本模块 —— 与 :mod:`forgeflow.hitl.pending`（T21 store）/
``api/routers/pending_actions.py``（T21 router）的既有分层一致。这是**声明的加性差异**。

状态机（任务书规格）
--------------------
``pending`` → ``approved``(版本 ``committed``) | ``rejected`` | ``expired``。

* ``reject`` 可附 ``reason``，原因写入 T16 的 ``feedback_events``；
* 对**非 pending** 版本 ``approve`` ⇒ :class:`VersionNotPending`（路由层 409）；
* ``out_of_region``（区间外变化）非空 ⇒ :class:`OutOfRegionBlocked`（阻断 approve），
  除非显式 ``override``（且记录 ``override_reason``）。

纪律
----
* 红线 5：``tenant`` 是每个读写方法的**第一个位置参数**；falsy 租户读为空集、拒绝写。
* 红线 4：未测量 / 未发生 ⇒ ``None`` / ``{}``，绝不写 ``0`` / ``""``。
* 红线 11：未经 approve **绝不**产生 ``committed`` 版本。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forgeflow.documents.store import DocArtifactStore
from forgeflow.repositories.base import new_id

logger = logging.getLogger(__name__)

__all__ = [
    # version states
    "PENDING",
    "COMMITTED",
    "REJECTED",
    "EXPIRED",
    "VERSION_STATES",
    # review statuses
    "REVIEW_PENDING",
    "REVIEW_APPROVED",
    "REVIEW_REJECTED",
    "REVIEW_EXPIRED",
    "REVIEW_STATUSES",
    # records + errors
    "ArtifactVersion",
    "ArtifactReview",
    "ArtifactReviewError",
    "ArtifactNotFound",
    "CrossTenantArtifact",
    "ReviewNotFound",
    "VersionNotPending",
    "OutOfRegionBlocked",
    # stores
    "ArtifactReviewStore",
    "InMemoryArtifactReviewStore",
    "PostgresArtifactReviewStore",
    "get_artifact_review_store",
    "set_artifact_review_store",
    "reset_artifact_review_store",
]

#: Version ``state`` vocabulary (迁移 028）。``committed`` is the historical default.
PENDING = "pending"
COMMITTED = "committed"
REJECTED = "rejected"
EXPIRED = "expired"
VERSION_STATES: tuple[str, ...] = (PENDING, COMMITTED, REJECTED, EXPIRED)

#: Review ``status`` vocabulary.
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"
REVIEW_EXPIRED = "expired"
REVIEW_STATUSES: tuple[str, ...] = (REVIEW_PENDING, REVIEW_APPROVED, REVIEW_REJECTED, REVIEW_EXPIRED)

#: T16 feedback kind a reject writes.
FEEDBACK_KIND_REJECTED = "REJECTED"

_FORMAT_SUFFIX: dict[str, str] = {
    "docx": ".docx",
    "pptx": ".pptx",
    "xlsx": ".xlsx",
    "text": ".txt",
    "pdf": ".pdf",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_json(value: Any) -> Any:
    """Decode a JSONB column that may arrive as an object or a JSON string."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


# --------------------------------------------------------------------------- #
# Records                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class ArtifactVersion:
    """One persisted version of a document artifact."""

    artifact_id: str
    version: int
    state: str = PENDING
    tenant_id: str | None = None
    base_version: int | None = None
    content_ref: str | None = None
    content_sha256: str | None = None
    format: str = "docx"
    diff: dict[str, Any] | None = None
    out_of_region: list[dict[str, Any]] | None = None
    review_id: str | None = None
    run_id: str | None = None
    created_at: datetime | None = None
    committed_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.state == PENDING

    @property
    def is_committed(self) -> bool:
        return self.state == COMMITTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "version": self.version,
            "state": self.state,
            "tenant_id": self.tenant_id,
            "base_version": self.base_version,
            "content_ref": self.content_ref,
            "content_sha256": self.content_sha256,
            "format": self.format,
            "diff": self.diff,
            "out_of_region": self.out_of_region,
            "review_id": self.review_id,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "committed_at": self.committed_at.isoformat() if self.committed_at else None,
        }


@dataclass
class ArtifactReview:
    """One "pending human confirmation" review record (public ``approval_id``)."""

    approval_id: str
    artifact_id: str
    version: int
    status: str = REVIEW_PENDING
    tenant_id: str | None = None
    run_id: str | None = None
    diff: dict[str, Any] | None = None
    out_of_region: list[dict[str, Any]] | None = None
    reason: str | None = None
    override: bool | None = None
    override_reason: str | None = None
    expires_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    created_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == REVIEW_PENDING

    @property
    def blocked_by_out_of_region(self) -> bool:
        """True iff a measured, non-empty out-of-region change set exists."""
        return bool(self.out_of_region)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "artifact_id": self.artifact_id,
            "version": self.version,
            "status": self.status,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "diff": self.diff,
            "out_of_region": self.out_of_region,
            "reason": self.reason,
            "override": self.override,
            "override_reason": self.override_reason,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #
class ArtifactReviewError(Exception):
    """Base class for every artifact-review failure (⇒ mapped to an HTTP status)."""


class ArtifactNotFound(ArtifactReviewError):
    """No such version for this tenant (⇒ 404)."""


class CrossTenantArtifact(ArtifactReviewError):
    """The artifact belongs to another tenant (⇒ 403). Never a shadow write."""


class ReviewNotFound(ArtifactReviewError):
    """No such review for this tenant (⇒ 404)."""


class VersionNotPending(ArtifactReviewError):
    """Approve/reject a version that is not ``pending`` ⇒ 409 (红线 11)."""

    def __init__(self, artifact_id: str, version: int, state: str) -> None:
        super().__init__(f"version {artifact_id}@v{version} is {state}, not pending")
        self.artifact_id = artifact_id
        self.version = version
        self.state = state


class OutOfRegionBlocked(ArtifactReviewError):
    """Out-of-region changes block approve (needs an explicit override) ⇒ 409."""

    def __init__(self, artifact_id: str, version: int, changes: list[dict[str, Any]]) -> None:
        super().__init__(
            f"version {artifact_id}@v{version} changes {len(changes)} region(s) outside the target"
        )
        self.artifact_id = artifact_id
        self.version = version
        self.changes = changes


# --------------------------------------------------------------------------- #
# Store — shared logic (template methods over per-backend primitives)          #
# --------------------------------------------------------------------------- #
class ArtifactReviewStore:
    """Tenant-scoped store for :class:`ArtifactVersion` / :class:`ArtifactReview`.

    Subclasses implement the storage primitives over canonical *row* dicts.
    All *behavioural* rules (fail-closed tenant, state machine, out-of-region
    block, idempotent-safe transitions) live here so both backends agree.
    """

    def __init__(self, *, blob_store: DocArtifactStore | None = None) -> None:
        self._blob_store = blob_store

    # --- storage primitives (implemented per backend) ---------------------- #
    def _load_version(self, tenant: str | None, artifact_id: str, version: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def _load_version_any(self, artifact_id: str, version: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def _scan_versions(self, tenant: str | None, artifact_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _save_version(self, row: dict[str, Any]) -> None:
        raise NotImplementedError

    def _load_review(self, tenant: str | None, approval_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def _save_review(self, row: dict[str, Any]) -> None:
        raise NotImplementedError

    # --- blob store -------------------------------------------------------- #
    def blob_store(self) -> DocArtifactStore:
        if self._blob_store is None:
            self._blob_store = DocArtifactStore()
        return self._blob_store

    # --- row ⇄ record ------------------------------------------------------ #
    @staticmethod
    def _row_to_version(row: dict[str, Any]) -> ArtifactVersion:
        return ArtifactVersion(
            artifact_id=str(row["artifact_id"]),
            version=int(row["version"]),
            state=str(row.get("state") or COMMITTED),
            tenant_id=(str(row["tenant_id"]) if row.get("tenant_id") else None),
            base_version=row.get("base_version"),
            content_ref=row.get("content_ref"),
            content_sha256=row.get("content_sha256"),
            format=str(row.get("format") or "docx"),
            diff=_coerce_json(row.get("diff")),
            out_of_region=_coerce_json(row.get("out_of_region")),
            review_id=row.get("review_id"),
            run_id=row.get("run_id"),
            created_at=row.get("created_at"),
            committed_at=row.get("committed_at"),
        )

    @staticmethod
    def _row_to_review(row: dict[str, Any]) -> ArtifactReview:
        return ArtifactReview(
            approval_id=str(row["approval_id"]),
            artifact_id=str(row["artifact_id"]),
            version=int(row["version"]),
            status=str(row.get("status") or REVIEW_PENDING),
            tenant_id=(str(row["tenant_id"]) if row.get("tenant_id") else None),
            run_id=row.get("run_id"),
            diff=_coerce_json(row.get("diff")),
            out_of_region=_coerce_json(row.get("out_of_region")),
            reason=row.get("reason"),
            override=row.get("override"),
            override_reason=row.get("override_reason"),
            expires_at=row.get("expires_at"),
            resolved_at=row.get("resolved_at"),
            resolved_by=row.get("resolved_by"),
            created_at=row.get("created_at"),
        )

    # --- writes ------------------------------------------------------------ #
    def create_pending_version(
        self,
        tenant: str | None,
        *,
        artifact_id: str,
        content: bytes,
        fmt: str = "docx",
        diff: dict[str, Any] | None = None,
        out_of_region: list[dict[str, Any]] | None = None,
        base_version: int | None = None,
        run_id: str | None = None,
        approval_id: str | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> tuple[ArtifactVersion, ArtifactReview]:
        """Persist a ``pending`` version + its review; return both.

        Raises:
            ValueError: falsy tenant (unscoped write is forbidden — 红线 5) or empty content.
        """
        if not tenant:
            raise ValueError("tenant required to create an artifact version (fail closed)")
        if not content:
            raise ValueError("artifact content must not be empty")

        moment = now or _now()
        suffix = _FORMAT_SUFFIX.get(fmt, ".bin")
        content_ref = self.blob_store().put(content, suffix=suffix)
        import hashlib

        digest = hashlib.sha256(bytes(content)).hexdigest()
        version = self._next_version(tenant, artifact_id)
        approval = approval_id or f"ap-{new_id()}"

        version_row = {
            "tenant_id": tenant,
            "artifact_id": artifact_id,
            "version": version,
            "state": PENDING,
            "base_version": base_version,
            "content_ref": content_ref,
            "content_sha256": digest,
            "format": fmt,
            "diff": diff,
            "out_of_region": out_of_region,
            "review_id": approval,
            "run_id": run_id,
            "created_at": moment,
            "committed_at": None,
        }
        review_row = {
            "tenant_id": tenant,
            "approval_id": approval,
            "artifact_id": artifact_id,
            "version": version,
            "run_id": run_id,
            "status": REVIEW_PENDING,
            "diff": diff,
            "out_of_region": out_of_region,
            "reason": None,
            "override": None,
            "override_reason": None,
            "expires_at": expires_at,
            "resolved_at": None,
            "resolved_by": None,
            "created_at": moment,
        }
        self._save_version(version_row)
        self._save_review(review_row)
        logger.info(
            "pending artifact version created | tenant=%s artifact=%s v=%d approval=%s",
            tenant, artifact_id, version, approval,
        )
        return self._row_to_version(version_row), self._row_to_review(review_row)

    def approve(
        self,
        tenant: str | None,
        artifact_id: str,
        version: int,
        *,
        actor: str | None = None,
        override: bool = False,
        override_reason: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ArtifactVersion, ArtifactReview]:
        """Commit a ``pending`` version (after a human approves its diff).

        Raises:
            ArtifactNotFound / CrossTenantArtifact (404 / 403)
            VersionNotPending (409) — not a pending version.
            OutOfRegionBlocked (409) — out-of-region changes without an override.
        """
        row = self._require_pending(tenant, artifact_id, version)
        out = _coerce_json(row.get("out_of_region"))
        if out and not override:
            raise OutOfRegionBlocked(artifact_id, version, out)

        moment = now or _now()
        row["state"] = COMMITTED
        row["committed_at"] = moment
        self._save_version(row)

        review_row = self._load_review(tenant, str(row.get("review_id") or "")) or {}
        if review_row:
            review_row["status"] = REVIEW_APPROVED
            review_row["override"] = bool(override) if out else None
            review_row["override_reason"] = override_reason if (out and override) else None
            review_row["resolved_at"] = moment
            review_row["resolved_by"] = actor
            self._save_review(review_row)

        version_record = self._row_to_version(row)
        review_record = (
            self._row_to_review(review_row)
            if review_row
            else ArtifactReview(
                approval_id=str(row.get("review_id") or ""),
                artifact_id=artifact_id,
                version=version,
                status=REVIEW_APPROVED,
                tenant_id=tenant,
                resolved_at=moment,
                resolved_by=actor,
            )
        )
        logger.info(
            "artifact version approved | tenant=%s artifact=%s v=%d by=%s override=%s",
            tenant, artifact_id, version, actor, bool(override and out),
        )
        return version_record, review_record

    def reject(
        self,
        tenant: str | None,
        artifact_id: str,
        version: int,
        *,
        actor: str | None = None,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ArtifactVersion, ArtifactReview]:
        """Reject a ``pending`` version; the reason (if any) enters T16 feedback."""
        row = self._require_pending(tenant, artifact_id, version)
        moment = now or _now()
        row["state"] = REJECTED
        self._save_version(row)

        review_row = self._load_review(tenant, str(row.get("review_id") or "")) or {}
        if review_row:
            review_row["status"] = REVIEW_REJECTED
            review_row["reason"] = reason
            review_row["resolved_at"] = moment
            review_row["resolved_by"] = actor
            self._save_review(review_row)

        version_record = self._row_to_version(row)
        review_record = (
            self._row_to_review(review_row)
            if review_row
            else ArtifactReview(
                approval_id=str(row.get("review_id") or ""),
                artifact_id=artifact_id,
                version=version,
                status=REVIEW_REJECTED,
                tenant_id=tenant,
                reason=reason,
                resolved_at=moment,
                resolved_by=actor,
            )
        )
        self._record_reject_feedback(tenant, version_record, reason, actor)
        logger.info(
            "artifact version rejected | tenant=%s artifact=%s v=%d by=%s",
            tenant, artifact_id, version, actor,
        )
        return version_record, review_record

    def expire_overdue(self, tenant: str | None, *, now: datetime | None = None) -> list[ArtifactReview]:
        """Flip overdue ``pending`` reviews (and their versions) to ``expired``.

        Returns the reviews that were flipped. Tenant scoped (falsy ⇒ empty list).
        """
        if not tenant:
            return []
        moment = now or _now()
        flipped: list[ArtifactReview] = []
        for row in self._scan_versions(tenant, ""):
            review_row = self._load_review(tenant, str(row.get("review_id") or ""))
            if not review_row or review_row.get("status") != REVIEW_PENDING:
                continue
            expires_at = review_row.get("expires_at")
            if isinstance(expires_at, str):
                expires_at = _parse_dt(expires_at)
            if expires_at is None or moment < expires_at:
                continue
            review_row["status"] = REVIEW_EXPIRED
            review_row["resolved_at"] = moment
            self._save_review(review_row)
            row["state"] = EXPIRED
            self._save_version(row)
            flipped.append(self._row_to_review(review_row))
        return flipped

    # --- reads ------------------------------------------------------------- #
    def get_version(
        self, tenant: str | None, artifact_id: str, version: int
    ) -> ArtifactVersion | None:
        if not tenant:
            return None
        row = self._load_version(tenant, artifact_id, version)
        return self._row_to_version(row) if row else None

    def list_versions(self, tenant: str | None, artifact_id: str) -> list[ArtifactVersion]:
        if not tenant:
            return []
        return [self._row_to_version(r) for r in self._scan_versions(tenant, artifact_id)]

    def get_review(self, tenant: str | None, approval_id: str) -> ArtifactReview | None:
        if not tenant:
            return None
        row = self._load_review(tenant, approval_id)
        return self._row_to_review(row) if row else None

    def latest_committed(self, tenant: str | None, artifact_id: str) -> ArtifactVersion | None:
        committed = [v for v in self.list_versions(tenant, artifact_id) if v.state == COMMITTED]
        if not committed:
            return None
        return max(committed, key=lambda v: v.version)

    def read_content(self, tenant: str | None, artifact_id: str, version: int) -> bytes:
        """Read back the deliverable bytes for a version (tenant scoped)."""
        record = self.get_version(tenant, artifact_id, version)
        if record is None:
            if self._load_version_any(artifact_id, version) is not None:
                raise CrossTenantArtifact(artifact_id)
            raise ArtifactNotFound(f"{artifact_id}@v{version}")
        if not record.content_ref:
            raise ArtifactNotFound(f"{artifact_id}@v{version} has no content")
        return self.blob_store().get(record.content_ref)

    # --- helpers ----------------------------------------------------------- #
    def _next_version(self, tenant: str, artifact_id: str) -> int:
        versions = [int(r["version"]) for r in self._scan_versions(tenant, artifact_id)]
        return (max(versions) + 1) if versions else 1

    def _require_pending(self, tenant: str | None, artifact_id: str, version: int) -> dict[str, Any]:
        if not tenant:
            raise ArtifactNotFound(f"{artifact_id}@v{version}")
        row = self._load_version(tenant, artifact_id, version)
        if row is None:
            if self._load_version_any(artifact_id, version) is not None:
                raise CrossTenantArtifact(artifact_id)
            raise ArtifactNotFound(f"{artifact_id}@v{version}")
        state = str(row.get("state") or COMMITTED)
        if state != PENDING:
            raise VersionNotPending(artifact_id, version, state)
        return row

    def _record_reject_feedback(
        self, tenant: str, version: ArtifactVersion, reason: str | None, actor: str | None
    ) -> None:
        """Write the reject reason into T16's feedback stream (best effort)."""
        run_id = version.run_id
        if not run_id:
            logger.info("reject has no run_id; reason kept on the review row only")
            return
        try:
            from forgeflow.outcomes.store import get_outcome_store

            store = get_outcome_store()
            store.record_feedback(
                tenant,
                run_id,
                FEEDBACK_KIND_REJECTED,
                actor=actor,
                idempotency_key=f"artifact-reject:{version.artifact_id}:v{version.version}",
                note=reason,
            )
        except Exception as exc:  # noqa: BLE001 — never let feedback break the reject
            logger.warning("reject feedback write skipped: %s", exc)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# In-memory backend (offline profile)                                          #
# --------------------------------------------------------------------------- #
class InMemoryArtifactReviewStore(ArtifactReviewStore):
    """Process-local dict store (offline profile). Mirrors the PG semantics."""

    def __init__(self, *, blob_store: DocArtifactStore | None = None) -> None:
        super().__init__(blob_store=blob_store)
        self._versions: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._reviews: dict[tuple[str, str], dict[str, Any]] = {}

    def _load_version(self, tenant: str | None, artifact_id: str, version: int) -> dict[str, Any] | None:
        if not tenant:
            return None
        row = self._versions.get((tenant, artifact_id, int(version)))
        return dict(row) if row is not None else None

    def _load_version_any(self, artifact_id: str, version: int) -> dict[str, Any] | None:
        for (_t, aid, v), row in self._versions.items():
            if aid == artifact_id and v == int(version):
                return dict(row)
        return None

    def _scan_versions(self, tenant: str | None, artifact_id: str) -> list[dict[str, Any]]:
        if not tenant:
            return []
        rows = [dict(r) for (t, _aid, _v), r in self._versions.items() if t == tenant]
        if artifact_id:
            rows = [r for r in rows if r.get("artifact_id") == artifact_id]
        rows.sort(key=lambda r: (r.get("artifact_id") or "", int(r["version"])))
        return rows

    def _save_version(self, row: dict[str, Any]) -> None:
        key = (str(row["tenant_id"]), str(row["artifact_id"]), int(row["version"]))
        self._versions[key] = dict(row)

    def _load_review(self, tenant: str | None, approval_id: str) -> dict[str, Any] | None:
        if not tenant or not approval_id:
            return None
        row = self._reviews.get((tenant, approval_id))
        return dict(row) if row is not None else None

    def _save_review(self, row: dict[str, Any]) -> None:
        key = (str(row["tenant_id"]), str(row["approval_id"]))
        self._reviews[key] = dict(row)


# --------------------------------------------------------------------------- #
# PostgreSQL backend (the real tables, migration 028)                          #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_VERSION_COLUMNS = (
    "tenant_id, artifact_id, version, state, base_version, content_ref, "
    "content_sha256, format, diff, out_of_region, review_id, run_id, created_at, committed_at"
)
_REVIEW_COLUMNS = (
    "tenant_id, approval_id, artifact_id, version, run_id, status, diff, out_of_region, "
    "reason, override, override_reason, expires_at, resolved_at, resolved_by, created_at"
)


class PostgresArtifactReviewStore(ArtifactReviewStore):
    """The real ``artifact_versions`` / ``artifact_reviews`` tables (migration 028)."""

    def __init__(self, dsn: str, *, blob_store: DocArtifactStore | None = None) -> None:
        super().__init__(blob_store=blob_store)
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    @staticmethod
    def _version_row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        names = (
            "tenant_id", "artifact_id", "version", "state", "base_version", "content_ref",
            "content_sha256", "format", "diff", "out_of_region", "review_id", "run_id",
            "created_at", "committed_at",
        )
        data = dict(zip(names, row))
        data["diff"] = _coerce_json(data.get("diff"))
        data["out_of_region"] = _coerce_json(data.get("out_of_region"))
        return data

    @staticmethod
    def _review_row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        names = (
            "tenant_id", "approval_id", "artifact_id", "version", "run_id", "status", "diff",
            "out_of_region", "reason", "override", "override_reason", "expires_at",
            "resolved_at", "resolved_by", "created_at",
        )
        data = dict(zip(names, row))
        data["diff"] = _coerce_json(data.get("diff"))
        data["out_of_region"] = _coerce_json(data.get("out_of_region"))
        return data

    def _load_version(self, tenant: str | None, artifact_id: str, version: int) -> dict[str, Any] | None:
        if not tenant:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_VERSION_COLUMNS} FROM artifact_versions "
                "WHERE tenant_id = %s AND artifact_id = %s AND version = %s",
                (tenant, artifact_id, int(version)),
            )
            return self._version_row(cur.fetchone())

    def _load_version_any(self, artifact_id: str, version: int) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_VERSION_COLUMNS} FROM artifact_versions "
                "WHERE artifact_id = %s AND version = %s LIMIT 1",
                (artifact_id, int(version)),
            )
            return self._version_row(cur.fetchone())

    def _scan_versions(self, tenant: str | None, artifact_id: str) -> list[dict[str, Any]]:
        if not tenant:
            return []
        sql = f"SELECT {_VERSION_COLUMNS} FROM artifact_versions WHERE tenant_id = %s"
        params: list[Any] = [tenant]
        if artifact_id:
            sql += " AND artifact_id = %s"
            params.append(artifact_id)
        sql += " ORDER BY artifact_id, version"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._version_row(r) for r in cur.fetchall()]

    def _save_version(self, row: dict[str, Any]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifact_versions
                    (tenant_id, artifact_id, version, state, base_version, content_ref,
                     content_sha256, format, diff, out_of_region, review_id, run_id,
                     created_at, committed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, artifact_id, version) DO UPDATE SET
                    state = EXCLUDED.state,
                    base_version = EXCLUDED.base_version,
                    content_ref = EXCLUDED.content_ref,
                    content_sha256 = EXCLUDED.content_sha256,
                    format = EXCLUDED.format,
                    diff = EXCLUDED.diff,
                    out_of_region = EXCLUDED.out_of_region,
                    review_id = EXCLUDED.review_id,
                    run_id = EXCLUDED.run_id,
                    committed_at = EXCLUDED.committed_at
                """,
                (
                    str(row["tenant_id"]),
                    str(row["artifact_id"]),
                    int(row["version"]),
                    str(row["state"]),
                    row.get("base_version"),
                    row.get("content_ref"),
                    row.get("content_sha256"),
                    str(row.get("format") or "docx"),
                    json.dumps(row.get("diff")) if row.get("diff") is not None else None,
                    json.dumps(row.get("out_of_region")) if row.get("out_of_region") is not None else None,
                    row.get("review_id"),
                    row.get("run_id"),
                    row.get("created_at") or _now(),
                    row.get("committed_at"),
                ),
            )
            conn.commit()

    def _load_review(self, tenant: str | None, approval_id: str) -> dict[str, Any] | None:
        if not tenant or not approval_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_REVIEW_COLUMNS} FROM artifact_reviews "
                "WHERE tenant_id = %s AND approval_id = %s",
                (tenant, approval_id),
            )
            return self._review_row(cur.fetchone())

    def _save_review(self, row: dict[str, Any]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifact_reviews
                    (tenant_id, approval_id, artifact_id, version, run_id, status, diff,
                     out_of_region, reason, override, override_reason, expires_at,
                     resolved_at, resolved_by, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, approval_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    diff = EXCLUDED.diff,
                    out_of_region = EXCLUDED.out_of_region,
                    reason = EXCLUDED.reason,
                    override = EXCLUDED.override,
                    override_reason = EXCLUDED.override_reason,
                    expires_at = EXCLUDED.expires_at,
                    resolved_at = EXCLUDED.resolved_at,
                    resolved_by = EXCLUDED.resolved_by
                """,
                (
                    str(row["tenant_id"]),
                    str(row["approval_id"]),
                    str(row["artifact_id"]),
                    int(row["version"]),
                    row.get("run_id"),
                    str(row["status"]),
                    json.dumps(row.get("diff")) if row.get("diff") is not None else None,
                    json.dumps(row.get("out_of_region")) if row.get("out_of_region") is not None else None,
                    row.get("reason"),
                    row.get("override"),
                    row.get("override_reason"),
                    row.get("expires_at"),
                    row.get("resolved_at"),
                    row.get("resolved_by"),
                    row.get("created_at") or _now(),
                ),
            )
            conn.commit()


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_STORE: Any = None


def get_artifact_review_store() -> ArtifactReviewStore:
    """Process-wide store: PG when a DSN is configured, else in-memory."""
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    if dsn:
        _STORE = PostgresArtifactReviewStore(dsn)
    else:
        _STORE = InMemoryArtifactReviewStore()
    return _STORE


def set_artifact_review_store(store: ArtifactReviewStore) -> None:
    """Test helper — pin an explicit store (usually the in-memory one)."""
    global _STORE
    _STORE = store


def reset_artifact_review_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None


def default_blob_root() -> Path:  # pragma: no cover — convenience for callers
    """The default blob root (outside the project tree) — see ``DocArtifactStore``."""
    from forgeflow.config import get_settings

    return get_settings().resource_store_path()
