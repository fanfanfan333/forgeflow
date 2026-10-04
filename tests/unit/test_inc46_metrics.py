"""INC46 T36 — 效果指标与端到端基准（Metrics & Benchmark）。

范围（任务书 T36 §测试）
------------------------
* **阳性**：构造 10 个有标签 run ⇒ 各指标可复算，与手算一致。
* **阴性**：0 个有标签 run ⇒ **全部指标 None**（禁止 0）；分母为 0 ⇒ None；
  基准集被修改（哈希不符）⇒ **拒绝运行**。
* **承重语义（反事实支撑）**：把 ``None`` 默认为 0 ⇒ 阴性用例必须转红
  （见 ``rate()`` 的分母 ≤ 0 分支）。
* **诚实纪律**：``UNKNOWN`` 不进成功率分母（红线 12）；未测量列一律 ``None``
  （红线 4）；租户 fail-closed（红线 5）；R8 锚点**刻意不导出** ``INTERLOCK_PROBE``
  （fail-closed，联锁保持锁死）。

Every test drives the real functions (never a re-implementation).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.auth.jwt import create_access_token
from forgeflow.benchmark import runner as bench
from forgeflow.metrics import aggregator as agg
from forgeflow.metrics import definitions as defs
from forgeflow.metrics.store import (
    BenchmarkRunRecord,
    InMemoryMetricsStore,
    MetricSnapshotRecord,
    MetricsStoreError,
    get_metrics_store,
    reset_metrics_store,
    set_metrics_store,
)
from forgeflow.outcomes.signals import (
    ACCEPTED_EXPLICIT,
    ACCEPTED_IMPLICIT,
    FAILED_SYSTEM,
    REJECTED,
    REVERTED,
    REVISED,
    UNKNOWN,
)
from forgeflow.outcomes.store import (
    InMemoryOutcomeStore,
    reset_outcome_store,
    set_outcome_store,
)
from forgeflow.runtime.orchestrator import (
    RunRecord,
    get_run_store,
    reset_run_store,
)

TENANT = "t-t36"
OTHER = "t-t36-other"


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_t36(force_memory_backend):
    """Fresh in-memory metrics / outcome / run stores per test."""
    set_metrics_store(InMemoryMetricsStore())
    set_outcome_store(InMemoryOutcomeStore())
    reset_run_store()
    yield
    reset_metrics_store()
    reset_outcome_store()
    reset_run_store()


def _r(
    task_id: str,
    label: str | None,
    *,
    rework: bool = False,
    approved: bool | None = None,
    reused: bool | None = None,
    repair_triggered: bool | None = None,
    repair_passed: bool | None = None,
    clarified: bool | None = None,
    failure: bool | None = None,
    rolled_back: bool | None = None,
    tokens: float | None = None,
    seconds: float | None = None,
    latency: float | None = None,
    kind: str = "",
    sequence: int = 0,
    version: str = "",
) -> defs.TaskRecord:
    return defs.TaskRecord(
        task_id=task_id,
        outcome_label=label,
        rework=rework,
        approved=approved,
        reused_skill=reused,
        repair_triggered=repair_triggered,
        repair_passed=repair_passed,
        clarified=clarified,
        failure_reported=failure,
        rolled_back=rolled_back,
        tokens=tokens,
        seconds=seconds,
        latency_ms=latency,
        kind=kind,
        sequence=sequence,
        version=version,
    )


def _ten_labeled_runs() -> list[defs.TaskRecord]:
    """The positive fixture — 10 runs, 9 with a rate-bearing label.

    Hand-computed expectations (see the individual asserts):
      labeled_runs = 9 (r10 is UNKNOWN ⇒ excluded, 红线 12)
      first_pass_success = 2/9      (r1, r2 are ACCEPTED_EXPLICIT, no rework)
      adoption_rate      = 6/9      (r5, r6, r9 reject)
      skill_reuse_rate   = 5/9      (r3, r4, r6, r9 reuse=false)
      self_repair_rate   = 1/2      (r3, r4 triggered; only r3 passed)
      clarification_rate = 1/9      (r8)
      failure_report_rate= 1/9      (r9)
      rollback_rate      = 1/9      (r7)
      tokens_per_task    = 450.0    ((100+…+800)/8, r9/r10 unmeasured)
      seconds_per_task   = 45.0     ((10+…+80)/8)
      latency p50 = 800, p95 = 1600
    """
    return [
        _r("r1", ACCEPTED_EXPLICIT, approved=True, reused=True,
           clarified=False, failure=False, rolled_back=False,
           tokens=100, seconds=10, latency=200),
        _r("r2", ACCEPTED_EXPLICIT, approved=True, reused=True,
           clarified=False, failure=False, rolled_back=False,
           tokens=200, seconds=20, latency=400),
        _r("r3", ACCEPTED_EXPLICIT, rework=True, approved=True, reused=False,
           repair_triggered=True, repair_passed=True,
           clarified=False, failure=False, rolled_back=False,
           tokens=300, seconds=30, latency=600),
        _r("r4", ACCEPTED_EXPLICIT, rework=True, approved=True, reused=False,
           repair_triggered=True, repair_passed=False,
           clarified=False, failure=False, rolled_back=False,
           tokens=400, seconds=40, latency=800),
        _r("r5", REJECTED, approved=False, reused=True,
           clarified=False, failure=False, rolled_back=False,
           tokens=500, seconds=50, latency=1000),
        _r("r6", REVISED, rework=True, approved=False, reused=False,
           clarified=False, failure=False, rolled_back=False,
           tokens=600, seconds=60, latency=1200),
        _r("r7", REVERTED, rework=True, approved=True, reused=True,
           clarified=False, failure=False, rolled_back=True,
           tokens=700, seconds=70, latency=1400),
        _r("r8", ACCEPTED_IMPLICIT, approved=True, reused=True,
           clarified=True, failure=False, rolled_back=False,
           tokens=800, seconds=80, latency=1600),
        _r("r9", FAILED_SYSTEM, approved=False, reused=False,
           clarified=False, failure=True, rolled_back=False),
        # UNKNOWN ⇒ not in any rate denominator (红线 12); totally unmeasured
        _r("r10", UNKNOWN),
    ]


# --------------------------------------------------------------------------- #
# 1. definitions — positive probe (recompute matches hand calculation)          #
# --------------------------------------------------------------------------- #
def test_positive_ten_labeled_runs_first_pass_success():
    records = _ten_labeled_runs()
    # 9 rate-bearing labels; 2 are ACCEPTED_EXPLICIT without rework.
    assert defs.first_pass_success(records) == pytest.approx(2 / 9)


def test_positive_ten_labeled_runs_adoption_rate():
    # approved known for r1..r9 ⇒ 6 approve (r5, r6, r9 reject).
    assert defs.adoption_rate(_ten_labeled_runs()) == pytest.approx(6 / 9)


def test_positive_ten_labeled_runs_skill_reuse_rate():
    # reused known for r1..r9 ⇒ 5 reused (r3, r4, r6, r9 not).
    assert defs.skill_reuse_rate(_ten_labeled_runs()) == pytest.approx(5 / 9)


def test_positive_ten_labeled_runs_self_repair_rate():
    # Only runs that TRIGGERED a repair form the denominator (r3, r4) ⇒ 1/2.
    assert defs.self_repair_rate(_ten_labeled_runs()) == pytest.approx(0.5)


def test_positive_ten_labeled_runs_clarification_failure_rollback_rates():
    records = _ten_labeled_runs()
    assert defs.clarification_rate(records) == pytest.approx(1 / 9)
    assert defs.failure_report_rate(records) == pytest.approx(1 / 9)
    assert defs.rollback_rate(records) == pytest.approx(1 / 9)


def test_positive_ten_labeled_runs_cost_columns_hand_calc():
    cost = defs.cost_per_task(_ten_labeled_runs())
    # tokens/seconds measured only for r1..r8 (r9/r10 have None) ⇒ 8 samples.
    assert cost["tokens_per_task"] == pytest.approx(450.0)
    assert cost["seconds_per_task"] == pytest.approx(45.0)


def test_positive_ten_labeled_runs_latency_percentiles():
    # nearest-rank over the 8 measured latencies 200..1600 step 200.
    records = _ten_labeled_runs()
    assert defs.latency_p50(records) == 800.0
    assert defs.latency_p95(records) == 1600.0


def test_compute_all_matches_individual_functions():
    records = _ten_labeled_runs()
    flat = defs.compute_all(records)
    assert flat[defs.FIRST_PASS_SUCCESS] == defs.first_pass_success(records)
    assert flat[defs.ADOPTION_RATE] == defs.adoption_rate(records)
    assert flat["cost_per_task.tokens_per_task"] == pytest.approx(450.0)
    assert flat["cost_per_task.seconds_per_task"] == pytest.approx(45.0)
    assert set(flat) == {
        defs.FIRST_PASS_SUCCESS,
        defs.ADOPTION_RATE,
        defs.SKILL_REUSE_RATE,
        defs.SELF_REPAIR_RATE,
        defs.CLARIFICATION_RATE,
        defs.FAILURE_REPORT_RATE,
        defs.ROLLBACK_RATE,
        "cost_per_task.tokens_per_task",
        "cost_per_task.seconds_per_task",
        defs.LATENCY_P50,
        defs.LATENCY_P95,
    }


# --------------------------------------------------------------------------- #
# 2. definitions — negative probe (all None, never 0)                          #
# --------------------------------------------------------------------------- #
def test_no_labeled_runs_all_metrics_none_never_zero():
    """阴性探针：0 个有标签 run ⇒ 全部指标 None（UI 显示「—」，禁止 0）。"""
    records = [_r("a", UNKNOWN), _r("b", None)]
    flat = defs.compute_all(records)
    assert all(v is None for v in flat.values()), flat
    assert defs.first_pass_success(records) is None
    assert defs.adoption_rate(records) is None
    assert defs.skill_reuse_rate(records) is None
    assert defs.self_repair_rate(records) is None
    assert defs.clarification_rate(records) is None
    assert defs.failure_report_rate(records) is None
    assert defs.rollback_rate(records) is None
    assert defs.latency_p50(records) is None
    assert defs.latency_p95(records) is None


def test_empty_input_all_metrics_none():
    flat = defs.compute_all([])
    assert set(flat) and all(v is None for v in flat.values())


def test_zero_denominator_returns_none_not_zero():
    """分母为 0 ⇒ None（这是反事实「把 None 默认为 0」要打红的那条分支）。"""
    assert defs.rate(0, 0) is None
    assert defs.rate(5, 0) is None
    assert defs.rate(0, -3) is None
    assert defs.rate(3, 4) == pytest.approx(0.75)


def test_unknown_label_excluded_from_success_denominator():
    """红线 12：UNKNOWN 不计入分母，也不计入分子。"""
    records = [
        _r("ok", ACCEPTED_EXPLICIT),
        _r("u1", UNKNOWN),
        _r("u2", UNKNOWN),
        _r("none", None),
    ]
    # 1 success over 1 labeled run — NOT over 4.
    assert defs.first_pass_success(records) == pytest.approx(1.0)


def test_revised_and_reverted_labels_count_as_labeled_but_fail_first_pass():
    records = [
        _r("a", ACCEPTED_EXPLICIT),
        _r("b", REVISED),
        _r("c", REVERTED),
        _r("d", REJECTED),
    ]
    assert defs.first_pass_success(records) == pytest.approx(0.25)


def test_unmeasured_columns_do_not_leak_into_measured_columns():
    """逐列独立：tokens 全空不影响 seconds / latency 的复算。"""
    records = [
        _r("a", ACCEPTED_EXPLICIT, tokens=None, seconds=10, latency=100),
        _r("b", ACCEPTED_EXPLICIT, tokens=None, seconds=30, latency=300),
    ]
    cost = defs.cost_per_task(records)
    assert cost["tokens_per_task"] is None
    assert cost["seconds_per_task"] == pytest.approx(20.0)
    assert defs.latency_p50(records) == 100.0


def test_self_repair_rate_none_when_no_repair_triggered():
    records = [_r("a", ACCEPTED_EXPLICIT, repair_triggered=False)]
    assert defs.self_repair_rate(records) is None


def test_cost_per_task_ignores_zeros_vs_none():
    """显式 0 token 是「测到的 0」，None 是「未测到」——两者都不该被平均成假值。"""
    records = [
        _r("a", ACCEPTED_EXPLICIT, tokens=0.0),
        _r("b", ACCEPTED_EXPLICIT, tokens=100.0),
        _r("c", ACCEPTED_EXPLICIT, tokens=None),
    ]
    assert defs.cost_per_task(records)["tokens_per_task"] == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# 3. aggregator — snapshot / learning curve / version comparison               #
# --------------------------------------------------------------------------- #
def test_aggregate_empty_snapshot_all_none():
    snap = agg.aggregate([], tenant_id=TENANT)
    assert snap.labeled_runs == 0
    assert snap.total_tasks == 0
    assert all(v is None for v in snap.metrics.values())


def test_aggregate_counts_labeled_runs_excluding_unknown():
    snap = agg.aggregate(_ten_labeled_runs(), tenant_id=TENANT)
    assert snap.total_tasks == 10
    assert snap.labeled_runs == 9


def test_snapshot_to_dict_preserves_none_as_null():
    snap = agg.aggregate([_r("a", UNKNOWN)], tenant_id=TENANT)
    payload = snap.to_dict()
    # JSON round-trip must keep null (never 0).
    restored = json.loads(json.dumps(payload))
    assert restored["metrics"][defs.FIRST_PASS_SUCCESS] is None
    assert "null" in json.dumps(payload["metrics"])


def test_learning_curve_nth_vs_first_cost_drops_and_success_holds():
    records = [
        _r("k1", ACCEPTED_EXPLICIT, kind="invoice-edit", sequence=1,
           tokens=300, seconds=30),
        _r("k2", ACCEPTED_EXPLICIT, kind="invoice-edit", sequence=2,
           tokens=200, seconds=20),
        _r("k3", ACCEPTED_EXPLICIT, kind="invoice-edit", sequence=3,
           tokens=100, seconds=10),
    ]
    curves = agg.learning_curve(records)
    assert set(curves) == {"invoice-edit"}
    points = curves["invoice-edit"]
    assert [p["sequence"] for p in points] == [1, 2, 3]
    # Cumulative first_pass_success stays 1.0; cost per task strictly falls.
    assert all(p["first_pass_success"] == pytest.approx(1.0) for p in points)
    assert points[0]["tokens_per_task"] == pytest.approx(300.0)
    assert points[2]["tokens_per_task"] == pytest.approx(200.0)
    assert points[2]["tokens_per_task"] < points[0]["tokens_per_task"]


def test_learning_curve_ignores_records_without_kind_or_sequence():
    records = [
        _r("x", ACCEPTED_EXPLICIT, kind="", sequence=1),
        _r("y", ACCEPTED_EXPLICIT, kind="edit", sequence=0),
    ]
    assert agg.learning_curve(records) == {}


def test_version_comparison_delta_only_when_both_measured():
    treatment = [_r("t1", ACCEPTED_EXPLICIT, version="v1.1")]
    control = [_r("c1", REJECTED, version="v1.0")]
    out = agg.version_comparison(treatment, control)
    assert out["treatment"][defs.FIRST_PASS_SUCCESS] == pytest.approx(1.0)
    assert out["control"][defs.FIRST_PASS_SUCCESS] == pytest.approx(0.0)
    assert out["delta"][defs.FIRST_PASS_SUCCESS] == pytest.approx(1.0)
    # No latency measured on either side ⇒ delta stays None (never 0).
    assert out["delta"][defs.LATENCY_P50] is None


# --------------------------------------------------------------------------- #
# 4. aggregator — R8 regression gate                                            #
# --------------------------------------------------------------------------- #
def test_regression_gate_blocks_on_drop_beyond_threshold():
    verdict = agg.regression_gate(
        {defs.FIRST_PASS_SUCCESS: 0.70}, {defs.FIRST_PASS_SUCCESS: 0.80}
    )
    assert verdict.blocked is True
    assert verdict.threshold_pp == pytest.approx(3.0)
    regressed = [d for d in verdict.deltas if d.regressed]
    assert [d.metric for d in regressed] == [defs.FIRST_PASS_SUCCESS]


def test_regression_gate_does_not_block_small_drop():
    verdict = agg.regression_gate(
        {defs.FIRST_PASS_SUCCESS: 0.79}, {defs.FIRST_PASS_SUCCESS: 0.80}
    )
    assert verdict.blocked is False
    assert verdict.deltas[0].regressed is False


def test_regression_gate_exact_boundary_is_not_a_regression():
    # 1.0 → 0.75 is exactly 25pp; threshold 25pp ⇒ not "beyond" the threshold.
    verdict = agg.regression_gate(
        {defs.FIRST_PASS_SUCCESS: 0.75},
        {defs.FIRST_PASS_SUCCESS: 1.0},
        threshold_pp=25.0,
    )
    assert verdict.blocked is False


def test_regression_gate_lower_is_better_rise_blocks():
    verdict = agg.regression_gate(
        {defs.ROLLBACK_RATE: 0.10}, {defs.ROLLBACK_RATE: 0.05}
    )
    assert verdict.blocked is True


def test_regression_gate_unjudged_when_either_side_is_none():
    verdict = agg.regression_gate(
        {defs.FIRST_PASS_SUCCESS: 0.5}, {defs.FIRST_PASS_SUCCESS: None}
    )
    assert verdict.blocked is False
    assert defs.FIRST_PASS_SUCCESS in verdict.unjudged


def test_regression_gate_only_report_core_metrics_by_default():
    verdict = agg.regression_gate(
        {defs.LATENCY_P50: 9999.0}, {defs.LATENCY_P50: 1.0}
    )
    # latency is not in CORE_METRICS ⇒ untouched (no verdict either way).
    assert verdict.deltas == ()
    assert defs.LATENCY_P50 not in verdict.unjudged


def test_gate_verdict_to_dict_shapes():
    payload = agg.regression_gate(
        {defs.FIRST_PASS_SUCCESS: 0.70}, {defs.FIRST_PASS_SUCCESS: 0.80}
    ).to_dict()
    assert payload["blocked"] is True
    assert payload["threshold_pp"] == pytest.approx(3.0)
    assert isinstance(payload["deltas"], list)
    assert isinstance(payload["unjudged"], list)


# --------------------------------------------------------------------------- #
# 5. store — tenant fail-closed + persistence round-trip                        #
# --------------------------------------------------------------------------- #
def test_store_rejects_write_without_tenant():
    store = get_metrics_store()
    with pytest.raises(MetricsStoreError):
        store.add_snapshot(MetricSnapshotRecord(tenant_id=""))
    with pytest.raises(MetricsStoreError):
        store.add_benchmark_run(BenchmarkRunRecord(tenant_id="", corpus_hash="x"))


def test_store_tenant_fail_closed_reads_empty():
    store = get_metrics_store()
    assert store.latest_snapshot(None) is None
    assert store.list_snapshots("") == []
    assert store.latest_benchmark_run(None) is None
    assert store.list_benchmark_runs(None) == []


def test_store_snapshot_roundtrip_and_latest_is_newest():
    store = get_metrics_store()
    store.add_snapshot(
        MetricSnapshotRecord(tenant_id=TENANT, metrics={defs.FIRST_PASS_SUCCESS: 0.5})
    )
    store.add_snapshot(
        MetricSnapshotRecord(tenant_id=TENANT, metrics={defs.FIRST_PASS_SUCCESS: 0.9})
    )
    latest = store.latest_snapshot(TENANT)
    assert latest is not None
    assert latest.metrics[defs.FIRST_PASS_SUCCESS] == pytest.approx(0.9)
    assert len(store.list_snapshots(TENANT)) == 2


def test_store_isolates_tenants():
    store = get_metrics_store()
    store.add_snapshot(MetricSnapshotRecord(tenant_id=OTHER, metrics={}))
    assert store.list_snapshots(TENANT) == []
    assert len(store.list_snapshots(OTHER)) == 1


def test_store_benchmark_run_roundtrip_preserves_null_report_values():
    store = get_metrics_store()
    store.add_benchmark_run(
        BenchmarkRunRecord(
            tenant_id=TENANT,
            corpus_hash="deadbeef",
            total_cases=52,
            passed=52,
            report={"by_category": {"clarify": {"passed": 12}}},
        )
    )
    latest = store.latest_benchmark_run(TENANT)
    assert latest is not None
    assert latest.corpus_hash == "deadbeef"
    assert latest.to_dict()["report"]["by_category"]["clarify"]["passed"] == 12


# --------------------------------------------------------------------------- #
# 6. benchmark — frozen corpus, integrity refusal, rule-derived verdicts        #
# --------------------------------------------------------------------------- #
def test_frozen_corpus_hash_matches_and_contract_is_satisfied():
    cases = bench.load_corpus()
    stats = bench.corpus_stats(cases)
    assert bench.canonical_sha256(cases) == bench.FROZEN_CORPUS_SHA256
    assert stats["satisfied"] is True, stats["contract"]
    assert stats["total"] >= bench.MIN_TOTAL_CASES
    assert stats["by_category"]["clarify"] >= bench.MIN_CLARIFY_CASES
    assert stats["reject_or_degrade"] >= bench.MIN_REJECT_OR_DEGRADE_CASES
    assert stats["by_category"]["injection"] >= bench.MIN_INJECTION_CASES


def test_runner_all_cases_pass_on_clean_corpus():
    report = bench.run_benchmark()
    assert report.errors == 0
    assert report.failed == 0
    assert report.passed == report.total_cases
    assert report.all_passed is True
    # Per-category pass matrix is complete — every category fully green.
    for name, row in report.by_category.items():
        assert row["passed"] == row["total"], (name, row)


def test_corpus_modified_hashes_differ_and_run_is_refused(tmp_path: Path):
    """阴性探针：基准集被修改（哈希不符）⇒ 拒绝运行。"""
    cases = json.loads(Path(bench.CORPUS_PATH).read_text(encoding="utf-8"))
    cases[0]["expect"] = "degrade"  # tamper one assertion
    tampered = tmp_path / "e2e_corpus.json"
    tampered.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    assert bench.canonical_sha256(cases) != bench.FROZEN_CORPUS_SHA256
    with pytest.raises(bench.CorpusIntegrityError):
        bench.load_corpus(tampered)
    with pytest.raises(bench.CorpusIntegrityError):
        bench.run_benchmark(path=tampered)


def test_corpus_load_without_expected_hash_is_allowed_for_diagnostics():
    cases = bench.load_corpus(expected_sha256=None)
    assert len(cases) == bench.corpus_stats(cases)["total"]


def test_verdict_is_derived_from_rules_not_echoed_from_expect():
    """反事实（结构性）：改掉用例的 ``expect``，派生结论**不变**。"""
    cases = json.loads(Path(bench.CORPUS_PATH).read_text(encoding="utf-8"))
    injection_case = next(c for c in cases if c["category"] == "injection")
    derived, evidence = bench.derive_verdict(injection_case)
    assert derived == "reject"
    assert evidence["rule"] == "document_injection"

    tampered = dict(injection_case)
    tampered["expect"] = "edit"  # lie about the label
    derived_after, _ = bench.derive_verdict(tampered)
    assert derived_after == "reject"  # the rule still says reject


def test_verdict_changes_when_the_instruction_content_changes():
    """反事实（承重）：把危险指令换成良性指令 ⇒ 派生结论随之改变。"""
    cases = json.loads(Path(bench.CORPUS_PATH).read_text(encoding="utf-8"))
    reject_case = next(c for c in cases if c["id"] == "e2e-r001")
    assert bench.derive_verdict(reject_case)[0] == "reject"

    benign = dict(reject_case)
    # "期限" is not a dangerous object; "金额" would trip the amount_override
    # rule, so the benign rewrite deliberately targets the term length instead.
    benign["instruction"] = "把第 2 条的期限改为 12 个月。"
    benign["target_region"] = {"start": 1, "end": 1}
    assert bench.derive_verdict(benign)[0] == "edit"


def test_injection_cases_are_caught_by_the_real_detector():
    cases = bench.load_corpus()
    injections = [c for c in cases if c["category"] == "injection"]
    assert len(injections) >= bench.MIN_INJECTION_CASES
    for case in injections:
        derived, evidence = bench.derive_verdict(case)
        assert derived == "reject"
        assert evidence["rule"] == "document_injection"
        assert evidence["reasons"]  # a real tag, not an empty verdict


def test_pdf_inplace_degrades_while_docx_sibling_edits():
    cases = bench.load_corpus()
    pdf = next(c for c in cases if c["category"] == "degrade")

    # --- 结果：PDF 原位编辑 ⇒ 诚实降级（unsupported），不是假装成功 --- #
    derived, evidence = bench.derive_verdict(pdf)
    assert derived == "degrade"
    assert evidence["rule"] == "unsupported_inplace_format"
    assert evidence["format"] == "pdf"

    # --- 控制：同样内容换成平台可原位编辑的格式 ⇒ 能力分支不再触发 --- #
    docx = dict(pdf)
    docx["doc_format"] = "docx"
    docx["instruction"] = "把第 2 条的期限改为 12 个月。"
    docx["target_region"] = {"start": 2, "end": 2}
    derived_docx, evidence_docx = bench.derive_verdict(docx)
    assert derived_docx == "edit"
    assert evidence_docx["rule"] != "unsupported_inplace_format"

    # --- 反事实：把「已给具体新值」的同一指令退回 PDF ⇒ 又降级 --- #
    still_pdf = dict(docx)
    still_pdf["doc_format"] = "pdf"
    assert bench.derive_verdict(still_pdf)[0] == "degrade"


def test_underspecified_instruction_requires_clarification():
    case = {
        "id": "tmp",
        "category": "clarify",
        "expect": "clarify",
        "doc_format": "docx",
        "doc": "合同\n第 1 条 价格\n第 2 条 期限",
        "instruction": "把金额改一下。",
        "target_region": {"start": 1, "end": 1},
        "invariants": [],
        "tags": [],
    }
    derived, evidence = bench.derive_verdict(case)
    assert derived == "clarify"
    assert evidence["rule"] == "no_concrete_value"


def test_report_to_dict_is_json_serialisable():
    payload = bench.run_benchmark().to_dict()
    restored = json.loads(json.dumps(payload, ensure_ascii=False))
    assert restored["corpus_hash"] == bench.FROZEN_CORPUS_SHA256
    assert restored["passed"] == restored["total_cases"]


def test_run_benchmark_generated_at_is_injectable():
    from datetime import datetime, timezone

    fixed = datetime(2026, 10, 4, 9, 0, 0, tzinfo=timezone.utc)
    report = bench.run_benchmark(generated_at=fixed)
    assert report.to_dict()["generated_at"] == fixed.isoformat()


def test_clean_corpus_injection_false_positive_rate_within_spec_ceiling():
    """T36 校准输入：§十「注入检测误报率上限 ≤ 2%（干净语料）」的**实测**值。

    冻结语料里**非注入**用例的文档（46 篇）构成干净语料；误报率必须落在 §十
    上限内。这一条是 T36 报告能真正回填 §十 的那一行（其余行依赖生产基线周期，
    未满 ⇒ 如实标「未校准」，不臆造数值）。
    """
    from forgeflow.security.injection_detector import (
        InjectionClassifier,
        clean_corpus_false_positive_rate,
        detect_injection,
    )

    cases = bench.load_corpus()
    clean_docs = [c["doc"] for c in cases if c["category"] != "injection"]
    assert len(clean_docs) >= 40, len(clean_docs)

    measured = clean_corpus_false_positive_rate(clean_docs)
    assert measured <= 0.02, f"干净语料误报率 {measured:.4f} 超出 §十 上限 2%"

    # 逐篇复算：上限是「≤2%」这条约束本身，不是把 0 当默认值。
    flagged = [
        c["id"]
        for c in cases
        if c["category"] != "injection" and detect_injection(c["doc"]).flagged
    ]
    assert flagged == [], flagged

    # 校准输入边界（诚实）：干净语料在全阈值带上都是 0 误报 ⇒ 它**不能**单独选定
    # 阈值，故 `DEFAULT_SCORE_THRESHOLD` 维持 0.6 并登记「未校准」。
    for threshold in (0.5, 0.6, 0.7, 0.8):
        classifier = InjectionClassifier(threshold=threshold)
        assert sum(1 for d in clean_docs if classifier.score(d) >= threshold) == 0


# --------------------------------------------------------------------------- #
# 7. R8 anchor — capability landed, interlock deliberately still UNMET          #
# --------------------------------------------------------------------------- #
def test_r8_anchor_module_exists_but_exposes_no_interlock_probe():
    import forgeflow.evaluation.effect_benchmarks as anchor

    assert callable(anchor.evaluate_effect)
    # fail-closed: exporting INTERLOCK_PROBE would flip R8 to met prematurely.
    assert not hasattr(anchor, "INTERLOCK_PROBE")


def test_publish_interlock_r8_is_still_unmet():
    from forgeflow.skills.publish_interlock import evaluate_interlock

    status = evaluate_interlock(TENANT)
    assert "R8" in status.missing
    assert status.released is False


def test_evaluate_effect_without_baseline_has_no_gate():
    import forgeflow.evaluation.effect_benchmarks as anchor

    out = anchor.evaluate_effect(_ten_labeled_runs(), None, tenant_id=TENANT)
    assert out["gate"] is None
    assert out["snapshot"].metrics[defs.FIRST_PASS_SUCCESS] == pytest.approx(2 / 9)


def test_evaluate_effect_with_baseline_blocks_on_regression():
    import forgeflow.evaluation.effect_benchmarks as anchor

    out = anchor.evaluate_effect(
        [_r("z", REJECTED)],
        {"metrics": {defs.FIRST_PASS_SUCCESS: 0.9}},
        tenant_id=TENANT,
    )
    assert out["gate"] is not None
    assert out["gate"].blocked is True


# --------------------------------------------------------------------------- #
# 8. API — /metrics/outcomes and /metrics/benchmark/latest                      #
# --------------------------------------------------------------------------- #
def _client_and_headers() -> tuple[TestClient, dict[str, str]]:
    token = create_access_token(user_id="u-t36", role="manager", workspace_id=TENANT)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_api_outcomes_with_no_runs_all_null_and_has_data_false():
    client, headers = _client_and_headers()
    resp = client.get("/metrics/outcomes", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == TENANT
    assert body["has_data"] is False
    assert body["labeled_runs"] == 0
    assert body["total_runs"] == 0
    assert all(v is None for v in body["metrics"].values()), body["metrics"]


def test_api_outcomes_recomputes_from_real_runs():
    from forgeflow.outcomes.store import get_outcome_store

    run_store = get_run_store()
    outcome_store = get_outcome_store()

    labels = {ACCEPTED_EXPLICIT: 2, REJECTED: 1, FAILED_SYSTEM: 1}
    idx = 0
    for label, count in labels.items():
        for _ in range(count):
            idx += 1
            run_id = f"api-run-{idx}"
            run_store.save(
                RunRecord(
                    run_id=run_id,
                    thread_id=run_id,
                    tenant_id=TENANT,
                    agent_id=None,
                    intent="edit",
                    status="completed",
                    outcome="succeeded",
                    steps=[],
                    errors=[],
                    created_at="2026-10-04T10:00:00+00:00",
                    completed_at="2026-10-04T10:00:02+00:00",
                    total_tokens=100,
                )
            )
            outcome_store.upsert_outcome(TENANT, run_id, label)

    client, headers = _client_and_headers()
    body = client.get("/metrics/outcomes", headers=headers).json()
    assert body["total_runs"] == 4
    assert body["labeled_runs"] == 4
    assert body["has_data"] is True
    # 2 of 4 explicit accepts ⇒ 0.5; latency measured (2s) on every run.
    assert body["metrics"][defs.FIRST_PASS_SUCCESS] == pytest.approx(0.5)
    assert body["metrics"][defs.LATENCY_P50] == pytest.approx(2000.0)


def test_api_outcomes_is_tenant_scoped():
    get_run_store().save(
        RunRecord(
            run_id="other-run",
            thread_id="other-run",
            tenant_id=OTHER,
            agent_id=None,
            intent="edit",
            status="completed",
            outcome="succeeded",
            steps=[],
            errors=[],
            created_at="2026-10-04T10:00:00+00:00",
            completed_at="2026-10-04T10:00:01+00:00",
        )
    )
    client, headers = _client_and_headers()
    body = client.get("/metrics/outcomes", headers=headers).json()
    assert body["total_runs"] == 0
    assert body["has_data"] is False


def test_api_benchmark_latest_reports_frozen_corpus_live_matrix():
    client, headers = _client_and_headers()
    resp = client.get("/metrics/benchmark/latest", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == TENANT
    assert body["corpus"]["frozen_hash"] == bench.FROZEN_CORPUS_SHA256
    assert body["corpus"]["contract_satisfied"] is True
    assert body["live"]["available"] is True
    assert body["live"]["passed"] == body["live"]["total_cases"]
    assert body["has_persisted"] is False
    assert body["latest"] is None


def test_api_benchmark_latest_surfaces_persisted_run():
    get_metrics_store().add_benchmark_run(
        BenchmarkRunRecord(
            tenant_id=TENANT, corpus_hash=bench.FROZEN_CORPUS_SHA256,
            total_cases=52, passed=52,
        )
    )
    client, headers = _client_and_headers()
    body = client.get("/metrics/benchmark/latest", headers=headers).json()
    assert body["has_persisted"] is True
    assert body["latest"]["corpus_hash"] == bench.FROZEN_CORPUS_SHA256
