"""INC23 N3 — the react-mode cross-layer pin: a ``blocked`` step stays honest on
**every** layer, including the user-visible report body.

The symptom this pins (documented in the INC23 change request, verbatim): the
react report body once carried BOTH 「适用性：required」 (the L1 plan line) and
「受阻（BLOCKED）」 (the L2 record line) for the *same* step. The plan claimed a
step was required while the execution record said it had been blocked — two layers
of one run contradicting each other in the deliverable.

The fix makes ``plan_from_records`` project a ``blocked`` L2 record as
``applicability == "blocked"`` (with the stated reason carried out verbatim), and
``PlanStep.to_payload`` emit the same ``blocked_reason``, so L1 ⇄ L2 agree and the
report's 任务计划 section renders 「适用性：blocked」.

How a *real* ``blocked`` is driven end-to-end (no hand-built record): the model's
**first** tool call is ``analysis.score``, which scores ``args["observations"]`` —
a **platform-owned** input (``ReactExecutor.PLATFORM_OWNED_ARGS``). On the first
call the run has produced no observation yet, so the platform supplies ``[]``;
the handler then fails closed (``not_executed``) and ``ToolExecutor`` records an
honest ``blocked``. Nothing here is fabricated.

This file drives a real ``run_task`` through the ReAct loop (scripted model, same
harness as ``tests/integration/test_inc17_react_four_layer.py``) and asserts:

* (a) ``task.context["plan"]["steps"][i]["applicability"] == "blocked"`` with a
  non-empty ``blocked_reason``;
* (b) ``handle.detail["steps"][i]["applicability"] == "blocked"`` and its
  ``blocked_reason`` is **byte-for-byte equal** to (a) — one reason, one voice;
* (c) in ``handle.detail["artifacts"][0]["content"]`` the 「任务计划」 section line
  for that step contains 「适用性：blocked」 and does **not** contain
  「适用性：required」 — the exact user-visible symptom above, gone.

A companion control drives a *successful* step through the same plumbing and
asserts the section renders 「适用性：required」, so (c) cannot pass by always
reading ``blocked``.

Citation discipline: this file uses ``file::symbol`` anchors (never ``file:line``).
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
def default_bindings():
    """Load the platform's real default bindings (so ``analysis.score`` resolves).

    ``research.search`` is overridden with a deterministic fake so the control run
    below converges without a network / Tavily dependency.
    """
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


def _ctx(role: str = "admin", tenant: str = "t-inc23-n3") -> RequestContext:
    return RequestContext(tenant_id=tenant, user_id="u-1", role=role)


async def _drive(monkeypatch, script, *, context=None, role="admin"):
    fake = _ScriptedModel(script)
    _patch_settings(monkeypatch, mode="react", provider="mock")
    _patch_models(monkeypatch, fake)
    task = TaskCreate(intent="分析本次运行", workflow_type="generic", context=context or {})
    bus = _RecordingBus()
    handle = await run_task(task, _ctx(role=role), bus=bus)
    return fake, task, bus, handle


def _plan_section(markdown: str) -> str:
    """The verbatim body of the report's 「任务计划」 (Task Plan) section."""
    out: list[str] = []
    grab = False
    for line in markdown.split("\n"):
        if line.startswith("## "):
            if grab:
                break
            grab = "任务计划" in line
            continue
        if grab:
            out.append(line)
    return "\n".join(out)


def _detail_step(handle, tool: str) -> dict:
    hits = [s for s in handle.detail["steps"] if s.get("tool") == tool]
    assert len(hits) == 1, f"expected exactly one {tool!r} step, found {len(hits)}"
    return hits[0]


def _plan_step(plan: dict, tool: str) -> dict:
    hits = [s for s in plan.get("steps", []) if s.get("tool") == tool]
    assert len(hits) == 1, f"expected exactly one {tool!r} plan step, found {len(hits)}"
    return hits[0]


