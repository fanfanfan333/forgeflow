"""INC46 T16 — Outcome signals: what "success" means, stated in code.

Before T16 the Pattern Miner called a run "success" whenever no step hard-failed
(``skills/pattern_miner.py::derive_run_outcome``) — i.e. *the process finished*
was silently treated as *the user was satisfied*. That is exactly the gap the
task book records as 「成功」无定义 (§7-T02 → T16).

This package defines the label vocabulary, the success-rate arithmetic and the
**Miner admission filter** in one auditable place.
"""

from forgeflow.outcomes.signals import (
    ACCEPTED_EXPLICIT,
    ACCEPTED_IMPLICIT,
    ABANDONED,
    FAILED_SYSTEM,
    IMPLICIT_WINDOW_HOURS,
    MINER_NEGATIVE,
    MINER_POSITIVE,
    OUTCOME_LABELS,
    REJECTED,
    REVISED,
    REVERTED,
    UNKNOWN,
    counts_toward_rate,
    is_miner_negative,
    is_miner_positive,
    label_distribution,
    success_rate,
)

__all__ = [
    "ACCEPTED_EXPLICIT",
    "ACCEPTED_IMPLICIT",
    "REJECTED",
    "REVISED",
    "REVERTED",
    "ABANDONED",
    "FAILED_SYSTEM",
    "UNKNOWN",
    "OUTCOME_LABELS",
    "MINER_POSITIVE",
    "MINER_NEGATIVE",
    "IMPLICIT_WINDOW_HOURS",
    "counts_toward_rate",
    "is_miner_positive",
    "is_miner_negative",
    "success_rate",
    "label_distribution",
]
