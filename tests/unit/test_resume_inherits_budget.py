"""FF-R1 B3 (FF-G9) — a checkpoint resume inherits the spent budget.

``build_resume_seed`` used to state the policy in its own docstring:
"the resumed run gets a FRESH model-call budget". The ExecutionBudget
ceiling was restored through a separate lineage channel, but the
checkpoint seed — the artefact that describes what the source run had
already done — carried no budget usage at all, and the react loop's
own turn counter restarted from zero, handing every resume a full
new set of loop turns on top of the source run's.

Now the checkpoint state records ``budget_usage`` (model/tool calls
already spent), the seed surfaces it, and the loop initialises its
turn counter from it.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

import forgeflow.config as config_mod
import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime import llm_planner
from forgeflow.runtime.checkpoints import (
    InMemoryCheckpointStore,
    build_resume_seed,
    build_state,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    run_task,
)

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _CountingReactModel:
    """Always proposes a *distinct* search call, then a final text."""

    def __init__(self) -> None:
        self.model = "fake-react-model"
        self._llm_type = "fake"
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls += 1
        if self.calls <= 10:
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
        return AIMessage(content="已有证据收尾。")


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
        lambda: (
            fake,
            fake,
            [{"slot": "strong", "class": "_CountingReactModel", "model": fake.model}],
        ),
    )


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    reset_checkpoint_store()
    llm_planner.clear_caches()
    yield
    reset_run_store()
    reset_checkpoint_store()
    llm_planner.clear_caches()


def _state(budget_usage=None):
    return build_state(
        intent="检索并总结",
        requirements=[],
        acceptance={"round": 1, "complete": False, "requirements": []},
        next_step={"kind": "model_call"},
        codeplane_ref=None,
        breaker_summary={},
        stuck_summary={},
        approval=None,
        pending_writes=[],
        trajectory_cursor=0,
        budget_usage=budget_usage,
    )


def test_build_state_and_seed_carry_budget_usage():
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    checkpoint = store.save(
        "t-react",
        run_id="src-1",
        round=1,
        kind="round",
        state=_state(budget_usage={"model_calls": 5, "tool_calls": 3}),
    )
    seed = build_resume_seed(checkpoint, [])
    assert seed["budget_usage"] == {"model_calls": 5, "tool_calls": 3}


def test_seed_without_recorded_usage_stays_fresh():
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    checkpoint = store.save("t-react", run_id="src-1", round=1, kind="round", state=_state())
    seed = build_resume_seed(checkpoint, [])
    assert seed.get("budget_usage") is None


async def _drive_resume(monkeypatch, budget_usage):
    fake = _CountingReactModel()
    _patch_react(monkeypatch, fake)
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    checkpoint = store.save(
        "t-react",
        run_id="src-1",
        round=1,
        kind="round",
        state=_state(budget_usage=budget_usage),
    )
    seed = build_resume_seed(checkpoint, [])
    task = TaskCreate(intent="检索并总结", workflow_type="generic")
    task._checkpoint_resume = seed
    handle = await run_task(
        task,
        RequestContext(tenant_id="t-react", user_id="u-1", role="admin"),
        bus=_RecordingBus(),
    )
    return fake, handle


async def test_resume_inherits_the_spent_model_budget(monkeypatch):
    # Control: no recorded usage — the loop gets its full turn budget
    # (default ceiling 6) and keeps calling the model (11 calls
    # measured: loop turns + stall-planning rounds across attempts).
    control, _ = await _drive_resume(monkeypatch, None)
    assert control.calls >= 5

    # The source run already spent 5 of the 6 loop turns: every
    # attempt of the resumed run inherits that counter, so each gets
    # only the one remaining turn before the ceiling (3 calls measured
    # — one per attempt — against the control's 11). A fresh counter
    # would reproduce the control's count exactly.
    resumed, _ = await _drive_resume(monkeypatch, {"model_calls": 5, "tool_calls": 3})
    assert resumed.calls <= 3
    assert resumed.calls < control.calls
