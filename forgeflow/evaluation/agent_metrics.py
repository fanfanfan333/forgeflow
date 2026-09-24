"""INC8 Phase-B — the 12 Agent evaluation metrics, as **pure** definitions.

This module is the design's ``N1`` (docs/sop/11-INC8-AGENT-EVALUATION-DESIGN.md
§2 / §4.1). It holds *only*:

  * :data:`SPECS` — one :class:`MetricSpec` per metric, each honestly labelled
    ``ok`` / ``needs_instrumentation`` / ``not_available`` (design §2.1), with
    its exact formula + data source. This is what lets the API surface tell
    "computed" apart from "computable but not yet wired" apart from "no ground
    truth exists" — never fabricating a number for the latter.
  * :func:`evaluate_run` — reduce a single ``RunRecord``-like object to a plain
    signal dict (no aggregation, no I/O).
  * :func:`aggregate` — fold a cohort of run records (+ optional persisted eval
    samples) into an :class:`AgentEvalSnapshot`.

Import safety (design §4.1-N1): **stdlib only, no I/O, no top-level import of
``api`` / ``runtime.orchestrator`` / ``asyncpg``**. ``RunRecord`` is duck-typed
(``getattr``) so this module never depends on the runtime package, and the
offline (``STORAGE_BACKEND=memory``) profile stays dependency-free.

Honesty contract (design §1.3): a metric whose ground truth is unavailable gets
``value=None`` and ``has_data=False``. It is **never** filled with ``0.0`` — a
fabricated zero is exactly the "声明了机制但没实现" defect this initiative
exists to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

__all__ = [
    "COMPUTABILITY_OK",
    "COMPUTABILITY_NEEDS_INSTRUMENTATION",
    "COMPUTABILITY_NOT_AVAILABLE",
    "DIMENSIONS",
    "AgentEvalMetric",
    "AgentEvalSnapshot",
    "MetricSpec",
    "SPECS",
    "aggregate",
    "evaluate_run",
    "spec_for",
]

#: Computable from existing data (design §2.1 ✅).
COMPUTABILITY_OK = "ok"
#: Computable, but needs new instrumentation / wiring first (design §2.1 🟡).
COMPUTABILITY_NEEDS_INSTRUMENTATION = "needs_instrumentation"
#: No ground truth exists — honestly unavailable (design §2.1 ⛔).
COMPUTABILITY_NOT_AVAILABLE = "not_available"

#: Metric dimensions, in report order.
DIMENSIONS: tuple[str, ...] = ("quality", "cost", "reliability")

#: Run statuses that count as a terminal (finished) run — matches
#: ``observability.metrics_source._TERMINAL_STATUSES``.
_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed")

#: ``metric_name`` values a persisted judge sample may carry for groundedness
#: (the design names it ``groundedness``; ``faithfulness`` is accepted as the
#: raw ``JudgeScore`` alias so a caller that stores the judge field verbatim
#: still lands in the right bucket).
_GROUNDEDNESS_ALIASES: frozenset[str] = frozenset({"groundedness", "faithfulness"})
#: ``metric_name`` values a persisted judge sample may carry for hallucination.
_HALLUCINATION_ALIASES: frozenset[str] = frozenset({"hallucination", "hallucination_rate"})

#: Sentinel distinguishing "the field is absent (not instrumented)" from "the
#: field is present but empty (a genuine zero)". This is what keeps
#: ``skill_reuse_rate`` honest on a data source that does not carry per-run
#: skill signals — it reports "no data" rather than a fabricated ``0.0``.
_UNSET: Any = object()


@dataclass(frozen=True)
class MetricSpec:
    """The honest definition of one metric (what it is, how, and from where).

    ``computability`` is the single most important field: ``not_available``
    means there is no ground truth and ``value`` must stay ``None``.
    """

    name: str
    dimension: str
    computability: str
    unit: str
    formula: str
    source: str
    note: str = ""


@dataclass
class AgentEvalMetric:
    """A measured (or honestly unmeasured) value for one metric."""

    value: float | None = None
    unit: str = ""
    computability: str = COMPUTABILITY_OK
    has_data: bool = False
    formula: str = ""
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "computability": self.computability,
            "has_data": self.has_data,
            "formula": self.formula,
            "source": self.source,
            "note": self.note,
        }


@dataclass
class AgentEvalSnapshot:
    """The three-dimension eval summary returned by ``/metrics/agent-eval``."""

    source: str = "hub_runs"
    durable: bool = False
    window_days: int = 30
    sample_runs: int = 0
    judged_samples: int = 0
    quality: dict[str, AgentEvalMetric] = field(default_factory=dict)
    cost: dict[str, AgentEvalMetric] = field(default_factory=dict)
    reliability: dict[str, AgentEvalMetric] = field(default_factory=dict)
    generated_at: str = ""

    def metrics_for(self, dimension: str) -> dict[str, AgentEvalMetric]:
        """The metric name → metric map for one dimension."""
        if dimension == "quality":
            return self.quality
        if dimension == "cost":
            return self.cost
        if dimension == "reliability":
            return self.reliability
        return {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "durable": self.durable,
            "window_days": self.window_days,
            "sample_runs": self.sample_runs,
            "judged_samples": self.judged_samples,
            "generated_at": self.generated_at,
            "quality": {"metrics": {k: m.to_dict() for k, m in self.quality.items()}},
            "cost": {"metrics": {k: m.to_dict() for k, m in self.cost.items()}},
            "reliability": {
                "metrics": {k: m.to_dict() for k, m in self.reliability.items()}
            },
        }


# --------------------------------------------------------------------------- #
# The catalogue — one spec per metric (design §2 table, verbatim).             #
# --------------------------------------------------------------------------- #
SPECS: tuple[MetricSpec, ...] = (
    # --- Quality ---------------------------------------------------------- #
    MetricSpec(
        name="task_success_rate",
        dimension="quality",
        computability=COMPUTABILITY_OK,
        unit="ratio",
        formula="completed / (completed + failed)",
        source="RunRecord.status | workflow_runs.status",
        note="运行级完成率（验证裁决 verdict.success），是对『任务成功』的代理，非 ground-truth 成功率",
    ),
    MetricSpec(
        name="tool_selection_accuracy",
        dimension="quality",
        computability=COMPUTABILITY_NOT_AVAILABLE,
        unit="ratio",
        formula="",
        source="RunRecord.llm.plan.{steps,dropped_tools}",
        note=(
            "缺『应选工具集』ground truth，暂不提供；"
            "替代诚实口径 tool_plan_validity_rate=1−|dropped_tools|/|planned_steps|"
        ),
    ),
    MetricSpec(
        name="planning_accuracy",
        dimension="quality",
        computability=COMPUTABILITY_NOT_AVAILABLE,
        unit="ratio",
        formula="",
        source="RunRecord.llm.plan.source / RunRecord.llm.reflection.success",
        note=(
            "缺『正确计划』ground truth，暂不提供；替代诚实口径（🟡）："
            "真实规划率 plan.source=='llm' + 反思自评通过率（自评，弱证据）"
        ),
    ),
    MetricSpec(
        name="retrieval_recall",
        dimension="quality",
        computability=COMPUTABILITY_NOT_AVAILABLE,
        unit="ratio",
        formula="",
        source="context_build_stats.hit_rate",
        note=(
            "缺『相关条目全集』ground truth，暂不提供；"
            "hit_rate 是压缩命中率，口径不同，不得冒充 recall"
        ),
    ),
    MetricSpec(
        name="citation_accuracy",
        dimension="quality",
        computability=COMPUTABILITY_NOT_AVAILABLE,
        unit="ratio",
        formula="",
        source="—",
        note="平台未建模引用/证据（P1-五 RAG 可信知识系统未建），暂不提供",
    ),
    MetricSpec(
        name="groundedness",
        dimension="quality",
        computability=COMPUTABILITY_NEEDS_INSTRUMENTATION,
        unit="score",
        formula="avg(faithfulness)",
        source="agent_eval_samples.metric_value (metric_name='groundedness')",
        note="judge 需接线并落库（agent_eval_samples）；离线/抽样/批量，严禁 run 回环内逐条 LLM",
    ),
    MetricSpec(
        name="hallucination_rate",
        dimension="quality",
        computability=COMPUTABILITY_NEEDS_INSTRUMENTATION,
        unit="ratio",
        formula="flagged / evaluated",
        source="agent_eval_samples.metric_value (metric_name='hallucination')",
        note="judge 需接线并落库；附加确定性代理：幻觉工具率（dropped_tools 非空的 run 占比）",
    ),
    MetricSpec(
        name="memory_recall",
        dimension="quality",
        computability=COMPUTABILITY_NOT_AVAILABLE,
        unit="ratio",
        formula="",
        source="context_build_stats (计数)",
        note="缺『应召回的 memory』ground truth，暂不提供；仅有运营计数信号（非 recall）",
    ),
    MetricSpec(
        name="skill_reuse_rate",
        dimension="quality",
        computability=COMPUTABILITY_NEEDS_INSTRUMENTATION,
        unit="ratio",
        formula="runs_with_reused_skill / all_runs",
        source="RunRecord.skills_used | context_build_stats.skill_refs",
        note="逐任务口径需新增埋点（RunRecord.skills_used）；平台级代理：usage_count>0 的技能占比",
    ),
    # --- Cost ------------------------------------------------------------- #
    MetricSpec(
        name="cost_per_task",
        dimension="cost",
        computability=COMPUTABILITY_OK,
        unit="usd",
        formula="avg(total_cost_usd)",
        source="RunRecord.total_cost_usd | workflow_runs.total_cost_usd",
        note="免费档（mock/Ollama）诚实为 0，非伪造",
    ),
    # --- Reliability ------------------------------------------------------ #
    MetricSpec(
        name="latency_p50_ms",
        dimension="reliability",
        computability=COMPUTABILITY_OK,
        unit="ms",
        formula="percentile(completed_at − created_at, 0.50)",
        source="RunRecord.{created_at,completed_at} | workflow_runs.{created_at,completed_at}",
        note="真分位数（现有 /metrics 只给均值、/metrics/slo 用均值冒充 p95）",
    ),
    MetricSpec(
        name="latency_p95_ms",
        dimension="reliability",
        computability=COMPUTABILITY_OK,
        unit="ms",
        formula="percentile(completed_at − created_at, 0.95)",
        source="RunRecord.{created_at,completed_at} | workflow_runs.{created_at,completed_at}",
        note="真分位数（现有 /metrics 只给均值、/metrics/slo 用均值冒充 p95）",
    ),
    MetricSpec(
        name="failure_recovery_rate",
        dimension="reliability",
        computability=COMPUTABILITY_OK,
        unit="ratio",
        formula="#{observations>0 & completed} / #{observations>0}",
        source="RunRecord.loop.observations + RunRecord.status",
        note="『需要重规划』= loop.observations>0（首跑成功 → 0）",
    ),
)

#: name → spec, for O(1) lookup.
_SPEC_BY_NAME: dict[str, MetricSpec] = {spec.name: spec for spec in SPECS}


def spec_for(name: str) -> MetricSpec | None:
    """The :class:`MetricSpec` for ``name`` (``None`` when unknown)."""
    return _SPEC_BY_NAME.get(name)


# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #
def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``obj.name`` from a dataclass **or** a mapping (duck-typed)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _duration_ms(created_at: Any, completed_at: Any) -> float | None:
    """Wall-clock duration in ms, or ``None`` when a timestamp is missing.

    Mirrors ``observability.metrics_source._duration_ms``; duplicated here so
    this module carries **no** import edge into the observability package.
    """
    if not created_at or not completed_at:
        return None
    try:
        start = datetime.fromisoformat(str(created_at))
        end = datetime.fromisoformat(str(completed_at))
    except (TypeError, ValueError):
        return None
    return max(0.0, (end - start).total_seconds() * 1000.0)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (same convention as ``EvalSummary.p95_latency_ms``)."""
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    idx = int(len(ordered) * q)
    idx = min(max(idx, 0), len(ordered) - 1)
    return ordered[idx]


