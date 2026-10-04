"""INC46 T30 — 渐进披露加载（Progressive Disclosure）。

Scope (阳性 / 阴性 / 反事实 / 红线 19):

* **阳性** — 预算 / 选择集内 ⇒ L1 常驻清单生成；随后**只对被选中**的 skill 加载
  L2 正文；L3 只对**显式引用**加载。
* **阴性（红线 19，核心指标）** — **未选中 skill 的正文加载计数必须 = 0**，且是
  **实测**断言（真实数字），不是「没出现」的弱断言。
* **反事实（真跑）** — 把「按需加载」改成「预加载全部正文」⇒ 同一个红线断言函数
  必须**转红**（抛 AssertionError）。证明该指标是**承重**的、不是恒真。
* **红线 4** — 未测量一律 ``None``，绝不写 ``0`` 冒充未测量。
* **预算** — 超限 ⇒ :class:`ProgressiveBudgetError` 显式抛错，不静默截断。

Every test drives the real functions in ``forgeflow.skills.progressive_loader`` and
the **real** T11 ``materialize_skill`` with a **real** seven-segment contract —
never a re-implementation.
"""

from __future__ import annotations

import copy

import pytest

from forgeflow.skills.models import SkillRecord, SkillVersionRecord
from forgeflow.skills.progressive_loader import (
    LAYER_BODY,
    LAYER_METADATA,
    LAYER_RESOURCE,
    LoadLedger,
    LoadRecord,
    ProgressiveBudgetError,
    ProgressiveLoader,
    assert_within_budget,
    budget_violations,
)
from forgeflow.skills.skill_md import materialize_skill
from forgeflow.skills.spec_validator import validate_skill_md

_TENANT = "default"


# --------------------------------------------------------------------------- #
# helpers — real seven-segment contract + real T11 materialiser                #
# --------------------------------------------------------------------------- #
def _contract(slug: str, *, display_name: str, description: str) -> dict:
    """A fresh, spec-compliant seven-segment contract (合规七段契约)."""
    return {
        "manifest": {
            "name": slug,
            "display_name": display_name,
            "version": "1.0.0",
            "description": description,
        },
        "knowledge": {"references": [{"file": "guide.md", "summary": "快速指南"}]},
        "procedure": {"steps": ["读取输入", "生成输出"]},
        "policies": {"constraints": ["不得泄露密钥"], "allowed_tools": ["log.write"]},
        "tool_bindings": {"tools": ["data.read"], "scripts": ["run.py"]},
        "evaluation": {"ref": "assets/evals/", "note": "权威记录在 ForgeFlow DB"},
        "examples": {"examples": ["输入 A → 输出 B"]},
    }


def _records(
    spec: dict, *, skill_id: str, name: str, version: str = "1.0.0"
) -> tuple[SkillRecord, SkillVersionRecord]:
    skill = SkillRecord(
        id=skill_id, tenant_id=_TENANT, name=name, current_version=version,
        status="published",
    )
    ver = SkillVersionRecord(
        skill_id=skill_id, tenant_id=_TENANT, semver=version, spec=spec
    )
    return skill, ver


#: three skills — sk-a is the *selected* one; sk-b / sk-c are NOT selected.
_SELECTED = "sk-a"
_UNSELECTED = ("sk-b", "sk-c")
_ALL = (_SELECTED,) + _UNSELECTED


def _world() -> tuple[list[SkillRecord], dict[str, SkillVersionRecord]]:
    """Build 3 real records; sk-a carries reference + script content (for L3)."""
    specs = {
        "sk-a": _contract("contract-review", display_name="合同审查",
                          description="自动识别合同风险点，生成审查报告。"),
        "sk-b": _contract("churn-analysis", display_name="客户流失分析",
                          description="基于历史行为识别客户流失风险并给出挽留建议。"),
        "sk-c": _contract("retro-summary", display_name="项目复盘总结",
                          description="基于项目文档与时间线生成复盘总结报告。"),
    }
    # 只给 sk-a 提供 L3 正文（references/guide.md 与 scripts/run.py）。
    specs["sk-a"]["_materialization"] = {
        "references": {"guide.md": "# 指南正文\n这是知识正文。\n"},
        "scripts": {"run.py": "print('hi')\n"},
    }
    skills: list[SkillRecord] = []
    versions: dict[str, SkillVersionRecord] = {}
    for sid, spec in specs.items():
        skill, ver = _records(spec, skill_id=sid, name=spec["manifest"]["display_name"])
        skills.append(skill)
        versions[sid] = ver
    return skills, versions


