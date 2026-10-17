"""ForgeFlow QA Code Agent — the single authoritative source of QA data models.

This module is layer L0 of the ``forgeflow.qa`` sub-package (Phase A–C of the QA
Code Agent closed loop: Discover → Plan → Execute → Analyze → Report). It owns
*every* public type, enum and ``TypedDict`` the rest of the sub-package shares
(§4 inventory, §7 test plan, §40 failure classes, §44 evidence, §6 ``QAState``).

Why one module
--------------
Every other QA module imports these names and none re-defines them. A single
source means the on-disk schemas (``capability_inventory.json``,
``test_plan.json``, ``evidence/*.json`` …) can never drift apart because two
modules disagree about a field — the "省去分支、单一数据源" discipline the
platform's ``observability.metrics_source`` follows.

Language: ``from __future__ import annotations`` is mandatory (the package's
floor is Python 3.11); no 3.12-only syntax is used.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

# NOTE: every enum below uses ``enum.StrEnum`` rather than ``(str, Enum)``. The
# two are behaviourally identical for JSON / pydantic purposes, but ``StrEnum``
# is the modern spelling ruff's UP042 rule requires — and this package's hard
# constraint is a zero-warning ``ruff check``. (Design §3.1 wrote ``(str, Enum)``;
# ``StrEnum`` is a deliberate, semantics-preserving substitution.)


# ── §40 unified failure classification (14 members, verbatim) ────────────────
class FailureClass(StrEnum):
    """The 14-way taxonomy every observed failure is bucketed into (§40).

    The *members* are a public contract: ``failure_analysis.json`` aggregates on
    these exact strings. ``UNKNOWN`` is the honest default — a failure is never
    silently promoted to ``SOURCE_BUG``.
    """

    SOURCE_BUG = "SOURCE_BUG"
    TEST_BUG = "TEST_BUG"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    DATABASE_FAILURE = "DATABASE_FAILURE"
    MIGRATION_FAILURE = "MIGRATION_FAILURE"
    TOOL_FAILURE = "TOOL_FAILURE"
    LLM_FAILURE = "LLM_FAILURE"
    PERMISSION_FAILURE = "PERMISSION_FAILURE"
    DATA_FAILURE = "DATA_FAILURE"
    UI_FAILURE = "UI_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    INFRA_FAILURE = "INFRA_FAILURE"
    UNKNOWN = "UNKNOWN"


# ── §72 verdict (A–C consumes a subset; SKIP ≠ PASS) ─────────────────────────
class Verdict(StrEnum):
    """The run-level conclusion (§72). ``INCONCLUSIVE`` is a first-class answer."""

    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    BLOCKED = "BLOCKED"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class EvidenceStatus(StrEnum):
    """Per-test evidence status. Only a real, exit-0 run may be ``PASS``."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"
    SKIP = "SKIP"


class Layer(StrEnum):
    """The 19 capability layers (§4). Every layer key is always present."""

    CORE = "CORE"
    API = "API"
    DATABASE = "DATABASE"
    RUNTIME = "RUNTIME"
    AGENT = "AGENT"
    TOOL = "TOOL"
    MCP = "MCP"
    SKILL = "SKILL"
    MEMORY = "MEMORY"
    EXPERIENCE = "EXPERIENCE"
    EVALUATION = "EVALUATION"
    GOVERNANCE = "GOVERNANCE"
    AUTH = "AUTH"
    DATA = "DATA"
    UI = "UI"
    OBSERVABILITY = "OBSERVABILITY"
    DURABILITY = "DURABILITY"
    BUILD = "BUILD"
    DEPLOYMENT = "DEPLOYMENT"


#: Stable iteration order for the 19 layers (§4). Spelled out explicitly (rather
#: than ``tuple(Layer)``) so the order is a reviewable, frozen contract and the
#: inventory map is guaranteed to carry every key even when a layer is empty.
ALL_LAYERS: tuple[Layer, ...] = (
    Layer.CORE,
    Layer.API,
    Layer.DATABASE,
    Layer.RUNTIME,
    Layer.AGENT,
    Layer.TOOL,
    Layer.MCP,
    Layer.SKILL,
    Layer.MEMORY,
    Layer.EXPERIENCE,
    Layer.EVALUATION,
    Layer.GOVERNANCE,
    Layer.AUTH,
    Layer.DATA,
    Layer.UI,
    Layer.OBSERVABILITY,
    Layer.DURABILITY,
    Layer.BUILD,
    Layer.DEPLOYMENT,
)