def _sample_metric_name(sample: Any) -> str:
    return str(_attr(sample, "metric_name", "") or "")


def _sample_value(sample: Any) -> float | None:
    value = _attr(sample, "metric_value", None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _plan_info(record: Any) -> dict[str, Any]:
    """The run's plan metadata (``RunRecord.llm["plan"]``) as a mapping."""
    llm = _attr(record, "llm", None) or {}
    plan = llm.get("plan") if isinstance(llm, dict) else None
    return plan if isinstance(plan, dict) else {}


def _loop_observations(record: Any) -> int:
    loop = _attr(record, "loop", None) or {}
    if not isinstance(loop, dict):
        return 0
    try:
        return int(loop.get("observations") or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Per-run reduction                                                           #
# --------------------------------------------------------------------------- #
def evaluate_run(record: Any) -> dict[str, Any]:
    """Reduce one run to the raw signals every metric aggregates over.

    Pure and side-effect free. Works on a ``RunRecord`` dataclass or an
    equivalent mapping, so it is trivially unit-testable without the runtime.

    The returned ``needs_replan`` is the loop-breaker signal: ``False`` for a
    run that never entered the replan loop (``observations == 0``), ``True``
    once it did (>0). ``recovered`` is ``needs_replan and status == "completed"``
    — the numerator of Failure Recovery Rate.
    """
    status = str(_attr(record, "status", "") or "")
    plan = _plan_info(record)
    steps = plan.get("steps") or []
    dropped = plan.get("dropped_tools") or []
    plan_source = str(_attr(record, "plan_source", "") or "") or str(
        plan.get("source") or ""
    )
    skills_raw = _attr(record, "skills_used", _UNSET)
    skills_instrumented = skills_raw is not _UNSET
    skills_count = len(skills_raw) if isinstance(skills_raw, (list, tuple)) else 0
    observations = _loop_observations(record)

    return {
        "run_id": _attr(record, "run_id", None),
        "status": status,
        "terminal": status in _TERMINAL_STATUSES,
        "completed": status == "completed",
        "duration_ms": _duration_ms(
            _attr(record, "created_at", None), _attr(record, "completed_at", None)
        ),
        "cost_usd": max(0.0, _as_float(_attr(record, "total_cost_usd", 0.0))),
        "tokens": int(_as_float(_attr(record, "total_tokens", 0))),
        "observations": observations,
        "needs_replan": observations > 0,
        "recovered": observations > 0 and status == "completed",
        "has_plan": bool(steps),
        "plan_source": plan_source,
        "dropped_tools": len(dropped) if isinstance(dropped, (list, tuple)) else 0,
        "planned_steps": len(steps) if isinstance(steps, (list, tuple)) else 0,
        "skills_count": skills_count,
        "skills_instrumented": skills_instrumented,
        "has_skill_signal": skills_count > 0,
    }


# --------------------------------------------------------------------------- #
# Aggregation                                                                 #
# --------------------------------------------------------------------------- #
def _unavailable(spec: MetricSpec, note: str | None = None) -> AgentEvalMetric:
    """An honest "no data" metric — ``value`` stays ``None``, never ``0``."""
    return AgentEvalMetric(
        value=None,
        unit=spec.unit,
        computability=spec.computability,
        has_data=False,
        formula=spec.formula,
        source=spec.source,
        note=note or spec.note,
    )


def _measured(spec: MetricSpec, value: float, note: str | None = None) -> AgentEvalMetric:
    return AgentEvalMetric(
        value=value,
        unit=spec.unit,
        computability=spec.computability,
        has_data=True,
        formula=spec.formula,
        source=spec.source,
        note=note or spec.note,
    )


def _sample_avg(samples: Sequence[Any], names: frozenset[str]) -> list[float]:
    """Values of every sample whose ``metric_name`` is in ``names``."""
    out: list[float] = []
    for sample in samples:
        if _sample_metric_name(sample) in names:
            value = _sample_value(sample)
            if value is not None:
                out.append(value)
    return out


def aggregate(
    records: Sequence[Any],
    samples: Sequence[Any] = (),
    *,
    source: str = "hub_runs",
    durable: bool = False,
    window_days: int = 30,
    judged_samples: int | None = None,
    generated_at: str | None = None,
) -> AgentEvalSnapshot:
    """Fold a cohort of run records (+ judge samples) into a snapshot.

    Every ``value`` is derived from real data; a metric with no data yields
    ``value=None`` / ``has_data=False``. ``computability`` comes straight from
    :data:`SPECS` so an ``ok`` metric is never reported as unavailable and a
    ``not_available`` metric never pretends to have a value.
    """
    signals = [evaluate_run(r) for r in records]
    total_runs = len(signals)
    terminal = [s for s in signals if s["terminal"]]
    completed = sum(1 for s in signals if s["completed"])
    durations = [s["duration_ms"] for s in signals if s["duration_ms"] is not None]
    costs = [s["cost_usd"] for s in signals]
    replanned = [s for s in signals if s["needs_replan"]]
    recovered = sum(1 for s in replanned if s["recovered"])
    skill_runs = sum(1 for s in signals if s["has_skill_signal"])
    skill_instrumented = any(s["skills_instrumented"] for s in signals)

    groundedness_values = _sample_avg(samples, _GROUNDEDNESS_ALIASES)
    hallucination_values = _sample_avg(samples, _HALLUCINATION_ALIASES)
    judged = len(samples) if judged_samples is None else int(judged_samples)

    def _q(name: str) -> AgentEvalMetric:
        spec = _SPEC_BY_NAME[name]
        if name == "task_success_rate":
            if not terminal:
                return _unavailable(spec)
            return _measured(spec, completed / len(terminal))
        if name == "tool_selection_accuracy":
            return _unavailable(spec)
        if name == "planning_accuracy":
            return _unavailable(spec)
        if name == "retrieval_recall":
            return _unavailable(spec)
        if name == "citation_accuracy":
            return _unavailable(spec)
        if name == "groundedness":
            if not groundedness_values:
                return _unavailable(
                    spec,
                    "judge 未接线/未落库（judged_samples=%d），暂无评分" % judged,
                )
            return _measured(spec, sum(groundedness_values) / len(groundedness_values))
        if name == "hallucination_rate":
            if not hallucination_values:
                return _unavailable(
                    spec,
                    "judge 未接线/未落库（judged_samples=%d）；确定性代理见 note" % judged,
                )
            return _measured(spec, sum(hallucination_values) / len(hallucination_values))
        if name == "memory_recall":
            return _unavailable(spec)
        if name == "skill_reuse_rate":
            if not total_runs or not skill_instrumented:
                return _unavailable(
                    spec,
                    "逐任务 skill 复用需 RunRecord.skills_used（或 context_build_stats.skill_refs）埋点",
                )
            return _measured(spec, skill_runs / total_runs)
        raise KeyError(name)  # pragma: no cover — guarded by SPECS

    def _c(name: str) -> AgentEvalMetric:
        spec = _SPEC_BY_NAME[name]
        if not total_runs:
            return _unavailable(spec)
        return _measured(spec, sum(costs) / total_runs)

    def _r(name: str) -> AgentEvalMetric:
        spec = _SPEC_BY_NAME[name]
        if name == "latency_p50_ms":
            value = _percentile(durations, 0.50)
            return _unavailable(spec) if value is None else _measured(spec, value)
        if name == "latency_p95_ms":
            value = _percentile(durations, 0.95)
            return _unavailable(spec) if value is None else _measured(spec, value)
        if name == "failure_recovery_rate":
            if not replanned:
                return _unavailable(spec)
            return _measured(spec, recovered / len(replanned))
        raise KeyError(name)  # pragma: no cover

    quality = {
        spec.name: (_q(spec.name) if spec.dimension == "quality" else None)
        for spec in SPECS
        if spec.dimension == "quality"
    }
    cost = {
        spec.name: (_c(spec.name) if spec.dimension == "cost" else None)
        for spec in SPECS
        if spec.dimension == "cost"
    }
    reliability = {
        spec.name: (_r(spec.name) if spec.dimension == "reliability" else None)
        for spec in SPECS
        if spec.dimension == "reliability"
    }

    return AgentEvalSnapshot(
        source=source,
        durable=durable,
        window_days=int(window_days),
        sample_runs=total_runs,
        judged_samples=judged,
        quality=quality,
        cost=cost,
        reliability=reliability,
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
    )
