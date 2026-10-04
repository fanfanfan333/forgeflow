"""INC46 T19 — 种子 Skill 与冷启动（seed skills + cold start）.

Scope（六问 ③④ / 红线 5、6、10）:

* **阳性** — 空租户 + 空经验库 ⇒ 检索命中种子 ⇒ **真执行**一次（走 T03 Skill
  Runtime 的原语 + 真实 :class:`ToolExecutor`）⇒ 有真实 ``run_steps`` 记录。
* **阴性** — ① 跨租户看不到别人的种子（红线 5）；② 非法 slug **加载时显式报错**
  （不静默跳过）；③ 缺必需段 / 白名单外工具 ⇒ 显式报错；④ 无 procedure 的种子
  **诚实降级**（红线 10，绝不假装执行成功）。
* **反事实（真跑）** — 把「演化原地覆盖种子」实现打开 ⇒ 「种子本体不变」断言
  **转红**（红线 6）。
* **同构** — 每个种子走同一套七段契约（T07）+ 同一物化路径（T11），与自研 Skill
  无差别；``origin=seed`` 用**既有列**表达（无迁移）。

引文纪律：一律 ``file.py::symbol``。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from forgeflow.repositories import get_skill_repository
from forgeflow.repositories.memory.skill_repo import clear_skill_store
from forgeflow.runtime import tool_registry
from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
from forgeflow.runtime.trace_store import TraceStep, reset_trace_sink, set_trace_sink
from forgeflow.skills.models import SkillRecord
from forgeflow.skills.registry import SkillRegistry
from forgeflow.skills.runtime import (
    UNDECLARED_PROCEDURE_REASON,
    SkillRuntime,
    load_procedure,
    to_plan_candidates,
)
from forgeflow.skills.schemas import validate_contract_document
from forgeflow.skills.seeds.loader import (
    ORIGIN_AUTHORED,
    ORIGIN_SEED,
    SEED_ORIGIN_TAG,
    SEED_OWNER,
    SeedError,
    apply_evolution,
    install_seeds,
    is_seed,
    load_seed_dir,
    load_seeds,
    origin_of,
    spec_sha256,
)
from forgeflow.skills.segments import REQUIRED_SEGMENTS, SEGMENT_ORDER
from forgeflow.skills.spec_validator import validate_skill_md

TENANT_A = "tenant-cold-start-a"
TENANT_B = "tenant-cold-start-b"
_QUERY = "合同风险审查"


# --------------------------------------------------------------------------- #
# fixtures / helpers                                                           #
# --------------------------------------------------------------------------- #
class _RecordingSink:
    """A minimal in-process :class:`TraceStore` (mirrors the T01 test seam)."""

    def __init__(self) -> None:
        self.steps: list[TraceStep] = []

    async def insert_step(self, step: TraceStep) -> None:
        self.steps.append(step)

    async def list_for_run(
        self, tenant_id: str | None, run_id: str, *, limit: int = 500
    ) -> list[TraceStep]:
        return [s for s in self.steps if s.run_id == run_id and s.tenant_id == tenant_id][:limit]

    async def list_for_tenant(
        self, tenant_id: str | None, *, limit: int = 1000
    ) -> list[TraceStep]:
        return [s for s in self.steps if s.tenant_id == tenant_id][:limit]


@pytest.fixture(autouse=True)
def _clean_state(force_memory_backend):
    """Every test starts from default tool bindings, no sink, empty skill store."""
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    reset_trace_sink()
    clear_skill_store()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    reset_trace_sink()
    clear_skill_store()


def _valid_spec(slug: str, *, domain: str = "general") -> dict:
    """A fresh, spec-compliant seven-segment contract for a given slug."""
    return {
        "manifest": {
            "name": slug,
            "display_name": "示例种子",
            "version": "1.0.0",
            "description": "示例种子：用于测试的合法七段契约。",
            "metadata": {"domain": domain},
        },
        "knowledge": {"references": [{"file": "guide.md", "summary": "指南"}]},
        "procedure": {"steps": ["第一步", "第二步"]},
        "policies": {"constraints": ["约束一"], "allowed_tools": []},
        "tool_bindings": {"tools": ["docs.parse"], "scripts": []},
        "evaluation": {"ref": "assets/evals/", "note": "DB"},
        "examples": {"examples": ["示例一"]},
    }


def _write_seed(root: Path, slug: str, spec: dict, procedure: dict | None = None) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "spec.json").write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8"
    )
    if procedure is not None:
        (directory / "procedure.json").write_text(
            json.dumps(procedure, ensure_ascii=False), encoding="utf-8"
        )
    return directory


def _step_args(tool: str) -> dict:
    """Per-tool args so each real handler actually runs and returns ``ok``."""
    if tool == "report.render":
        return {"plan": {}, "records": [], "intent": _QUERY}
    if tool == "analysis.score":
        return {"observations": [{"executed": True, "status": "ok", "result_ref": "r1"}]}
    return {"text": "条款一：管辖法院。\n\n条款二：违约金上限。", "intent": _QUERY}


def _require_seed_body_unchanged(before: str, after: str) -> None:
    """The 红线 6 invariant — evolution must not rewrite a seed's body."""
    assert after == before, "红线6：种子本体不得被演化原地覆盖（spec sha256 必须恒定）"


