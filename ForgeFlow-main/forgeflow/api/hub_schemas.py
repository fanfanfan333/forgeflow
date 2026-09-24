"""Request/response schemas for the AgentFlow hub APIs.

Kept in a dedicated module (rather than growing ``api/schemas.py``) so the
existing schemas and their consumers stay byte-identical (constraint C3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Tasks / Runs                                                                 #
# --------------------------------------------------------------------------- #

class TaskCreateRequest(BaseModel):
    intent: str = Field(..., min_length=1, description="Natural-language task intent")
    title: str = ""
    workflow_type: str = "generic"
    context: dict[str, Any] = Field(default_factory=dict)


class RunHandleResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str
    detail: dict[str, Any] = Field(default_factory=dict)


class RunDetailResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str
    outcome: str
    intent: str
    steps: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: str
    completed_at: str | None = None
    experience_id: str | None = None
    # INC4 §A — the runtime now really measures these, so surface them. Without
    # them a consumer cannot tell a genuine LLM run (tokens > 0, runtime_mode
    # "llm") from the deterministic platform graph, which is exactly the
    # "looks wired, silently isn't" confusion this project keeps hunting.
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    runtime_mode: str = "deterministic"
    #: Which executor produced this run + what models were actually built and
    #: whether the run was degraded. Empty for pre-INC4 records.
    llm: dict[str, Any] = Field(default_factory=dict)
    #: Agent Loop budget-breaker audit trail (review finding #10): the ceilings,
    #: how many times the breaker was consulted, and every breach. ``observations
    #: == 0`` means the loop never needed to replan — distinct from "the breaker
    #: was never consulted".
    loop: dict[str, Any] = Field(default_factory=dict)


class RunSummaryResponse(BaseModel):
    """Compact run row for list views (home page "近期任务")."""

    run_id: str
    thread_id: str
    status: str
    outcome: str
    intent: str
    title: str = ""
    created_at: str
    completed_at: str | None = None
    experience_id: str | None = None
    step_count: int = 0


class RunListResponse(BaseModel):
    total: int
    items: list[RunSummaryResponse]


class ReplanRequest(BaseModel):
    reason: str = ""


# --------------------------------------------------------------------------- #
# Experiences                                                                  #
# --------------------------------------------------------------------------- #

class ExperienceCreateRequest(BaseModel):
    run_id: str
    summary: str = ""
    outcome: str = "success"
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    reusable_steps: list[dict[str, Any]] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    team_id: str | None = None
    memory_ids: list[str] = Field(default_factory=list)


class ExperienceResponse(BaseModel):
    id: str
    tenant_id: str | None = None
    team_id: str | None = None
    run_id: str
    summary: str
    decisions: list[dict[str, Any]]
    outcome: str
    reusable_steps: list[dict[str, Any]]
    tags: list[str]
    memory_ids: list[str] = Field(default_factory=list)
    created_at: datetime


class ExperienceListResponse(BaseModel):
    total: int
    items: list[ExperienceResponse]


class LineageResponse(BaseModel):
    experience: ExperienceResponse
    run: dict[str, Any] | None = None
    memories: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Memory (five-layer scopes)                                                   #
# --------------------------------------------------------------------------- #

class MemoryCreateRequest(BaseModel):
    scope: str = "semantic"
    content: str = Field(..., min_length=1)
    team_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryResponse(BaseModel):
    id: str
    tenant_id: str | None = None
    scope: str
    content: str
    team_id: str | None = None
    namespace: str
    metadata: dict[str, Any]
    created_at: datetime
    #: INC9 B3 — the type dimension of the two-dimensional memory model
    #: (working / episodic / semantic / procedural). Additive; the default ""
    #: keeps every pre-existing construction site valid.
    memory_type: str = ""
    #: INC9 B2 — additive lifecycle fields (default = the historical meaning:
    #: never reused, not archived).
    reuse_count: int = 0
    archived: bool = False


class MemoryListResponse(BaseModel):
    total: int
    items: list[MemoryResponse]


class MemoryScopeResponse(BaseModel):
    scope: str
    label: str
    description: str
    writable_by: list[str]
    readable_by: list[str]
    promotable: bool
    #: INC9 B3 — the default memory type this ownership scope maps to (additive).
    memory_type: str = ""


class MemoryLifecycleResponse(BaseModel):
    """``GET /memory/lifecycle`` — read-only lifecycle health (INC9 B2)."""

    total: int = 0
    active: int = 0
    archived: int = 0
    avg_score: float | None = None
    decay_enabled: bool = False


class MemorySweepResponse(BaseModel):
    """``POST /memory/lifecycle/sweep`` — one score/decay/archive pass (INC9 B2)."""

    enabled: bool = False
    scored: int = 0
    archived: int = 0
    skipped: int = 0


# --------------------------------------------------------------------------- #
# Skills                                                                       #
# --------------------------------------------------------------------------- #

class SkillCreateRequest(BaseModel):
    name: str = Field(..., min_length=1)
    domain: str = "general"
    owner: str | None = None
    description: str = ""


class SkillResponse(BaseModel):
    id: str
    tenant_id: str | None = None
    name: str
    domain: str
    owner: str | None = None
    description: str
    current_version: str | None = None
    status: str
    usage_count: int
    featured: bool
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SkillListResponse(BaseModel):
    total: int
    items: list[SkillResponse]


class SkillVersionCreateRequest(BaseModel):
    semver: str | None = None
    spec: dict[str, Any] = Field(default_factory=dict)
    changelog: str | None = None
    bump: str = "patch"


class SkillVersionResponse(BaseModel):
    id: str
    skill_id: str
    semver: str
    spec: dict[str, Any]
    changelog: str
    eval_score: float | None = None
    source_experience_ids: list[str] = Field(default_factory=list)
    approved_by: str | None = None
    created_at: datetime
    #: INC9 B1 — release state: ``promoted`` (default) / ``canary`` /
    #: ``rolled_back``. Additive; the default matches the historical meaning of
    #: every version (it took effect at once).
    release_state: str = "promoted"


class RollbackRequest(BaseModel):
    to_version: str


class CanaryResolveRequest(BaseModel):
    """Body for ``POST /skills/{skill_id}/canary/resolve`` (INC9 B1).

    ``canary_metrics`` are the candidate version's recorded evaluation metrics;
    ``incumbent_metrics`` the incumbent's (typically ``{"score": eval_score}``).
    ``sample_n`` is how many observations the A/B window actually collected — a
    value below the configured floor yields an honest ``hold``.
    """

    canary_metrics: dict[str, Any] | None = None
    incumbent_metrics: dict[str, Any] | None = None
    sample_n: int = 0


class CanaryResolveResponse(BaseModel):
    skill_id: str
    action: str  # promote | hold | rollback
    severity: str
    reason: str
    sample_n: int
    baseline_present: bool
    current_version: str | None = None
    canary_version: str | None = None


# --------------------------------------------------------------------------- #
# Skill candidates                                                             #
# --------------------------------------------------------------------------- #

class CandidateCreateRequest(BaseModel):
    experience_ids: list[str] | None = None
    mode: str = "auto"


class CandidateResponse(BaseModel):
    id: str
    tenant_id: str | None = None
    name: str
    domain: str
    experience_ids: list[str]
    draft_spec: dict[str, Any]
    similarity_score: float
    status: str
    created_at: datetime


class CandidateListResponse(BaseModel):
    total: int
    items: list[CandidateResponse]


class EvaluateRequest(BaseModel):
    dataset: str | None = None


class EvaluationResponse(BaseModel):
    id: str
    target_id: str
    dataset: str | None = None
    metrics: dict[str, Any]
    verdict: str
    created_at: datetime


class PromoteRequest(BaseModel):
    actor: str | None = None


# --------------------------------------------------------------------------- #
# Policies / approvals / security                                              #
# --------------------------------------------------------------------------- #

class PolicyCreateRequest(BaseModel):
    subject: str = "*"
    resource: str = "*"
    action: str = "*"
    condition: dict[str, Any] = Field(default_factory=dict)
    effect: str = "allow"
    description: str = ""


class PolicyResponse(BaseModel):
    id: str
    tenant_id: str | None = None
    subject: str
    resource: str
    action: str
    condition: dict[str, Any]
    effect: str
    description: str
    created_at: datetime


class PolicyListResponse(BaseModel):
    total: int
    items: list[PolicyResponse]


class EvalRequest(BaseModel):
    subject: str
    resource: str
    action: str
    context: dict[str, Any] = Field(default_factory=dict)


class EvalDecisionResponse(BaseModel):
    effect: str
    risk_level: str
    hit_policy_id: str | None = None
    requires_approval: bool = False
    reason: str = ""
    approval_id: str | None = None
    #: Auditable risk basis (which rule fired) — additive; empty on legacy paths.
    risk_basis: str = ""


class ApprovalResponse(BaseModel):
    id: str
    tenant_id: str | None = None
    run_id: str | None = None
    risk_level: str
    requested_action: str
    requester: str | None = None
    approver: str | None = None
    decision: str | None = None
    status: str
    note: str = ""
    created_at: datetime
    resolved_at: datetime | None = None


class ApprovalListResponse(BaseModel):
    total: int
    items: list[ApprovalResponse]


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(..., description="approve | reject")
    note: str = ""


class SecurityOverviewResponse(BaseModel):
    status: str
    isolation_level: str
    dlp_enabled: bool
    encryption: str
    access_control: str
    blocked_count: int
    pending_approvals: int
    policies: int


# --------------------------------------------------------------------------- #
# Context Builder observability (GET /context)                                 #
# --------------------------------------------------------------------------- #

class ContextStatsResponse(BaseModel):
    """Context-build aggregate — keys mirror ``context_stats.get_build_stats()``.

    ``source`` names the backend the numbers came from and ``degraded`` flags an
    explicit postgres read failure (with ``error`` carrying the reason). A
    degraded payload always has ``has_data=False`` — the reader is never shown
    memory numbers dressed up as database numbers.
    """

    builds: int = 0
    tokens_raw: int = 0
    tokens_used: int = 0
    compression_ratio: float | None = None
    hit_rate: float | None = None
    has_data: bool = False
    recent: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "memory"
    degraded: bool = False
    error: str | None = None
