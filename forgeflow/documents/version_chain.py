"""INC46 T25 — 多轮迭代与版本链（version DAG + refine / revert / compare）。

Why this module exists（书面差异 B1）
------------------------------------
任务书把 T25 的代码落点写成 ``artifacts/version_chain.py`` / ``documents/followup.py``，
并写「``artifact.create_version`` [M 只增 parent/state 参数]」。经核对仓库实际结构：

  * 本仓**没有** ``forgeflow/artifacts/`` 包；文档产物的版本 + 评审持久层是 T22 新建的
    :mod:`forgeflow.documents.review_store`（T22 自己也已书面声明过它把落点放在
    ``documents/`` 而非 ``artifacts/``，因为该表当时并不存在）；
  * ``artifact_versions`` 已经有 ``base_version`` 与 ``state`` 两列（迁移 028），
    故「加 parent/state 参数」这一步**无需再改列**，只需在链层把 parent 语义显式化。

因此本模块落在 ``forgeflow/documents/version_chain.py``（与 ``review_store`` 同包，
依赖方向 ``documents.version_chain → documents.review_store``），
``documents/followup.py`` 与任务书一致。这是**声明的加性差异**，不新建空壳包。

规格（任务书 TABLE 31）
------------------------
* 每个版本带 ``parent_version_id``；指令解析为 ``refine(base=最新 committed)`` /
  ``revert(to=vN)`` / ``compare(vA, vB)``;
* ``revert`` 生成**新版本**（内容 == 目标版本），**不删除、不改写历史**；
* 并发：乐观锁 —— ``base_version`` 不是 head ⇒ :class:`VersionConflict`（路由层 409）；
* ``refine`` / ``revert`` 事件写入 T16 的 ``feedback_events``（``revise`` / ``revert``）。

纪律（INC46 §8）
----------------
* 红线 4：未测量 ⇒ ``None``；``source_version`` 仅 ``revert`` 有值。
* 红线 5：``tenant`` 是每个读写方法的**第一个位置参数**；falsy 租户读空、拒绝写。
* 红线 6：只追加新版本 —— 不删除、不改写既有版本行。
* 红线 11：refine / revert 都只产出 **pending** 版本；``committed`` 仍须 T22 的 approve。
* 红线 20：改动集合加性（新表 ``artifact_version_edges``），既有列语义不动。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from forgeflow.documents.diff import diff_documents
from forgeflow.documents.review_store import (
    ArtifactNotFound,
    ArtifactReviewStore,
    ArtifactVersion,
    CrossTenantArtifact,
    get_artifact_review_store,
)

logger = logging.getLogger(__name__)

__all__ = [
    # edge kinds
    "EDGE_INITIAL",
    "EDGE_REFINE",
    "EDGE_REVERT",
    "EDGE_KINDS",
    # feedback kinds (T16 vocabulary — lowercase)
    "FEEDBACK_KIND_REVISED",
    "FEEDBACK_KIND_REVERTED",
    # --- records + errors ---
    "VersionEdge",
    "VersionChainError",
    "VersionConflict",
    "NotCommittedBase",
    "NoCommittedBase",
    # stores
    "VersionChainStore",
    "InMemoryVersionChainStore",
    "PostgresVersionChainStore",
    "get_version_chain_store",
    "set_version_chain_store",
    "reset_version_chain_store",
    # operations
    "resolve_head",
    "refine",
    "revert",
    "compare",
    "version_chain",
]

#: Edge ``edge_kind`` vocabulary (迁移 031).
EDGE_INITIAL = "initial"
EDGE_REFINE = "refine"
EDGE_REVERT = "revert"
EDGE_KINDS: tuple[str, ...] = (EDGE_INITIAL, EDGE_REFINE, EDGE_REVERT)

#: T16 feedback ``kind`` values a refine / revert writes. Lowercase on purpose:
#: :mod:`forgeflow.outcomes.labeler` maps ``"revise"``→REVISED and ``"revert"``→
#: REVERTED (the DB column has no CHECK, but the labeler's vocabulary is exact).
FEEDBACK_KIND_REVISED = "revise"
FEEDBACK_KIND_REVERTED = "revert"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Records                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class VersionEdge:
    """One edge in an artifact's version DAG (child → parent, with a kind)."""

    artifact_id: str
    child_version: int
    edge_kind: str
    tenant_id: str | None = None
    parent_version: int | None = None
    source_version: int | None = None
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "child_version": self.child_version,
            "edge_kind": self.edge_kind,
            "tenant_id": self.tenant_id,
            "parent_version": self.parent_version,
            "source_version": self.source_version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #
