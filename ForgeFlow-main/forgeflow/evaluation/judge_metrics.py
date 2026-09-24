"""INC8 Phase-B — offline judge sampling for Groundedness / Hallucination (#6/#7).

This is the design's ``N3`` (docs/sop/11-INC8-AGENT-EVALUATION-DESIGN.md §4.1 / §5).
It reuses the existing :mod:`forgeflow.evaluation.judge` — the model is injectable
(INC8-N3) — but runs it **only** as an explicit, offline, sampled, batched,
cached pass:

  * **explicit trigger, never the run loop.** The design's hard constraint is the
    judge's latency (~18.6 s/real Ollama call): a run must never pay it. This
    module is called out-of-band; nothing here is imported by the runtime hot path.
  * **offline default = no judge.** When no judge is injected *and* the configured
    LLM provider is the ``mock`` (the offline profile), :meth:`JudgeSampler.sample`
    is a no-op — ``judged_samples`` stays ``0``, honestly, instead of being filled
    with fabricated scores.
  * **batch + cache.** At most ``max_batch`` records are scored per call; a
    ``(tenant, run_id, dataset)`` cache means a re-sample never re-pays for an
    unchanged run.
  * **persisted.** Each score is written to ``agent_eval_samples`` (migration
    ``012``) via the house ``EvalSampleRepository`` so the read surface
    (``/metrics/agent-eval``) can aggregate it without touching the LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from forgeflow.repositories.eval_sample_repo import EvalSample

logger = logging.getLogger(__name__)

__all__ = [
    "JudgeSampler",
    "JudgeSamplingResult",
    "clear_judge_cache",
]

#: Persisted ``metric_name`` for the two judge-derived metrics.
GROUNDEDNESS_METRIC = "groundedness"
HALLUCINATION_METRIC = "hallucination"

#: Default per-call batch ceiling (design §5: 批量 ≤N).
DEFAULT_MAX_BATCH = 20

#: Result cache: ``(tenant_id, run_id, dataset) -> (faithfulness, hallucination)``.
_JUDGE_CACHE: dict[tuple[str | None, str | None, str | None], tuple[float, float]] = {}


def clear_judge_cache() -> None:
    """Drop the in-process judge cache (tests + a manual "force re-score")."""
    _JUDGE_CACHE.clear()


@dataclass
class JudgeSamplingResult:
    """Outcome of one :meth:`JudgeSampler.sample` call (the offline audit trail)."""

    evaluated: int = 0
    written: int = 0
    cached: int = 0
    skipped: int = 0
    dataset: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluated": self.evaluated,
            "written": self.written,
            "cached": self.cached,
            "skipped": self.skipped,
            "dataset": self.dataset,
            "reason": self.reason,
        }


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _judge_inputs(record: Any) -> tuple[str, str, str]:
    """Derive ``(input, context, output)`` strings for the judge from a record."""
    intent = str(_attr(record, "intent", "") or "")
    llm = _attr(record, "llm", None) or {}
    plan = llm.get("plan") if isinstance(llm, dict) else None
    context = ""
    if isinstance(plan, dict):
        context = str(plan.get("reasoning") or "")
    steps = _attr(record, "steps", None) or []
    try:
        output = "; ".join(
            f"{s.get('tool', '?')}[{s.get('status', '?')}]"
            for s in steps
            if isinstance(s, dict)
        )
    except (TypeError, AttributeError):
        output = ""
    if not output:
        output = str(_attr(record, "outcome", "") or "")
    return intent, context, output


class JudgeSampler:
    """Offline, batched, cached judge sampling that persists to ``agent_eval_samples``.

    Args:
        judge: an object exposing ``async evaluate(input, context, output) ->
            JudgeScore``. When ``None`` a default :class:`LLMJudge` is built
            **only if** sampling is enabled (see ``enabled``).
        repo: the ``EvalSampleRepository`` to write to. Defaults to the active
            backend's repository via ``get_eval_sample_repository()``.
        max_batch: hard cap on records scored per call.
        enabled: force-on / force-off. ``None`` (default) enables sampling only
            when a judge is injected or the configured provider is not ``mock``.
    """

    def __init__(
        self,
        judge: Any | None = None,
        *,
        repo: Any | None = None,
        max_batch: int = DEFAULT_MAX_BATCH,
        enabled: bool | None = None,
    ) -> None:
        self._judge = judge
        self._repo = repo
        self._max_batch = max(1, int(max_batch))
        self._enabled = enabled

    # -- internals --------------------------------------------------------- #
    def _resolve_enabled(self) -> bool:
        if self._enabled is not None:
            return bool(self._enabled)
        if self._judge is not None:
            return True
        from forgeflow.config import get_settings

        provider = str(getattr(get_settings(), "llm_provider", "") or "").strip().lower()
        return provider not in ("", "mock")

    def _resolve_judge(self) -> Any | None:
        if self._judge is not None:
            return self._judge
        from forgeflow.evaluation.judge import LLMJudge

        try:
            return LLMJudge()
        except Exception as exc:  # noqa: BLE001 — a judge build failure must not raise
            logger.warning("judge sampler: could not build a judge: %s", exc)
            return None

    def _resolve_repo(self) -> Any:
        if self._repo is not None:
            return self._repo
        from forgeflow.repositories.factory import get_eval_sample_repository

        return get_eval_sample_repository()

    # -- public API -------------------------------------------------------- #
    async def sample(
        self,
        tenant_id: str | None,
        records: Sequence[Any],
        *,
        dataset: str | None = None,
        n: int | None = None,
    ) -> JudgeSamplingResult:
        """Score up to ``n`` (≤ ``max_batch``) records and persist the results.

        Offline / disabled → a no-op result with ``reason="disabled"`` and nothing
        written; the read surface then honestly reports ``judged_samples=0``.
        """
        if not self._resolve_enabled():
            return JudgeSamplingResult(
                dataset=dataset, skipped=len(records), reason="disabled"
            )

        judge = self._resolve_judge()
        if judge is None:
            return JudgeSamplingResult(
                dataset=dataset, skipped=len(records), reason="no_judge"
            )

        repo = self._resolve_repo()
        cap = self._max_batch if n is None else min(int(n), self._max_batch)
        result = JudgeSamplingResult(dataset=dataset)

        for record in list(records)[:cap]:
            run_id = _attr(record, "run_id", None)
            key = (tenant_id, run_id, dataset)
            if key in _JUDGE_CACHE:
                result.cached += 1
                continue

            try:
                score = await judge.evaluate(*_judge_inputs(record))
                faithfulness = float(getattr(score, "faithfulness", 0.0) or 0.0)
                hallucination = 1.0 if getattr(score, "hallucination_flag", False) else 0.0
            except Exception as exc:  # noqa: BLE001 — one bad record must not abort the batch
                logger.warning("judge sampler: run %s scoring failed: %s", run_id, exc)
                result.skipped += 1
                continue

            await repo.save_sample(
                tenant_id,
                EvalSample(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    metric_name=GROUNDEDNESS_METRIC,
                    metric_value=faithfulness,
                    metric_unit="score",
                    dimension="quality",
                    source="judge",
                    dataset=dataset,
                    meta={"run_id": run_id},
                ),
            )
            await repo.save_sample(
                tenant_id,
                EvalSample(
                    tenant_id=tenant_id,
                    run_id=run_id,
                    metric_name=HALLUCINATION_METRIC,
                    metric_value=hallucination,
                    metric_unit="ratio",
                    dimension="quality",
                    source="judge",
                    dataset=dataset,
                    meta={"run_id": run_id},
                ),
            )
            _JUDGE_CACHE[key] = (faithfulness, hallucination)
            result.evaluated += 1
            result.written += 2

        return result
