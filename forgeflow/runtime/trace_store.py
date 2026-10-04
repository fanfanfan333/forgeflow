"""INC46 T01 — additive materialisation of the **real** execution trace.

Why this module exists
----------------------
Every runtime tool call already flows through exactly one choke point,
:meth:`forgeflow.runtime.tool_executor.ToolExecutor.execute`, and the
:class:`~forgeflow.runtime.tool_executor.ToolInvocation` it returns is a
complete, honest record of that call (tool / status / executed / error /
summary / result_ref / ``latency_ms: float | None`` / payload / actor …). What
was missing is *persistence*: the invocation lived only for the lifetime of the
run, so nothing downstream (pattern mining, evolution, the Skill insights
surface) could **recompute** anything from it.

This module turns each invocation into one ``run_steps`` row — the additive,
already-existing table from migration ``010`` — so the trace becomes a
first-class, replayable data substrate. It is deliberately **decoupled** from
:mod:`tool_executor`: the executor only calls
:func:`persist_invocation`, and this module owns every decision about *whether*
and *how* a row is written.

Design rules (INC46 §1.1 / §8)
------------------------------
* **Best-effort, never a gate.** Persistence runs *after* the real call and its
  result is already decided; any failure here is swallowed and logged verbatim.
  A trace write can never change a run's outcome, and
  :meth:`ToolExecutor.execute` returns the byte-identical ``ToolInvocation``.
* **Tenant fail-closed.** A trace with no ``tenant_id`` is never written — the
  platform does not invent a ``"default"`` owner for an unattributable call.
* **``run_id`` stays a UUID.** ``run_steps.run_id`` is ``UUID NOT NULL``
  (migration 010; deliberately *not* widened by 014). A non-UUID run id is
  skipped with a verbatim log rather than coerced or truncated (never fabricate
  a linkage).
* **Honest latency.** ``latency_ms`` is stored verbatim as ``float | None`` —
  ``None`` ("never measured") is a real value and is **never** coerced to ``0``
  (migration ``019`` drops the old ``NOT NULL DEFAULT 0`` so this is storable).
* **Memory profile = no-op.** ``STORAGE_BACKEND=memory`` has no ``run_steps``
  table and no pool; persistence is skipped, never faked.

The ``TraceStore`` protocol keeps the write path swappable: the production
implementation is :class:`PgTraceStore` (asyncpg), while tests inject an
in-process sink via :func:`set_trace_sink` to exercise row-building offline.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from forgeflow.config import get_settings

logger = logging.getLogger(__name__)

__all__ = [
    "TraceStep",
    "TraceStore",
    "PgTraceStore",
    "to_row",
    "parse_step_index",
    "trace_persistence_enabled",
    "persist_invocation",
    "list_for_run",
    "list_for_tenant",
    "set_trace_sink",
    "reset_trace_sink",
]

#: The step type every materialised tool invocation carries (the table's own
#: default is ``"tool"``; we state it explicitly so a row is self-describing).
DEFAULT_STEP_TYPE = "tool"


# --------------------------------------------------------------------------- #
# Value object                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class TraceStep:
    """One materialised ``run_steps`` row (mirrors the table's column set).

    ``latency_ms`` is the one field with a *meaningful* ``None``: it means "the
    handler was never measured", not "0 ms". The table column is nullable with
    no default (migration ``019``) precisely so this survives a round-trip.
    """

    run_id: str
    tenant_id: str | None
    step_index: int
    step_type: str = DEFAULT_STEP_TYPE
    tool: str | None = None
    status: str | None = None
    artifact_ref: str | None = None
    verification: dict[str, Any] | None = None
    latency_ms: float | None = None
    attempt: int | None = None
    actor_user_id: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_row(self) -> dict[str, Any]:
        """The column→value mapping for the ``run_steps`` INSERT.

        JSONB columns carry Python objects (``dict`` / ``None``); the asyncpg
        JSON/JSONB codec installed by ``forgeflow.database._init_connection``
        serialises them, and ``None`` binds as SQL ``NULL`` (not JSON ``null``).
        """
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "step_index": self.step_index,
            "step_type": self.step_type,
            "tool": self.tool,
            "input": self.input,
            "output": self.output,
            "latency_ms": self.latency_ms,
            "status": self.status,
            "artifact_ref": self.artifact_ref,
            "verification": self.verification,
            "actor_user_id": self.actor_user_id,
            "attempt": self.attempt,
            "created_at": self.created_at,
        }


# --------------------------------------------------------------------------- #
# invocation → TraceStep                                                       #
# --------------------------------------------------------------------------- #
def _payload_dict(invocation: dict[str, Any]) -> dict[str, Any]:
    """The invocation's ``payload`` when it is a dict, else ``{}``.

    ``payload`` may be a bounded dict (the common case) or a sanitised string
    (external tools) — only the dict form can carry ``verification`` /
    ``artifact_ref`` hints.
    """
    payload = invocation.get("payload")
    return payload if isinstance(payload, dict) else {}


def to_row(
    invocation: dict[str, Any],
    *,
    args: dict[str, Any] | None,
    step_index: int,
    tenant_id: str | None,
    step_type: str = DEFAULT_STEP_TYPE,
) -> TraceStep:
    """Project one ``ToolInvocation.to_dict()`` onto a :class:`TraceStep`.

    Field mapping (INC46 §1.1), each honest by construction:

      * ``run_id``     ← ``invocation["run_id"]`` (already validated as a UUID);
      * ``tool`` / ``status`` / ``attempt`` / ``actor_user_id`` ← verbatim;
      * ``latency_ms`` ← ``invocation["latency_ms"]`` (``float | None``; never
        coerced to ``0``);
      * ``artifact_ref`` ← ``payload["artifact_ref"]`` when present, else
        ``invocation["result_ref"]`` (``None`` when neither exists — never
        invented);
      * ``verification`` ← ``payload["verification"]`` when it is a dict, else
        ``None``;
      * ``input``      ← ``{"args": args, "arguments_hash": …}`` (the tool's real
        inputs, straight off the call context);
      * ``output``     ← the **whole** invocation dict, so the row is
        self-contained evidence;
      * ``created_at`` ← ``invocation["started_at"]`` (ISO-8601 UTC).
    """
    payload = _payload_dict(invocation)

    artifact_ref = invocation.get("result_ref")
    if payload.get("artifact_ref"):
        artifact_ref = payload["artifact_ref"]

    verification = payload.get("verification")
    if not isinstance(verification, dict):
        verification = None

    latency_ms = invocation.get("latency_ms")
    if latency_ms is not None:
        latency_ms = float(latency_ms)

    attempt = invocation.get("attempt")
    attempt = int(attempt) if attempt is not None else None

    return TraceStep(
        run_id=str(invocation.get("run_id") or ""),
        tenant_id=tenant_id,
        step_index=int(step_index),
        step_type=step_type,
        tool=invocation.get("tool"),
        status=invocation.get("status"),
        artifact_ref=artifact_ref if isinstance(artifact_ref, str) else None,
        verification=verification,
        latency_ms=latency_ms,
        attempt=attempt,
        actor_user_id=invocation.get("actor_user_id"),
        input={
            "args": dict(args or {}),
            "arguments_hash": invocation.get("arguments_hash"),
        },
        output=dict(invocation),
        created_at=str(invocation.get("started_at") or ""),
    )


# --------------------------------------------------------------------------- #
# step_index resolution                                                        #
# --------------------------------------------------------------------------- #
def parse_step_index(step_id: str | None) -> int | None:
    """Extract the integer index from a ``"{run_id}:{attempt}:{index}"`` step id.

    The runtime stamps every step id in exactly that shape
    (:mod:`forgeflow.runtime.planning`), so the trailing segment is the index.
    Returns ``None`` when ``step_id`` is absent / malformed (the caller then
    falls back to a per-run arrival counter).
    """
    if not isinstance(step_id, str) or ":" not in step_id:
        return None
    tail = step_id.rsplit(":", 1)[-1].strip()
    try:
        return int(tail)
    except (TypeError, ValueError):
        return None


#: Per-run arrival counter, used only when a step id cannot be parsed. Reset by
#: :func:`reset_trace_sink` so tests never inherit another test's count.
_COUNTERS: dict[str, int] = {}


def _next_step_index(run_id: str) -> int:
    nxt = _COUNTERS.get(run_id, 0)
    _COUNTERS[run_id] = nxt + 1
    return nxt


def _resolve_step_index(run_id: str, step_id: str | None) -> int:
    """The step index for ``run_id``: parsed from ``step_id`` or arrival order."""
    parsed = parse_step_index(step_id)
    return parsed if parsed is not None else _next_step_index(run_id)


# --------------------------------------------------------------------------- #
# gate + sink                                                                  #
# --------------------------------------------------------------------------- #
#: Test seam — an in-process :class:`TraceStore` injected via
#: :func:`set_trace_sink`. ``None`` in production (the gate then decides).
_sink: TraceStore | None = None


def set_trace_sink(store: TraceStore | None) -> None:
    """Inject an explicit :class:`TraceStore` (tests only). ``None`` clears it."""
    global _sink
    _sink = store


def reset_trace_sink() -> None:
    """Clear the injected sink and the arrival counter. Test helper only."""
    global _sink
    _sink = None
    _COUNTERS.clear()


def _backend() -> str:
    """The active storage backend (``""`` when settings cannot be read)."""
    try:
        return str(getattr(get_settings(), "storage_backend", "") or "").lower()
    except Exception:  # noqa: BLE001 — a gate read must never break a run
        return ""


def _active_store() -> TraceStore | None:
    """The store to write/read through, or ``None`` when persistence is off.

    An injected test sink always wins (so row-building is exercisable offline);
    otherwise the postgres profile gets the real :class:`PgTraceStore` and every
    other profile (``memory``) is a no-op.
    """
    if _sink is not None:
        return _sink
    if _backend() == "postgres":
        return PgTraceStore()
    return None


def trace_persistence_enabled() -> bool:
    """Whether a ``run_steps`` write would do anything in this process.

    ``True`` for the postgres profile (or when a test sink is injected);
    ``False`` for ``memory`` — where persistence is an honest no-op, never a
    fabricated success.
    """
    return _active_store() is not None


# --------------------------------------------------------------------------- #
# validation helpers                                                           #
# --------------------------------------------------------------------------- #
def _canonical_uuid(value: Any) -> str | None:
    """Return the canonical UUID string for ``value``, or ``None`` if invalid."""
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def _parse_ts(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp, falling back to ``now(UTC)`` (never ``0``)."""
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# TraceStore protocol + postgres implementation                                #
# --------------------------------------------------------------------------- #
@runtime_checkable
class TraceStore(Protocol):
    """Minimal persistence port for :class:`TraceStep` rows."""

    async def insert_step(self, step: TraceStep) -> None:
        """Persist one materialised step row."""

    async def list_for_run(
        self, tenant_id: str | None, run_id: str, *, limit: int = 500
    ) -> list[TraceStep]:
        """Return a run's steps (ascending ``step_index``), tenant-scoped."""

    async def list_for_tenant(
        self, tenant_id: str | None, *, limit: int = 1000
    ) -> list[TraceStep]:
        """Return a tenant's most recent steps (newest first), tenant-scoped."""


def _row_to_step(row: Any) -> TraceStep:
    """Convert an asyncpg record into a :class:`TraceStep`."""
    d = dict(row)
    verification = d.get("verification")
    input_val = d.get("input")
    output_val = d.get("output")
    return TraceStep(
        run_id=str(d.get("run_id") or ""),
        tenant_id=str(d["tenant_id"]) if d.get("tenant_id") else None,
        step_index=int(d.get("step_index") or 0),
        step_type=str(d.get("step_type") or DEFAULT_STEP_TYPE),
        tool=d.get("tool"),
        status=d.get("status"),
        artifact_ref=d.get("artifact_ref"),
        verification=verification if isinstance(verification, dict) else None,
        latency_ms=float(d["latency_ms"]) if d.get("latency_ms") is not None else None,
        attempt=int(d["attempt"]) if d.get("attempt") is not None else None,
        actor_user_id=d.get("actor_user_id"),
        input=input_val if isinstance(input_val, dict) else {},
        output=output_val if isinstance(output_val, dict) else {},
        created_at=str(d.get("created_at")) if d.get("created_at") is not None else "",
    )


class PgTraceStore:
    """asyncpg-backed :class:`TraceStore` over the ``run_steps`` table.

    A pool may be injected (tests); otherwise the shared application pool is
    used. JSONB params are passed as Python objects so the connection-level
    JSON/JSONB codec serialises them once (passing a pre-``json.dumps`` string
    would double-encode).
    """

    def __init__(self, pool: Any | None = None) -> None:
        self._pool = pool

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        from forgeflow.database import get_pool

        return await get_pool()

    async def insert_step(self, step: TraceStep) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO run_steps
                  (run_id, tenant_id, step_index, step_type, tool, input, output,
                   latency_ms, status, artifact_ref, verification, actor_user_id,
                   attempt, created_at)
                VALUES ($1::uuid,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,
                        $9,$10,$11::jsonb,$12,$13,$14)
                """,
                step.run_id,
                step.tenant_id,
                step.step_index,
                step.step_type,
                step.tool,
                step.input or {},
                step.output or {},
                step.latency_ms,
                step.status,
                step.artifact_ref,
                step.verification,
                step.actor_user_id,
                step.attempt,
                _parse_ts(step.created_at),
            )

    async def list_for_run(
        self, tenant_id: str | None, run_id: str, *, limit: int = 500
    ) -> list[TraceStep]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT run_id, tenant_id, step_index, step_type, tool, input,
                       output, latency_ms, status, artifact_ref, verification,
                       actor_user_id, attempt, created_at
                FROM run_steps
                WHERE run_id = $1::uuid
                  AND tenant_id IS NOT DISTINCT FROM $2
                ORDER BY step_index ASC, created_at ASC
                LIMIT $3
                """,
                run_id,
                tenant_id,
                int(limit),
            )
        return [_row_to_step(r) for r in rows]

    async def list_for_tenant(
        self, tenant_id: str | None, *, limit: int = 1000
    ) -> list[TraceStep]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT run_id, tenant_id, step_index, step_type, tool, input,
                       output, latency_ms, status, artifact_ref, verification,
                       actor_user_id, attempt, created_at
                FROM run_steps
                WHERE tenant_id IS NOT DISTINCT FROM $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                tenant_id,
                int(limit),
            )
        return [_row_to_step(r) for r in rows]


