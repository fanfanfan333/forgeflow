"""INC-41 F-127 / QA-P2 (+ end-to-end F-124/F-125) — the react profile.

Three defects pinned here; see the round-4 acceptance report.

* **F-127** — a declared CSV attachment was not steered to ``analysis.profile``,
  so the model picked ``data.query`` (which needs a platform ``table``) and every
  round blocked. The fix advertises ``analysis.profile`` to the model (a
  ``_TOOL_DESCRIPTIONS`` entry) and adds an explicit system-prompt rule: with a
  platform-declared local data file, use ``analysis.profile`` and give only the
  ``column`` — do not switch to ``data.query``.

* **F-125 / F-124** — end-to-end: a declared CSV (``paths`` + ``column``) is
  profiled by the react loop with the platform's ``column`` injected (the same
  inputs the deterministic path resolves), so the task is no longer structurally
  blocked and the goal is reachable.

* **QA-P2** — the react path must honour the demo ``simulate_failure`` affordance
  exactly like ``_default_executor`` / ``_llm_executor``. The run is recorded
  honestly as failed **without** degrading the runtime and **without** losing the
  deliverable.

The loop is driven with a scripted model + a fake tool binding — no daemon, no
real provider.

Citation discipline: ``file.py::symbol`` anchors, never line numbers.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.react_executor import _TOOL_DESCRIPTIONS, REACT_SYSTEM
from forgeflow.runtime.tool_registry import (
    ToolBinding,
    load_default_bindings,
    register,
    reset_registry,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Scripted-model harness (mirrors ``test_inc17_react_loop``)                    #
# --------------------------------------------------------------------------- #
class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _ScriptedBound:
    def __init__(self, model: _ScriptedModel) -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next(messages)


class _ScriptedModel:
    def __init__(self, script: list[dict], usage: tuple[int, int] = (10, 5)) -> None:
        self._script = list(script)
        self._usage = usage
        self._last: dict = {"content": "", "tool_calls": []}
        self.calls: list[list] = []
        self.bound_tools: list | None = None
        self.model = "fake-react-model"
        self._llm_type = "fake"

    def bind_tools(self, tools):  # noqa: ANN001
        self.bound_tools = tools
        return _ScriptedBound(self)

    def bind(self, **kwargs):  # noqa: ANN003
        return _ScriptedBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next(messages)

    def _next(self, messages) -> AIMessage:  # noqa: ANN001
        self.calls.append(list(messages))
        if self._script:
            self._last = self._script.pop(0)
        return AIMessage(
            content=self._last.get("content", ""),
            tool_calls=self._last.get("tool_calls", []),
            usage_metadata={
                "input_tokens": self._usage[0],
                "output_tokens": self._usage[1],
                "total_tokens": sum(self._usage),
            },
        )


_UNSET = object()


class _SettingsProxy:
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


def _patch_settings(monkeypatch, *, mode="react", provider="mock") -> None:
    proxy = _SettingsProxy(get_settings(), mode=mode, provider=provider)
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)


def _patch_models(monkeypatch, fake: _ScriptedModel) -> None:
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_ScriptedModel", "model": fake.model}]),
    )


async def _fake_research(args, ctx):  # noqa: ANN001
    return {"ok": True, "provider": "test", "results": [], "summary": "fake search ok"}


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()
    reset_registry()


@pytest.fixture
def fake_research():
    load_default_bindings()
    register(
        ToolBinding(
            tool_id="research.search",
            handler=_fake_research,
            kind="real",
            provider="test",
            description="scripted test search",
        )
    )
    yield


def _ctx(role: str = "admin", tenant: str = "t-inc41") -> RequestContext:
    return RequestContext(tenant_id=tenant, user_id="u-1", role=role)


async def _drive(monkeypatch, script, *, context=None, role="admin", intent="分析数据"):
    fake = _ScriptedModel(script)
    _patch_settings(monkeypatch, mode="react", provider="mock")
    _patch_models(monkeypatch, fake)
    task = TaskCreate(intent=intent, workflow_type="generic", context=context or {})
    bus = _RecordingBus()
    handle = await run_task(task, _ctx(role=role), bus=bus)
    return fake, task, bus, handle


# --------------------------------------------------------------------------- #
# F-127 — the model is steered to ``analysis.profile``                          #
# --------------------------------------------------------------------------- #
async def test_analysis_profile_is_advertised_to_the_model():
    assert "analysis.profile" in _TOOL_DESCRIPTIONS
    desc = _TOOL_DESCRIPTIONS["analysis.profile"]
    assert desc.strip()
    # The description tells the model the platform injects ``paths`` and that it
    # only needs to name the aggregation ``column``.
    assert "column" in desc


async def test_system_prompt_steers_away_from_data_query():
    assert "analysis.profile" in REACT_SYSTEM
    assert "data.query" in REACT_SYSTEM, "系统提示未把模型从 data.query 引向 analysis.profile"


async def test_system_prompt_discloses_platform_injected_repo_path():
    """INC-41 F-136 ② — the prompt discloses the platform-injected ``repo_path``.

    The platform injects ``repo_path`` / ``table`` into the effective args
    (``ReactExecutor.PLATFORM_OWNED_ARGS`` + ``_effective_args``) exactly as it
    injects a declared data file's ``paths``. Rule 6 discloses the
    ``analysis.profile`` path; without a parallel disclosure the model cannot
    know a project/repo task already carries a platform ``repo_path`` and can
    only guess. This pins that isomorphic disclosure while keeping the two
    pre-existing steers intact.
    """
    assert "repo_path" in REACT_SYSTEM
    assert "code.run" in REACT_SYSTEM
    # The rule-6 steers must survive unchanged (substring contract).
    assert "analysis.profile" in REACT_SYSTEM
    assert "data.query" in REACT_SYSTEM


# --------------------------------------------------------------------------- #
# F-125 / F-124 — end-to-end: the declared column is injected                   #
# --------------------------------------------------------------------------- #
async def test_react_injects_platform_column_into_analysis(monkeypatch, force_memory_backend, fake_research):
    seen: dict = {}

    async def _fake_analysis(args, ctx):  # noqa: ANN001
        seen.clear()
        seen.update(args)
        return {"ok": True, "rows": 3, "aggregate_value": 42}

    register(
        ToolBinding(
            tool_id="analysis.profile",
            handler=_fake_analysis,
            kind="real",
            provider="test",
            description="scripted analysis",
        )
    )
    script = [
        {"content": "先做统计分析", "tool_calls": [
            {"name": "analysis.profile", "args": {}, "id": "a1"}]},
        {"content": "最终答复：amount 合计 42", "tool_calls": []},
    ]
    _fake, _task, _bus, _handle = await _drive(
        monkeypatch,
        script,
        context={"paths": ["/tmp/leads.csv"], "column": "amount"},
    )

    # The platform-injected paths + column reached the handler without the model
    # supplying them — the react/deterministic symmetry (F-125).
    assert seen.get("paths") == ["/tmp/leads.csv"]
    assert seen.get("column") == "amount"


# --------------------------------------------------------------------------- #
# QA-P2 — the react path honours ``simulate_failure``                            #
# --------------------------------------------------------------------------- #
async def test_react_honours_simulate_failure_without_degrading(monkeypatch, force_memory_backend, fake_research):
    script = [{"content": "结论：已完成", "tool_calls": []}]
    _fake, task, bus, handle = await _drive(
        monkeypatch, script, context={"simulate_failure": True}
    )

    meta = task.context["llm_runtime"]
    # The react loop completed normally — a missing ``simulate_failure`` binding
    # would raise NameError and silently degrade to the deterministic executor.
    assert "degraded" not in meta, f"react 档意外降级：{meta.get('degraded')}"
    assert meta["terminated_by"] == "model"

    # The failure is recorded honestly, with a run.error event.
    assert any("模拟失败" in e for e in handle.detail["errors"])
    assert any(t == "run.error" for t, _ in bus.events)
    # ...and the deliverable is retained.
    assert any(a["source"] == "report.render" for a in handle.detail["artifacts"])


async def test_react_without_simulate_failure_has_no_simulated_error(monkeypatch, force_memory_backend, fake_research):
    script = [{"content": "结论：一切正常", "tool_calls": []}]
    _fake, _task, bus, handle = await _drive(monkeypatch, script)

    assert not any("模拟失败" in e for e in handle.detail["errors"])
    assert not any(t == "run.error" for t, _ in bus.events)


async def test_react_business_intent_with_failure_word_is_not_forced_to_fail(
    monkeypatch, force_memory_backend, fake_research
):
    """INC-41 F-136 ① — an ordinary business intent that merely *contains* 「失败」
    must run to ``completed``.

    The removed substring heuristic force-failed every such intent and fabricated
    an error reason (``模拟失败：下游工具返回异常``) into the L2/L3 records — both a
    functional-correctness and a **data-honesty** defect. Here the intent says
    「失败的订单」 but carries no explicit ``simulate_failure`` flag.
    """
    script = [{"content": "结论：上季度失败订单的主因是发货延迟", "tool_calls": []}]
    _fake, task, bus, handle = await _drive(
        monkeypatch,
        script,
        intent="分析上季度失败的订单原因并给出结论",
        context={},  # NO simulate_failure flag
    )

    assert handle.status == "completed", "普通业务意图被「失败」子串误判为失败"
    assert not any("模拟失败" in e for e in handle.detail["errors"]), (
        "为普通业务意图伪造了失败原因（数据不诚实）"
    )
    assert not any(t == "run.error" for t, _ in bus.events)


# --------------------------------------------------------------------------- #
# QA-P2 — the ``_simulate_failure`` trigger semantics                           #
# --------------------------------------------------------------------------- #
async def test_simulate_failure_trigger_is_explicit_only():
    """INC-41 F-136 — the substring heuristic is gone; only the explicit flag fires."""
    from forgeflow.runtime.orchestrator import _simulate_failure

    ctx = RequestContext(tenant_id="t", user_id="u", role="admin")

    # The explicit context affordance is the **only** trigger.
    assert _simulate_failure(
        TaskCreate(intent="随便", context={"simulate_failure": True}), ctx
    )

    # A mere 「失败」 in an ordinary business intent must NOT force a failure — the
    # removed substring heuristic force-failed these and faked an error reason.
    assert _simulate_failure(
        TaskCreate(intent="分析上季度失败的订单原因并给出结论", context={}), ctx
    ) is False
    assert _simulate_failure(TaskCreate(intent="处理失败的记录", context={}), ctx) is False
