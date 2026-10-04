"""INC46 T17 — 独立 Golden 回归集（Held-out Eval Registry）。

目标（任务书 T17）
------------------
* 评测集与训练/挖掘数据**隔离**；evolution 对其**只读**；
* 防数据泄漏造成的「自己考自己」；
* 集合一旦 ``frozen``，内容哈希锁定；
* case 来源只能是 ``human_curated`` 或 ``customer_approved_export``，
  ``mined`` 来源不得进入 holdout；
* 按 ``run_id`` / 源文档指纹切 train / holdout，holdout ≥ 30%；
* 初始集 ≥ 20 例人工策划（T36 扩展到基准 ≥ 50）。

红线
----
* **13 不伪造**：本模块**不预置任何 golden 用例**。用例只能经 admin 通过
  ``POST /eval/golden-sets`` 真实导入。因此在一个全新环境里「已冻结集」为 0，
  R4 探针如实报**未满足**（fail-closed），绝不把能力自检伪装成「已有评测集」。
* **5 租户隔离**：``tenant_id`` 是每个读写的第一参数；未解析租户一律 fail-closed
  （读空集 / 写被拒），绝不回落到共享 ``default`` 桶跨租户泄漏。
* **4 未测量 ⇒ None**：``passed`` / ``content_hash`` 未测量时为 ``None``，绝不写 0/空串。

写保护（evolution 只读）
------------------------
:func:`assert_can_write_golden` 是唯一写入门；只有 ``admin`` 通过。evolution 的服务
账号（``service``）与其它角色**写** ``golden_*`` 一律 403；读不受限。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from forgeflow.config import get_settings
from forgeflow.evaluation.leakage_guard import normalize_fingerprint
from forgeflow.repositories.base import new_id, utcnow
from forgeflow.skills.errors import GovernanceError

logger = logging.getLogger(__name__)

__all__ = [
    "PROVENANCE_HUMAN_CURATED",
    "PROVENANCE_CUSTOMER_EXPORT",
    "ALLOWED_PROVENANCE",
    "FORBIDDEN_PROVENANCE",
    "HOLDOUT_MIN_RATIO",
    "INITIAL_MIN_CASES",
    "BENCHMARK_MIN_CASES",
    "GOLDEN_WRITE_ROLES",
    "SPLIT_TRAIN",
    "SPLIT_HOLDOUT",
    "GoldenRegistryError",
    "ProvenanceError",
    "FrozenSetMutation",
    "GoldenSetHashMismatch",
    "assert_can_write_golden",
    "GoldenCase",
    "GoldenSet",
    "GoldenRun",
    "compute_content_hash",
    "verify_content_hash",
    "freeze_set",
    "split_train_holdout",
    "GoldenRegistryStore",
    "MemoryGoldenRegistry",
    "PostgresGoldenRegistry",
    "get_golden_registry",
    "reset_golden_registry",
    "frozen_set_count",
    "register_frozen_index",
    "clear_frozen_index",
    "self_test",
]

# --------------------------------------------------------------------------- #
# provenance / thresholds                                                      #
# --------------------------------------------------------------------------- #
PROVENANCE_HUMAN_CURATED = "human_curated"
PROVENANCE_CUSTOMER_EXPORT = "customer_approved_export"

#: The only two admissible case provenances (任务书 T17 规格).
ALLOWED_PROVENANCE: tuple[str, ...] = (
    PROVENANCE_HUMAN_CURATED,
    PROVENANCE_CUSTOMER_EXPORT,
)
#: Provenances explicitly barred from the holdout (「自己考自己」的来源).
FORBIDDEN_PROVENANCE: tuple[str, ...] = ("mined", "synthetic", "generated")

#: holdout 初始默认占比下限。
HOLDOUT_MIN_RATIO = 0.30
#: 初始人工策划集的最小规模（T17）；基准线（T36 扩展）。
INITIAL_MIN_CASES = 20
BENCHMARK_MIN_CASES = 50

#: 唯一允许写 golden_* 的角色集合（``admin``；evolution 服务账号被拒）。
GOLDEN_WRITE_ROLES: frozenset[str] = frozenset({"admin"})

SPLIT_TRAIN = "train"
SPLIT_HOLDOUT = "holdout"


# --------------------------------------------------------------------------- #
# errors                                                                       #
# --------------------------------------------------------------------------- #
class GoldenRegistryError(Exception):
    """Base class for golden-registry domain errors."""


class ProvenanceError(GoldenRegistryError):
    """A case/set carries a provenance that is not admissible."""


class FrozenSetMutation(GoldenRegistryError):
    """An attempt was made to change an already-frozen set."""


class GoldenSetHashMismatch(GoldenRegistryError):
    """A frozen set's recomputed content hash differs from its locked hash."""


