"""C1 multi-model fallback — get_model() degrades instead of raising.

Design verdict C1: a memory-constrained host (``qwen3:8b`` ≈ 6 GB, OOM-critical)
must always have a degrade path. These tests pin the contract:

  * the chain is walked in order — the configured provider first, then
    ``MODEL_FALLBACK_CHAIN``, always terminated by ``mock``;
  * a configured-but-unreachable Ollama daemon degrades to the next entry;
  * thinking models are never selected;
  * a hard *configuration* error on the chosen provider still fails fast.

No network is touched: the Ollama ``ChatOllama`` class and the reachability
probe are both faked.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from forgeflow.config import get_settings
from forgeflow.models import provider
from forgeflow.models.provider import MockChatModel, get_model


@pytest.fixture(autouse=True)
def _reset_caches():
    get_settings.cache_clear()
    provider._ollama_probe_cache.update({"at": 0.0, "ok": False})
    yield
    get_settings.cache_clear()
    provider._ollama_probe_cache.update({"at": 0.0, "ok": False})


def _fake_ollama(monkeypatch, *, reachable: bool) -> MagicMock:
    """Install a fake ``langchain_ollama`` module + a deterministic probe."""
    module = types.ModuleType("langchain_ollama")
    instance = MagicMock(name="ChatOllama-instance")
    module.ChatOllama = MagicMock(name="ChatOllama", return_value=instance)
    monkeypatch.setitem(sys.modules, "langchain_ollama", module)
    monkeypatch.setattr(provider, "_ollama_available", lambda settings: reachable)
    return instance


def test_chain_degrades_to_mock_when_ollama_unreachable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")
    _fake_ollama(monkeypatch, reachable=False)

    model = get_model()

    assert isinstance(model, MockChatModel)


def test_ollama_is_used_when_reachable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")
    instance = _fake_ollama(monkeypatch, reachable=True)

    model = get_model()

    assert model is instance


def test_thinking_models_are_never_built(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,qwen3:8b,deepseek-r1,mock")
    _fake_ollama(monkeypatch, reachable=False)

    built: list[str] = []
    real_build = provider._build_ollama_model

    def _record(settings, model_name):  # noqa: ANN001
        built.append(model_name)
        return real_build(settings, model_name)

    monkeypatch.setattr(provider, "_build_ollama_model", _record)

    model = get_model()

    assert isinstance(model, MockChatModel)
    assert "qwen3:8b" not in built
    assert "deepseek-r1" not in built


def test_candidates_are_ordered_and_mock_is_terminal(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,something:latest")
    settings = get_settings()

    assert provider._fallback_candidates(settings) == ["ollama", "something:latest", "mock"]


def test_primary_missing_key_still_fails_fast(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")

    with pytest.raises(ValueError, match="OPENAI_API_KEY is required"):
        get_model()


def test_primary_unknown_provider_still_fails_fast(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")

    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_model()


# --------------------------------------------------------------------------- #
# T3 — the thinking-model guard must act on the RESOLVED model tag, not only   #
# on the chain-entry name. The primary `ollama` provider builds its model from  #
# OLLAMA_MODEL / OLLAMA_MODEL_STRONG (never from the chain string), so a        #
# thinking tag configured there previously slipped straight through.            #
# --------------------------------------------------------------------------- #


def test_primary_resolved_thinking_tag_is_skipped(monkeypatch):
    """A thinking OLLAMA_MODEL on the primary provider is never built (default)."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")  # thinking — resolved tag
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")
    _fake_ollama(monkeypatch, reachable=True)

    built: list[str] = []
    real_build = provider._build_ollama_model

    def _record(settings, model_name):  # noqa: ANN001
        built.append(model_name)
        return real_build(settings, model_name)

    monkeypatch.setattr(provider, "_build_ollama_model", _record)

    model = get_model()

    assert isinstance(model, MockChatModel)
    assert "qwen3:8b" not in built


def test_primary_thinking_tag_is_allowed_when_ollama_think_enabled(monkeypatch):
    """OLLAMA_THINK=true is the operator's explicit opt-in to a thinking model."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("OLLAMA_THINK", "true")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")
    instance = _fake_ollama(monkeypatch, reachable=True)

    model = get_model()

    assert model is instance


# --------------------------------------------------------------------------- #
# T1 — mock is an environment-controlled DEVELOPMENT fallback, never a prod one #
# --------------------------------------------------------------------------- #


def test_development_chain_appends_mock_implicitly(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama")
    monkeypatch.setenv("OTEL_ENVIRONMENT", "development")

    settings = get_settings()

    assert provider._fallback_candidates(settings) == ["ollama", "mock"]


def test_production_chain_does_not_append_mock(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama")
    monkeypatch.setenv("OTEL_ENVIRONMENT", "prod")

    settings = get_settings()

    assert provider._fallback_candidates(settings) == ["ollama"]


def test_production_exhausted_chain_raises_instead_of_mock(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5vl:3b")  # non-thinking
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")
    monkeypatch.setenv("OTEL_ENVIRONMENT", "prod")
    _fake_ollama(monkeypatch, reachable=False)

    with pytest.raises(provider.ModelUnavailableError) as excinfo:
        get_model()

    # The error names the candidates that were actually attempted.
    assert "ollama" in str(excinfo.value)
    assert "mock" in str(excinfo.value)  # mentioned as the disabled prod fallback


def test_production_mock_primary_is_refused(monkeypatch):
    # Even a directly-configured mock primary must not silently serve prod.
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "mock")
    monkeypatch.setenv("OTEL_ENVIRONMENT", "prod")

    with pytest.raises(provider.ModelUnavailableError):
        get_model()
