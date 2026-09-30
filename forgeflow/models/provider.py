"""Model-provider factory.

Returns a LangChain BaseChatModel based on settings.llm_provider:

  - ollama  (default) - ChatOllama, runs against a local Ollama daemon
  - mock              - deterministic offline stub, no network / no API key

The ``openai`` and ``anthropic`` providers were **removed in INC16** (Ollama-only
profile). Requesting either is now a hard configuration error — the factory
fails fast with :func:`_removed_provider_message` instead of silently degrading
to the mock stub. See ``_REMOVED_PROVIDERS``.

Each provider is imported lazily so the base install stays lean. Install the
Ollama extra to enable the local provider:

    pip install forgeflow[ollama]
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


class ModelUnavailableError(RuntimeError):
    """Raised when no usable model provider can be built.

    In production the fallback chain must **not** terminate at the deterministic
    ``mock`` stub — that stub is a *development* fallback (``_fallback_candidates``
    only appends it outside production). An exhausted production chain therefore
    fails loudly instead of silently handing back canned output. The message
    names every candidate that was attempted so the operator can fix the config
    (bring the Ollama daemon up, or edit ``MODEL_FALLBACK_CHAIN`` to name a
    reachable model tag).
    """

    def __init__(self, tried: list[str]) -> None:
        self.tried = list(tried)
        summary = ", ".join(self.tried) if self.tried else "(none)"
        super().__init__(
            f"No usable LLM provider could be built in production. Tried: {summary}. "
            "The 'mock' provider is a development-only fallback and is disabled in "
            "production — bring the Ollama daemon up, or edit MODEL_FALLBACK_CHAIN "
            "to name a reachable model."
        )


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


def _defaults_for_model(model: Any) -> dict[str, Any]:
    """A ``{field: default}`` seed for every **required** field of ``model``.

    Optional fields are omitted so pydantic applies their declared default —
    keeping the result identical to ``model_validate({})`` for a schema that has
    no required fields (the historical, correct behaviour), while giving a
    schema *with* required fields a valid, deterministic seed instead of the old
    silent ``{}``.
    """
    defaults: dict[str, Any] = {}
    fields = getattr(model, "model_fields", None)
    if not fields:
        return defaults
    for name, field in fields.items():
        try:
            required = field.is_required()
        except Exception:  # noqa: BLE001 — defensive: unknown field object shape
            required = True
        if not required:
            continue
        defaults[name] = _default_for_annotation(getattr(field, "annotation", None))
    return defaults


def _default_for_annotation(annotation: Any) -> Any:
    """Deterministic default value for one pydantic field annotation.

    Used by :meth:`MockChatModel.with_structured_output` to synthesise a *valid*
    instance for a schema with required fields, instead of collapsing to ``{}``
    (which made downstream attribute access, e.g. supervisor ``decision.next``,
    raise ``AttributeError``). The mapping is intentionally simple and total:

    ``str→""`` · ``int→0`` · ``float→0.0`` · ``bool→False`` · ``list→[]`` ·
    ``dict→{}`` · ``tuple→()`` · ``Optional[X]→None`` · nested ``BaseModel`` →
    its recursively-built defaults · ``Enum→`` first member · ``Literal→`` first
    choice · anything unrecognised ``→None``.
    """
    import enum
    import types as _types
    import typing

    from pydantic import BaseModel

    if annotation is None or annotation is type(None):
        return None

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is typing.Union or origin is getattr(_types, "UnionType", None):
        # Optional[X] (or X | None) ⇒ None per spec; otherwise the first member.
        if type(None) in args:
            return None
        return _default_for_annotation(args[0]) if args else None

    if origin is typing.Literal:
        return args[0] if args else None
    if origin in (list, set, frozenset):
        return []
    if origin is tuple:
        return ()
    if origin is dict:
        return {}

    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            members = list(annotation)
            return members[0].value if members else None
        if issubclass(annotation, BaseModel):
            return _defaults_for_model(annotation)
        if annotation is bool:  # `bool` first: it is a subclass of `int`
            return False
        if annotation is str:
            return ""
        if annotation is int:
            return 0
        if annotation is float:
            return 0.0
        if annotation is bytes:
            return b""
        if annotation in (list, set, frozenset):
            return []
        if annotation is dict:
            return {}

    return None


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
        still return a Runnable so ``.invoke()``/``.ainvoke()`` never explode;
        it generates a **valid, deterministic** instance by seeding every
        required field with a type-appropriate default (see
        :func:`_default_for_annotation`). A schema with no required fields thus
        behaves exactly as before (``model_validate({})``), while a schema that
        *has* required fields no longer degrades to ``{}`` — which is what made
        the real graph unusable under the mock provider (P0-2).
        """
        from langchain_core.runnables import RunnableLambda

        def _populate(_: Any, _schema: Any = schema) -> Any:
            if hasattr(_schema, "model_validate"):
                defaults = _defaults_for_model(_schema)
                try:
                    return _schema.model_validate(defaults)
                except Exception as exc:  # noqa: BLE001 — never break the offline path
                    # Extreme fallback only: a schema whose defaults we could not
                    # satisfy (custom validators, exotic types). Kept so the mock
                    # can never raise, but it is not the normal path.
                    logger.debug(
                        "MockChatModel: could not synthesise %s from defaults "
                        "(%s); returning {}",
                        getattr(_schema, "__name__", _schema),
                        exc,
                    )
            return {}

        return RunnableLambda(_populate)


