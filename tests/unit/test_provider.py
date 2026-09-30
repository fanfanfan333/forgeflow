"""Tests for the model-provider factory.

INC16: the ``openai`` and ``anthropic`` providers were removed (Ollama-only
profile). Requesting either must **fail fast** — never silently degrade to the
mock stub. These tests pin that contract plus the surviving Ollama / unknown-name
behaviour and the T2 ``ollama_think`` wiring.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from forgeflow.config import Settings, get_settings
from forgeflow.models import provider
from forgeflow.models.provider import ProviderNotInstalledError, get_model


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# INC16 — removed providers fail fast (no silent degradation)                  #
# --------------------------------------------------------------------------- #


def test_openai_provider_is_removed_and_fails_fast(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")

    with pytest.raises(ValueError, match="removed in INC16") as excinfo:
        get_model()

    # The old "missing key" message must be gone — the provider itself is gone.
    assert "OPENAI_API_KEY is required" not in str(excinfo.value)


def test_anthropic_provider_is_removed_and_fails_fast(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")

    with pytest.raises(ValueError, match="removed in INC16") as excinfo:
        get_model()

    assert "ANTHROPIC_API_KEY is required" not in str(excinfo.value)


def test_removed_provider_never_silently_degrades_to_mock(monkeypatch):
    """Primary removed + a chain that *would* have worked ⇒ raise, not degrade.

    This is the highest-priority guard for INC16: a request for a removed
    provider must never be swallowed by the fallback machinery and answered by
    the next candidate or the mock stub.
    """
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama")
    monkeypatch.setenv("OTEL_ENVIRONMENT", "development")
    # Ollama would be perfectly reachable — a silent degrade would have succeeded.
    _install_fake_ollama(monkeypatch, reachable=True)

    with pytest.raises(ValueError, match="removed in INC16"):
        get_model()


def test_removed_provider_in_fallback_chain_fails_fast(monkeypatch):
    """A removed *chain* entry is rejected too, not skipped like a dead daemon."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "openai")
    # Primary Ollama is unreachable → the loop would advance to the 'openai' entry.
    _install_fake_ollama(monkeypatch, reachable=False)

    with pytest.raises(ValueError, match="removed in INC16"):
        get_model()


def test_ollama_provider_is_the_only_real_provider_in_chain(monkeypatch):
    assert provider._KNOWN_PROVIDERS == ("ollama", "mock")
    assert provider._REMOVED_PROVIDERS == ("openai", "anthropic")

    # A removed primary is rejected before the bare-tag path is ever consulted.
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama")
    with pytest.raises(ValueError, match="removed in INC16"):
        get_model()


def test_default_llm_provider_is_ollama(monkeypatch):
    # The declared field default must be 'ollama' (it was 'openai').
    assert Settings.model_fields["llm_provider"].default == "ollama"
    # And with no LLM_PROVIDER in the environment it actually resolves to ollama.
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert Settings(_env_file=None).llm_provider == "ollama"


# --------------------------------------------------------------------------- #
# Unknown / surviving provider names                                           #
# --------------------------------------------------------------------------- #


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bogus")

    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_model()


def test_ollama_missing_extra_raises_helpful_error(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    # Simulate langchain-ollama not installed
    with (
        patch.dict(sys.modules, {"langchain_ollama": None}),
        pytest.raises(ProviderNotInstalledError, match="forgeflow\\[ollama\\]"),
    ):
        get_model()


# --------------------------------------------------------------------------- #
# Judge default is provider-routed (INC16) — no hard-coded OpenAI client       #
# --------------------------------------------------------------------------- #


async def test_judge_default_model_is_provider_routed():
    """Under the offline (``mock``) profile the default judge builds a valid score.

    Proves the default judge no longer hard-codes a ``langchain_openai`` client:
    ``LLMJudge()`` routes through ``get_model(strong=True)`` and — with
    ``LLM_PROVIDER=mock`` (set by conftest) — yields a deterministic
    ``JudgeScore``.
    """
    from forgeflow.evaluation.judge import JudgeScore, LLMJudge

    judge = LLMJudge()
    score = await judge.evaluate(input="a question", context="some context", output="an answer")
    assert isinstance(score, JudgeScore)
    assert score.faithfulness == 0.0
    assert score.hallucination_flag is False


async def test_judge_routes_through_get_model_strong(monkeypatch):
    """INC16/P2-4: pin the *routing*, not merely the effect.

    The sibling test above asserts only the resulting ``JudgeScore``. That is
    not a sufficient pin: under the offline profile a hard-coded client would be
    replaced by the mock anyway, so the score assertions would stay green even if
    ``LLMJudge`` regressed back to building its own client. This test therefore
    records the call that ``LLMJudge.__init__`` actually makes, proving the
    default path is ``get_model(strong=True)`` — the **strong** slot, the one
    supervisor + judge are supposed to share.
    """
    import forgeflow.evaluation.judge as judge_mod
    from forgeflow.evaluation.judge import JudgeScore, LLMJudge
    from forgeflow.models.provider import MockChatModel

    calls: list[dict] = []

    def _recording_get_model(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return MockChatModel(strong=True)

    monkeypatch.setattr(judge_mod, "get_model", _recording_get_model)

    judge = LLMJudge()
    score = await judge.evaluate(input="q", context="c", output="o")

    assert calls == [{"strong": True}], (
        "the default judge must route through get_model(strong=True); "
        f"recorded calls: {calls!r}"
    )
    assert isinstance(score, JudgeScore)
    # The INC16 removal is complete for this module: no direct-OpenAI symbol may
    # survive in its namespace (a lingering import would be a silent second path).
    assert "ChatOpenAI" not in vars(judge_mod)
    assert "ChatAnthropic" not in vars(judge_mod)


# --------------------------------------------------------------------------- #
# T2 — the previously-dead ``ollama_think`` knob is now wired into the Ollama  #
# provider's ``reasoning`` flag. Default (False) keeps the old behaviour.      #
# --------------------------------------------------------------------------- #


def _install_fake_ollama(monkeypatch, *, reachable: bool = True) -> list[dict]:
    """Install a fake ``langchain_ollama`` capturing ChatOllama() kwargs."""
    module = types.ModuleType("langchain_ollama")
    captured: list[dict] = []

    def _ctor(**kwargs):  # noqa: ANN003
        captured.append(kwargs)
        return MagicMock(name="ChatOllama-instance")

    module.ChatOllama = _ctor
    monkeypatch.setitem(sys.modules, "langchain_ollama", module)
    monkeypatch.setattr(provider, "_ollama_available", lambda settings: reachable)
    return captured


def test_ollama_provider_defaults_to_reasoning_false(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_THINK", "false")
    captured = _install_fake_ollama(monkeypatch)

    get_model()

    assert captured, "ChatOllama was not constructed"
    assert captured[0]["reasoning"] is False


def test_ollama_provider_uses_reasoning_true_when_think_enabled(monkeypatch):
    # INC16: the C1 guard skips a thinking model only when OLLAMA_THINK=true, so
    # to isolate the *T2 wiring* (``reasoning`` follows ``OLLAMA_THINK``) we use a
    # NON-thinking tag — otherwise the guard skips it before ChatOllama is built.
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5vl:3b")
    monkeypatch.setenv("OLLAMA_MODEL_STRONG", "qwen2.5vl:3b")
    monkeypatch.setenv("OLLAMA_THINK", "true")
    captured = _install_fake_ollama(monkeypatch)

    get_model()

    assert captured, "ChatOllama was not constructed"
    assert captured[0]["reasoning"] is True
