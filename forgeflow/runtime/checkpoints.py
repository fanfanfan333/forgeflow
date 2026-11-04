"""Run checkpoints (8-feature program, Phase 7).

Run-internal step-boundary resume + approval-boundary persistence,
coexisting with (never replacing) run records, handoff payloads,
and trajectory refs. A checkpoint is the resume cursor for an
UNFINISHED run: intent, requirements snapshot, per-round
acceptance, next step, codeplane/diff refs, breaker + stuck
summaries, approval state + payload hash, pending writes, and a
trajectory cursor. It is NOT a full snapshot: message history and
tool output text stay in the trajectory store, and the checkpoint
points at them (a full per-step snapshot is a proven PG-killer,
and LangGraph's own guidance is refs-only for large objects).

Four rules, decided before code (research §8):

* Step boundary: one react-loop tool-batch round = one checkpoint,
  written AFTER the batch completes (side effects confirmed) and
  before the next model call. Tool-call-level state is pending
  writes only. Model calls get no checkpoint (a model call cannot
  be resumed mid-call, only re-issued).
* Idempotency keys: ``run_id:round:tool_call_id``. Recorded on
  pending writes (and available to tools that honor them); v1
  guarantees the resume cursor is not lost, NOT that a
  non-idempotent tool's side effect runs exactly once — that needs
  tool-side key support, a follow-up.
* Single writer: PG advisory lock per run_id inside the save
  transaction (InMemory: one lock for the process store). Resume
  is additionally gated on run status: only ``interrupted`` /
  ``awaiting_approval`` / ``failed`` may resume; ``running``
  refuses a second writer, ``completed`` refuses outright.
* Retention: prune keeps the latest N round checkpoints + ALL
  approval checkpoints (approval boundaries are rare and are the
  points a human actually resumes from; deleting them would
  delete the resume story).

Approval re-verification (the anti-pattern to avoid): a checkpoint
never authorizes a resume by itself. ``verify_resume`` re-checks
the approval against the live PendingAction (exists, RESOLVED +
approve, resolver == checkpoint owner, not expired, payload
args_hash matches) and refuses to continue when any check fails —
the run stops at the boundary instead of replaying an approved
write it can no longer prove.

Checkpoint writes are best-effort: a failed write logs and the
run continues (a missing checkpoint costs a from-scratch rerun,
never a failed run). The store is sync (psycopg, mirroring the
pending store); async callers offload with ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "Checkpoint",
    "CheckpointResumeRefused",
    "DEFAULT_KEEP_LATEST",
    "InMemoryCheckpointStore",
    "PostgresCheckpointStore",
    "RESUMABLE_STATUSES",
    "SCHEMA_VERSION",
    "build_resume_seed",
    "build_state",
    "get_checkpoint_store",
    "idempotency_key",
    "reset_checkpoint_store",
    "set_checkpoint_store",
    "verify_resume",
]

SCHEMA_VERSION = 1
RESUMABLE_STATUSES: frozenset[str] = frozenset({"interrupted", "awaiting_approval", "failed"})
DEFAULT_KEEP_LATEST = 10


class CheckpointResumeRefused(Exception):
    """Resume is not safe from this checkpoint (reason in the message)."""


@dataclass
class Checkpoint:
    tenant_id: str
    run_id: str
    round: int
    kind: str  # "round" | "approval"
    schema_version: int
    state: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    seq: int | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "round": self.round,
            "kind": self.kind,
            "schema_version": self.schema_version,
            "state": self.state,
            "seq": self.seq,
            "created_at": self.created_at,
        }


def idempotency_key(run_id: str, round: int, tool_call_id: str) -> str:
    """Stable key for one tool call in one round of one run."""
    return f"{run_id}:{round}:{tool_call_id}"


def build_state(
    *,
    intent: str,
    requirements: list[dict[str, Any]],
    acceptance: dict[str, Any] | None,
    next_step: dict[str, Any] | None,
    codeplane_ref: dict[str, Any] | None,
    breaker_summary: dict[str, Any],
    stuck_summary: dict[str, Any],
    approval: dict[str, Any] | None,
    pending_writes: list[dict[str, Any]],
    trajectory_cursor: int,
) -> dict[str, Any]:
    """Assemble a minimal checkpoint state (pure; refs only, no bodies)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "intent": intent,
        "requirements": requirements,
        "acceptance": acceptance,
        "next_step": next_step,
        "codeplane_ref": codeplane_ref,
        "breaker_summary": breaker_summary,
        "stuck_summary": stuck_summary,
        "approval": approval,
        "pending_writes": pending_writes,
        "trajectory_cursor": trajectory_cursor,
    }


