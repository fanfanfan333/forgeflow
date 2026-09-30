"""C1 multi-model fallback — get_model() degrades instead of raising.

Design verdict C1: a memory-constrained host (``qwen3:8b`` ≈ 6 GB, OOM-critical)
must always have a degrade path. These tests pin the contract:

  * the chain is walked in order — the configured provider first, then
    ``MODEL_FALLBACK_CHAIN``, always terminated by ``mock``;
  * a configured-but-unreachable Ollama daemon degrades to the next entry;
  * a thinking model is **built** when ``OLLAMA_THINK=false`` (the measured
    working combo) and **skipped** only when ``OLLAMA_THINK=true`` (which returns
    empty content) — the INC16 semantic reversal. Evidence:
    ``qa_tmp/inc16/ollama_think_probe.json`` and
    ``qa_tmp/inc16/chatollama_reasoning_probe.json``;
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


def _record_builds(monkeypatch) -> list[str]:
    """Wrap ``_build_ollama_model`` so tests can see which tags were *built*."""
    built: list[str] = []
    real_build = provider._build_ollama_model

    def _record(settings, model_name):  # noqa: ANN001
        built.append(model_name)
        return real_build(settings, model_name)

    monkeypatch.setattr(provider, "_build_ollama_model", _record)
    return built


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


def test_thinking_chain_entries_are_not_skipped_when_ollama_think_disabled(monkeypatch):
    """INC16 reversal: a thinking *chain entry* is attempted when THINK is false.

    The old guard skipped a thinking entry purely for being a thinking model.
    Measurement shows ``think=false`` is the *working* combination (see the C1
    section below), so the entry must now be built rather than skipped. The faked
    daemon is unreachable, so after being built each entry degrades past — which
    is exactly how we observe that a build really happened.
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_THINK", "false")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,qwen3:8b,deepseek-r1,mock")
    _fake_ollama(monkeypatch, reachable=False)

    built = _record_builds(monkeypatch)

    model = get_model()

    assert isinstance(model, MockChatModel)
    # Built, not skipped: both thinking entries were attempted before degrading.
    assert "qwen3:8b" in built
    assert "deepseek-r1" in built