class VersionChainError(Exception):
    """Base class for every version-chain failure (⇒ mapped to an HTTP status)."""


class VersionConflict(VersionChainError):
    """The caller's ``base_version`` is not the current head ⇒ 409 (乐观锁).

    This is the optimistic-lock signal: someone read the chain, another writer
    committed a newer version, and the caller's base is now stale. We refuse
    rather than silently rebase (which would lose the other writer's work).
    """

    def __init__(self, artifact_id: str, base_version: int | None, head_version: int | None) -> None:
        super().__init__(
            f"artifact {artifact_id}: base_version={base_version} is stale, head={head_version}"
        )
        self.artifact_id = artifact_id
        self.base_version = base_version
        self.head_version = head_version


class NotCommittedBase(VersionChainError):
    """``refine`` / ``revert`` was pointed at a base that is not ``committed`` ⇒ 409.

    追问必须基于「最近一个**已批准**版本」——一个 rejected / pending 版本不构成
    可信基线（T16 阴性探针：基于 rejected / pending 版本继续改 ⇒ 拒绝并提示）。
    """

    def __init__(self, artifact_id: str, base_version: int, state: str) -> None:
        super().__init__(
            f"artifact {artifact_id}: base v{base_version} is {state}, not committed"
        )
        self.artifact_id = artifact_id
        self.base_version = base_version
        self.state = state


class NoCommittedBase(VersionChainError):
    """``refine`` was asked to continue an artifact that has **no committed version** ⇒ 409.

    链上已有版本（全部 rejected / pending），却没有一个可作为基线的已批准版本：
    此时继续改等于凭空选一个基线（红线 3 不得折算），故 fail-closed。
    """

    def __init__(self, artifact_id: str) -> None:
        super().__init__(
            f"artifact {artifact_id} has no committed version to refine from"
        )
        self.artifact_id = artifact_id


