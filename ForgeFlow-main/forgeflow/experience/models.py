"""Domain models for the Memory Hub — the ``Experience`` first-class asset."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from forgeflow.repositories.base import new_id, utcnow

# Valid values for ExperienceRecord.outcome.
OUTCOMES = ("success", "failure", "aborted")


@dataclass
class ExperienceRecord:
    """A reusable summary of one task execution.

    Mirrors the ``experiences`` table (docs §3.1/§3.2) one-to-one; ``memory_ids``
    carries the N:M ``experience_memory`` relation so the dataclass and the SQL
    row describe the same object.
    """

    id: str = field(default_factory=new_id)
    tenant_id: str | None = None
    team_id: str | None = None
    run_id: str = ""
    summary: str = ""
    decisions: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "success"
    reusable_steps: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    embedding: list[float] | None = None
    # --- INC2-16 (B4) de-duplication lineage / arbitration metadata --------- #
    # Real columns added by migration 011 on ``experiences``:
    #   merged_from   UUID[] NOT NULL DEFAULT '{}'  → list[str]
    #   conflict_with UUID[] NOT NULL DEFAULT '{}'  → list[str]
    #   dedup_key     TEXT            (nullable)    → str | None
    #   confidence    DOUBLE PRECISION (nullable)   → float | None
    # NOTE: the two arrays are NOT NULL in the schema — always emit ``[]``,
    # never ``None`` (PG treats NULL as distinct for UNIQUE / array semantics).
    merged_from: list[str] = field(default_factory=list)
    conflict_with: list[str] = field(default_factory=list)
    dedup_key: str | None = None
    confidence: float | None = None
    created_at: datetime = field(default_factory=utcnow)
    memory_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation (datetimes → ISO 8601 strings)."""
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExperienceRecord":
        """Rebuild from ``to_dict()`` output (or a DB row mapping)."""
        payload = dict(data)
        created = payload.get("created_at")
        if isinstance(created, str):
            try:
                payload["created_at"] = datetime.fromisoformat(created)
            except ValueError:
                payload["created_at"] = utcnow()
        elif created is None:
            payload["created_at"] = utcnow()
        known = {f for f in cls.__dataclass_fields__}  # noqa: SLF001 — dataclass API
        payload = {k: v for k, v in payload.items() if k in known}
        return cls(**payload)
