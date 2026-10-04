"""INC46 T17 — 独立 Golden 回归集（Held-out Eval Registry）单测。

覆盖（任务书 T17 六问）：
* **阳性** — 冻结集被 evolution 回归**真实调用**并产生 ``golden_run_id``；回归结果可
  追溯到集合内容哈希（``set_content_hash``）。
* **阴性** — evolution 服务账号写 ``golden_*`` ⇒ 拒绝；holdout 源文档指纹与训练
  Experience 重叠 ⇒ 泄漏报告 + 作废该回归；修改已冻结集内容 ⇒ 哈希不符，拒绝。
* **反事实（套件内）** — 旁路 ``detect_leakage`` ⇒ 泄漏用例必须转红；旁路写保护 ⇒
  写入用例必须转红。（物理摘除 ``leakage_guard`` 的验证记录在 T17 汇报与
  ``_t17_counterfactual.py``。）
* **诚实** — 无冻结集 ⇒ ``ran=False``/``allowed=None``（未测量，非 0/True）；无基线
  ⇒ ``passed=None``；未解析租户 ⇒ 403。

红线 13（不伪造）：本套件**不预置任何生产 golden 用例**，只在测试内构造临时集合，
且测后清空（autouse fixture），绝不泄漏进其它用例。

每个用例都驱动**真实**函数（不重写实现）。
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.main import app
from forgeflow.auth.jwt import create_access_token
from forgeflow.evaluation import golden_registry as gr
from forgeflow.evaluation import golden_regression as grr
from forgeflow.evaluation import leakage_guard as lg
from forgeflow.evaluation.golden_regression import INTERLOCK_PROBE, run_golden_regression
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP
from forgeflow.skills import evolution_loop as el
from forgeflow.skills import publish_interlock as pi
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.evolution_loop import maybe_evolve, reset_evolution_state
from forgeflow.skills.models import SkillVersionRecord

TENANT = "tenant-golden-unit-1"


def _fp(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _case(i: int, run_id: str | None = None, fp: str | None = None) -> gr.GoldenCase:
    return gr.GoldenCase(
        case_id=f"case-{i:02d}",
        source_doc_fingerprint=fp or _fp(f"doc-{i}"),
        run_id=run_id if run_id is not None else f"run-{i}",
        instruction=f"把第 {i} 段改成草稿",
        expected_summary=f"expected-{i}",
    )


async def _import_set(
    *,
    tenant: str = TENANT,
    name: str = "golden-v1",
    cases: list[gr.GoldenCase] | None = None,
    baseline: dict[str, float] | None = None,
    actor_role: str = "admin",
    provenance: str = gr.PROVENANCE_HUMAN_CURATED,
):
    gset = gr.GoldenSet(
        tenant_id=tenant,
        name=name,
        provenance=provenance,
        cases=cases or [_case(i) for i in range(4)],
        baseline_metrics=dict(baseline or {}),
    )
    return await gr.get_golden_registry().import_set(gset, actor_role=actor_role)


@pytest.fixture(autouse=True)
def _clean_state():
    gr.reset_golden_registry()
    pi.reset_interlock_state()
    reset_evolution_state()
    yield
    gr.reset_golden_registry()
    pi.reset_interlock_state()
    reset_evolution_state()


# --------------------------------------------------------------------------- #
# 1. freeze / hash lock / provenance                                           #
# --------------------------------------------------------------------------- #
def test_freeze_locks_content_hash_and_mutation_breaks_it():
    cases = [_case(i) for i in range(3)]
    frozen = gr.freeze_set(gr.GoldenSet(tenant_id=TENANT, name="s", cases=cases))
    assert frozen.frozen is True
    assert frozen.content_hash and frozen.content_hash.startswith("sha256:")
    assert frozen.frozen_at is not None
    # Recomputing the same content is stable…
    assert gr.verify_content_hash(frozen) == frozen.content_hash

    # …and mutating the frozen content is refused (哈希锁定).
    tampered = gr.freeze_set(gr.GoldenSet(tenant_id=TENANT, name="s", cases=list(cases)))
    tampered.cases[0].instruction = "被篡改"
    with pytest.raises(gr.GoldenSetHashMismatch):
        gr.verify_content_hash(tampered)


def test_content_hash_is_order_independent():
    a = [_case(0), _case(1), _case(2)]
    b = [_case(2), _case(0), _case(1)]
    assert gr.compute_content_hash(a, gr.PROVENANCE_HUMAN_CURATED) == gr.compute_content_hash(
        b, gr.PROVENANCE_HUMAN_CURATED
    )


def test_provenance_must_be_curated_and_mined_is_rejected():
    with pytest.raises(gr.ProvenanceError):
        gr.freeze_set(
            gr.GoldenSet(tenant_id=TENANT, name="s", provenance="mined", cases=[_case(0)])
        )
    mined_case = _case(0)
    mined_case.provenance = "mined"
    with pytest.raises(gr.ProvenanceError):
        gr.freeze_set(
            gr.GoldenSet(tenant_id=TENANT, name="s", provenance=gr.PROVENANCE_HUMAN_CURATED, cases=[mined_case])
        )


def test_freeze_refuses_empty_set():
    with pytest.raises(gr.GoldenRegistryError):
        gr.freeze_set(gr.GoldenSet(tenant_id=TENANT, name="s", cases=[]))


# --------------------------------------------------------------------------- #
# 2. train / holdout split (holdout ≥ 30%, group-isolated)                     #
# --------------------------------------------------------------------------- #
def test_split_holdout_is_at_least_30_percent_and_isolates_run_groups():
    import math

    cases = [_case(i, run_id=f"run-{i // 3}") for i in range(15)]  # 5 runs × 3 cases
    train, holdout = gr.split_train_holdout(cases)
    assert len(holdout) >= math.ceil(len(cases) * gr.HOLDOUT_MIN_RATIO)
    assert len(train) + len(holdout) == len(cases)
    # No run_id straddles the split (isolation by run / source-doc fingerprint).
    train_runs = {c.run_id for c in train}
    holdout_runs = {c.run_id for c in holdout}
    assert train_runs.isdisjoint(holdout_runs)
    assert all(c.split == gr.SPLIT_HOLDOUT for c in holdout)
    assert all(c.split == gr.SPLIT_TRAIN for c in train)


# --------------------------------------------------------------------------- #
# 3. write protection — the evolution service account cannot write golden_*    #
# --------------------------------------------------------------------------- #
async def test_evolution_service_account_cannot_write_golden_sets():
    for role in ("service", "manager", "viewer", "sales_rep"):
        with pytest.raises(GovernanceError) as exc:
            await _import_set(actor_role=role)
        assert exc.value.status_code == 403, role
    # admin may import.
    frozen = await _import_set(actor_role="admin")
    assert frozen.frozen is True


def test_assert_can_write_golden_gate():
    with pytest.raises(GovernanceError):
        gr.assert_can_write_golden("service")
    with pytest.raises(GovernanceError):
        gr.assert_can_write_golden(None)
    gr.assert_can_write_golden("admin")  # no raise


# --------------------------------------------------------------------------- #
# 4. honest skip when no frozen set                                            #
# --------------------------------------------------------------------------- #
async def test_no_frozen_set_is_an_honest_skip_not_a_pass():
    assert await gr.get_golden_registry().latest_frozen_set(TENANT) is None
    result = await run_golden_regression(TENANT, skill_id="s1", candidate_id="c1")
    assert result.ran is False
    assert result.allowed is None  # 未测量 ⇒ None (红线 4)，绝非 True
    assert result.passed is None
    assert result.golden_run_id is None


async def test_unresolved_tenant_fails_closed_before_any_read():
    with pytest.raises(GovernanceError) as exc:
        await run_golden_regression(None, skill_id="s1")
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# 5. 阳性 — frozen set ⇒ a traceable golden_run_id                            #
# --------------------------------------------------------------------------- #
async def test_frozen_set_produces_a_traceable_golden_run_id():
    frozen = await _import_set(baseline={"score": 0.80}, name="golden-positive")

    result = await run_golden_regression(
        TENANT, skill_id="s1", candidate_id="c1", new_metrics={"score": 0.95}
    )
    assert result.ran is True
    assert result.golden_run_id  # a real id, not hand-constructed
    assert result.set_content_hash == frozen.content_hash
    assert result.passed is True
    assert result.voided is False
    assert result.holdout_size >= 1

    # Persisted + traceable to the set hash.
    runs = await gr.get_golden_registry().list_runs(TENANT, frozen.set_id)
    assert len(runs) == 1
    assert runs[0].golden_run_id == result.golden_run_id
    assert runs[0].set_content_hash == frozen.content_hash
    assert runs[0].passed is True


async def test_golden_run_without_baseline_is_unmeasured_not_false():
    await _import_set(baseline={}, name="golden-nobaseline")
    result = await run_golden_regression(
        TENANT, skill_id="s1", candidate_id="c1", new_metrics={"score": 0.95}
    )
    assert result.ran is True
    assert result.passed is None  # no comparable baseline ⇒ 未测量
    assert result.voided is False


# --------------------------------------------------------------------------- #
# 6. 阴性 — leakage overlaps void the candidate's regression                    #
# --------------------------------------------------------------------------- #
class _Exp:
    def __init__(self, exp_id: str, tags: list[str]):
        self.id = exp_id
        self.tags = tags
        self.reusable_steps: list[dict] = []


class _FakeExpRepo:
    def __init__(self, records):
        self.by_id = {r.id: r for r in records}
        self.saved: list = []

    async def save(self, record):
        self.saved.append(record)
        self.by_id[getattr(record, "id", "")] = record
        return record

    async def get(self, tenant, exp_id):
        return self.by_id.get(exp_id)


def test_detect_leakage_flags_a_shared_source_document():
    fp = _fp("shared-doc")
    case = _case(0, fp=fp)
    leaking = lg.detect_leakage([case], [_Exp("e1", [f"source_doc:{fp}"])])
    clean = lg.detect_leakage([case], [_Exp("e1", [f"source_doc:{_fp('other')}"])])
    assert leaking.leaked is True and leaking.overlaps[0].experience_id == "e1"
    assert clean.leaked is False


async def test_leakage_overlap_voids_the_regression_and_records_the_report():
    fp = _fp("leaked-doc")
    # A single-case set ⇒ that case is the whole holdout.
    frozen = await _import_set(cases=[_case(0, run_id="r-leak", fp=fp)], baseline={"score": 0.80})
    exp_repo = _FakeExpRepo([_Exp("e1", [f"source_doc:{fp}"])])

    result = await run_golden_regression(
        TENANT,
        skill_id="s1",
        candidate_id="c1",
        experience_ids=["e1"],
        experience_repo=exp_repo,
        new_metrics={"score": 0.99},
    )
    assert result.ran is True
    assert result.voided is True
    assert result.allowed is False
    assert result.passed is None  # 作废 ⇒ 未测量，绝非 0/False 冒充
    assert result.leakage["leaked"] is True
    assert "泄漏" in result.reason

    runs = await gr.get_golden_registry().list_runs(TENANT, frozen.set_id)
    assert runs and runs[-1].voided is True
    assert runs[-1].leakage["overlaps"][0]["experience_id"] == "e1"


# --------------------------------------------------------------------------- #
# 7. 阴性 — a tampered frozen set is refused on read                            #
# --------------------------------------------------------------------------- #
async def test_tampering_a_frozen_set_is_refused_on_read():
    frozen = await _import_set(name="golden-tamper")
    # Simulate a write that bypassed the lock by mutating the stored object.
    stored = gr._MEM_SETS[TENANT][frozen.set_id]  # noqa: SLF001 — deliberate tamper
    stored.cases[0].instruction = "越权篡改"

    with pytest.raises(gr.GoldenSetHashMismatch):
        await gr.get_golden_registry().get_set(TENANT, frozen.set_id)


# --------------------------------------------------------------------------- #
# 8. R4 interlock probe — flips only once a real frozen set exists             #
# --------------------------------------------------------------------------- #
def test_interlock_probe_is_unmet_without_a_frozen_set_and_met_with_one():
    # Fresh process: capability code present, but no golden set (红线 13) ⇒ unmet.
    probe = INTERLOCK_PROBE()
    assert probe["ok"] is False
    assert "无" in probe["evidence"] or "尚无" in probe["evidence"]
    assert pi._probe_capability(pi.REQUIREMENTS[3]).met is False  # R4 index 3


async def test_interlock_probe_flips_to_met_once_a_real_set_is_frozen():
    await _import_set(name="golden-probe")
    probe_after = INTERLOCK_PROBE()
    assert probe_after["ok"] is True
    assert pi._probe_capability(pi.REQUIREMENTS[3]).met is True
    assert probe_after["evidence"]


def test_self_test_reports_every_mechanism():
    report = gr.self_test()
    assert report["ok"] is True
    assert all(report["checks"].values()), report["checks"]


# --------------------------------------------------------------------------- #
# 9. evolution regression call path ⇒ real golden_run_id (DoD #4)              #
# --------------------------------------------------------------------------- #
class _Skill:
    id = "skill-golden-1"
    name = "golden 演示技能"
    current_version = "1.0.0"
    status = "published"


class _FakeSkillRepo:
    def __init__(self, skill, versions):
        self.skill = skill
        self.versions = versions

    async def get_skill(self, tenant, skill_id):
        return self.skill if (self.skill and self.skill.id == skill_id) else None

    async def get_version(self, tenant, skill_id, semver):
        for v in self.versions:
            if v.skill_id == skill_id and v.semver == semver:
                return v
        return None

    async def list_versions(self, tenant, skill_id):
        return list(self.versions)

    async def add_version(self, tenant, version):
        self.versions.append(version)
        return version

    async def update_skill(self, skill):
        self.skill = skill
        return skill


class _FakeCandidateRepo:
    def __init__(self, metrics):
        self.saved: list = []
        self.evaluation = SimpleNamespace(metrics=dict(metrics), verdict="pass")

    async def save_candidate(self, candidate):
        self.saved.append(candidate)
        return candidate

    async def get_candidate(self, *args):
        wanted = args[-1] if args else None
        for cand in self.saved:
            if getattr(cand, "id", None) == wanted:
                return cand
        return None

    async def get_evaluation_for(self, *args):
        return self.evaluation


class _FakePolicyRepo:
    async def save_approval(self, record):
        return record


def _candidate(experience_ids):
    return SimpleNamespace(
        id="cand-golden-1",
        status="compiled",
        name="golden 演示技能",
        domain="general",
        experience_ids=list(experience_ids),
        draft_spec={
            "prompt": "生成周报",
            "steps": ["读取数据", "渲染"],
            "tools": ["report.render"],
            "io_schema": {"input": {"t": "str"}, "output": {"p": "str"}},
        },
    )


def _drive_evolution(monkeypatch, metrics, exp_repo):
    import forgeflow.skills.candidate_compiler as cc
    import forgeflow.skills.engineering as eng

    async def _many(tenant, *, skill_tools, window_days=30):
        return [{"run_id": f"run-{i}", "modes": ["tool_error"], "steps": []} for i in range(4)]

    async def _compile(tenant, ids, mode, **kwargs):
        return _candidate(["e1", "e2"])

    async def _eng(*args, **kwargs):
        return SimpleNamespace(passed=True, degraded_reason=None)

    monkeypatch.setattr(el, "collect_skill_failures", _many)
    monkeypatch.setattr(cc, "compile_candidate", _compile)
    monkeypatch.setattr(eng, "run_engineering_loop", _eng)

    skill = _Skill()
    repos = {
        "skill_repo": _FakeSkillRepo(
            skill, [SkillVersionRecord(tenant_id=TENANT, skill_id=skill.id, semver="1.0.0", eval_score=0.80)]
        ),
        "candidate_repo": _FakeCandidateRepo(metrics),
        "experience_repo": exp_repo,
        "policy_repo": _FakePolicyRepo(),
    }
    return skill, repos


async def test_evolution_regression_calls_the_frozen_set_and_mints_a_golden_run_id(monkeypatch):
    frozen = await _import_set(baseline={"score": 0.80}, name="golden-evolve")
    # Training experiences carry fingerprints that do NOT overlap the holdout.
    exp_repo = _FakeExpRepo(
        [_Exp("e1", [f"source_doc:{_fp('train-1')}"]), _Exp("e2", [f"source_doc:{_fp('train-2')}"])]
    )
    skill, repos = _drive_evolution(monkeypatch, {"score": 0.95}, exp_repo)

    out = await maybe_evolve(TENANT, skill.id, actor="u1", **repos)

    golden = out.regression.get("golden")
    assert golden is not None, "evolution 回归处必须接入 golden 集（只读）"
    assert golden["ran"] is True
    assert golden["golden_run_id"], "必须产生真实 golden_run_id"
    assert golden["set_content_hash"] == frozen.content_hash, "结果须可追溯到集合哈希"
    assert golden["voided"] is False
    # The golden run is persisted and traceable.
    runs = await gr.get_golden_registry().list_runs(TENANT, frozen.set_id)
    assert any(r.golden_run_id == golden["golden_run_id"] for r in runs)


async def test_evolution_refuses_a_leaking_candidate(monkeypatch):
    fp = _fp("leak-into-holdout")
    await _import_set(cases=[_case(0, run_id="r-leak", fp=fp)], baseline={"score": 0.80}, name="golden-leak")
    exp_repo = _FakeExpRepo([_Exp("e1", [f"source_doc:{fp}"]), _Exp("e2", [])])
    skill, repos = _drive_evolution(monkeypatch, {"score": 0.99}, exp_repo)

    out = await maybe_evolve(TENANT, skill.id, actor="u1", **repos)

    assert out.applied is False
    assert "泄漏" in out.reason
    assert out.regression["golden"]["voided"] is True
    # incumbent untouched.
    assert skill.current_version == "1.0.0"


async def test_counterfactual_bypassing_leakage_guard_turns_the_leak_case_red(monkeypatch):
    """套件内反事实：旁路 detect_leakage ⇒ 泄漏断言必须转红（非 vacuous）。

    物理摘除 ``leakage_guard`` 的验证见 ``_t17_counterfactual.py``（文件哈希复原）。
    """
    fp = _fp("counterfactual-doc")
    await _import_set(cases=[_case(0, run_id="r-cf", fp=fp)], baseline={"score": 0.80}, name="cf")
    exp_repo = _FakeExpRepo([_Exp("e1", [f"source_doc:{fp}"])])

    def _assert_refused_by_leakage(out) -> None:
        assert out.applied is False and "泄漏" in out.reason, (
            f"泄漏用例未被拒绝（reason={out.reason!r}）"
        )

    # (1) guard in place ⇒ refusal (green).
    skill, repos = _drive_evolution(monkeypatch, {"score": 0.99}, exp_repo)
    out = await maybe_evolve(TENANT, skill.id, actor="u1", **repos)
    _assert_refused_by_leakage(out)

    # (2) bypass the guard ⇒ the very same assertion goes red.
    monkeypatch.setattr(
        grr,
        "detect_leakage",
        lambda holdout, training: lg.LeakageReport(leaked=False, reason="bypassed"),
    )
    reset_evolution_state()
    pi.reset_interlock_state()
    skill2, repos2 = _drive_evolution(monkeypatch, {"score": 0.99}, exp_repo)
    out2 = await maybe_evolve(TENANT, skill2.id, actor="u1", **repos2)
    with pytest.raises(AssertionError) as exc:
        _assert_refused_by_leakage(out2)
    assert "泄漏" in str(exc.value)


# --------------------------------------------------------------------------- #
# 10. RBAC + HTTP surface                                                       #
# --------------------------------------------------------------------------- #
def test_eval_routes_are_mapped_in_rbac():
    assert ROUTE_PERMISSION_MAP[("GET", "/eval/golden-sets")] == ("read", "skills")
    assert ROUTE_PERMISSION_MAP[("POST", "/eval/golden-sets")] == ("write", "skills")
    assert ROUTE_PERMISSION_MAP[("POST", "/eval/runs")] == ("write", "skills")


def test_import_endpoint_is_admin_only_over_http():
    client = TestClient(app)
    body = {
        "name": "http-golden",
        "provenance": "human_curated",
        "cases": [{"case_id": "c1", "source_doc_fingerprint": _fp("http-doc")}],
    }
    assert client.post("/eval/golden-sets", json=body).status_code == 401

    manager = create_access_token(user_id="manager-1", role="manager")
    assert (
        client.post(
            "/eval/golden-sets", json=body, headers={"Authorization": f"Bearer {manager}"}
        ).status_code
        == 403
    )

    admin = create_access_token(user_id="admin-1", role="admin")
    resp = client.post(
        "/eval/golden-sets", json=body, headers={"Authorization": f"Bearer {admin}"}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["frozen"] is True
    assert payload["content_hash"].startswith("sha256:")
    assert payload["case_count"] == 1


def test_import_endpoint_rejects_forbidden_provenance_over_http():
    client = TestClient(app)
    admin = create_access_token(user_id="admin-1", role="admin")
    body = {
        "name": "bad-golden",
        "provenance": "mined",
        "cases": [{"case_id": "c1", "source_doc_fingerprint": _fp("x")}],
    }
    resp = client.post(
        "/eval/golden-sets", json=body, headers={"Authorization": f"Bearer {admin}"}
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# 8. T17 收尾 —— 初始集下限必须是「可校验契约」，不是静默缺口                    #
# --------------------------------------------------------------------------- #
def test_initial_gate_reports_the_real_count_and_the_floor():
    """不足量时 meets_initial_minimum 就是 False（不折算成功、不静默通过）。"""
    small = gr.GoldenSet(tenant_id=TENANT, name="small", cases=[_case(i) for i in range(4)])
    gate = small.initial_gate()
    assert gate["initial_case_count"] == 4
    assert gate["initial_min_cases"] == gr.INITIAL_MIN_CASES
    assert gate["meets_initial_minimum"] is False

    full = gr.GoldenSet(
        tenant_id=TENANT,
        name="full",
        cases=[_case(i) for i in range(gr.INITIAL_MIN_CASES)],
    )
    assert full.initial_gate()["initial_case_count"] == gr.INITIAL_MIN_CASES
    assert full.initial_gate()["meets_initial_minimum"] is True


def test_initial_gate_is_carried_by_to_dict_additively():
    gset = gr.GoldenSet(tenant_id=TENANT, name="s", cases=[_case(i) for i in range(3)])
    payload = gset.to_dict()
    # 新键出现…
    assert payload["initial_case_count"] == 3
    assert payload["initial_min_cases"] == gr.INITIAL_MIN_CASES
    assert payload["meets_initial_minimum"] is False
    # …且既有键一个不少、语义不变（加性接入）。
    for key in (
        "set_id",
        "tenant_id",
        "name",
        "provenance",
        "frozen",
        "content_hash",
        "frozen_at",
        "case_count",
        "train_count",
        "holdout_count",
        "baseline_metrics",
        "created_at",
    ):
        assert key in payload, f"既有键 {key} 被 to_dict 丢掉了"


def test_assert_meets_initial_minimum_is_fail_closed_and_names_the_gap():
    short = gr.GoldenSet(tenant_id=TENANT, name="short", cases=[_case(i) for i in range(5)])
    with pytest.raises(gr.GoldenRegistryError) as exc:
        gr.assert_meets_initial_minimum(short)
    msg = str(exc.value)
    assert "5" in msg and str(gr.INITIAL_MIN_CASES) in msg  # 真实数 + 下限
    assert "15" in msg  # 差额，失败是显式的

    ok = gr.GoldenSet(
        tenant_id=TENANT,
        name="ok",
        cases=[_case(i) for i in range(gr.INITIAL_MIN_CASES)],
    )
    gr.assert_meets_initial_minimum(ok)  # 不抛


def test_freeze_still_allows_honest_small_batches():
    """冻结小批量真实案例仍被允许——阻塞导入会逼 ops 凑数（红线 12/13 的反面）。"""
    small = gr.GoldenSet(tenant_id=TENANT, name="batch", cases=[_case(i) for i in range(2)])
    frozen = gr.freeze_set(small)
    assert frozen.frozen is True
    assert frozen.initial_gate()["meets_initial_minimum"] is False  # 但缺口可见


def test_counterfactual_raising_the_floor_turns_the_gate_red(monkeypatch):
    """反事实：把下限提到不可能满足的值 ⇒ 今天通过的集必须转 False 且门必须拦下。

    证明 INITIAL_MIN_CASES 真的被读、真的起作用，而不是一个没人用的装饰常量。
    """
    full = gr.GoldenSet(
        tenant_id=TENANT,
        name="full",
        cases=[_case(i) for i in range(gr.INITIAL_MIN_CASES)],
    )
    assert full.initial_gate()["meets_initial_minimum"] is True  # 基线：今天是 True
    gr.assert_meets_initial_minimum(full)  # 基线：今天不抛

    monkeypatch.setattr(gr, "INITIAL_MIN_CASES", 10**9)
    assert full.initial_gate()["meets_initial_minimum"] is False  # ⇒ 转红
    with pytest.raises(gr.GoldenRegistryError):
        gr.assert_meets_initial_minimum(full)  # ⇒ 门拦下
