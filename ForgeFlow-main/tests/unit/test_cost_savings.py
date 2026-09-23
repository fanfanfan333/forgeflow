"""INC2-05 — savings vs. the previous period (architecture §7.1).

The frozen rule: with no previous-period baseline the API must return ``null``
(SPA renders ``—``). It may never return ``0``, and may never substitute the
current period's cost as its own baseline.
"""

from __future__ import annotations

import pytest

from forgeflow.cost.savings import (
    compute_savings,
    savings_from_series,
    savings_from_totals,
)


class TestSavingsWithBaseline:
    def test_saved_is_baseline_minus_actual(self):
        result = savings_from_totals(100.0, 60.0)

        assert result.has_baseline is True
        assert result.baseline == pytest.approx(100.0)
        assert result.actual == pytest.approx(60.0)
        assert result.amount == pytest.approx(40.0)
        assert result.delta_pct == pytest.approx(0.4)

    def test_multiplier_scales_the_baseline(self):
        result = savings_from_totals(100.0, 60.0, multiplier=1.5)

        assert result.baseline == pytest.approx(150.0)
        assert result.amount == pytest.approx(90.0)
        assert result.delta_pct == pytest.approx(0.6)

    def test_overspend_is_clamped_to_zero_not_negative(self):
        result = savings_from_totals(100.0, 150.0)

        assert result.amount == pytest.approx(0.0)
        assert result.has_baseline is True

    def test_series_sums_per_bucket(self):
        # bucket 1 saves 40, bucket 2 overspends (clamped to 0).
        result = compute_savings([(100.0, 60.0), (50.0, 80.0)])

        assert result.amount == pytest.approx(40.0)
        assert result.baseline == pytest.approx(150.0)
        assert result.actual == pytest.approx(140.0)

    def test_series_from_two_sequences(self):
        result = savings_from_series([100.0, 200.0], [80.0, 150.0])

        assert result.amount == pytest.approx(70.0)
        assert result.baseline == pytest.approx(300.0)


class TestNoBaseline:
    def test_missing_baseline_returns_null_not_zero(self):
        result = savings_from_totals(None, 60.0)

        assert result.has_baseline is False
        assert result.amount is None
        assert result.baseline is None
        assert result.delta_pct is None
        # The current cost is still reported — it is the one thing we know.
        assert result.actual == pytest.approx(60.0)

    def test_empty_window_returns_null(self):
        result = compute_savings([])

        assert result.has_baseline is False
        assert result.amount is None
        assert result.actual == pytest.approx(0.0)

    def test_partial_baseline_only_counts_pairs_that_have_one(self):
        result = compute_savings([(None, 30.0), (100.0, 60.0)])

        assert result.has_baseline is True
        assert result.baseline == pytest.approx(100.0)
        assert result.amount == pytest.approx(40.0)
        assert result.actual == pytest.approx(90.0)
