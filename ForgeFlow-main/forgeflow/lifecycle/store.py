"""INC46 T35 — Skill 生命周期持久层（迁移 033 的两张表，双后端）。

表与语义（任务书 T35 §代码/§DB）
================================
* ``skill_lifecycle_events``  —— 状态迁移的**只追加**审计流（from→to + 理由 +
  操作者）。历史不可改写：任何一次迁移都新增一行，绝不 UPDATE / DELETE
  （红线 6：不得覆盖历史）。
* ``skill_merge_proposals``   —— 去重 / 合并**提案**（不自动合并）。一次提案
  保留两个**源 skill ID**（来源链），人工 approve 后才落 `approved` 并记录
  `merged_skill_id`。提案是「决策记录」，其 status 字段可更新（这不是版本历史）。

双后端纪律
----------
* 内存后端服务于离线 / 单测（``STORAGE_BACKEND=memory`` 或未配 DSN）；
* PG 后端直连 ``POSTGRES_SYNC_URL``（psycopg 同步），与
  :mod:`forgeflow.rollout.store` / :mod:`forgeflow.documents.version_chain` 同一模式；
* ``tenant_id`` 是**每表第一列且 NOT NULL**（红线 5）；未解析租户读写皆空 / 拒写。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "LifecycleStoreError",
    "LifecycleEvent",
    "MergeProposalRecord",
    "LifecycleStore",
    "InMemoryLifecycleStore",
    "PostgresLifecycleStore",
    "get_lifecycle_store",
    "set_lifecycle_store",
    "reset_lifecycle_store",
    "PROPOSAL_PROPOSED",
    "PROPOSAL_APPROVED",
    "PROPOSAL_REJECTED",
]

PROPOSAL_PROPOSED = "proposed"
PROPOSAL_APPROVED = "approved"
PROPOSAL_REJECTED = "rejected"
_PROPOSAL_STATUSES = (PROPOSAL_PROPOSED, PROPOSAL_APPROVED, PROPOSAL_REJECTED)


class LifecycleStoreError(ValueError):
    """非法生命周期持久化操作（fail-closed）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


@dataclass
class LifecycleEvent:
    """一次状态迁移（``skill_lifecycle_events`` 一行）—— **只追加**。"""

    tenant_id: str
    skill_id: str
    from_state: str
    to_state: str
    reason: str = ""
    actor: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "skill_id": self.skill_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "actor": self.actor,
            "created_at": _iso(self.created_at),
        }


@dataclass
class MergeProposalRecord:
    """一条去重 / 合并提案（``skill_merge_proposals`` 一行）。"""

    tenant_id: str
    primary_skill_id: str
    duplicate_skill_id: str
    source_skill_ids: list[str] = field(default_factory=list)
    description_cosine: float | None = None
    tool_jaccard: float | None = None
    status: str = PROPOSAL_PROPOSED
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: datetime = field(default_factory=_now)
    decided_by: str | None = None
    decided_at: datetime | None = None
    merged_skill_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "primary_skill_id": self.primary_skill_id,
            "duplicate_skill_id": self.duplicate_skill_id,
            "source_skill_ids": list(self.source_skill_ids),
            "description_cosine": self.description_cosine,
            "tool_jaccard": self.tool_jaccard,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "decided_by": self.decided_by,
            "decided_at": _iso(self.decided_at),
            "merged_skill_id": self.merged_skill_id,
        }


@runtime_checkable
class LifecycleStore(Protocol):
    """生命周期读写面（两后端语义一致；``tenant_id`` 是第一参数）。"""

    def append_event(self, record: LifecycleEvent) -> LifecycleEvent: ...
    def list_events(self, tenant_id: str | None, skill_id: str) -> list[LifecycleEvent]: ...
    def latest_event(self, tenant_id: str | None, skill_id: str) -> LifecycleEvent | None: ...

    def save_proposal(self, record: MergeProposalRecord) -> MergeProposalRecord: ...
    def get_proposal(self, tenant_id: str | None, proposal_id: str) -> MergeProposalRecord | None: ...
    def list_proposals(
        self, tenant_id: str | None, *, status: str | None = None
    ) -> list[MergeProposalRecord]: ...
    def find_proposal(
        self, tenant_id: str | None, primary_skill_id: str, duplicate_skill_id: str
    ) -> MergeProposalRecord | None: ...
    def update_proposal(self, record: MergeProposalRecord) -> MergeProposalRecord: ...


# --------------------------------------------------------------------------- #
# In-memory backend                                                            #
# --------------------------------------------------------------------------- #
@dataclass
class _MemoryState:
    events: list[LifecycleEvent] = field(default_factory=list)
    proposals: dict[tuple[str, str], MergeProposalRecord] = field(default_factory=dict)


