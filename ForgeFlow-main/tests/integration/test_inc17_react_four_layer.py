"""INC17 T05 — the four-layer contract under the ``react`` runtime mode.

Drives a real ``run_task`` through the ReAct closed loop (scripted model + a fake
tool) and asserts the platform's four-layer contract still holds:

  * **L1 plan ⇄ L2 records, 1:1** — ``len(steps) == len(tool_invocations) ==
    len(plan.steps)``. Both the per-attempt case and the **run-level (with one
    replan)** case are pinned; the react executor keeps ``steps`` *cumulative*
    (same trail as ``tool_invocations``) so the run-level 1:1 holds.
  * **L3 observations** are the pure ``executed is True`` projection of the records
    (verbatim dicts).
  * **``latency_ms`` semantics** — a non-executed record is always ``None`` (never
    a fabricated ``0``); an executed one carries its real measurement.
  * **L4 report** is produced only by ``report.render`` and carries the model's
    final answer verbatim; the last plan step is the deliverable.
  * the HITL / RBAC early-return path sets ``terminated_by == "halted"``, runs **no**
    ``report.render``, and still keeps the attempt 1:1.
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
from forgeflow.runtime.tool_registry import (
    ToolBinding,
    load_default_bindings,
    register,
    reset_registry,
)

pytestmark = pytest.mark.asyncio


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
    def __init__(self, script: list[dict], usage: tuple[int, int] = (10, 5)) -> None:
        self._script = list(script)
        self._usage = usage
        self._last: dict = {"content": "", "tool_calls": []}
        self.calls: list[list] = []
        self.model = "fake-react-model"
        self._llm_type = "fake"

    def bind_tools(self, tools):  # noqa: ANN001
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
    return {"ok": True, "provider": "test", "query": args.get("query"), "results": [], "summary": "ok"}


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


def _ctx(role: str = "admin", tenant: str = "t-inc17-fl") -> RequestContext:
    return RequestContext(tenant_id=tenant, user_id="u-1", role=role)


async def _drive(monkeypatch, script, *, context=None, role="admin"):
    fake = _ScriptedModel(script)
    _patch_settings(monkeypatch, mode="react", provider="mock")
    _patch_models(monkeypatch, fake)
    task = TaskCreate(intent="检索并总结", workflow_type="generic", context=context or {})
    bus = _RecordingBus()
    handle = await run_task(task, _ctx(role=role), bus=bus)
    return fake, task, bus, handle


def _assert_latency_semantics(records):
    """A non-executed record's latency is ``None``; never a fabricated ``0``."""
    for r in records:
        if r.get("executed") is not True:
            assert r.get("latency_ms") is None, r


def _assert_observation_projection(handle, records):
    obs = handle.detail["observations"]
    assert obs == [r for r in records if r.get("executed") is True]


# --------------------------------------------------------------------------- #
# 1. Single attempt — the full four layers, 1:1                                  #
# --------------------------------------------------------------------------- #
async def test_single_attempt_four_layers(monkeypatch, force_memory_backend, fake_research):
    script = [
        {"content": "查", "tool_calls": [{"name": "research.search", "args": {"query": "q"}, "id": "c1"}]},
        {"content": "最终答复：检索完成，结论为 A", "tool_calls": []},
    ]
    _fake, task, _bus, handle = await _drive(monkeypatch, script)

    records = handle.detail["tool_invocations"]
    steps = handle.detail["steps"]
    plan = task.context["plan"]

    # L1 ⇄ L2 (per-attempt): strict 1:1, step_id aligned.
    assert len(steps) == len(records) == len(plan["steps"]) == 2
    assert [s["step_id"] for s in steps] == [r["step_id"] for r in records]
    assert plan["steps"][-1]["tool"] == "report.render"
    assert records[-1]["tool"] == "report.render"

    # L3 observations are the executed-only projection.
    _assert_observation_projection(handle, records)
    # latency semantics
    _assert_latency_semantics(records)

    # L4 report carries the model's answer verbatim; produced only by report.render.
    artifacts = handle.detail["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["source"] == "report.render"
    assert "最终答复：检索完成，结论为 A" in artifacts[0]["content"]

    # Mode markers.
    meta = task.context["llm_runtime"]
    assert meta["terminated_by"] == "model"
    assert "report.render" not in meta["allowed_tools"]
    assert meta["platform_forced_tools"] == ["report.render"]


# --------------------------------------------------------------------------- #
# 2. Run-level 1:1 with ONE replan (steps & records both cumulative)             #
# --------------------------------------------------------------------------- #
async def test_run_level_1to1_across_one_replan(monkeypatch, force_memory_backend, fake_research):
    # Attempt 0: the model calls an UNBOUND tool (skill.publish) ⇒ recorded
    # ``unavailable`` ⇒ an error ⇒ the run fails and replans. Attempt 1: the model
    # converges cleanly. The closing report.render runs on *both* attempts.
    script = [
        {"content": "尝试发布技能", "tool_calls": [
            {"name": "skill.publish", "args": {"name": "x"}, "id": "s1"}]},
        {"content": "第一轮结束", "tool_calls": []},
        {"content": "最终答复：第二轮完成", "tool_calls": []},
    ]
    _fake, task, _bus, handle = await _drive(monkeypatch, script)

    records = handle.detail["tool_invocations"]
    steps = handle.detail["steps"]
    plan = task.context["plan"]

    # Two attempts really happened.
    attempts = {r["attempt"] for r in records}
    assert {0, 1} <= attempts

    # RUN-LEVEL 1:1 — steps and records are BOTH the cumulative trail.
    assert len(steps) == len(records) == len(plan["steps"])
    assert [s["step_id"] for s in steps] == [r["step_id"] for r in records]
    assert steps[-1]["tool"] == "report.render"

    _assert_observation_projection(handle, records)
    _assert_latency_semantics(records)

    # The final attempt's report carries the model's final answer.
    assert "最终答复：第二轮完成" in handle.detail["artifacts"][-1]["content"]


# --------------------------------------------------------------------------- #
# 3. HITL / RBAC early return — halted, no report.render, still 1:1              #
# --------------------------------------------------------------------------- #
async def test_hitl_halt_is_one_to_one_and_skips_report(monkeypatch, force_memory_backend, fake_research):
    script = [{"content": "发起转账", "tool_calls": [
        {"name": "payment.transfer", "args": {"amount": 1}, "id": "p1"}]}]
    _fake, task, bus, handle = await _drive(monkeypatch, script)

    meta = task.context["llm_runtime"]
    assert meta["terminated_by"] == "halted"
    assert meta["final_answer"] is None

    records = handle.detail["tool_invocations"]
    steps = handle.detail["steps"]
    assert records == []
    assert steps == []
    assert len(steps) == len(records)  # 1:1 (0 == 0)
    # No report.render ran on the halted path.
    assert not any(t == "run.observation" and d.get("tool") == "report.render" for t, d in bus.events)
    assert not any(e for e in handle.detail.get("artifacts", []))
    assert handle.detail["errors"]