async def _installed(tenant: str):
    repo = get_skill_repository()
    return await install_seeds(tenant, repo=repo), repo


# --------------------------------------------------------------------------- #
# 1. 阳性 —— 目录加载 + T07 契约 + T11 物化                                     #
# --------------------------------------------------------------------------- #
def test_catalogue_loads_and_each_contract_passes_t07() -> None:
    seeds = load_seeds()
    assert len(seeds) >= 3, "冷启动目录至少应有数个种子"
    for seed in seeds:
        report = validate_contract_document(seed.spec, parent_dir=seed.slug)
        assert report.ok, f"{seed.slug} 的契约必须通过 T07：{report.errors}"


def test_every_seed_is_materialisable_with_valid_skill_md() -> None:
    for seed in load_seeds():
        assert "SKILL.md" in seed.bundle.files, f"{seed.slug} 必须物化出 SKILL.md"
        assert seed.bundle.validation["ok"] is True
        report = validate_skill_md(seed.bundle.files["SKILL.md"], parent_dir=seed.slug)
        assert report.ok, f"{seed.slug} 的 SKILL.md 必须合规：{report.errors}"
        assert seed.bundle.files["SKILL.md"].startswith("---")


def test_seeds_are_isomorphic_seven_segment() -> None:
    """种子与自研 Skill 同构：七段齐全，不是简化格式。"""
    for seed in load_seeds():
        present = set(seed.spec)
        for segment in REQUIRED_SEGMENTS:
            assert segment in present, f"{seed.slug} 缺少段 {segment}"
        assert tuple(s for s in SEGMENT_ORDER if s in present) == SEGMENT_ORDER


# --------------------------------------------------------------------------- #
# 2. 阳性 —— origin=seed 用既有列表达                                           #
# --------------------------------------------------------------------------- #
async def test_origin_seed_is_distinguishable_from_authored() -> None:
    repo = get_skill_repository()
    clear_skill_store()
    installed, repo = await _installed(TENANT_A)

    seed_skill = installed[0].skill
    assert seed_skill.owner == SEED_OWNER
    assert SEED_ORIGIN_TAG in seed_skill.tags
    assert is_seed(seed_skill) is True
    assert origin_of(seed_skill) == ORIGIN_SEED

    authored = SkillRecord(tenant_id=TENANT_A, name="自研技能", owner="alice", tags=["x"])
    assert is_seed(authored) is False
    assert origin_of(authored) == ORIGIN_AUTHORED


