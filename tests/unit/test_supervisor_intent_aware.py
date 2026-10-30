"""Intent-aware supervisor routing: task intent reaches the prompt."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from forgeflow.agents.supervisor import RoutingDecision, SupervisorAgent


class _Structured:
    def __init__(self):
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        return RoutingDecision(next="researcher", reasoning="gather first")


def _agent():
    agent = SupervisorAgent.__new__(SupervisorAgent)
    agent.system_prompt = "system"
    agent._structured_model = _Structured()
    agent._log_start = lambda state: None
    return agent


@pytest.mark.asyncio
async def test_task_intent_included_and_supervisor_message_excluded():
    agent = _agent()
    state = {
        "current_stage": "qualify",
        "errors": [],
        "analysis_scores": [],
        "approval_status": None,
        "lead_data": {"company": "Acme Corp"},
        "messages": [
            HumanMessage(content="请调研 Acme 的融资和规模，判断是否值得跟进"),
            AIMessage(content="[Supervisor] Routing to researcher: gather first"),
        ],
    }
    result = await agent.run(state)
    assert result["next_agent"] == "researcher"
    prompt_text = "\n".join(str(m.content) for m in agent._structured_model.prompts[0])
    assert "Acme Corp" in prompt_text
    assert "请调研 Acme 的融资和规模" in prompt_text
    assert "Routing to researcher" not in prompt_text


@pytest.mark.asyncio
async def test_intent_truncated_to_500_chars():
    agent = _agent()
    state = {
        "current_stage": "qualify",
        "errors": [],
        "analysis_scores": [],
        "approval_status": None,
        "lead_data": None,
        "messages": [HumanMessage(content="x" * 1000)],
    }
    await agent.run(state)
    prompt_text = "\n".join(str(m.content) for m in agent._structured_model.prompts[0])
    intent_line = [line for line in prompt_text.splitlines() if line.startswith("Task intent:")][0]
    assert len(intent_line) - len("Task intent: ") == 500