def assert_can_write_golden(actor_role: str | None) -> None:
    """Fail-closed write gate — only ``admin`` may write ``golden_*``.

    Raises:
        GovernanceError: ``status_code == 403`` for any other role (including the
            ``service`` identity the evolution loop runs as). This is the
            ``evolution 服务账号写 golden_* ⇒ 拒绝`` 阴性探针的执行点。
    """
    if (actor_role or "") not in GOLDEN_WRITE_ROLES:
        raise GovernanceError(
            f"角色 '{actor_role or '-'}' 无写 golden 评测集权限（仅 admin 可人工导入），写入被拒绝",
            status_code=403,
        )


# --------------------------------------------------------------------------- #
# value objects                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class GoldenCase:
    """One held-out / training document-edit case.

    ``source_doc_fingerprint`` is the single leakage key — see
    :mod:`forgeflow.evaluation.leakage_guard` for its canonical form.
    """

    case_id: str
    source_doc_fingerprint: str
    run_id: str = ""
    instruction: str = ""
    expected_summary: str = ""
    #: ``train`` | ``holdout`` — (re)assigned by :func:`split_train_holdout`.
    split: str = SPLIT_TRAIN
    provenance: str = PROVENANCE_HUMAN_CURATED
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoldenCase":
        payload = dict(data)
        created = payload.get("created_at")
        if isinstance(created, str):
            try:
                payload["created_at"] = datetime.fromisoformat(created)
            except ValueError:
                payload.pop("created_at", None)
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 — dataclass API
        payload = {k: v for k, v in payload.items() if k in known}
        return cls(**payload)


@dataclass
class GoldenSet:
    """A versioned, hash-locked collection of golden cases."""

    set_id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    name: str = ""
    provenance: str = PROVENANCE_HUMAN_CURATED
    cases: list[GoldenCase] = field(default_factory=list)
    #: Frozen ⇒ content locked by :attr:`content_hash`. ``None`` before freezing
    #: (未测量 ⇒ None，红线 4).
    frozen: bool = False
    content_hash: str | None = None
    frozen_at: datetime | None = None
    #: Optional recorded baseline metrics (the dimension a candidate is compared
    #: against). Empty ⇒ the regression is honestly reported as ``no_baseline``.
    baseline_metrics: dict[str, float] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    @property
    def case_count(self) -> int:
        return len(self.cases)

    def counts(self) -> tuple[int, int]:
        """Return ``(train_count, holdout_count)`` from the cases' own splits."""
        holdout = sum(1 for c in self.cases if c.split == SPLIT_HOLDOUT)
        return len(self.cases) - holdout, holdout

    def to_dict(self) -> dict[str, Any]:
        train, holdout = self.counts()
        return {
            "set_id": self.set_id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "provenance": self.provenance,
            "frozen": self.frozen,
            "content_hash": self.content_hash,
            "frozen_at": self.frozen_at.isoformat() if self.frozen_at else None,
            "case_count": self.case_count,
            "train_count": train,
            "holdout_count": holdout,
            "baseline_metrics": dict(self.baseline_metrics),
            "created_at": self.created_at.isoformat(),
        }

    def to_dict_with_cases(self) -> dict[str, Any]:
        data = self.to_dict()
        data["cases"] = [c.to_dict() for c in self.cases]
        return data