# --------------------------------------------------------------------------- #
# 3. 阴性 —— 非法 slug / 缺段 / 白名单外工具 显式报错（不静默跳过）              #
# --------------------------------------------------------------------------- #
def test_illegal_directory_slug_raises_explicitly(tmp_path: Path) -> None:
    _write_seed(tmp_path, "Bad Slug", _valid_spec("Bad Slug"))
    with pytest.raises(SeedError) as exc:
        load_seed_dir(tmp_path / "Bad Slug")
    assert "Bad Slug" in str(exc.value) and "slug" in str(exc.value)


def test_manifest_name_mismatch_raises_explicitly(tmp_path: Path) -> None:
    _write_seed(tmp_path, "demo-store", _valid_spec("different-name"))
    with pytest.raises(SeedError) as exc:
        load_seed_dir(tmp_path / "demo-store")
    assert "必须与目录名" in str(exc.value)


def test_missing_required_segment_raises_explicitly(tmp_path: Path) -> None:
    spec = _valid_spec("demo-store")
    del spec["examples"]
    _write_seed(tmp_path, "demo-store", spec)
    with pytest.raises(SeedError) as exc:
        load_seed_dir(tmp_path / "demo-store")
    assert "examples" in str(exc.value)


def test_off_catalogue_tool_raises_explicitly(tmp_path: Path) -> None:
    procedure = {"domain": "general", "procedure": [{"purpose": "越权", "tool": "evil.tool"}]}
    _write_seed(tmp_path, "demo-store", _valid_spec("demo-store"), procedure)
    with pytest.raises(SeedError) as exc:
        load_seed_dir(tmp_path / "demo-store")
    assert "evil.tool" in str(exc.value) and "白名单" in str(exc.value)


# --------------------------------------------------------------------------- #
# 4. 红线 10 —— 无 procedure ⇒ 诚实降级，不假装执行                              #
# --------------------------------------------------------------------------- #
def test_seed_without_procedure_degrades_honestly(tmp_path: Path) -> None:
    _write_seed(tmp_path, "demo-store", _valid_spec("demo-store"))  # no procedure.json
    seed = load_seed_dir(tmp_path / "demo-store")
    assert seed.procedure == ()
    assert seed.procedure_tools() == []

    # T03 — nothing to drive ⇒ the verbatim honest-degradation reason.
    runtime = SkillRuntime()
    assert runtime.degrade_reason([], role="manager") == UNDECLARED_PROCEDURE_REASON
    # And no plan candidate is produced (nothing is executed).
    runtime_skill = {"id": "x", "name": "示例种子", "version": "1.0.0", "steps": list(seed.steps)}
    assert load_procedure(runtime_skill) == []
    assert to_plan_candidates(runtime_skill, []) == []


# --------------------------------------------------------------------------- #
# 5. 阳性 —— 空经验库冷启动命中 + 真执行（承重）                                 #
# --------------------------------------------------------------------------- #
async def test_cold_start_empty_tenant_hits_seed() -> None:
    installed, repo = await _installed(TENANT_A)
    assert installed, "冷启动必须安装种子"

    registry = SkillRegistry(repo=repo)
    hits = await registry.retrieve(TENANT_A, _QUERY, k=3)
    assert hits, "空经验库首次检索必须命中种子 —— 空结果会让阳性断言变成空集永真"
    assert hits[0].skill.name == "合同审查"
    assert hits[0].lexical_score is not None and hits[0].lexical_score > 0.0


