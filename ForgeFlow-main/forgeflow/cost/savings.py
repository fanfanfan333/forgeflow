"""Cost savings vs. the previous period (INC2 A1, architecture §7.1).

Formula::

    baseline = previous_period_cost * multiplier
    saved    = Σ max(0, baseline_i - actual_i)

``multiplier`` comes from ``Settings.cost_savings_baseline_multiplier`` so the
baseline can be tuned without a code change.

⚠️ **Honesty rule (§7.1, hard requirement):** when there is no previous-period
baseline, every derived field is ``None`` so the API/SPA renders ``—``. It must
**not** return ``0`` and must **not** substitute the current period's cost —
"0 saved" is a claim we cannot make without a baseline to compare against.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from forgeflow.cost.models import SavingsResult
from forgeflow.config import get_settings

__all__ = ["compute_savings", "savings_from_totals"]


def compute_savings(
    pairs: Iterable[tuple[float | None, float]],
    *,
    multiplier: float | None = None,
) -> SavingsResult:
    """Savings across ``(previous_period_cost, current_cost)`` pairs.

    Pairs whose ``previous`` is ``None`` contribute to ``actual`` only — they
    have no baseline to compare against. If **no** pair supplies a baseline the
    result is all-``None`` (``has_baseline=False``).
    """
    if multiplier is None:
        multiplier = float(get_settings().cost_savings_baseline_multiplier)
    multiplier = float(multiplier)

    actual_total = 0.0
    baseline_total: float | None = None
    saved_total = 0.0

    for previous, current in pairs:
        actual_total += float(current or 0.0)
        if previous is None:
            continue
        baseline = float(previous) * multiplier
        baseline_total = (baseline_total or 0.0) + baseline
        saved_total += max(0.0, baseline - float(current or 0.0))

    if baseline_total is None:
        # No baseline data at all — report "—", never 0.
        return SavingsResult(
            amount=None,
            baseline=None,
            actual=actual_total,
            delta_pct=None,
            has_baseline=False,
        )

    delta_pct = (saved_total / baseline_total) if baseline_total > 0 else None
    return SavingsResult(
        amount=saved_total,
        baseline=baseline_total,
        actual=actual_total,
        delta_pct=delta_pct,
        has_baseline=True,
    )


def savings_from_totals(
    previous_total: float | None,
    current_total: float | None = 0.0,
    *,
    multiplier: float | None = None,
) -> SavingsResult:
    """Convenience wrapper for the single-total case (one window vs. the last)."""
    return compute_savings([(previous_total, float(current_total or 0.0))], multiplier=multiplier)


def savings_from_series(
    previous: Sequence[float | None],
    current: Sequence[float],
    *,
    multiplier: float | None = None,
) -> SavingsResult:
    """Zip two like-for-like series (same buckets) into :func:`compute_savings`."""
    pairs: list[tuple[float | None, float]] = []
    for index, value in enumerate(current):
        prior = previous[index] if index < len(previous) else None
        pairs.append((prior, float(value or 0.0)))
    return compute_savings(pairs, multiplier=multiplier)
