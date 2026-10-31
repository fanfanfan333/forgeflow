"""Supervisor routing is a stage rule, not a model judgement.

Three LLM rounds scored Analyzer 0/15, 1/15, 1/15 on qwen3:8b.
The documented mapping is determinative, so SupervisorAgent.run
must return it without spending a model call — even if the model
would have misread the stage.
"""

import asyncio

from forgeflow.agents.supervisor import SupervisorAgent, deterministic_route_for_stage


class _WrongModel:
    """A model that would route every research-stage case to researcher."""

    def with_structured_output(self, schema):
        class _S:
            async def ainvoke(self, prompt):
                raise AssertionError("deterministic stage must not call the model")

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


def test_run_does_not_call_model_for_documented_stage():
    agent = SupervisorAgent(model=_WrongModel())
    result = asyncio.run(agent.run({"current_stage": "research", "messages": []}))
    assert result["next_agent"] == "analyzer"
    assert result["current_stage"] == "analyze"
