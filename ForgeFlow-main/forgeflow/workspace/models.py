"""``WorkspaceRunRecord`` — the persisted run header + relationship facts (INC32).

This is the **projection** the ``workspace_runs`` table (migration ``016``)
stores: the run id, its tenant / session / parent relationships, the header
fields the history & conversation columns render (intent / title / status /
outcome / timestamps) and the run's artifacts (so an artifact download still
serves after a restart). It is deliberately **not** the full ``RunRecord`` body.

The dataclass carries no behaviour beyond serialisation, so both backends
(:mod:`forgeflow.workspace.store`) and their callers share one shape.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = ["WorkspaceRunRecord"]


def _now_iso() -> str:
    """Timezone-aware ISO-8601 UTC timestamp — the one clock for this record."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WorkspaceRunRecord:
    """A tenant-scoped run header + relationship + artifacts projection.

    Attributes:
        run_id: Primary key; soft-joins to ``experiences.run_id`` (no FK).
        tenant_id: Owning tenant (opaque text; see the post-``013`` convention).
        session_id: The conversation this run belongs to. The first run of a
            session uses its own ``run_id`` (ADR-02); a Follow-up reuses the
            parent's.
        parent_run_id: The run this one continues (Follow-up chain, AC-39).
        actor_user_id / actor_role: Who started the run (audit leg).
        intent: The task intent, verbatim.
        title: Short display title (the intent excerpt).
        workflow_type: The declared workflow type (default ``"generic"``).
        status: Run status vocabulary (``running`` / ``completed`` / ``failed``
            / ``aborted`` / ``interrupted`` / ``awaiting_approval`` …).
        outcome: The validator outcome (``success`` / ``failure`` / ``aborted``).
        declared_inputs: The caller's explicit inputs, verbatim.
        artifacts: The run's deliverables — the restart-durable copy that backs
            the artifact download endpoint (AC-31).
        created_at / completed_at / updated_at: ISO-8601 text timestamps.
    """

    run_id: str
    tenant_id: str | None
    session_id: str
    parent_run_id: str = ""
    actor_user_id: str = ""
    actor_role: str = ""
    intent: str = ""
    title: str = ""
    workflow_type: str = "generic"
    status: str = "running"
    outcome: str = ""
    declared_inputs: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    completed_at: str | None = None
    updated_at: str = ""

    def __post_init__(self) -> None:
        # Never fabricate a timestamp: default an empty one to "now" so the
        # 016 columns (created_at / updated_at are NOT NULL) always hold a real
        # value.
        now = _now_iso()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = self.completed_at or self.created_at

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form (the shape ``GET /workspace/sessions`` serves)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceRunRecord":
        """Rebuild a record from a stored row / dict (defensive defaults)."""
        raw = dict(data or {})
        parent = raw.get("parent_run_id")
        return cls(
            run_id=str(raw.get("run_id") or ""),
            tenant_id=(str(raw["tenant_id"]) if raw.get("tenant_id") else None),
            session_id=str(raw.get("session_id") or ""),
            parent_run_id=str(parent or ""),
            actor_user_id=str(raw.get("actor_user_id") or ""),
            actor_role=str(raw.get("actor_role") or ""),
            intent=str(raw.get("intent") or ""),
            title=str(raw.get("title") or ""),
            workflow_type=str(raw.get("workflow_type") or "generic"),
            status=str(raw.get("status") or "running"),
            outcome=str(raw.get("outcome") or ""),
            declared_inputs=dict(raw.get("declared_inputs") or {}),
            artifacts=[
                dict(a) for a in (raw.get("artifacts") or []) if isinstance(a, dict)
            ],
            created_at=str(raw.get("created_at") or ""),
            completed_at=(raw.get("completed_at") or None),
            updated_at=str(raw.get("updated_at") or ""),
        )

    @classmethod
    def from_run_record(cls, record: Any) -> "WorkspaceRunRecord":
        """Project a runtime ``RunRecord`` into the persisted header.

        Reads only what the run record really carries (``getattr``-defensive so
        a pre-INC32 record without ``session_id`` / ``parent_run_id`` degrades to
        the honest empty string rather than breaking). ``title`` is the intent
        excerpt — the same 60-char convention the rest of the hub uses.
        """
        intent = str(getattr(record, "intent", "") or "")
        return cls(
            run_id=str(getattr(record, "run_id", "") or ""),
            tenant_id=getattr(record, "tenant_id", None),
            session_id=str(getattr(record, "session_id", "") or ""),
            parent_run_id=str(getattr(record, "parent_run_id", "") or ""),
            actor_user_id=str(getattr(record, "actor_user_id", "") or ""),
            actor_role=str(getattr(record, "actor_role", "") or ""),
            intent=intent,
            title=intent[:60],
            workflow_type=str(getattr(record, "workflow_type", "generic") or "generic"),
            status=str(getattr(record, "status", "") or ""),
            outcome=str(getattr(record, "outcome", "") or ""),
            declared_inputs=dict(getattr(record, "declared_inputs", None) or {}),
            artifacts=[
                dict(a)
                for a in (getattr(record, "artifacts", None) or [])
                if isinstance(a, dict)
            ],
            created_at=str(getattr(record, "created_at", "") or ""),
            completed_at=getattr(record, "completed_at", None),
            updated_at=_now_iso(),
        )