# --------------------------------------------------------------------------- #
# The pin: a blocked step is honest on L1, L2 AND in the visible report body     #
# --------------------------------------------------------------------------- #
async def test_react_blocked_step_is_blocked_on_every_layer(
    monkeypatch, force_memory_backend, default_bindings
):
    """A ``blocked`` step ⇒ L1 ``blocked`` / L2 ``blocked`` / report 「适用性：blocked」.

    The model's first call is ``analysis.score`` — a tool whose ``observations``
    input is platform-owned and (on the first call) empty, so it fails closed to
    ``blocked`` through the **real** ``ToolExecutor``.
    """
    script = [
        {"content": "先对本次运行打分", "tool_calls": [
            {"name": "analysis.score", "args": {}, "id": "c1"}]},
        {"content": "最终答复：分析完成", "tool_calls": []},
    ]
    _fake, task, _bus, handle = await _drive(monkeypatch, script)

    # Fixture reality — the run really recorded a blocked analysis.score (L2).
    rec = next(
        (r for r in handle.detail["tool_invocations"] if r.get("tool") == "analysis.score"),
        None,
    )
    assert rec is not None, "analysis.score was never recorded — the blocked path was not driven"
    assert rec["status"] == "blocked", rec
    assert rec["executed"] is False, rec

    # (a) L1 plan: the step is blocked, with a stated (non-empty) reason.
    plan = task.context["plan"]
    ps = _plan_step(plan, "analysis.score")
    assert ps["applicability"] == "blocked", ps
    assert ps["blocked_reason"], f"blocked plan step carries no reason: {ps}"
    # the run record's plan is the same object/value (single source).
    assert handle.detail["plan"]["steps"] == plan["steps"]

    # (b) L2 steps payload: same applicability AND byte-for-byte the same reason.
    ds = _detail_step(handle, "analysis.score")
    assert ds["applicability"] == "blocked", ds
    assert ds["blocked_reason"] == ps["blocked_reason"], (
        "L1 plan and L2 steps disagree on the blocked reason:\n"
        f"  plan  : {ps['blocked_reason']!r}\n"
        f"  steps : {ds['blocked_reason']!r}"
    )

    # (c) The user-visible symptom, gone: the report's 任务计划 line says blocked,
    #     never required.
    artifacts = handle.detail["artifacts"]
    assert artifacts and artifacts[0]["source"] == "report.render", artifacts
    section = _plan_section(artifacts[0]["content"])
    step_line = next((ln for ln in section.split("\n") if "analysis.score" in ln), None)
    assert step_line is not None, f"the blocked step is missing from the 任务计划 section:\n{section}"
    assert "适用性：blocked" in step_line, step_line
    assert "适用性：required" not in step_line, step_line
    assert "适用性：required" not in section, (
        "report 任务计划 section still claims a required step alongside a blocked one:\n" + section
    )


# --------------------------------------------------------------------------- #
# Discriminating companion: the same assertion renders 「required」 for a success  #
# --------------------------------------------------------------------------- #
async def test_react_successful_step_renders_required_in_the_plan_section(
    monkeypatch, force_memory_backend, default_bindings
):
    """Control — a successful step's 任务计划 line says 「适用性：required」.

    Proves (c) is discriminating: it is not that the section always reads
    ``blocked``.
    """
    script = [
        {"content": "检索", "tool_calls": [
            {"name": "research.search", "args": {"query": "q"}, "id": "c1"}]},
        {"content": "最终答复：检索完成", "tool_calls": []},
    ]
    _fake, task, _bus, handle = await _drive(monkeypatch, script)

    ps = _plan_step(task.context["plan"], "research.search")
    assert ps["applicability"] == "required", ps

    artifacts = handle.detail["artifacts"]
    assert artifacts, "no report artifact was produced"
    section = _plan_section(artifacts[0]["content"])
    step_line = next((ln for ln in section.split("\n") if "research.search" in ln), None)
    assert step_line is not None, f"the successful step is missing from the 任务计划 section:\n{section}"
    assert "适用性：required" in step_line, step_line
    assert "适用性：blocked" not in section, section