#: Provider identifiers ``get_model`` knows how to build directly. Anything else
#: found in ``MODEL_FALLBACK_CHAIN`` is treated as an explicit Ollama model tag.
_KNOWN_PROVIDERS: tuple[str, ...] = ("ollama", "mock")

#: Providers removed in INC16. They are retained here *only* so the factory can
#: recognise (and reject) them explicitly: a request for one must fail fast with
#: :func:`_removed_provider_message`, never be mistaken for a bare Ollama model
#: tag and never silently degrade to the mock stub.
_REMOVED_PROVIDERS: tuple[str, ...] = ("openai", "anthropic")


def _removed_provider_message(name: str) -> str:
    """Canonical error text for a provider removed in INC16 (single source).

    Deliberately names ``ollama`` / ``mock`` as the supported alternatives and
    never mentions a missing API key — the point is that the provider itself is
    gone, not that it is misconfigured.
    """
    return (
        f"LLM_PROVIDER='{name}' is no longer supported: the '{name}' provider was "
        f"removed in INC16 (Ollama-only profile). Use 'ollama' (local model) or "
        f"'mock' (deterministic offline/testing). See docs/configuration.md."
    )

#: Substrings that mark a *thinking* model. C1 gates them on the thinking switch
#: (INC16 reversal): a thinking model is built while ``OLLAMA_THINK=false``
#: (``think=false`` — measured to answer normally) and skipped only when
#: ``OLLAMA_THINK=true`` (which spends the whole ``num_predict`` budget on the
#: hidden trace and returns ""). See :func:`get_model`.
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
    """True when a name/tag contains a thinking-model marker.

    This is a *pure name test*: it only reports whether ``name`` looks like a
    thinking model. It does **not** decide whether such a model may be built or
    served — that is the caller's job, keyed on ``settings.ollama_think`` (see
    :func:`get_model` and :func:`get_vision_model`). The direction is backed by
    daemon measurement (``qa_tmp/inc16/ollama_think_probe.json``): a thinking
    model with ``think=false`` answers normally (62-char JSON,
    ``done_reason=stop``), while ``think=true`` returns empty content
    (``done_reason=length``). So a thinking tag is built when ``ollama_think`` is
    false and skipped when it is true.
    """
    key = (name or "").strip().lower()
    if not key or key in _KNOWN_PROVIDERS:
        return False
    return any(marker in key for marker in _THINKING_MODEL_MARKERS)


