"""FF-R1 B6 (FF-G12) — LangGraph agents must execute tools via ToolExecutor.

Before B6, ``ExecutorAgent._sync_to_crm`` and the ``ResearcherAgent``
loop invoked LangChain tools directly (``tool.ainvoke`` / the
``_tool_data`` helper), bypassing the platform's unified approval,
timeout, circuit-breaker and redaction path. These tests pin the new
bridge (``forgeflow.agents.tool_bridge.execute_agent_tool``):

1. a gated sales write tool is blocked without an approval — the
   handler never runs and exactly one approval action is staged;
2. a one-time approval executes exactly once and cannot be replayed;
3. the platform tool timeout applies to agent tool calls;
4. errors and results crossing the bridge carry no secrets;
5. an approval granted under one tenant is refused under another.

Everything runs on fakes — no paid models, no live PostgreSQL.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

import forgeflow.hitl.pending as P
from forgeflow.agents.executor import ExecutorAgent
from forgeflow.agents.tool_bridge import (
    AgentToolApprovalRequired,
    AgentToolError,
    execute_agent_tool,
)
from forgeflow.runtime import approval_policy, tool_registry

_TENANT = "tenant-b6-a"
_OTHER_TENANT = "tenant-b6-b"
_OWNER = "u-b6-owner"
_GATED = "hubspot_hubspot_upsert_contact"
_FAKE_SECRET = "sk-FAKEB6SECRET000111222"


@pytest.fixture(autouse=True)
def _fresh_pending_store():
    store = P.InMemoryPendingActionStore()
    P.set_pending_store(store)
    yield store
    P.reset_pending_store()


@pytest.fixture(autouse=True)
def _working_checkpoint_store():
    from forgeflow.runtime.checkpoints import (
        InMemoryCheckpointStore,
        reset_checkpoint_store,
        set_checkpoint_store,
    )

    set_checkpoint_store(InMemoryCheckpointStore())
    yield
    reset_checkpoint_store()


@pytest.fixture(autouse=True)
def _clean_registry():
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    yield
    tool_registry.reset_registry()
    tool_registry.load_default_bindings()
    approval_policy.unregister_pre_approval_tool(_GATED)


def _fake_tool(name: str, *, result=None, error: Exception | None = None, delay: float = 0.0):
    tool = MagicMock(name=name)
    tool.name = name

    async def _run(payload):
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        return result if result is not None else {"ok": True, "id": "obj-1"}

    tool.ainvoke = AsyncMock(side_effect=_run)
    return tool


def _state(tenant: str = _TENANT, user: str = _OWNER) -> dict:
    return {
        "workflow_id": "wf-b6-1",
        "tenant_id": tenant,
        "run_metadata": {"user_id": user, "role": "admin"},
        "lead_data": {"company_name": "Acme", "contact_email": "a@acme.example"},
        "proposal": {},
        "dry_run": False,
    }


# --------------------------------------------------------------------- #
# 1. No approval -> blocked (agent level, through ExecutorAgent)         #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sales_write_without_approval_is_blocked(monkeypatch, _fresh_pending_store):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "hubspot_access_token", SecretStr("fake-token"))
    approval_policy.register_pre_approval_tool(_GATED)
    tool = _fake_tool(_GATED)
    model = MagicMock(spec=BaseChatModel)
    model.bind_tools.return_value = model
    model.with_structured_output.return_value = model
    agent = ExecutorAgent(model=model, tools=[tool])

    actions = await agent._sync_to_crm(
        _state(), _state()["lead_data"], {}, "Acme", "wf-b6-1"
    )

    assert tool.ainvoke.await_count == 0  # the write never executed
    assert "hubspot_contact_failed" in actions
    staged = _fresh_pending_store.list(_TENANT, run_id="wf-b6-1")
    assert len(staged) == 1
    assert staged[0].kind == P.CONFIRM_PERMISSION


# --------------------------------------------------------------------- #
# 2. One-time approval executes once; replay is refused                  #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_one_time_approval_executes_and_cannot_replay(_fresh_pending_store):
    approval_policy.register_pre_approval_tool(_GATED)
    tool = _fake_tool(_GATED)
    state = _state()
    args = {"email": "a@acme.example"}

    with pytest.raises(AgentToolApprovalRequired) as blocked:
        await execute_agent_tool(tool, args, state=state, step="crm_contact")
    pending_id = blocked.value.pending_id
    assert pending_id
    assert tool.ainvoke.await_count == 0

    _fresh_pending_store.resolve(_TENANT, pending_id, decision="approve", actor=_OWNER)

    result = await execute_agent_tool(
        tool, args, state=state, step="crm_contact", approval_id=pending_id
    )
    assert result["id"] == "obj-1"
    assert tool.ainvoke.await_count == 1

    with pytest.raises(AgentToolError):
        await execute_agent_tool(
            tool, args, state=state, step="crm_contact", approval_id=pending_id
        )
    assert tool.ainvoke.await_count == 1  # still exactly one execution


# --------------------------------------------------------------------- #
# 3. The platform timeout applies                                        #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_tool_timeout_applies(monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "tool_timeout_seconds", 0.2)
    tool = _fake_tool("research_slow_search", delay=5.0)
    started = time.perf_counter()
    with pytest.raises(AgentToolError):
        await execute_agent_tool(tool, {"q": "acme"}, state=_state(), step="research")
    assert time.perf_counter() - started < 3.0


# --------------------------------------------------------------------- #
# 4. No secrets in errors or results                                     #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_tool_errors_and_results_carry_no_secrets():
    failing = _fake_tool(
        "research_leaky_search",
        error=RuntimeError(f"upstream rejected api_key={_FAKE_SECRET} badly"),
    )
    with pytest.raises(AgentToolError) as raised:
        await execute_agent_tool(failing, {"q": "x"}, state=_state(), step="research")
    assert _FAKE_SECRET not in str(raised.value)

    leaky = _fake_tool(
        "research_leaky_result",
        result={"ok": True, "note": f"used token {_FAKE_SECRET} here"},
    )
    result = await execute_agent_tool(leaky, {"q": "x"}, state=_state(), step="research")
    assert _FAKE_SECRET not in str(result)


# --------------------------------------------------------------------- #
# 5. Cross-tenant approval is refused                                    #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cross_tenant_approval_is_refused(_fresh_pending_store):
    approval_policy.register_pre_approval_tool(_GATED)
    tool = _fake_tool(_GATED)
    args = {"email": "a@acme.example"}

    with pytest.raises(AgentToolApprovalRequired) as blocked:
        await execute_agent_tool(tool, args, state=_state(_TENANT), step="crm_contact")
    pending_id = blocked.value.pending_id
    _fresh_pending_store.resolve(_TENANT, pending_id, decision="approve", actor=_OWNER)

    with pytest.raises(AgentToolError):
        await execute_agent_tool(
            tool, args, state=_state(_OTHER_TENANT), step="crm_contact", approval_id=pending_id
        )
    assert tool.ainvoke.await_count == 0
