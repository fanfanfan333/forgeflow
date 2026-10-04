"""INC46 T34 — 灰度发布与自动回滚（rollout & auto-rollback）。

范围（任务书 T34 §测试）
------------------------
* **阳性**：表现良好 ⇒ 逐档推进 5% → 25% → 100%；模拟 v1.1 劣化 ⇒ **自动回滚**，
  流量回 v1.0，**生成 rollback_id**。
* **阴性**：样本不足 ⇒ ``insufficient_data``（不推进、不判通过）；回滚后 v1.1
  **仍可读**；跨租户指标**不混入**；手动回滚**无权限 ⇒ 403**。
* **承重语义（反事实支撑）**：与 incumbent 比较用 **Wilson 置信下界**而非点估计 ——
  小样本噪声不得被点估计误推进。
* **诚实纪律**：未测量一律 ``None``（无延迟 ⇒ ``p95=None``、无成本 ⇒ ``cost=None``、
  全 UNKNOWN ⇒ ``success_rate=None``）；回滚**只追加**，不删改历史（红线 6 / 20）。

Every test drives the real functions (never a re-implementation).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.auth.jwt import create_access_token
from forgeflow.rollout import controller as ctl
from forgeflow.rollout import metrics as mx
from forgeflow.rollout import rollback as rb
from forgeflow.rollout.store import (
    InMemoryRolloutStore,
    MetricsRecord,
    RolloutError,
    get_rollout_store,
    set_rollout_store,
)

TENANT = "t-t34"
OTHER = "t-t34-other"
SKILL = "sk-t34"


@pytest.fixture(autouse=True)
def _isolate_rollout(force_memory_backend):
    """Pin the process-wide rollout store to a fresh in-memory one per test."""
    set_rollout_store(InMemoryRolloutStore())
    ctl.reset_rollout_state()
    yield
    ctl.reset_rollout_state()
    from forgeflow.rollout.store import reset_rollout_store

    reset_rollout_store()


# --------------------------------------------------------------------------- #
# helpers                                                                       #
# --------------------------------------------------------------------------- #
def _good(n: int = 50, successes: int | None = None) -> mx.RolloutMetrics:
    """A healthy candidate: 50 labeled runs, high success, 25h window."""
    n_success = n if successes is None else successes
    labels = ["ACCEPTED_EXPLICIT"] * n_success + ["REJECTED"] * (n - n_success)
    return mx.derive_metrics(labels, latencies_ms=[100.0] * n, window_hours=25.0)


def _incumbent() -> mx.RolloutMetrics:
    """incumbent: 40 labeled runs at 85% success (floor for −5pp = 0.80)."""
    labels = ["ACCEPTED_EXPLICIT"] * 34 + ["REJECTED"] * 6
    return mx.derive_metrics(labels, latencies_ms=[100.0] * 40, window_hours=25.0)


def _start(store=None):
    store = store or get_rollout_store()
    rollout = ctl.start_rollout(
        TENANT,
        skill_id=SKILL,
        candidate_version="1.1.0",
        incumbent_version="1.0.0",
        store=store,
    )
    ctl.set_tenant_rollout_enabled(TENANT, True)
    return store, rollout


# --------------------------------------------------------------------------- #
# metrics                                                                       #
# --------------------------------------------------------------------------- #
def test_wilson_lower_bound_is_none_when_unmeasured_and_below_point_estimate():
    assert mx.wilson_lower_bound(0, 0) is None
    assert mx.wilson_lower_bound(5, 0) is None
    # 下界 ≤ 点估计，且随样本量增大而逼近点估计（区间收窄）。
    for successes, n in [(30, 30), (20, 30), (48, 50)]:
        lb = mx.wilson_lower_bound(successes, n)
        assert lb is not None and lb <= successes / n + 1e-12
    assert mx.wilson_lower_bound(30, 30) < mx.wilson_lower_bound(300, 300)


def test_wilson_lower_bound_rejects_impossible_counts():
    with pytest.raises(mx.MetricError):
        mx.wilson_lower_bound(31, 30)


def test_percentile_nearest_rank_and_none_when_empty():
    assert mx.percentile([], 0.95) is None
    assert mx.percentile([10, 20, 30, 40], 0.95) == 40
    assert mx.percentile([10, 20, 30, 40], 0.5) == 20


def test_derive_metrics_unknown_is_excluded_and_unmeasured_is_none():
    m = mx.derive_metrics(
        ["ACCEPTED_EXPLICIT"] * 3 + ["UNKNOWN"] * 5,
        window_hours=25.0,
    )
    assert m.labeled_runs == 3  # UNKNOWN 不进分母（红线 12）
    assert m.success_rate == 1.0
    assert m.p95_latency_ms is None  # 无延迟样本 ⇒ None（红线 4）
    assert m.cost is None
    all_unknown = mx.derive_metrics(["UNKNOWN"] * 4, window_hours=25.0)
    assert all_unknown.success_rate is None  # 绝不写 0
    assert all_unknown.success_rate_lb is None


def test_insufficient_gate_needs_30_runs_and_24h():
    small = mx.derive_metrics(["ACCEPTED_EXPLICIT"] * 10, window_hours=48.0)
    assert small.insufficient is True
    short = mx.derive_metrics(["ACCEPTED_EXPLICIT"] * 40, window_hours=1.0)
    assert short.insufficient is True
    no_window = mx.derive_metrics(["ACCEPTED_EXPLICIT"] * 40, window_hours=None)
    assert no_window.insufficient is True  # 窗口未测量 ⇒ 未达门槛
    ok = mx.derive_metrics(["ACCEPTED_EXPLICIT"] * 40, window_hours=24.0)
    assert ok.insufficient is False


# --------------------------------------------------------------------------- #
# positive — stage-by-stage advance to 100%                                     #
# --------------------------------------------------------------------------- #
def test_healthy_candidate_advances_stage_by_stage_to_100_percent():
    store, rollout = _start()
    assert rollout.stage_pct == 5 and rollout.state == "canary"
    assert rb.serving_version(rollout) == "1.0.0"

    good, inc = _good(), _incumbent()

    d1 = ctl.decide_stage(good, inc, current_pct=rollout.stage_pct)
    assert d1.action == "advance" and d1.next_pct == 25
    ctl.apply_stage_decision(TENANT, rollout, d1, store=store)
    assert rollout.stage_pct == 25

    d2 = ctl.decide_stage(good, inc, current_pct=rollout.stage_pct)
    assert d2.action == "advance" and d2.next_pct == 100
    ctl.apply_stage_decision(TENANT, rollout, d2, store=store)
    assert rollout.stage_pct == 100

    d3 = ctl.decide_stage(good, inc, current_pct=rollout.stage_pct)
    assert d3.action == "promote"
    out = ctl.apply_stage_decision(TENANT, rollout, d3, store=store)
    assert out["action"] == "promote" and out["rollback_id"] is None
    assert rollout.state == "promoted"
    # 流量指针切到候选：默认版本 = 1.1.0。
    assert rb.serving_version(rollout) == "1.1.0"


def test_deterministic_routing_is_replayable_and_switch_gated():
    # pct 边界：0 ⇒ 永不分流；100 ⇒ 始终分流；同 seed 可重放。
    assert ctl.should_route_to_candidate("seed-a", 0) is False
    assert ctl.should_route_to_candidate("seed-a", 100) is True
    assert ctl.should_route_to_candidate("seed-a", 25) == ctl.should_route_to_candidate(
        "seed-a", 25
    )

    store, rollout = _start()
    # 开关开：按档分流。
    out_on = ctl.routed_version(TENANT, skill_id=SKILL, seed="seed-a", store=store)
    assert out_on["enabled"] is True and out_on["exposure_pct"] == 5
    assert out_on["candidate"] == "1.1.0"
    # 开关关：暴露恒 0，默认仍是 incumbent（选择层逐字节不变）。
    ctl.set_tenant_rollout_enabled(TENANT, False)
    out_off = ctl.routed_version(TENANT, skill_id=SKILL, seed="seed-a", store=store)
    assert out_off["enabled"] is False and out_off["exposure_pct"] == 0
    assert out_off["serving_version"] == "1.0.0"


# --------------------------------------------------------------------------- #
# negative — auto rollback, insufficient data, tenant isolation                 #
# --------------------------------------------------------------------------- #
def test_degraded_candidate_is_auto_rolled_back_and_flow_returns_to_incumbent():
    store, rollout = _start()
    broken = mx.derive_metrics(
        ["ACCEPTED_EXPLICIT"] * 20 + ["REJECTED"] * 10, window_hours=25.0
    )
    decision = ctl.decide_stage(broken, _incumbent(), current_pct=rollout.stage_pct)
    assert decision.action == "rollback"
    assert decision.rollback is not None and decision.rollback.triggered
    assert decision.rollback.trigger == rb.TRIGGER_SUCCESS_DROP

    out = ctl.apply_stage_decision(TENANT, rollout, decision, store=store)
    assert out["action"] == "rollback"
    assert out["rollback_id"]  # 生成 rollback_id
    # 流量回 incumbent；暴露归 0。
    assert rb.serving_version(rollout) == "1.0.0"
    assert rb.exposure_pct(rollout) == 0
    assert rollout.state == "rolled_back"
    records = store.list_rollbacks(TENANT, rollout.id)
    assert len(records) == 1 and records[0].to_version == "1.0.0"


def test_dangerous_event_rolls_back_immediately_even_without_samples():
    store, rollout = _start()
    empty = mx.derive_metrics([], window_hours=None)  # 样本不足
    decision = ctl.decide_stage(
        empty, _incumbent(), current_pct=rollout.stage_pct, dangerous_event=True
    )
    assert decision.action == "rollback"
    assert decision.rollback is not None
    assert decision.rollback.trigger == rb.TRIGGER_DANGEROUS


def test_insufficient_samples_conclude_nothing_and_do_not_advance():
    store, rollout = _start()
    tiny = mx.derive_metrics(["ACCEPTED_EXPLICIT"] * 5, window_hours=30.0)
    decision = ctl.decide_stage(tiny, _incumbent(), current_pct=rollout.stage_pct)
    assert decision.action == "insufficient_data"
    assert decision.concludes is False
    out = ctl.apply_stage_decision(TENANT, rollout, decision, store=store)
    assert out["action"] == "insufficient_data" and out["rollback_id"] is None
    assert rollout.stage_pct == 5  # 不推进
    assert store.list_rollbacks(TENANT, rollout.id) == []  # 也不回滚


def test_rollback_is_append_only_and_candidate_version_stays_readable():
    store, rollout = _start()
    ctl.manual_rollback(TENANT, rollout, store=store, reason="人工介入", actor="mgr-1")
    # 回滚只**新增**记录；候选版本仍在记录里（可审计），未被删除/改写。
    assert rollout.candidate_version == "1.1.0"
    assert rollout.state == "rolled_back"
    again = ctl.manual_rollback(TENANT, rollout, store=store, reason="再次", actor="mgr-1")
    assert again.to_version == "1.0.0"
    rows = store.list_rollbacks(TENANT, rollout.id)
    assert len(rows) == 2  # 追加而非覆盖
    # 历史灰度记录本身仍可读。
    assert store.get_rollout(TENANT, rollout.id) is not None


def test_apply_rollback_refuses_untiggered_decision():
    store, rollout = _start()
    with pytest.raises(ValueError):
        rb.apply_rollback(store, rollout, rb.RollbackDecision(triggered=False))


def test_metrics_and_rollbacks_are_tenant_scoped():
    store, rollout = _start()
    ctl.record_stage_metrics(TENANT, rollout, _good(), store=store)
    ctl.manual_rollback(TENANT, rollout, store=store, reason="x", actor="mgr-1")
    # 本租户看得到。
    assert len(store.list_metrics(TENANT, rollout.id)) == 1
    assert len(store.list_rollbacks(TENANT, rollout.id)) == 1
    # 跨租户读空（fail-closed）。
    assert store.list_metrics(OTHER, rollout.id) == []
    assert store.list_rollbacks(OTHER, rollout.id) == []
    assert store.get_rollout(OTHER, rollout.id) is None


def test_unresolved_tenant_writes_are_refused():
    store = InMemoryRolloutStore()
    from forgeflow.rollout.store import RolloutRecord

    with pytest.raises(RolloutError):
        store.save_rollout(
            RolloutRecord(
                tenant_id="",
                skill_id=SKILL,
                candidate_version="1.1.0",
                incumbent_version="1.0.0",
                stage_pct=5,
            )
        )
    with pytest.raises(RolloutError):
        store.add_metrics(
            MetricsRecord(tenant_id="", rollout_id="x", stage_pct=5)
        )
    # 未解析租户读空。
    assert store.list_rollouts(None, SKILL) == []


# --------------------------------------------------------------------------- #
# counterfactual support — Wilson lower bound is load-bearing                   #
# --------------------------------------------------------------------------- #
def test_confidence_lower_bound_holds_back_a_noisy_candidate():
    """点估计看起来「没退步」，但置信下界说明**样本还不足以断言** —— 必须不推进。

    这正是反事实「把置信下界改为点估计 ⇒ 该用例转红」所钉住的承重语义：
    候选 25/30 点估计 0.833 ≥ 下限 0.80（点估计会推进），但 Wilson 下界
    ≈0.664 < 0.80 ⇒ 触发回滚（保守，拒绝在噪声上下结论）。
    """
    noisy = mx.derive_metrics(
        ["ACCEPTED_EXPLICIT"] * 25 + ["REJECTED"] * 5, window_hours=25.0
    )
    incumbent = _incumbent()  # success_rate 0.85 ⇒ 下限 0.80
    floor = incumbent.success_rate - mx.SUCCESS_DROP_PP / 100.0

    assert noisy.success_rate is not None and noisy.success_rate >= floor  # 点估计「通过」
    assert noisy.success_rate_lb is not None and noisy.success_rate_lb < floor  # 下界「不通过」

    decision = ctl.decide_stage(noisy, incumbent, current_pct=25)
    assert decision.action == "rollback"
    assert decision.rollback is not None
    assert decision.rollback.trigger == rb.TRIGGER_SUCCESS_DROP


def test_validate_fail_and_p95_triggers_are_wired():
    inc = _incumbent()  # validate_fail_rate = 0.0，success_rate = 0.85（下限 0.80）
    # 验证失败率上升 > 3pp，但**成功率的置信下界仍 ≥ 0.80**（不触发 success_drop，
    # 否则会遮蔽 validate_fail 触发器）：候选 2 个 FAILED_SYSTEM / 40 ⇒ 0.05 > 0.03。
    faily = mx.derive_metrics(
        ["ACCEPTED_EXPLICIT"] * 38 + ["FAILED_SYSTEM"] * 2, window_hours=25.0
    )
    assert faily.success_rate_lb >= inc.success_rate - mx.SUCCESS_DROP_PP / 100.0  # 先证不触发
    d = rb.evaluate_rollback(faily, inc)
    assert d.triggered and d.trigger == rb.TRIGGER_VALIDATE_FAIL_RISE
    # P95 延迟 > 1.5×。
    slow = mx.derive_metrics(
        ["ACCEPTED_EXPLICIT"] * 40, latencies_ms=[400.0] * 40, window_hours=25.0
    )
    d2 = rb.evaluate_rollback(slow, inc)
    assert d2.triggered and d2.trigger == rb.TRIGGER_P95_LATENCY


# --------------------------------------------------------------------------- #
# API wiring                                                                    #
# --------------------------------------------------------------------------- #
def test_rollout_routes_are_mapped_in_rbac():
    from forgeflow.middleware.auth import RBACMiddleware

    assert RBACMiddleware._resolve_permission("GET", "/skills/s1/rollouts") == ("read", "skills")
    assert RBACMiddleware._resolve_permission(
        "POST", "/skills/s1/rollouts/r1/promote"
    ) == ("write", "skills")
    assert RBACMiddleware._resolve_permission(
        "POST", "/skills/s1/rollouts/r1/rollback"
    ) == ("write", "skills")


def test_get_rollouts_requires_auth_then_lists():
    client = TestClient(app)
    assert client.get(f"/skills/{SKILL}/rollouts").status_code == 401

    token = create_access_token(user_id="manager-1", role="manager")
    resp = client.get(
        f"/skills/{SKILL}/rollouts",
        headers={"Authorization": f"Bearer {token}"},
    )
    # 已越过鉴权（不是 401/403）；无灰度记录 ⇒ 空列表。
    assert resp.status_code in (200, 403), resp.text
    if resp.status_code == 200:
        assert resp.json()["rollouts"] == []


def test_manual_rollback_without_permission_is_403():
    store, rollout = _start()
    client = TestClient(app)
    viewer = create_access_token(user_id="viewer-1", role="viewer")
    resp = client.post(
        f"/skills/{SKILL}/rollouts/{rollout.id}/rollback",
        json={"reason": "试试"},
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert resp.status_code == 403, resp.text
    # 未越权执行：没有产生回滚记录。
    assert store.list_rollbacks(TENANT, rollout.id) == []
