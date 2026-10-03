"""INC46 T02 — Experience/Pattern Miner: a **recomputable** pattern score.

Sits directly on top of T01's materialised trace. Given the tool sequences of a
tenant's runs (each run = its ordered ``run_steps``), it collapses each run to a
normalised **pattern key** and computes four honest, independently-recomputable
metrics plus a weighted ``pattern_score``. There is **no** heuristics-by-vibes and
**no** LLM anywhere in this module — every number has a stated numerator and
denominator.

Definitions (INC46 §1.2 / §7-T02)
---------------------------------
Let ``R`` = number of runs in the window, and for a pattern ``P`` let
``S(P)`` be the runs whose collapsed signature equals ``P`` with ``N = |S(P)|``.

===============  =============================================  ===============
metric           formula                                        zero denominator
===============  =============================================  ===============
frequency        ``N / R``                                      ``None``
success_rate     ``#{r∈S(P): outcome(r)=="success"} / N``       ``None``
tool_consistency ``max_seq_count / N``  (modal **raw** seq)     ``None``
output_consistency ``max_keyset_count / N`` (modal keyset)      ``None``
pattern_score    ``0.25·freq + 0.35·succ + 0.20·tool + 0.20·out`` ``None`` (any None)
===============  =============================================  ===============

**Zero denominator ⇒ ``None``, never a fabricated ``0``** (the data-honesty red
line, §8). ``ExperiencePattern.to_dict()`` emits every intermediate quantity —
the numerator *and* denominator of all four metrics — so any reviewer can
recompute the score by hand.

Derivations that are *not* stored in ``run_steps``
--------------------------------------------------
``run_steps`` carries the tool sequence but not a run-level ``outcome`` nor a
"output key set". Both are derived here, deterministically, from the same step
list, and exposed as public helpers so they are recomputable too:

* :func:`derive_run_outcome` — a run is ``"failure"`` iff any step carries a
  hard-failure status (``error`` / ``unavailable`` / ``refused``, exactly the
  statuses the executor contract marks as *run failed*), else ``"success"``.
* :func:`run_output_keyset` — the sorted union of the steps' ``output.payload``
  keys (the tool's real output shape), as an immutable tuple.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "PATTERN_WEIGHTS",
    "PATTERN_SCORE_THRESHOLD",
    "FAILURE_STATUSES",
    "PatternMetrics",
    "ExperiencePattern",
    "pattern_key",
    "safe_ratio",
    "score_components",
    "derive_run_outcome",
    "run_output_keyset",
    "qualifies",
    "step_tool",
    "step_status",
    "step_index",
    "run_id_of",
    "mine_patterns",
    "mine_tenant_patterns",
]

#: Weighted blend of the four metrics. The weights **must** sum to exactly 1.0
#: (pinned by the unit suite) so ``pattern_score`` is itself on a 0..1 scale.
PATTERN_WEIGHTS: dict[str, float] = {
    "frequency": 0.25,
    "success_rate": 0.35,
    "tool_consistency": 0.20,
    "output_consistency": 0.20,
}

#: A pattern is a *qualified* candidate only when its score clears this bar
#: **and** its support clears ``settings.skill_candidate_min_experiences``.
PATTERN_SCORE_THRESHOLD = 0.60

#: Step statuses that make a **run** a failure — verbatim the executor contract
#: (tool_executor.py: ``error`` / ``unavailable`` / ``refused`` ⇒ run failed;
#: ``blocked`` / ``awaiting_approval`` ⇒ not a failure).
FAILURE_STATUSES: frozenset[str] = frozenset({"error", "unavailable", "refused"})

#: Internal rounding for every published metric (6 dp) so a value round-trips
#: through JSONB unchanged and a reviewer's hand-computation matches exactly.
_ROUND = 6


# --------------------------------------------------------------------------- #
# step accessors — accept a TraceStep OR a plain mapping (offline/test use)    #
# --------------------------------------------------------------------------- #
def _attr(step: Any, name: str, default: Any = None) -> Any:
    if isinstance(step, dict):
        return step.get(name, default)
    return getattr(step, name, default)


def step_tool(step: Any) -> str:
    """The step's tool id as a non-empty string (``""`` when absent)."""
    value = _attr(step, "tool")
    return str(value) if value else ""


