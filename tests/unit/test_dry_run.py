"""Tests for the dry-run flag on the executor's side-effecting path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel

from forgeflow.agents.executor import ExecutorAgent


def _fake_model() -> BaseChatModel:
    llm = MagicMock(spec=BaseChatModel)
    llm.bind_tools = MagicMock(return_value=llm)
    llm.with_structured_output = MagicMock(return_value=llm)
    return llm


def _fake_tool(name: str):
    tool = MagicMock()
    tool.name = name
    tool.ainvoke = AsyncMock(return_value={"ok": True})
    return tool


class TestExecutorDryRun:
    @pytest.mark.asyncio
    async def test_missing_connectors_report_failure_instead_of_mock_success(self):
        executor = ExecutorAgent(model=_fake_model(), tools=[])
        result = await executor._execute_approved(
            {
                "lead_data": {"company_name": "Acme", "contact_email": "a@acme.example"},
                "proposal": {},
                "approval_status": "approved",
            }
        )
        assert result["current_stage"] == "failed"
        assert "crm_update_unavailable" in result["executed_actions"]
        assert "email_unavailable" in result["executed_actions"]
        assert result["errors"] and "Workflow complete" not in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_business_error_and_missing_address_report_partial_failure(self):
        crm = _fake_tool("crm_update_lead")
        crm.ainvoke = AsyncMock(return_value={"ok": False, "error": "rejected"})
        executor = ExecutorAgent(model=_fake_model(), tools=[crm])
        result = await executor._execute_approved(
            {"lead_data": {"company_name": "Acme"}, "proposal": {}}
        )
        assert result["current_stage"] == "failed"
        assert set(result["executed_actions"]) == {
            "crm_update_unavailable",
            "email_skipped_no_address",
        }
        crm.ainvoke.assert_not_called()

    def test_failed_executor_is_not_retried_by_supervisor(self):
        from forgeflow.graph.edges import route_after_executor

        assert route_after_executor({"current_stage": "failed"}) == "END"
        assert route_after_executor({"current_stage": "done"}) == "supervisor"

    @pytest.mark.asyncio
    async def test_dry_run_skips_crm_and_email_calls(self):
        # Tool names match what FastMCP exposes after .mount(prefix=...).
        crm = _fake_tool("crm_update_lead")
        email = _fake_tool("email_send_email")

        executor = ExecutorAgent(model=_fake_model(), tools=[crm, email])

        state = {
            "lead_data": {"company_name": "Acme", "contact_email": "a@acme.example"},
            "proposal": {"executive_summary": "Hello", "estimated_deal_value_usd": 50_000},
            "lead_id": "lead-1",
            "dry_run": True,
            "current_stage": "execute",
            "approval_status": "approved",
        }

        result = await executor._execute_approved(state)

        # Tools were NOT called
        crm.ainvoke.assert_not_called()
        email.ainvoke.assert_not_called()

        # Actions reflect dry-run
        assert "crm_updated_dry_run" in result["executed_actions"]
        assert "email_sent_dry_run" in result["executed_actions"]
        assert result["current_stage"] == "done"

    @pytest.mark.asyncio
    async def test_normal_run_rejects_demo_tools(self):
        crm = _fake_tool("crm_update_lead")
        email = _fake_tool("email_send_email")

        executor = ExecutorAgent(model=_fake_model(), tools=[crm, email])

        state = {
            "lead_data": {"company_name": "Acme", "contact_email": "a@acme.example"},
            "proposal": {"executive_summary": "Hello", "estimated_deal_value_usd": 50_000},
            "lead_id": "lead-1",
            "dry_run": False,
            "current_stage": "execute",
            "approval_status": "approved",
        }

        result = await executor._execute_approved(state)

        crm.ainvoke.assert_not_called()
        email.ainvoke.assert_not_called()
        assert "crm_update_unavailable" in result["executed_actions"]
        assert "email_unavailable" in result["executed_actions"]
        assert result["current_stage"] == "failed"

    @pytest.mark.asyncio
    async def test_dry_run_default_is_false(self):
        """A missing dry_run flag does not authorize demo tools as real actions."""
        crm = _fake_tool("crm_update_lead")
        email = _fake_tool("email_send_email")

        executor = ExecutorAgent(model=_fake_model(), tools=[crm, email])

        # contact_email is required for the email path now — the old "synthesize
        # contact@<slug>.com" fallback was a footgun in production.
        state = {
            "lead_data": {"company_name": "Acme", "contact_email": "ops@acme.example"},
            "proposal": {"executive_summary": "x", "estimated_deal_value_usd": 1},
            "lead_id": "lead-1",
            "current_stage": "execute",
            "approval_status": "approved",
        }
        await executor._execute_approved(state)

        crm.ainvoke.assert_not_called()
        email.ainvoke.assert_not_called()


class TestPipelineDryRunFlag:
    @pytest.mark.asyncio
    async def test_sales_ops_initial_state_carries_dry_run(self):
        from forgeflow.workflows.sales_ops.models import LeadInput
        from forgeflow.workflows.sales_ops.pipeline import SalesOpsPipeline

        graph = MagicMock()
        graph.ainvoke = AsyncMock(
            return_value={
                "current_stage": "done",
                "total_tokens": 0,
                "total_cost_usd": 0.0,
            }
        )
        pipeline = SalesOpsPipeline(graph=graph)
        await pipeline.run(LeadInput(company_name="X"), dry_run=True)

        initial_state = graph.ainvoke.call_args[0][0]
        assert initial_state["dry_run"] is True
        assert "dry_run" in initial_state["run_metadata"]["langsmith_tags"]
