"""INC17 — platform-owned tool inputs are injected by the platform, never by the model.

Why this file exists
--------------------
A **real Ollama end-to-end run** (qa_tmp/inc17/e2e_real_v2.txt) — not a unit test —
exposed this: the ReAct loop passed the model's ``args`` through verbatim, so

  * ``analysis.score`` (which scores ``args["observations"]``, i.e. the run's own
    execution trail) was advertised to the model yet **permanently unusable** —
    it could only ever return ``blocked`` ("observations 为空"); and
  * the mirror risk: a model could hand ``analysis.score`` a self-invented
    ``observations`` list, or ``data.query`` an invented ``table`` /
    ``code.run`` an invented ``paths`` — a **fabricated** number in the
    deliverable.

The fix (``ReactExecutor._effective_args``) splits the two halves: the model
decides *what to ask* (``query`` / ``text`` / ``object`` …); the platform supplies
what the model structurally cannot know (``observations``) and what only an
operator may declare (``table`` / ``paths`` / ``repo_path``).

Measurement scope
-----------------
This file pins the **react** runtime only (scripted model + fake handlers, no
daemon, no provider). It says nothing about ``llm`` / ``deterministic``, whose
args were always resolved by ``orchestrator._execution_args`` — the very single
source of truth this fix reuses.
"""

from __future__ import annotations

import json

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
from forgeflow.runtime.tool_registry import (
    ToolBinding,
    load_default_bindings,
    register,
    reset_registry,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Test doubles (same shape as test_inc17_react_loop.py, kept local on purpose) #
# --------------------------------------------------------------------------- #
class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _ScriptedBound:
    def __init__(self, model: "_ScriptedModel") -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next(messages)


class _ScriptedModel:
    def __init__(self, script: list[dict]) -> None:
        self._script = list(script)
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
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
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


def _patch(monkeypatch, fake: _ScriptedModel) -> None:
    proxy = _SettingsProxy(get_settings(), mode="react", provider="mock")
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_ScriptedModel", "model": fake.model}]),
    )


async def _fake_research(args, ctx):  # noqa: ANN001
    return {
        "ok": True,
        "provider": "test",
        "query": args.get("query"),
        "results": [{"title": "t", "url": "https://example.org/x"}],
        "summary": "fake search ok",
    }


SEEN: dict[str, dict] = {}


def _spy(tool: str, result: dict | None = None):
    """Register a fake handler that records the args it actually received."""

    async def _handler(args, ctx):  # noqa: ANN001
        SEEN[tool] = dict(args)
        return result if result is not None else {"ok": True, "echo": tool}

    return _handler


@pytest.fixture(autouse=True)
def _clean_state():
    SEEN.clear()
    reset_run_store()
    yield
    reset_run_store()
    reset_registry()


@pytest.fixture
def bindings():
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


def _ctx(role: str = "admin", tenant: str = "t-inc17") -> RequestContext:
    return RequestContext(tenant_id=tenant, user_id="u-1", role=role)


async def _drive(monkeypatch, script, *, context=None, role="admin", extras=()):
    fake = _ScriptedModel(script)
    _patch(monkeypatch, fake)
    for tool, result in extras:
        register(
            ToolBinding(
                tool_id=tool,
                handler=_spy(tool, result),
                kind="real",
                provider="test",
                description=f"spy {tool}",
            )
        )
    task = TaskCreate(intent="检索并评分", workflow_type="generic", context=context or {})
    bus = _RecordingBus()
    handle = await run_task(task, _ctx(role=role), bus=bus)
    return fake, task, bus, handle


