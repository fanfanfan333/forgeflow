"""AuditMiddleware — writes every request/response to the audit sink.

Two sinks behind one call site:

  * PostgreSQL — the immutable, partitioned ``audit_log`` table (production).
  * Offline (``STORAGE_BACKEND=memory``) — a bounded in-process ring buffer via
    ``api.routers.audit.record_audit_entry`` (no PG available ⇒ still audited,
    never a 500). Both sinks receive the **same fields**, so the ``/audit/search``
    and ``/audit/export`` views are identical regardless of backend.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

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
        # record SECURITY_AUDIT.md §6 demands. The offline (memory) sink is
        # protected by the SAME try/except — a broken audit write must never
        # affect the request being audited.
        metadata = {
            "status_code": response.status_code,
            "latency_ms": round(latency_ms, 1),
            "user_agent": request.headers.get("user-agent", "")[:512],
            "user_id_str": str(user_id),
            "client_ip": _client_ip(request),
        }
        workspace_id = getattr(request.state, "workspace_id", None)
        try:
            pool = getattr(request.app.state, "pool", None)
            if pool:
                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO audit_log
                          (user_id, role, action, resource, outcome, request_id,
                           workspace_id, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        _uuid_or_none(user_id),
                        role,
                        request.method,
                        request.url.path,
                        outcome,
                        uuid.UUID(request_id),
                        # Non-UUID claims (e.g. tenant slug "acme") must not blow
                        # up the whole INSERT — write NULL and keep the record.
                        _uuid_or_none(workspace_id),
                        metadata,
                    )
            else:
                # Offline profile — no PostgreSQL, so the in-process ring buffer
                # is the audit sink. Field names/values mirror the INSERT above
                # (incl. the ``_uuid_or_none`` non-UUID guard) so /audit/search
                # and /audit/export stay identical across backends.
                from forgeflow.api.routers.audit import record_audit_entry

                record_audit_entry(
                    {
                        "id": request_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "user_id": str(_uuid_or_none(user_id)) if _uuid_or_none(user_id) else None,
                        "role": role,
                        "action": request.method,
                        "resource": request.url.path,
                        "resource_id": None,
                        "outcome": outcome,
                        "request_id": request_id,
                        "workspace_id": str(_uuid_or_none(workspace_id))
                        if _uuid_or_none(workspace_id)
                        else None,
                        "metadata": metadata,
                    }
                )
        except Exception as e:
            # Best-effort by design — never fail the request because audit
            # is down. The ERROR log line is the alerting hook.
            logger.error("AUDIT_WRITE_FAILED request_id=%s: %s", request_id, e)

        response.headers["X-Request-Id"] = request_id
        return response
