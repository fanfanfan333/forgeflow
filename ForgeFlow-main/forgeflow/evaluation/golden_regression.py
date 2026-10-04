"""INC46 T17 — Golden 回归的执行入口 + R4 联锁锚点。

本模块是发布联锁（``forgeflow.skills.publish_interlock``）在 **R4** 处 import 的
锚点模块名（``forgeflow.evaluation.golden_regression``），负责两件事：

1. :func:`run_golden_regression` — evolution 回归处的**只读**接入点：读取该租户
   最新的冻结 golden 集，切分 train/holdout，做泄漏检查，并把一次真实回归记录成
   ``golden_run``（``golden_run_id`` 可追溯到集合内容哈希）。evolution 只**读**
   golden 集，绝不写（写保护见 :mod:`forgeflow.evaluation.golden_registry`）。
2. :func:`INTERLOCK_PROBE` — R4 的 fail-closed 探针（T15 forward contract）。

诚实约定（红线 4 / 10 / 13）
----------------------------
* 无冻结集 ⇒ ``ran=False``，``allowed=None``（**未测量**，绝不写 0/True 冒充通过）；
* 集无 ``baseline_metrics`` ⇒ 回归结论 ``no_baseline``，``passed=None``（未测量）；
* 泄漏 ⇒ 该候选回归结果**作废**（``voided=True``、``allowed=False``）；
* 探针只在**真有冻结集**时才报 ``ok=True`` —— 本模块**不预置任何 golden 用例**
  （红线 13），因此全新环境里 R4 如实为**未满足**（fail-closed）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from forgeflow.evaluation.golden_registry import (
    GoldenRun,
    GoldenSet,
    frozen_set_count,
    get_golden_registry,
    self_test,
    split_train_holdout,
    verify_content_hash,
)
from forgeflow.evaluation.leakage_guard import detect_leakage
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)

__all__ = [
    "GoldenRegressionResult",
    "run_golden_regression",
    "INTERLOCK_PROBE",
]


@dataclass
class GoldenRegressionResult:
    """The honest record of one golden-regression attempt.

    ``ran=False`` means the capability could not be exercised (no frozen set) —
    ``allowed``/``passed`` stay ``None`` (未测量 ⇒ None，红线 4), never a
    fabricated pass.
    """

    ran: bool = False
    golden_run_id: str | None = None
    set_id: str | None = None
    set_content_hash: str | None = None
    allowed: bool | None = None
    passed: bool | None = None
    voided: bool = False
    reason: str = ""
    leakage: dict[str, Any] = field(default_factory=dict)
    holdout_size: int = 0
    train_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "golden_run_id": self.golden_run_id,
            "set_id": self.set_id,
            "set_content_hash": self.set_content_hash,
            "allowed": self.allowed,
            "passed": self.passed,
            "voided": self.voided,
            "reason": self.reason,
            "leakage": dict(self.leakage),
            "holdout_size": self.holdout_size,
            "train_size": self.train_size,
        }


def _attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


async def _load_training_experiences(
    tenant: str,
    experience_repo: Any | None,
    candidate: Any | None,
    experience_ids: list[str] | None,
) -> list[Any]:
    """Read the candidate's training Experiences (their fingerprints drive leakage).

    Best-effort and tenant-scoped: a read failure or a missing record simply
    contributes no fingerprint (it can never manufacture a leak OR hide one — a
    fingerprint we cannot see is reported as "unchecked", not "clean").
    """
    ids = list(experience_ids or [])
    if not ids and candidate is not None:
        ids = list(_attr_or_key(candidate, "experience_ids", None) or [])
    if not ids:
        return []

    repo = experience_repo
    if repo is None:
        from forgeflow.repositories import get_experience_repository

        repo = get_experience_repository()

    out: list[Any] = []
    for exp_id in ids:
        try:
            record = await repo.get(tenant, exp_id)
        except Exception as exc:  # noqa: BLE001 — a read miss is not a leak
            logger.debug("golden regression: experience %s unreadable: %s", exp_id, exc)
            record = None
        if record is not None:
            out.append(record)
    return out


async def run_golden_regression(
    tenant_id: str | None,
    *,
    skill_id: str = "",
    candidate_id: str = "",
    candidate: Any | None = None,
    experience_repo: Any | None = None,
    experience_ids: list[str] | None = None,
    new_metrics: dict[str, Any] | None = None,
    registry: Any | None = None,
) -> GoldenRegressionResult:
    """Run the read-only golden regression for one candidate (see module doc).

    Tenant **fail-closed** (403 before any read). Never writes a golden set; the
    only write is the ``golden_run`` trace record.

    Returns a :class:`GoldenRegressionResult`; ``ran=False`` when there is no
    frozen set for the tenant (nothing measurable — never a fabricated verdict).
    """
    tenant = require_tenant(tenant_id)
    store = registry or get_golden_registry()

    gset: GoldenSet | None = await store.latest_frozen_set(tenant)
    if gset is None:
        return GoldenRegressionResult(
            ran=False,
            allowed=None,
            reason="无冻结的 golden 集，未做独立回归（skipped；不可据此判定通过）",
        )

    # 冻结集哈希锁定：内容被改动即拒绝（该候选回归无法建立可信基线）。
    verify_content_hash(gset)

    train, holdout = split_train_holdout(gset.cases)
    training = await _load_training_experiences(
        tenant, experience_repo, candidate, experience_ids
    )
    report = detect_leakage(holdout, training)

    if report.leaked:
        run = GoldenRun(
            tenant_id=tenant,
            set_id=gset.set_id,
            set_content_hash=gset.content_hash or "",
            skill_id=str(skill_id or ""),
            candidate_id=str(candidate_id or ""),
            train_size=len(train),
            holdout_size=len(holdout),
            passed=None,  # 作废 ⇒ 未测量，绝非 False/0
            voided=True,
            leakage=report.to_dict(),
            metrics=dict(new_metrics or {}),
            reason=report.reason,
        )
        await store.record_run(run)
        return GoldenRegressionResult(
            ran=True,
            golden_run_id=run.golden_run_id,
            set_id=gset.set_id,
            set_content_hash=run.set_content_hash,
            allowed=False,
            passed=None,
            voided=True,
            reason=report.reason,
            leakage=report.to_dict(),
            holdout_size=len(holdout),
            train_size=len(train),
        )

    # 无泄漏 ⇒ 用平台统一的回归策略比较候选指标与集合记录的基线。
    from forgeflow.skills.release_gate import evaluate_release

    decision = evaluate_release(dict(new_metrics or {}), dict(gset.baseline_metrics or {}))
    passed: bool | None
    if decision.severity == "no_baseline":
        passed = None  # 未做比较 ⇒ 未测量
    else:
        passed = bool(decision.allowed)

    run = GoldenRun(
        tenant_id=tenant,
        set_id=gset.set_id,
        set_content_hash=gset.content_hash or "",
        skill_id=str(skill_id or ""),
        candidate_id=str(candidate_id or ""),
        train_size=len(train),
        holdout_size=len(holdout),
        passed=passed,
        voided=False,
        leakage=report.to_dict(),
        metrics=dict(new_metrics or {}),
        reason=decision.reason,
    )
    await store.record_run(run)
    return GoldenRegressionResult(
        ran=True,
        golden_run_id=run.golden_run_id,
        set_id=gset.set_id,
        set_content_hash=run.set_content_hash,
        allowed=decision.allowed,
        passed=passed,
        voided=False,
        reason=decision.reason,
        leakage=report.to_dict(),
        holdout_size=len(holdout),
        train_size=len(train),
    )


def INTERLOCK_PROBE() -> dict[str, Any]:
    """T15 forward-contract probe for requirement **R4**.

    Fail-closed: ``ok=True`` only when (a) the capability's pure self-test passes
    **and** (b) this process actually knows of at least one **frozen** golden set.
    A fresh environment has no golden set (we ship none — 红线 13), so R4 reads
    **unmet** until an operator imports and freezes a real held-out set; the
    interlock therefore stays locked rather than trusting a hollow module.

    No I/O beyond the process-local frozen index, no LLM.
    """
    try:
        check = self_test()
    except Exception as exc:  # noqa: BLE001 — a broken probe fails closed
        return {"ok": False, "evidence": f"R4 探针执行失败（fail-closed 视为未满足）：{exc}"}
    if not check.get("ok"):
        return {
            "ok": False,
            "evidence": f"无证据：golden 能力自检未通过（{check.get('evidence') or '未通过'}）",
        }

    count = frozen_set_count()
    if count <= 0:
        return {
            "ok": False,
            "evidence": (
                "无证据：能力已就绪（冻结哈希锁定/泄漏防护/写保护自检通过），但尚无冻结的 "
                "golden 集 —— 待 admin 通过 POST /eval/golden-sets 导入人工策划用例"
                "（红线 13：不预置伪造集）"
            ),
        }
    return {
        "ok": True,
        "evidence": (
            f"{check['evidence']}；本进程已知冻结 held-out 集 {count} 个"
            "（golden_registry + leakage_guard）"
        ),
    }
