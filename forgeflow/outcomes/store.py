"""T16 — outcome storage: an append-only feedback stream + one current label.

Backed by migration ``024`` (``run_outcomes`` / ``feedback_events``). Two
implementations share one interface so the unit suite runs fully offline while
the integration suite proves the real PG tables:

* :class:`InMemoryOutcomeStore` — process-local dicts (default, offline-safe).
* :class:`PostgresOutcomeStore` — the real tables via ``psycopg`` (sync).

Isolation discipline (红线 5)
-----------------------------
Every method takes ``tenant`` first and never looks across it. A run that
already belongs to **another** tenant is reported as a conflict
(:class:`CrossTenantRun`) so the router can answer **403** instead of silently
writing a second, shadow copy under the caller's tenant (任务书 T16 阴性探针).
A run nobody owns yet is fine — the first feedback to it *creates* ownership.

Honesty discipline
------------------
* ``hard_pass`` is stored as ``None`` when unmeasured — never coerced to
  ``False`` (红线 4).
* ``get_outcome`` returns ``None`` for an unknown run rather than fabricating an
  ``UNKNOWN`` row: "no row" and "a row that says UNKNOWN" are different facts.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from forgeflow.outcomes.labeler import derive_label

logger = logging.getLogger(__name__)

__all__ = [
    "CrossTenantRun",
    "InMemoryOutcomeStore",
    "PostgresOutcomeStore",
    "get_outcome_store",
]


class CrossTenantRun(Exception):
    """The ``run_id`` is already owned by a **different** tenant (⇒ HTTP 403)."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id} belongs to another tenant")
        self.run_id = run_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryOutcomeStore:
    """Offline store: ``(tenant, run_id)`` scoped, append-only feedback."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._keys: set[tuple[str, str]] = set()
        self._outcomes: dict[tuple[str, str], dict[str, Any]] = {}
        self._context: dict[tuple[str, str], dict[str, Any]] = {}

    # --- run context (written by T23 / T24, not by the feedback endpoint) --- #
    def set_run_context(
        self,
        tenant: str,
        run_id: str,
        *,
        run_status: str | None = None,
        hard_pass: bool | None = None,
        verified_at: datetime | None = None,
    ) -> None:
        """Record the objective facts a label is derived from.

        ``hard_pass`` stays ``None`` when the validation stack never reached a
        verdict — that is *unmeasured*, not a failure (红线 4).
        """
        self._context[(tenant, run_id)] = {
            "run_status": run_status,
            "hard_pass": hard_pass,
            "verified_at": verified_at,
        }

    # --- writes ------------------------------------------------------------ #
    def record_feedback(
        self,
        tenant: str,
        run_id: str,
        kind: str,
        *,
        actor: str | None = None,
        idempotency_key: str,
        note: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Append one feedback event.

        Returns:
            ``(event, created)`` — ``created=False`` when the same
            ``(tenant, idempotency_key)`` was already recorded, in which case
            the **existing** event is returned unchanged and nothing is counted
            twice (任务书 T16 阴性探针).
        """
        key = (tenant, idempotency_key)
        if key in self._keys:
            for event in self._events:
                if (event["tenant_id"], event["idempotency_key"]) == key:
                    return event, False
        self._assert_not_foreign(tenant, run_id)
        event = {
            "tenant_id": tenant,
            "run_id": run_id,
            "kind": kind,
            "actor": actor,
            "idempotency_key": idempotency_key,
            "note": note,
            "created_at": _now(),
        }
        self._events.append(event)
        self._keys.add(key)
        return event, True

    def _assert_not_foreign(self, tenant: str, run_id: str) -> None:
        owners = {
            t for (t, r) in list(self._outcomes) + list(self._context) if r == run_id
        }
        owners |= {e["tenant_id"] for e in self._events if e["run_id"] == run_id}
        foreign = owners - {tenant}
        if foreign:
            raise CrossTenantRun(run_id)

    def upsert_outcome(
        self,
        tenant: str,
        run_id: str,
        label: str,
        *,
        hard_pass: bool | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "tenant_id": tenant,
            "run_id": run_id,
            "outcome_label": label,
            "hard_pass": hard_pass,
            "reason": reason,
            "labeled_at": _now(),
            "updated_at": _now(),
        }
        self._outcomes[(tenant, run_id)] = row
        return row

    # --- reads ------------------------------------------------------------- #
    def list_feedback(self, tenant: str, run_id: str) -> list[dict[str, Any]]:
        return [
            e for e in self._events if e["tenant_id"] == tenant and e["run_id"] == run_id
        ]

    def get_outcome(self, tenant: str, run_id: str) -> dict[str, Any] | None:
        return self._outcomes.get((tenant, run_id))

    def relabel(self, tenant: str, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Recompute and store the label from the stored context + feedback."""
        ctx = self._context.get((tenant, run_id), {})
        kinds = [e["kind"] for e in self.list_feedback(tenant, run_id)]
        label = derive_label(
            run_status=ctx.get("run_status"),
            feedback_kinds=kinds,
            hard_pass=ctx.get("hard_pass"),
            verified_at=ctx.get("verified_at"),
            now=now,
        )
        return self.upsert_outcome(tenant, run_id, label, hard_pass=ctx.get("hard_pass"))


class PostgresOutcomeStore:
    """The real ``run_outcomes`` / ``feedback_events`` tables (migration 024)."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _connect(self):  # noqa: ANN202 — psycopg connection, imported lazily
        import psycopg

        return psycopg.connect(self._dsn)

    def set_run_context(
        self,
        tenant: str,
        run_id: str,
        *,
        run_status: str | None = None,
        hard_pass: bool | None = None,
        verified_at: datetime | None = None,
    ) -> None:
        """Persist the objective facts on the run's outcome row (upsert)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_outcomes (tenant_id, run_id, outcome_label, hard_pass)
                VALUES (%s, %s, 'UNKNOWN', %s)
                ON CONFLICT (tenant_id, run_id)
                DO UPDATE SET hard_pass = EXCLUDED.hard_pass,
                              updated_at = now()
                """,
                (tenant, run_id, hard_pass),
            )
            conn.commit()

    def record_feedback(
        self,
        tenant: str,
        run_id: str,
        kind: str,
        *,
        actor: str | None = None,
        idempotency_key: str,
        note: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, kind, actor, note, created_at FROM feedback_events "
                "WHERE tenant_id = %s AND idempotency_key = %s",
                (tenant, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                return (
                    {
                        "tenant_id": existing[0],
                        "run_id": run_id,
                        "kind": existing[1],
                        "actor": existing[2],
                        "idempotency_key": idempotency_key,
                        "note": existing[3],
                        "created_at": existing[4],
                    },
                    False,
                )
            # 该 run 是否已被别的租户认领？
            cur.execute(
                "SELECT tenant_id FROM run_outcomes WHERE run_id = %s "
                "UNION SELECT tenant_id FROM feedback_events WHERE run_id = %s LIMIT 1",
                (run_id, run_id),
            )
            owner = cur.fetchone()
            if owner is not None and owner[0] != tenant:
                raise CrossTenantRun(run_id)

            cur.execute(
                """
                INSERT INTO feedback_events
                    (tenant_id, run_id, kind, actor, idempotency_key, note)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING tenant_id, kind, actor, note, created_at
                """,
                (tenant, run_id, kind, actor, idempotency_key, note),
            )
            row = cur.fetchone()
            conn.commit()
        return (
            {
                "tenant_id": row[0],
                "run_id": run_id,
                "kind": row[1],
                "actor": row[2],
                "idempotency_key": idempotency_key,
                "note": row[3],
                "created_at": row[4],
            },
            True,
        )

    def list_feedback(self, tenant: str, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, actor, idempotency_key, note, created_at FROM feedback_events "
                "WHERE tenant_id = %s AND run_id = %s ORDER BY created_at",
                (tenant, run_id),
            )
            return [
                {
                    "tenant_id": tenant,
                    "run_id": run_id,
                    "kind": r[0],
                    "actor": r[1],
                    "idempotency_key": r[2],
                    "note": r[3],
                    "created_at": r[4],
                }
                for r in cur.fetchall()
            ]

    def get_outcome(self, tenant: str, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT outcome_label, hard_pass, reason, labeled_at, updated_at "
                "FROM run_outcomes WHERE tenant_id = %s AND run_id = %s",
                (tenant, run_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "tenant_id": tenant,
            "run_id": run_id,
            "outcome_label": row[0],
            "hard_pass": row[1],
            "reason": row[2],
            "labeled_at": row[3],
            "updated_at": row[4],
        }

    def upsert_outcome(
        self,
        tenant: str,
        run_id: str,
        label: str,
        *,
        hard_pass: bool | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_outcomes (tenant_id, run_id, outcome_label, hard_pass, reason)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, run_id)
                DO UPDATE SET outcome_label = EXCLUDED.outcome_label,
                              hard_pass = EXCLUDED.hard_pass,
                              reason = EXCLUDED.reason,
                              updated_at = now()
                RETURNING outcome_label, hard_pass, reason, labeled_at, updated_at
                """,
                (tenant, run_id, label, hard_pass, reason),
            )
            row = cur.fetchone()
            conn.commit()
        return {
            "tenant_id": tenant,
            "run_id": run_id,
            "outcome_label": row[0],
            "hard_pass": row[1],
            "reason": row[2],
            "labeled_at": row[3],
            "updated_at": row[4],
        }

    def relabel(self, tenant: str, run_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = self.get_outcome(tenant, run_id) or {}
        kinds = [e["kind"] for e in self.list_feedback(tenant, run_id)]
        label = derive_label(
            run_status=None,  # run 状态由调用方通过 upsert/context 提供
            feedback_kinds=kinds,
            hard_pass=current.get("hard_pass"),
            verified_at=current.get("labeled_at"),
            now=now,
        )
        return self.upsert_outcome(tenant, run_id, label, hard_pass=current.get("hard_pass"))


_STORE: Any = None


def get_outcome_store() -> Any:
    """Process-wide store: PG when a DSN is configured, else in-memory.

    The default is the in-memory store so every offline path (and the whole
    unit suite) works with no database at all.
    """
    global _STORE
    if _STORE is not None:
        return _STORE
    dsn = os.environ.get("POSTGRES_SYNC_URL") or os.environ.get("POSTGRES_DSN")
    if dsn:
        _STORE = PostgresOutcomeStore(dsn)
    else:
        _STORE = InMemoryOutcomeStore()
    return _STORE


def reset_outcome_store() -> None:
    """Test helper — drop the cached store (next call re-resolves it)."""
    global _STORE
    _STORE = None


def set_outcome_store(store: Any) -> None:
    """Test helper — pin an explicit store (usually an :class:`InMemoryOutcomeStore`)."""
    global _STORE
    _STORE = store