class InMemoryLifecycleStore:
    """Process-local store (offline profile / unit seam).

    Tenant fail-closed: an unresolved tenant reads empty and refuses writes.
    """

    def __init__(self, state: _MemoryState | None = None) -> None:
        self._state = state if state is not None else _MemoryState()

    # -- events (append-only) --------------------------------------------- #
    def append_event(self, record: LifecycleEvent) -> LifecycleEvent:
        if not record.tenant_id:
            raise LifecycleStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        self._state.events.append(record)
        return record

    def list_events(self, tenant_id: str | None, skill_id: str) -> list[LifecycleEvent]:
        if not tenant_id:
            return []
        rows = [
            e
            for e in self._state.events
            if e.tenant_id == str(tenant_id) and (not skill_id or e.skill_id == skill_id)
        ]
        rows.sort(key=lambda e: (e.created_at, e.id))
        return rows

    def latest_event(self, tenant_id: str | None, skill_id: str) -> LifecycleEvent | None:
        rows = self.list_events(tenant_id, skill_id)
        return rows[-1] if rows else None

    # -- proposals ---------------------------------------------------------- #
    def save_proposal(self, record: MergeProposalRecord) -> MergeProposalRecord:
        if not record.tenant_id:
            raise LifecycleStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        if record.status not in _PROPOSAL_STATUSES:
            raise LifecycleStoreError(f"非法提案状态：{record.status!r}")
        self._state.proposals[(record.tenant_id, record.id)] = record
        return record

    def get_proposal(self, tenant_id: str | None, proposal_id: str) -> MergeProposalRecord | None:
        if not tenant_id:
            return None
        return self._state.proposals.get((str(tenant_id), str(proposal_id)))

    def list_proposals(
        self, tenant_id: str | None, *, status: str | None = None
    ) -> list[MergeProposalRecord]:
        if not tenant_id:
            return []
        rows = [
            p
            for (t, _i), p in self._state.proposals.items()
            if t == str(tenant_id) and (status is None or p.status == status)
        ]
        rows.sort(key=lambda p: (p.created_at, p.id))
        return rows

    def find_proposal(
        self, tenant_id: str | None, primary_skill_id: str, duplicate_skill_id: str
    ) -> MergeProposalRecord | None:
        if not tenant_id:
            return None
        # Unordered pair: a proposal for (a, b) and (b, a) is the same pair.
        want = {str(primary_skill_id), str(duplicate_skill_id)}
        for p in self.list_proposals(tenant_id):
            if {p.primary_skill_id, p.duplicate_skill_id} == want:
                return p
        return None

    def update_proposal(self, record: MergeProposalRecord) -> MergeProposalRecord:
        if not record.tenant_id:
            raise LifecycleStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        if record.status not in _PROPOSAL_STATUSES:
            raise LifecycleStoreError(f"非法提案状态：{record.status!r}")
        self._state.proposals[(record.tenant_id, record.id)] = record
        return record


# --------------------------------------------------------------------------- #
# PostgreSQL backend (migration 033)                                            #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_EVENT_COLS = "id, tenant_id, skill_id, from_state, to_state, reason, actor, created_at"
_PROPOSAL_COLS = (
    "id, tenant_id, primary_skill_id, duplicate_skill_id, source_skill_ids, "
    "description_cosine, tool_jaccard, status, created_at, decided_by, decided_at, "
    "merged_skill_id"
)