def step_status(step: Any) -> str | None:
    """The step's status as a string, or ``None`` when absent."""
    value = _attr(step, "status")
    return str(value) if value else None


def _attempt_of(step: Any) -> int | None:
    value = _attr(step, "attempt")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def step_index(step: Any) -> int:
    """The step's positional index within its run (``0`` when absent)."""
    value = _attr(step, "step_index", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _output_of(step: Any) -> dict[str, Any]:
    value = _attr(step, "output")
    return value if isinstance(value, dict) else {}


def _payload_of(step: Any) -> Any:
    """The invocation payload (the tool's real output) when it is a dict."""
    return _output_of(step).get("payload")


# --------------------------------------------------------------------------- #
# public derivations (recomputable)                                            #
# --------------------------------------------------------------------------- #
def safe_ratio(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator`` rounded to 6 dp, or ``None`` when ``den <= 0``.

    **Never** returns a fabricated ``0`` for an empty denominator — an undefined
    rate is ``None`` (incident-honesty red line, §8).
    """
    if denominator <= 0:
        return None
    return round(numerator / denominator, _ROUND)


def derive_run_outcome(steps: list[Any]) -> str:
    """``"failure"`` iff any step has a hard-failure status, else ``"success"``."""
    for step in steps:
        if step_status(step) in FAILURE_STATUSES:
            return "failure"
    return "success"


def run_output_keyset(steps: list[Any]) -> tuple[str, ...]:
    """Sorted union of the run's step ``output.payload`` keys (immutable)."""
    keys: set[str] = set()
    for step in steps:
        payload = _payload_of(step)
        if isinstance(payload, dict):
            keys.update(str(k) for k in payload)
    return tuple(sorted(keys))


def pattern_key(sequence: list[str]) -> str:
    """Normalised signature: drop empties, collapse **adjacent** duplicates, join ``"→"``.

    ``["a","a","b","a"] → "a→b→a"``; ``[] → ""``. Distinct tools keep their
    first-occurrence order (collapsing adjacent repeats only), which is what
    makes two runs with the same distinct-tool *order* land in one pattern even
    when they repeat a step a different number of times — and what leaves
    ``tool_consistency`` a meaningful (non-degenerate) measurement of the raw
    sequence's agreement *within* that pattern.
    """
    collapsed: list[str] = []
    for tool in sequence:
        name = str(tool) if tool else ""
        if not name:
            continue
        if collapsed and collapsed[-1] == name:
            continue
        collapsed.append(name)
    return "→".join(collapsed)


def _collapse_adjacent(sequence: list[str]) -> list[str]:
    """The collapsed list (same normalisation ``pattern_key`` applies)."""
    collapsed: list[str] = []
    for tool in sequence:
        name = str(tool) if tool else ""
        if not name:
            continue
        if collapsed and collapsed[-1] == name:
            continue
        collapsed.append(name)
    return collapsed


def run_id_of(steps: list[Any]) -> str:
    """The run id shared by a run's steps (first step wins; ``""`` when unknown)."""
    for step in steps:
        value = _attr(step, "run_id")
        if value:
            return str(value)
    return ""


def _example_step_ids(steps: list[Any]) -> list[str]:
    """Runtime-shaped step ids (``"{run_id}:{attempt}:{index}"``) for one run.

    ``run_steps`` (migration 010/019) stores no ``step_id``; the runtime's step
    id is reconstructible from ``run_id`` + ``attempt`` + ``step_index`` (the
    same ``"{run_id}:{attempt}:{index}"`` shape ``planning.py`` stamps), so this
    is evidence, not invented text.
    """
    run_id = run_id_of(steps)
    out: list[str] = []
    for step in steps:
        idx = step_index(step)
        attempt = _attempt_of(step)
        if attempt is None:
            out.append(f"{run_id}:{idx}")
        else:
            out.append(f"{run_id}:{attempt}:{idx}")
    return out


# --------------------------------------------------------------------------- #
# value objects                                                                #
# --------------------------------------------------------------------------- #
@dataclass
class PatternMetrics:
    """The four metrics + the weighted score + every intermediate quantity."""

    frequency: float | None
    success_rate: float | None
    tool_consistency: float | None
    output_consistency: float | None
    pattern_score: float | None
    #: ``support`` = N (runs containing the pattern); ``sample_size`` = R (runs
    #: in the window). ``frequency`` is therefore exactly ``support / sample_size``.
    support: int
    sample_size: int
    #: Numerators that are *not* implied by the two counts above (the
    #: denominators are ``support``) — emitted so the score is fully recomputable.
    success_count: int = 0
    max_seq_count: int = 0
    max_keyset_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Full intermediate quantities: every metric **with its numerator/denominator**."""
        return {
            "frequency": self.frequency,
            "success_rate": self.success_rate,
            "tool_consistency": self.tool_consistency,
            "output_consistency": self.output_consistency,
            "pattern_score": self.pattern_score,
            "support": self.support,
            "sample_size": self.sample_size,
            # --- explicit numerators / denominators (recomputable) --------- #
            "frequency_numerator": self.support,
            "frequency_denominator": self.sample_size,
            "success_numerator": self.success_count,
            "success_denominator": self.support,
            "tool_consistency_numerator": self.max_seq_count,
            "tool_consistency_denominator": self.support,
            "output_consistency_numerator": self.max_keyset_count,
            "output_consistency_denominator": self.support,
        }


@dataclass
class ExperiencePattern:
    """One mined pattern: its key, tools, metrics and evidence runs."""

    key: str
    tools: list[str]
    metrics: PatternMetrics
    source_run_ids: list[str] = field(default_factory=list)
    example_step_ids: list[str] = field(default_factory=list)

    @property
    def pattern_score(self) -> float | None:
        """Convenience passthrough to the weighted score."""
        return self.metrics.pattern_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "tools": list(self.tools),
            "metrics": self.metrics.to_dict(),
            "source_run_ids": list(self.source_run_ids),
            "example_step_ids": list(self.example_step_ids),
        }


def qualifies(metrics: PatternMetrics, *, min_support: int) -> bool:
    """``pattern_score ≥ PATTERN_SCORE_THRESHOLD`` **and** ``support ≥ min_support``.

    An undefined score (``None``) never qualifies (never a lenient ``0``).
    """
    score = metrics.pattern_score
    return score is not None and score >= PATTERN_SCORE_THRESHOLD and metrics.support >= min_support


def score_components(components: dict[str, float | None]) -> float | None:
    """The weighted blend of the four metrics; ``None`` when **any** is undefined.

    ``components`` must carry the four keys of :data:`PATTERN_WEIGHTS`. A single
    ``None`` component propagates to a ``None`` score — a missing measurement
    never silently becomes a lenient ``0`` contribution.
    """
    values = [components.get(name) for name in PATTERN_WEIGHTS]
    if any(v is None for v in values):
        return None
    total = sum(PATTERN_WEIGHTS[name] * float(components[name]) for name in PATTERN_WEIGHTS)
    return round(total, _ROUND)


# --------------------------------------------------------------------------- #
# the pure miner                                                               #
# --------------------------------------------------------------------------- #
def mine_patterns(runs: list[list[Any]], *, min_support: int) -> list[ExperiencePattern]:
    """Mine patterns from ``runs`` (each a list of one run's TraceStep-likes).

    Pure and deterministic. ``R = len(runs)`` is the window size; runs are grouped
    by :func:`pattern_key` of their tool sequence. Only groups with ``support ≥
    min_support`` are returned, sorted by ``pattern_score`` desc, then ``support``
    desc, then ``key`` asc (stable). An empty ``runs`` list yields ``[]`` (there
    is nothing to score — never a fabricated pattern).
    """
    total_runs = len(runs)
    groups: dict[str, dict[str, Any]] = {}

    for steps in runs:
        raw = [tool for tool in (step_tool(s) for s in steps) if tool]
        key = pattern_key(raw)
        bucket = groups.setdefault(key, {"tools": list(dict.fromkeys(raw)), "members": []})
        bucket["members"].append(
            {
                "raw": tuple(raw),
                "keyset": run_output_keyset(steps),
                "outcome": derive_run_outcome(steps),
                "run_id": run_id_of(steps),
                "steps": steps,
            }
        )

    patterns: list[ExperiencePattern] = []
    for key, bucket in groups.items():
        members: list[dict[str, Any]] = bucket["members"]
        support = len(members)
        if support < min_support:
            continue

        success_count = sum(1 for m in members if m["outcome"] == "success")
        seq_counts = Counter(m["raw"] for m in members)
        max_seq_count = max(seq_counts.values()) if seq_counts else 0
        keyset_counts = Counter(m["keyset"] for m in members)
        max_keyset_count = max(keyset_counts.values()) if keyset_counts else 0

        components: dict[str, float | None] = {
            "frequency": safe_ratio(support, total_runs),
            "success_rate": safe_ratio(success_count, support),
            "tool_consistency": safe_ratio(max_seq_count, support),
            "output_consistency": safe_ratio(max_keyset_count, support),
        }
        metrics = PatternMetrics(
            frequency=components["frequency"],
            success_rate=components["success_rate"],
            tool_consistency=components["tool_consistency"],
            output_consistency=components["output_consistency"],
            pattern_score=score_components(components),
            support=support,
            sample_size=total_runs,
            success_count=success_count,
            max_seq_count=max_seq_count,
            max_keyset_count=max_keyset_count,
        )

        example_steps = members[0]["steps"]
        patterns.append(
            ExperiencePattern(
                key=key,
                tools=list(bucket["tools"]),
                metrics=metrics,
                source_run_ids=[m["run_id"] for m in members if m["run_id"]],
                example_step_ids=_example_step_ids(example_steps),
            )
        )

    patterns.sort(
        key=lambda p: (
            -(p.metrics.pattern_score if p.metrics.pattern_score is not None else -1.0),
            -p.metrics.support,
            p.key,
        )
    )
    return patterns


# --------------------------------------------------------------------------- #
# the tenant read path (consumes T01's trace)                                  #
# --------------------------------------------------------------------------- #
def _in_window(created_at: Any, window_days: int) -> bool:
    """Whether ``created_at`` (ISO-8601) falls within the last ``window_days``.

    An unparseable/missing timestamp is **kept** (not silently dropped) — a
    missing clock reading is not evidence that the run is out of window.
    """
    if window_days <= 0:
        return True
    if not isinstance(created_at, str) or not created_at.strip():
        return True
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment >= datetime.now(timezone.utc) - timedelta(days=window_days)


async def mine_tenant_patterns(
    tenant_id: str | None,
    *,
    window_days: int = 30,
    min_support: int | None = None,
) -> list[ExperiencePattern]:
    """Read a tenant's trace (T01) and mine its patterns.

    Tenant **fail-closed**: an unresolved tenant reads nothing (``[]``), never
    every tenant's rows. The window filter trims to ``window_days`` by each
    step's ``created_at``; runs are regrouped by ``run_id`` and ordered by
    ``step_index`` before mining.
    """
    if not tenant_id:
        return []

    from forgeflow.runtime.trace_store import list_for_tenant

    steps = await list_for_tenant(tenant_id)
    steps = [s for s in steps if _in_window(_attr(s, "created_at"), window_days)]

    by_run: dict[str, list[Any]] = {}
    for step in steps:
        by_run.setdefault(run_id_of([step]), []).append(step)

    runs: list[list[Any]] = []
    for run_id, run_steps in by_run.items():
        run_steps.sort(key=step_index)
        runs.append(run_steps)

    if min_support is None:
        min_support = int(get_settings().skill_candidate_min_experiences)
    return mine_patterns(runs, min_support=min_support)