def _loader(**kw) -> ProgressiveLoader:
    skills, versions = _world()
    return ProgressiveLoader(skills=skills, versions=versions, **kw)


def _assert_red_line(loader: ProgressiveLoader, selected, all_ids) -> dict[str, int]:
    """The red-line assertion: every **unselected** skill's body-load count is 0.

    Returned dict is the **实测** count vector so callers can print real numbers.
    """
    counts = {sid: loader.ledger.body_load_count(sid) for sid in all_ids}
    for sid in all_ids:
        if sid not in selected:
            assert counts[sid] == 0, (
                f"红线 19：未选中 {sid} 的正文加载计数必须 = 0，实测 {counts[sid]}"
            )
    return counts


# --------------------------------------------------------------------------- #
# 1. 阳性 — L1 常驻清单生成，且**绝不物化**                                     #
# --------------------------------------------------------------------------- #
def test_l1_index_builds_metadata_only_never_materialises() -> None:
    """L1 清单只含元数据 + 一行摘要；注入一个「一旦物化就爆炸」的物化器证明。"""

    def _boom(skill, version):  # pragma: no cover - must never be called
        raise AssertionError("l1_index 不得物化（不得读 SKILL.md 正文）")

    skills, versions = _world()
    loader = ProgressiveLoader(materializer=_boom)
    entries = loader.l1_index(skills, versions)

    assert [e["skill_id"] for e in entries] == list(_ALL)
    first = entries[0]
    assert first["slug"] == "contract-review"
    assert first["display_name"] == "合同审查"
    assert first["description"].startswith("自动识别合同风险点")
    assert first["summary"] and "\n" not in first["summary"]  # 一行摘要
    assert all(e["layer"] == LAYER_METADATA for e in entries)

    # 未触发任何 L2 / L3 加载。
    assert loader.ledger.l2_loaded == []
    assert loader.ledger.l3_loaded == []
    # 但 L1 加载被记账（实测字节 > 0，条目 = 3）。
    l1_records = [r for r in loader.ledger.records if r.layers == [LAYER_METADATA]]
    assert len(l1_records) == 1
    assert l1_records[0].entries == 3
    assert l1_records[0].bytes is not None and l1_records[0].bytes > 0
    assert set(loader.ledger.l1_loaded) == set(_ALL)


# --------------------------------------------------------------------------- #
# 2. 红线 19 — 未选中 skill 正文加载计数 = 0（实测）                             #
# --------------------------------------------------------------------------- #
def test_selected_body_loaded_unselected_count_is_zero() -> None:
    loader = _loader()
    entries = loader.l1_index(_world()[0], _world()[1])
    assert entries  # 阳性对照：清单非空，否则下面的 0 可能是空集永真

    bodies = loader.load_selected([_SELECTED])

    # 阳性：被选中的正文真的加载了，且是合规 SKILL.md 主体。
    assert _SELECTED in bodies
    assert "## 步骤" in bodies[_SELECTED] and "读取输入" in bodies[_SELECTED]
    assert loader.ledger.body_load_count(_SELECTED) == 1

    # 阳性对照：先证明 sk-a 的「未选中计数」可以非零（加载器对 sk-a 敏感）。
    # 阴性（红线 19）：未选中者 = 0，贴出**真实数字**。
    counts = _assert_red_line(loader, selected={_SELECTED}, all_ids=_ALL)
    print(f"[T30 red-line] body_load_count = {counts}")
    assert counts == {"sk-a": 1, "sk-b": 0, "sk-c": 0}


