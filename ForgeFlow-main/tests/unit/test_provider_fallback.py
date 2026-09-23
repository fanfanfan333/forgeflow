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
