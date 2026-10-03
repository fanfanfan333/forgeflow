"""INC46 T08 — interlock anchor (T15 requirement **R1**).

The publish interlock (``forgeflow.skills.publish_interlock``) discovers a landed
capability by importing the *anchor module* ``forgeflow.skills.candidate_gate``
and calling its ``INTERLOCK_PROBE()`` (fail-closed forward contract). T08's
primary implementation lives in :mod:`forgeflow.skills.candidate_gates` (the
task-book file name, plural); this module is the thin, additive anchor the
interlock actually probes, so requirement **R1** ("Candidate Gate / Critic
增强生效，DANGEROUS 候选阻断自动发布") flips to *met* the moment T08 lands — no
edit to ``publish_interlock`` required.

It re-exports the public surface so ``candidate_gate`` and ``candidate_gates``
are interchangeable for callers.
"""

from __future__ import annotations

# Re-export the full public surface of the primary module.
from forgeflow.skills.candidate_gates import (  # noqa: F401
    CONFLICT_CODES,
    GATE_BLOCKING_CODES,
    INTERLOCK_PROBE,
    CandidateGateResult,
    blocks_auto_publish,
    detect_conflicts,
    evaluate_candidate_gate,
)

__all__ = [
    "GATE_BLOCKING_CODES",
    "CONFLICT_CODES",
    "CandidateGateResult",
    "evaluate_candidate_gate",
    "blocks_auto_publish",
    "detect_conflicts",
    "INTERLOCK_PROBE",
]