@dataclass
class GoldenRun:
    """One recorded golden regression over a frozen set (traceable to its hash).

    ``passed`` is ``None`` when nothing was measurable (no shared metric with the
    baseline) — 未测量 ⇒ None，红线 4；it is never coerced to ``False``/``0``.
    """

    golden_run_id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    set_id: str = ""
    set_content_hash: str = ""
    skill_id: str = ""
    candidate_id: str = ""
    train_size: int = 0
    holdout_size: int = 0
    passed: bool | None = None
    voided: bool = False
    leakage: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


# --------------------------------------------------------------------------- #
# content hash (the frozen lock)                                               #
# --------------------------------------------------------------------------- #
def _case_content(case: GoldenCase) -> dict[str, Any]:
    """The content fields a case contributes to the set hash.

    ``split`` (assigned at read time) and timestamps are **excluded** so the hash
    is stable across runs; ``provenance`` is hashed at the set level.
    """
    return {
        "case_id": case.case_id,
        "source_doc_fingerprint": normalize_fingerprint(case.source_doc_fingerprint) or "",
        "run_id": case.run_id,
        "instruction": case.instruction,
        "expected_summary": case.expected_summary,
        "provenance": case.provenance,
    }


def compute_content_hash(cases: list[GoldenCase], provenance: str) -> str:
    """Return the canonical content hash ``"sha256:<hex>"`` of a set's content.

    Deterministic: cases are sorted by ``case_id`` and serialised with sorted
    keys, so two sets with the same content hash identically regardless of input
    order.
    """
    payload = {
        "provenance": provenance,
        "cases": sorted((_case_content(c) for c in cases), key=lambda c: c["case_id"]),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(blob.encode('utf-8')).hexdigest()}"


def verify_content_hash(gset: GoldenSet) -> str:
    """Recompute a frozen set's hash and assert it matches the locked value.

    Raises:
        GoldenSetHashMismatch: when the content no longer matches the lock — the
            ``修改 frozen 集内容 ⇒ 哈希不符，拒绝`` 阴性探针的执行点.
    """
    recomputed = compute_content_hash(gset.cases, gset.provenance)
    if gset.content_hash != recomputed:
        raise GoldenSetHashMismatch(
            f"集合 {gset.set_id} 的冻结内容哈希不符：锁定 {gset.content_hash}，"
            f"实算 {recomputed}（frozen 集不可被修改）"
        )
    return recomputed


def _validate_provenance(case_or_set_provenance: str) -> str:
    value = str(case_or_set_provenance or "").strip()
    if value not in ALLOWED_PROVENANCE:
        raise ProvenanceError(
            f"非法来源 {value or '(空)'}：case 来源只能是 "
            f"{' / '.join(ALLOWED_PROVENANCE)}（mined 等不得进入 holdout）"
        )
    return value


def freeze_set(gset: GoldenSet, *, require_min_cases: int = 0) -> GoldenSet:
    """Validate + hash-lock a set, returning a frozen copy.

    Args:
        gset: the (unfrozen) set to freeze.
        require_min_cases: when > 0, refuse to freeze a set smaller than this
            (used to enforce the initial curated-set floor where required). The
            default 0 allows smaller sets (e.g. unit fixtures).

    Raises:
        ProvenanceError: a case/set carries an inadmissible provenance.
        FrozenSetMutation: the set is already frozen (content is immutable).
        GoldenRegistryError: no cases, or a case without a source fingerprint.
    """
    if gset.frozen:
        raise FrozenSetMutation(f"集合 {gset.set_id} 已冻结，内容不可再改")
    provenance = _validate_provenance(gset.provenance)
    if not gset.cases:
        raise GoldenRegistryError("golden 集不能为空（无案例）")
    if require_min_cases and len(gset.cases) < require_min_cases:
        raise GoldenRegistryError(
            f"golden 集案例数 {len(gset.cases)} < 最小要求 {require_min_cases}"
        )
    for case in gset.cases:
        _validate_provenance(case.provenance)
        if not normalize_fingerprint(case.source_doc_fingerprint):
            raise GoldenRegistryError(f"case {case.case_id} 缺少源文档指纹（泄漏检查的键）")

    frozen = replace(gset)
    frozen.cases = [replace(c) for c in gset.cases]
    frozen.provenance = provenance
    frozen.content_hash = compute_content_hash(frozen.cases, provenance)
    frozen.frozen = True
    frozen.frozen_at = utcnow()
    return frozen