def test_many_unselected_skills_load_zero_bodies() -> None:
    """扩展阴性：10 个候选里只选 1 个 ⇒ 其余 9 个正文加载计数全 0。"""
    skills = []
    versions: dict[str, SkillVersionRecord] = {}
    for i in range(10):
        sid = f"sk-{i}"
        spec = _contract(f"skill-{i}", display_name=f"技能{i}", description="描述。")
        skill, ver = _records(spec, skill_id=sid, name=f"技能{i}")
        skills.append(skill)
        versions[sid] = ver
    loader = ProgressiveLoader(skills=skills, versions=versions)
    loader.l1_index(skills, versions)
    loader.load_selected(["sk-0"])

    counts = {s: loader.ledger.body_load_count(s) for s in loader.known_skill_ids()}
    assert counts["sk-0"] == 1
    assert all(v == 0 for k, v in counts.items() if k != "sk-0")
    assert sum(counts.values()) == 1  # 总共只加载了 1 份正文


# --------------------------------------------------------------------------- #
# 3. 反事实（真跑）— 预加载全部正文 ⇒ 红线断言转红                               #
# --------------------------------------------------------------------------- #
def test_counterfactual_preloading_all_bodies_turns_red_line_red() -> None:
    """同一个红线断言函数：按需加载通过；预加载全部 ⇒ 必须转红。"""

    # (a) 按需加载：红线通过（不抛）。
    progressive = _loader()
    progressive.l1_index(_world()[0], _world()[1])
    progressive.load_selected([_SELECTED])
    _assert_red_line(progressive, selected={_SELECTED}, all_ids=_ALL)  # must NOT raise

    # (b) 反事实（真跑）：把「按需」误改成「预加载全部正文」。
    eager = _loader()
    eager.l1_index(_world()[0], _world()[1])
    for sid in eager.known_skill_ids():  # 预加载全部
        eager.load_body(sid)

    with pytest.raises(AssertionError) as exc:
        _assert_red_line(eager, selected={_SELECTED}, all_ids=_ALL)
    msg = str(exc.value)
    assert "红线 19" in msg and "sk-b" in msg  # 点名的就是被预加载的未选中 skill
    # 反事实的可观测证据：未选中的 sk-b / sk-c 计数 > 0。
    assert eager.ledger.body_load_count("sk-b") == 1
    assert eager.ledger.body_load_count("sk-c") == 1


# --------------------------------------------------------------------------- #
# 4. L3 — 只在显式引用时加载                                                    #
# --------------------------------------------------------------------------- #
def test_l3_resource_loads_only_when_explicitly_referenced() -> None:
    loader = _loader()
    loader.l1_index(_world()[0], _world()[1])

    ref = "references/guide.md"
    assert loader.ledger.resource_load_count(_SELECTED, ref) == 0  # 尚未引用
    body = loader.load_body(_SELECTED)
    assert body is not None
    # 加载正文**不**会连带加载任何资源（渐进披露：层间解耦）。
    assert loader.ledger.l3_loaded == []

    content = loader.load_resource(_SELECTED, ref)
    assert content is not None and content.startswith("# 指南正文")
    assert loader.ledger.resource_load_count(_SELECTED, ref) == 1

    # 反例：未引用 / 不存在的资源 ⇒ None，且不计入已加载。
    assert loader.load_resource(_SELECTED, "references/missing.md") is None
    assert loader.ledger.resource_load_count(_SELECTED, "references/missing.md") == 0


def test_l3_on_unselected_skill_is_zero() -> None:
    loader = _loader()
    loader.l1_index(_world()[0], _world()[1])
    loader.load_resource(_SELECTED, "references/guide.md")
    # 未选中 skill 的资源加载计数 = 0（既未请求，也无资源）。
    assert loader.ledger.resource_load_count("sk-b") == 0
    assert loader.ledger.resource_load_count("sk-c") == 0
    assert all(not tagged.startswith("sk-b:") for tagged in loader.ledger.l3_loaded)


def test_load_resource_requires_explicit_ref() -> None:
    """L3 **必须**显式 ref —— 空 / 缺省引用被显式拒绝，绝不隐式批量加载。"""
    loader = _loader()
    loader.l1_index(_world()[0], _world()[1])
    with pytest.raises(ValueError):
        loader.load_resource(_SELECTED, "")
    with pytest.raises(ValueError):
        loader.load_resource(_SELECTED, "   ")


