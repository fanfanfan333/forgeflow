"""ForgeFlow QA Code Agent — Phase A–C.

The closed loop the orchestrator runs is Discover → Plan → Execute → Observe →
Analyze → Classify → Report. Phase A–C implements its *foundation*: what
capabilities exist, how to test them, and what really happened when the tests
ran — always leaving non-forgeable evidence (a real subprocess run, pinned to a
unique ``run_id``).

Public surface note
-------------------
The authoritative type definitions live in :mod:`forgeflow.qa.models`. Only a
small, stable subset is re-exported here for convenience; import everything else
from ``forgeflow.qa.models`` directly. The CLI entry point is
``python -m forgeflow.qa`` (see :mod:`forgeflow.qa.__main__`).
"""

from __future__ import annotations

from forgeflow.qa.models import (
    Capability,
    CapabilityInventory,
    Evidence,
    EvidenceStatus,
    FailureClass,
    FailureFingerprint,
    RegressionReport,
    RegressionScope,
    RootCauseCandidate,
    TestPlanItem,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "Capability",
    "CapabilityInventory",
    "Evidence",
    "EvidenceStatus",
    "FailureClass",
    "FailureFingerprint",
    "RegressionReport",
    "RegressionScope",
    "RootCauseCandidate",
    "TestPlanItem",
    "Verdict",
]
