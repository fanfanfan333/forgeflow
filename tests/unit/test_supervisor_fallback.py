"""Supervisor repair-retry + deterministic fallback, driven through
``SupervisorAgent.run`` with a stubbed model.

The repair-retry (one retry with the validation error fed back) and the
unparsable-output fallback were unit-verified only at the helper level
and had NEVER fired in a real run (routing rounds: model_error=0,
fallbacks 0). These tests drive the real ``run`` path with a model whose
structured output fails, so the fallback is proven end-to-end, not just
as an isolated branch: the fallback fires only after the repair retry
also fails, returns the deterministic route for a documented stage, and
re-raises for an unknown stage where no safe fallback exists.
"""

import pytest
from langchain_core.messages import HumanMessage

from forgeflow.agents.supervisor import RoutingDecision, SupervisorAgent


class _FailingStructured:
    """Structured-output stub whose ainvoke always raises (unparsable)."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list = []

    async def ainvoke(self, messages):
        self.calls += 1
        self.seen_messages.append(messages)
        raise ValueError("unparsable routing output: not a valid decision")


class _RepairingStructured:
    """First call fails; the repair retry succeeds."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list = []

    async def ainvoke(self, messages):
        self.calls += 1
        self.seen_messages.append(messages)
        if self.calls == 1:
            raise ValueError("unparsable routing output: not a valid decision")
        return RoutingDecision(next="researcher", reasoning="repair ok")


class _StubModel:
    """Duck-typed stand-in: SupervisorAgent only needs with_structured_output."""

    def __init__(self, structured) -> None:
        self._structured = structured

    def with_structured_output(self, _schema):
        return self._structured


def _state(stage: str) -> dict:
    return {
        "current_stage": stage,
        "messages": [HumanMessage(content="gather company intelligence")],
        "errors": [],
        "analysis_scores": [],
    }


@pytest.mark.asyncio
async def test_fallback_after_failed_repair_returns_deterministic_route():
    structured = _FailingStructured()
    agent = SupervisorAgent(model=_StubModel(structured))

    result = await agent.run(_state("qualify"))

    # qualify -> researcher is the documented deterministic route.
    assert result["next_agent"] == "researcher"
    assert result["current_stage"] == "research"
    assert "deterministic fallback" in result["messages"][0].content
    # First attempt + exactly one repair retry, then the fallback.
    assert structured.calls == 2


@pytest.mark.asyncio
async def test_repair_retry_success_feeds_back_validation_error():
    structured = _RepairingStructured()
    agent = SupervisorAgent(model=_StubModel(structured))

    result = await agent.run(_state("qualify"))

    assert result["next_agent"] == "researcher"
    assert structured.calls == 2
    # The repair call carries the validation error back to the model.
    repair_prompt = structured.seen_messages[1]
    repair_text = " ".join(str(getattr(m, "content", "")) for m in repair_prompt)
    assert "无法解析" in repair_text
    assert "researcher" in repair_text


@pytest.mark.asyncio
async def test_fallback_reraises_for_unknown_stage():
    structured = _FailingStructured()
    agent = SupervisorAgent(model=_StubModel(structured))

    with pytest.raises(ValueError, match="unparsable routing output"):
        await agent.run(_state("mystery_stage"))

    assert structured.calls == 2
