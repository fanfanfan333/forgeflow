"""INC46 T02 — Rule assets: deterministic MUST / MUST_NOT with recomputable evidence.

Every rule's ``support`` and ``confidence`` is recomputed by hand here, the
"never succeeded" guard is pinned, the tenant-fail-closed write/read discipline
is exercised, and a static scan proves the extractor contains **no** chat-model
import path (零 LLM).
"""

from __future__ import annotations

import pathlib

import pytest

from forgeflow.skills.rule_assets import (
    RULE_KINDS,
    RULE_MIN_CONFIDENCE,
    RULE_MIN_SUPPORT,
    RuleAsset,
    clear_rule_store,
    extract_rules,
    list_rules,
    persist_rules,
)

# ``asyncio_mode = "auto"`` (pyproject) collects the async cases; the pure
# (synchronous) cases must NOT carry an asyncio mark, so none is applied here.

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SKILLS = _REPO / "forgeflow" / "skills"


def _step(tool: str, *, status: str = "ok", run_id: str = "r1", index: int = 0) -> dict:
    return {"tool": tool, "status": status, "run_id": run_id, "step_index": index}


def _run(run_id: str, tools: list[str], *, statuses: list[str] | None = None) -> list[dict]:
    statuses = statuses or ["ok"] * len(tools)
    return [_step(t, status=st, run_id=run_id, index=i) for i, (t, st) in enumerate(zip(tools, statuses))]


@pytest.fixture(autouse=True)
def _clean_rule_store():
    clear_rule_store()
    yield
    clear_rule_store()


# --------------------------------------------------------------------------- #
# 1. constants                                                                 #
# --------------------------------------------------------------------------- #
def test_rule_constants():
    assert RULE_KINDS == ("must", "must_not")
    assert RULE_MIN_SUPPORT == 2
    assert RULE_MIN_CONFIDENCE == 0.8


# --------------------------------------------------------------------------- #
# 2. must — Y before X across the successful runs                              #
# --------------------------------------------------------------------------- #
def test_must_rule_is_extracted_with_recomputable_confidence():
    runs = [_run("r1", ["X", "Y"]), _run("r2", ["X", "Y"]), _run("r3", ["X", "Y"])]
    rules = extract_rules([], runs, min_support=2, min_confidence=0.8)
    must = [r for r in rules if r.rule_kind == "must"]
    xy = next(r for r in must if "先执行 X 再执行 Y" in r.rule_text)
    assert xy.support == 3
    assert xy.confidence == 1.0  # Y precedes X in 3/3 successful runs
    assert set(xy.source_run_ids) == {"r1", "r2", "r3"}
    # The reverse order never reached confidence ⇒ not emitted.
    assert not any("先执行 Y 再执行 X" in r.rule_text for r in must)


def test_must_rule_accepts_near_universal_order_at_threshold():
    runs = [
        _run("r1", ["X", "Y"]),
        _run("r2", ["X", "Y"]),
        _run("r3", ["X", "Y"]),
        _run("r4", ["X", "Y"]),
        _run("r5", ["Y", "X"]),
    ]
    rules = extract_rules([], runs, min_support=2, min_confidence=0.8)
    xy = [r for r in rules if r.rule_kind == "must" and "先执行 X 再执行 Y" in r.rule_text]
    assert len(xy) == 1
    assert xy[0].confidence == 0.8  # 4/5
    assert xy[0].support == 5
    # The minority order (1/5 = 0.2) is below the floor ⇒ dropped.
    assert not any("先执行 Y 再执行 X" in r.rule_text for r in rules)


def test_no_must_rule_when_order_is_not_universal_enough():
    runs = [_run("r1", ["X", "Y"]), _run("r2", ["Y", "X"]), _run("r3", ["Y", "X"])]
    rules = extract_rules([], runs, min_support=2, min_confidence=0.8)
    assert [r for r in rules if r.rule_kind == "must"] == []  # 1/3 and 2/3 both < 0.8