# ── §4 capability inventory ──────────────────────────────────────────────────
class Capability(BaseModel):
    """One discovered capability: a name, a real path and how it was detected."""

    capability_id: str
    layer: Layer
    name: str
    path: str
    entrypoints: list[str] = Field(default_factory=list)
    detected_by: list[str] = Field(default_factory=list)
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    verified: bool = False


class CapabilityInventory(BaseModel):
    """The full, run-scoped inventory written to ``capability_inventory.json``."""

    run_id: str
    generated_at: str
    root: str
    technology: dict[str, Any] = Field(default_factory=dict)
    layers: dict[Layer, list[Capability]] = Field(default_factory=dict)
    totals: dict[str, int] = Field(default_factory=dict)


class DiscoveryReport(BaseModel):
    """§3/§5 discovery output: inventory + dead candidates + static checks."""

    run_id: str
    generated_at: str
    inventory: CapabilityInventory
    dead_capability_candidates: list[str] = Field(default_factory=list)
    static_checks: dict[str, Any] = Field(default_factory=dict)


# ── §7 test plan ─────────────────────────────────────────────────────────────
class TestType(StrEnum):
    """The four test families A–C can plan for (§8)."""

    __test__ = False  # not a pytest test class despite the ``Test`` name

    STATIC = "static"
    UNIT = "unit"
    INTEGRATION = "integration"
    CONTRACT = "contract"