# --------------------------------------------------------------------------- #
# public write / read API                                                      #
# --------------------------------------------------------------------------- #
async def persist_invocation(
    invocation: dict[str, Any],
    *,
    args: dict[str, Any] | None,
    run_id: str,
    tenant_id: str | None,
    step_id: str | None = None,
) -> None:
    """Materialise one invocation into ``run_steps`` — best-effort, never a gate.

    Returns ``None`` unconditionally. Every reason *not* to write is logged
    verbatim and the call still returns:

      * no active store (``memory`` backend / persistence disabled) → no-op;
      * missing ``tenant_id`` → refused (fail-closed — no fabricated owner);
      * non-UUID ``run_id`` → refused (``run_steps.run_id`` is ``UUID NOT NULL``;
        never coerced, never truncated);
      * any SQL/transport error → swallowed (the run's result is already
        decided and must not be disturbed).
    """
    store = _active_store()
    if store is None:
        return

    if not tenant_id:
        logger.warning(
            "INC46 trace: skip run_steps write — no tenant_id "
            "(run_id=%s, step_id=%s); fail-closed",
            run_id,
            step_id,
        )
        return

    canonical_run_id = _canonical_uuid(run_id)
    if canonical_run_id is None:
        logger.warning(
            "INC46 trace: skip run_steps write — run_id %r is not a UUID "
            "(step_id=%s); run_steps.run_id is UUID NOT NULL, never coerced",
            run_id,
            step_id,
        )
        return

    try:
        step = to_row(
            invocation,
            args=args,
            step_index=_resolve_step_index(canonical_run_id, step_id),
            tenant_id=tenant_id,
        )
        await store.insert_step(step)
    except Exception:  # noqa: BLE001 — the trace must never break a run
        logger.warning(
            "INC46 trace: run_steps persistence skipped (best-effort) "
            "(run_id=%s, step_id=%s)",
            run_id,
            step_id,
            exc_info=True,
        )


async def list_for_run(
    tenant_id: str | None, run_id: str, *, limit: int = 500
) -> list[TraceStep]:
    """Return a run's materialised steps, tenant-scoped (fail-closed on tenant).

    An unresolved tenant yields ``[]`` (never every tenant's rows); a non-UUID
    ``run_id`` yields ``[]`` (it cannot exist in a ``UUID`` column). A store or
    transport error propagates — an empty trace must not masquerade as a read
    failure.
    """
    if not tenant_id:
        return []
    canonical_run_id = _canonical_uuid(run_id)
    if canonical_run_id is None:
        return []
    store = _active_store()
    if store is None:
        return []
    return await store.list_for_run(tenant_id, canonical_run_id, limit=limit)


async def list_for_tenant(
    tenant_id: str | None, *, limit: int = 1000
) -> list[TraceStep]:
    """Return a tenant's recent steps, newest first (fail-closed on tenant)."""
    if not tenant_id:
        return []
    store = _active_store()
    if store is None:
        return []
    return await store.list_for_tenant(tenant_id, limit=limit)
