"""FF-R1 P2 — the recovery channel must not reset what B3/B4 already fixed.

P2 ports the snapshot's recovery/resume theme (directory digests,
plan-progress checkpoints). 大川's condition for the port: reuse main's
existing resume machinery instead of re-implementing it, and prove
the two properties earlier batches established still hold **through
the RecoveryStore channel** (the ``/runs/{id}/resume`` route's claim):

* B3 (FF-G9): a resume inherits the spent execution budget — here via
  the recovery state's ``budget`` snapshot, restored by ``run_task``;
* B4 (a1/a2): the identical-call guard's streak survives a resume —
  here via ``identical_call_state`` carried in the recovery state and
  seeded into the same executor-internal run state the checkpoint
  channel uses (one mechanism, two carriers).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import forgeflow.config as config_mod
import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime import llm_planner, recovery
from forgeflow.runtime.execution_budget import ExecutionBudget
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.react_executor import ReactExecutor
from forgeflow.runtime.recovery import RecoveryStore

pytestmark = pytest.mark.asyncio


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event: str, payload: dict | None = None) -> None:
        self.events.append((event, dict(payload or {})))


class _SettingsProxy:
    def __init__(self, real, **overrides) -> None:
        self._real = real
        self._overrides = overrides

    def __getattr__(self, item: str):
        return self._overrides.get(item, getattr(self._real, item))


def _patch_react(monkeypatch, fake) -> None:
    proxy = _SettingsProxy(
        get_settings(),
        agent_runtime_mode="react",
        llm_provider="mock",
        agent_tool_loading="direct",
    )
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_mod, "get_settings", lambda: proxy)
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "fake", "model": fake.model}]),
    )
    import forgeflow.runtime.react_executor as react_module

    monkeypatch.setattr(react_module, "backoff_delay", lambda *a, **k: 0.0)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    llm_planner.clear_caches()
    yield
    reset_run_store()
    llm_planner.clear_caches()


def _recovery_state(**overrides) -> dict:
    base = {
        "task": {"intent": "检索 forgeflow", "title": "t", "workflow_type": "generic"},
        "input_versions": {},
        "budget": ExecutionBudget().snapshot(),
        "messages": [{"type": "human", "data": {"content": "检索 forgeflow"}}],
        "source_run_id": "run-src",
        "inflight": False,
        "evaluation_only": False,
        "unsupported": "",
        "tool_invocations": [],
        "llm_usage": [],
        "tool_schema_hashes": {},
        "loaded_tools": [],
        "context_requests": [],
    }
    base.update(overrides)
    return base


def _claim(tmp_path, state) -> dict:
    store = RecoveryStore(path=tmp_path / "journal.sqlite3")
    version = store.save("t-p2", "u-p2", "run-src", state, status="paused")
    return store.claim("t-p2", "u-p2", "run-src", version)


# --------------------------------------------------------------------------- #
# B3: the spent budget is inherited through the recovery claim                 #
# --------------------------------------------------------------------------- #


class _CountingModel:
    def __init__(self) -> None:
        self.model = "fake-react-model"
        self._llm_type = "fake"
        self.calls = 0

    def bind_tools(self, tools):  # noqa: ANN001, ANN202
        return self

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "research.search",
                    "args": {"query": f"q{self.calls}"},
                    "id": f"c{self.calls}",
                    "type": "tool_call",
                }
            ],
        )


async def test_recovery_resume_inherits_the_spent_budget(tmp_path, monkeypatch):
    # The source run already spent 5 of its 6 model calls.
    spent = ExecutionBudget(model_limit=6, model_calls=5).snapshot()
    claimed = _claim(tmp_path, _recovery_state(budget=spent))
    fake = _CountingModel()
    _patch_react(monkeypatch, fake)
    task = TaskCreate(intent="检索 forgeflow", workflow_type="generic")
    task._recovery_state = claimed
    handle = await run_task(
        task,
        RequestContext(tenant_id="t-p2", user_id="u-p2", role="admin"),
        bus=_Bus(),
    )
    # Exactly one more model call fits the inherited budget — a fresh
    # budget would have allowed six calls and five searches. With the
    # ceiling reached, the loop closes the run honestly ("模型调用预算
    # 已耗尽") without executing the proposed call; the inherited
    # ceiling is what this test pins.
    assert fake.calls == 1
    executions = [
        row
        for row in task.context.get("tool_invocations") or []
        if row.get("tool") == "research.search" and row.get("executed")
    ]
    assert executions == []


# --------------------------------------------------------------------------- #
# B4: the identical-call streak is inherited through the recovery claim        #
# --------------------------------------------------------------------------- #


class _RepeatingModel:
    def __init__(self) -> None:
        self.model = "fake-react-model"
        self._llm_type = "fake"
        self.calls = 0

    def bind_tools(self, tools):  # noqa: ANN001, ANN202
        return self

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN202
        self.calls += 1
        last = messages[-1] if messages else None
        if isinstance(last, HumanMessage) and "最终答复" in str(last.content):
            return AIMessage(content="最终答复：已检索。")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "research.search",
                    "args": {"query": "forgeflow"},
                    "id": f"c{self.calls}",
                    "type": "tool_call",
                }
            ],
        )


async def test_recovery_resume_inherits_the_identical_call_streak(tmp_path, monkeypatch):
    signature = ReactExecutor._tool_call_signature(
        {"name": "research.search", "args": {"query": "forgeflow"}}
    )
    pair = [signature[0], signature[1]]
    claimed = _claim(
        tmp_path,
        _recovery_state(identical_call_state={"signatures": [pair, pair], "executed": [pair]}),
    )
    fake = _RepeatingModel()
    _patch_react(monkeypatch, fake)
    task = TaskCreate(intent="检索 forgeflow", workflow_type="generic")
    task._recovery_state = claimed
    handle = await run_task(
        task,
        RequestContext(tenant_id="t-p2", user_id="u-p2", role="admin"),
        bus=_Bus(),
    )
    executions = [
        row
        for row in task.context.get("tool_invocations") or []
        if row.get("tool") == "research.search" and row.get("executed")
    ]
    # The third identical proposal (the first of the resumed run) hits
    # the inherited streak and forces the final answer — the call is
    # never executed again.
    assert executions == []
    assert handle.status == "completed"
    meta = (task.context.get("llm_runtime") or {}).get("repeated_tool_call_forced_final")
    assert meta is True


# --------------------------------------------------------------------------- #
# The writer side: recovery.checkpoint carries the streak state                #
# --------------------------------------------------------------------------- #


def test_recovery_checkpoint_writes_identical_call_state(tmp_path):
    store = RecoveryStore(path=tmp_path / "journal.sqlite3")
    task = SimpleNamespace(
        context={},
        _identical_call_state={"signatures": [["research.search", "{}"]], "executed": []},
    )
    task._recovery_session = (store, {"input_versions": {}})
    ctx = SimpleNamespace(tenant_id="t-p2", user_id="u-p2", _skill_read_versions={})
    from forgeflow.runtime.execution_budget import current_budget

    token = current_budget.set(ExecutionBudget())
    try:
        recovery.checkpoint(task, ctx, "run-w", [], [], 0)
    finally:
        current_budget.reset(token)
    state = store.get("t-p2", "u-p2", "run-w")["state"]
    assert state["identical_call_state"] == {
        "signatures": [["research.search", "{}"]],
        "executed": [],
    }