class Risk(StrEnum):
    """Coarse risk bucket attached to each plan item (§18)."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class TestStatus(StrEnum):
    """Lifecycle status of a plan item as the run progresses."""

    __test__ = False  # not a pytest test class despite the ``Test`` name

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


class TestPlanItem(BaseModel):
    """One planned test. ``task_target`` must exist in the Makefile (§15)."""

    __test__ = False  # not a pytest test class despite the ``Test`` name

    test_id: str
    capability_id: str
    layer: Layer
    test_type: TestType
    description: str
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    risk: Risk = Risk.LOW
    adapter: Literal["make"] = "make"
    task_target: str | None = None
    command: list[str] = Field(default_factory=list)
    cwd: str | None = None
    timeout_s: int = Field(default=600, ge=1)
    expected_failure_class: FailureClass | None = None
    failure_class: FailureClass | None = None
    evidence_path: str | None = None
    requires: list[str] = Field(default_factory=list)
    status: TestStatus = TestStatus.PENDING


# ── §80 subprocess execution result ──────────────────────────────────────────
class ProcessStatus(StrEnum):
    """Outcome of a single subprocess invocation (never an exception)."""

    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SPAWN_ERROR = "spawn_error"


class ProcessResult(BaseModel):
    """The *real* facts of one subprocess run (§80) — nothing is fabricated."""

    command: list[str]
    cwd: str
    env_diff: dict[str, str] = Field(default_factory=dict)
    exit_code: int | None
    status: ProcessStatus
    stdout: str
    stderr: str
    duration_ms: int
    started_at: str
    timed_out: bool = False
    timeout_s: int


# ── §15 TaskRunner ───────────────────────────────────────────────────────────
class TaskSpec(BaseModel):
    """A discoverable build target (Make) with an existence flag."""

    target: str
    description: str = ""
    exists: bool = True
    kind: Literal["make"] = "make"


class TaskResult(BaseModel):
    """A target run: the target name plus the raw :class:`ProcessResult`."""

    target: str
    ok: bool
    result: ProcessResult


# ── §44 QA Evidence (first 11 fields verbatim; last 2 are additive) ──────────
class Evidence(BaseModel):
    """The immutable, per-test proof artifact written to ``evidence/`` (§44).

    A ``SKIP``/``INCONCLUSIVE`` never masquerades as a ``PASS``: the status is
    copied from the real process outcome, never assumed.
    """

    evidence_id: str
    run_id: str
    capability_id: str
    test_id: str
    command: list[str]
    environment: dict[str, Any] = Field(default_factory=dict)
    exit_code: int | None
    status: EvidenceStatus
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    attachments: list[str] = Field(default_factory=list)
    timestamp: str
    # —— additive (does not change §44 semantics) ——
    duration_ms: int = 0
    failure_class: FailureClass | None = None


# ── §6 QAState (fields verbatim; D–K fields stay empty in A–C) ───────────────
class QAState(TypedDict, total=False):
    """The orchestration state record. Phase A–C populates the first block only.

    ``root_causes`` / ``change_sets`` / ``regressions`` / ``skill_candidates``
    are the D–K hand-off points and are deliberately empty in this increment.
    """

    project: dict[str, Any]
    capabilities: CapabilityInventory
    test_plan: list[TestPlanItem]
    execution_queue: list[str]
    current_test: str | None
    observations: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    root_causes: list[dict[str, Any]]
    change_sets: list[dict[str, Any]]
    validation_results: list[dict[str, Any]]
    regressions: list[dict[str, Any]]
    evidence: list[Evidence]
    skill_candidates: list[dict[str, Any]]
    final_summary: dict[str, Any]


# ── §68 failure fingerprint (Phase D) ────────────────────────────────────────
class FailureFingerprint(BaseModel):
    """A stable, cross-run fingerprint of one failure "signature family" (§68).

    ``fingerprint`` is ``sha1("|".join(sorted(signature)))[:12]``; ``signature``
    is a bounded, normalised list of signal tokens recovered from the *real*
    stdout/stderr. It never contains timestamps, absolute paths or bare numbers,
    so the same root cause yields the same fingerprint across runs.
    """

    fingerprint: str
    failure_class: FailureClass
    test_ids: list[str] = Field(default_factory=list)
    count: int = 0
    signature: list[str] = Field(default_factory=list)


# ── §90 深链：根因候选（保守；不写终判）────────────────────────────────────
class RootCauseCandidate(BaseModel):
    """A *candidate* root cause for one failure — never a final verdict.

    The final verdict belongs to Phase E (fix planning); Phase D only proposes a
    candidate, always paired with the real evidence excerpt it came from and a
    deliberately conservative ``confidence``.
    """

    test_id: str
    failure_class: FailureClass
    fingerprint: str
    attribution: Literal["environment", "source", "test", "unknown"] = "unknown"
    hypothesis: str = ""
    evidence_excerpt: str = ""  # bounded (<= 400 chars) excerpt from the transcript
    confidence: float = Field(0.0, ge=0.0, le=1.0)


# ── §20 Regression Scope (Phase D) ───────────────────────────────────────────
class RegressionScope(BaseModel):
    """The affected surface of one change (§20), used to *narrow* regression.

    Every list is the best *offline-determinable* answer; ``coverage_note`` says
    honestly where that answer stops being trustworthy (never a claim of
    completeness).
    """

    changed_files: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    affected_packages: list[str] = Field(default_factory=list)
    affected_routes: list[str] = Field(default_factory=list)
    affected_skills: list[str] = Field(default_factory=list)
    affected_tools: list[str] = Field(default_factory=list)
    affected_policies: list[str] = Field(default_factory=list)
    affected_tests: list[str] = Field(default_factory=list)
    recommended_targets: list[str] = Field(default_factory=list)  # real Make targets only
    full_repo_fallback: bool = False
    rationale: list[str] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    coverage_note: str = ""


# ── §42/§69 regression report (Phase D; persisted as regression_report.json) ─
class RegressionReport(BaseModel):
    """The persisted regression artifact (§42) built from scope (§20) + baseline (§69)."""

    run_id: str
    generated_at: str  # ISO-8601 UTC
    scope: RegressionScope
    baseline_available: bool = False
    baseline_path: str | None = None
    comparison: dict[str, Any] = Field(default_factory=dict)  # eval_regression.check_dict(...)
    metrics: dict[str, Any] = Field(default_factory=dict)  # this run's QA metrics
    pass_rate_lower_bound: float | None = None  # Wilson lower bound; None when unmeasured
    environment_attributed: int = 0
    source_candidates: int = 0
    findings: list[dict[str, Any]] = Field(default_factory=list)
    coverage_note: str = ""


__all__ = [
    "ALL_LAYERS",
    "Capability",
    "CapabilityInventory",
    "DiscoveryReport",
    "Evidence",
    "EvidenceStatus",
    "FailureClass",
    "FailureFingerprint",
    "Layer",
    "ProcessResult",
    "ProcessStatus",
    "QAState",
    "RegressionReport",
    "RegressionScope",
    "Risk",
    "RootCauseCandidate",
    "TaskResult",
    "TaskSpec",
    "TestPlanItem",
    "TestStatus",
    "TestType",
    "Verdict",
]
