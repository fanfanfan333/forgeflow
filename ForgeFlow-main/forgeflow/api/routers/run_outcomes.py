"""INC46 T16 — run outcome routes: the feedback write + the label read.

  * ``POST /runs/{run_id}/feedback`` — append one user signal, de-duplicated by
    an **idempotency key**; recomputes and returns the run's label.
  * ``GET  /runs/{run_id}/outcome``  — the current label (read-only).

Design boundaries
-----------------
* **Tenant fail-closed (红线 5).** The tenant is resolved first; an unresolved
  tenant is a real ``403``, never a silent default tenant. A run already owned by
  **another** tenant is also ``403`` — the endpoint refuses rather than writing a
  shadow copy under the caller's tenant (任务书 T16 阴性探针).
* **Unknown is not success (红线 12).** ``UNKNOWN`` is a first-class label. This
  endpoint never upgrades a run to a positive label because someone merely asked
  about it, and never returns ``0`` for an unmeasured ``hard_pass`` (红线 4: it
  is ``None``).
* **Idempotency is per tenant.** The same key submitted twice under the same
  tenant records **one** event (``created=False`` the second time); the same key
  under a different tenant is a different event and must not collide.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.outcomes.store import (
    CrossTenantRun,
    get_outcome_store,
)
from forgeflow.skills.errors import GovernanceError
from forgeflow.skills.tenant_scope import require_tenant

logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackRequest(BaseModel):
    """One user signal about a run."""

    kind: str = Field(description="approval|like|reject|revise|revert|abandon|system")
    idempotency_key: str = Field(description="调用方生成的幂等键（同租户内去重）")
    actor: str | None = None
    note: str | None = None


class FeedbackResponse(BaseModel):
    """The recorded event plus whether this call actually created it."""

    run_id: str
    kind: str
    created: bool
    outcome_label: str
    hard_pass: bool | None = None
    feedback_count: int


class OutcomeResponse(BaseModel):
    """The run's current outcome label (``None`` when the run is unknown)."""

    run_id: str
    outcome_label: str | None = None
    hard_pass: bool | None = None
    reason: str | None = None
    found: bool


def _tenant_or_403(tenant: str | None) -> str:
    """Fail closed on an unresolved tenant (红线 5)."""
    try:
        return require_tenant(tenant)
    except GovernanceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/{run_id}/feedback", response_model=FeedbackResponse)
async def post_feedback(
    run_id: str,
    payload: FeedbackRequest,
    tenant: str = Depends(resolve_tenant),
) -> FeedbackResponse:
    """Record one user signal and recompute the label.

    Re-submitting the same ``idempotency_key`` under the same tenant is a no-op
    that returns ``created=False`` — the event is **not** counted twice.
    """
    tenant_id = _tenant_or_403(tenant)
    store = get_outcome_store()
    try:
        event, created = store.record_feedback(
            tenant_id,
            run_id,
            payload.kind,
            actor=payload.actor,
            idempotency_key=payload.idempotency_key,
            note=payload.note,
        )
    except CrossTenantRun as exc:
        # 该 run 已属他租户 ⇒ 403，不写影子副本（红线 5）。
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    outcome = store.relabel(tenant_id, run_id)
    feedback_count = len(store.list_feedback(tenant_id, run_id))
    logger.info(
        "run outcome feedback: tenant=%s run=%s kind=%s created=%s label=%s",
        tenant_id, run_id, event["kind"], created, outcome["outcome_label"],
    )
    return FeedbackResponse(
        run_id=run_id,
        kind=event["kind"],
        created=created,
        outcome_label=outcome["outcome_label"],
        hard_pass=outcome.get("hard_pass"),
        feedback_count=feedback_count,
    )


@router.get("/{run_id}/outcome", response_model=OutcomeResponse)
async def get_outcome(
    run_id: str,
    tenant: str = Depends(resolve_tenant),
) -> OutcomeResponse:
    """Read the current label. A run with no row reports ``found=False``.

    ``outcome_label`` is ``None`` (not ``'UNKNOWN'``) when there is no row:
    "we have no record" and "we recorded UNKNOWN" are different facts and must
    not be conflated.
    """
    tenant_id = _tenant_or_403(tenant)
    row: dict[str, Any] | None = get_outcome_store().get_outcome(tenant_id, run_id)
    if row is None:
        return OutcomeResponse(run_id=run_id, found=False)
    return OutcomeResponse(
        run_id=run_id,
        outcome_label=row.get("outcome_label"),
        hard_pass=row.get("hard_pass"),
        reason=row.get("reason"),
        found=True,
    )