async def test_cold_start_seed_really_executes_and_persists_run_steps() -> None:
    """阳性承重 —— 检索命中种子后，逐步经 T03 真实执行并由 T01 落 ``run_steps``。"""
    sink = _RecordingSink()
    set_trace_sink(sink)

    installed, repo = await _installed(TENANT_A)
    contract = next(item for item in installed if item.slug == "contract-review")

    # ① 检索命中（T09）
    registry = SkillRegistry(repo=repo)
    hits = await registry.retrieve(TENANT_A, _QUERY, k=3)
    assert hits[0].skill.id == contract.skill.id

    # ② T03 —— 声明可驱动，plan 逐字取自 procedure[i].tool
    runtime_skill = contract.runtime_skill()
    procedure = load_procedure(runtime_skill)
    assert procedure, "种子必须声明可驱动的 procedure"
    assert SkillRuntime().degrade_reason(procedure, role="manager") is None
    plan = to_plan_candidates(runtime_skill, procedure)
    assert [c["tool"] for c in plan] == contract.seed.procedure_tools()

    # ③ 真执行 —— 每一步都交给真实 ToolExecutor（handler 真跑）
    run_id = str(uuid.uuid4())
    invocations = []
    for index, candidate in enumerate(plan):
        inv = await ToolExecutor().execute(
            candidate["tool"],
            ctx=ToolCallContext(
                run_id=run_id,
                step_id=f"{run_id}:0:{index}",
                tenant_id=TENANT_A,
                user_id="u-cold-start",
                role="manager",
                intent=_QUERY,
                args=_step_args(candidate["tool"]),
            ),
        )
        invocations.append(inv)

    assert invocations, "必须真的执行了步骤（空执行 = 假装成功）"
    for inv in invocations:
        assert inv.status == "ok", f"{inv.tool} 未真执行成功：{inv.error}"
        assert inv.executed is True and inv.invoked is True
        assert inv.development_stub is False, f"{inv.tool} 不得是开发态 stub"
        assert inv.provider != "development-stub"
        assert inv.latency_ms is not None and inv.latency_ms > 0  # 真测量

    # ④ 真实 run_steps（T01 落盘，tenant 作用域）
    rows = [s for s in sink.steps if s.run_id == run_id]
    assert len(rows) == len(plan)
    assert {r.tool for r in rows} == set(contract.seed.procedure_tools())
    assert all(r.status == "ok" for r in rows)
    assert all(r.tenant_id == TENANT_A for r in rows)


# --------------------------------------------------------------------------- #
# 6. 阴性 —— 跨租户隔离（红线 5）                                               #
# --------------------------------------------------------------------------- #
async def test_cross_tenant_seed_isolation() -> None:
    installed, repo = await _installed(TENANT_A)
    registry = SkillRegistry(repo=repo)

    # 阳性对照：宿主租户必须命中
    hits_a = await registry.retrieve(TENANT_A, _QUERY, k=3)
    assert hits_a and hits_a[0].skill.name == "合同审查"

    # 阴性：另一个租户看不到任何种子
    hits_b = await registry.retrieve(TENANT_B, _QUERY, k=3)
    assert hits_b == [], "跨租户不得看到别人的种子（红线 5）"
    skills_b, total_b = await repo.list_skills(TENANT_B, limit=100)
    assert total_b == 0

    # 阳性对照：在 TENANT_B 独立安装后，它才可见（证明上面的空不是 vacuous）
    await install_seeds(TENANT_B, repo=repo)
    hits_b2 = await registry.retrieve(TENANT_B, _QUERY, k=3)
    assert hits_b2 and hits_b2[0].skill.name == "合同审查"


async def test_install_is_idempotent() -> None:
    installed, repo = await _installed(TENANT_A)
    again = await install_seeds(TENANT_A, repo=repo)
    assert [i.skill.id for i in again] == [i.skill.id for i in installed]
    _skills, total = await repo.list_skills(TENANT_A, limit=100)
    assert total == len(installed), "重复安装不得产生重复种子"


