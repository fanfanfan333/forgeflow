"""Per-call authorisation for **write** MCP tools.

Why this module exists
----------------------
Until now every deployment had to declare ``read_only: true`` for an MCP tool,
and :mod:`forgeflow.mcp.auth` refused anything else. That made the whole write
half of the MCP surface unreachable — the platform could *detect* a write tool
but never *call* it, so "MCP only supports read-only tools" was a structural
limit, not a policy choice.

Flipping that boolean would have been the easy (and wrong) fix: a token minted
once per session would then authorise unlimited writes for its 120 s lifetime.
A write needs three independent facts, and this module owns all three:

1. **A human decision that already happened.** The ticket can only be minted
   from an ``agent_approvals`` row with ``requested_action == "mcp.write:<tool>"``
   in status ``approved``, decided by a named approver, inside
   ``mcp_write_approval_window_seconds``. The first blocked attempt *creates* the
   pending row (``request_approval``), so the operator sees the request instead
   of a silent denial; only the approvals API can turn it into ``approved``.
2. **Binding to the exact payload, twice.** The approval row carries
   ``payload_digest`` — the ``sha256:`` of the arguments the approver was shown,
   next to a redacted preview in ``note`` — and the gate refuses any call whose
   digest differs (``no_approved_request``). The ticket then repeats the same
   digest, so a call that swaps the payload after approval is refused at dispatch
   (``argument_mismatch``). Approve "send to a@b", call with "send to c@d" ⇒
   nothing runs, at either layer.
3. **Single use, short life.** ``jti`` is burnt on first dispatch; the ticket
   lives ``mcp_write_ticket_ttl_seconds`` (default 30 s, always shorter than the
   token's 120 s). A retry after a *denied or failed* write needs a fresh ticket,
   because the burnt ticket can never be replayed.

Why a window and not a run id: a halted run is resumed through
``POST /runs/{id}/replan``, which mints a *new* run id — a run-bound approval
could therefore never be consumed. The window bounds reuse instead (default
15 min), and it is the only reuse the ticket mechanism allows; each call still
burns its own payload-bound ticket and lands in the audit ring below.

Residual, documented limit: the burn ledger lives in the MCP server process.
A server restart forgets burnt tickets, so an unconsumed ticket stays usable
until it expires (≤ ``mcp_write_ticket_ttl_seconds``). That window is bounded by
construction — it is not an open replay hole — and every decision, including the
rejections, is written to the bounded audit ring below so the window is
observable rather than assumed away.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DIGEST_PREFIX = "sha256:"
APPROVAL_KEY_PREFIX = "mcp.write:"
MAX_TICKETS_PER_TOKEN = 4
AUDIT_RING = 500

#: Reasons are part of the contract: callers and tests branch on them, and the
#: audit trail must be readable without guessing.
MISSING = "missing_ticket"
TOOL_MISMATCH = "tool_mismatch"
ARGUMENT_MISMATCH = "argument_mismatch"
EXPIRED = "expired"
ALREADY_USED = "already_used"
NO_APPROVAL = "no_approved_request"
OK = "ok"


def args_digest(arguments: Any) -> str:
    """Stable digest of a tool-call payload.

    Sorted keys + compact separators so two semantically identical payloads
    hash the same regardless of the model's key order, and ``default=str`` so an
    exotic value never raises (a digest that raises would fail *open* — the
    caller would have to skip the binding check).
    """
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)
    return DIGEST_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: Keys whose *values* must never reach the approval inbox verbatim. Matched on
#: a lower-cased substring of the key, so ``x-api-key`` and ``smtp_password``
#: are both covered. Deliberately a substring test and not a regex: a regex over
#: attacker-controlled keys is how ReDoS gets in.
_SECRET_HINTS = (
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "access_key",
    "cookie",
    "session",
)

#: A preview exists to let a human judge the *shape* of the write, not to copy
#: a document into an approval inbox. Every limit below is enforced while
#: walking, so a pathological payload costs linear time and bounded memory.
PREVIEW_MAX_CHARS = 1200
PREVIEW_MAX_STRING = 120
PREVIEW_MAX_ITEMS = 8
PREVIEW_MAX_DEPTH = 3
PREVIEW_REDACTED = "[redacted]"


def _preview_scalar(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) <= PREVIEW_MAX_STRING:
            return value
        head = value[:PREVIEW_MAX_STRING]
        return f"{head}…(len={len(value)})"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    # bytes / datetime / exotic — str() is bounded by the caller's slice below.
    return str(value)[:PREVIEW_MAX_STRING]


def _preview(value: Any, *, depth: int, budget: list[int]) -> Any:
    """Redacted, bounded, depth-limited view of a tool payload."""
    if budget[0] <= 0:
        return "…"
    if isinstance(value, dict):
        if depth >= PREVIEW_MAX_DEPTH:
            return "{…}"
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= PREVIEW_MAX_ITEMS:
                out[f"…+{len(value) - PREVIEW_MAX_ITEMS}"] = "more keys"
                break
            name = str(key)
            if any(hint in name.lower() for hint in _SECRET_HINTS):
                out[name] = PREVIEW_REDACTED
                budget[0] -= len(name) + len(PREVIEW_REDACTED)
                continue
            budget[0] -= len(name)
            out[name] = _preview(item, depth=depth + 1, budget=budget)
            if budget[0] <= 0:
                break
        return out
    if isinstance(value, (list, tuple)):
        if depth >= PREVIEW_MAX_DEPTH:
            return "[…]"
        items = [
            _preview(item, depth=depth + 1, budget=budget)
            for item in list(value)[:PREVIEW_MAX_ITEMS]
        ]
        if len(value) > PREVIEW_MAX_ITEMS:
            items.append(f"…+{len(value) - PREVIEW_MAX_ITEMS}")
        return items
    scaled = _preview_scalar(value)
    budget[0] -= len(str(scaled))
    return scaled


def payload_preview(arguments: Any) -> str:
    """One-line, redacted JSON for the approval inbox.

    This is what the human reads before deciding, so it is deliberately a
    *human* artefact: secrets are replaced, long strings are truncated with their
    real length, and the whole thing is capped. It is never used as an
    authorisation input — :func:`args_digest` is.
    """
    try:
        preview = _preview(arguments or {}, depth=0, budget=[PREVIEW_MAX_CHARS])
        text = json.dumps(preview, ensure_ascii=False, sort_keys=True, default=str)
    except Exception as exc:  # noqa: BLE001 — a preview must never fail the gate
        return f"<unrenderable payload: {type(exc).__name__}>"
    if len(text) > PREVIEW_MAX_CHARS:
        return text[:PREVIEW_MAX_CHARS] + f"…(+{len(text) - PREVIEW_MAX_CHARS})"
    return text


class WriteApprovalRequired(RuntimeError):
    """A write call was attempted without a live, matching human approval.

    Deliberately a ``RuntimeError`` subclass raised *before* dispatch: the
    executor records it as a refusal (``blocked``), never as a success, so an
    unapproved write can never be reported to the user as done.
    """


@dataclass(frozen=True)
class WriteTicket:
    """One signed, single-use authorisation for one write call."""

    id: str
    tool: str
    digest: str
    approval_id: str = ""
    tenant: str = ""
    actor: str = ""
    run_id: str = ""
    expires_at: float = 0.0

    def to_claim(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "digest": self.digest,
            "approval": self.approval_id,
            "exp": int(self.expires_at),
        }

    @classmethod
    def from_claim(cls, claim: Any) -> WriteTicket | None:
        """Parse a claim defensively; ``None`` means "not a usable ticket"."""
        if not isinstance(claim, dict):
            return None
        keys = ("id", "tool", "digest")
        if any(not isinstance(claim.get(key), str) or not claim.get(key) for key in keys):
            return None
        digest = claim["digest"]
        if not digest.startswith(DIGEST_PREFIX):
            return None
        try:
            expires = float(claim.get("exp", 0))
        except (TypeError, ValueError):
            return None
        if expires <= 0:
            return None
        approval_id = claim.get("approval", "")
        if not isinstance(approval_id, str):
            return None
        return cls(
            id=claim["id"],
            tool=claim["tool"],
            digest=digest,
            approval_id=approval_id,
            expires_at=expires,
        )


@dataclass(frozen=True)
class TicketVerdict:
    ok: bool
    reason: str
    tool: str = ""
    ticket_id: str = ""
    approval_id: str = ""

    @property
    def detail(self) -> str:
        return f"{self.tool or '?'}:{self.reason}"


class TicketLedger:
    """Burns a ticket id the first time it is accepted.

    Thread-safe because the MCP middleware may run on several event loops
    (anyio worker threads); entries are pruned by expiry and the map is capped
    so a hostile client cannot grow it without bound.
    """

    def __init__(self, *, capacity: int = 4096) -> None:
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()
        self._capacity = capacity

    def consume(
        self, claim: Any, *, tool: str, digest: str, now: float | None = None
    ) -> TicketVerdict:
        ticket = WriteTicket.from_claim(claim)
        if ticket is None:
            return TicketVerdict(False, MISSING)
        moment = time.time() if now is None else now
        if ticket.expires_at <= moment:
            return TicketVerdict(False, EXPIRED, ticket.tool, ticket.id, ticket.approval_id)
        if ticket.tool != tool:
            return TicketVerdict(False, TOOL_MISMATCH, ticket.tool, ticket.id, ticket.approval_id)
        if ticket.digest != digest:
            return TicketVerdict(
                False, ARGUMENT_MISMATCH, ticket.tool, ticket.id, ticket.approval_id
            )
        with self._lock:
            if ticket.id in self._used:
                return TicketVerdict(
                    False, ALREADY_USED, ticket.tool, ticket.id, ticket.approval_id
                )
            self._used[ticket.id] = ticket.expires_at
            if len(self._used) > self._capacity:
                for key, _ in sorted(self._used.items(), key=lambda item: item[1])[
                    : len(self._used) - self._capacity
                ]:
                    self._used.pop(key, None)
        return TicketVerdict(True, OK, ticket.tool, ticket.id, ticket.approval_id)

    def forget(self, ticket_id: str) -> None:
        with self._lock:
            self._used.pop(ticket_id, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._used)


#: The server-side burn ledger. One per process, matching one MCP server.
LEDGER = TicketLedger()


@dataclass
class _AuditEntry:
    at: float
    phase: str
    outcome: str
    tenant: str = ""
    actor: str = ""
    tool: str = ""
    digest: str = ""
    approval_id: str = ""
    run_id: str = ""
    detail: str = ""


AUDIT: deque[_AuditEntry] = deque(maxlen=AUDIT_RING)


def record(
    phase: str,
    outcome: str,
    *,
    tenant: str = "",
    actor: str = "",
    tool: str = "",
    digest: str = "",
    approval_id: str = "",
    run_id: str = "",
    detail: str = "",
) -> _AuditEntry:
    """Append one observable decision (accept *and* reject) to the audit ring."""
    entry = _AuditEntry(
        at=time.time(),
        phase=phase,
        outcome=outcome,
        tenant=tenant,
        actor=actor,
        tool=tool,
        digest=digest,
        approval_id=approval_id,
        run_id=run_id,
        detail=detail,
    )
    AUDIT.append(entry)
    level = logging.WARNING if outcome not in {OK, "minted"} else logging.INFO
    logger.log(
        level,
        "mcp.write.%s | outcome=%s tenant=%s actor=%s tool=%s approval=%s detail=%s",
        phase,
        outcome,
        tenant or "-",
        actor or "-",
        tool or "-",
        approval_id or "-",
        entry.detail or "-",
    )
    return entry


def recent_audit(limit: int = 50) -> list[dict[str, Any]]:
    """Newest-last view of the ring (JSON-safe) — for probes and tests."""
    items = list(AUDIT)[-limit:]
    return [
        {
            "at": item.at,
            "phase": item.phase,
            "outcome": item.outcome,
            "tenant": item.tenant,
            "actor": item.actor,
            "tool": item.tool,
            "digest": item.digest,
            "approval_id": item.approval_id,
            "run_id": item.run_id,
            "detail": item.detail,
        }
        for item in items
    ]


def clear_audit() -> None:
    AUDIT.clear()


def _window_seconds() -> float:
    from forgeflow.config import get_settings

    return float(get_settings().mcp_write_approval_window_seconds)


def approval_key(tool: str) -> str:
    """The exact ``agent_approvals.requested_action`` that authorises one tool.

    Exact equality on both sides — no substring/segment matching. An approval
    for ``email_send`` can therefore never authorise ``slack_send`` just because
    both end in "send".
    """
    return APPROVAL_KEY_PREFIX + tool


def _stamp_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value.timestamp())
    except (AttributeError, TypeError, ValueError, OSError):
        return None


async def find_approved_write(tenant: str | None, actor: str, tool: str, digest: str) -> Any | None:
    """The approval that lets ``tool`` write **this payload** right now, or ``None``.

    Accepted only when **all** of these hold: same tenant, ``status=approved``,
    ``decision=approve``, ``requested_action`` exactly this tool's key, a named
    approver, the decision is inside ``mcp_write_approval_window_seconds``, and
    ``payload_digest`` equals the arguments about to be sent.

    The last condition is the difference between "a human approved this write"
    and "a human approved this tool". A row with an empty ``payload_digest``
    (written before migration 043) names no payload, so it is refused and logged
    as ``legacy_approval`` — never widened to a wildcard.

    The window (not the run id) is what makes this usable: a halted run is
    resumed through ``POST /runs/{id}/replan``, which mints a *new* run id, so a
    run-bound approval could never be consumed. The window bounds the reuse
    instead, and each individual call still burns its own payload-bound ticket.
    """
    if not tenant or not tool:
        return None
    from forgeflow.repositories import get_policy_repository

    repo = get_policy_repository()
    try:
        rows = await repo.list_approvals(tenant, status="approved", limit=200)
    except Exception as exc:  # noqa: BLE001 — an unreadable store must deny, not allow
        record(
            "lookup",
            "store_error",
            tenant=tenant,
            actor=actor,
            tool=tool,
            detail=f"{type(exc).__name__}: {exc}"[:200],
        )
        return None
    cutoff = time.time() - _window_seconds()
    legacy = False
    for row in rows:
        if str(getattr(row, "requested_action", "") or "") != approval_key(tool):
            continue
        if str(getattr(row, "status", "") or "") != "approved":
            continue
        if str(getattr(row, "decision", "") or "") != "approve":
            continue
        if not str(getattr(row, "approver", "") or "").strip():
            continue
        stamp = _stamp_seconds(getattr(row, "resolved_at", None)) or _stamp_seconds(
            getattr(row, "created_at", None)
        )
        if stamp is None or stamp < cutoff:
            continue
        approved_digest = str(getattr(row, "payload_digest", "") or "")
        if not approved_digest:
            legacy = True
            continue
        if approved_digest != digest:
            continue
        return row
    if legacy:
        record(
            "lookup",
            "legacy_approval",
            tenant=tenant,
            actor=actor,
            tool=tool,
            digest=digest,
            detail="approval names no payload (pre-043); re-approve to bind it",
        )
    return None


async def request_approval(
    *,
    tenant: str | None,
    actor: str,
    run_id: str,
    tool: str,
    arguments: Any = None,
    reason: str = "",
) -> str | None:
    """Create the pending decision an operator has to make — at most one per payload.

    Without this the write gate would only ever *deny*: nothing else in the
    platform raises an ``mcp.write:`` approval, so the human would never see the
    request and the capability would be dead on arrival.

    The row records **what** would be written: ``payload_digest`` is the binding
    the gate later checks, and ``note`` carries a redacted, bounded preview so the
    approver judges the actual payload instead of a tool name. Idempotent per
    ``(run, tool, digest)``: a retrying agent cannot flood the approval centre,
    but a *different* payload legitimately needs its own decision.
    """
    if not tenant or not tool:
        return None
    from forgeflow.governance.models import ApprovalRecord
    from forgeflow.repositories import get_policy_repository

    repo = get_policy_repository()
    key = approval_key(tool)
    digest = args_digest(arguments)
    try:
        pending = await repo.list_approvals(tenant, status="pending", limit=200)
    except Exception:  # noqa: BLE001 — discovery failure must not create duplicates
        pending = []
    for row in pending:
        if (
            str(getattr(row, "run_id", "") or "") == str(run_id or "")
            and str(getattr(row, "requested_action", "") or "") == key
            and str(getattr(row, "payload_digest", "") or "") == digest
        ):
            return str(getattr(row, "id", "") or "")
    preview = payload_preview(arguments)
    note = reason or f"MCP write '{tool}' needs a human decision before it runs"
    approval = ApprovalRecord(
        tenant_id=tenant,
        run_id=str(run_id or "") or None,
        risk_level="high",
        requested_action=key,
        requester=actor,
        status="pending",
        note=f"{note}\npayload: {preview}"[:2000],
        payload_digest=digest,
    )
    try:
        saved = await repo.save_approval(approval)
    except Exception as exc:  # noqa: BLE001 — report the refusal, never fake approval
        record(
            "request",
            "store_error",
            tenant=tenant,
            actor=actor,
            tool=tool,
            digest=digest,
            run_id=str(run_id or ""),
            detail=f"{type(exc).__name__}: {exc}"[:200],
        )
        return None
    record(
        "request",
        "pending",
        tenant=tenant,
        actor=actor,
        tool=tool,
        digest=digest,
        approval_id=str(getattr(saved, "id", "") or ""),
        run_id=str(run_id or ""),
    )
    return str(getattr(saved, "id", "") or "")


def mint_ticket(
    tool: str,
    arguments: Any,
    *,
    approval_id: str,
    tenant: str,
    actor: str,
    run_id: str,
    ttl_seconds: float | None = None,
) -> WriteTicket:
    """Build the claim the caller puts in its per-call token."""
    if ttl_seconds is None:
        from forgeflow.config import get_settings

        ttl_seconds = float(get_settings().mcp_write_ticket_ttl_seconds)
    expires = time.time() + max(1.0, float(ttl_seconds))
    ticket = WriteTicket(
        id=uuid.uuid4().hex,
        tool=tool,
        digest=args_digest(arguments),
        approval_id=str(approval_id or ""),
        tenant=tenant,
        actor=actor,
        run_id=str(run_id or ""),
        expires_at=expires,
    )
    record(
        "mint",
        "minted",
        tenant=tenant,
        actor=actor,
        tool=tool,
        digest=ticket.digest,
        approval_id=ticket.approval_id,
        run_id=ticket.run_id,
        detail=ticket.id,
    )
    return ticket


async def authorize_write(
    *,
    tenant: str | None,
    actor: str,
    role: str,
    run_id: str,
    tool: str,
    arguments: Any,
) -> WriteTicket:
    """Server-side (`control plane`) authorisation for one write call.

    Raises :class:`WriteApprovalRequired` — never returning a make-believe
    ticket — when the approval is missing, or when the identity lacks the
    role/tenant grant in the first place.
    """
    from forgeflow.mcp.auth import approved_grant

    grant = approved_grant(tool, tenant or "", role)
    if grant is None or grant.get("read_only") is True:
        record(
            "authorize",
            "not_granted",
            tenant=tenant or "",
            actor=actor,
            tool=tool,
            run_id=run_id,
        )
        raise WriteApprovalRequired(f"MCP write tool '{tool}' is not granted for this identity")
    digest = args_digest(arguments)
    approval = await find_approved_write(tenant, actor, tool, digest)
    if approval is None:
        pending = await request_approval(
            tenant=tenant,
            actor=actor,
            run_id=run_id,
            tool=tool,
            arguments=arguments,
        )
        record(
            "authorize",
            NO_APPROVAL,
            tenant=tenant or "",
            actor=actor,
            tool=tool,
            digest=digest,
            run_id=run_id,
            detail=f"pending={pending or 'none'}",
        )
        raise WriteApprovalRequired(
            f"MCP write tool '{tool}' needs an approved request before it can run"
        )
    ticket = mint_ticket(
        tool,
        arguments,
        approval_id=str(getattr(approval, "id", "") or ""),
        tenant=tenant or "",
        actor=actor,
        run_id=run_id,
    )
    record(
        "authorize",
        OK,
        tenant=tenant or "",
        actor=actor,
        tool=tool,
        digest=ticket.digest,
        approval_id=ticket.approval_id,
        run_id=run_id,
    )
    return ticket
