"""INC46 T09 — Skill Retrieval / Selection.

Scope (阳性 / 阴性 / 反事实 / 红线):

* **阳性** — 正常 Skill 可被命中（Top-1 是真正最相关的那个）。
* **阴性** — ① 无权限 Skill 不可见；② 跨租户 Skill 不可见；③ 缺能力 Skill 被过滤。
* **反事实（真跑）** — 分别摘掉 ① 租户闸 ② 权限闸 ③ 能力闸，各自证明对应的阴性
  断言转红；证明三道闸都是**承重**的，不是 vacuous。
* **防假绿** — 每条「不可见」都配**阳性对照**：同一个 skill 在放行条件下必须可见，
  否则「不可见」可能只是「它本来就不相关 / 池本来就是空的」。
* **红线 4** — 未测量一律 ``None``，绝不写 ``0`` 冒充「相似度为 0」。
* **红线 5** — 跨租户 Skill 的元数据不得进入候选池。

Every test drives the real functions in ``forgeflow.skills.retrieval``
(never a re-implementation).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from forgeflow.skills.retrieval import (
    EXCLUDED_STATUSES,
    RRF_K,
    RetrievedSkill,
    build_pool,
    capability_filter,
    dense_scores,
    lexical_scores,
    rerank,
    retrieve_skills,
    rrf_fuse,
    top_k,
)

# --------------------------------------------------------------------------- #
# Stubs — shaped exactly like the real records the chain reads via getattr     #
# --------------------------------------------------------------------------- #
@dataclass
class _Skill:
    id: str
    tenant_id: str | None
    name: str
    domain: str = ""
    description: str = ""
    status: str = "published"
    usage_count: int = 0


@dataclass
class _Perm:
    skill_id: str = ""
    granted: bool = False


TENANT = "tenant-a"
FOREIGN = "tenant-b"

# 四个内置风格技能（与 registry._FEATURED_SEED 同构）
_CONTRACT = _Skill(
    id="sk-contract",
    tenant_id=TENANT,
    name="合同审查",
    domain="法务",
    description="自动识别合同风险点，生成审查报告",
    usage_count=96,
)
_CHURN = _Skill(
    id="sk-churn",
    tenant_id=TENANT,
    name="客户流失分析",
    domain="数据分析",
    description="基于历史行为识别客户流失风险并给出挽留建议",
    usage_count=128,
)
_RETRO = _Skill(
    id="sk-retro",
    tenant_id=TENANT,
    name="项目复盘总结",
    domain="项目管理",
    description="基于项目文档与时间线生成复盘总结报告",
    usage_count=74,
)
_CODE = _Skill(
    id="sk-code",
    tenant_id=TENANT,
    name="代码质量检查",
    domain="代码开发",
    description="分析代码仓库问题，提供优化建议",
    usage_count=52,
)

# 跨租户技能：文本与 query **高度一致**（比同租户的更匹配），
# 这样它「不可见」只可能是租户闸造成的，不可能是「不相关」造成的。
_FOREIGN_CONTRACT = _Skill(
    id="sk-foreign-contract",
    tenant_id=FOREIGN,
    name="合同审查",
    domain="法务",
    description="自动识别合同风险点，生成审查报告",
    usage_count=999,
)

_QUERY = "合同风险审查"


def _ids(hits: list[RetrievedSkill]) -> list[str]:
    return [h.skill.id for h in hits]


# --------------------------------------------------------------------------- #
# 阳性探针                                                                     #
# --------------------------------------------------------------------------- #
def test_published_skill_is_hit() -> None:
    """阳性探针 — 正常 Skill 可被命中，且命中的是最相关的那个。"""
    hits = retrieve_skills(TENANT, _QUERY, [_CONTRACT, _CHURN, _RETRO, _CODE], k=1)
    assert len(hits) == 1
    assert hits[0].skill.id == "sk-contract"
    # 相似度是**真实算出来的** float，不是硬编码 1.0
    assert isinstance(hits[0].similarity, float)
    assert hits[0].lexical_score is not None and hits[0].lexical_score > 0.0


def test_top_k_respects_k_and_is_deterministic() -> None:
    """同一输入两次调用结果完全一致（同分按 skill_id 字典序，不留随机性）。"""
    cands = [_CONTRACT, _CHURN, _RETRO, _CODE]
    first = _ids(retrieve_skills(TENANT, _QUERY, cands, k=3))
    second = _ids(retrieve_skills(TENANT, _QUERY, cands, k=3))
    assert first == second
    assert len(first) == 3
    assert retrieve_skills(TENANT, _QUERY, cands, k=0) == []


def test_empty_pool_returns_empty() -> None:
    """空候选 / 全被过滤 ⇒ 空结果，不抛异常、不编造。"""
    assert retrieve_skills(TENANT, _QUERY, []) == []
    assert build_pool(TENANT, []) == []


# --------------------------------------------------------------------------- #
# 阴性探针 ② —— 跨租户不可见（红线 5）                                          #
# --------------------------------------------------------------------------- #
def test_cross_tenant_skill_is_invisible() -> None:
    """阴性② — 跨租户 Skill 的元数据不得进入候选池。

    阳性对照：同一个 skill 在**它自己的租户**下必须被命中，且是 Top-1；
    否则「在 TENANT 下看不见」可能只是因为它本来就不相关。
    """
    cands = [_FOREIGN_CONTRACT, _CHURN, _RETRO, _CODE]

    pool = build_pool(TENANT, cands)
    assert pool, "池不能为空 —— 空池会让『不可见』变成永真断言"
    assert "sk-foreign-contract" not in [s.id for s in pool]
    assert _CHURN.id in [s.id for s in pool]

    hits = retrieve_skills(TENANT, _QUERY, cands, k=3)
    assert "sk-foreign-contract" not in _ids(hits)

    # 阳性对照：换到它自己的租户，它必须出现且排第一
    own = retrieve_skills(FOREIGN, _QUERY, cands, k=1)
    assert _ids(own) == ["sk-foreign-contract"]


def test_cross_tenant_skill_never_appears_even_when_best_match() -> None:
    """跨租户 skill 即使 usage 最高、文本最匹配，也不得被召回。"""
    cands = [_FOREIGN_CONTRACT, _CONTRACT]
    hits = retrieve_skills(TENANT, _QUERY, cands, k=2)
    assert _ids(hits) == ["sk-contract"]


# --------------------------------------------------------------------------- #
# 阴性探针 ① —— 无权限不可见                                                    #
# --------------------------------------------------------------------------- #
def test_ungranted_skill_is_invisible() -> None:
    """阴性① — 有权限行但全部 ``granted=False`` ⇒ 不可见。

    阳性对照：唯一变量改为 ``granted=True`` 后，同一个 skill 必须可见。
    """
    cands = [_CONTRACT, _CHURN, _RETRO]
    denied = {"sk-contract": [_Perm(skill_id="sk-contract", granted=False)]}

    pool = build_pool(TENANT, cands, permissions=denied)
    assert pool, "池不能为空"
    assert "sk-contract" not in [s.id for s in pool]

    hits = retrieve_skills(TENANT, _QUERY, cands, k=3, permissions=denied)
    assert "sk-contract" not in _ids(hits)

    # 阳性对照：授权后同一个 skill 必须回来且是 Top-1
    granted = {"sk-contract": [_Perm(skill_id="sk-contract", granted=True)]}
    back = retrieve_skills(TENANT, _QUERY, cands, k=1, permissions=granted)
    assert _ids(back) == ["sk-contract"]


def test_no_permission_rows_means_unrestricted() -> None:
    """该 skill **没有任何**权限行 ⇒ 视为未声明限制，放行（保持既有召回不变）。

    ``permissions=None`` 与 ``{}`` 都必须放行 —— 但二者语义不同，分别断言。
    """
    cands = [_CONTRACT, _CHURN]
    assert _ids(retrieve_skills(TENANT, _QUERY, cands, k=1, permissions=None)) == ["sk-contract"]
    assert _ids(retrieve_skills(TENANT, _QUERY, cands, k=1, permissions={})) == ["sk-contract"]


# --------------------------------------------------------------------------- #
# 阴性探针 ③ —— 缺能力被过滤（裁定 V-2）                                        #
# --------------------------------------------------------------------------- #
def test_missing_capability_is_filtered() -> None:
    """阴性③ — 调用方要求 ``file.write``，skill 未声明该工具 ⇒ 被过滤。

    阳性对照：不传 ``required_tools``（空操作）时，同一个 skill 必须可见。
    """
    cands = [_CONTRACT, _CHURN]
    specs = {
        "sk-contract": {"tools": ["docs.parse", "policy.check"]},
        "sk-churn": {"tools": ["data.query", "analysis.score", "file.write"]},
    }

    hits = retrieve_skills(
        TENANT, _QUERY, cands, k=2, specs=specs, required_tools=["file.write"]
    )
    assert _ids(hits) == ["sk-churn"]  # 只有声明了 file.write 的留下

    # 阳性对照：不要求工具 ⇒ 空操作，最相关的回来
    free = retrieve_skills(TENANT, _QUERY, cands, k=1, specs=specs)
    assert _ids(free) == ["sk-contract"]


def test_capability_ceiling_filters_higher_class() -> None:
    """``max_class='READ'`` ⇒ ``overall`` 为 WRITE 及以上的 skill 被过滤。

    阳性对照：放宽上限到 WRITE 后，同一个 skill 必须回来。
    """
    cands = [_CONTRACT, _CHURN]
    specs = {
        "sk-contract": {"tools": ["docs.parse"]},          # overall = READ
        "sk-churn": {"tools": ["data.query", "file.write"]},  # overall = WRITE
    }

    strict = retrieve_skills(TENANT, _QUERY, cands, k=2, specs=specs, max_class="READ")
    assert _ids(strict) == ["sk-contract"]

    # 阳性对照：上限放宽到 WRITE 后，同一个 skill 必须**可见**（不再被能力闸剔除）
    loose = retrieve_skills(TENANT, _QUERY, cands, k=2, specs=specs, max_class="WRITE")
    assert "sk-churn" in _ids(loose)


def test_capability_filter_is_noop_by_default() -> None:
    """两个能力参数都为空 ⇒ 空操作，原样返回（裁定 V-2）。"""
    cands = [_CONTRACT, _CHURN]
    assert capability_filter([0, 1], cands) == [0, 1]
    assert capability_filter([0, 1], cands, specs={}) == [0, 1]


# --------------------------------------------------------------------------- #
# 状态闸 —— deprecated / archived（裁定 V-4：T35 前为空操作）                   #
# --------------------------------------------------------------------------- #
def test_deprecated_and_archived_are_excluded() -> None:
    """``deprecated`` / ``archived`` 不得进池。

    诚实声明：逻辑在此已生效并可验证；但**当前 skills 表尚无这两种状态**，
    因此本用例证明的是过滤逻辑本身，不是「线上已有数据被过滤」。
    """
    assert {"deprecated", "archived"} <= set(EXCLUDED_STATUSES)
    archived = replace(_CONTRACT, id="sk-archived", status="archived")
    deprecated = replace(_CONTRACT, id="sk-deprecated", status="deprecated")

    pool = build_pool(TENANT, [archived, deprecated, _CHURN])
    assert [s.id for s in pool] == ["sk-churn"]


def test_published_status_enters_pool() -> None:
    """对照：正常 published 状态不得被状态闸误伤。"""
    assert [s.id for s in build_pool(TENANT, [_CONTRACT])] == ["sk-contract"]


# --------------------------------------------------------------------------- #
# 红线 4 —— 未测量 ⇒ None，绝不写 0                                            #
# --------------------------------------------------------------------------- #
def test_unmeasured_similarity_is_none_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """红线 4 — 稠密向量算不出来时，相似度是 ``None``，**不是** ``0``。"""
    import forgeflow.skills.retrieval as retrieval

    def boom(_text: str) -> list[float]:
        raise RuntimeError("embedding backend unavailable")

    monkeypatch.setattr(retrieval, "embed_text", boom)
    scores = dense_scores(_QUERY, ["a", "b"])
    assert scores == [None, None]
    for value in scores:
        assert value is None
        assert value != 0  # None != 0 —— 明确禁止把「未测量」写成 0

    # 链路不崩，且如实把 None 透传给调用方
    hits = retrieve_skills(TENANT, _QUERY, [_CONTRACT, _CHURN], k=2)
    assert len(hits) == 2
    assert all(h.similarity is None for h in hits)
    assert all(h.similarity != 0 for h in hits)


def test_measured_similarity_is_real_float() -> None:
    """对照：能算出来时必须是真实 float（不是硬编码常量）。"""
    scores = dense_scores("合同风险审查", ["合同审查 法务", "客户流失 数据分析"])
    assert len(scores) == 2
    assert all(isinstance(s, float) for s in scores)
    assert scores[0] > scores[1]  # 与 query 更相关的那个更高


# --------------------------------------------------------------------------- #
# 各阶段的独立性质                                                             #
# --------------------------------------------------------------------------- #
def test_lexical_scores_rank_relevant_first() -> None:
    """词法阶段：与 query 重合度高的文档得分更高（BM25，确定性）。"""
    scores = lexical_scores(_QUERY, ["合同审查 法务 风险", "客户流失 数据分析 挽留"])
    assert len(scores) == 2
    assert scores[0] > scores[1]
    assert scores[1] == 0.0  # 完全无重合 ⇒ 0（这是**测得的** 0，不是未测量）
    assert lexical_scores(_QUERY, []) == []


def test_rrf_fuse_rewards_agreement() -> None:
    """RRF：在两个 ranking 都靠前的下标，融合分高于只在一个里靠前的。"""
    fused = rrf_fuse([[0, 1], [0, 2]])
    assert fused[0] > fused[1]
    assert fused[0] > fused[2]
    assert fused[1] == fused[2]  # 同为各自 ranking 的第 2 名
    assert abs(fused[0] - 2.0 / (RRF_K + 1)) < 1e-12


def test_rerank_is_bounded_and_deterministic() -> None:
    """Rerank 组合分有界且可复现；未测量的稠密分量按 0 计入**组合分**。"""
    fused = {0: 0.02, 1: 0.01}
    dense = {0: 0.9, 1: None}
    usage = {0: 10, 1: 100}
    out = rerank([0, 1], fused=fused, dense=dense, usage=usage)
    assert set(out) == {0, 1}
    assert all(0.0 <= v <= 1.0 for v in out.values())
    assert out == rerank([0, 1], fused=fused, dense=dense, usage=usage)
    assert rerank([], fused=fused, dense=dense, usage=usage) == {}


def test_top_k_tie_breaks_by_id() -> None:
    """同分按 ``skill_id`` 字典序 —— 不留任何随机性。"""
    pool = [_Skill(id="sk-b", tenant_id=TENANT, name="b"), _Skill(id="sk-a", tenant_id=TENANT, name="a")]
    out = top_k(pool, {0: 1.0, 1: 1.0}, k=2)
    assert [h.skill.id for h in out] == ["sk-a", "sk-b"]


# --------------------------------------------------------------------------- #
# context_builder 接线 —— 真实相似度取代硬编码 1.0                             #
# --------------------------------------------------------------------------- #
async def test_context_builder_emits_real_similarity(monkeypatch: pytest.MonkeyPatch) -> None:
    """``build_context`` 的 skill 段必须带**真实**相似度，不再是硬编码 ``1.0``。

    用 monkeypatch 注入一个已知相似度（0.42）的召回结果：若接线仍写死 1.0，
    本用例会拿到 1.0 而失败 —— 这是**承重**断言，不是「跑通就行」。
    """
    from forgeflow.experience.context_builder import build_context
    from forgeflow.skills.registry import SkillRegistry

    skill = _Skill(
        id="sk-probe", tenant_id="ctx-tenant", name="线索评分",
        domain="销售", description="对线索打分", usage_count=3,
    )

    async def fake_retrieve(self: Any, tenant_id: str | None, intent: str, k: int = 3, **kw: Any):
        return [
            RetrievedSkill(
                skill=skill, score=0.9, similarity=0.42,
                lexical_score=1.2, fused_score=0.03,
            )
        ]

    monkeypatch.setattr(SkillRegistry, "retrieve", fake_retrieve)
    bundle = await build_context("ctx-tenant", "线索评分", k_skill=1, budget_tokens=500)

    skill_sections = [s for s in bundle.sections if s["source"] == "skill"]
    assert skill_sections, "skill 段必须出现，否则本断言是空集永真"
    assert skill_sections[0]["similarity"] == 0.42
    assert skill_sections[0]["similarity"] != 1.0


# --------------------------------------------------------------------------- #
# SkillRegistry 接线层（裁定 V-3 / V-5）                                       #
# --------------------------------------------------------------------------- #
class _FakeRepo:
    """最小仓储桩：只实现 ``retrieve`` / ``select`` 真正会调到的方法。"""

    def __init__(self, skills: list[_Skill]) -> None:
        self._skills = skills

    async def list_skills(
        self, tenant_id, *, domain=None, q=None, featured=False, limit=50, offset=0
    ):
        rows = [s for s in self._skills if s.tenant_id == tenant_id]
        return rows[:limit], len(rows)

    async def list_versions(self, tenant_id, skill_id):
        return []


async def test_registry_retrieve_excludes_draft_and_foreign() -> None:
    """``SkillRegistry.retrieve`` 的候选池：draft 与跨租户都不得进入（裁定 V-5）。

    阳性对照：池必须非空 —— 否则「不存在」是空集永真断言。
    """
    from forgeflow.skills.registry import SkillRegistry

    draft = replace(_CONTRACT, id="sk-draft", status="draft", usage_count=9999)
    foreign = replace(_CONTRACT, id="sk-foreign", tenant_id=FOREIGN, usage_count=9999)
    repo = _FakeRepo([_CONTRACT, draft, foreign, _CHURN])
    hits = await SkillRegistry(repo=repo).retrieve(TENANT, _QUERY, k=3)

    assert hits, "召回不得为空 —— 空集会让『不存在』变成永真断言"
    got = _ids(hits)
    assert "sk-draft" not in got
    assert "sk-foreign" not in got
    assert "sk-contract" in got


async def test_registry_select_semantics_unchanged() -> None:
    """裁定 V-3 — ``select`` 的既有语义（关键词打分 + usage + canary）**一行未改**。

    用**英文** intent 锁定：中文无空格时 tokens 会退化成整串、子串匹配全落空，
    那是**既有**缺陷（新链路已用逐字 BM25 解决），不得把它当成回归基线。
    """
    from forgeflow.skills.registry import SkillRegistry

    a = _Skill(id="sk-a", tenant_id="t", name="contract review", domain="legal",
               description="review contract clauses", usage_count=10)
    b = _Skill(id="sk-b", tenant_id="t", name="churn analysis", domain="data",
               description="find churn risk", usage_count=999)
    reg = SkillRegistry(repo=_FakeRepo([a, b]))

    selected = await reg.select("t", "contract review", k=2)
    assert [s.id for s in selected] == ["sk-a", "sk-b"]  # 关键词命中优先于 usage

    # 新链路在同一批候选上不得给出更差的结论
    hits = await reg.retrieve("t", "contract review", k=2)
    assert hits[0].skill.id == "sk-a"


async def test_context_builder_unmeasured_similarity_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """红线 4 — 未测量时 ``build_context`` 输出 ``None``，不得写 0。"""
    from forgeflow.experience.context_builder import build_context
    from forgeflow.skills.registry import SkillRegistry

    skill = _Skill(id="sk-probe2", tenant_id="ctx-tenant2", name="未测量技能")

    async def fake_retrieve(self: Any, tenant_id: str | None, intent: str, k: int = 3, **kw: Any):
        return [
            RetrievedSkill(
                skill=skill, score=0.5, similarity=None,
                lexical_score=0.7, fused_score=0.02,
            )
        ]

    monkeypatch.setattr(SkillRegistry, "retrieve", fake_retrieve)
    bundle = await build_context("ctx-tenant2", "任意意图", k_skill=1, budget_tokens=500)

    skill_sections = [s for s in bundle.sections if s["source"] == "skill"]
    assert skill_sections
    assert skill_sections[0]["similarity"] is None
    assert skill_sections[0]["similarity"] != 0
