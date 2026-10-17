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
    ctx.task_context.context["tool_invocations"] = [{"tool": "research.read", "status": "ok", "tenant_id": ctx.tenant_id, "actor_user_id": ctx.user_id, "payload": {"source": "https://example.org/docs", "source_ref": claim["source_ref"], "source_hash": claim["source_hash"]}}]
    return ctx, claim, settings


class Reviewer:
    model = "independent-review"

    def __init__(self, supported):
        self.supported = supported
        self.calls = 0

    def with_structured_output(self, schema, **kwargs):
        assert issubclass(schema, ClaimReview)
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        return {
            "parsed": ClaimReview(
                supported=[self.supported], relevant=True, complete=True, reason="checked"
            ),
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


def test_source_url_binds_only_owned_actual_read_receipt():
    from types import SimpleNamespace

    from forgeflow.runtime.research_verification import bind_source_receipt

    receipt = {
        "tool": "research.read",
        "status": "ok",
        "tenant_id": "tenant",
        "actor_user_id": "actor",
        "payload": {
            "source": "https://example.org/paper",
            "source_ref": "aa/hash.json",
            "source_hash": "measured-hash",
        },
    }
    ctx = SimpleNamespace(
        tenant_id="tenant",
        user_id="actor",
        task_context=SimpleNamespace(context={"tool_invocations": [receipt]}),
    )
    bound = bind_source_receipt(
        {"source_url": "https://example.org/paper", "text": "claim", "quote": "a real quote"}, ctx
    )
    assert bound["source_ref"] == "aa/hash.json" and bound["source_hash"] == "measured-hash"
    with pytest.raises(ValueError, match="actually read"):
        bind_source_receipt({"source_url": "https://unread.example/paper"}, ctx)
    ctx.user_id = "other"
    with pytest.raises(ValueError, match="actually read"):
        bind_source_receipt({"source_url": "https://example.org/paper"}, ctx)


def test_url_alias_does_not_override_explicit_receipt_version():
    from types import SimpleNamespace

    from forgeflow.runtime.research_verification import bind_source_receipt

    ctx = SimpleNamespace(
        tenant_id="t",
        user_id="u",
        task_context=SimpleNamespace(
            context={
                "tool_invocations": [
                    {
                        "tool": "research.read",
                        "status": "ok",
                        "tenant_id": "t",
                        "actor_user_id": "u",
                        "payload": {
                            "source": "https://example.org",
                            "source_ref": "aa/hash.json",
                            "source_hash": "current",
                        },
                    }
                ]
            }
        ),
    )
    with pytest.raises(ValueError, match="version"):
        bind_source_receipt(
            {
                "source_url": "https://example.org",
                "source_ref": "aa/hash.json",
                "source_hash": "old",
            },
            ctx,
        )


async def test_real_executor_guard_preserves_typed_source_receipts(source, monkeypatch):
    from forgeflow.runtime.research_verification import bind_source_receipt
    from forgeflow.runtime.tool_executor import ToolCallContext, ToolExecutor
    from forgeflow.runtime.tool_registry import ToolBinding

    ctx, claim, _ = source

    async def read(args, context):
        return {
            "ok": True,
            "source": "https://example.org/docs",
            "requested_url": "https://example.org/docs",
            "source_ref": claim["source_ref"],
            "source_hash": claim["source_hash"],
            "content": "external source content",
            "retrieved_at": "2026-10-15",
        }

    binding = ToolBinding(
        tool_id="research.read", handler=read, kind="real", provider="safe-http", description="test"
    )
    monkeypatch.setattr("forgeflow.runtime.tool_executor.resolve", lambda tool: binding)
    call = ToolCallContext(
        run_id="run",
        step_id="step",
        tenant_id=ctx.tenant_id,
        user_id=ctx.user_id,
        role="manager",
        intent=ctx.intent,
        args={},
        task_context=ctx.task_context,
    )
    invocation = await ToolExecutor().execute("research.read", ctx=call, policy_decision="allow")
    assert isinstance(invocation.payload, dict)
    assert "<UNTRUSTED_TOOL_OUTPUT" in invocation.payload["untrusted_content"]
    ctx.task_context.context["tool_invocations"] = [invocation.to_dict()]
    bound = bind_source_receipt(
        {
            "text": "Test claim",
            "quote": "external source content",
            "source_url": "https://example.org/docs",
        },
        ctx,
    )
    assert bound["source_ref"] == claim["source_ref"]


async def test_source_pages_share_one_retained_version_and_query_is_in_schema(source, monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    from forgeflow.runtime.professional_tools import research_read

    ctx, _, _ = source
    body = (
        "<html><title>Source</title><main>"
        + "intro " * 400
        + "needle verified evidence "
        + "tail " * 400
        + "</main></html>"
    )
    getter = AsyncMock(
        return_value=httpx.Response(
            200, text=body, request=httpx.Request("GET", "https://example.org/docs")
        )
    )
    monkeypatch.setattr("forgeflow.security.ssrf_guard.safe_get", getter)
    ctx.task_context.context["tool_invocations"] = []
    first = await research_read({"url": "https://example.org/docs", "query": "needle"}, ctx)
    assert "needle verified evidence" in first["content"]
    ctx.task_context.context["tool_invocations"] = [
        {
            "tool": "research.read",
            "status": "ok",
            "tenant_id": ctx.tenant_id,
            "actor_user_id": ctx.user_id,
            "payload": first,
        }
    ]
    second = await research_read(
        {"url": "https://example.org/docs", "offset": first["next_offset"]}, ctx
    )
    assert (
        second["source_ref"] == first["source_ref"]
        and second["source_hash"] == first["source_hash"]
    )
    assert second["retrieved_at"] == first["retrieved_at"]
    assert getter.await_count == 1


@pytest.mark.parametrize("supported", [True, False])
async def test_passage_selection_uses_actual_source_and_independent_review(
    source, monkeypatch, supported
):
    ctx, old, _ = source
    reviewer = Reviewer(supported)
    monkeypatch.setattr("forgeflow.models.provider.get_model", lambda **kwargs: reviewer)
    ctx.task_context.context["tool_invocations"] = [
        {
            "tool": "research.read",
            "status": "ok",
            "tenant_id": ctx.tenant_id,
            "actor_user_id": ctx.user_id,
            "payload": {
                "source": "https://example.org/docs",
                "source_ref": old["source_ref"],
                "source_hash": old["source_hash"],
                "passage_id": 0,
                "excerpt_start": 0,
                "excerpt_end": 55,
            },
        }
    ]
    token = current_budget.set(ExecutionBudget())
    try:
        result = await verify_research(
            {
                "claims": [
                    {
                        "text": "Tests verify edits.",
                        "source_url": "https://example.org/docs",
                        "passage_id": 0,
                    }
                ]
            },
            ctx,
        )
        assert result["ok"] is supported
        assert result["validation"]["citations_ok"] is True
        assert reviewer.calls == 1
        with pytest.raises(ValueError, match="actually read"):
            await verify_research(
                {
                    "claims": [
                        {
                            "text": "Invented",
                            "source_url": "https://example.org/docs",
                            "passage_id": 99,
                        }
                    ]
                },
                ctx,
            )
        assert reviewer.calls == 1
    finally:
        current_budget.reset(token)


def test_reviewer_schema_cannot_silently_omit_all_claim_decisions():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ClaimReview(supported=[], relevant=True, complete=True, reason="No decisions")


async def test_model_approval_cannot_omit_an_explicit_second_research_subject(source, monkeypatch):
    ctx, claim, _ = source
    ctx.intent = "研究资料，解释 loads 和 dumps 的用途。"
    claim["text"] = "loads decodes JSON."
    reviewer = Reviewer(True)
    monkeypatch.setattr("forgeflow.models.provider.get_model", lambda **kwargs: reviewer)
    token = current_budget.set(ExecutionBudget())
    try:
        result = await verify_research({"claims": [claim]}, ctx)
        assert result["ok"] is False and result["validation"]["coverage_ok"] is False
        assert result["validation"]["missing_symbols"] == ["dumps"]
    finally:
        current_budget.reset(token)


async def test_source_reader_prefers_main_content_over_navigation(source, monkeypatch):
    import httpx
    from unittest.mock import AsyncMock
    from forgeflow.runtime.professional_tools import research_read

    ctx, _, _ = source
    response = httpx.Response(
        200,
        text="<html><title>Page</title><div>navigation noise</div><main>Actual supported documentation.</main></html>",
        request=httpx.Request("GET", "https://example.org/docs"),
    )
    monkeypatch.setattr("forgeflow.security.ssrf_guard.safe_get", AsyncMock(return_value=response))
    ctx.task_context.context["tool_invocations"] = []
    result = await research_read({"url": "https://example.org/docs"}, ctx)
    assert result["content"] == "Actual supported documentation."


async def test_source_hash_alone_cannot_bypass_the_actual_read_requirement(source):
    ctx,claim,_=source
    ctx.task_context.context["tool_invocations"]=[]
    with pytest.raises(ValueError,match="actually read"):
        await verify_research({"claims":[claim]},ctx)


async def test_source_context_is_guarded_again_before_independent_review(source,monkeypatch):
    from forgeflow.security.prompt_guard import RiskLevel
    ctx,claim,_=source
    reviewer=Reviewer(True)
    monkeypatch.setattr("forgeflow.models.provider.get_model",lambda **kw:reviewer)
    monkeypatch.setattr("forgeflow.security.prompt_guard.scan_prompt",lambda text:SimpleNamespace(level=RiskLevel.HIGH))
    with pytest.raises(ValueError,match="prompt-injection"):
        await verify_research({"claims":[claim]},ctx)
    assert reviewer.calls==0
