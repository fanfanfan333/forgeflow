"""Eval-sample repository — persistence for ``agent_eval_samples`` (INC8 §3.2/§4.1-N4).

House pattern (``repositories/base.py``): a Protocol both backends implement,
``tenant_id`` as the first positional argument so row-level isolation can never
be forgotten, and an in-process dict implementation for the offline profile.

The PG implementation lives in ``repositories/postgres/eval_sample_repo.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from forgeflow.repositories.base import TenantScopedRepository, new_id, utcnow

__all__ = [
    "EvalSample",
    "EvalSampleRepository",
    "MemoryEvalSampleRepo",
    "aggregate_samples",
    "clear_eval_sample_store",
]

#: The three metric dimensions (design §2).
DIMENSIONS: tuple[str, ...] = ("quality", "cost", "reliability")


@dataclass
class EvalSample:
    """One persisted eval measurement (mirrors an ``agent_eval_samples`` row)."""

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    run_id: str | None = None
    metric_name: str = ""
    metric_value: float = 0.0
    metric_unit: str = ""
    dimension: str = "quality"
    source: str = "judge"
    dataset: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "metric_name": self.metric_name,
            "metric_value": self.metric_value,
            "metric_unit": self.metric_unit,
            "dimension": self.dimension,
            "source": self.source,
            "dataset": self.dataset,
            "meta": dict(self.meta),
            "created_at": self.created_at.isoformat(),
        }


def aggregate_samples(rows: list[EvalSample]) -> dict[str, Any]:
    """Aggregate a set of samples into ``{count, avg, min, max, has_data}``.

    ``has_data`` is ``False`` (and every statistic ``None``) on an empty cohort —
    never a fabricated ``0.0`` that would masquerade as a real score.
    """
    n = len(rows)
    if n == 0:
        return {"count": 0, "avg": None, "min": None, "max": None, "has_data": False}
    values = [float(r.metric_value) for r in rows]
    return {
        "count": n,
        "avg": sum(values) / n,
        "min": min(values),
        "max": max(values),
        "has_data": True,
    }


@runtime_checkable
class EvalSampleRepository(Protocol):
    """Persistence for judge + deterministic-proxy eval samples."""

    async def save_sample(
        self, tenant_id: str | None, sample: EvalSample
    ) -> EvalSample:
        """Insert one sample. Returns it back."""
        ...

    async def list_samples(
        self,
        tenant_id: str | None,
        *,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[EvalSample]:
        """List samples (newest first), optionally filtered to one metric."""
        ...

    async def aggregate(
        self, tenant_id: str | None, metric: str
    ) -> dict[str, Any]:
        """``{count, avg, min, max, has_data}`` for one metric."""
        ...


_SAMPLES: dict[str, list[EvalSample]] = {}
_LOCK = asyncio.Lock()


def clear_eval_sample_store() -> None:
    """Reset all in-memory eval samples. Test helper only."""
    _SAMPLES.clear()


class MemoryEvalSampleRepo(TenantScopedRepository):
    """Dict-backed ``EvalSampleRepository`` (offline profile)."""

    async def save_sample(
        self, tenant_id: str | None, sample: EvalSample
    ) -> EvalSample:
        if sample.tenant_id is None:
            sample.tenant_id = tenant_id
        key = self.scope_key(tenant_id)
        async with _LOCK:
            _SAMPLES.setdefault(key, []).append(sample)
        return sample

    async def list_samples(
        self,
        tenant_id: str | None,
        *,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[EvalSample]:
        rows = list(_SAMPLES.get(self.scope_key(tenant_id), []))
        if metric:
            rows = [r for r in rows if r.metric_name == metric]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    async def aggregate(
        self, tenant_id: str | None, metric: str
    ) -> dict[str, Any]:
        rows = [
            r
            for r in _SAMPLES.get(self.scope_key(tenant_id), [])
            if r.metric_name == metric
        ]
        return aggregate_samples(rows)
