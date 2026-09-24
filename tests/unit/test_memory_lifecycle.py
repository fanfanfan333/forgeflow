"""INC9 B2 — memory lifecycle pure functions (score / decay / archive).

Covers docs/sop/12-INC9-DESIGN.md §10 (unit, pure functions): score monotonicity,
freshness decay shape, the "min_score=0 ⇒ never archive" default, and
``is_archived`` duck-typing.
"""

from __future__ import annotations

from forgeflow.experience.lifecycle import (
    MemoryHealth,
    compute_score,
    freshness,
    is_archived,
    should_archive,
)

# --------------------------------------------------------------------------- #
# freshness                                                                    #
# --------------------------------------------------------------------------- #

def test_freshness_is_one_for_a_brand_new_entry():
    assert freshness(0.0, half_life_days=30.0) == 1.0


def test_freshness_halves_every_half_life():
    assert freshness(30.0, half_life_days=30.0) == 0.5
    assert freshness(60.0, half_life_days=30.0) == 0.25


def test_freshness_is_monotonic_decreasing():
    values = [freshness(a, half_life_days=30.0) for a in range(0, 200, 10)]
    assert all(b <= a for a, b in zip(values, values[1:]))


def test_freshness_negative_age_is_treated_as_new():
    assert freshness(-5.0, half_life_days=30.0) == 1.0


def test_freshness_non_positive_half_life_degrades_safely():
    assert freshness(0.0, half_life_days=0.0) == 1.0
    assert freshness(1.0, half_life_days=0.0) == 0.0


# --------------------------------------------------------------------------- #
# compute_score                                                                #
# --------------------------------------------------------------------------- #

def test_score_is_in_unit_range():
    for reuse in (0, 5, 100):
        for age in (0.0, 30.0, 365.0):
            score = compute_score(reuse_count=reuse, age_days=age, promoted=False)
            assert 0.0 <= score <= 1.0


def test_score_increases_with_reuse():
    low = compute_score(reuse_count=0, age_days=0.0, promoted=False)
    high = compute_score(reuse_count=5, age_days=0.0, promoted=False)
    assert high > low


def test_score_decreases_with_age():
    fresh = compute_score(reuse_count=0, age_days=0.0, promoted=False)
    stale = compute_score(reuse_count=0, age_days=120.0, promoted=False)
    assert stale < fresh


def test_score_rewards_promoted():
    plain = compute_score(reuse_count=0, age_days=0.0, promoted=False)
    promoted = compute_score(reuse_count=0, age_days=0.0, promoted=True)
    assert promoted > plain


def test_reuse_is_capped():
    at_cap = compute_score(reuse_count=10, age_days=0.0, promoted=False)
    beyond = compute_score(reuse_count=1000, age_days=0.0, promoted=False)
    assert at_cap == beyond


def test_components_are_explainable():
    score = compute_score(reuse_count=10, age_days=0.0, promoted=True)
    # 0.5*1 (reuse full) + 0.3*1 (fresh) + 0.2*1 (promoted) == 1.0
    assert score == 1.0


def test_default_weights_sum_to_one():
    # The default weighting is a convex blend, so a fully-partitioned entry is 1.0.
    full = compute_score(reuse_count=10, age_days=0.0, promoted=True)
    assert abs(full - 1.0) < 1e-9


# --------------------------------------------------------------------------- #
# should_archive                                                               #
# --------------------------------------------------------------------------- #

def _health(score: float, age_days: float = 0.0) -> MemoryHealth:
    return MemoryHealth(
        score=score,
        reuse_count=0,
        age_days=age_days,
        promoted=False,
        archived=False,
        components={},
    )


def test_min_score_zero_never_archives():
    # The default ⇒ existing behaviour is unchanged, no matter how low the score.
    assert should_archive(_health(0.0), min_score=0.0) is False
    assert should_archive(_health(0.0), min_score=0.0, max_age_days=0.0) is False


def test_archives_below_min_score():
    assert should_archive(_health(0.10), min_score=0.20) is True
    assert should_archive(_health(0.30), min_score=0.20) is False


def test_archives_beyond_max_age():
    assert should_archive(_health(0.9, age_days=400.0), min_score=0.0, max_age_days=365.0) is True
    assert should_archive(_health(0.9, age_days=10.0), min_score=0.0, max_age_days=365.0) is False


# --------------------------------------------------------------------------- #
# is_archived duck-typing                                                      #
# --------------------------------------------------------------------------- #

class _Entry:
    def __init__(self, archived=None, metadata=None):
        if archived is not None:
            self.archived = archived
        self.metadata = metadata or {}


def test_is_archived_reads_the_field():
    assert is_archived(_Entry(archived=True)) is True
    assert is_archived(_Entry(archived=False)) is False


def test_is_archived_falls_back_to_metadata():
    assert is_archived(_Entry(metadata={"archived": True})) is True
    assert is_archived(_Entry(metadata={})) is False


def test_is_archived_handles_mappings():
    assert is_archived({"archived": True}) is True
    assert is_archived({"metadata": {"archived": True}}) is True
    assert is_archived({}) is False
