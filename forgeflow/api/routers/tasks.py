"""Task routes — ``POST /tasks`` creates a Run and drives the closed loop."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field

from forgeflow.api.dependencies import get_current_user
from forgeflow.api.hub_deps import resolve_tenant
from forgeflow.api.hub_schemas import RunHandleResponse, TaskCreateRequest
from forgeflow.cost.degrade import current_degrade_state
from forgeflow.governance.policy_engine import evaluate_task_entry
from forgeflow.rbac.models import UserContext
from forgeflow.runtime.attachments import (
    AttachmentInput,
    AttachmentTooLargeError,
    prepare_attachments,
)
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

logger = logging.getLogger(__name__)
router = APIRouter()


class TaskCreateRequestWithAttachments(TaskCreateRequest):
    """``POST /tasks`` body = the hub request + optional C4 attachments.

    Declared here (rather than widening ``hub_schemas``) so the additive
    ``attachments`` field travels with the route that implements it (INC2-24);
    the body stays backward-compatible — ``attachments`` defaults to empty.
    """

    attachments: list[AttachmentInput] = Field(default_factory=list)


def _admission_guard(workflow_type: str | None) -> None:
    """Refuse a new run while a ``pause_noncritical`` degrade is in force.

    Review finding ③: the ``pause_noncritical`` action was computed but never
    consumed, so ``GET /metrics/slo`` reported a breach while the platform kept
    accepting every run. This is the consumer.

    The HTTP semantics are deliberately **honest**: a capacity / degradation
    refusal is a ``503 Service Unavailable``, *not* a ``403`` — the caller's RBAC
    is fine; the platform is shedding non-critical load after a budget/SLO
    breach. Core workflow types (``CORE_WORKFLOW_TYPES``) always pass.
    """
    state = current_degrade_state()
    if state.denies_new_run(workflow_type):
        raise HTTPException(
            status_code=503,
            detail=(
                "平台处于降级状态，暂不接受非核心工作流 "
                f"'{workflow_type or 'generic'}' 的新运行"
                f"（{state.reason or 'budget/SLO breached'}）；核心工作流不受影响"
            ),
        )


@router.post("", response_model=RunHandleResponse)
async def create_task(
    request: TaskCreateRequestWithAttachments,
    user: UserContext = Depends(get_current_user),
    tenant: str = Depends(resolve_tenant),
):
    """Create a task, risk-grade it, then run it to a terminal state.

    Returns the RunHandle; the run's experience is extracted automatically
    (jump ②) and its events are streamed on ``GET /runs/{id}/events``.

    INC2-24: any attached PDF/image is pre-processed and its text injected into
    the task intent before the run starts. Optional multimodal dependencies
    degrade to "attachment ignored" rather than failing the request.
    """
    decision = await evaluate_task_entry(
        user.user_id, user.role, request.intent, tenant_id=tenant
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=decision.reason)
    if decision.requires_approval:
        raise HTTPException(
            status_code=403,
            detail=f"high-risk task requires human approval: {decision.reason}",
        )

    # Review finding ③ — a real degrade (``pause_noncritical``) must shed new
    # non-critical runs instead of only being reported on /metrics/slo.
    _admission_guard(request.workflow_type)

    try:
        prepared = await prepare_attachments(request.attachments, intent=request.intent)
    except AttachmentTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    ctx = RequestContext(tenant_id=tenant, user_id=user.user_id, role=user.role)
    task = TaskCreate(
        intent=prepared.intent,
        title=request.title or request.intent[:60],
        workflow_type=request.workflow_type,
        context={**request.context, **prepared.context},
    )

    # INC27 — the code-plane Policy Engine gate (architecture note §2: "模型提出
    # Action → ForgeFlow Policy 允许 / 拒绝 → 才交给 OpenHands 执行").
    # ``execute:workflows`` only answers "may this role start a run"; it must NOT
    # imply "may drive the code agent". The runtime's own predicate is reused so
    # the route gate and the step gate can never drift, and the refusal happens
    # BEFORE ``run_task`` so no run record is created at all.
    from forgeflow.runtime.gate import check_code_permission, describe_code_denial
    from forgeflow.runtime.orchestrator import _is_code_task

    if _is_code_task(task, ctx) and not check_code_permission(user.role, "code.execute"):
        raise HTTPException(
            status_code=403, detail=describe_code_denial(user.role, "code.execute")
        )

    try:
        handle = await run_task(task, ctx)
    except Exception as exc:  # noqa: BLE001 — surface a clean 500
        logger.exception("task run failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return RunHandleResponse(**handle.to_dict())