def split_train_holdout(
    cases: list[GoldenCase],
    min_holdout_ratio: float = HOLDOUT_MIN_RATIO,
) -> tuple[list[GoldenCase], list[GoldenCase]]:
    """Deterministically split ``cases`` into ``(train, holdout)``.

    Isolation is preserved by grouping on ``run_id`` (falling back to the source
    fingerprint, then the case id): a whole group lands on one side, so the same
    run / source document never straddles the split (which would itself leak).
    Groups are taken from the deterministic tail until the holdout reaches at
    least ``ceil(n * min_holdout_ratio)`` cases — guaranteeing ``holdout ≥ ratio``.

    Returns the two lists with each returned case's ``split`` set accordingly.
    """
    ordered = sorted(cases, key=lambda c: (_group_key(c), c.case_id))
    n = len(ordered)
    if n == 0:
        return [], []

    keys = {_group_key(c) for c in ordered}
    keyer = _group_key if len(keys) > 1 else (lambda c: c.case_id)
    groups: dict[str, list[GoldenCase]] = {}
    for case in ordered:
        groups.setdefault(keyer(case), []).append(case)

    target = max(1, math.ceil(n * max(0.0, min_holdout_ratio)))
    holdout_keys: set[str] = set()
    holdout_n = 0
    for key in sorted(groups, reverse=True):  # deterministic tail
        if holdout_n >= target:
            break
        holdout_keys.add(key)
        holdout_n += len(groups[key])

    train: list[GoldenCase] = []
    holdout: list[GoldenCase] = []
    for case in ordered:
        if keyer(case) in holdout_keys:
            holdout.append(replace(case, split=SPLIT_HOLDOUT))
        else:
            train.append(replace(case, split=SPLIT_TRAIN))
    return train, holdout


def _group_key(case: GoldenCase) -> str:
    return (
        case.run_id
        or normalize_fingerprint(case.source_doc_fingerprint)
        or case.case_id
    )


# --------------------------------------------------------------------------- #
# process-level frozen index (feeds the R4 interlock probe without I/O)         #
# --------------------------------------------------------------------------- #
#: ``(tenant, set_id) -> case_count`` for every frozen set this process has seen.
#: Populated on memory import and on any registry read; the synchronous R4 probe
#: consults it so it needs no blocking I/O.
_FROZEN_INDEX: dict[tuple[str, str], int] = {}


def register_frozen_index(tenant_id: str | None, set_id: str, case_count: int) -> None:
    """Record that ``(tenant, set_id)`` is a frozen set with ``case_count`` cases."""
    if not tenant_id or not set_id:
        return
    _FROZEN_INDEX[(str(tenant_id), str(set_id))] = int(case_count)


def clear_frozen_index() -> None:
    """Drop the process-level frozen index (test helper + backend switch)."""
    _FROZEN_INDEX.clear()


def frozen_set_count(tenant_id: str | None = None) -> int:
    """Number of frozen sets currently known to this process (optionally per-tenant)."""
    if tenant_id is None:
        return len(_FROZEN_INDEX)
    return sum(1 for (t, _s) in _FROZEN_INDEX if t == str(tenant_id))


