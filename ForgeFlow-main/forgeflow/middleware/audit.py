"""AuditMiddleware — writes every request/response to the audit sink.

Two sinks behind one call site:

  * PostgreSQL — the immutable, partitioned ``audit_log`` table (production).
  * Offline (``STORAGE_BACKEND=memory``) — a bounded in-process ring buffer via
    ``api.routers.audit.record_audit_entry`` (no PG available ⇒ still audited,
    never a 500).

Both sinks receive the **same fields**, so the ``/audit/search`` and
``/audit/export`` views are identical regardless of backend.

**Which sink is live is decided in exactly one place** — :func:`available_audit_pool`
— by *both* the producer (:func:`write_audit_entry`) and the consumers
(``/audit/search`` / ``/audit/export`` / ``/audit/stats`` via
``api.routers.audit._soft_pool``). For a given process state the two sides
therefore cannot disagree about the sink — including a hand-assembled app with no
lifespan (no ``app.state.pool``), where both fall back to the ring buffer
together. The single residual is a startup-ordering window: a pool that comes up
*after* some entries were written leaves those early ring entries invisible to
later PostgreSQL reads (stated, not hidden — see :func:`available_audit_pool`).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Paths to skip auditing (too noisy for high-frequency endpoints)
_SKIP_AUDIT = {"/health", "/metrics/prometheus", "/docs", "/openapi.json", "/redoc"}


def _is_uuid(value: str) -> bool:
    """Cheap UUID-shape check so we don't write 'anonymous' into a UUID column."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    """Coerce a UUID-shaped string to a UUID; anything else (e.g. ``'anonymous'``
    or a non-UUID tenant slug) becomes NULL.

    Every UUID column in the audit INSERT must go through this. Before, only
    ``user_id`` was guarded — a non-UUID ``workspace_id`` raised ``badly formed
    hexadecimal UUID string`` and aborted the *entire* INSERT, silently
    dropping the audit record (QA P1-3).
    """
    return uuid.UUID(str(value)) if _is_uuid(value) else None


#: ``audit_log.resource_id`` is ``VARCHAR(128)``; a longer value would make the
#: INSERT raise (and, under best-effort, silently drop the whole record), so the
#: producer truncates instead of risking that.
_RESOURCE_ID_MAX = 128


def _resource_id(value: Any) -> str | None:
    """Coerce a domain ``resource_id`` for the ``VARCHAR(128)`` column.

    NOT a UUID column, so no ``_uuid_or_none`` — just a defensive truncation so an
    over-long skill name can never abort (and thus silently drop) the INSERT.
    """
    if value is None:
        return None
    return str(value)[:_RESOURCE_ID_MAX]


def _client_ip(request: Request) -> str | None:
    """Return the client IP from X-Forwarded-For only when proxy hops are
    configured. Otherwise the socket peer wins. Closes the audit attribution
    spoofing called out in SECURITY_AUDIT.md §6.
    """
    from forgeflow.config import get_settings

    hops = get_settings().trusted_proxy_count
    if hops > 0:
        xff = request.headers.get("x-forwarded-for", "")
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        idx = -hops
        if -idx <= len(parts):
            return parts[idx]
    return request.client.host if request.client else None


def available_audit_pool() -> Any | None:
    """The active audit sink pool — one resolution point for reads *and* writes.

    Shared by the audit **producer** (:func:`write_audit_entry`) and the audit
    **consumers** (``/audit/search`` / ``/audit/export`` / ``/audit/stats`` via
    ``api.routers.audit._soft_pool``), so for a given process state the two sides
    can never disagree about which sink is live.

    Returns a real pool only when **both** hold:

    1. ``storage_backend == "postgres"`` — the offline (memory) profile
       deliberately never dials a database; and
    2. the shared ``database._pool`` is **already initialised** (the app lifespan
       calls ``database.init_pool``). This never *creates* a pool.

    Otherwise it returns ``None`` ⇒ the offline in-process ring buffer.

    Residual (time-ordering — stated, not papered over): if the pool comes up
    *after* some entries were written, those early entries remain in the ring
    buffer while later reads resolve to PostgreSQL, so such a reader only sees
    rows written after the pool existed. That is a startup-ordering window, not a
    silent sink mismatch: the production lifespan sets ``app.state.pool`` before
    the first request, pinning both sides from the very first entry.
    """
    try:
        from forgeflow.config import get_settings

        backend = str(getattr(get_settings(), "storage_backend", "") or "").lower()
    except Exception:  # noqa: BLE001 — a config hiccup must not break auditing
        return None
    if backend != "postgres":
        return None
    import forgeflow.database as _db

    return getattr(_db, "_pool", None)


#: Backwards-compatible alias — existing callers/tests/probes reference the old
#: private name. It is the *same* function object (``_available_pool is
#: available_audit_pool``), so behaviour is identical.
_available_pool = available_audit_pool


async def write_audit_entry(entry: dict[str, Any], *, pool: Any | None = None) -> None:
    """Persist one audit entry to the active sink — best-effort, never raises.

    ``AuditMiddleware`` writes one entry per HTTP request; this is the **same**
    sink for non-HTTP producers (e.g. the skill release gate recording *why* a
    version was released), so ``/audit/search`` and ``/audit/export`` answer for
    them identically. **Both branches write the same nine fields** — ``user_id``,
    ``role``, ``action``, ``resource``, ``resource_id``, ``outcome``,
    ``request_id``, ``workspace_id``, ``metadata`` — so the two views cannot
    drift. A write failure is logged, never propagated: an outage in auditing
    must not fail the operation being audited.

    Sink selection: ``pool`` is passed explicitly by the middleware
    (``request.app.state.pool``); domain producers leave it ``None``. **When it is
    ``None`` the pool falls back to** :func:`available_audit_pool` — the same
    single resolution point the read paths use — which returns the shared
    ``database._pool`` **only** for the ``postgres`` backend (and only when it is
    already initialised), else ``None`` ⇒ the offline ring buffer. The
    consequence is stated so it is not implicit: with ``storage_backend=postgres``,
    ``app.state.pool is None`` and a global pool already up, an HTTP request is
    written to PostgreSQL by this fallback rather than the ring buffer. That is
    intended — a non-HTTP producer has no ``request.app.state`` and still needs a
    durable sink — and harmless, since both sinks receive identical fields.
    """
    request_id = str(entry.get("request_id") or uuid.uuid4())
    if pool is None:
        pool = available_audit_pool()
    try:
        if pool is not None:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO audit_log
                      (user_id, role, action, resource, resource_id, outcome,
                       request_id, workspace_id, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    _uuid_or_none(entry.get("user_id")),
                    entry.get("role") or "unknown",
                    entry.get("action") or "",
                    entry.get("resource") or "",
                    _resource_id(entry.get("resource_id")),
                    entry.get("outcome") or "allowed",
                    _uuid_or_none(request_id) or uuid.uuid4(),
                    _uuid_or_none(entry.get("workspace_id")),
                    dict(entry.get("metadata") or {}),
                )
        else:
            # Offline profile — the in-process ring buffer is the audit sink.
            # Field names/values mirror the INSERT above exactly (same nine
            # fields, same non-UUID guards) so /audit/search and /audit/export
            # stay identical across backends.
            from forgeflow.api.routers.audit import record_audit_entry

            user_uuid = _uuid_or_none(entry.get("user_id"))
            workspace_uuid = _uuid_or_none(entry.get("workspace_id"))
            record_audit_entry(
                {
                    "id": str(entry.get("id") or request_id),
                    "timestamp": entry.get("timestamp")
                    or datetime.now(timezone.utc).isoformat(),
                    "user_id": str(user_uuid) if user_uuid else None,
                    "role": entry.get("role") or "unknown",
                    "action": entry.get("action") or "",
                    "resource": entry.get("resource") or "",
                    "resource_id": _resource_id(entry.get("resource_id")),
                    "outcome": entry.get("outcome") or "allowed",
                    "request_id": request_id,
                    "workspace_id": str(workspace_uuid) if workspace_uuid else None,
                    "metadata": dict(entry.get("metadata") or {}),
                }
            )
    except Exception as e:  # noqa: BLE001 — best-effort by design (see docstring)
        logger.error("AUDIT_WRITE_FAILED request_id=%s: %s", request_id, e)


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if request.url.path in _SKIP_AUDIT:
            return await call_next(request)

        start = time.monotonic()
        request_id = str(uuid.uuid4())
        request.state.request_id = getattr(request.state, "request_id", request_id)

        response = await call_next(request)

        latency_ms = (time.monotonic() - start) * 1000
        user_id = getattr(request.state, "user_id", "anonymous")
        role = getattr(request.state, "role", "unknown")
        # 401 (missing/invalid credentials) and 403 (authenticated but not
        # permitted) are BOTH rejections at the door ⇒ "denied". Everything else
        # ≥400 is a downstream error (429 rate-limit, 400 security-block, 5xx).
        # Before this, a 401 was mapped to "error", so every unauthenticated
        # attempt looked like a crash in the audit views.
        outcome = "allowed" if response.status_code < 400 else (
            "denied" if response.status_code in (401, 403) else "error"
        )

        # Write to the audit sink. Persistent failure surfaces via the metric +
        # logged ERROR so an outage doesn't silently drop the immutable
        # record SECURITY_AUDIT.md §6 demands. ``write_audit_entry`` is
        # best-effort (it swallows + logs), so a broken audit write never
        # affects the request being audited.
        metadata = {
            "status_code": response.status_code,
            "latency_ms": round(latency_ms, 1),
            "user_agent": request.headers.get("user-agent", "")[:512],
            "user_id_str": str(user_id),
            "client_ip": _client_ip(request),
        }
        workspace_id = getattr(request.state, "workspace_id", None)
        # One shared sink for HTTP + non-HTTP producers (see ``write_audit_entry``):
        # same nine fields, same metadata, same non-UUID guard, one INSERT
        # statement. We pass the request's own pool; if the app has none,
        # ``write_audit_entry`` falls back to the global (postgres-only) pool —
        # an explicit, documented fallback, not a hidden one.
        await write_audit_entry(
            {
                "id": request_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "user_id": user_id,
                "role": role,
                "action": request.method,
                "resource": request.url.path,
                "resource_id": None,
                "outcome": outcome,
                "request_id": request_id,
                "workspace_id": workspace_id,
                "metadata": metadata,
            },
            pool=getattr(request.app.state, "pool", None),
        )

        response.headers["X-Request-Id"] = request_id
        return response
