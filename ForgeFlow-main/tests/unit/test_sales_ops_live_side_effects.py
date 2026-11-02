from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from pydantic import SecretStr

from forgeflow.agents.executor import ExecutorAgent, _tool_data


@pytest.mark.asyncio
async def test_live_workflow_does_not_claim_mock_crm_and_email_succeeded(monkeypatch):
    from forgeflow.config import get_settings

    monkeypatch.setattr(get_settings(), "hubspot_access_token", SecretStr(""))
    model = MagicMock(spec=BaseChatModel)
    model.bind_tools.return_value = model
    model.with_structured_output.return_value = model
    crm = MagicMock(name="crm")
    crm.name = "crm_update_lead"
    crm.ainvoke = AsyncMock(return_value={"id": "demo"})
    email = MagicMock(name="email")
    email.name = "email_send_email"
    email.ainvoke = AsyncMock(return_value={"success": True})
    result = await ExecutorAgent(model=model, tools=[crm, email])._execute_approved(
        {
            "lead_data": {"company_name": "Acme", "contact_email": "a@acme.example"},
            "lead_id": "demo",
            "proposal": {},
            "dry_run": False,
        }
    )
    assert result["current_stage"] == "failed"
    assert "crm_update_unavailable" in result["executed_actions"]
    assert "email_unavailable" in result["executed_actions"]
    crm.ainvoke.assert_not_called()
    email.ainvoke.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"mock": True, "id": "fake"}, {"success": False}])
async def test_failed_or_simulated_vendor_reply_is_not_a_real_action(payload):
    tool = MagicMock()
    tool.name = "vendor_write"
    tool.ainvoke = AsyncMock(return_value=payload)
    with pytest.raises(RuntimeError):
        await _tool_data(tool, {})
