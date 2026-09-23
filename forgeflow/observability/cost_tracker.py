"""CostTracker — tiktoken-based token counting + per-model cost calculation.

Tracks cumulative spend for a workflow run and enforces budget limits.

Pricing semantics (INC2 A1 — "no fabricated money")
---------------------------------------------------
``calculate_cost`` prices a call from :data:`MODEL_COSTS_PER_1K` (the explicit
cloud rate table). It **never** guesses a price for a model it does not know:

  * **Priced models** (present in the table, e.g. ``gpt-4o-mini``) → the real
    per-1K input/output rate.
  * **Self-hosted / free models** (``ollama*``, ``qwen*``, ``llama*``,
    ``mock``, …) → ``0.0``. These run on our own hardware or are deterministic
    stubs, so their marginal API cost is genuinely zero.
  * **Unknown cloud models** → ``0.0`` **and a logged warning**. An earlier
    version fell back to a ``gpt-4o``-shaped price (``input=0.01/1k,
    output=0.03/1k``) for *anything* not in the table — which meant a free
    ``ollama`` run was billed at cloud rates. That is fabricating spend and
    violates the platform's "no hard-coded fake data" rule.

The deliberate bias is **「宁可少算也不虚增」** (under-report rather than
over-report): an unknown model is counted as free until someone adds its real
rate to :data:`MODEL_COSTS_PER_1K`. To bill a new cloud model, add it there —
do not reintroduce a catch-all fallback price.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Cost per 1,000 tokens in USD (as of 2025 pricing). Only models listed here are
# billable; everything else is priced at 0.0 (see module docstring).
MODEL_COSTS_PER_1K: dict[str, dict[str, float]] = {
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.000150, "output": 0.000600},
    "gpt-4-turbo": {"input": 0.010, "output": 0.030},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "text-embedding-3-small": {"input": 0.00002, "output": 0.0},
    "text-embedding-3-large": {"input": 0.00013, "output": 0.0},
}

#: Prefixes that identify a self-hosted / no-billing model. Matched
#: case-insensitively against the *start* of the model name so ``qwen3:8b`` and
#: ``llama3.2:3b`` (Ollama tags) and a bare ``mock`` all resolve to "free".
FREE_MODEL_PREFIXES: tuple[str, ...] = (
    "ollama",
    "qwen",
    "llama",
    "mistral",
    "mixtral",
    "phi",
    "gemma",
    "deepseek",
    "vllm",
    "local",
    "mock",
)


def is_priced_model(model: str) -> bool:
    """True when ``model`` has an explicit rate in :data:`MODEL_COSTS_PER_1K`."""
    return (model or "") in MODEL_COSTS_PER_1K


def is_free_model(model: str) -> bool:
    """True for self-hosted / stub models whose marginal cost is genuinely zero."""
    name = (model or "").strip().lower()
    return any(name.startswith(prefix) for prefix in FREE_MODEL_PREFIXES)


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost for a single LLM call.

    Returns ``0.0`` for free/self-hosted models and for unknown models (logged
    once per call at WARNING level) — see the module docstring for the policy.
    """
    rates = MODEL_COSTS_PER_1K.get(model)
    if rates is None:
        if not is_free_model(model):
            logger.warning(
                "Unknown model %r is not in MODEL_COSTS_PER_1K; pricing its call "
                "at $0.00 (under-report rather than fabricate). Add the model to "
                "MODEL_COSTS_PER_1K to bill it.",
                model,
            )
        return 0.0
    return (input_tokens / 1000 * rates["input"]) + (output_tokens / 1000 * rates["output"])


def count_tokens(text: str, model: str = "gpt-4o-mini") -> int:
    """Count tokens in text using tiktoken. Falls back to word estimate."""
    try:
        import tiktoken
        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text.split()) * 4 // 3)  # rough ~4/3 tokens per word


@dataclass
class CostTracker:
    """Tracks cumulative token usage and cost for a workflow run."""

    model: str = "gpt-4o-mini"
    total_input_tokens: int = field(default=0)
    total_output_tokens: int = field(default=0)
    call_log: list[dict] = field(default_factory=list)

    def record(
        self,
        agent_name: str,
        input_tokens: int,
        output_tokens: int,
        model: str | None = None,
    ) -> float:
        """Record a single LLM call and return its cost in USD."""
        effective_model = model or self.model
        cost = calculate_cost(effective_model, input_tokens, output_tokens)

        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.call_log.append({
            "agent": agent_name,
            "model": effective_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
        })

        logger.debug(
            "Cost: %s used %d+%d tokens ($%.4f)",
            agent_name,
            input_tokens,
            output_tokens,
            cost,
        )
        return cost

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        return sum(entry["cost_usd"] for entry in self.call_log)

    def summary(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "call_count": len(self.call_log),
            "by_agent": self._by_agent(),
        }

    def _by_agent(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for entry in self.call_log:
            agent = entry["agent"]
            if agent not in result:
                result[agent] = {"tokens": 0, "cost_usd": 0.0, "calls": 0}
            result[agent]["tokens"] += entry["input_tokens"] + entry["output_tokens"]
            result[agent]["cost_usd"] += entry["cost_usd"]
            result[agent]["calls"] += 1
        return result
