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
from datetime import UTC, datetime
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
    return datetime.now(UTC)


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

    # --- orphan-blob report support ---------------------------------------- #
    def referenced_content_refs(self) -> set[str]:
        """Every ``content_ref`` any version row in this store points at.

        This is the referenced set for the orphan-blob report
        (:mod:`forgeflow.documents.blob_gc`): a ref in the returned
        set is by definition still referenced by a record.  Rows of
        every state count (pending / committed / expired / rejected
        alike — the record exists and names the blob).  Enumeration
        failure raises: callers must never be handed a partial set
        presented as complete.
        """
        raise NotImplementedError

    def has_content_ref(self, ref: str) -> bool:
        """True when any version row references ``ref``.

        A single-ref membership query over the same enumeration as
        :meth:`referenced_content_refs`, kept as a store read API.
        The orphan-blob report works from the snapshot set and does
        not call this; like the snapshot, it raises when the
        underlying enumeration cannot be completed.
        """
        return ref in self.referenced_content_refs()

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
            LeaseFencedError: the executing run's lease no longer holds
                (commit-time fencing — see below); nothing is persisted.

        Run-lease fencing (forgeflow.runtime.run_lease): this method is
        the run-scoped COMMIT point for the document/text/sheet edit
        tools, so when the caller is a run executing under a lease, the
        fencing token is validated here — at the receiving end, at
        commit time — not just before the handler started.  A takeover
        that landed mid-handler is therefore observed and the version
        is NOT persisted.  The validation is ATOMIC with the write:
        the whole commit runs inside ``lease_fenced_section`` — for
        the in-memory lease backend that holds the lease store's own
        lock (the lock takeover contends on) across validate + write;
        for Postgres, ``_save_version_and_review`` locks the lease row
        ``FOR UPDATE`` inside the commit transaction.  Blob ordering
        per path: in-memory — the blob is written after the
        validation (which, inside the lease lock, IS the final fence)
        and a blob this call created is removed again if persistence
        then fails; Postgres — the content-addressed blob is written
        BEFORE the fenced row commit (see the Postgres override of
        ``_create_pending_version_fenced``): an unreferenced blob is
        invisible to readers, so a refused commit can strand at most
        an orphan blob awaiting cleanup, while the reverse order
        could strand a committed version whose content was never
        written.  Neither path can leave a committed record pointing
        at missing content.
        """
        if not tenant:
            raise ValueError("tenant required to create an artifact version (fail closed)")
        if not content:
            raise ValueError("artifact content must not be empty")

        from forgeflow.runtime.run_lease import (
            assert_fenced_commit,
            current_lease_session_for,
            lease_fenced_section,
        )

        session = current_lease_session_for(tenant, run_id)
        with lease_fenced_section(session):
            assert_fenced_commit(tenant, run_id)
            return self._create_pending_version_fenced(
                tenant,
                artifact_id=artifact_id,
                content=content,
                fmt=fmt,
                diff=diff,
                out_of_region=out_of_region,
                base_version=base_version,
                run_id=run_id,
                approval_id=approval_id,
                expires_at=expires_at,
                now=now,
            )

    def _create_pending_version_fenced(
        self,
        tenant: str,
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
        """The commit body of :meth:`create_pending_version` (base /
        in-memory ordering; the Postgres store overrides this — see
        its docstring for its content-first order and why the two
        paths order the same steps differently).

        Runs inside the caller's fencing section (in-memory) /
        transaction discipline (Postgres hook below).  The blob goes
        first — but only after the caller's validation has passed —
        and any failure after that point removes a blob this call
        created and nothing else references, so a refused commit is
        fully rolled back, blob included.  For the in-memory path
        that validation IS the final fence (it runs inside the lease
        lock), so unlike the Postgres path — whose final fence is
        in-transaction, after the blob — nothing needs to survive a
        refusal here.
        """
        moment = now or _now()
        suffix = _FORMAT_SUFFIX.get(fmt, ".bin")
        blob_store = self.blob_store()
        content_ref = blob_store.ref_for(bytes(content), suffix=suffix)
        blob_preexisting = blob_store.exists(content_ref)
        blob_store.put(content, suffix=suffix)
        import hashlib

        digest = hashlib.sha256(bytes(content)).hexdigest()
        try:
            version = self._next_version(tenant, artifact_id)
            approval = approval_id or f"ap-{new_id()}"
            return self._persist_pending_version(
                tenant,
                artifact_id=artifact_id,
                version=version,
                approval=approval,
                content_ref=content_ref,
                digest=digest,
                fmt=fmt,
                diff=diff,
                out_of_region=out_of_region,
                base_version=base_version,
                run_id=run_id,
                expires_at=expires_at,
                moment=moment,
            )
        except BaseException:
            if not blob_preexisting:
                self._discard_unreferenced_blob(tenant, content_ref)
            raise

    def _discard_unreferenced_blob(self, tenant: str, content_ref: str) -> None:
        """Remove a just-created blob no version row points at.

        Best effort: content addressing means identical bytes share
        one reference, so the blob is deleted only when this store
        holds no version row referencing it (the caller additionally
        only calls this for blobs that did not exist before its own
        commit attempt).
        """
        try:
            if any(
                row.get("content_ref") == content_ref
                for row in self._scan_versions(tenant, "")
            ):
                return
            self.blob_store().delete(content_ref)
        except Exception:  # noqa: BLE001 — cleanup must not mask the refusal
            logger.warning(
                "orphan blob cleanup failed | tenant=%s ref=%s", tenant, content_ref, exc_info=True
            )

    def _persist_pending_version(
        self,
        tenant: str,
        *,
        artifact_id: str,
        version: int,
        approval: str,
        content_ref: str,
        digest: str,
        fmt: str,
        diff: dict[str, Any] | None,
        out_of_region: list[dict[str, Any]] | None,
        base_version: int | None,
        run_id: str | None,
        expires_at: datetime | None,
        moment: datetime,
    ) -> tuple[ArtifactVersion, ArtifactReview]:

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
        self._save_version_and_review(version_row, review_row)
        logger.info(
            "pending artifact version created | tenant=%s artifact=%s v=%d approval=%s",
            tenant, artifact_id, version, approval,
        )
        return self._row_to_version(version_row), self._row_to_review(review_row)

    def _save_version_and_review(
        self, version_row: dict[str, Any], review_row: dict[str, Any]
    ) -> None:
        """Persist one pending version + its review as a single commit.

        Default: the two primitive saves back to back (the in-memory
        backend runs them inside its version lock + the lease fencing
        section the caller holds).  The Postgres backend overrides
        this to run both inserts in ONE transaction that first locks
        the lease row ``FOR UPDATE`` — a version row without its
        review row (or vice versa) can then never be committed by a
        stale owner whose takeover landed between the two writes.
        """
        try:
            self._save_version(version_row)
            self._save_review(review_row)
        except Exception:
            # In-memory compensation: if the review insert failed
            # after the version insert landed, remove the version row
            # again so no half-committed pair remains (the caller
            # additionally discards the blob this call created).  The
            # Postgres override never reaches this — its transaction
            # rolls the pair back atomically.
            versions = getattr(self, "_versions", None)
            if isinstance(versions, dict):
                versions.pop(
                    (
                        version_row["tenant_id"],
                        version_row["artifact_id"],
                        version_row["version"],
                    ),
                    None,
                )
            raise

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
        from threading import RLock

        super().__init__(blob_store=blob_store)
        self._version_lock = RLock()
        self._versions: dict[tuple[str, str, int], dict[str, Any]] = {}
        self._reviews: dict[tuple[str, str], dict[str, Any]] = {}

    def create_pending_version(self, tenant, **kwargs):
        with self._version_lock:
            return super().create_pending_version(tenant, **kwargs)

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

    def referenced_content_refs(self) -> set[str]:
        with self._version_lock:
            return {
                str(row["content_ref"]) for row in self._versions.values() if row.get("content_ref")
            }

    def has_content_ref(self, ref: str) -> bool:
        with self._version_lock:
            return any(row.get("content_ref") == ref for row in self._versions.values())


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

    def _connect(self, tenant_id: str | None = None):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        from forgeflow.tenant_scope import current_tenant
        conn = psycopg.connect(self._dsn)
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id or current_tenant() or "",))
        return conn

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
        with self._connect(tenant) as conn, conn.cursor() as cur:
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
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._version_row(r) for r in cur.fetchall()]

    # --- orphan-blob report support (cross-tenant enumeration under RLS) --- #
    # ``artifact_versions`` sits under FORCE row-level security
    # (migration 035): a connection sees only the rows of the tenant
    # pinned in ``app.tenant_id`` (an empty pin sees nothing).  A
    # single unpinned scan would therefore UNDERCOUNT the referenced
    # set — the one direction a GC must never err in.  Enumeration is
    # per-tenant: the tenant list comes from ``tenant_memberships``
    # (the tenancy registry, deliberately outside RLS so membership
    # can be resolved at all), plus whatever the caller's own session
    # can see (a maintenance role with BYPASSRLS gets everything in
    # the first pass; the per-tenant passes are then redundant but
    # harmless).  If the tenant registry itself cannot be read, the
    # enumeration raises (see ``_gc_tenant_ids``): a partial set must
    # never be handed to the orphan-blob report as if it were whole.
    def _gc_tenant_ids(self) -> list[str]:
        # A failure to enumerate tenants PROPAGATES.  The previous
        # behaviour — log a warning and return [] — silently presented
        # a partial referenced set as complete: every blob belonging
        # to an unenumerated tenant looked unreferenced.  Enumeration
        # failure must be a loud failure for the whole report, never
        # a smaller set quietly treated as the truth.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT tenant_id FROM tenant_memberships")
            return [str(r[0]) for r in cur.fetchall() if r and r[0]]

    def _refs_visible_to(self, tenant: str | None) -> set[str]:
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT content_ref FROM artifact_versions "
                "WHERE content_ref IS NOT NULL AND content_ref <> ''"
            )
            return {str(r[0]) for r in cur.fetchall() if r and r[0]}

    def referenced_content_refs(self) -> set[str]:
        refs = self._refs_visible_to(None)
        for tenant in self._gc_tenant_ids():
            refs |= self._refs_visible_to(tenant)
        return refs

    def _ref_visible_to(self, tenant: str | None, ref: str) -> bool:
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM artifact_versions WHERE content_ref = %s LIMIT 1",
                (ref,),
            )
            return cur.fetchone() is not None

    def has_content_ref(self, ref: str) -> bool:
        if self._ref_visible_to(None, ref):
            return True
        return any(self._ref_visible_to(tenant, ref) for tenant in self._gc_tenant_ids())

    def _assert_lease_fence(self, cur: Any, row: dict[str, Any]) -> None:
        """Same-transaction commit fencing for run-scoped rows.

        When this process is executing the row's run under a Postgres
        lease session, the ``run_leases`` row is LOCKED (``SELECT ...
        FOR UPDATE``) on THIS connection, inside the same transaction
        as the write that follows, and the token must still be the
        current, unexpired, unreleased holder.  Holding the row lock
        until this transaction commits is what makes the check atomic:
        a concurrent takeover's ``INSERT ... ON CONFLICT DO UPDATE``
        needs the same row lock, so it blocks until this write has
        committed — it cannot slip in between the check and the write
        and then have the stale write land on top of it.  No row ⇒
        the lease was taken over mid-handler ⇒ raise
        ``LeaseFencedError`` and the surrounding transaction rolls
        back — the write is not persisted.  Rows not written by a
        lease-holding run (reviewer actions, direct callers) carry
        no matching session and pass unfenced.
        """
        run_id = row.get("run_id")
        if not run_id:
            return
        from forgeflow.runtime.run_lease import LeaseFencedError, current_run_lease

        session = current_run_lease.get()
        if session is None:
            return
        lease = session.lease
        if lease.tenant_id != row.get("tenant_id") or lease.run_id != run_id:
            return
        if getattr(session.store, "backend", None) != "postgres":
            return  # the base-class check already covered other backends
        cur.execute(
            "SELECT fencing_token FROM run_leases WHERE tenant_id = %s AND run_id = %s "
            "AND owner = %s AND fencing_token = %s AND released = false "
            "AND expires_at > now() FOR UPDATE",
            (lease.tenant_id, lease.run_id, lease.owner, lease.fencing_token),
        )
        if cur.fetchone() is None:
            session.mark_lost()
            raise LeaseFencedError(
                f"run {lease.run_id} lease fenced at commit: owner {lease.owner} "
                f"(fencing token {lease.fencing_token}) no longer holds the "
                "lease — it expired and was taken over; the write was NOT persisted"
            )

    @staticmethod
    def _insert_version(cur: Any, row: dict[str, Any]) -> None:
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

    @staticmethod
    def _insert_review(cur: Any, row: dict[str, Any]) -> None:
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

    def _save_version_and_review(
        self, version_row: dict[str, Any], review_row: dict[str, Any]
    ) -> None:
        """One transaction for the version + review commit.

        Overrides the base two-save default: the lease row is locked
        (``_assert_lease_fence`` ⇒ ``SELECT ... FOR UPDATE``) and both
        inserts run in the SAME transaction on one connection, so the
        commit is atomic against a concurrent takeover — the takeover
        blocks on the lease row lock until this transaction commits,
        and a fence failure rolls both inserts back together.
        """
        with self._connect(version_row.get("tenant_id")) as conn, conn.cursor() as cur:
            self._assert_lease_fence(cur, version_row)
            self._insert_version(cur, version_row)
            self._insert_review(cur, review_row)
            conn.commit()

    def _create_pending_version_fenced(
        self,
        tenant: str,
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
        """Postgres commit body: content FIRST, fenced reference second.

        The blob is written before anything else.  That is safe —
        and it is the only ordering with no bad window — because the
        blob is content-addressed: ``ref_for`` derives the reference
        from the SHA-256 of the bytes, and an UNREFERENCED blob is
        invisible.  Nothing can resolve it: every read path goes
        through a version row, and no version row points at it yet.

        The fenced transaction in :meth:`_save_version_and_review`
        (lease row ``SELECT ... FOR UPDATE``, then the version +
        review inserts, one commit) is what makes the reference
        visible, and by then the content it references is already
        durable.  The two naive alternatives both lose:

        * rows-then-blob (the previous order): a crash — or any
          failure — between the row commit and the blob put leaves a
          COMMITTED version whose content does not exist: a broken
          read that compensation deletes could not reliably undo
          (the delete itself can fail after readers have seen the
          row).
        * check-then-blob-then-rows without the in-transaction
          fence would reopen the takeover window the fence exists
          to close.

        A fence refusal (or any failure before the row commit)
        therefore leaves at most an ORPHAN blob: bytes no row
        references, invisible to readers, reclaimable by
        ``DocArtifactStore.delete``-based cleanup.  (No scheduled
        blob GC exists in the codebase today; orphans are inert and
        content-addressed, so identical bytes re-put under the same
        ref — the waste is bounded by distinct refused contents.)
        If the blob put itself fails, nothing has been committed
        and the error simply propagates — no compensation is needed
        in either direction, which is the point of the ordering.
        """
        import hashlib

        moment = now or _now()
        suffix = _FORMAT_SUFFIX.get(fmt, ".bin")
        blob_store = self.blob_store()
        content_ref = blob_store.ref_for(bytes(content), suffix=suffix)
        digest = hashlib.sha256(bytes(content)).hexdigest()
        blob_store.put(content, suffix=suffix)
        version = self._next_version(tenant, artifact_id)
        approval = approval_id or f"ap-{new_id()}"
        return self._persist_pending_version(
            tenant,
            artifact_id=artifact_id,
            version=version,
            approval=approval,
            content_ref=content_ref,
            digest=digest,
            fmt=fmt,
            diff=diff,
            out_of_region=out_of_region,
            base_version=base_version,
            run_id=run_id,
            expires_at=expires_at,
            moment=moment,
        )

    def _save_version(self, row: dict[str, Any]) -> None:
        with self._connect(row.get("tenant_id")) as conn, conn.cursor() as cur:
            self._assert_lease_fence(cur, row)
            self._insert_version(cur, row)
            conn.commit()

    def _load_review(self, tenant: str | None, approval_id: str) -> dict[str, Any] | None:
        if not tenant or not approval_id:
            return None
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_REVIEW_COLUMNS} FROM artifact_reviews "
                "WHERE tenant_id = %s AND approval_id = %s",
                (tenant, approval_id),
            )
            return self._review_row(cur.fetchone())

    def _save_review(self, row: dict[str, Any]) -> None:
        with self._connect(row.get("tenant_id")) as conn, conn.cursor() as cur:
            self._assert_lease_fence(cur, row)
            self._insert_review(cur, row)
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