# --------------------------------------------------------------------------- #
# 1. The defect: analysis.score must receive the run's REAL observations         #
# --------------------------------------------------------------------------- #
async def test_analysis_score_gets_the_runs_real_observations(monkeypatch, bindings):
    """Real ``analysis.score`` handler: it must now be able to score something.

    Pre-fix this round came back ``blocked`` ("没有可打分的 observation 输入"),
    which is what the real Ollama run showed.
    """
    script = [
        {"content": "先检索", "tool_calls": [
            {"name": "research.search", "args": {"query": "q1"}, "id": "c1"}]},
        {"content": "再评分", "tool_calls": [
            {"name": "analysis.score", "args": {}, "id": "c2"}]},
        {"content": "结论", "tool_calls": []},
    ]
    _fake, task, _bus, _handle = await _drive(monkeypatch, script)

    records = task.context["tool_invocations"]
    assert [r["tool"] for r in records] == [
        "research.search", "analysis.score", "report.render"
    ]
    scored = records[1]
    # The whole point: it really executed (it used to be structurally blocked).
    assert scored["executed"] is True
    assert scored["status"] == "ok"
    assert "score" in json.dumps(scored["payload"], ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 2. A model-supplied `observations` is never trusted                            #
# --------------------------------------------------------------------------- #
async def test_model_supplied_observations_are_overridden(monkeypatch, bindings,
                                                          extras=None):
    """The model hands ``analysis.score`` an invented observation list; the
    platform's real trail must win (otherwise the score is fabricated)."""
    fabricated = [{"tool": "fake.success", "executed": True, "status": "ok"}]
    script = [
        {"content": "先检索", "tool_calls": [
            {"name": "research.search", "args": {"query": "q1"}, "id": "c1"}]},
        {"content": "评分", "tool_calls": [
            {"name": "analysis.score",
             "args": {"observations": fabricated, "object": "x"}, "id": "c2"}]},
        {"content": "结论", "tool_calls": []},
    ]
    _fake, task, _bus, _handle = await _drive(
        monkeypatch, script, extras=[("analysis.score", {"ok": True, "scored": True})]
    )

    seen = SEEN["analysis.score"]
    real_trail = task.context["tool_invocations"][:1]
    # 1) the fabricated list did NOT reach the tool
    assert seen["observations"] != fabricated
    # 2) what reached it is the run's own record, verbatim
    assert seen["observations"] == real_trail
    # 3) the model's own choices still survive (it decides WHAT to ask)
    assert seen["object"] == "x"
    # 4) the trace discloses that the platform injected the key
    assert "observations" in task.context["llm_runtime"]["rounds"][1]["injected_args"]
    # 5) the trace still shows what the MODEL asked for
    assert task.context["llm_runtime"]["rounds"][1]["args"] == {
        "observations": fabricated, "object": "x"
    }


# --------------------------------------------------------------------------- #
# 3. Operator-only resources: no explicit input ⇒ no key (fail closed)           #
# --------------------------------------------------------------------------- #
async def test_model_supplied_table_is_dropped_without_operator_input(monkeypatch, bindings):
    """``table`` may only come from an operator's ``explicit_inputs`` — a model
    that invents one must not be able to aim ``data.query`` at it."""
    script = [
        {"content": "查表", "tool_calls": [
            {"name": "data.query", "args": {"table": "invented_table", "sql": "select 1"},
             "id": "c1"}]},
        {"content": "结论", "tool_calls": []},
    ]
    _fake, task, _bus, _handle = await _drive(
        monkeypatch, script, extras=[("data.query", {"ok": True, "rows": []})]
    )

    seen = SEEN["data.query"]
    assert "table" not in seen, "模型自造的 table 不得进入执行参数"
    assert seen["sql"] == "select 1", "模型自己的选择仍然保留"


async def test_operator_declared_table_still_reaches_the_tool(monkeypatch, bindings):
    """Positive control for #3 — with a real ``explicit_inputs`` the key IS
    passed (so #3 proves "not trusted from the model", not "never passed")."""
    script = [
        {"content": "查表", "tool_calls": [
            {"name": "data.query", "args": {"table": "invented_table"}, "id": "c1"}]},
        {"content": "结论", "tool_calls": []},
    ]
    _fake, task, _bus, _handle = await _drive(
        monkeypatch,
        script,
        context={"table": "real_orders_table"},
        extras=[("data.query", {"ok": True, "rows": []})],
    )

    seen = SEEN["data.query"]
    assert seen["table"] == "real_orders_table", "运营显式声明的表必须生效"


# --------------------------------------------------------------------------- #
# 4. The model's own choices are never silently rewritten                        #
# --------------------------------------------------------------------------- #
async def test_model_chosen_query_still_wins(monkeypatch, bindings):
    """Guard against over-correction: the fix must not start overriding the
    model's real decisions (that would turn the loop back into a script)."""
    script = [
        {"content": "检索", "tool_calls": [
            {"name": "research.search", "args": {"query": "模型自己定的检索词"}, "id": "c1"}]},
        {"content": "结论", "tool_calls": []},
    ]
    _fake, task, _bus, _handle = await _drive(monkeypatch, script)

    assert SEEN == {}  # real-ish handler used; check via the recorded payload
    record = task.context["tool_invocations"][0]
    assert '"query": "模型自己定的检索词"' in json.dumps(
        record["payload"], ensure_ascii=False
    ) or "模型自己定的检索词" in json.dumps(record["payload"], ensure_ascii=False)
