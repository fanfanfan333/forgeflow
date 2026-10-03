"""INC46 T15 — read-only evolution / publish-interlock status routes.

Mounted at ``/evolution``. The single endpoint answers the T06 Readiness
surface's 「发布联锁状态」question: are the R1–R8 publish guardrails in place,
and may anything publish automatically? It is strictly read-only — no staging,
no approval, no mutation — and it always reports the **real** probe outcomes:
a capability that is not implemented is reported as unmet with an explicit
「无证据」note, never as an empty 200 masquerading as a lifted interlock
(INC46 §六-T15 失败表现).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from forgeflow.api.hub_deps import resolve_tenant

router = APIRouter()


@router.get("/interlock")
async def get_publish_interlock(tenant: str = Depends(resolve_tenant)) -> dict:
    """Return the tenant's publish-interlock status (R1–R8 + evidence).

    Payload (``InterlockStatus.to_dict``):

    * ``requirements`` — every R1–R8 probe outcome with its evidence
      reference (无证据则注明);
    * ``level1_open`` / ``level1_missing`` — the manual-approval channel
      (R1–R6) and its missing items;
    * ``auto_publish_flag`` — the tenant's ``evolution.auto_publish`` switch;
    * ``released`` / ``missing`` — whether the interlock is lifted (R1–R8 ∧
      flag) and which requirements are still unmet.
    """
    from forgeflow.skills.publish_interlock import evaluate_interlock

    return evaluate_interlock(tenant).to_dict()