def test_candidates_are_ordered_and_mock_is_terminal(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,something:latest")
    settings = get_settings()

    assert provider._fallback_candidates(settings) == ["ollama", "something:latest", "mock"]


def test_primary_removed_provider_still_fails_fast(monkeypatch):
    # INC16: a removed provider (openai) is a hard config error — fail fast,
    # never degrade to the fallback chain.
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")

    with pytest.raises(ValueError, match="removed in INC16"):
        get_model()


def test_primary_unknown_provider_still_fails_fast(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "bogus")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")

    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        get_model()


@pytest.mark.parametrize("removed", ["openai", "anthropic"])
def test_removed_provider_as_chain_entry_fails_fast_not_skipped(monkeypatch, removed):
    """INC16/P2-5: a removed provider *inside* ``MODEL_FALLBACK_CHAIN`` raises.

    Two distinct properties are pinned here, and both matter:

      1. **Not skipped like a dead daemon.** A legitimately unreachable Ollama
         entry is ``continue``-ed past. A removed provider must NOT be treated
         that way — otherwise the request would be answered by the next
         candidate (``mock``), i.e. exactly the silent degradation INC16 exists
         to prevent. The fast-fail check must sit *outside* the try/except that
         decides degrade-vs-raise.
      2. **Reached at all.** Primary Ollama is faked as unreachable so the loop
         genuinely advances to the offending entry rather than dying earlier.

    Parametrised over both removed providers so a regression that special-cases
    one of them is still caught.
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", f"{removed},mock")
    _fake_ollama(monkeypatch, reachable=False)

    with pytest.raises(ValueError, match="removed in INC16"):
        get_model()


# --------------------------------------------------------------------------- #
# INC16 — C1 guard SEMANTIC REVERSAL.                                          #
#                                                                              #
# The guard used to act the wrong way round: it skipped a thinking model when   #
# thinking was OFF (the usable config) and allowed it when thinking was ON (the  #
# empty-output config). Daemon measurement on this box pins the correct         #
# direction — qwen3:8b with think=false returns a valid 62-char JSON            #
# (done_reason=stop), while think=true returns EMPTY content (306-char trace,    #
# done_reason=length). ``settings.ollama_think`` is what is sent as ``think``.   #
# The guard must therefore act on the RESOLVED tag (the primary ``ollama``       #
# provider builds from OLLAMA_MODEL / OLLAMA_MODEL_STRONG, not from the chain    #
# string), and skip ONLY when ``ollama_think is True``.                          #
# --------------------------------------------------------------------------- #


def test_primary_resolved_thinking_tag_is_built_when_ollama_think_disabled(monkeypatch):
    """A thinking OLLAMA_MODEL is BUILT when OLLAMA_THINK=false (INC16 reversal).

    Evidence (``qa_tmp/inc16/ollama_think_probe.json``): qwen3:8b with
    ``think=false`` → 62-char JSON content, ``done_reason=stop``. The old guard
    wrongly skipped exactly this usable configuration.
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")  # thinking — resolved tag
    monkeypatch.setenv("OLLAMA_THINK", "false")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama")
    instance = _fake_ollama(monkeypatch, reachable=True)

    built = _record_builds(monkeypatch)

    model = get_model()

    assert model is instance
    assert "qwen3:8b" in built


def test_thinking_model_is_skipped_when_ollama_think_enabled(monkeypatch):
    """A thinking OLLAMA_MODEL is SKIPPED when OLLAMA_THINK=true (INC16 reversal).

    Evidence (``qa_tmp/inc16/ollama_think_probe.json``): qwen3:8b with
    ``think=true`` → EMPTY content (306-char trace, ``done_reason=length``) — the
    broken combo the guard now skips. The chain then serves ``mock``.
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("OLLAMA_THINK", "true")
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")
    _fake_ollama(monkeypatch, reachable=True)

    built = _record_builds(monkeypatch)

    model = get_model()

    assert isinstance(model, MockChatModel)
    assert "qwen3:8b" not in built


def test_c1_thinking_guard_direction_is_pinned_both_ways(monkeypatch):
    """Anti-drift: the C1 guard's DIRECTION must not silently flip again.

    The guard was previously inverted (skip a thinking model when thinking is
    OFF; allow it when ON) — the exact reverse of correct. This one test pins
    BOTH directions against daemon measurements
    (``qa_tmp/inc16/ollama_think_probe.json``,
    ``qa_tmp/inc16/chatollama_reasoning_probe.json``):

      qwen3:8b + think=false → 62-char JSON, done_reason=stop   ⇒ BUILT
      qwen3:8b + think=true  → 0-char content, 306-char trace,
                               done_reason=length              ⇒ SKIPPED

    A regression that re-inverts the guard fails one half of this test.
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")  # thinking — resolved tag
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama,mock")

    built = _record_builds(monkeypatch)

    # --- think=false ⇒ the thinking model IS built (the working combo) --------
    monkeypatch.setenv("OLLAMA_THINK", "false")
    instance_false = _fake_ollama(monkeypatch, reachable=True)
    get_settings.cache_clear()
    provider._ollama_probe_cache.update({"at": 0.0, "ok": False})

    model_false = get_model()
    assert model_false is instance_false
    assert "qwen3:8b" in built

    # --- think=true ⇒ the thinking model is SKIPPED (the broken combo) --------
    built.clear()
    monkeypatch.setenv("OLLAMA_THINK", "true")
    _fake_ollama(monkeypatch, reachable=True)
    get_settings.cache_clear()
    provider._ollama_probe_cache.update({"at": 0.0, "ok": False})

    model_true = get_model()
    assert isinstance(model_true, MockChatModel)
    assert "qwen3:8b" not in built


def test_c1_guard_applies_to_the_resolved_strong_slot(monkeypatch):
    """INC16/P2-3: ``get_model(strong=True)`` obeys the same C1 direction.

    The guard resolves its tag via ``_resolved_ollama_tag(name, settings, strong)``,
    so the ``strong=True`` half of that branch had no coverage at all — a
    regression that bypassed the guard for ``strong`` (the supervisor + judge
    slot, i.e. the slot that most needs a working model) would have stayed green.

    Direction per ``qa_tmp/inc16/ollama_think_probe.json``: qwen3:8b with
    ``think=false`` → 62-char JSON / ``done_reason=stop`` (BUILT); with
    ``think=true`` → empty content / ``done_reason=length`` (SKIPPED).
    """
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5vl:3b")  # worker slot: non-thinking
    monkeypatch.setenv("OLLAMA_MODEL_STRONG", "qwen3:8b")  # strong slot: thinking
    monkeypatch.setenv("MODEL_FALLBACK_CHAIN", "ollama")

    built = _record_builds(monkeypatch)

    # --- think=false ⇒ the STRONG slot's thinking tag IS built ---------------
    monkeypatch.setenv("OLLAMA_THINK", "false")
    instance = _fake_ollama(monkeypatch, reachable=True)
    get_settings.cache_clear()
    provider._ollama_probe_cache.update({"at": 0.0, "ok": False})

    model = get_model(strong=True)

    assert model is instance
    # It must have resolved the STRONG tag, not the worker tag.
    assert built == ["qwen3:8b"], f"strong slot not resolved; built={built!r}"
    assert "qwen2.5vl:3b" not in built

    # --- think=true ⇒ the same STRONG tag is SKIPPED -------------------------
    built.clear()
    monkeypatch.setenv("OLLAMA_THINK", "true")
    _fake_ollama(monkeypatch, reachable=True)
    get_settings.cache_clear()
    provider._ollama_probe_cache.update({"at": 0.0, "ok": False})

    assert isinstance(get_model(strong=True), MockChatModel)
    assert built == [], f"guard did not skip the strong slot; built={built!r}"


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
