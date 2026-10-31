"""Supervisor routing: LLM consulted, deterministic map corrects.

Three LLM rounds scored Analyzer 0/15, 1/15, 1/15 on qwen3:8b.
The documented mapping is determinative, so SupervisorAgent.run
must correct a misread model decision to the mapped route —
while still building the intent-aware prompt (see
test_supervisor_intent_aware.py).
"""

import asyncio

from forgeflow.agents.supervisor import (
    RoutingDecision,
    SupervisorAgent,
    deterministic_route_for_stage,
)


class _WrongModel:
    """A model that would route every research-stage case to researcher."""

    def with_structured_output(self, schema):
        class _S:
            def __init__(self):
                self.prompts = []

            async def ainvoke(self, prompt):
                self.prompts.append(prompt)
                return RoutingDecision(next="researcher", reasoning="gather first")

        return _S()


def test_deterministic_map_covers_all_documented_stages():
    assert deterministic_route_for_stage("qualify") == "researcher"
    assert deterministic_route_for_stage("research") == "analyzer"
    assert deterministic_route_for_stage("analyze", 8.5) == "executor"
    assert deterministic_route_for_stage("analyze", 2.5) == "FINISH"
    assert deterministic_route_for_stage("propose") == "human_approval"
    assert deterministic_route_for_stage("approve") == "executor"
    assert deterministic_route_for_stage("done") == "FINISH"
    assert deterministic_route_for_stage("unknown") is None


def test_run_corrects_wrong_model_decision_for_documented_stage():
    agent = SupervisorAgent(model=_WrongModel())
    result = asyncio.run(
        agent.run(
            {
                "current_stage": "research",
                "messages": [],
                "analysis_scores": [],
                "errors": [],
                "lead_data": {},
                "approval_status": None,
            }
        )
    )
    assert result["next_agent"] == "analyzer"
    assert result["current_stage"] == "analyze"
    # The LLM path is still consulted (intent prompt contract intact),
    # the deterministic map corrects its decision afterwards.
    assert agent._structured_model.prompts