class PostgresLifecycleStore:
    """The real ``skill_lifecycle_events`` / ``skill_merge_proposals`` tables."""

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    # -- events ------------------------------------------------------------- #
    @staticmethod
    def _event_row(row: Any) -> LifecycleEvent:
        names = ("id", "tenant_id", "skill_id", "from_state", "to_state", "reason", "actor", "created_at")
        d = dict(zip(names, row))
        return LifecycleEvent(
            id=str(d["id"]),
            tenant_id=d["tenant_id"],
            skill_id=d["skill_id"],
            from_state=d["from_state"],
            to_state=d["to_state"],
            reason=d["reason"] or "",
            actor=d["actor"],
            created_at=d["created_at"],
        )

    def append_event(self, record: LifecycleEvent) -> LifecycleEvent:
        if not record.tenant_id:
            raise LifecycleStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skill_lifecycle_events
                    (id, tenant_id, skill_id, from_state, to_state, reason, actor, created_at)
                VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(record.id),
                    record.tenant_id,
                    record.skill_id,
                    record.from_state,
                    record.to_state,
                    record.reason,
                    record.actor,
                    record.created_at,
                ),
            )
            conn.commit()
        return record

    def list_events(self, tenant_id: str | None, skill_id: str) -> list[LifecycleEvent]:
        if not tenant_id:
            return []
        sql = f"SELECT {_EVENT_COLS} FROM skill_lifecycle_events WHERE tenant_id = %s"
        params: list[Any] = [str(tenant_id)]
        if skill_id:
            sql += " AND skill_id = %s"
            params.append(str(skill_id))
        sql += " ORDER BY created_at, id"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._event_row(r) for r in cur.fetchall()]

    def latest_event(self, tenant_id: str | None, skill_id: str) -> LifecycleEvent | None:
        if not tenant_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_EVENT_COLS} FROM skill_lifecycle_events "
                "WHERE tenant_id = %s AND skill_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (str(tenant_id), str(skill_id)),
            )
            row = cur.fetchone()
            return self._event_row(row) if row is not None else None

    # -- proposals ---------------------------------------------------------- #
    @staticmethod
    def _proposal_row(row: Any) -> MergeProposalRecord:
        names = (
            "id", "tenant_id", "primary_skill_id", "duplicate_skill_id", "source_skill_ids",
            "description_cosine", "tool_jaccard", "status", "created_at", "decided_by",
            "decided_at", "merged_skill_id",
        )
        d = dict(zip(names, row))
        raw = d["source_skill_ids"]
        if isinstance(raw, str):  # JSONB comes back as a python list via psycopg3
            import json

            raw = json.loads(raw)
        return MergeProposalRecord(
            id=str(d["id"]),
            tenant_id=d["tenant_id"],
            primary_skill_id=d["primary_skill_id"],
            duplicate_skill_id=d["duplicate_skill_id"],
            source_skill_ids=[str(x) for x in (raw or [])],
            description_cosine=d["description_cosine"],
            tool_jaccard=d["tool_jaccard"],
            status=d["status"],
            created_at=d["created_at"],
            decided_by=d["decided_by"],
            decided_at=d["decided_at"],
            merged_skill_id=d["merged_skill_id"],
        )

    def save_proposal(self, record: MergeProposalRecord) -> MergeProposalRecord:
        if not record.tenant_id:
            raise LifecycleStoreError("拒绝写入：未解析租户（fail-closed，红线 5）")
        if record.status not in _PROPOSAL_STATUSES:
            raise LifecycleStoreError(f"非法提案状态：{record.status!r}")
        import json

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO skill_merge_proposals
                    (id, tenant_id, primary_skill_id, duplicate_skill_id, source_skill_ids,
                     description_cosine, tool_jaccard, status, created_at, decided_by,
                     decided_at, merged_skill_id)
                VALUES (%s::uuid, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    decided_by = EXCLUDED.decided_by,
                    decided_at = EXCLUDED.decided_at,
                    merged_skill_id = EXCLUDED.merged_skill_id
                """,
                (
                    str(record.id),
                    record.tenant_id,
                    record.primary_skill_id,
                    record.duplicate_skill_id,
                    json.dumps(list(record.source_skill_ids)),
                    record.description_cosine,
                    record.tool_jaccard,
                    record.status,
                    record.created_at,
                    record.decided_by,
                    record.decided_at,
                    record.merged_skill_id,
                ),
            )
            conn.commit()
        return record

    def get_proposal(self, tenant_id: str | None, proposal_id: str) -> MergeProposalRecord | None:
        if not tenant_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_PROPOSAL_COLS} FROM skill_merge_proposals "
                "WHERE tenant_id = %s AND id = %s::uuid",
                (str(tenant_id), str(proposal_id)),
            )
            row = cur.fetchone()
            return self._proposal_row(row) if row is not None else None

    def list_proposals(
        self, tenant_id: str | None, *, status: str | None = None
    ) -> list[MergeProposalRecord]:
        if not tenant_id:
            return []
        sql = f"SELECT {_PROPOSAL_COLS} FROM skill_merge_proposals WHERE tenant_id = %s"
        params: list[Any] = [str(tenant_id)]
        if status is not None:
            sql += " AND status = %s"
            params.append(status)
        sql += " ORDER BY created_at, id"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._proposal_row(r) for r in cur.fetchall()]

    def find_proposal(
        self, tenant_id: str | None, primary_skill_id: str, duplicate_skill_id: str
    ) -> MergeProposalRecord | None:
        if not tenant_id:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_PROPOSAL_COLS} FROM skill_merge_proposals "
                "WHERE tenant_id = %s AND ("
                "  (primary_skill_id = %s AND duplicate_skill_id = %s) OR "
                "  (primary_skill_id = %s AND duplicate_skill_id = %s)) "
                "ORDER BY created_at, id LIMIT 1",
                (
                    str(tenant_id),
                    str(primary_skill_id),
                    str(duplicate_skill_id),
                    str(duplicate_skill_id),
                    str(primary_skill_id),
                ),
            )
            row = cur.fetchone()
            return self._proposal_row(row) if row is not None else None

    def update_proposal(self, record: MergeProposalRecord) -> MergeProposalRecord:
        return self.save_proposal(record)


# --------------------------------------------------------------------------- #
# Factory                                                                       #
# --------------------------------------------------------------------------- #
_STORE: Any = None


def get_lifecycle_store() -> LifecycleStore:
    """Process-wide store: PG when a DSN is configured, else in-memory."""
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    _STORE = PostgresLifecycleStore(dsn) if dsn else InMemoryLifecycleStore()
    return _STORE


def set_lifecycle_store(store: LifecycleStore) -> None:
    """Test helper — pin an explicit store (usually the in-memory one)."""
    global _STORE
    _STORE = store


def reset_lifecycle_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None