# --------------------------------------------------------------------------- #
# store protocol + backends                                                    #
# --------------------------------------------------------------------------- #
@runtime_checkable
class GoldenRegistryStore(Protocol):
    """Read/write surface for golden sets + runs (identical across backends)."""

    async def import_set(
        self, gset: GoldenSet, *, actor_role: str, require_min_cases: int = 0
    ) -> GoldenSet: ...

    async def get_set(self, tenant_id: str | None, set_id: str) -> GoldenSet | None: ...

    async def list_sets(self, tenant_id: str | None) -> list[GoldenSet]: ...

    async def latest_frozen_set(self, tenant_id: str | None) -> GoldenSet | None: ...

    async def record_run(self, run: GoldenRun) -> GoldenRun: ...

    async def list_runs(
        self, tenant_id: str | None, set_id: str | None = None
    ) -> list[GoldenRun]: ...


def _clone_set(gset: GoldenSet) -> GoldenSet:
    return copy.deepcopy(gset)


# --- memory backend -------------------------------------------------------- #
_MEM_SETS: dict[str, dict[str, GoldenSet]] = {}
_MEM_RUNS: dict[str, list[GoldenRun]] = {}


class MemoryGoldenRegistry:
    """Dict-backed registry — authoritative for the offline/memory profile."""

    async def import_set(
        self, gset: GoldenSet, *, actor_role: str, require_min_cases: int = 0
    ) -> GoldenSet:
        assert_can_write_golden(actor_role)
        tenant = str(gset.tenant_id or "")
        if not tenant:
            raise GovernanceError("tenant context required", status_code=403)
        frozen = freeze_set(gset, require_min_cases=require_min_cases)
        frozen.tenant_id = tenant
        sets = _MEM_SETS.setdefault(tenant, {})
        # A frozen set is immutable: re-importing the same name is only allowed
        # when the content hash matches (idempotent); otherwise it is refused.
        for existing in sets.values():
            if existing.name == frozen.name:
                if existing.content_hash == frozen.content_hash:
                    return _clone_set(existing)
                raise FrozenSetMutation(
                    f"已存在同名冻结集合 '{frozen.name}'（内容不同），拒绝覆盖"
                )
        sets[frozen.set_id] = _clone_set(frozen)
        register_frozen_index(tenant, frozen.set_id, frozen.case_count)
        return frozen

    async def get_set(self, tenant_id: str | None, set_id: str) -> GoldenSet | None:
        if not tenant_id:
            return None
        stored = _MEM_SETS.get(str(tenant_id), {}).get(str(set_id))
        if stored is None:
            return None
        if stored.frozen:
            verify_content_hash(stored)  # 哈希锁定：读到被篡改的冻结集即拒绝
            register_frozen_index(tenant_id, stored.set_id, stored.case_count)
        return _clone_set(stored)

    async def list_sets(self, tenant_id: str | None) -> list[GoldenSet]:
        if not tenant_id:
            return []
        rows = list(_MEM_SETS.get(str(tenant_id), {}).values())
        rows.sort(key=lambda s: s.created_at)
        for row in rows:
            if row.frozen:
                verify_content_hash(row)
                register_frozen_index(tenant_id, row.set_id, row.case_count)
        return [_clone_set(r) for r in rows]

    async def latest_frozen_set(self, tenant_id: str | None) -> GoldenSet | None:
        frozen = [s for s in await self.list_sets(tenant_id) if s.frozen]
        if not frozen:
            return None
        return max(frozen, key=lambda s: s.frozen_at or s.created_at)

    async def record_run(self, run: GoldenRun) -> GoldenRun:
        if not run.tenant_id:
            raise GovernanceError("tenant context required", status_code=403)
        _MEM_RUNS.setdefault(str(run.tenant_id), []).append(copy.deepcopy(run))
        return run

    async def list_runs(
        self, tenant_id: str | None, set_id: str | None = None
    ) -> list[GoldenRun]:
        if not tenant_id:
            return []
        rows = list(_MEM_RUNS.get(str(tenant_id), []))
        if set_id is not None:
            rows = [r for r in rows if r.set_id == str(set_id)]
        rows.sort(key=lambda r: r.created_at)
        return [copy.deepcopy(r) for r in rows]


def _reset_memory_registry() -> None:
    _MEM_SETS.clear()
    _MEM_RUNS.clear()


