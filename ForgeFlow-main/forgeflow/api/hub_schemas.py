"""Request/response schemas for the AgentFlow hub APIs.

Kept in a dedicated module (rather than growing ``api/schemas.py``) so the
existing schemas and their consumers stay byte-identical (constraint C3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from forgeflow.runtime.attachments import AttachmentInput

# --------------------------------------------------------------------------- #
# Tasks / Runs                                                                 #
# --------------------------------------------------------------------------- #

class TaskCreateRequest(BaseModel):
    intent: str = Field(..., min_length=1, description="Natural-language task intent")
    title: str = ""
    workflow_type: str = "generic"
    context: dict[str, Any] = Field(default_factory=dict)


class WorkspaceTaskCreateRequest(TaskCreateRequest):
    """Body for the async ``POST /workspace/tasks`` (INC32 ADR-01 / ADR-06).

    The hub ``TaskCreateRequest`` plus the workspace relationships and the same
    optional C4 attachments ``POST /tasks`` accepts. All additive: a caller that
    sends only ``intent`` gets a single-run session (``session_id`` defaults to
    the new run's id, ``parent_run_id`` empty).
    """

    session_id: str = Field("", description="Reuse an existing conversation; empty ⇒ a new one")
    parent_run_id: str = Field("", description="The run this one continues (Follow-up)")
    attachments: list[AttachmentInput] = Field(default_factory=list)


class RunHandleResponse(BaseModel):
    run_id: str
    thread_id: str
    status: str
    detail: dict[str, Any] = Field(default_factory=dict)
    #: INC32 (additive, default-safe) — the workspace relationships. Defaults to
    #: ``""`` so every pre-INC32 response stays valid; a run with no declared
    #: parent honestly reports an empty ``parent_run_id``.
    session_id: str = ""
    parent_run_id: str = ""


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
    #
    # INC20 / T01 —— 「未测量」与「已测量为 0」在**传输层**必须可分。原默认值
    # ``int = 0`` / ``float = 0.0`` 会让「字段缺失（pre-INC4 记录）」被伪装成
    # 「确为 0」，前端据此渲染「Token 用量 0」即是谎报。默认值改为 ``None``，与既有
    # ``latency_ms: float | None``（client.ts::RunToolInvocation.latency_ms）同口径：``None`` = 未测量/不适用。
    #
    # ⚠️ 残余不可分性（如实记录，勿删）：本期**未**改生产者
    # （``orchestrator.py::run_task`` 中 ``cost_summary = tracker.summary()`` 与 ``total_tokens=int(cost_summary["total_tokens"])`` 处）。确定性档生产者仍会写 ``0``，
    # 因此真实的 ``0`` **既有可能是「真实计量为 0」也可能是「生产者回填」**。
    # 故消费方（前端）**不得**仅凭 token 数值本身判断「是否跑过模型」——唯一判据是
    # **模型驱动证据**（``runtime_mode`` ∈ {llm,react,graph} 或 ``llm`` 非空）。
    total_tokens: int | None = None
    total_cost_usd: float | None = None
    runtime_mode: str = "deterministic"
    #: Which executor produced this run + what models were actually built and
    #: whether the run was degraded. Empty for pre-INC4 records.
    llm: dict[str, Any] = Field(default_factory=dict)
    #: Agent Loop budget-breaker audit trail (review finding #10): the ceilings,
    #: how many times the breaker was consulted, and every breach. ``observations
    #: == 0`` means the loop never needed to replan — distinct from "the breaker
    #: was never consulted".
    loop: dict[str, Any] = Field(default_factory=dict)
    #: INC12 A1 — every real tool invocation this run produced (across all replan
    #: rounds), each a ``ToolInvocation.to_dict()``. A step is ``ok`` only when a
    #: handler actually ran and returned a result; otherwise the honest
    #: ``error`` / ``unavailable`` / ``refused`` / ``skipped`` is recorded with a
    #: reason. Additive; the default keeps pre-INC12 records valid.
    tool_invocations: list[dict[str, Any]] = Field(default_factory=list)
    #: INC12 A5 — **who initiated this run** (the first acceptance question:
    #: "谁发起的？ → user_id / tenant_id / role"). Additive; the defaults match
    #: ``RequestContext``'s, so a pre-A5 record degrades honestly to these
    #: rather than inventing an actor.
    actor_user_id: str = "anonymous"
    actor_role: str = "viewer"
    #: INC12 A5b — the third leg of Q1's triple ("user_id / tenant_id / role"):
    #: which tenant the run belongs to. ``RunRecord.tenant_id`` has always
    #: carried it (and ``_load_run`` already scopes on it), it was simply never
    #: surfaced — so an exported run detail lost its ownership. Additive;
    #: ``None`` keeps every pre-A5b construction site valid.
    tenant_id: str | None = None
    #: INC14 — the run's deliverables, verbatim (Markdown). Additive; the default
    #: keeps pre-INC14 records valid and degrades honestly to ``[]``.
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    #: INC15 — the run's **Task Plan** (L1): the planned steps, the candidate
    #: steps that did not apply (``not_applicable``) with their reason, and the
    #: plan/execution counts. Additive; ``{}`` keeps a pre-INC15 record valid.
    plan: dict[str, Any] = Field(default_factory=dict)
    #: INC15 — the run's **Observations** (L3): the ``executed is True``
    #: projection of ``tool_invocations``. Additive; degrades to ``[]``.
    observations: list[dict[str, Any]] = Field(default_factory=list)
    #: INC22 W1 — the run's **declared workflow type** (``task.workflow_type``),
    #: verbatim. Additive; ``"generic"`` keeps every pre-INC22 record valid and is
    #: exactly the producer's (``TaskCreate``) own default, so a record without
    #: the attribute degrades to the generic flow it honestly was — never a
    #: fabricated domain. Surfaced so a manual replan can re-declare it.
    workflow_type: str = "generic"
    #: INC22 W1 — the run's **explicit declared inputs** (ONLY the keys the caller
    #: really supplied — ``table`` / ``paths`` / ``repo_path`` …), verbatim.
    #: Additive; ``{}`` keeps every pre-INC22 record valid and lets a manual
    #: replan re-declare exactly what was there — never more.
    declared_inputs: dict[str, Any] = Field(default_factory=dict)
    #: INC25 W2 — the run's **code-execution plane** summary (engine availability,
    #: ``degraded`` value, workspace lifecycle, timeline, tests, diff, approval).
    #: **Additive** and a field **parallel to** ``llm`` (never a mutation of it):
    #: ``llm`` proves which model ran, ``codeplane`` proves what the code plane did
    #: or could not do. Defaults to ``{}`` so every pre-INC25 record — and every
    #: non-code run — degrades honestly instead of inventing an engine status.
    codeplane: dict[str, Any] = Field(default_factory=dict)
    #: INC32 ADR-02 (additive, default-safe) — the run's workspace relationships.
    #: ``session_id`` groups runs into a conversation; ``parent_run_id`` records
    #: the Follow-up chain (AC-39). Defaults to ``""`` so a pre-INC32 record /
    #: a plain run degrades honestly to "no session recorded" rather than a
    #: fabricated one.
    session_id: str = ""
    parent_run_id: str = ""
    #: INC33 (additive, default-safe) — whether this run's **execution detail**
    #: (``steps`` / ``tool_invocations`` / ``observations`` / ``plan`` / timeline)
    #: survived into the current process. ``True`` for a run this process really
    #: drove; ``False`` for a record hydrated at startup from the persisted
    #: ``workspace_runs`` header (whose body is in-memory by design, migration
    #: ``016``). The UI renders the ``False`` case as an honest Chinese note
    #: (「执行明细未随本次进程保留」) rather than a confident empty step list.
    #: Defaults to ``True`` so every pre-INC33 payload stays valid.
    detail_retained: bool = True


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
    #: INC32 (additive, default-safe) — the workspace relationships (see above).
    session_id: str = ""
    parent_run_id: str = ""


class RunListResponse(BaseModel):
    total: int
    items: list[RunSummaryResponse]


class ReplanRequest(BaseModel):
    reason: str = ""


class RunAbortResponse(BaseModel):
    """Response for ``POST /runs/{run_id}/abort`` (INC32 ADR-04)."""

    run_id: str
    status: str


class SessionSummaryResponse(BaseModel):
    """One conversation group for ``GET /workspace/sessions`` (INC32 ADR-02)."""

    session_id: str
    title: str = ""
    created_at: str = ""
    run_count: int = 0
    latest_status: str = ""


class SessionListResponse(BaseModel):
    total: int
    items: list[SessionSummaryResponse]


class WorkspaceSessionDetailResponse(BaseModel):
    """``GET /workspace/sessions/{session_id}`` (INC32 ADR-02 / ADR-06)."""

    session_id: str
    title: str = ""
    runs: list[dict[str, Any]] = Field(default_factory=list)



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


class RolloutStageRequest(BaseModel):
    """Body for ``POST /skills/{skill_id}/rollouts/{rid}/promote`` (INC46 T34).

    客户端只提交**观测**（T16 标签 / 延迟 / 成本），成功率等指标由**服务端**
    推导 —— 不接受客户端直接给「成功率」等结论，避免把结论冒充成事实
    （红线 4 / 8）。样本不足 ⇒ 服务端如实回 ``insufficient_data``。
    """

    labels: list[str | None] | None = None
    latencies_ms: list[float] | None = None
    costs: list[float] | None = None
    window_hours: float | None = None
    #: incumbent（旧版）作为对照的观测（影子对比，只读）。
    incumbent_labels: list[str | None] | None = None
    incumbent_latencies_ms: list[float] | None = None
    incumbent_costs: list[float] | None = None
    incumbent_window_hours: float | None = None
    dangerous_event: bool = False
    reason: str | None = None


class RolloutRollbackRequest(BaseModel):
    """Body for ``POST /skills/{skill_id}/rollouts/{rid}/rollback`` (INC46 T34)."""

    reason: str | None = None


class MergeApproveRequest(BaseModel):
    """Body for ``POST /skills/lifecycle/proposals/{id}/approve`` (INC46 T35).

    合并提案**不自动合并**；人工 approve 后才落 ``approved``，并把两个源 skill 的
    来源链与新产生的 skill 记录下来（红线 6：不覆盖任何历史版本）。
    """

    note: str | None = None


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
    #: INC34 (additive, default-safe) — the minimum number of similar
    #: experiences the compiler requires (``SKILL_CANDIDATE_MIN_EXPERIENCES``).
    #: Populated **only** when ``status == "insufficient"`` so a caller can state
    #: the honest "需要至少 N 条相似经验" without inventing the number; ``None`` on
    #: every other candidate and on any pre-INC34 payload.
    required_experiences: int | None = None


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


class ApprovePublishRequest(BaseModel):
    """Body for ``POST /skills/{skill_id}/versions/{semver}/approve-publish`` (INC46 T15).

    ``reason`` is optional and stays ``None`` when the approver gives none —
    the audit record must not fabricate one (未测量 ⇒ None).
    """

    reason: str | None = None


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


# --------------------------------------------------------------------------- #
# Skill engineering (INC43 S3 / BE-3) — additive response models             #
#                                                                             #
# Every model below is **additive**: it is a new class only, so no existing    #
# schema or consumer changes. Field tables mirror ``skills/contracts.py``      #
# (§3.2) one-for-one, so the router can serialise a contract-layer object      #
# without a second projection.                                                 #
# --------------------------------------------------------------------------- #

class SkillContractResponse(BaseModel):
    """A fully-shaped, reviewable skill definition (``SkillContract``)."""

    goal: str = ""
    preconditions: list[str] = Field(default_factory=list)
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    procedure: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    #: ``⊆ trust_baseline.allowed_tool_set()`` — the router reuses the same
    #: whitelist the sandbox/release gate consume (never a second copy).
    policies: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    applicable_when: dict[str, Any] = Field(default_factory=dict)
    not_applicable_when: dict[str, Any] = Field(default_factory=dict)
    #: ``low`` / ``medium`` / ``high``; ``high`` ⇒ mandatory REVIEW (HITL).
    risk_level: str = "low"


class SkillCritiqueResponse(BaseModel):
    """An independent review verdict over a contract (``SkillCritique``)."""

    findings: list[dict[str, Any]] = Field(default_factory=list)
    severity: str = "none"
    #: Blocking items — a non-empty list keeps the lifecycle at ``DRAFT``.
    must_fix: list[str] = Field(default_factory=list)


class SkillTestCaseResponse(BaseModel):
    """One generated test case (``SkillTestCase``)."""

    id: str = ""
    #: ``normal`` / ``boundary`` / ``adversarial`` / ``security``.
    category: str = "normal"
    input: dict[str, Any] = Field(default_factory=dict)
    expectation: str = ""
    assertion: str = ""


class SkillTestRunResponse(BaseModel):
    """The deterministic sandbox verdict for one case (``SkillTestRun``)."""

    case_id: str = ""
    #: ``pass`` / ``fail`` / ``error`` — ``error`` never collapses to ``pass``.
    verdict: str = "error"
    detail: str = ""


class SkillEvaluationSummaryResponse(BaseModel):
    """Aggregate sandbox result over a case set (``SkillEvaluation``).

    ``pass_rate`` = ``passed / total`` (total includes ``error``);
    ``verified_pass_rate``'s denominator **excludes** ``error`` — a tooling
    failure can never inflate a rate. Both are recomputable from the runs.
    """

    pass_rate: float = 0.0
    verified_pass_rate: float = 0.0
    failure_modes: list[str] = Field(default_factory=list)
    sample_size: int = 0
    ran_at: str = ""


class SkillEngineeringResponse(BaseModel):
    """The full record of one engineering-loop view (read-only facts or a run).

    Used by both the read-only ``GET`` surfaces and the ``POST`` trigger, so the
    front-end consumes one shape. ``revisions`` are the repair rounds'
    ``SkillRevision.to_dict()`` (embedded as dicts to keep the schema surface
    minimal — see §3.2).
    """

    tenant_id: str = ""
    candidate_id: str = ""
    #: Populated only by the skill-scoped route (a candidate has no skill yet).
    skill_id: str = ""
    #: One of the six read-only lifecycle states (§3.3).
    lifecycle: str = "DRAFT"
    contract: SkillContractResponse = Field(default_factory=SkillContractResponse)
    critique: SkillCritiqueResponse = Field(default_factory=SkillCritiqueResponse)
    test_cases: list[SkillTestCaseResponse] = Field(default_factory=list)
    test_runs: list[SkillTestRunResponse] = Field(default_factory=list)
    evaluation: SkillEvaluationSummaryResponse = Field(
        default_factory=SkillEvaluationSummaryResponse
    )
    revisions: list[dict[str, Any]] = Field(default_factory=list)
    rounds: int = 0
    passed: bool = False
    #: Honest reason for a ``DRAFT`` downgrade (empty when the run passed).
    degraded_reason: str = ""
    #: The legal lifecycle moves from ``lifecycle`` (read-only derivation).
    next_states: list[str] = Field(default_factory=list)
    #: Whether any move out of ``lifecycle`` requires ``approve:skills`` (HITL).
    requires_approval: bool = False


class SkillLifecycleResponse(BaseModel):
    """Read-only projection of the six-state lifecycle for one subject (§3.3)."""

    skill_id: str = ""
    candidate_id: str = ""
    lifecycle: str = "DRAFT"
    #: The six states, in order (``DRAFT`` … ``DEPRECATED``).
    states: list[str] = Field(default_factory=list)
    #: The states reachable from ``lifecycle`` by a legal transition.
    next_states: list[str] = Field(default_factory=list)
    #: Whether any move out of ``lifecycle`` requires ``approve:skills`` (HITL).
    requires_approval: bool = False


# --------------------------------------------------------------------------- #
# Skill insights (INC46 T06) — additive response models                        #
#                                                                             #
# Rules / experience / readiness / forge. Every model is a NEW class only, so  #
# no existing schema or consumer changes (additive). They carry the honesty     #
# contract explicitly: an unmeasured rate is ``None`` on the wire, never ``0``. #
# --------------------------------------------------------------------------- #

class SkillRuleItem(BaseModel):
    """One ``must`` / ``must_not`` rule with its recomputable evidence."""

    rule_id: str = ""
    #: ``must`` / ``must_not`` (``rule_assets.RULE_KINDS``).
    rule_kind: str = "must"
    rule_text: str = ""
    support: int = 0
    confidence: float = 0.0
    source_run_ids: list[str] = Field(default_factory=list)
    #: Whether the platform **really** enforces the rule (vs. advisory only).
    enforced: bool = False
    #: Human-readable evidence for ``enforced`` (why / why not).
    enforcement: str = ""


class SkillEnforcementSummary(BaseModel):
    """The skill's declared tools classified by the platform's single truth."""

    #: Where the classification comes from (``forgeflow.skills.tool_permissions``).
    source: str = "forgeflow.skills.tool_permissions"
    declared_tools: list[str] = Field(default_factory=list)
    #: ``{tool: READ|WRITE|EXTERNAL|DANGEROUS}`` for every declared tool.
    tool_classes: dict[str, str] = Field(default_factory=dict)
    #: The platform's real ``DANGEROUS`` id set (from ``gate.TOOL_PERMISSION_MAP``).
    dangerous_tools: list[str] = Field(default_factory=list)
    #: ``low`` / ``medium`` / ``high`` — the skill's aggregate risk tier.
    risk_level: str = "low"
    #: Declared tools the platform genuinely blocks (∩ ``dangerous_tools``).
    blocked_tools: list[str] = Field(default_factory=list)


class SkillRulesResponse(BaseModel):
    """``GET /skills/{id}/rules`` — tenant rules + real enforcement."""

    skill_id: str = ""
    tenant_id: str = ""
    must: list[SkillRuleItem] = Field(default_factory=list)
    must_not: list[SkillRuleItem] = Field(default_factory=list)
    enforcement: SkillEnforcementSummary = Field(default_factory=SkillEnforcementSummary)


class SkillExperienceResponse(BaseModel):
    """``GET /skills/{id}/experience`` — the version's source experiences."""

    skill_id: str = ""
    tenant_id: str = ""
    total: int = 0
    items: list[ExperienceResponse] = Field(default_factory=list)


class SkillReadinessCheck(BaseModel):
    """One named readiness fact (``ok`` + its evidence string)."""

    name: str = ""
    ok: bool = False
    evidence: str = ""


class SkillReadinessResponse(BaseModel):
    """``GET /skills/{id}/readiness`` — honest readiness facts.

    ``rate`` is ``None`` (never ``0``) when nothing was measured; ``evaluated``
    distinguishes "unmeasured" (``0``) from "measured as zero". This is the
    wire-level enforcement of INC46 红线 4.
    """

    skill_id: str = ""
    tenant_id: str = ""
    ready: bool = False
    #: Number of measured evaluations backing ``rate`` (``0`` or ``1`` here).
    evaluated: int = 0
    #: The measured rate, or ``None`` when unmeasured (⇒ UI renders「—」).
    rate: float | None = None
    has_version: bool = False
    current_version: str | None = None
    checks: list[SkillReadinessCheck] = Field(default_factory=list)


class SkillForgeRequest(BaseModel):
    """Body for ``POST /skills/forge`` (the Forge trigger)."""

    #: Explicit experience ids (``mode="manual"``); omitted ⇒ auto-cluster.
    experience_ids: list[str] | None = None
    #: ``auto``（聚类相似经验） / ``manual``（按 ids 指定）.
    mode: str = "auto"


class SkillForgeResponse(BaseModel):
    """``POST /skills/forge`` + ``GET /skills/forge/{id}``.

    ``status`` ∈ ``compiled`` / ``insufficient``; ``required_experiences`` is
    populated only on ``insufficient`` so the caller can state the real
    threshold without inventing it.
    """

    forge_id: str = ""
    tenant_id: str = ""
    status: str = ""
    candidate_id: str = ""
    name: str = ""
    domain: str = ""
    experience_ids: list[str] = Field(default_factory=list)
    similarity_score: float = 0.0
    required_experiences: int | None = None
