"""INC46 T16 — Outcome signal: label derivation, success-rate arithmetic,
Miner admission and the feedback API.

Scope (阳性 / 阴性 / 反事实 / 红线)
---------------------------------
* **阳性** — 用户批准 ⇒ ``ACCEPTED_EXPLICIT`` 且进入 Miner 成功样本；
  窗口内对同一目标再次修改 ⇒ ``REVISED`` 且进入失败模式。
* **阴性** — ① 无任何信号 ⇒ ``UNKNOWN``，不进分子/分母；② 全 UNKNOWN ⇒
  成功率 ``None``（红线 4）；③ 跨租户反馈 ⇒ ``403``（红线 5）；
  ④ 重复提交同一幂等键 ⇒ 不重复计数。
* **反事实（真跑）** — :func:`test_unknown_never_enters_miner_success_samples`
  是登记的红证目标：摘掉 ``pattern_miner.mine_patterns`` 里的准入过滤后它必须转红，
  证明「UNKNOWN 不进 Miner」是**承重**的（红线 12）。
* **红线 4** — 未测量 ⇒ ``None``，绝不写 ``0``；``hard_pass=None`` 不得走隐式接受。
* **红线 12** — 未知结果不等于成功。

Every test drives the real functions (never a re-implementation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.outcomes.labeler import derive_label
from forgeflow.outcomes.signals import (
    ACCEPTED_EXPLICIT,
    ACCEPTED_IMPLICIT,
    FAILED_SYSTEM,
    REJECTED,
    REVISED,
    UNKNOWN,
    is_miner_negative,
    is_miner_positive,
    label_distribution,
    miner_sample_ids,
    success_rate,
)
from forgeflow.outcomes.store import (
    InMemoryOutcomeStore,
    reset_outcome_store,
    set_outcome_store,
)
from forgeflow.skills.pattern_miner import mine_patterns

_TENANT = "t-inc46-t16"
_OTHER = "t-inc46-t16-other"
_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _fresh_store():
    """Pin a fresh in-memory outcome store per test (no DB, fully offline)."""
    set_outcome_store(InMemoryOutcomeStore())
    yield
    reset_outcome_store()


def _run(run_id: str) -> list[dict]:
    """One synthetic run: a single successful step (T02 miner input shape)."""
    return [
        {
            "tool": "docs.parse",
            "status": "ok",
            "run_id": run_id,
            "index": 0,
            "attempt": 1,
            "output": {"payload": {"summary": "x"}},
        }
    ]


# --------------------------------------------------------------------------- #
# 标签推导                                                                     #
# --------------------------------------------------------------------------- #
def test_approval_yields_accepted_explicit() -> None:
    """阳性① — 用户批准 ⇒ ``ACCEPTED_EXPLICIT``。"""
    label = derive_label(feedback_kinds=["approval"])
    assert label == ACCEPTED_EXPLICIT
    assert is_miner_positive(label)


def test_revise_yields_revised_and_is_a_failure_mode() -> None:
    """阳性② — 再次修改 ⇒ ``REVISED``，且作为**失败模式**（负样本）。"""
    label = derive_label(feedback_kinds=["revise"])
    assert label == REVISED
    assert is_miner_negative(label)
    assert not is_miner_positive(label)


def test_no_signal_yields_unknown() -> None:
    """阴性① — 无任何信号 ⇒ ``UNKNOWN``，两侧都不进。"""
    label = derive_label()
    assert label == UNKNOWN
    assert not is_miner_positive(label)
    assert not is_miner_negative(label)


def test_failed_validation_yields_failed_system() -> None:
    """系统判定的客观失败，无需用户信号。"""
    assert derive_label(run_status="failed_validation") == FAILED_SYSTEM


def test_explicit_reject_beats_system_pass() -> None:
    """裁定 Y1 — 显式用户信号盖过系统的正向推断。"""
    assert (
        derive_label(run_status="succeeded", feedback_kinds=["reject"], hard_pass=True)
        == REJECTED
    )


def test_hard_pass_before_window_is_unknown() -> None:
    """窗口**未满** ⇒ ``UNKNOWN`` —— 不得把「还没来得及返工」冒充「认可」。"""
    verified = _NOW - timedelta(hours=23)
    label = derive_label(hard_pass=True, verified_at=verified, now=_NOW)
    assert label == UNKNOWN


def test_hard_pass_after_window_yields_accepted_implicit() -> None:
    """窗口已满且无返工 ⇒ ``ACCEPTED_IMPLICIT``（仅统计，不进 Miner 成功样本）。"""
    verified = _NOW - timedelta(hours=25)
    label = derive_label(hard_pass=True, verified_at=verified, now=_NOW)
    assert label == ACCEPTED_IMPLICIT
    assert not is_miner_positive(label)  # A10：默认不纳入


def test_hard_pass_none_never_yields_implicit() -> None:
    """红线 4 — ``hard_pass`` 未测量 ⇒ **绝不**走隐式接受。"""
    verified = _NOW - timedelta(hours=100)
    assert derive_label(hard_pass=None, verified_at=verified, now=_NOW) == UNKNOWN


def test_rework_within_window_blocks_implicit_acceptance() -> None:
    """窗口内出现返工 ⇒ 不构成隐式接受。"""
    verified = _NOW - timedelta(hours=100)
    assert (
        derive_label(
            feedback_kinds=["revise"], hard_pass=True, verified_at=verified, now=_NOW
        )
        == REVISED
    )


# --------------------------------------------------------------------------- #
# 成功率算术（红线 4 / 12）                                                     #
# --------------------------------------------------------------------------- #
def test_unknown_excluded_from_numerator_and_denominator() -> None:
    """阴性① — ``UNKNOWN`` 既不进分子也不进分母。"""
    labels = [ACCEPTED_EXPLICIT, UNKNOWN, UNKNOWN]
    assert success_rate(labels) == 1.0  # 1/1，不是 1/3


def test_all_unknown_rate_is_none_not_zero() -> None:
    """阴性② — 全部 UNKNOWN ⇒ ``None``（红线 4：绝不写 0）。"""
    rate = success_rate([UNKNOWN, UNKNOWN, None])
    assert rate is None
    assert rate != 0


def test_success_rate_counts_implicit_as_accepted() -> None:
    """统计口径：显式与隐式都算「接受」（但只有显式进 Miner）。"""
    assert success_rate([ACCEPTED_EXPLICIT, ACCEPTED_IMPLICIT, REJECTED]) == pytest.approx(2 / 3)


def test_label_distribution_always_has_all_keys() -> None:
    """分布样例恒含全部八个标签，缺者为 0（便于跨 run 集比较）。"""
    dist = label_distribution([ACCEPTED_EXPLICIT, None])
    assert set(dist) >= {ACCEPTED_EXPLICIT, ACCEPTED_IMPLICIT, UNKNOWN}
    assert dist[UNKNOWN] == 1
    assert dist[ACCEPTED_IMPLICIT] == 0


# --------------------------------------------------------------------------- #
# Miner 准入（红线 12）—— 含登记的反事实目标                                     #
# --------------------------------------------------------------------------- #
def test_explicit_enters_miner_positive_samples() -> None:
    """阳性① — 显式接受进入 Miner 成功样本。"""
    positives, negatives = miner_sample_ids({"r1": ACCEPTED_EXPLICIT, "r2": REVISED})
    assert positives == ["r1"]
    assert negatives == ["r2"]


def test_implicit_does_not_enter_miner_positive_samples() -> None:
    """A10 — 隐式接受**不**进 Miner 成功样本（仅用于统计）。"""
    positives, _ = miner_sample_ids({"r1": ACCEPTED_IMPLICIT})
    assert positives == []


def test_unknown_never_enters_miner_success_samples() -> None:
    """阴性 + **反事实目标（登记）** — UNKNOWN 的 run 不得进成功样本，
    且成功率必须是 ``None`` 而不是 ``0``。

    红证：摘掉 ``pattern_miner.mine_patterns`` 的准入过滤
    （``eligible = [...]`` 这段）后本用例必须转红 —— 届时分母退回 ``support``，
    成功率会变成 ``0.0`` 而非 ``None``。
    """
    patterns = mine_patterns(
        [_run("r1")], min_support=1, outcome_labels={"r1": UNKNOWN}
    )
    metrics = patterns[0].metrics
    assert metrics.success_count == 0
    assert metrics.success_denominator == 0  # UNKNOWN 不进分母
    assert metrics.success_rate is None  # 红线 4：不是 0


def test_miner_success_rate_excludes_unknown_from_denominator() -> None:
    """分母只数**已定标签**的 run；``support``（frequency 用）不受影响。"""
    labels = {
        "r1": ACCEPTED_EXPLICIT,
        "r2": ACCEPTED_IMPLICIT,
        "r3": UNKNOWN,
        "r4": ACCEPTED_EXPLICIT,
    }
    patterns = mine_patterns(
        [_run("r1"), _run("r2"), _run("r3"), _run("r4")],
        min_support=1,
        outcome_labels=labels,
    )
    m = patterns[0].metrics
    assert m.support == 4  # frequency 分母不变
    assert m.success_denominator == 3  # UNKNOWN 被排除
    assert m.success_count == 2
    assert m.success_rate == pytest.approx(2 / 3)


def test_miner_without_labels_is_unchanged() -> None:
    """向后兼容 — 不传 ``outcome_labels`` 时与 T02 行为一致（分母 = support）。"""
    patterns = mine_patterns([_run("r1")], min_support=1)
    assert patterns[0].metrics.success_denominator is None
    assert patterns[0].metrics.to_dict()["success_denominator"] == 1


# --------------------------------------------------------------------------- #
# API（红线 5：跨租户 ⇒ 403；幂等键不重复计数）                                  #
# --------------------------------------------------------------------------- #
def _client() -> TestClient:
    """The real outcome router behind the real RBAC middleware (no stubs)."""
    from forgeflow.api.routers import run_outcomes

    minimal = FastAPI()
    minimal.add_middleware(RBACMiddleware)
    minimal.include_router(run_outcomes.router, prefix="/runs")
    return TestClient(minimal)


def _auth(tenant: str = _TENANT, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id="eng-t16", role=role, workspace_id=tenant)
    return {"Authorization": f"Bearer {token}"}


def test_feedback_approval_flows_into_outcome() -> None:
    """阳性 — 提交 approval 后，GET 回来的标签是 ``ACCEPTED_EXPLICIT``。"""
    client = _client()
    resp = client.post(
        "/runs/run-1/feedback",
        headers=_auth(),
        json={"kind": "approval", "idempotency_key": "k-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome_label"] == ACCEPTED_EXPLICIT
    assert body["created"] is True

    got = client.get("/runs/run-1/outcome", headers=_auth())
    assert got.status_code == 200
    assert got.json()["outcome_label"] == ACCEPTED_EXPLICIT
    assert got.json()["found"] is True


def test_duplicate_idempotency_key_is_not_counted_twice() -> None:
    """阴性④ — 同一幂等键重复提交 ⇒ 不重复计数。"""
    client = _client()
    payload = {"kind": "approval", "idempotency_key": "dup-key"}
    first = client.post("/runs/run-2/feedback", headers=_auth(), json=payload)
    second = client.post("/runs/run-2/feedback", headers=_auth(), json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["feedback_count"] == 1  # 仍然只有一条


def test_cross_tenant_feedback_is_403() -> None:
    """阴性③ — 跨租户反馈 ⇒ ``403``，且不得写入影子副本（红线 5）。"""
    client = _client()
    # run-3 先由 _TENANT 认领
    claim = client.post(
        "/runs/run-3/feedback",
        headers=_auth(_TENANT),
        json={"kind": "approval", "idempotency_key": "own-1"},
    )
    assert claim.status_code == 200, claim.text

    foreign = client.post(
        "/runs/run-3/feedback",
        headers=_auth(_OTHER),
        json={"kind": "reject", "idempotency_key": "foreign-1"},
    )
    assert foreign.status_code == 403, foreign.text

    # 他租户读不到该 run（不泄露）
    peek = client.get("/runs/run-3/outcome", headers=_auth(_OTHER))
    assert peek.status_code == 200
    assert peek.json()["found"] is False


def test_unknown_run_reports_found_false() -> None:
    """没有任何记录的 run ⇒ ``found=False``（不伪造一条 UNKNOWN 行）。"""
    client = _client()
    resp = client.get("/runs/does-not-exist/outcome", headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["found"] is False
    assert resp.json()["outcome_label"] is None