# --- postgres backend ------------------------------------------------------ #
class PostgresGoldenRegistry:
    """asyncpg-backed registry (migration ``027``: golden_sets/cases/runs)."""

    def __init__(self, pool: Any | None = None) -> None:
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    async def import_set(
        self, gset: GoldenSet, *, actor_role: str, require_min_cases: int = 0
    ) -> GoldenSet:
        assert_can_write_golden(actor_role)
        tenant = str(gset.tenant_id or "")
        if not tenant:
            raise GovernanceError("tenant context required", status_code=403)
        frozen = freeze_set(gset, require_min_cases=require_min_cases)
        frozen.tenant_id = tenant
        train, holdout = frozen.counts()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            dup = await conn.fetchrow(
                "SELECT id, content_hash FROM golden_sets "
                "WHERE tenant_id = $1 AND name = $2 AND frozen = TRUE",
                tenant,
                frozen.name,
            )
            if dup is not None:
                if str(dup["content_hash"]) == frozen.content_hash:
                    existing = await self.get_set(tenant, str(dup["id"]))
                    if existing is not None:
                        return existing
                raise FrozenSetMutation(
                    f"已存在同名冻结集合 '{frozen.name}'（内容不同），拒绝覆盖"
                )
            await conn.execute(
                """
                INSERT INTO golden_sets
                    (id, tenant_id, name, provenance, content_hash, case_count,
                     train_count, holdout_count, baseline_metrics, frozen,
                     frozen_at, created_at)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, TRUE, $10, $11)
                """,
                frozen.set_id,
                tenant,
                frozen.name,
                frozen.provenance,
                frozen.content_hash,
                frozen.case_count,
                train,
                holdout,
                json.dumps(frozen.baseline_metrics, ensure_ascii=False),
                frozen.frozen_at,
                frozen.created_at,
            )
            for case in frozen.cases:
                await conn.execute(
                    """
                    INSERT INTO golden_cases
                        (set_id, tenant_id, case_id, source_doc_fingerprint, run_id,
                         instruction, expected_summary, split, provenance, created_at)
                    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    frozen.set_id,
                    tenant,
                    case.case_id,
                    case.source_doc_fingerprint,
                    case.run_id or None,
                    case.instruction or None,
                    case.expected_summary or None,
                    case.split,
                    case.provenance,
                    case.created_at,
                )
        register_frozen_index(tenant, frozen.set_id, frozen.case_count)
        return frozen

    async def _load_set_row(self, conn: Any, tenant: str, row: Any) -> GoldenSet:
        case_rows = await conn.fetch(
            "SELECT case_id, source_doc_fingerprint, run_id, instruction, "
            "expected_summary, split, provenance, created_at "
            "FROM golden_cases WHERE set_id = $1::uuid AND tenant_id = $2 "
            "ORDER BY case_id",
            str(row["id"]),
            tenant,
        )
        cases = [
            GoldenCase(
                case_id=str(r["case_id"]),
                source_doc_fingerprint=str(r["source_doc_fingerprint"]),
                run_id=str(r["run_id"] or ""),
                instruction=str(r["instruction"] or ""),
                expected_summary=str(r["expected_summary"] or ""),
                split=str(r["split"] or SPLIT_TRAIN),
                provenance=str(r["provenance"]),
                created_at=r["created_at"] or utcnow(),
            )
            for r in case_rows
        ]
        baseline = row["baseline_metrics"]
        if isinstance(baseline, str):
            try:
                baseline = json.loads(baseline)
            except ValueError:
                baseline = {}
        return GoldenSet(
            set_id=str(row["id"]),
            tenant_id=tenant,
            name=str(row["name"]),
            provenance=str(row["provenance"]),
            cases=cases,
            frozen=bool(row["frozen"]),
            content_hash=str(row["content_hash"]) if row["content_hash"] else None,
            frozen_at=row["frozen_at"],
            baseline_metrics={k: float(v) for k, v in dict(baseline or {}).items()},
            created_at=row["created_at"] or utcnow(),
        )

    async def get_set(self, tenant_id: str | None, set_id: str) -> GoldenSet | None:
        if not tenant_id:
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM golden_sets WHERE id = $1::uuid AND tenant_id = $2",
                str(set_id),
                str(tenant_id),
            )
            if row is None:
                return None
            gset = await self._load_set_row(conn, str(tenant_id), row)
        if gset.frozen:
            verify_content_hash(gset)
            register_frozen_index(tenant_id, gset.set_id, gset.case_count)
        return gset

    async def list_sets(self, tenant_id: str | None) -> list[GoldenSet]:
        if not tenant_id:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM golden_sets WHERE tenant_id = $1 ORDER BY created_at",
                str(tenant_id),
            )
            sets = [await self._load_set_row(conn, str(tenant_id), r) for r in rows]
        for gset in sets:
            if gset.frozen:
                verify_content_hash(gset)
                register_frozen_index(tenant_id, gset.set_id, gset.case_count)
        return sets

    async def latest_frozen_set(self, tenant_id: str | None) -> GoldenSet | None:
        frozen = [s for s in await self.list_sets(tenant_id) if s.frozen]
        if not frozen:
            return None
        return max(frozen, key=lambda s: s.frozen_at or s.created_at)

    async def record_run(self, run: GoldenRun) -> GoldenRun:
        if not run.tenant_id:
            raise GovernanceError("tenant context required", status_code=403)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO golden_runs
                    (id, tenant_id, set_id, set_content_hash, skill_id, candidate_id,
                     train_size, holdout_size, passed, voided, leakage, metrics,
                     reason, created_at)
                VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10,
                        $11::jsonb, $12::jsonb, $13, $14)
                """,
                run.golden_run_id,
                str(run.tenant_id),
                run.set_id or None,
                run.set_content_hash,
                run.skill_id or None,
                run.candidate_id or None,
                run.train_size,
                run.holdout_size,
                run.passed,  # NULLABLE — 未测量 ⇒ NULL (红线 4)
                run.voided,
                json.dumps(run.leakage, ensure_ascii=False),
                json.dumps(run.metrics, ensure_ascii=False),
                run.reason or None,
                run.created_at,
            )
        return run

    async def list_runs(
        self, tenant_id: str | None, set_id: str | None = None
    ) -> list[GoldenRun]:
        if not tenant_id:
            return []
        pool = await self._get_pool()
        args: list[Any] = [str(tenant_id)]
        sql = "SELECT * FROM golden_runs WHERE tenant_id = $1"
        if set_id is not None:
            args.append(str(set_id))
            sql += f" AND set_id = ${len(args)}::uuid"
        sql += " ORDER BY created_at"
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        out: list[GoldenRun] = []
        for row in rows:
            leakage = row["leakage"]
            metrics = row["metrics"]
            if isinstance(leakage, str):
                leakage = json.loads(leakage)
            if isinstance(metrics, str):
                metrics = json.loads(metrics)
            out.append(
                GoldenRun(
                    golden_run_id=str(row["id"]),
                    tenant_id=str(row["tenant_id"]),
                    set_id=str(row["set_id"]) if row["set_id"] else "",
                    set_content_hash=str(row["set_content_hash"] or ""),
                    skill_id=str(row["skill_id"] or ""),
                    candidate_id=str(row["candidate_id"] or ""),
                    train_size=int(row["train_size"] or 0),
                    holdout_size=int(row["holdout_size"] or 0),
                    passed=row["passed"],
                    voided=bool(row["voided"]),
                    leakage=dict(leakage or {}),
                    metrics=dict(metrics or {}),
                    reason=str(row["reason"] or ""),
                    created_at=row["created_at"] or utcnow(),
                )
            )
        return out