# --------------------------------------------------------------------------- #
# 3. must_not — T errors ≥min_support times and never succeeds                  #
# --------------------------------------------------------------------------- #
def test_must_not_rule_is_extracted_with_recomputable_confidence():
    runs = [
        _run("r1", ["T"], statuses=["error"]),
        _run("r2", ["T"], statuses=["error"]),
        _run("r3", ["T"], statuses=["error"]),
        _run("r4", ["U"], statuses=["ok"]),
    ]
    rules = extract_rules([], runs, min_support=2, min_confidence=0.8)
    must_not = [r for r in rules if r.rule_kind == "must_not"]
    assert len(must_not) == 1
    rule = must_not[0]
    assert "T" in rule.rule_text
    assert rule.support == 3  # errored in 3 runs
    assert rule.confidence == 1.0  # 3 errors / 3 calls
    assert set(rule.source_run_ids) == {"r1", "r2", "r3"}


def test_must_not_is_suppressed_when_the_tool_ever_succeeds():
    runs = [
        _run("r1", ["T"], statuses=["error"]),
        _run("r2", ["T"], statuses=["error"]),
        _run("r3", ["T"], statuses=["ok"]),  # one success ⇒ never "always fails"
    ]
    rules = extract_rules([], runs, min_support=2, min_confidence=0.8)
    assert [r for r in rules if r.rule_kind == "must_not"] == []


def test_no_rule_below_min_support():
    runs = [_run("r1", ["T"], statuses=["error"])]
    assert extract_rules([], runs, min_support=2, min_confidence=0.8) == []


# --------------------------------------------------------------------------- #
# 4. zero LLM (static scan)                                                    #
# --------------------------------------------------------------------------- #
_FORBIDDEN = (
    "forgeflow.models",
    "get_model",
    "ChatOllama",
    "langchain",
    "openai",
    "anthropic",
    "llm_provider",
)


def test_pattern_miner_and_rule_assets_have_no_model_import_path():
    for name in ("pattern_miner.py", "rule_assets.py"):
        source = (_SKILLS / name).read_text(encoding="utf-8")
        for forbidden in _FORBIDDEN:
            assert forbidden not in source, f"{name} must not reference {forbidden!r}"


# --------------------------------------------------------------------------- #
# 5. persistence — tenant fail-closed, idempotent                              #
# --------------------------------------------------------------------------- #
def _sample_rules() -> list[RuleAsset]:
    runs = [_run("r1", ["X", "Y"]), _run("r2", ["X", "Y"])]
    return extract_rules([], runs, min_support=2, min_confidence=0.8)


async def test_persist_and_list_rules(force_memory_backend):
    rules = _sample_rules()
    assert rules  # X-before-Y at support 2
    saved = await persist_rules("t-rules", rules)
    assert saved
    assert all(r.tenant_id == "t-rules" for r in saved)
    listed = await list_rules("t-rules")
    assert len(listed) == len(saved)
    assert {r.rule_kind for r in listed} <= {"must", "must_not"}


async def test_persist_rules_is_idempotent(force_memory_backend):
    rules = _sample_rules()
    await persist_rules("t-idem", rules)
    first = len(await list_rules("t-idem"))
    await persist_rules("t-idem", rules)  # same evidence again
    second = len(await list_rules("t-idem"))
    assert first >= 1
    assert first == second


async def test_persist_rules_fails_closed_without_tenant(force_memory_backend):
    rule = RuleAsset(
        tenant_id=None, pattern_key="k", rule_kind="must", rule_text="t", support=2, confidence=1.0
    )
    assert await persist_rules(None, [rule]) == []
    assert await list_rules(None) == []


async def test_list_rules_is_tenant_isolated(force_memory_backend):
    await persist_rules("t-a", _sample_rules())
    assert await list_rules("t-b") == []
    assert len(await list_rules("t-a")) >= 1
