"""INC17 T05 — the offline (mock) profile is unchanged (negative nail).

The whole point of keeping ``react`` behind the provider signal is that the
offline profile — ``STORAGE_BACKEND=memory`` + ``LLM_PROVIDER=mock`` +
``EMBEDDING_PROVIDER=mock`` — is byte-for-byte what it was. ``conftest`` pins
``LLM_PROVIDER=mock``, so this file nails:

  * ``resolve_agent_runtime_mode() == "deterministic"`` offline;
  * a plain ``run_task`` on the offline profile records ``runtime_mode ==
    "deterministic"`` and carries **no** ``llm`` provenance (the react loop never
    activated — there is no ``task.context["llm_runtime"]``);
  * the deterministic path is still the dynamic platform plan (``source ==
    "deterministic"``).
"""

from __future__ import annotations

import pytest

from forgeflow.config import get_settings
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    reset_run_store,
    resolve_agent_runtime_mode,
    run_task,
)

pytestmark = pytest.mark.asyncio


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def _ctx() -> RequestContext:
    return RequestContext(tenant_id="t-mock", user_id="u-1", role="admin")


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()


def test_offline_profile_resolves_to_deterministic(force_memory_backend):
    settings = get_settings()
    assert settings.llm_provider == "mock", "conftest 必须固定 LLM_PROVIDER=mock"
    assert resolve_agent_runtime_mode() == "deterministic"


async def test_offline_run_task_does_not_enter_react(force_memory_backend):
    task = TaskCreate(intent="分析华东地区销售数据并生成报告", workflow_type="generic")
    handle = await run_task(task, _ctx(), bus=_RecordingBus())

    assert handle.status == "completed"
    assert handle.detail["runtime_mode"] == "deterministic"
    # No LLM provenance ⇒ the react loop never ran.
    assert handle.detail["llm"] is None
    # The negative nail: react writes ``llm_runtime``; the offline path must not.
    assert not task.context.get("llm_runtime")
    # Still the dynamic deterministic plan.
    assert task.context["plan"]["source"] == "deterministic"