# --------------------------------------------------------------------------- #
# facade                                                                       #
# --------------------------------------------------------------------------- #
_REGISTRY_CACHE: dict[str, GoldenRegistryStore] = {}


def _active_backend() -> str:
    try:
        return str(getattr(get_settings(), "storage_backend", "") or "").lower()
    except Exception:  # noqa: BLE001 — a probe must never raise
        return ""


def get_golden_registry(*, pool: Any | None = None) -> GoldenRegistryStore:
    """Return the registry for the active ``STORAGE_BACKEND`` (cached)."""
    backend = _active_backend()
    cached = _REGISTRY_CACHE.get(backend)
    if cached is not None and pool is None:
        return cached
    if backend == "memory":
        store: GoldenRegistryStore = MemoryGoldenRegistry()
    else:
        store = PostgresGoldenRegistry(pool=pool)
    if pool is None:
        _REGISTRY_CACHE[backend] = store
    return store


def reset_golden_registry() -> None:
    """Drop cached stores, the memory data and the frozen index (test helper)."""
    _REGISTRY_CACHE.clear()
    _reset_memory_registry()
    clear_frozen_index()


# --------------------------------------------------------------------------- #
# pure functional self-test (feeds INTERLOCK_PROBE — no I/O, no store mutation) #
# --------------------------------------------------------------------------- #
def self_test() -> dict[str, Any]:
    """A pure, side-effect-free self-check of the R4 capability.

    Exercises the three mechanisms the capability promises — **freeze & hash
    lock**, **leakage detection**, **write protection** — entirely on throwaway
    in-memory objects (it never touches the store, so it can run inside a
    synchronous probe). Returns ``{"ok": bool, "evidence": str, "checks": {...}}``.
    """
    checks: dict[str, bool] = {}
    try:
        # 1. freeze locks the content; mutating it breaks the hash.
        cases = [
            GoldenCase(case_id="c1", source_doc_fingerprint="sha256:" + "a" * 64, run_id="r1"),
            GoldenCase(case_id="c2", source_doc_fingerprint="sha256:" + "b" * 64, run_id="r2"),
        ]
        frozen = freeze_set(GoldenSet(tenant_id="__selftest__", name="selftest", cases=cases))
        checks["freeze_hash_lock"] = verify_content_hash(frozen) == frozen.content_hash
        tampered = replace(frozen)
        tampered.cases = [replace(c) for c in frozen.cases]
        tampered.cases[0].instruction = "篡改"
        try:
            verify_content_hash(tampered)
            checks["tamper_rejected"] = False
        except GoldenSetHashMismatch:
            checks["tamper_rejected"] = True

        # 2. holdout split ≥ 30%, isolating whole run/doc groups.
        many = [
            GoldenCase(case_id=f"k{i}", source_doc_fingerprint="sha256:" + f"{i:064x}"[:64])
            for i in range(10)
        ]
        train, holdout = split_train_holdout(many)
        checks["holdout_ge_ratio"] = len(holdout) >= math.ceil(len(many) * HOLDOUT_MIN_RATIO)

        # 3. leakage detection flips on a shared source-document fingerprint.
        from forgeflow.evaluation.leakage_guard import detect_leakage

        overlap_case = [GoldenCase(case_id="x", source_doc_fingerprint="sha256:" + "c" * 64)]
        leaking = detect_leakage(
            overlap_case, [{"id": "e1", "tags": ["source_doc:" + "c" * 64]}]
        )
        clean = detect_leakage(
            overlap_case, [{"id": "e2", "tags": ["source_doc:" + "d" * 64]}]
        )
        checks["leakage_detected"] = leaking.leaked and not clean.leaked

        # 4. write protection refuses a non-admin writer.
        try:
            assert_can_write_golden("service")
            checks["write_protected"] = False
        except GovernanceError:
            checks["write_protected"] = True
    except Exception as exc:  # noqa: BLE001 — a broken self-test fails closed
        return {"ok": False, "evidence": f"golden 能力自检异常（fail-closed）：{exc}", "checks": checks}

    ok = all(checks.values())
    if ok:
        evidence = (
            "golden 回归能力自检通过（冻结哈希锁定/篡改拒绝/holdout≥30%/泄漏检测/写保护）"
        )
    else:
        failed = ", ".join(sorted(k for k, v in checks.items() if not v))
        evidence = f"golden 能力自检未通过：{failed}"
    return {"ok": ok, "evidence": evidence, "checks": checks}