# --------------------------------------------------------------------------- #
# Store — shared logic (template methods over per-backend primitives)          #
# --------------------------------------------------------------------------- #
class VersionChainStore:
    """Tenant-scoped store for :class:`VersionEdge`.

    Subclasses implement three storage primitives over canonical *row* dicts;
    all behavioural rules (fail-closed tenant, one-edge-per-child) live here so
    both backends agree.
    """

    # --- storage primitives (implemented per backend) ---------------------- #
    def _load(self, tenant: str | None, artifact_id: str, child_version: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def _scan(self, tenant: str | None, artifact_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _save(self, row: dict[str, Any]) -> None:
        raise NotImplementedError

    # --- row ⇄ record ------------------------------------------------------ #
    @staticmethod
    def _row_to_edge(row: dict[str, Any]) -> VersionEdge:
        return VersionEdge(
            artifact_id=str(row["artifact_id"]),
            child_version=int(row["child_version"]),
            edge_kind=str(row["edge_kind"]),
            tenant_id=(str(row["tenant_id"]) if row.get("tenant_id") else None),
            parent_version=(int(row["parent_version"]) if row.get("parent_version") is not None else None),
            source_version=(int(row["source_version"]) if row.get("source_version") is not None else None),
            created_at=row.get("created_at"),
        )

    # --- writes ------------------------------------------------------------ #
    def record_edge(
        self,
        tenant: str | None,
        *,
        artifact_id: str,
        child_version: int,
        edge_kind: str,
        parent_version: int | None = None,
        source_version: int | None = None,
        now: datetime | None = None,
    ) -> VersionEdge:
        """Append one edge (idempotent per child version).

        Raises:
            ValueError: falsy tenant (fail-closed write — 红线 5) or an unknown
                ``edge_kind`` (a writer bug, not data).
        """
        if not tenant:
            raise ValueError("tenant required to record a version edge (fail closed)")
        if edge_kind not in EDGE_KINDS:
            raise ValueError(f"unknown edge_kind {edge_kind!r}; allowed={EDGE_KINDS}")
        row = {
            "tenant_id": tenant,
            "artifact_id": artifact_id,
            "child_version": int(child_version),
            "edge_kind": edge_kind,
            "parent_version": parent_version,
            "source_version": source_version,
            "created_at": now or _now(),
        }
        # One edge per child: a re-record must not silently duplicate (红线 6 —
        # the chain is a set of facts, not an append-only log of duplicates).
        existing = self._load(tenant, artifact_id, int(child_version))
        if existing is not None:
            return self._row_to_edge(existing)
        self._save(row)
        logger.info(
            "version edge recorded | tenant=%s artifact=%s child=v%d kind=%s parent=%s source=%s",
            tenant, artifact_id, int(child_version), edge_kind, parent_version, source_version,
        )
        return self._row_to_edge(row)

    # --- reads ------------------------------------------------------------- #
    def get_edge(self, tenant: str | None, artifact_id: str, child_version: int) -> VersionEdge | None:
        if not tenant:
            return None
        row = self._load(tenant, artifact_id, int(child_version))
        return self._row_to_edge(row) if row else None

    def list_edges(self, tenant: str | None, artifact_id: str) -> list[VersionEdge]:
        if not tenant:
            return []
        return [self._row_to_edge(r) for r in self._scan(tenant, artifact_id)]


# --------------------------------------------------------------------------- #
# In-memory backend (offline profile)                                          #
# --------------------------------------------------------------------------- #
class InMemoryVersionChainStore(VersionChainStore):
    """Process-local dict store (offline profile). Mirrors the PG semantics."""

    def __init__(self) -> None:
        self._edges: dict[tuple[str, str, int], dict[str, Any]] = {}

    def _load(self, tenant: str | None, artifact_id: str, child_version: int) -> dict[str, Any] | None:
        if not tenant:
            return None
        row = self._edges.get((tenant, artifact_id, int(child_version)))
        return dict(row) if row is not None else None

    def _scan(self, tenant: str | None, artifact_id: str) -> list[dict[str, Any]]:
        if not tenant:
            return []
        rows = [
            dict(r)
            for (t, aid, _c), r in self._edges.items()
            if t == tenant and (not artifact_id or aid == artifact_id)
        ]
        rows.sort(key=lambda r: (r.get("artifact_id") or "", int(r["child_version"])))
        return rows

    def _save(self, row: dict[str, Any]) -> None:
        key = (str(row["tenant_id"]), str(row["artifact_id"]), int(row["child_version"]))
        self._edges[key] = dict(row)


# --------------------------------------------------------------------------- #
# PostgreSQL backend (the real table, migration 031)                           #
# --------------------------------------------------------------------------- #
def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


_EDGE_COLUMNS = (
    "tenant_id, artifact_id, child_version, parent_version, edge_kind, source_version, created_at"
)


class PostgresVersionChainStore(VersionChainStore):
    """The real ``artifact_version_edges`` table (migration 031)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    @staticmethod
    def _edge_row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        names = (
            "tenant_id", "artifact_id", "child_version", "parent_version",
            "edge_kind", "source_version", "created_at",
        )
        return dict(zip(names, row))

    def _load(self, tenant: str | None, artifact_id: str, child_version: int) -> dict[str, Any] | None:
        if not tenant:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_EDGE_COLUMNS} FROM artifact_version_edges "
                "WHERE tenant_id = %s AND artifact_id = %s AND child_version = %s",
                (tenant, artifact_id, int(child_version)),
            )
            return self._edge_row(cur.fetchone())

    def _scan(self, tenant: str | None, artifact_id: str) -> list[dict[str, Any]]:
        if not tenant:
            return []
        sql = f"SELECT {_EDGE_COLUMNS} FROM artifact_version_edges WHERE tenant_id = %s"
        params: list[Any] = [tenant]
        if artifact_id:
            sql += " AND artifact_id = %s"
            params.append(artifact_id)
        sql += " ORDER BY artifact_id, child_version"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._edge_row(r) for r in cur.fetchall()]

    def _save(self, row: dict[str, Any]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO artifact_version_edges
                    (tenant_id, artifact_id, child_version, parent_version, edge_kind,
                     source_version, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, artifact_id, child_version) DO UPDATE SET
                    parent_version = EXCLUDED.parent_version,
                    edge_kind = EXCLUDED.edge_kind,
                    source_version = EXCLUDED.source_version
                """,
                (
                    str(row["tenant_id"]),
                    str(row["artifact_id"]),
                    int(row["child_version"]),
                    row.get("parent_version"),
                    str(row["edge_kind"]),
                    row.get("source_version"),
                    row.get("created_at") or _now(),
                ),
            )
            conn.commit()


# --------------------------------------------------------------------------- #
# Factory                                                                      #
# --------------------------------------------------------------------------- #
_STORE: Any = None


def get_version_chain_store() -> VersionChainStore:
    """Process-wide store: PG when a DSN is configured, else in-memory."""
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    _STORE = PostgresVersionChainStore(dsn) if dsn else InMemoryVersionChainStore()
    return _STORE


def set_version_chain_store(store: VersionChainStore) -> None:
    """Test helper — pin an explicit store (usually the in-memory one)."""
    global _STORE
    _STORE = store


def reset_version_chain_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None


# --------------------------------------------------------------------------- #
# Operations                                                                   #
# --------------------------------------------------------------------------- #
def resolve_head(review_store: ArtifactReviewStore, tenant: str | None, artifact_id: str) -> ArtifactVersion | None:
    """The current head = the latest **committed** version (None when there is none)."""
    return review_store.latest_committed(tenant, artifact_id)


def _require_committed_base(
    review_store: ArtifactReviewStore, tenant: str, artifact_id: str, base_version: int
) -> ArtifactVersion:
    """Load ``base_version`` and refuse it unless it is ``committed`` (红线 11)."""
    base = review_store.get_version(tenant, artifact_id, base_version)
    if base is None:
        if review_store._load_version_any(artifact_id, base_version) is not None:  # noqa: SLF001
            raise CrossTenantArtifact(artifact_id)
        raise ArtifactNotFound(f"{artifact_id}@v{base_version}")
    if base.state != "committed":
        raise NotCommittedBase(artifact_id, base_version, base.state)
    return base


def _record_chain_feedback(
    tenant: str, version: ArtifactVersion, kind: str, actor: str | None
) -> None:
    """Write a refine / revert signal into T16's ``feedback_events`` (best effort).

    Best-effort on purpose: a feedback failure must never undo a version write
    (the same rule T22's reject path follows). When there is no ``run_id`` there
    is nothing to attach the signal to, so it is skipped explicitly — never
    fabricated onto a made-up run.
    """
    run_id = version.run_id
    if not run_id:
        logger.info("chain feedback %s has no run_id; kept off the feedback stream", kind)
        return
    try:
        from forgeflow.outcomes.store import get_outcome_store

        store = get_outcome_store()
        store.record_feedback(
            tenant,
            run_id,
            kind,
            actor=actor,
            idempotency_key=f"artifact-{kind}:{version.artifact_id}:v{version.version}",
        )
    except Exception as exc:  # noqa: BLE001 — never let feedback break the chain write
        logger.warning("chain feedback write skipped: %s", exc)


def refine(
    tenant: str | None,
    *,
    artifact_id: str,
    content: bytes,
    base_version: int | None,
    fmt: str = "docx",
    run_id: str | None = None,
    diff: dict[str, Any] | None = None,
    out_of_region: list[dict[str, Any]] | None = None,
    actor: str | None = None,
    review_store: ArtifactReviewStore | None = None,
    chain_store: VersionChainStore | None = None,
    now: datetime | None = None,
) -> tuple[ArtifactVersion, Any]:
    """追问（「再正式一点」）：基于**最近一个 committed 版本**产出一个 pending 版本。

    乐观锁：``base_version`` 必须**恰好**是当前 head（最新 committed 版本）；
    否则 :class:`VersionConflict`（路由层 409），并提示正确基线。

    当链上还没有任何 committed 版本时，``base_version`` 必须为 ``None``，
    产出的是**根版本**（edge ``kind=initial``）。

    Raises:
        ValueError: falsy tenant（fail-closed，红线 5）。
        VersionConflict: base 不是 head（乐观锁失败）。
        NotCommittedBase: base 存在但不是 committed。
        ArtifactNotFound / CrossTenantArtifact: base 不存在 / 属他租户。
    """
    if not tenant:
        raise ValueError("tenant required to refine an artifact (fail closed)")
    review_store = review_store or get_artifact_review_store()
    chain_store = chain_store or get_version_chain_store()

    head = resolve_head(review_store, tenant, artifact_id)
    head_version = head.version if head else None

    if base_version is None:
        # No base named: only legal as a **root** (an empty chain). If versions
        # already exist but none is committed, there is no trustworthy baseline.
        if head_version is not None:
            raise VersionConflict(artifact_id, None, head_version)
        if review_store.list_versions(tenant, artifact_id):
            raise NoCommittedBase(artifact_id)
    else:
        # The named base must itself be committed (a pending/rejected version is
        # not a baseline), and must still be the head (optimistic lock).
        _require_committed_base(review_store, tenant, artifact_id, base_version)
        if base_version != head_version:
            raise VersionConflict(artifact_id, base_version, head_version)

    version, review = review_store.create_pending_version(
        tenant,
        artifact_id=artifact_id,
        content=content,
        fmt=fmt,
        diff=diff,
        out_of_region=out_of_region,
        base_version=base_version,
        run_id=run_id,
    )
    edge_kind = EDGE_REFINE if base_version is not None else EDGE_INITIAL
    chain_store.record_edge(
        tenant,
        artifact_id=artifact_id,
        child_version=version.version,
        edge_kind=edge_kind,
        parent_version=base_version,
        now=now,
    )
    if base_version is not None:
        _record_chain_feedback(tenant, version, FEEDBACK_KIND_REVISED, actor)
    logger.info(
        "artifact refined | tenant=%s artifact=%s base=%s new=v%d",
        tenant, artifact_id, base_version, version.version,
    )
    return version, review


def revert(
    tenant: str | None,
    *,
    artifact_id: str,
    to_version: int,
    fmt: str = "docx",
    run_id: str | None = None,
    base_version: int | None = None,
    actor: str | None = None,
    review_store: ArtifactReviewStore | None = None,
    chain_store: VersionChainStore | None = None,
    now: datetime | None = None,
) -> tuple[ArtifactVersion, Any]:
    """回退到 ``to_version``：产出一个**新** pending 版本，内容 == 目标版本。

    红线 6：**不删除、不改写历史** —— 目标版本与所有既有版本都原样保留，
    回退只是在其后追加一个内容等于目标版本的新版本。

    Args:
        to_version: 要回退到的版本号（其内容被复制进新版本）。
        base_version: 可选乐观锁 —— 若给出且 != 当前 head ⇒ :class:`VersionConflict`。
    """
    if not tenant:
        raise ValueError("tenant required to revert an artifact (fail closed)")
    review_store = review_store or get_artifact_review_store()
    chain_store = chain_store or get_version_chain_store()

    target = review_store.get_version(tenant, artifact_id, to_version)
    if target is None:
        if review_store._load_version_any(artifact_id, to_version) is not None:  # noqa: SLF001
            raise CrossTenantArtifact(artifact_id)
        raise ArtifactNotFound(f"{artifact_id}@v{to_version}")
    # The restored content must exist (a version with no blob cannot be reverted to).
    content = review_store.read_content(tenant, artifact_id, to_version)

    head = resolve_head(review_store, tenant, artifact_id)
    head_version = head.version if head else None
    if base_version is not None and base_version != head_version:
        raise VersionConflict(artifact_id, base_version, head_version)

    # The preview must be real: diff(current head → restored content).
    diff: dict[str, Any] | None = None
    if head is not None and head_version != to_version:
        try:
            head_bytes = review_store.read_content(tenant, artifact_id, head_version)
            diff = diff_documents(head_bytes, content, fmt=fmt).to_dict()
        except Exception as exc:  # noqa: BLE001 — a diff we cannot compute stays None
            logger.info("revert diff unmeasured (%s); stored as None (红线 4)", exc)
            diff = None

    new_version, review = review_store.create_pending_version(
        tenant,
        artifact_id=artifact_id,
        content=content,
        fmt=fmt,
        diff=diff,
        base_version=head_version,
        run_id=run_id,
    )
    chain_store.record_edge(
        tenant,
        artifact_id=artifact_id,
        child_version=new_version.version,
        edge_kind=EDGE_REVERT,
        parent_version=head_version,
        source_version=to_version,
        now=now,
    )
    _record_chain_feedback(tenant, new_version, FEEDBACK_KIND_REVERTED, actor)
    logger.info(
        "artifact reverted | tenant=%s artifact=%s to=v%d new=v%d (history preserved)",
        tenant, artifact_id, to_version, new_version.version,
    )
    return new_version, review


def compare(
    tenant: str | None,
    *,
    artifact_id: str,
    a: int,
    b: int,
    fmt: str = "docx",
    review_store: ArtifactReviewStore | None = None,
) -> dict[str, Any]:
    """Diff any two versions of one artifact (``compare(vA, vB)``).

    Raises:
        ValueError: falsy tenant（fail-closed）。
        ArtifactNotFound / CrossTenantArtifact: 任一版本不存在 / 属他租户。
    """
    if not tenant:
        raise ValueError("tenant required to compare artifact versions (fail closed)")
    review_store = review_store or get_artifact_review_store()
    left = review_store.read_content(tenant, artifact_id, a)
    right = review_store.read_content(tenant, artifact_id, b)
    return diff_documents(left, right, fmt=fmt).to_dict()


def version_chain(
    tenant: str | None,
    artifact_id: str,
    *,
    review_store: ArtifactReviewStore | None = None,
    chain_store: VersionChainStore | None = None,
) -> dict[str, Any]:
    """The full chain view: every version + every registered edge + the head."""
    review_store = review_store or get_artifact_review_store()
    chain_store = chain_store or get_version_chain_store()
    versions = review_store.list_versions(tenant, artifact_id)
    edges = chain_store.list_edges(tenant, artifact_id)
    head = review_store.latest_committed(tenant, artifact_id)
    return {
        "artifact_id": artifact_id,
        "tenant_id": tenant,
        "head_version": head.version if head else None,
        "versions": [v.to_dict() for v in versions],
        "edges": [e.to_dict() for e in edges],
    }