# --------------------------------------------------------------------------- #
# 5. 红线 4 — 未测量 ⇒ None，禁止写 0                                            #
# --------------------------------------------------------------------------- #
def test_unmeasured_is_none_not_zero() -> None:
    # 请求一个不存在的 skill：没有任何内容可测 ⇒ bytes/entries 为 None。
    loader = _loader()
    loader.l1_index(_world()[0], _world()[1])
    assert loader.load_body("sk-does-not-exist") is None
    rec = loader.ledger.records[-1]
    assert rec.layers == [LAYER_BODY]
    assert rec.bytes is None and rec.entries is None
    assert rec.bytes != 0 and rec.entries != 0  # None ≠ 0，明确禁止冒充

    # 直接构造的未测量记录同样为 None。
    bare = LoadRecord(skill_id="x", layers=[LAYER_METADATA])
    assert bare.bytes is None and bare.entries is None
    assert bare.bytes != 0 and bare.entries != 0


def test_measured_counts_are_real_ints() -> None:
    """对照：真的加载了 ⇒ 字节 / 条目是**实测**正整数。"""
    loader = _loader()
    loader.l1_index(_world()[0], _world()[1])
    body = loader.load_body(_SELECTED)
    assert body is not None
    rec = loader.ledger.records[-1]
    assert isinstance(rec.bytes, int) and rec.bytes > 0
    assert isinstance(rec.entries, int) and rec.entries > 0
    assert rec.bytes == len(body.encode("utf-8"))
    assert rec.entries == len(body.splitlines())


# --------------------------------------------------------------------------- #
# 6. 预算 — 超出 ⇒ 显式报错（不静默截断 / 不静默丢弃）                           #
# --------------------------------------------------------------------------- #
def test_within_budget_passes() -> None:
    loader = _loader()
    loader.l1_index(_world()[0], _world()[1])
    loader.load_selected([_SELECTED])
    assert_within_budget(loader.ledger, max_bytes=10_000_000)  # 不抛
    assert budget_violations(loader.ledger, max_bytes=10_000_000) == []


def test_over_budget_raises_explicitly() -> None:
    loader = _loader()
    loader.l1_index(_world()[0], _world()[1])
    loader.load_selected([_SELECTED])

    measured = loader.ledger.measured_bytes()
    with pytest.raises(ProgressiveBudgetError) as exc:
        assert_within_budget(loader.ledger, max_bytes=measured - 1)
    msg = str(exc.value)
    assert "超过预算" in msg
    assert str(measured) in msg, "错误消息必须给出**实测**总量（可复算）"


# --------------------------------------------------------------------------- #
# 7. 真跑 T11 物化 + 真七段契约（非 mock）+ 真仓储补齐路径                       #
# --------------------------------------------------------------------------- #
def test_default_materializer_is_t11_and_output_passes_spec_validation() -> None:
    """默认物化器 = T11；产物是合规 SKILL.md（过真实 validate_skill_md）。"""
    loader = _loader()  # materializer 默认 = materialize_skill
    loader.l1_index(_world()[0], _world()[1])
    body = loader.load_body(_SELECTED)
    assert body is not None
    report = validate_skill_md(body, parent_dir="contract-review")
    assert report.ok, report.errors
    assert body.startswith("---\n")  # frontmatter 存在

    # L3 正文来自真实物化的 bundle.files。
    assert loader.load_resource(_SELECTED, "scripts/run.py") == "print('hi')\n"


class _FakeRepo:
    """Minimal repo faithful to the real ``list_versions`` call surface."""

    def __init__(self) -> None:
        self._versions: dict[str, list[SkillVersionRecord]] = {}

    def add(self, version: SkillVersionRecord) -> None:
        self._versions.setdefault(version.skill_id, []).append(version)

    async def list_versions(self, *args, **kwargs):
        skill_id = args[-1] if args else kwargs.get("skill_id")
        return list(self._versions.get(skill_id, []))


