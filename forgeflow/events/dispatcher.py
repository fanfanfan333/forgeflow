"""Event -> workflow dispatcher.

Sits between an event-source consumer (Redis Streams, Kafka, ...) and the
existing workflow pipelines. The consumer hands each raw event to the
dispatcher; the dispatcher validates, routes by `workflow_type`, and
invokes the right SalesOpsPipeline / SupportOpsPipeline / FinanceReconPipeline.

Why a separate dispatcher: it lets us drop in new event sources without
re-implementing the pipeline-selection logic three times. The same
dispatcher serves Redis today and Kafka tomorrow.

Event envelope contract:
    {
      "workflow_type": "sales_ops" | "support_ops" | "finance_recon",
      "lead_data":     {<domain-specific payload>},
      "user_id":       "<optional>",
      "role":          "<optional>",
      "dry_run":       false                          # optional
    }

INC2-23 (docs §2.14) adds a second, AgentFlow-native envelope that drives a hub
Run instead of a legacy pipeline:

    {
      "task":     {"intent": "分析华东销售数据", "context": {...}},
      "user_id":  "<optional>",
      "role":     "<optional>",
      "tenant_id": "<optional>"
    }

(``{"workflow_type": "agentflow_task", "task": {...}}`` is accepted too.) The
three legacy workflow branches are left byte-for-byte unchanged.

Trust model for the task envelope (the legacy branches take no tenant and
are unchanged): an envelope is just bytes on a queue — anyone who can
publish can *claim* any tenant and any role, so declared identity is
never trusted on its own.

* When ``EVENT_SIGNING_SECRET`` is configured, a task event MUST carry
  a valid ``signature`` (HMAC-SHA256 hex over the canonical identity +
  payload, see :func:`sign_task_event`).  Only then are the declared
  ``tenant_id`` / ``user_id`` honored — and the role is still resolved
  server-side to the fixed least-privilege ``service`` role
  (``forgeflow.rbac.policies``: the narrow identity that "the cron /
  dispatcher needs", ``execute:workflows`` only).  An envelope-declared
  role is never used for a trusted event, so it cannot elevate.
* When no secret is configured, the entrypoint is untrusted by
  default: task events are REJECTED (fail closed) rather than executed
  under a forged identity — except in the explicit single-process
  dev/test mode (``app_env=dev``, the same signal that gates the
  in-memory run-lease backend), where the legacy unsigned behavior is
  kept for local development and a warning is logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from forgeflow.workflows.finance_recon.models import ReconciliationInput
from forgeflow.workflows.finance_recon.pipeline import FinanceReconPipeline
from forgeflow.workflows.sales_ops.models import LeadInput
from forgeflow.workflows.sales_ops.pipeline import SalesOpsPipeline
from forgeflow.workflows.support_ops.models import TicketInput
from forgeflow.workflows.support_ops.pipeline import SupportOpsPipeline

logger = logging.getLogger(__name__)

# The envelope that routes to ``runtime.orchestrator.run_task`` (INC2-23).
TASK_WORKFLOW_TYPE = "agentflow_task"
_LEGACY_WORKFLOW_TYPES = ("sales_ops", "support_ops", "finance_recon")

#: Envelope field carrying the HMAC signature for task events.
_EVENT_SIGNATURE_FIELD = "signature"

#: The role a *trusted* task event runs under.  Resolved server-side —
#: never taken from the envelope — to the RBAC ``service`` identity
#: (forgeflow.rbac.policies: "Narrow service identity — pick exactly
#: what the cron / dispatcher needs": read/execute workflows, nothing
#: else).  A signed event's producer asserts *who* the user is; it
#: cannot assert a more privileged role than this.
EVENT_TASK_ROLE = "service"


def _canonical_event_bytes(
    *, payload: dict[str, Any], tenant_id: str | None, user_id: str, role: str
) -> bytes:
    """Canonical bytes an event signature covers: the declared identity
    (tenant, user, role) plus the full task payload, as one sorted-key
    JSON object.  Any change to a covered field invalidates the
    signature."""
    canonical = {
        "payload": payload,
        "role": role,
        "tenant_id": tenant_id,
        "user_id": user_id,
    }
    return json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_task_event(
    payload: dict[str, Any],
    *,
    tenant_id: str | None,
    user_id: str,
    role: str,
    secret: str,
) -> str:
    """Produce the ``signature`` value for a task envelope (producer side).

    Trusted producers sign with the deployment's ``EVENT_SIGNING_SECRET``
    and place the returned hex digest in the envelope's ``signature``
    field alongside ``task`` / ``user_id`` / ``role`` / ``tenant_id``.
    """
    return hmac.new(
        secret.encode("utf-8"),
        _canonical_event_bytes(
            payload=payload, tenant_id=tenant_id, user_id=user_id, role=role
        ),
        hashlib.sha256,
    ).hexdigest()


def _event_signing_secret() -> str:
    """The configured envelope-signing secret ("" when unconfigured)."""
    try:
        from forgeflow.config import get_settings

        secret = get_settings().event_signing_secret
        return secret.get_secret_value() if secret is not None else ""
    except Exception:  # settings unavailable → untrusted (fail closed)
        return ""


def _verify_task_event_signature(
    raw: dict[str, Any],
    payload: dict[str, Any],
    *,
    user_id: str,
    role: str,
) -> bool:
    """True iff the envelope carries a valid signature for its declared
    identity + payload under the configured secret.  False when no
    secret is configured (nothing can be verified) or the signature is
    missing/invalid."""
    secret = _event_signing_secret()
    signature = raw.get(_EVENT_SIGNATURE_FIELD)
    if not secret or not isinstance(signature, str) or not signature:
        return False
    tenant_id = raw.get("tenant_id") or payload.get("tenant_id")
    expected = sign_task_event(
        payload,
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        user_id=user_id,
        role=role,
        secret=secret,
    )
    return hmac.compare_digest(expected, signature)


@dataclass
class WorkflowTrigger:
    """A parsed, validated event ready for dispatch."""
    workflow_type: str
    payload: dict[str, Any]
    user_id: str = "events"
    role: str = "system"
    dry_run: bool = False
    source_event_id: str | None = None      # the upstream queue's message id
    extra: dict[str, Any] = field(default_factory=dict)
    #: Task events only: the envelope's signature verified against its
    #: declared tenant/user/role + payload under the configured
    #: EVENT_SIGNING_SECRET.  Parsing records the declared identity
    #: either way; *dispatch* decides whether it may be honored.
    signature_valid: bool = False


@dataclass
class DispatchResult:
    ok: bool
    workflow_id: str | None = None
    thread_id: str | None = None
    error: str | None = None


class EventDispatcher:
    """Routes WorkflowTrigger events to the right pipeline.

    Holds references to compiled graphs (one per workflow_type) so it
    doesn't pay the compile cost per event.
    """

    def __init__(self, graphs: dict[str, Any]) -> None:
        self.graphs = graphs

    @staticmethod
    def parse_event(raw: dict[str, Any], source_event_id: str | None = None) -> WorkflowTrigger:
        """Parse + validate an event envelope. Raises ValueError on malformed input."""
        if not isinstance(raw, dict):
            raise ValueError("event must be a JSON object")

        wf = raw.get("workflow_type")
        task_payload = raw.get("task")

        # INC2-23: the AgentFlow-native {task:{intent}} envelope (or an explicit
        # workflow_type="agentflow_task"). Detected BEFORE the legacy validation
        # so the three workflow branches below stay untouched. A ``task`` object
        # with no intent is still routed here so it gets a precise error.
        if isinstance(task_payload, dict) or wf == TASK_WORKFLOW_TYPE:
            payload = task_payload if isinstance(task_payload, dict) else None
            if payload is None:
                payload = raw.get("lead_data") or raw.get("payload")
            if not isinstance(payload, dict) or not payload.get("intent"):
                raise ValueError("agentflow_task event missing {task: {intent}}")
            user_id = str(raw.get("user_id", "events"))
            role = str(raw.get("role", "system"))
            return WorkflowTrigger(
                workflow_type=TASK_WORKFLOW_TYPE,
                payload=payload,
                user_id=user_id,
                role=role,
                dry_run=bool(raw.get("dry_run", False)),
                source_event_id=source_event_id,
                extra={
                    k: v
                    for k, v in raw.items()
                    if k not in {
                        "task", "workflow_type", "lead_data", "payload",
                        "user_id", "role", "dry_run", _EVENT_SIGNATURE_FIELD,
                    }
                },
                # The declared identity above is recorded verbatim —
                # whether it may be HONORED is decided at dispatch from
                # this flag (see the module docstring's trust model).
                signature_valid=_verify_task_event_signature(
                    raw, payload, user_id=user_id, role=role
                ),
            )

        if wf not in _LEGACY_WORKFLOW_TYPES:
            raise ValueError(f"unknown workflow_type: {wf!r}")
        payload = raw.get("lead_data") or raw.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("event missing 'lead_data' (or 'payload') object")

        return WorkflowTrigger(
            workflow_type=wf,
            payload=payload,
            user_id=str(raw.get("user_id", "events")),
            role=str(raw.get("role", "system")),
            dry_run=bool(raw.get("dry_run", False)),
            source_event_id=source_event_id,
            extra={k: v for k, v in raw.items() if k not in {
                "workflow_type", "lead_data", "payload", "user_id", "role", "dry_run"
            }},
        )

    async def dispatch(self, trigger: WorkflowTrigger) -> DispatchResult:
        """Resolve pipeline + input model + invoke. Catches errors so the
        consumer can ack/nack appropriately rather than crashing its loop."""
        # INC2-23: the task envelope routes to the hub runtime, not a pipeline.
        if trigger.workflow_type == TASK_WORKFLOW_TYPE:
            return await self._dispatch_task(trigger)

        graph = self.graphs.get(trigger.workflow_type)
        if graph is None:
            return DispatchResult(ok=False, error=f"no graph for {trigger.workflow_type}")

        try:
            pipeline, domain_input = self._select_pipeline(
                trigger.workflow_type, graph, trigger.payload
            )
        except Exception as exc:
            logger.warning(
                "event %s rejected: invalid payload for %s — %s",
                trigger.source_event_id,
                trigger.workflow_type,
                exc,
            )
            return DispatchResult(ok=False, error=f"invalid payload: {exc}")

        try:
            workflow_id, thread_id, _ = await pipeline.run(
                domain_input,
                user_id=trigger.user_id,
                role=trigger.role,
                dry_run=trigger.dry_run,
            )
        except Exception as exc:
            logger.exception(
                "event %s dispatch failed for %s: %s",
                trigger.source_event_id,
                trigger.workflow_type,
                exc,
            )
            return DispatchResult(ok=False, error=str(exc))

        logger.info(
            "event %s -> workflow %s (%s)",
            trigger.source_event_id,
            workflow_id,
            trigger.workflow_type,
        )
        return DispatchResult(ok=True, workflow_id=workflow_id, thread_id=thread_id)

    async def _dispatch_task(self, trigger: WorkflowTrigger) -> DispatchResult:
        """Drive a hub Run from a ``{task:{intent}}`` envelope (INC2-23).

        Imports are local so this module keeps loading with no runtime/LLM
        dependency until a task event actually arrives. Any failure is turned
        into a structured ``DispatchResult`` (the consumer owns ack/nack).
        """
        from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

        payload = trigger.payload
        intent = str(payload.get("intent") or "")
        if not intent:
            return DispatchResult(ok=False, error="agentflow_task event missing 'intent'")

        context = dict(payload.get("context") or {})
        declared_tenant = trigger.extra.get("tenant_id") or payload.get("tenant_id")

        # Trust gate (see the module docstring): the envelope's declared
        # tenant/role are claims, not facts.  Only a validly signed
        # event's tenant/user are honored, and even then the role is
        # resolved server-side to the fixed least-privilege service
        # role — the envelope can never elevate.  Unsigned/invalid
        # events are rejected (fail closed), except in the explicit
        # single-process dev/test mode where the legacy behavior is
        # kept for local development under a warning.
        if _event_signing_secret():
            if not trigger.signature_valid:
                logger.warning(
                    "event %s rejected: task event signature missing or "
                    "invalid; declared tenant/role are not honored",
                    trigger.source_event_id,
                )
                return DispatchResult(
                    ok=False,
                    error=(
                        "task event rejected: missing or invalid signature — "
                        "envelope-declared tenant/role are not honored "
                        "without a valid signature"
                    ),
                )
            tenant_id = declared_tenant
            user_id = trigger.user_id
            role = EVENT_TASK_ROLE
        else:
            from forgeflow.runtime.run_lease import single_process_dev_mode

            if not single_process_dev_mode():
                logger.warning(
                    "event %s rejected: no event signing secret configured "
                    "outside dev/test mode; refusing to dispatch a task "
                    "event under an envelope-declared tenant/role",
                    trigger.source_event_id,
                )
                return DispatchResult(
                    ok=False,
                    error=(
                        "task event rejected: EVENT_SIGNING_SECRET is not "
                        "configured, so envelope-declared tenant/role "
                        "cannot be trusted outside dev/test mode"
                    ),
                )
            logger.warning(
                "event %s: dispatching UNSIGNED task event under its "
                "declared tenant/role (single-process dev/test mode only)",
                trigger.source_event_id,
            )
            tenant_id = declared_tenant
            user_id = trigger.user_id
            role = trigger.role

        try:
            ctx = RequestContext(
                tenant_id=tenant_id,
                user_id=user_id,
                role=role,
            )
            task = TaskCreate(
                intent=intent,
                title=str(payload.get("title") or ""),
                workflow_type=str(payload.get("workflow_type") or "generic"),
                context=context,
            )
            handle = await run_task(task, ctx)
        except Exception as exc:  # noqa: BLE001 — surface as a dispatch error
            logger.exception(
                "event %s task dispatch failed: %s", trigger.source_event_id, exc
            )
            return DispatchResult(ok=False, error=str(exc))

        logger.info(
            "event %s -> run %s (%s)",
            trigger.source_event_id,
            handle.run_id,
            TASK_WORKFLOW_TYPE,
        )
        return DispatchResult(ok=True, workflow_id=handle.run_id, thread_id=handle.thread_id)

    @staticmethod
    def _select_pipeline(workflow_type: str, graph: Any, payload: dict) -> tuple[Any, Any]:
        """Mirror the dispatcher used by /workflows/run."""
        if workflow_type == "support_ops":
            return SupportOpsPipeline(graph), TicketInput(**payload)
        if workflow_type == "finance_recon":
            return FinanceReconPipeline(graph), ReconciliationInput(**payload)
        return SalesOpsPipeline(graph), LeadInput(**payload)
