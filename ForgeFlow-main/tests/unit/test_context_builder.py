"""INC2-09 — Context Builder (docs/sop/05-ARCHITECTURE-INC2.md §2.5).

Covers: budget-bounded bundle assembly, per-item capping, dedup, tiered pruning
(episodic first), and the compression_ratio / hit_rate metrics. The pipeline is
LLM-free by design, so these run entirely on the memory+mock profile.
"""

from __future__ import annotations

import uuid

import pytest

from forgeflow.experience.context_builder import (
    build_context,
    get_context_stats,
    reset_context_stats,
)
from forgeflow.experience.memory_store import clear_memory_entries, save_memory
from forgeflow.experience.token_budget import (
    estimate_tokens,
    per_item_cap,
    truncate_to_tokens,
)


def _tenant() -> str:
    return f"t-ctx-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _memory_backend(force_memory_backend):
    """The context builder is LLM-free and offline by design (module docstring).

    Pinning the backend keeps the recall stage reading the in-process memory /
    skill / experience stores rather than a shared live database, whose residual
    ``skills`` rows would leak a spurious section into an "empty tenant" bundle.
    """
    return force_memory_backend



# --------------------------------------------------------------------------- #
# token_budget primitives                                                     #
# --------------------------------------------------------------------------- #

def test_estimate_tokens_ascii_and_cjk():
    assert estimate_tokens("") == 0
    # 4 ASCII chars ≈ 1 token.
    assert estimate_tokens("abcd") == 1
    # CJK is denser: 1.7 chars/token → a short Chinese string costs more.
    assert estimate_tokens("销售线索评分规则") == 5
    assert estimate_tokens("销售线索评分规则") > estimate_tokens("abcdabcd")


def test_truncate_to_tokens_respects_cap():
    text = "word " * 200
    clipped = truncate_to_tokens(text, 15)
    assert estimate_tokens(clipped) <= 15
    assert len(clipped) < len(text)


def test_per_item_cap():
    assert per_item_cap(1000, 0.4) == 400
    assert per_item_cap(1, 0.4) == 1


# --------------------------------------------------------------------------- #
# build_context                                                               #
# --------------------------------------------------------------------------- #

async def test_build_context_returns_bundle_within_budget():
    clear_memory_entries()
    reset_context_stats()
    tenant = _tenant()
    await save_memory(tenant, "semantic", "销售线索评分规则：结合互动频次与预算打分。" * 2)
    await save_memory(tenant, "user", "我的签名格式偏好。", actor_id="u1")

    bundle = await build_context(tenant, "销售线索评分", budget_tokens=200, k_memory=5)

    assert bundle.sections
    assert bundle.tokens_used <= 200
    assert 0.0 <= bundle.compression_ratio <= 1.0
    assert 0.0 <= bundle.hit_rate <= 1.0
    for section in bundle.sections:
        assert {"source", "ref_id", "text"} <= set(section)
    assert get_context_stats()["builds"] == 1


async def test_build_context_dedups_identical_content():
    clear_memory_entries()
    tenant = _tenant()
    duplicate = "完全一致的去重测试内容 duplicated content"
    await save_memory(tenant, "semantic", duplicate)
    await save_memory(tenant, "semantic", duplicate)

    bundle = await build_context(tenant, duplicate, budget_tokens=500, k_memory=5)

    texts = [s["text"] for s in bundle.sections]
    assert len(texts) == len(set(texts))  # no duplicate text survives
    assert len(bundle.sections) < bundle.recalled  # one of two collapsed


async def test_per_item_cap_is_enforced():
    clear_memory_entries()
    tenant = _tenant()
    await save_memory(tenant, "semantic", "longfact " * 400)
    bundle = await build_context(tenant, "longfact", budget_tokens=100, k_memory=5)
    cap = per_item_cap(100, 0.4)
    for section in bundle.sections:
        assert estimate_tokens(section["text"]) <= cap


async def test_over_budget_drops_episodic_first():
    clear_memory_entries()
    tenant = _tenant()
    await save_memory(tenant, "episodic", "EPISODIC " + "detail " * 80)
    await save_memory(tenant, "semantic", "SEMANTIC " + "fact " * 80)
    await save_memory(tenant, "user", "USER " + "pref " * 80, actor_id="u1")
    await save_memory(tenant, "team", "TEAM " + "consensus " * 80, team_id="t1")

    bundle = await build_context(tenant, "fact", budget_tokens=40, k_memory=10)

    assert bundle.tokens_used <= 40
    assert bundle.dropped
    assert any(d["reason"] == "episodic_pruned" for d in bundle.dropped)
    assert bundle.compression_ratio < 1.0


async def test_skill_source_included_when_published():
    clear_memory_entries()
    tenant = _tenant()
    from forgeflow.skills.registry import SkillRegistry

    await SkillRegistry().create(
        tenant, "线索评分", "数据分析", description="对销售线索打分", status="published"
    )
    bundle = await build_context(tenant, "线索评分", budget_tokens=500, k_skill=3)
    sources = {s["source"] for s in bundle.sections}
    assert "skill" in sources


async def test_build_context_offline_no_llm():
    # A tenant with no data must still return a valid (empty) bundle, not raise.
    clear_memory_entries()
    bundle = await build_context(_tenant(), "no data here", budget_tokens=100)
    assert bundle.tokens_used == 0
    assert bundle.sections == []
    assert bundle.hit_rate == 0.0