async def test_repo_injection_resolves_versions_then_loads_real_body() -> None:
    """注入 repo ⇒ resolve_versions 补齐版本 ⇒ load_body 真跑 T11 物化。"""
    skills, versions = _world()
    repo = _FakeRepo()
    for ver in versions.values():
        repo.add(ver)

    # 刻意**不**传 versions，只传 repo —— 版本必须由仓储补齐。
    loader = ProgressiveLoader(skills=skills, repo=repo)
    await loader.resolve_versions(_TENANT)
    body = loader.load_body(_SELECTED)
    assert body is not None and "## 步骤" in body
    assert loader.ledger.body_load_count(_SELECTED) == 1


# --------------------------------------------------------------------------- #
# 8. 记账快照（证据用）                                                          #
# --------------------------------------------------------------------------- #
def test_ledger_accounting_snapshot() -> None:
    """一次典型加载的字节 / 条目记账快照 —— 数字全部**实测**且可复算。"""
    skills, versions = _world()
    loader = ProgressiveLoader(skills=skills, versions=versions)
    loader.l1_index(skills, versions)
    loader.load_selected([_SELECTED])
    loader.load_resource(_SELECTED, "references/guide.md")

    # 复算期望值：直接用真物化得到的 SKILL.md / 资源正文。
    skill_a = next(s for s in skills if s.id == "sk-a")
    bundle = materialize_skill(skill_a, versions["sk-a"])
    expected_body_bytes = len(bundle.files["SKILL.md"].encode("utf-8"))
    expected_body_lines = len(bundle.files["SKILL.md"].splitlines())
    expected_res_bytes = len(bundle.files["references/guide.md"].encode("utf-8"))

    l1 = next(r for r in loader.ledger.records if r.layers == [LAYER_METADATA])
    l2 = next(r for r in loader.ledger.records if r.layers == [LAYER_BODY])
    l3 = next(
        r for r in loader.ledger.records if r.layers == [f"{LAYER_RESOURCE}:references/guide.md"]
    )
    print(
        "[T30 snapshot] "
        f"L1 bytes={l1.bytes} entries={l1.entries} | "
        f"L2(sk-a) bytes={l2.bytes} entries={l2.entries} | "
        f"L3(sk-a,references/guide.md) bytes={l3.bytes} entries={l3.entries} | "
        f"measured_total={loader.ledger.measured_bytes()}"
    )

    assert l1.entries == 3 and l1.bytes is not None and l1.bytes > 0
    assert l2.bytes == expected_body_bytes and l2.entries == expected_body_lines
    assert l3.bytes == expected_res_bytes
    assert loader.ledger.measured_bytes() == l1.bytes + l2.bytes + l3.bytes
    # 未选中者依旧 0（快照里也如实体现）。
    assert loader.ledger.body_load_count("sk-b") == 0
    assert loader.ledger.body_load_count("sk-c") == 0
    assert copy.deepcopy(bundle.files) == bundle.files  # 输入未被加载器篡改


# --------------------------------------------------------------------------- #
# 9. 与 T09 的加性接缝：retrieve_metadata_only 不加载任何正文                     #
# --------------------------------------------------------------------------- #
def test_retrieve_metadata_only_is_additive_and_body_free() -> None:
    from forgeflow.skills.retrieval import retrieve_metadata_only, retrieve_skills

    skills, versions = _world()
    loader = ProgressiveLoader(skills=skills, versions=versions)
    query = "合同风险审查"

    # 既有链路（默认路径）逐字节不变地仍可调用。
    hits = retrieve_skills(_TENANT, query, skills, k=3)
    assert hits and hits[0].skill.id == _SELECTED

    index = retrieve_metadata_only(
        _TENANT, query, skills, k=3, versions=versions, loader=loader
    )
    assert index and index[0]["skill_id"] == _SELECTED
    assert index[0]["slug"] == "contract-review"
    # 关键：L1 入口没有加载任何正文 / 资源。
    assert loader.ledger.l2_loaded == []
    assert loader.ledger.l3_loaded == []
    assert all(loader.ledger.body_load_count(sid) == 0 for sid in _ALL)
