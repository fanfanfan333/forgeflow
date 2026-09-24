"""INC8 Phase-B / T03 — the offline judge sampler (``judge_metrics``).

Pins the design's §5 / O4 contract: judge sampling is **explicit, offline,
batched and cached**, writes to ``agent_eval_samples``, and is a **no-op** by
default in the offline (``mock``) profile — so ``judged_samples`` stays honest.

No real LLM is ever called: an injected fake judge supplies ``JudgeScore``s.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from forgeflow.evaluation.judge import JudgeScore
from forgeflow.evaluation.judge_metrics import (
    GROUNDEDNESS_METRIC,
    HALLUCINATION_METRIC,
    JudgeSampler,
    clear_judge_cache,
)
from forgeflow.repositories.eval_sample_repo import EvalSample, clear_eval_sample_store
from forgeflow.runtime.orchestrator import RunRecord

pytestmark = pytest.mark.asyncio

_UTC = _dt.timezone.utc
_BASE = _dt.datetime(2026, 9, 24, 9, 0, 0, tzinfo=_UTC)


class _FakeJudge:
    """A deterministic stand-in for ``LLMJudge`` (no network)."""

    def __init__(self, scores: list[JudgeScore]) -> None:
        self._scores = list(scores)
        self.calls = 0

    async def evaluate(self, input: str, context: str, output: str) -> JudgeScore:  # noqa: A002
        score = self._scores[self.calls]
        self.calls += 1
        return score


class _RecordingRepo:
    """In-memory ``EvalSampleRepository`` stand-in that counts writes."""

    def __init__(self) -> None:
        self.saved: list[EvalSample] = []

    async def save_sample(self, tenant_id, sample: EvalSample) -> EvalSample:
        sample.tenant_id = tenant_id
        self.saved.append(sample)
        return sample

    async def list_samples(self, tenant_id, *, metric=None, limit: int = 100):
        rows = [s for s in self.saved if metric is None or s.metric_name == metric]
        return rows[:limit]

    async def aggregate(self, tenant_id, metric: str):
        rows = [s for s in self.saved if s.metric_name == metric]
        if not rows:
            return {"count": 0, "avg": None, "min": None, "max": None, "has_data": False}
        vals = [s.metric_value for s in rows]
        return {
            "count": len(vals),
            "avg": sum(vals) / len(vals),
            "min": min(vals),
            "max": max(vals),
            "has_data": True,
        }


def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        thread_id=f"t-{run_id}",
        tenant_id="t-judge",
        agent_id=None,
        intent="summarise the sales report",
        status="completed",
        outcome="ok",
        steps=[{"tool": "research.search", "status": "ok"}],
        errors=[],
        created_at=_BASE.isoformat(),
        completed_at=_BASE.isoformat(),
        llm={"plan": {"source": "llm", "reasoning": "grounded in the retrieved docs"}},
    )


@pytest.fixture(autouse=True)
def _clean(force_memory_backend):
    clear_judge_cache()
    clear_eval_sample_store()
    yield
    clear_judge_cache()
    clear_eval_sample_store()


def _scores(n: int, *, faithful: float = 0.8, halluc: bool = False) -> list[JudgeScore]:
    return [
        JudgeScore(
            faithfulness=faithful, relevance=0.9, coherence=0.9, hallucination_flag=halluc
        )
        for _ in range(n)
    ]


async def test_offline_default_is_a_noop():
    """No injected judge + mock provider ⇒ nothing scored, nothing written."""
    repo = _RecordingRepo()
    result = await JudgeSampler(repo=repo).sample(
        "t-judge", [_record("r1"), _record("r2")]
    )
    assert result.evaluated == 0
    assert result.written == 0
    assert result.skipped == 2
    assert result.reason == "disabled"
    assert repo.saved == []


async def test_injected_judge_scores_and_persists_two_metrics_per_run():
    repo = _RecordingRepo()
    judge = _FakeJudge(_scores(2, faithful=0.75, halluc=True))
    result = await JudgeSampler(judge, repo=repo).sample(
        "t-judge", [_record("r1"), _record("r2")], dataset="sales-eval-20"
    )
    assert result.evaluated == 2
    assert result.written == 4
    assert judge.calls == 2
    names = [s.metric_name for s in repo.saved]
    assert names.count(GROUNDEDNESS_METRIC) == 2
    assert names.count(HALLUCINATION_METRIC) == 2
    # groundedness carries faithfulness; hallucination carries 1.0 when flagged.
    grd = repo.saved[0]
    assert grd.metric_value == 0.75
    assert grd.source == "judge"
    assert grd.dimension == "quality"
    assert grd.dataset == "sales-eval-20"
    hal = next(s for s in repo.saved if s.metric_name == HALLUCINATION_METRIC)
    assert hal.metric_value == 1.0


async def test_second_call_is_served_from_cache():
    repo = _RecordingRepo()
    judge = _FakeJudge(_scores(1, faithful=0.6))
    sampler = JudgeSampler(judge, repo=repo)
    first = await sampler.sample("t-judge", [_record("r1")])
    assert first.evaluated == 1
    second = await sampler.sample("t-judge", [_record("r1")])
    assert second.evaluated == 0
    assert second.cached == 1
    assert second.written == 0
    assert judge.calls == 1  # never re-scored


async def test_batch_cap_limits_how_many_are_scored():
    repo = _RecordingRepo()
    judge = _FakeJudge(_scores(10, faithful=0.5))
    result = await JudgeSampler(judge, repo=repo, max_batch=2).sample(
        "t-judge", [_record(f"r{i}") for i in range(10)]
    )
    assert result.evaluated == 2
    assert judge.calls == 2


async def test_enabled_false_forces_a_noop_even_with_a_judge():
    repo = _RecordingRepo()
    judge = _FakeJudge(_scores(3, faithful=0.5))
    result = await JudgeSampler(judge, repo=repo, enabled=False).sample(
        "t-judge", [_record("r1")]
    )
    assert result.evaluated == 0
    assert result.reason == "disabled"
    assert judge.calls == 0


async def test_a_failing_record_is_skipped_not_fatal():
    class _FlakyJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, *args):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("judge exploded")
            return JudgeScore(
                faithfulness=0.9, relevance=0.9, coherence=0.9, hallucination_flag=False
            )

    repo = _RecordingRepo()
    result = await JudgeSampler(_FlakyJudge(), repo=repo).sample(
        "t-judge", [_record("r1"), _record("r2")]
    )
    assert result.skipped == 1
    assert result.evaluated == 1
    assert result.written == 2


async def test_samples_feed_the_read_surface():
    """The persisted samples aggregate into the groundedness metric."""
    from forgeflow.repositories.factory import get_eval_sample_repository

    await get_eval_sample_repository().save_sample(
        "t-judge", EvalSample(metric_name=GROUNDEDNESS_METRIC, metric_value=0.55)
    )
    agg = await get_eval_sample_repository().aggregate("t-judge", GROUNDEDNESS_METRIC)
    assert agg["has_data"] is True
    assert agg["avg"] == pytest.approx(0.55)
