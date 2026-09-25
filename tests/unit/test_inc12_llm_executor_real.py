"""INC12 A1 — the runtime executors record REAL tool invocations.

Where ``test_runtime_llm_agent`` pins that the LLM plan *drives* the run, this
suite pins that a step's ``status`` reflects what **actually happened**:

  * a plan step whose handler had no valid input is ``skipped`` (``executed
    False``) — NOT the old fake ``ok``;
  * every step carries its ``observation`` (the full invocation);
  * the run's ``tool_invocations`` reach ``GET /runs/{id}`` (the "schema has a
    field but the router drops it" defect);
  * every replan round leaves its own invocations on the trail.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime import llm_planner
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Test doubles (same shape as test_runtime_llm_agent)                          #
# --------------------------------------------------------------------------- #
class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _FakeBound:
    def __init__(self, model: "_FakeModel") -> None:
        self._model = model

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        return self._model._next()


class _FakeModel:
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
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_FakeModel", "model": fake.model}]),
    )


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    llm_planner.clear_caches()
    yield
    reset_run_store()
    llm_planner.clear_caches()


def _ctx(role: str = "admin", tenant: str = "t-inc12") -> RequestContext:
    return RequestContext(tenant_id=tenant, user_id="u-1", role=role)


# --------------------------------------------------------------------------- #
# 1. A step with no valid input is skipped, never a fake ok                    #
# --------------------------------------------------------------------------- #
async def test_llm_step_with_no_input_is_skipped_not_ok(monkeypatch, force_memory_backend):
    # report.render is FIRST, so it has no prior observation to render ⇒ skipped.
    plan_json = (
        '{"steps": [{"tool": "report.render", "note": "渲染报告"}, '
        '{"tool": "docs.parse", "note": "解析意图文本"}]}'
    )
    reflect_json = '{"success": true, "score": 0.9, "summary": "完成", "reasons": ["ok"]}'
    fake = _FakeModel([plan_json, reflect_json])

    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    _patch_models(monkeypatch, fake)

    bus = _RecordingBus()
    handle = await run_task(
        TaskCreate(intent="分析华东区销售数据并生成报告", workflow_type="generic"),
        _ctx(),
        bus=bus,
    )

    steps = handle.detail["steps"]
    assert [s["tool"] for s in steps] == ["report.render", "docs.parse"]
    # The counter-example: the un-renderable first step is NOT recorded ok.
    assert steps[0]["status"] == "skipped"
    assert steps[0]["observation"]["executed"] is False
    assert steps[1]["status"] == "ok"
    assert steps[1]["observation"]["executed"] is True

    # Every step carries its full observation.
    assert all("observation" in s for s in steps)

    # run.step.done carries the REAL status + observation; a skipped step warns.
    done = [d for t, d in bus.events if t == "run.step.done"]
    assert done[0]["status"] == "skipped"
    assert "observation" in done[0]
    assert any(t == "run.warning" and d.get("reason") == "tool_skipped" for t, d in bus.events)
    assert any(t == "run.observation" for t, _ in bus.events)

    # The run still completes — a skipped step is not a failure.
    assert handle.status == "completed"
    assert handle.detail["errors"] == []


# --------------------------------------------------------------------------- #
# 2. Invocations are persisted on the run and exposed through GET /runs/{id}    #
# --------------------------------------------------------------------------- #
def _runs_client(tenant: str, record) -> TestClient:
    from fastapi import FastAPI

    from forgeflow.api.hub_deps import resolve_tenant
    from forgeflow.api.routers import runs as runs_router

    app = FastAPI()
    app.dependency_overrides[resolve_tenant] = lambda: tenant
    get_run_store().save(record)
    app.include_router(runs_router.router, prefix="/runs")
    return TestClient(app)


async def test_run_detail_exposes_tool_invocations(monkeypatch, force_memory_backend):
    plan_json = '{"steps": [{"tool": "docs.parse", "note": "解析意图文本"}]}'
    reflect_json = '{"success": true, "score": 1.0, "summary": "完成"}'
    fake = _FakeModel([plan_json, reflect_json])

    _patch_settings(monkeypatch, mode="llm", provider="ollama")
    _patch_models(monkeypatch, fake)

    handle = await run_task(
        TaskCreate(intent="汇总上季度订单", workflow_type="generic"),
        _ctx(tenant="t-inc12-detail"),
        bus=_RecordingBus(),
    )
    record = get_run_store().get(handle.run_id)
    assert record.tool_invocations  # persisted on the run

    client = _runs_client(record.tenant_id, record)
    response = client.get(f"/runs/{handle.run_id}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert isinstance(body["tool_invocations"], list)
    assert len(body["tool_invocations"]) == len(record.tool_invocations)
    first = body["tool_invocations"][0]
    assert first["tool"] == "docs.parse"
    assert first["status"] == "ok"
    assert first["executed"] is True


# --------------------------------------------------------------------------- #
# 3. Every replan round leaves its own invocations                             #
# --------------------------------------------------------------------------- #
async def test_replan_leaves_every_round_on_the_trail(force_memory_backend):
    bus = _RecordingBus()
    handle = await run_task(
        TaskCreate(
            intent="离线确定性跑一遍并触发重规划",
            workflow_type="generic",
            context={"simulate_failure": True},
        ),
        _ctx(tenant="t-inc12-replan"),
        bus=bus,
    )

    invocations = handle.detail["tool_invocations"]
    attempts = {inv["attempt"] for inv in invocations}
    # At least the first two rounds must both be present (not just the last).
    assert {0, 1} <= attempts
    # Each round runs the three deterministic steps.
    assert len(invocations) >= 6
    # Every recorded invocation is a real, resolved tool id.
    assert {inv["tool"] for inv in invocations} >= {"research.search", "data.query", "code.run"}