def _resolved_ollama_tag(name: str, settings: Settings, strong: bool) -> str | None:
    """The Ollama model tag a chain entry *resolves to*, or ``None`` if not Ollama.

    The C1 guard has to look past the chain-entry *name*: the primary ``ollama``
    provider creates its model from ``OLLAMA_MODEL`` / ``OLLAMA_MODEL_STRONG``
    (never from the chain string), so a thinking tag configured there would slip
    through a name-only check. This helper surfaces that resolved tag:

      * ``"ollama"``            → ``settings.ollama_model[_strong]``
      * a bare tag (``qwen3:8b``) → the tag itself
      * ``mock`` / ``openai`` / ``anthropic`` → ``None`` (not Ollama-backed; the
        last two are the INC16-removed providers, kept as explicit non-Ollama
        names so they are never probed as a daemon tag)
    """
    if name == "ollama":
        return settings.ollama_model_strong if strong else settings.ollama_model
    if name in _KNOWN_PROVIDERS or name in _REMOVED_PROVIDERS:
        return None
    return name


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
    ``MODEL_FALLBACK_CHAIN``.

    The deterministic ``mock`` stub is appended **only outside production**: it
    is a development / test fallback, never a production one. In production an
    exhausted chain raises ``ModelUnavailableError`` instead (see :func:`get_model`).
    """
    candidates: list[str] = []
    for raw in [settings.llm_provider, *(settings.model_fallback_chain or [])]:
        key = str(raw or "").strip().lower()
        if key and key not in candidates:
            candidates.append(key)
    if not settings.is_production() and "mock" not in candidates:
        candidates.append("mock")
    return candidates


def _uses_ollama(name: str) -> bool:
    """True for the ``ollama`` provider and for bare Ollama model-tag entries.

    The INC16-removed providers (``openai`` / ``anthropic``) are explicit
    non-Ollama names: they must never be routed through the daemon-reachability
    probe.
    """
    return name == "ollama" or (
        name not in _KNOWN_PROVIDERS and name not in _REMOVED_PROVIDERS
    )


def _build_candidate(
    name: str, settings: Settings, strong: bool, *, is_primary: bool
) -> BaseChatModel:
    """Build one chain entry.

    A removed provider name fails fast unconditionally (primary or fallback). A
    non-provider name is an explicit Ollama model tag — but only as a
    *fallback*; as the primary it is a misconfiguration and raises.
    """
    if name in _REMOVED_PROVIDERS:
        raise ValueError(_removed_provider_message(name))
    if name == "ollama":
        return _build_ollama(settings, strong)
    if name == "mock":
        return _build_mock(settings, strong)
    if is_primary:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{settings.llm_provider}'. "
            f"Expected one of: ollama, mock."
        )
    return _build_ollama_model(settings, name)


def get_model(strong: bool = False) -> BaseChatModel:
    """Return a chat model instance for the configured provider.

    C1 multi-model fallback: the configured provider is tried first, then each
    entry of ``MODEL_FALLBACK_CHAIN`` in order. A thinking model is **built**
    while ``OLLAMA_THINK=false`` (``think=false`` — measured to answer normally)
    and **skipped** only when ``OLLAMA_THINK=true`` (which returns empty content;
    INC16 semantic reversal — see the guard below). The guard applies to the
    chain-entry name **and** to the tag an ``ollama`` entry resolves to
    (``OLLAMA_MODEL`` / ``OLLAMA_MODEL_STRONG``). When Ollama is the chosen
    provider but its daemon is unreachable, the chain degrades to the next model
    instead of handing back a model that cannot answer.

    Environment-controlled fallback (T1): outside production the chain always
    ends at the deterministic ``mock`` model, so a memory-constrained host
    always has a working degrade path. In **production** ``mock`` is *never* an
    automatic fallback — an exhausted chain raises :class:`ModelUnavailableError`
    so a dead LLM can't silently serve canned output.

    A hard *configuration* error on the explicitly chosen provider (unknown
    provider name, a provider removed in INC16, or a missing optional extra) still
    fails fast — only the *fallback* path is allowed to degrade, so a typo in
    ``LLM_PROVIDER`` never silently runs on the mock.

    Args:
        strong: If True, return the higher-capability variant (used by
            supervisor + judge). Otherwise return the cheaper worker model.
    """
    settings = get_settings()
    prod = settings.is_production()
    candidates = _fallback_candidates(settings)
    attempted: list[str] = []

    for index, name in enumerate(candidates):
        is_primary = index == 0

        # INC16 — OpenAI / Anthropic were removed. Requesting one is a hard
        # configuration error and must fail fast *here*, outside the try/except
        # that decides degrade-vs-raise, so the fallback machinery can never
        # swallow it and serve the next candidate silently. This check also runs
        # before the bare model-tag path below, so a removed provider name is
        # never mistaken for an Ollama tag.
        if name in _REMOVED_PROVIDERS:
            raise ValueError(_removed_provider_message(name))

        # C1 guard (INC16 — semantic REVERSAL, backed by daemon measurements).
        # The dangerous combination is NOT "the model is a thinking model"; it is
        # "a thinking model with thinking *enabled*". Measured on this box
        # (qa_tmp/inc16/ollama_think_probe.json, chatollama_reasoning_probe.json):
        #   qwen3:8b + think=false → 62-char JSON content, done_reason=stop   ✅
        #   qwen3:8b + think=true  → EMPTY content, 306-char trace,
        #                            done_reason=length                      ❌
        # ``settings.ollama_think`` is the switch sent to the daemon as ``think``
        # (via ChatOllama(reasoning=…)). So we skip a thinking model ONLY when
        # OLLAMA_THINK=true (the broken combo that burns num_predict and returns
        # ""). With OLLAMA_THINK=false (the enforced default, think=false) we
        # build it normally — measured to answer correctly. The check looks at the
        # chain-entry name AND the tag the entry resolves to, so a thinking
        # OLLAMA_MODEL on the primary provider (not named in the chain) is seen.
        resolved_tag = _resolved_ollama_tag(name, settings, strong)
        offending = (
            name
            if _is_thinking_model(name)
            else (
                resolved_tag
                if resolved_tag and _is_thinking_model(resolved_tag)
                else None
            )
        )
        if offending is not None:
            if settings.ollama_think:
                logger.warning(
                    "C1: skipping thinking model %r (entry %r) — OLLAMA_THINK=true "
                    "makes a thinking model spend its whole num_predict budget on "
                    "the hidden trace and return empty content (measured: qwen3:8b "
                    "→ 0-char content, 306-char think, done_reason=length). Set "
                    "OLLAMA_THINK=false to use it normally (measured: 62-char JSON, "
                    "done_reason=stop).",
                    offending,
                    name,
                )
                continue
            logger.info(
                "C1: thinking model %r (entry %r) with OLLAMA_THINK=false "
                "(think=false) — measured to answer normally (62-char JSON, "
                "done_reason=stop); building it.",
                offending,
                name,
            )

        # Production must never silently fall back to the development mock stub.
        # validate_runtime() also flags this as a fatal misconfiguration; refusing
        # it here keeps a direct get_model() call fail-closed too.
        if prod and name == "mock":
            logger.warning(
                "C1: refusing 'mock' in production — it is a development-only "
                "stub and must not serve prod traffic"
            )
            continue

        attempted.append(name)
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

    # Production: refuse to hand back the mock stub — fail closed instead.
    if prod:
        raise ModelUnavailableError(attempted)

    # Non-production: the deterministic dev fallback always works.
    return _build_mock(settings, strong)


def get_vision_model() -> BaseChatModel:
    """Return the chat model for the *vision* path (image description).

    The vision capability gets its own dedicated slot
    (``settings.ollama_vision_model``) instead of reusing ``OLLAMA_MODEL`` /
    ``OLLAMA_MODEL_STRONG``. Why: ``multimodal.images.describe_image`` always
    built ``get_model(strong=True)``, yet ``runtime.attachments.vision_available``
    sniffed *both* reasoning slots — so a text-only model in the ``strong`` slot
    silently broke image handling while the predicate still advertised vision (a
    promise the call path could not keep). Routing both the call and the predicate
    through this single slot makes them agree.

    Provider behaviour mirrors :func:`get_model` but stays a single, explicit
    slot (no fallback chain):

      * ``ollama`` → a ``ChatOllama`` for ``settings.ollama_vision_model``;
      * ``mock`` (or any other provider) → the deterministic offline stub, so the
        offline / test profile never dials a real vision model;
      * a thinking vision model with ``OLLAMA_THINK=true`` fails fast — the same
        C1 rule as :func:`get_model` (that combo returns empty content);
      * a provider removed in INC16 fails fast with the same message as
        :func:`get_model`.
    """
    settings = get_settings()
    provider = (settings.llm_provider or "").strip().lower()
    if provider in _REMOVED_PROVIDERS:
        raise ValueError(_removed_provider_message(provider))
    if provider == "ollama":
        vision_tag = settings.ollama_vision_model
        # C1 guard for the vision slot (mirrors get_model()): a thinking vision
        # model with thinking ON returns EMPTY content — measured on this box
        # (qa_tmp/inc16/ollama_think_probe.json: qwen3:8b + think=true → 0-char
        # content, 306-char trace, done_reason=length; think=false → 62-char JSON,
        # done_reason=stop). Building it would make image description silently
        # return "" — a promise the path cannot keep. Fail fast instead; the
        # attachment boundary (runtime.attachments._ingest_image) wraps the vision
        # call in try/except and degrades the image to metadata_only, so the error
        # never reaches the user as a fake (or empty) description.
        if _is_thinking_model(vision_tag) and settings.ollama_think:
            raise ValueError(
                f"OLLAMA_VISION_MODEL='{vision_tag}' is a thinking model and "
                f"OLLAMA_THINK=true: the model spends its whole num_predict budget "
                f"on the hidden trace and returns empty content (measured: 0-char "
                f"content, 306-char think, done_reason=length), so image "
                f"description would silently come back empty. Either set "
                f"OLLAMA_THINK=false (measured to answer normally: 62-char JSON, "
                f"done_reason=stop) or point OLLAMA_VISION_MODEL at a non-thinking "
                f"vision model (e.g. qwen2.5vl:3b)."
            )
        return _build_ollama_model(settings, vision_tag)
    # ``mock`` and everything else → the offline stub (keeps the offline suite
    # runnable without a real vision model).
    return _build_mock(settings, False)


def _build_mock(settings: Settings, strong: bool) -> BaseChatModel:
    """Offline deterministic model — no credentials, no network."""
    logger.debug("Building MockChatModel(strong=%s)", strong)
    return MockChatModel(strong=strong)


def _build_ollama(settings: Settings, strong: bool) -> BaseChatModel:
    model_name = settings.ollama_model_strong if strong else settings.ollama_model
    return _build_ollama_model(settings, model_name)


def _build_ollama_model(settings: Settings, model_name: str) -> BaseChatModel:
    """Build a ChatOllama for an explicit model tag (shared with the C1 chain).

    ``reasoning`` is wired to ``settings.ollama_think`` (T2) and is what the
    daemon receives as ``think``. The default ``False`` maps to ``think=false``:
    a thinking model such as qwen3 then answers normally (measured on this box:
    qwen3:8b → 62-char JSON, ``done_reason=stop``). ``OLLAMA_THINK=true`` maps to
    ``think=true``, under which the same model spends its whole ``num_predict``
    budget on the hidden trace and returns an empty ``content`` (measured:
    0-char content, 306-char think, ``done_reason=length``) — the C1 guard skips
    a thinking model in exactly that case. See :func:`get_model`.
    """
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise ProviderNotInstalledError(
            "Ollama support requires the optional 'ollama' extra. "
            "Install with: pip install 'forgeflow[ollama]'"
        ) from exc

    logger.debug(
        "Building ChatOllama(model=%s, base_url=%s, reasoning=%s)",
        model_name,
        settings.ollama_base_url,
        settings.ollama_think,
    )
    # langchain-ollama >=0.3 exposes the ``reasoning`` field; on older builds
    # that lack it we fall back to a plain construction so the provider keeps
    # working (the guard still prevents thinking tags from reaching here unless
    # the operator opted in).
    try:
        return ChatOllama(
            model=model_name,
            base_url=settings.ollama_base_url,
            temperature=0,
            reasoning=settings.ollama_think,
        )
    except (TypeError, ValueError):  # pragma: no cover — only on langchain-ollama < 0.3
        return ChatOllama(
            model=model_name,
            base_url=settings.ollama_base_url,
            temperature=0,
        )