# --------------------------------------------------------------------------- #
# 7. 红线 6 —— 演化产出新版本，种子本体不变                                     #
# --------------------------------------------------------------------------- #
async def test_evolution_creates_new_version_and_keeps_seed_body_unchanged() -> None:
    installed, repo = await _installed(TENANT_A)
    contract = next(item for item in installed if item.slug == "contract-review")

    before = contract.spec_hash()
    original_semver = contract.version.semver

    evolved = json.loads(json.dumps(contract.seed.spec, ensure_ascii=False))
    evolved["procedure"]["steps"].append("新增：复核条款引用")

    new_version = await apply_evolution(contract, evolved, repo=repo)

    # 红线 6 —— 种子本体（原版本 spec）sha256 恒定。
    _require_seed_body_unchanged(before, contract.spec_hash())
    stored_original = await repo.get_version(TENANT_A, contract.skill.id, original_semver)
    assert stored_original is not None
    assert spec_sha256(stored_original.spec) == before

    # 演化确实**产出了新版本**（不是原地改）。
    assert new_version.semver != original_semver
    assert contract.skill.current_version == new_version.semver
    assert spec_sha256(new_version.spec) != before


async def test_counterfactual_in_place_overwrite_turns_red(monkeypatch: pytest.MonkeyPatch) -> None:
    """反事实（真跑）—— 打开「演化原地覆盖种子」实现 ⇒ 「本体不变」断言转红。"""
    import forgeflow.skills.versioning as versioning

    installed, repo = await _installed(TENANT_A)
    contract = next(item for item in installed if item.slug == "contract-review")
    before = contract.spec_hash()

    evolved = json.loads(json.dumps(contract.seed.spec, ensure_ascii=False))
    evolved["procedure"]["steps"].append("新增：复核条款引用")

    async def _overwrite_in_place(repo_, tenant_id, skill, spec, **kwargs):
        """The FORBIDDEN implementation: rewrite the incumbent body in place."""
        versions = await repo_.list_versions(tenant_id, skill.id)
        incumbent = next(v for v in versions if v.semver == skill.current_version)
        incumbent.spec = dict(spec)
        return incumbent

    monkeypatch.setattr(versioning, "create_version", _overwrite_in_place)
    await apply_evolution(contract, evolved, repo=repo)

    after = contract.spec_hash()
    assert after != before, "反事实前提：原地覆盖实现确实改动了种子本体"
    # The SAME invariant assertion now fails ⇒ it is load-bearing, not vacuous.
    with pytest.raises(AssertionError, match="红线6"):
        _require_seed_body_unchanged(before, after)


# --------------------------------------------------------------------------- #
# 8. 完整轨迹 —— 加载 → 物化 → 检索命中 → 真执行                                 #
# --------------------------------------------------------------------------- #
async def test_trajectory_load_materialize_retrieve_execute() -> None:
    sink = _RecordingSink()
    set_trace_sink(sink)

    installed, repo = await _installed(TENANT_A)
    contract = next(item for item in installed if item.slug == "contract-review")

    trajectory = {
        "load": contract.slug,
        "materialize": contract.seed.bundle.content_hash,
        "skill_md_lines": len(contract.seed.bundle.files["SKILL.md"].splitlines()),
        "retrieve": [],
        "execute": [],
    }

    registry = SkillRegistry(repo=repo)
    hits = await registry.retrieve(TENANT_A, _QUERY, k=3)
    trajectory["retrieve"] = [h.skill.name for h in hits]

    runtime_skill = contract.runtime_skill()
    plan = to_plan_candidates(runtime_skill, load_procedure(runtime_skill))
    run_id = str(uuid.uuid4())
    for index, candidate in enumerate(plan):
        inv = await ToolExecutor().execute(
            candidate["tool"],
            ctx=ToolCallContext(
                run_id=run_id,
                step_id=f"{run_id}:0:{index}",
                tenant_id=TENANT_A,
                user_id="u-trace",
                role="manager",
                intent=_QUERY,
                args=_step_args(candidate["tool"]),
            ),
        )
        trajectory["execute"].append((inv.tool, inv.status))

    assert trajectory["retrieve"][0] == "合同审查"
    assert trajectory["execute"] == [(tool, "ok") for tool in contract.seed.procedure_tools()]
    assert len([s for s in sink.steps if s.run_id == run_id]) == len(plan)
