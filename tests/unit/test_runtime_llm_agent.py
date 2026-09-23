"""INC4 §A regression: the Agent runtime must *really* use the configured LLM.

Before INC4 the default path behind ``POST /tasks`` (``_default_executor``)
walked the hard-coded ``_DEFAULT_STEPS`` constant, made **no** LLM call, never
planned and never reflected — so a configured Ollama provider was never used by
the agent's main execution path.

These tests pin the contract of the new LLM path
(``orchestrator._llm_executor`` + ``runtime/llm_planner``):

  * the offline (``LLM_PROVIDER=mock``) default keeps the deterministic path
    byte-for-byte — the 668-case offline suite is unaffected;
  * ``agent_runtime_mode="llm"`` selects the LLM path and the plan is taken from
    the *model's* JSON output (not ``_DEFAULT_STEPS``);
  * the same RBAC + HITL gates run inside the LLM path (a high-risk tool is
    blocked before it executes);
  * a hallucinated tool name is dropped, never executed;
  * real token usage lands on the ``RunRecord``;
  * planning and reflection are **one batched call each** (no per-step call).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.governance.policy_engine import PolicyEngine
from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
from forgeflow.runtime import llm_planner
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    resolve_agent_runtime_mode,
    run_task,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Test doubles                                                                 #
# --------------------------------------------------------------------------- #
class _RecordingBus:
    """Minimal bus stub — run_task only ever calls ``emit``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _FakeBound:
    def __init__(self, model: _FakeModel) -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next()


class _FakeModel:
    """A tiny chat-model stand-in that returns scripted JSON with real usage.

    Only the surface ``LLMPlanner`` uses is implemented (``bind`` →
    ``ainvoke``), which keeps the test free of any provider SDK. When the
    script is exhausted the last payload repeats, so a replan loop stays
    deterministic.
    """

    def __init__(self, payloads: list[str], usage: tuple[int, int] = (10, 5)) -> None:
        self._payloads = list(payloads)
        self._usage = usage
        self._last = "{}"
        self.calls: list[str] = []
        self.model = "fake-test-model"
        self._llm_type = "fake"

    def bind(self, **kwargs):  # noqa: ANN003
        return _FakeBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next()

    def _next(self) -> AIMessage:
        if self._payloads:
            self._last = self._payloads.pop(0)
        self.calls.append(self._last)
        return AIMessage(
            content=self._last,
            usage_metadata={
                "input_tokens": self._usage[0],
                "output_tokens": self._usage[1],
                "total_tokens": sum(self._usage),
            },
        )


_UNSET = object()


class _SettingsProxy:
    """Read-through proxy that overrides ``agent_runtime_mode`` / ``llm_provider``.

    ``Settings`` is a frozen-shape pydantic model, so an undeclared attribute
    cannot be set on an instance (``ValueError``). The proxy lets a test choose
    the mode without touching ``config.py`` (which another engineer owns).
    """

    def __init__(self, real, *, mode: object = _UNSET, provider: object = _UNSET) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_mode", mode)
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, item: str):
        if item == "agent_runtime_mode" and self._mode is not _UNSET:
            return self._mode
        if item == "llm_provider" and self._provider is not _UNSET:
            return self._provider
        return getattr(self._real, item)


def _patch_settings(monkeypatch, *, mode=None, provider=None) -> None:
    proxy = _SettingsProxy(
        get_settings(),
        mode=_UNSET if mode is None else mode,
        provider=_UNSET if provider is None else provider,
    )
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)


