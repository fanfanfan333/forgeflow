"""INC2-19 — Skill Evolution: rule pass → ONE batched LLM call → cache.

The 18.6s-per-call constraint makes "loop over skills calling the LLM" the
single most expensive mistake available here. These tests pin the call count.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from forgeflow.skills import evolution


@dataclass
class _Skill:
    id: str
    name: str = "skill"
    status: str = "published"
    usage_count: int = 0
    eval_score: float | None = None
    retire_suggested: bool = False
    last_used_at: datetime | None = None
    current_version: str = "0.1.0"


def _fresh(**kw) -> _Skill:
    return _Skill(id="s-fresh", last_used_at=datetime.now(timezone.utc), usage_count=3, **kw)


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeModel:
    """Counts invocations — the whole point of the batching rule."""

    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.calls = 0
        self.prompts: list[str] = []
        self._replies = replies or {}

    async def ainvoke(self, prompt: str, *a, **k):
        self.calls += 1
        self.prompts.append(prompt)
        payload = [{"id": sid, "reason": reason} for sid, reason in self._replies.items()]
        return _Msg(json.dumps(payload, ensure_ascii=False))


class _Settings:
    def __init__(self, provider: str) -> None:
        self.llm_provider = provider


def test_rule_pass_is_llm_free(monkeypatch):
    """Under mock the LLM must never be touched, and advice is still produced."""
    import forgeflow.models as models

    def _boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("LLM must not be called when LLM_PROVIDER=mock")

    monkeypatch.setattr(models, "get_model", _boom)
    monkeypatch.setattr(evolution, "get_settings", lambda: _Settings("mock"))
    evolution.reset_advice_cache()

    skills = [
        _Skill(id="s-retire", name="旧技能", retire_suggested=True),
        _Skill(id="s-low", name="低分技能", eval_score=0.30, usage_count=9),
        _Skill(id="s-ok", name="健康技能", eval_score=0.95, usage_count=9),
    ]

    advice = _run(evolution.advise("t-evo-1", skills=skills))

    assert [a.skill_id for a in advice] == ["s-retire", "s-low"]
    assert advice[0].kind == "retire"
    assert advice[1].kind == "optimize"
    assert all(a.source == "rule" for a in advice)


def test_idle_unused_skill_is_retirement_candidate():
    evolution.reset_advice_cache()
    old = datetime.now(timezone.utc) - timedelta(days=evolution.RETIRE_IDLE_DAYS + 1)
    skills = [_Skill(id="s-idle", name="沉睡技能", usage_count=0, last_used_at=old)]

    advice = _run(evolution.advise("t-evo-2", skills=skills, use_llm=False))

    assert len(advice) == 1
    assert advice[0].kind == "retire"
    assert "天无调用" in advice[0].reason


def test_llm_is_called_exactly_once_for_the_whole_batch(monkeypatch):
    """20 candidates → 1 prompt, 1 call. Never one call per skill."""
    import forgeflow.models as models

    fake = _FakeModel({f"s-{i}": f"模型建议 {i}" for i in range(20)})
    monkeypatch.setattr(models, "get_model", lambda strong=False: fake)
    monkeypatch.setattr(evolution, "get_settings", lambda: _Settings("ollama"))
    evolution.reset_advice_cache()

    skills = [
        _Skill(id=f"s-{i}", name=f"技能{i}", eval_score=0.1, usage_count=5) for i in range(20)
    ]

    advice = _run(evolution.advise("t-evo-3", skills=skills))

    assert fake.calls == 1, f"expected exactly 1 batched LLM call, got {fake.calls}"
    assert len(advice) == 20
    # Every candidate was packed into that single prompt.
    assert all(f"s-{i}" in fake.prompts[0] for i in range(20))
    assert all(a.source == "llm" for a in advice)
    assert advice[0].reason.startswith("模型建议")


def test_cache_eliminates_the_second_call(monkeypatch):
    """A repeat analysis of an unchanged skill costs zero LLM calls."""
    import forgeflow.models as models

    fake = _FakeModel({"s-cache": "模型建议"})
    monkeypatch.setattr(models, "get_model", lambda strong=False: fake)
    monkeypatch.setattr(evolution, "get_settings", lambda: _Settings("ollama"))
    evolution.reset_advice_cache()

    skills = [_Skill(id="s-cache", name="缓存技能", eval_score=0.2, usage_count=5)]

    first = _run(evolution.advise("t-evo-4", skills=skills))
    second = _run(evolution.advise("t-evo-4", skills=skills))

    assert fake.calls == 1
    assert first[0].reason == second[0].reason


def test_llm_failure_falls_back_to_rule_advice(monkeypatch):
    """An unavailable LLM degrades to rule advice, it does not raise."""
    import forgeflow.models as models

    def _boom(*a, **k):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(models, "get_model", _boom)
    monkeypatch.setattr(evolution, "get_settings", lambda: _Settings("ollama"))
    evolution.reset_advice_cache()

    skills = [_Skill(id="s-fallback", name="降级技能", eval_score=0.2, usage_count=5)]
    advice = _run(evolution.advise("t-evo-5", skills=skills))

    assert len(advice) == 1
    assert advice[0].source == "rule"
    assert "评估得分" in advice[0].reason


def test_advice_becomes_pending_approval_with_kind():
    """§7.5 — advice never mutates a skill; it creates an approval."""
    from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository

    repo = MemoryPolicyRepository()
    advice = _run(
        evolution.advise(
            "t-evo-6",
            skills=[
                _Skill(id="s-a", name="退役技能", retire_suggested=True),
                _Skill(id="s-b", name="优化技能", eval_score=0.2, usage_count=5),
            ],
            use_llm=False,
        )
    )

    saved = _run(evolution.submit_advice_for_approval("t-evo-6", advice, policy_repo=repo))

    assert len(saved) == 2
    pending = _run(repo.list_approvals("t-evo-6", status="pending"))
    kinds = sorted(a.kind for a in pending)
    assert kinds == ["skill.optimize", "skill.retire"]
    # Nothing was applied: the requested action names the skill, nothing else.
    assert all(a.status == "pending" for a in pending)
    assert any("skill.retire:s-a" in a.requested_action for a in pending)


def _run(coro):
    """Run a coroutine from a sync test — a fresh loop each time, always closed."""
    import asyncio

    return asyncio.run(coro)
