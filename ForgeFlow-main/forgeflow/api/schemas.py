"""API-level Pydantic request/response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

WorkflowType = Literal["sales_ops", "support_ops", "finance_recon"]

# ------------------------------------------------------------------ #
# Workflow                                                             #
# ------------------------------------------------------------------ #

class WorkflowRunRequest(BaseModel):
    workflow_type: WorkflowType = "sales_ops"
    lead_data: dict = Field(
        ...,
        description=(
            "Domain-specific input payload. sales_ops: LeadInput; "
            "support_ops: TicketInput; finance_recon: ReconciliationInput."
        ),
    )
    # NOTE: identity (user_id/role) is taken from the verified JWT in the
    # RBAC middleware — never from the request body. Body-supplied identity
    # fields were removed to avoid the impression that a client can self-assert
    # its role here (it cannot).
    dry_run: bool = Field(
        False,
        description=(
            "Simulation mode. LLM calls still happen; CRM writes, emails, Slack "
            "posts, and other side-effecting tools are skipped and stub a success."
        ),
    )


class WorkflowRunResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str
    message: str = ""


class WorkflowStatusResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str
    current_stage: str
    total_tokens: int
    total_cost_usd: float
    created_at: datetime | None = None
    completed_at: datetime | None = None
    lead_data: dict | None = None
    proposal: dict | None = None
    analysis_scores: list[dict] = []
    executed_actions: list[str] = []
    errors: list[str] = []


class AgentTraceResponse(BaseModel):
    id: str
    agent_name: str
    stage: str
    started_at: datetime
    completed_at: datetime | None
    tokens_used: int
    cost_usd: float
    error: str | None
    output_patch: dict | None


# ------------------------------------------------------------------ #
# Approvals                                                            #
# ------------------------------------------------------------------ #

class ApprovalRequestResponse(BaseModel):
    id: str
    run_id: str
    token: str
    stage: str
    status: str
    payload: dict
    requested_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None
    resolution_note: str | None = None


class ApprovalActionRequest(BaseModel):
    note: str = ""
    reason: str = ""


# ------------------------------------------------------------------ #
# Memory                                                               #
# ------------------------------------------------------------------ #

class MemoryStoreRequest(BaseModel):
    content: str = Field(..., min_length=1)
    namespace: str = "default"
    metadata: dict = Field(default_factory=dict)
    ttl_hours: int | None = None


class MemoryStoreResponse(BaseModel):
    memory_id: str


class MemorySearchResult(BaseModel):
    id: str
    content: str
    similarity: float
    namespace: str
    metadata: dict


# ------------------------------------------------------------------ #
# Metrics                                                              #
# ------------------------------------------------------------------ #

class MetricsSummaryResponse(BaseModel):
    """Home-dashboard KPI payload (docs/sop/02-ARCHITECTURE.md §7).

    ``has_data`` / ``has_cost`` are the explicit "no real data" signals the SPA
    uses to render ``—`` instead of a misleading ``0`` / ``0.0%``. The numeric
    fields stay ``0.0``-defaulted so the legacy Streamlit dashboard, which reads
    them directly, keeps working unchanged.
    """

    total_runs: int = 0
    terminal_runs: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    avg_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    has_data: bool = False
    has_cost: bool = False
    source: str = "hub_runs"
    # Per-field "is this number meaningful?" flags (INC2-06). They default to
    # True so any caller that does not compute them keeps the historical
    # render; sources that can prove a field is meaningless set it False and
    # the SPA shows "—" instead of 0.0% / 0.
    has_success_rate: bool = True
    has_latency: bool = True


class SloTierResponse(BaseModel):
    """One SLO tier: target vs. observed, plus a breach flag (INC2-07)."""

    tier: str
    availability_target: float = 0.0
    availability_actual: float | None = None
    p95_target_ms: int = 0
    p95_actual_ms: float | None = None
    attainment: float | None = None
    breaching: bool = False
    has_data: bool = False
    degrade: str = "none"


class SloSummaryResponse(BaseModel):
    """``GET /metrics/slo`` payload — the three frozen tiers (§7.2)."""

    tiers: list[SloTierResponse] = []
    source: str = "metrics_source"


class EvaluationSummaryResponse(BaseModel):
    avg_faithfulness: float
    avg_relevance: float
    avg_coherence: float
    hallucination_rate: float
    sample_count: int
