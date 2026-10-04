"""Privacy hub — PII scrubbing + scrub audit (INC46 T32).

Public surface:

  * :mod:`forgeflow.privacy.scrubber` — the configurable PII scrubber
    (categories / strategies ``redact`` | ``hash`` | ``drop``), the write-before-
    scrub helper and the miner admission gate;
  * :mod:`forgeflow.privacy.audit` — the minimised scrub audit trail
    (``scrub_audits``, migration ``026``).

Importing this package never opens a database connection (backends are resolved
lazily).
"""

from __future__ import annotations

from forgeflow.privacy.audit import (
    InMemoryScrubAuditStore,
    PostgresScrubAuditStore,
    ScrubAudit,
    ScrubAuditStore,
    get_scrub_audit_store,
    reset_scrub_audit_store,
    set_scrub_audit_store,
)
from forgeflow.privacy.scrubber import (
    DEFAULT_CATEGORIES,
    DEFAULT_RULES,
    SCRUB_STATUS_REFUSED,
    SCRUB_STATUS_SCRUBBED,
    SCRUB_VERSION,
    STRATEGIES,
    STRATEGY_DROP,
    STRATEGY_HASH,
    STRATEGY_REDACT,
    ScrubAvailabilityError,
    Scrubber,
    ScrubError,
    ScrubMatch,
    ScrubOutcome,
    ScrubResult,
    ScrubRule,
    check_availability,
    filter_miner_eligible,
    get_scrubber,
    miner_eligible,
    mine_eligible_experiences,
    reference_tokens,
    reset_scrubber,
    scrub_experience_record,
    set_scrubber,
)

__all__ = [
    # scrubber
    "SCRUB_VERSION",
    "STRATEGY_REDACT",
    "STRATEGY_HASH",
    "STRATEGY_DROP",
    "STRATEGIES",
    "SCRUB_STATUS_SCRUBBED",
    "SCRUB_STATUS_REFUSED",
    "DEFAULT_CATEGORIES",
    "DEFAULT_RULES",
    "ScrubRule",
    "ScrubMatch",
    "ScrubResult",
    "ScrubOutcome",
    "ScrubError",
    "ScrubAvailabilityError",
    "Scrubber",
    "get_scrubber",
    "set_scrubber",
    "reset_scrubber",
    "check_availability",
    "reference_tokens",
    "scrub_experience_record",
    "miner_eligible",
    "filter_miner_eligible",
    "mine_eligible_experiences",
    # audit
    "ScrubAudit",
    "ScrubAuditStore",
    "InMemoryScrubAuditStore",
    "PostgresScrubAuditStore",
    "get_scrub_audit_store",
    "set_scrub_audit_store",
    "reset_scrub_audit_store",
]