def _patch_models(monkeypatch, fake: _FakeModel) -> None:
    """Force the LLM path to use the scripted fake model."""
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_FakeModel", "model": fake.model}]),
    )


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts with an empty run store and empty planner caches."""
    reset_run_store()
    llm_planner.clear_caches()
    yield
    reset_run_store()
    llm_planner.clear_caches()


def _ctx(role: str = "admin", tenant: str = "t-llm") -> RequestContext:
    return RequestContext(tenant_id=tenant, user_id="u-1", role=role)


# --------------------------------------------------------------------------- #
# 1. Mode resolution                                                           #
# --------------------------------------------------------------------------- #
async def test_auto_resolves_from_provider(monkeypatch):
    _patch_settings(monkeypatch, mode="auto", provider="mock")
    assert resolve_agent_runtime_mode() == "deterministic"

    _patch_settings(monkeypatch, mode="auto", provider="ollama")
    assert resolve_agent_runtime_mode() == "llm"


async def test_explicit_mode_pins_the_path(monkeypatch):
    # Explicit llm + mock provider must still select the LLM path (so the path
    # is testable offline); explicit deterministic wins even with a real provider.
    _patch_settings(monkeypatch, mode="llm", provider="mock")
    assert resolve_agent_runtime_mode() == "llm"

    _patch_settings(monkeypatch, mode="deterministic", provider="ollama")
    assert resolve_agent_runtime_mode() == "deterministic"


async def test_unknown_mode_falls_back_to_auto(monkeypatch):
    _patch_settings(monkeypatch, mode="bogus", provider="mock")
    assert resolve_agent_runtime_mode() == "deterministic"
    _patch_settings(monkeypatch, mode="bogus", provider="ollama")
    assert resolve_agent_runtime_mode() == "llm"


# --------------------------------------------------------------------------- #
# 2. Offline default is unchanged                                              #
# --------------------------------------------------------------------------- #
async def test_offline_default_is_still_deterministic(monkeypatch):
    # conftest pins LLM_PROVIDER=mock, so auto ⇒ deterministic.
    settings = get_settings()
    if settings.llm_provider != "mock":  # pragma: no cover — guarded in conftest
        pytest.skip("offline profile expected")

    bus = _RecordingBus()
    handle = await run_task(
        TaskCreate(intent="分析华东地区销售数据并生成报告", workflow_type="generic"),
        _ctx(),
        bus=bus,
    )

    assert handle.status == "completed"
    assert handle.detail["runtime_mode"] == "deterministic"
    assert handle.detail["llm"] is None
    assert [s["tool"] for s in handle.detail["steps"]] == [
        "research.search",
        "data.query",
        "code.run",
    ]
    assert get_run_store().get(handle.run_id).runtime_mode == "deterministic"


# --------------------------------------------------------------------------- #
# 3. The LLM path is selected and the plan comes from the model                #
# --------------------------------------------------------------------------- #
async def test_llm_path_uses_mock_provider_without_crashing(monkeypatch):
    """Explicit llm + LLM_PROVIDER=mock ⇒ the real MockChatModel drives both
    phases; its empty ``steps`` falls back to the deterministic plan safely."""
    _patch_settings(monkeypatch, mode="llm", provider="mock")

    bus = _RecordingBus()
    handle = await run_task(
        TaskCreate(intent="整理客户反馈要点", workflow_type="generic"), _ctx(), bus=bus
    )

    assert handle.detail["runtime_mode"] == "llm"
    llm = handle.detail["llm"]
    assert llm is not None
    # The mock reply carries no usable plan ⇒ honest fallback, run still completes.
    assert llm["plan"]["source"] == "fallback"
    assert handle.status == "completed"
    assert any(t == "run.plan" for t, _ in bus.events)
    assert any(t == "run.reflection" for t, _ in bus.events)


async def test_plan_steps_come_from_the_model(monkeypatch):
    plan_json = (
        '{"reasoning": "先查数据再出报告", "steps": ['
        '{"tool": "data.query", "note": "查询华东区 Q3 数据"}, '
        '{"tool": "report.render", "note": "生成一页报告"}]}'
    )
    reflect_json = '{"success": true, "score": 0.9, "summary": "完成", "reasons": ["步骤齐全"]}'
    fake = _FakeModel([plan_json, reflect_json])

    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    _patch_models(monkeypatch, fake)

    bus = _RecordingBus()
    handle = await run_task(
        TaskCreate(intent="分析华东区 Q3 销售数据并生成报告", workflow_type="generic"),
        _ctx(),
        bus=bus,
    )

    assert handle.status == "completed"
    assert handle.detail["runtime_mode"] == "llm"

    steps = handle.detail["steps"]
    tools = [s["tool"] for s in steps]
    # The steps are the MODEL's plan — different from the hard-coded default.
    assert tools == ["data.query", "report.render"]
    assert tools != ["research.search", "data.query", "code.run"]
    assert steps[0]["note"] == "查询华东区 Q3 数据"

    llm = handle.detail["llm"]
    assert llm["plan"]["source"] == "llm"
    assert llm["plan"]["steps"] == ["data.query", "report.render"]
    assert llm["reflection"]["source"] == "llm"
    assert llm["reflection"]["success"] is True

    # Planning + reflection = exactly two model calls (no per-step call).
    assert len(fake.calls) == 2

    # Real usage landed on the run record (10 + 5 tokens × 2 calls).
    record = get_run_store().get(handle.run_id)
    assert record is not None
    assert record.runtime_mode == "llm"
    assert record.total_tokens == 30
    assert handle.detail["total_tokens"] == 30
    assert record.cost_by_agent  # per-agent ledger is populated


async def test_reflection_can_fail_a_run(monkeypatch):
    plan_json = '{"steps": [{"tool": "data.query", "note": "查数据"}]}'
    reflect_json = '{"success": false, "score": 0.1, "summary": "未完成意图", "reasons": ["缺少报告"]}'
    fake = _FakeModel([plan_json, reflect_json])

    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    _patch_models(monkeypatch, fake)

    handle = await run_task(
        TaskCreate(intent="分析销售数据并产出结论文档", workflow_type="generic"),
        _ctx(),
        bus=_RecordingBus(),
    )

    assert handle.status == "failed"
    assert any("反思" in e for e in handle.detail["errors"])
    assert handle.detail["llm"]["reflection"]["success"] is False


# --------------------------------------------------------------------------- #
# 4. The same safety gates run in the LLM path                                 #
# --------------------------------------------------------------------------- #
async def test_high_risk_tool_is_blocked_in_the_llm_path(monkeypatch):
    plan_json = '{"steps": [{"tool": "payment.transfer", "note": "发起转账"}]}'
    fake = _FakeModel([plan_json])

    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    _patch_models(monkeypatch, fake)

    tenant = "t-llm-hitl"
    repo = MemoryPolicyRepository()
    engine = PolicyEngine(repo=repo)
    bus = _RecordingBus()

    handle = await run_task(
        TaskCreate(intent="处理供应商付款", workflow_type="generic"),
        _ctx(tenant=tenant),
        bus=bus,
        policy_engine=engine,
    )

    # Blocked *before* execution — no step ran.
    assert handle.status == "failed"
    assert handle.detail["steps"] == []
    assert any("拦截" in e for e in handle.detail["errors"])
    assert any(t == "run.error" for t, _ in bus.events)

    approvals = await repo.list_approvals(tenant, status="pending")
    assert len(approvals) >= 1
    assert any("transfer" in a.requested_action for a in approvals)

    # Only the planning call happened; a blocked plan is not reflected on, and
    # the replan loop reuses the cached plan (no extra model call).
    assert len(fake.calls) == 1


async def test_hallucinated_tool_is_dropped(monkeypatch):
    plan_json = (
        '{"steps": [{"tool": "made.up.tool", "note": "幻觉工具"}, '
        '{"tool": "data.query", "note": "查数据"}]}'
    )
    reflect_json = '{"success": true, "score": 1.0, "summary": "完成"}'
    fake = _FakeModel([plan_json, reflect_json])

    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    _patch_models(monkeypatch, fake)

    handle = await run_task(
        TaskCreate(intent="查询订单数据并汇总", workflow_type="generic"),
        _ctx(),
        bus=_RecordingBus(),
    )

    llm = handle.detail["llm"]
    assert llm["plan"]["dropped_tools"] == ["made.up.tool"]
    assert [s["tool"] for s in handle.detail["steps"]] == ["data.query"]


async def test_role_cannot_plan_a_tool_it_cannot_execute(monkeypatch):
    """A sales_rep plan is filtered to tools the role may run; a privileged
    catalogue tool the role lacks is dropped rather than executed."""
    plan_json = (
        '{"steps": [{"tool": "analysis.score", "note": "打分"}, '
        '{"tool": "data.query", "note": "查数据"}]}'
    )
    reflect_json = '{"success": true, "score": 1.0, "summary": "完成"}'
    fake = _FakeModel([plan_json, reflect_json])

    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    _patch_models(monkeypatch, fake)

    handle = await run_task(
        TaskCreate(intent="给最近的线索打分", workflow_type="generic"),
        _ctx(role="sales_rep"),
        bus=_RecordingBus(),
    )

    # analysis.score needs execute:analysis which sales_rep does not hold.
    assert handle.detail["llm"]["plan"]["dropped_tools"] == ["analysis.score"]
    assert [s["tool"] for s in handle.detail["steps"]] == ["data.query"]
