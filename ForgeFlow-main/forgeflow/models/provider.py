"""Model-provider factory.

Returns a LangChain BaseChatModel based on settings.llm_provider:

  - openai     (default) - ChatOpenAI, requires OPENAI_API_KEY
  - ollama     - ChatOllama, runs against a local Ollama daemon
  - anthropic  - ChatAnthropic, requires ANTHROPIC_API_KEY
  - mock       - deterministic offline stub, no network / no API key

Each provider is imported lazily so the base install stays lean. Install
optional extras to enable a provider:

    pip install forgeflow[ollama]
    pip install forgeflow[anthropic]
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from forgeflow.config import Settings, get_settings

logger = logging.getLogger(__name__)


class ProviderNotInstalledError(RuntimeError):
    """Raised when the optional extras for a chosen provider are not installed."""


# Canonical answer the offline mock returns. It is a small, valid JSON object so
# structured-output callers that JSON-parse the content can still make progress.
_MOCK_REPLY: str = json.dumps(
    {
        "next": "done",
        "reasoning": "mock provider — deterministic offline response",
        "summary": "Mock execution completed",
        "steps": [],
    }
)


class MockChatModel(BaseChatModel):
    """Deterministic, offline BaseChatModel used when LLM_PROVIDER=mock.

    Keeps the closed loop（任务 → 经验 → 技能候选）runnable with no network,
    no API key, and no Ollama daemon. Every call returns the same canned
    message so results are reproducible in tests and demos.
    """

    strong: bool = False
    reply: str = _MOCK_REPLY

    @property
    def _llm_type(self) -> str:
        return "mock"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.reply))]
        )

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        """Best-effort structured output for the offline path.

        Callers that need a real schema should use the rule-based fallback
        (see skills/candidate_compiler.py) when LLM_PROVIDER=mock. Here we
        still return a Runnable so `.invoke()`/`.ainvoke()` never explode;
        it attempts to populate the schema with default/empty values.
        """
        from langchain_core.runnables import RunnableLambda

        def _populate(_: Any, _schema: Any = schema) -> Any:
            if hasattr(_schema, "model_validate"):
                try:
                    return _schema.model_validate({})
                except Exception:  # noqa: BLE001 — schema has required fields
                    pass
            return {}

        return RunnableLambda(_populate)


#: Provider identifiers ``get_model`` knows how to build directly. Anything else
#: found in ``MODEL_FALLBACK_CHAIN`` is treated as an explicit Ollama model tag.
_KNOWN_PROVIDERS: tuple[str, ...] = ("openai", "ollama", "anthropic", "mock")

#: Substrings that mark a *thinking* model. C1 is explicit: the fallback chain
#: must never select one — a thinking model (e.g. ``qwen3:8b``) can spend its
#: whole ``num_predict`` budget on the hidden trace and return "".
_THINKING_MODEL_MARKERS: tuple[str, ...] = (
    "qwen3",
    "qwq",
    "deepseek-r1",
    "deepseek-reasoner",
    "reasoning",
    "think",
)

# Ollama reachability probe — short timeout, result cached so a healthy daemon
# costs at most one probe per TTL window rather than one per get_model() call.
_OLLAMA_PROBE_TTL_SECONDS = 5.0
_OLLAMA_PROBE_TIMEOUT_SECONDS = 0.5
_ollama_probe_cache: dict[str, Any] = {"at": 0.0, "ok": False}


def _is_thinking_model(name: str) -> bool:
    """True when a chain entry names a thinking model (never a fallback target)."""
    key = (name or "").strip().lower()
    if not key or key in _KNOWN_PROVIDERS:
        return False
    return any(marker in key for marker in _THINKING_MODEL_MARKERS)


def _ollama_available(settings: Settings) -> bool:
    """Probe the Ollama daemon (``GET /api/tags``), memoised for a short TTL.

    Deliberately tiny (0.5 s timeout) and only ever aimed at the configured
    ``OLLAMA_BASE_URL`` — no other provider is probed, so C1's blast radius stays
    limited to the one provider that can realistically be down (a local daemon).
    Any error means "unavailable".
    """
    now = time.monotonic()
    if now - float(_ollama_probe_cache["at"]) < _OLLAMA_PROBE_TTL_SECONDS:
        return bool(_ollama_probe_cache["ok"])

    url = settings.ollama_base_url.rstrip("/") + "/api/tags"
    ok = False
    try:
        with urllib.request.urlopen(url, timeout=_OLLAMA_PROBE_TIMEOUT_SECONDS) as resp:
            ok = 200 <= int(getattr(resp, "status", 200)) < 500
    except Exception as exc:  # noqa: BLE001 — an unreachable daemon is the point
        logger.debug("Ollama probe failed (%s); treating as unavailable", exc)
        ok = False

    _ollama_probe_cache["at"] = now
    _ollama_probe_cache["ok"] = ok
    return ok


def _fallback_candidates(settings: Settings) -> list[str]:
    """Ordered, de-duplicated provider list: the configured provider first, then
    ``MODEL_FALLBACK_CHAIN``, always terminated by ``mock`` (which never raises)."""
    candidates: list[str] = []
    for raw in [settings.llm_provider, *(settings.model_fallback_chain or [])]:
        key = str(raw or "").strip().lower()
        if key and key not in candidates:
            candidates.append(key)
    if "mock" not in candidates:
        candidates.append("mock")
    return candidates


def _uses_ollama(name: str) -> bool:
    """True for the ``ollama`` provider and for bare Ollama model-tag entries."""
    return name == "ollama" or name not in _KNOWN_PROVIDERS


def _build_candidate(
    name: str, settings: Settings, strong: bool, *, is_primary: bool
) -> BaseChatModel:
    """Build one chain entry.

    A non-provider name is an explicit Ollama model tag — but only as a
    *fallback*; as the primary it is a misconfiguration and raises.
    """
    if name == "openai":
        return _build_openai(settings, strong)
    if name == "ollama":
        return _build_ollama(settings, strong)
    if name == "anthropic":
        return _build_anthropic(settings, strong)
    if name == "mock":
        return _build_mock(settings, strong)
    if is_primary:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{settings.llm_provider}'. "
            f"Expected one of: openai, ollama, anthropic, mock."
        )
    return _build_ollama_model(settings, name)


def get_model(strong: bool = False) -> BaseChatModel:
    """Return a chat model instance for the configured provider.

    C1 multi-model fallback: the configured provider is tried first, then each
    entry of ``MODEL_FALLBACK_CHAIN`` in order, always ending at the deterministic
    ``mock`` model. Thinking models are never selected. When Ollama is the chosen
    provider but its daemon is unreachable, the chain degrades to the next model
    instead of handing back a model that cannot answer.

    A hard *configuration* error on the explicitly chosen provider (unknown
    provider name, missing API key, missing optional extra) still fails fast —
    only the *fallback* path is allowed to degrade, so a typo in ``LLM_PROVIDER``
    never silently runs on the mock.

    Args:
        strong: If True, return the higher-capability variant (used by
            supervisor + judge). Otherwise return the cheaper worker model.
    """
    settings = get_settings()

    for index, name in enumerate(_fallback_candidates(settings)):
        is_primary = index == 0

        if _is_thinking_model(name):
            logger.warning("C1: skipping thinking model %r in fallback chain", name)
            continue

        try:
            model = _build_candidate(name, settings, strong, is_primary=is_primary)
        except Exception as exc:  # noqa: BLE001 — fail-fast vs degrade decided below
            if is_primary:
                raise
            logger.warning("C1: provider %r unavailable (%s); trying next", name, exc)
            continue

        if _uses_ollama(name) and not _ollama_available(settings):
            logger.warning("C1: Ollama unreachable; degrading past %r", name)
            continue

        if not is_primary:
            logger.warning("C1: serving %r after provider fallback", name)
        return model

    # ``mock`` is always appended by _fallback_candidates and never raises — this
    # is defensive only.
    return _build_mock(settings, strong)


def _build_mock(settings: Settings, strong: bool) -> BaseChatModel:
    """Offline deterministic model — no credentials, no network."""
    logger.debug("Building MockChatModel(strong=%s)", strong)
    return MockChatModel(strong=strong)



def _build_openai(settings: Settings, strong: bool) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    key = settings.openai_api_key.get_secret_value()
    if not key:
        raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")

    model_name = settings.openai_model_strong if strong else settings.openai_model
    logger.debug("Building ChatOpenAI(model=%s, strong=%s)", model_name, strong)
    return ChatOpenAI(
        model=model_name,
        api_key=key,
        temperature=0,
        max_retries=settings.max_retries,
    )


def _build_ollama(settings: Settings, strong: bool) -> BaseChatModel:
    model_name = settings.ollama_model_strong if strong else settings.ollama_model
    return _build_ollama_model(settings, model_name)


def _build_ollama_model(settings: Settings, model_name: str) -> BaseChatModel:
    """Build a ChatOllama for an explicit model tag (shared with the C1 chain)."""
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise ProviderNotInstalledError(
            "Ollama support requires the optional 'ollama' extra. "
            "Install with: pip install 'forgeflow[ollama]'"
        ) from exc

    logger.debug(
        "Building ChatOllama(model=%s, base_url=%s)", model_name, settings.ollama_base_url
    )
    # `reasoning=False` maps to Ollama's ``think=false``. "Thinking" models such
    # as qwen3 otherwise spend the whole ``num_predict`` budget on their hidden
    # reasoning trace and return an empty ``content`` (and run several× slower).
    # langchain-ollama >=0.3 exposes this field; on older builds that lack it we
    # fall back to a plain construction so the provider keeps working.
    try:
        return ChatOllama(
            model=model_name,
            base_url=settings.ollama_base_url,
            temperature=0,
            reasoning=False,
        )
    except (TypeError, ValueError):  # pragma: no cover — only on langchain-ollama < 0.3
        return ChatOllama(
            model=model_name,
            base_url=settings.ollama_base_url,
            temperature=0,
        )


def _build_anthropic(settings: Settings, strong: bool) -> BaseChatModel:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:
        raise ProviderNotInstalledError(
            "Anthropic support requires the optional 'anthropic' extra. "
            "Install with: pip install 'forgeflow[anthropic]'"
        ) from exc

    key = settings.anthropic_api_key.get_secret_value()
    if not key:
        raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic")

    model_name = settings.anthropic_model_strong if strong else settings.anthropic_model
    logger.debug("Building ChatAnthropic(model=%s, strong=%s)", model_name, strong)
    return ChatAnthropic(
        model=model_name,
        api_key=key,
        temperature=0,
        max_retries=settings.max_retries,
    )
