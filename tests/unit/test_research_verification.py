from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget
from forgeflow.runtime.professional_tools import store_evidence
from forgeflow.runtime.research_verification import ClaimReview, verify_research


@pytest.fixture
def source(tmp_path, monkeypatch):
    settings = SimpleNamespace(resource_store_path=lambda: tmp_path, llm_provider="ollama")
    monkeypatch.setattr("forgeflow.config.get_settings", lambda: settings)
    monkeypatch.setattr("forgeflow.runtime.professional_tools.get_settings", lambda: settings)
    monkeypatch.setattr("forgeflow.documents.store.get_settings", lambda: settings)
    ctx = SimpleNamespace(
        tenant_id="one",
        user_id="alice",
        intent="How are edits verified?",
        task_context=SimpleNamespace(context={}),
    )
    saved = store_evidence(
        {
            "source": "https://example.org/docs",
            "retrieved_at": "2026-10-14",
            "content": "Edits are verified by running the project tests.",
        },
        ctx,
    )
    claim = {
        "text": "Tests verify edits.",
        "quote": "running the project tests",
        "source_ref": saved["evidence_ref"],
        "source_hash": saved["content_hash"],
    }
    return ctx, claim, settings


class Reviewer:
    model = "independent-review"

    def __init__(self, supported):
        self.supported = supported
        self.calls = 0

    def with_structured_output(self, schema, **kwargs):
        assert schema is ClaimReview
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        return {
            "parsed": ClaimReview(supported=[self.supported], relevant=True, reason="checked"),
            "raw": AIMessage(
                content="",
                usage_metadata={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
            ),
        }


@pytest.mark.parametrize("supported", [True, False])
async def test_resolved_citations_require_independent_support(source, monkeypatch, supported):
    from forgeflow.documents.store import DocArtifactStore

    ctx, claim, _ = source
    reviewer = Reviewer(supported)
    monkeypatch.setattr("forgeflow.models.provider.get_model", lambda **kwargs: reviewer)
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    try:
        result = await verify_research({"claims": [claim]}, ctx)
    finally:
        current_budget.reset(token)
    assert result["ok"] is supported
    assert result["validation"]["claims_supported"] is supported
    assert budget.model_calls == 1
    assert ctx.task_context.context["llm_usage"][0]["input_tokens"] == 11
    if supported:
        assert "https://example.org/docs" in DocArtifactStore().get(result["artifact_ref"]).decode()
    else:
        assert "artifact_ref" not in result


@pytest.mark.parametrize("mutation", ["hash", "quote", "actor"])
async def test_invalid_evidence_rejected_before_model(source, monkeypatch, mutation):
    ctx, claim, _ = source
    reviewer = Reviewer(True)
    monkeypatch.setattr("forgeflow.models.provider.get_model", lambda **kwargs: reviewer)
    if mutation == "hash":
        claim["source_hash"] = "wrong"
    elif mutation == "quote":
        claim["quote"] = "this source never said this"
    else:
        ctx.user_id = "bob"
    with pytest.raises((ValueError, FileNotFoundError)):
        await verify_research({"claims": [claim]}, ctx)
    assert reviewer.calls == 0


async def test_mock_review_is_not_a_measured_success(source):
    ctx, claim, settings = source
    settings.llm_provider = "mock"
    result = await verify_research({"claims": [claim]}, ctx)
    assert result["not_executed"] is True
    assert result["validation"]["claims_supported"] is None
    assert "artifact_ref" not in result


async def test_failed_judge_does_not_fabricate_neutral_scores():
    from forgeflow.evaluation.judge import LLMJudge

    class Unavailable:
        def with_structured_output(self, schema):
            return self

        async def ainvoke(self, messages):
            raise RuntimeError("model unavailable")

    with pytest.raises(RuntimeError, match="model unavailable"):
        await LLMJudge(model=Unavailable()).evaluate("request", "context", "answer")
