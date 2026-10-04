"""INC46 T32 — scrub **backfill** job (既有经验幂等重刷).

Re-scrubs every experience of one tenant with the current scrubber version and
stamps ``scrub_status`` / ``scrub_version``. The job is:

* **tenant-scoped & fail-closed** (红线 5) — a falsy tenant scans nothing, and a
  tenant's run can never touch another tenant's rows (the repository is
  tenant-fenced);
* **idempotent** — a second run over already-scrubbed, unchanged rows reports
  ``changed == 0``;
* **effective when it should be** — a legacy row (``scrub_status`` NULL) gets
  both ``scrub_status`` and ``scrub_version`` set; re-running with a bumped
  ``scrub_version`` is a real change (which is what makes the new-version
  pattern miner run over it);
* **no residual PII** — after scrubbing, a detection pass must find nothing;
* **audited** — one minimised :class:`~forgeflow.privacy.audit.ScrubAudit` row per
  processed experience.

A structure-breaking scrub is **refused** (禁止静默通过): the record is left
untouched, not saved, and reported in ``refused`` — never silently persisted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from forgeflow.privacy.audit import (
    ScrubAudit,
    ScrubAuditStore,
    get_scrub_audit_store,
)
from forgeflow.privacy.scrubber import (
    SCRUB_STATUS_REFUSED,
    SCRUB_STATUS_SCRUBBED,
    SCRUB_VERSION,
    Scrubber,
    get_scrubber,
    scrub_experience_record,
)

logger = logging.getLogger(__name__)

__all__ = ["BackfillReport", "backfill_scrub"]


@dataclass
class BackfillReport:
    """The outcome of one backfill pass (every count is real, never faked)."""

    tenant_id: str | None
    version: str
    scanned: int = 0
    changed: int = 0
    unchanged: int = 0
    refused: int = 0
    residual: int = 0
    audits: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "version": self.version,
            "scanned": self.scanned,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "refused": self.refused,
            "residual": self.residual,
            "audits": self.audits,
            "details": list(self.details),
        }


def _record_texts(record: Any) -> list[str]:
    """Every free-text field the scrubber touched (for the residual check)."""
    texts: list[str] = [getattr(record, "summary", "") or ""]
    for step in getattr(record, "reusable_steps", None) or []:
        if isinstance(step, dict) and isinstance(step.get("note"), str):
            texts.append(step["note"])
    for decision in getattr(record, "decisions", None) or []:
        if isinstance(decision, dict):
            for key in ("reason", "detail", "decision", "note", "text"):
                value = decision.get(key)
                if isinstance(value, str):
                    texts.append(value)
        elif isinstance(decision, str):
            texts.append(decision)
    return texts


async def backfill_scrub(
    tenant_id: str | None,
    *,
    repo: Any | None = None,
    audit_store: ScrubAuditStore | None = None,
    scrubber: Scrubber | None = None,
    version: str = SCRUB_VERSION,
    limit: int = 1000,
) -> BackfillReport:
    """Re-scrub a tenant's experiences idempotently. See module docstring."""
    report = BackfillReport(tenant_id=tenant_id, version=version)
    if not tenant_id:
        logger.info("backfill skipped: no tenant (fail closed)")
        return report

    if repo is None:
        from forgeflow.repositories import get_experience_repository

        repo = get_experience_repository()
    audit_store = audit_store or get_scrub_audit_store()
    scrubber = scrubber or get_scrubber()

    records = await repo.list(tenant_id, limit=limit)
    report.scanned = len(records)

    for record in records:
        from_version = getattr(record, "scrub_version", None)
        outcome = scrub_experience_record(
            record, tenant_id=tenant_id, version=version, scrubber=scrubber
        )

        if outcome.refused:
            report.refused += 1
            report.details.append(
                {
                    "experience_id": getattr(record, "id", ""),
                    "status": SCRUB_STATUS_REFUSED,
                    "reason": outcome.refused,
                }
            )
            _audit(
                audit_store,
                tenant_id,
                record,
                from_version=from_version,
                to_version=version,
                status=SCRUB_STATUS_REFUSED,
                categories=outcome.categories,
                match_count=outcome.match_count,
                changed=False,
                content_sha256=None,
                report=report,
            )
            continue

        # Residual-PII check: a correctly scrubbed record detects nothing.
        residual = sum(len(scrubber.detect(text)) for text in _record_texts(record))
        report.residual += residual

        if outcome.changed:
            await repo.save(record)
            report.changed += 1
        else:
            report.unchanged += 1

        report.details.append(
            {
                "experience_id": getattr(record, "id", ""),
                "status": SCRUB_STATUS_SCRUBBED,
                "changed": outcome.changed,
                "categories": outcome.categories,
                "residual": residual,
            }
        )
        _audit(
            audit_store,
            tenant_id,
            record,
            from_version=from_version,
            to_version=version,
            status=SCRUB_STATUS_SCRUBBED,
            categories=outcome.categories,
            match_count=outcome.match_count,
            changed=outcome.changed,
            content_sha256=outcome.content_sha256,
            report=report,
        )

    logger.info(
        "scrub backfill | tenant=%s version=%s scanned=%d changed=%d unchanged=%d "
        "refused=%d residual=%d",
        tenant_id,
        version,
        report.scanned,
        report.changed,
        report.unchanged,
        report.refused,
        report.residual,
    )
    return report


def _audit(
    store: ScrubAuditStore,
    tenant_id: str,
    record: Any,
    *,
    from_version: str | None,
    to_version: str,
    status: str,
    categories: list[str],
    match_count: int,
    changed: bool,
    content_sha256: str | None,
    report: BackfillReport,
) -> None:
    """Record one minimised audit row (no raw PII is ever stored)."""
    store.record(
        ScrubAudit(
            tenant_id=tenant_id,
            experience_id=str(getattr(record, "id", "")),
            from_version=from_version,
            to_version=to_version,
            status=status,
            categories=list(categories),
            match_count=match_count,
            changed=changed,
            content_sha256=content_sha256,
        )
    )
    report.audits += 1
