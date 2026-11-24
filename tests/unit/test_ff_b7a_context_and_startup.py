"""FF-R1 B7a (FF-G13 / FF-G14) — per-model context capacity + ollama startup smoke.

FF-G13: a model's context window comes from the ``model_context_tokens``
configuration; unknown models keep the existing default
(``ollama_num_ctx``). Invalid configuration fails with a clear error.
The trimming path (``prepare_request``) must use the configured value.

FF-G14: with the optional ``langchain_ollama`` package absent, startup
(graph compilation — what the API lifespan does at boot) succeeds when
the configured provider is not ollama, and fails with a clear,
actionable error — never an ImportError — when it is.
"""

import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from forgeflow.config import Settings, get_settings
from forgeflow.models.provider import ProviderNotInstalledError, get_model
from forgeflow.runtime.context_window import (
    ContextWindowExceeded,
    context_window_tokens,
    prepare_request,
)


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ctx_task(model: str | None):
    ctx = SimpleNamespace(tenant_id="tenant-a", user_id="reader")
    runtime = {"model": model} if model else {}
    return ctx, SimpleNamespace(context={"llm_runtime": runtime})


# --------------------------------------------------------------------- #
# FF-G13 — per-model context capacity                                    #
# --------------------------------------------------------------------- #


def test_configured_model_context_tokens_drive_trimming(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ollama_num_ctx", 4096)
    monkeypatch.setattr(settings, "agent_context_output_reserve", 1024)
    monkeypatch.setattr(settings, "model_context_tokens", {"b7a-big-model": 1_000_000})
    assert context_window_tokens("b7a-big-model") == 1_000_000

    ctx, task = _ctx_task("b7a-big-model")
    big = HumanMessage(content="保持金额和公式" * 5000)
    # The default capacity (4096-1024) would refuse this outright; the
    # configured model's window admits it without trimming errors.
    selected, _receipt = prepare_request([SystemMessage(content="policy"), big], [], ctx, task)
    assert big in selected


def test_unknown_model_uses_existing_default(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ollama_num_ctx", 4096)
    monkeypatch.setattr(settings, "agent_context_output_reserve", 1024)
    monkeypatch.setattr(settings, "model_context_tokens", {"b7a-big-model": 1_000_000})
    assert context_window_tokens("no-such-model") == 4096
    assert Settings().ollama_num_ctx == 8192  # the shipped default is unchanged

    ctx, task = _ctx_task("no-such-model")
    with pytest.raises(ContextWindowExceeded):
        prepare_request(
            [SystemMessage(content="policy"), HumanMessage(content="保持金额和公式" * 5000)],
            [],
            ctx,
            task,
        )


@pytest.mark.parametrize(
    "bad_value",
    [0, -1024, "8192", 8192.5, True, None],
)
def test_invalid_model_context_tokens_fail_clearly(bad_value):
    with pytest.raises(Exception) as raised:
        Settings(model_context_tokens={"some-model": bad_value})
    assert "model_context_tokens" in str(raised.value)


# --------------------------------------------------------------------- #
# FF-G14 — startup smoke without the ollama extra                        #
# --------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_startup_without_ollama_extra_and_non_ollama_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    get_settings.cache_clear()
    from forgeflow.graph.builder import compile_graph

    with patch.dict(sys.modules, {"langchain_ollama": None}):
        graph = await compile_graph(mcp_tools=[], workflow_type="sales_ops")
    assert graph is not None


def test_ollama_selected_without_extra_fails_clearly(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    get_settings.cache_clear()
    with patch.dict(sys.modules, {"langchain_ollama": None}), pytest.raises(
        ProviderNotInstalledError
    ) as raised:
        get_model()
    message = str(raised.value)
    assert "forgeflow[ollama]" in message
    assert not isinstance(raised.value, ImportError)
    # No ImportError in the displayed chain either: the error must read
    # as an actionable configuration problem, not a stack of import
    # failures (``raise ... from None`` suppresses the context display).
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