MAX_EVIDENCE_CHARS = 4000


def build_resume_seed(
    checkpoint: Checkpoint, source_invocations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the seed for a checkpoint-resumed run (pure).

    Records are the source run's raw invocations up to the
    checkpoint's trajectory cursor (never beyond: the cursor is
    the confirmed boundary). The evidence summary is derived here,
    at resume time — it is NOT stored in the checkpoint (checkpoints
    stay refs-only). The resumed run gets a FRESH model-call budget:
    the checkpoint round is audit metadata only, so a resume never
    starts against the old run's ceiling.
    """
    state = checkpoint.state or {}
    cursor = int(state.get("trajectory_cursor") or 0)
    records = [dict(r) for r in list(source_invocations or [])[:cursor]]
    lines = [f"检查点轮次 {checkpoint.round}（来源 run {checkpoint.run_id}）："]
    for record in records:
        tool = record.get("tool", "?")
        status = record.get("status", "?")
        lines.append(f"- {tool}: {status}")
    acceptance = state.get("acceptance")
    if isinstance(acceptance, dict):
        rows = acceptance.get("requirements") or []
        done = sum(1 for row in rows if isinstance(row, dict) and row.get("satisfied"))
        lines.append(f"验收：{done}/{len(rows)} 行已满足，complete={acceptance.get('complete')}")
    summary = "\n".join(lines)
    if len(summary) > MAX_EVIDENCE_CHARS:
        summary = summary[:MAX_EVIDENCE_CHARS] + "\n…（摘要已截断，完整轨迹见源 run 记录）"
    return {
        "source_run_id": checkpoint.run_id,
        "checkpoint_id": checkpoint.id,
        "checkpoint_round": checkpoint.round,
        "records": records,
        "acceptance_history": [acceptance] if isinstance(acceptance, dict) else [],
        "stuck_patterns": (state.get("stuck_summary") or {}).get("patterns") or [],
        "stuck_forced_final": bool((state.get("stuck_summary") or {}).get("forced_final")),
        "evidence_summary": summary,
    }


def verify_resume(
    checkpoint: Checkpoint,
    *,
    run_status: str,
    approval_resolver: Callable[[str], dict[str, Any] | None] | None = None,
    now_iso: str | None = None,
    skip_status: bool = False,
) -> dict[str, Any]:
    """Gate a resume: return the state, or raise CheckpointResumeRefused.

    Checks, in order: schema version (cross-version restores must
    migrate or refuse, never silently reinterpret); run status
    resumable; and, when the checkpoint carries approval state, the
    live PendingAction (exists, RESOLVED + approve, resolver ==
    owner, not expired, payload args_hash matches). ``now_iso`` is
    an ISO timestamp for expiry comparison (tests inject it).

    ``skip_status`` is the fork case only (Phase 9 A3): a fork starts
    a NEW lineage from any checkpoint while the source run keeps its
    own status, so the source need not be resumable — but schema and
    approval re-verification still apply, and approval checkpoints
    are refused by the caller regardless.
    """
    if checkpoint.schema_version != SCHEMA_VERSION:
        raise CheckpointResumeRefused(
            f"checkpoint schema_version {checkpoint.schema_version} != {SCHEMA_VERSION} "
            "(migrate or rerun from scratch; refusing to reinterpret)"
        )
    if not skip_status and run_status not in RESUMABLE_STATUSES:
        raise CheckpointResumeRefused(
            f"run status {run_status!r} is not resumable (resumable: {sorted(RESUMABLE_STATUSES)})"
        )
    approval = (checkpoint.state or {}).get("approval")
    if approval:
        if approval_resolver is None:
            raise CheckpointResumeRefused("checkpoint carries approval but no resolver given")
        action = approval_resolver(str(approval.get("pending_id")))
        if action is None:
            raise CheckpointResumeRefused("approval action no longer exists; stopping at boundary")
        if action.get("status") != "resolved" or action.get("resolution") != "approve":
            raise CheckpointResumeRefused(
                f"approval not approved (status={action.get('status')!r}, "
                f"resolution={action.get('resolution')!r}); stopping at boundary"
            )
        if action.get("resolved_by") != approval.get("owner"):
            raise CheckpointResumeRefused(
                "approval resolver != checkpoint owner; stopping at boundary"
            )
        expires_at = action.get("expires_at")
        if expires_at and now_iso:
            # Parse both sides (fromisoformat accepts space- and
            # T-separated ISO, with or without offset); string
            # comparison would misorder " " vs "T" separators.
            from datetime import datetime as _datetime

            try:
                exp = (
                    expires_at
                    if isinstance(expires_at, _datetime)
                    else _datetime.fromisoformat(str(expires_at))
                )
                now = _datetime.fromisoformat(now_iso)
            except ValueError as exc:
                raise CheckpointResumeRefused(
                    "approval expiry unparseable; stopping at boundary"
                ) from exc
            if exp <= now:
                raise CheckpointResumeRefused("approval expired; stopping at boundary")
        payload = action.get("payload") or {}
        if (
            isinstance(payload, dict)
            and payload.get("args_hash") is not None
            and payload.get("args_hash") != approval.get("args_hash")
        ):
            raise CheckpointResumeRefused("approval payload hash changed; stopping at boundary")
    return checkpoint.state


# --- stores (mirror the pending-action store shape) -------------------------- #


class InMemoryCheckpointStore:
    """Process-lifetime store (tests / no-PG runs)."""

    #: Phase 9 B9 durability tier: "ephemeral" — rows die with the
    #: process, so cross-restart resume cannot rely on them (the
    #: trajectory-gone refusal exists for exactly this reason).
    durability = "ephemeral"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: list[Checkpoint] = []
        self._seq = 0

    def save(
        self, tenant: str, *, run_id: str, round: int, kind: str, state: dict[str, Any]
    ) -> Checkpoint:
        if not tenant:
            raise ValueError("tenant is required")
        if kind not in ("round", "approval"):
            raise ValueError(f"unknown checkpoint kind {kind!r}")
        with self._lock:
            self._seq += 1
            row = Checkpoint(
                tenant_id=tenant,
                run_id=run_id,
                round=round,
                kind=kind,
                schema_version=SCHEMA_VERSION,
                state=state,
                id=f"mem-{self._seq}",
                seq=self._seq,
            )
            self._rows.append(row)
            return row

    def latest(self, tenant: str, run_id: str) -> Checkpoint | None:
        if not tenant:
            return None
        with self._lock:
            matches = [r for r in self._rows if r.tenant_id == tenant and r.run_id == run_id]
            return matches[-1] if matches else None

    def history(self, tenant: str, run_id: str, limit: int = 20) -> list[Checkpoint]:
        if not tenant:
            return []
        with self._lock:
            matches = [r for r in self._rows if r.tenant_id == tenant and r.run_id == run_id]
            return list(reversed(matches[-limit:]))

    def prune(self, tenant: str, run_id: str, keep_latest: int = DEFAULT_KEEP_LATEST) -> int:
        """Delete round checkpoints beyond the latest N; never approvals."""
        if not tenant:
            return 0
        with self._lock:
            rounds = [
                r
                for r in self._rows
                if r.tenant_id == tenant and r.run_id == run_id and r.kind == "round"
            ]
            if len(rounds) <= keep_latest:
                return 0
            doomed = {id(r) for r in rounds[:-keep_latest]}
            before = len(self._rows)
            self._rows = [r for r in self._rows if id(r) not in doomed]
            return before - len(self._rows)


def _normalise_dsn(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://")


class PostgresCheckpointStore:
    """The real ``run_checkpoints`` table (migration ``042``) via ``psycopg``.

    Phase 9 B9 durability tier "durable": rows survive process restarts.
    (The checkpoint rows do; the hub run records they point at do NOT,
    which is why resume still refuses when the source trajectory is
    gone — durability tiers compose, they do not substitute.)
    """

    # Synchronous by design, mirroring the pending-action Postgres
    # store. Saves take a transaction-scoped advisory lock on the
    # run_id (single writer per run); latest-wins is by ``seq``.
    durability = "durable"

    def __init__(self, dsn: str) -> None:
        self._dsn = _normalise_dsn(dsn)

    def _connect(self, tenant_id: str | None = None):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        from forgeflow.tenant_scope import current_tenant

        conn = psycopg.connect(self._dsn)
        conn.execute(
            "SELECT set_config('app.tenant_id', %s, true)", (tenant_id or current_tenant() or "",)
        )
        return conn

    def save(
        self, tenant: str, *, run_id: str, round: int, kind: str, state: dict[str, Any]
    ) -> Checkpoint:
        import json

        if not tenant:
            raise ValueError("tenant is required")
        if kind not in ("round", "approval"):
            raise ValueError(f"unknown checkpoint kind {kind!r}")
        lock_key = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:15], 16)
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            cur.execute(
                "INSERT INTO run_checkpoints "
                "(tenant_id, run_id, round, kind, schema_version, state) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
                "RETURNING id, seq, created_at",
                (tenant, run_id, round, kind, SCHEMA_VERSION, json.dumps(state)),
            )
            row = cur.fetchone()
            conn.commit()
        return Checkpoint(
            tenant_id=tenant,
            run_id=run_id,
            round=round,
            kind=kind,
            schema_version=SCHEMA_VERSION,
            state=state,
            id=str(row[0]),
            seq=int(row[1]),
            created_at=row[2].isoformat() if row[2] is not None else None,
        )

    def latest(self, tenant: str, run_id: str) -> Checkpoint | None:
        import json

        if not tenant:
            return None
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, round, kind, schema_version, state, seq, created_at "
                "FROM run_checkpoints WHERE tenant_id = %s AND run_id = %s "
                "ORDER BY seq DESC LIMIT 1",
                (tenant, run_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return Checkpoint(
            tenant_id=tenant,
            run_id=run_id,
            round=int(row[1]),
            kind=str(row[2]),
            schema_version=int(row[3]),
            state=row[4] if isinstance(row[4], dict) else json.loads(row[4]),
            id=str(row[0]),
            seq=int(row[5]),
            created_at=row[6].isoformat() if row[6] is not None else None,
        )

    def history(self, tenant: str, run_id: str, limit: int = 20) -> list[Checkpoint]:
        import json

        if not tenant:
            return []
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, round, kind, schema_version, state, seq, created_at "
                "FROM run_checkpoints WHERE tenant_id = %s AND run_id = %s "
                "ORDER BY seq DESC LIMIT %s",
                (tenant, run_id, limit),
            )
            rows = cur.fetchall()
        out: list[Checkpoint] = []
        for row in rows:
            out.append(
                Checkpoint(
                    tenant_id=tenant,
                    run_id=run_id,
                    round=int(row[1]),
                    kind=str(row[2]),
                    schema_version=int(row[3]),
                    state=row[4] if isinstance(row[4], dict) else json.loads(row[4]),
                    id=str(row[0]),
                    seq=int(row[5]),
                    created_at=row[6].isoformat() if row[6] is not None else None,
                )
            )
        return out

    def prune(self, tenant: str, run_id: str, keep_latest: int = DEFAULT_KEEP_LATEST) -> int:
        if not tenant:
            return 0
        with self._connect(tenant) as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM run_checkpoints WHERE tenant_id = %s AND run_id = %s "
                "AND kind = 'round' AND seq NOT IN ("
                "  SELECT seq FROM run_checkpoints "
                "  WHERE tenant_id = %s AND run_id = %s AND kind = 'round' "
                "  ORDER BY seq DESC LIMIT %s"
                ")",
                (tenant, run_id, tenant, run_id, keep_latest),
            )
            deleted = cur.rowcount
            conn.commit()
        return int(deleted)


_STORE: Any = None


def get_checkpoint_store() -> Any:
    """Process-wide store: PG when a sync DSN is set, else InMemory."""
    global _STORE
    if _STORE is None:
        dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
        _STORE = PostgresCheckpointStore(dsn) if dsn else InMemoryCheckpointStore()
    return _STORE


def set_checkpoint_store(store: Any) -> None:
    global _STORE
    _STORE = store


def reset_checkpoint_store() -> None:
    global _STORE
    _STORE = None
