"""INC17 T05 — anti-drift nails: the main path's tool calls go through ToolExecutor.

There are (historically) **two** ReAct-ish loops in the platform: the main path's
executors (which must call every tool through the single honest entry point,
``runtime.tool_executor.ToolExecutor``) and ``agents/researcher.py``'s own loop
(which calls ``tool.ainvoke`` directly, bypassing ``ToolExecutor`` and therefore
RBAC / HITL / latency / PI-sanitisation).

INC17 keeps ``researcher.py`` **unchanged** but adds this nail so the new main path
can never quietly grow the same bypass. It asserts, over a **declared scan domain**:

  * **source scan** — ``forgeflow/runtime/react_executor.py`` does not mention
    ``researcher`` at all, and DOES reference ``ToolExecutor``;
  * **AST import scan** — no import (module or symbol) in ``react_executor.py``
    resolves to ``forgeflow.agents.researcher``;
  * **orchestrator scan** — the main-path orchestrator does not reference
    ``researcher``;
  * **live shape** — after a real react run, every ``tool_invocations`` record
    carries the ``ToolExecutor``-produced fields (``executed`` / ``invoked`` /
    ``latency_ms`` / ``policy_decision``), i.e. each record *is* an executor record.

Scan domain (explicit): ``forgeflow/runtime/react_executor.py`` and
``forgeflow/runtime/orchestrator.py`` (main path). ``agents/researcher.py`` is
deliberately out of scope (it is not modified by INC17).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, reset_run_store, run_task
from forgeflow.runtime.tool_registry import (
    ToolBinding,
    load_default_bindings,
    register,
    reset_registry,
)

pytestmark = pytest.mark.asyncio

_ROOT = Path(__file__).resolve().parents[2]
_REACT_SRC = _ROOT / "forgeflow" / "runtime" / "react_executor.py"
_ORCH_SRC = _ROOT / "forgeflow" / "runtime" / "orchestrator.py"
_FORBIDDEN = "researcher"


# --------------------------------------------------------------------------- #
# 1. Source scan — no researcher, does use ToolExecutor                          #
# --------------------------------------------------------------------------- #
def test_react_executor_source_has_no_researcher_reference(force_memory_backend):
    text = _REACT_SRC.read_text(encoding="utf-8")
    assert _FORBIDDEN not in text, "react_executor 不得引用会被旁路 PI 防护的 researcher 闭环"
    assert "ToolExecutor" in text, "react_executor 必须经 ToolExecutor 执行工具"


def test_orchestrator_main_path_has_no_researcher_reference(force_memory_backend):
    text = _ORCH_SRC.read_text(encoding="utf-8")
    assert _FORBIDDEN not in text, "主路径 orchestrator 不得引用 researcher"


# --------------------------------------------------------------------------- #
# 2. AST import scan — no import resolves to agents.researcher                   #
# --------------------------------------------------------------------------- #
def test_react_executor_ast_imports_have_no_researcher(force_memory_backend):
    tree = ast.parse(_REACT_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert _FORBIDDEN not in alias.name, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert _FORBIDDEN not in module, module
            for alias in node.names:
                assert _FORBIDDEN not in alias.name, alias.name


# --------------------------------------------------------------------------- #
# 3. Live shape — every recorded invocation is a ToolExecutor record             #
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
        self.model = "fake-react-model"
        self._llm_type = "fake"

    def bind_tools(self, tools):  # noqa: ANN001
        return _ScriptedBound(self)

    def bind(self, **kwargs):  # noqa: ANN003
        return _ScriptedBound(self)

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._next(messages)

    def _next(self, messages) -> AIMessage:  # noqa: ANN001
        if self._script:
            self._last = self._script.pop(0)
        return AIMessage(
            content=self._last.get("content", ""),
            tool_calls=self._last.get("tool_calls", []),
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


async def _fake_research(args, ctx):  # noqa: ANN001
    return {"ok": True, "provider": "test", "results": [], "summary": "ok"}


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
    reset_registry()


async def test_every_invocation_carries_the_executor_fields(monkeypatch, force_memory_backend, fake_research):
    reset_run_store()
    fake = _ScriptedModel([
        {"content": "查", "tool_calls": [{"name": "research.search", "args": {"query": "q"}, "id": "c1"}]},
        {"content": "答", "tool_calls": []},
    ])
    monkeypatch.setattr(orch, "get_settings", lambda: _ProxySettings())
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_ScriptedModel", "model": fake.model}]),
    )

    task = TaskCreate(intent="检索", workflow_type="generic")
    handle = await run_task(
        task, RequestContext(tenant_id="t-d", user_id="u", role="admin"), bus=_RecordingBus()
    )

    records = handle.detail["tool_invocations"]
    assert records
    required_fields = {"tool", "status", "executed", "invoked", "latency_ms", "policy_decision"}
    for record in records:
        assert required_fields <= set(record), record
        # ``executed``/``invoked``/``status`` are consistent ToolExecutor semantics.
        assert isinstance(record["executed"], bool)
        assert isinstance(record["invoked"], bool)

    meta = task.context["llm_runtime"]
    assert "report.render" not in meta["allowed_tools"]
    assert "report.render" in meta["platform_forced_tools"]
    reset_registry()


class _ProxySettings:
    """Settings view forcing the react mode (kept local to the live-shape test)."""

    def __init__(self) -> None:
        self._real = get_settings()

    @property
    def agent_runtime_mode(self) -> str:
        return "react"

    @property
    def llm_provider(self) -> str:
        return "mock"

    def __getattr__(self, item: str):
        return getattr(self._real, item)
