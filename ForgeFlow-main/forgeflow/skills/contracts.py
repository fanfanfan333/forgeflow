"""BE-3 contract layer — the data models the Skill Engineering loop speaks in.

These are **pure data** (dataclasses, JSON-serialisable, no I/O, no LLM). They
are a *contract layer* over the existing domain records, not a replacement for
them: :class:`SkillContract` is lifted from a compiler-produced
``DraftSpec`` / ``draft_spec`` dict, and the loop's outputs (critique, test
cases/runs, evaluation, revisions) are reported in these shapes. The existing
``models.SkillCandidateRecord`` / ``SkillEvaluationRecord`` /
``SKILL_STATUSES`` / ``CANDIDATE_STATUSES`` are untouched (INC43 §3.3).

Every model carries a ``to_dict`` so the router layer can serialise without a
second projection, and so the ``skill_engineering_runs`` archive (Phase 2) is a
straight JSONB dump.

Honesty rules baked into the shapes:

* :class:`SkillEvaluation` reports ``pass_rate`` (= ``passed / total``) **and**
  ``verified_pass_rate`` whose denominator deliberately **excludes** ``error``
  verdicts, so a tooling failure can never masquerade as a pass.
* :class:`SkillTestRun.verdict`` is one of ``pass`` / ``fail`` / ``error`` —
  ``error`` means "the sandbox could not decide", which is *never* collapsed to
  ``pass``.
* ``SkillContract.risk_level == "high"`` is the HITL trigger consumed by the
  lifecycle machine (never silently published).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "RISK_LEVELS",
    "LOW_RISK",
    "MEDIUM_RISK",
    "HIGH_RISK",
    "CRITIQUE_SEVERITIES",
    "TEST_CATEGORIES",
    "TEST_VERDICTS",
    "SkillContract",
    "SkillCritique",
    "SkillTestCase",
    "SkillTestRun",
    "SkillEvaluation",
    "SkillRevision",
]

#: Risk tiers a contract may declare. ``high`` ⇒ mandatory human review (HITL).
RISK_LEVELS = ("low", "medium", "high")
LOW_RISK = "low"
MEDIUM_RISK = "medium"
HIGH_RISK = "high"

#: Critique severity ladder — ``high`` findings become ``must_fix`` blockers.
CRITIQUE_SEVERITIES = ("none", "low", "medium", "high")

#: The four test categories ``generate_tests`` must cover (INC43 §2.3).
TEST_CATEGORIES = ("normal", "boundary", "adversarial", "security")

#: Allowed ``SkillTestRun.verdict`` values. ``error`` never means "pass".
TEST_VERDICTS = ("pass", "fail", "error")


@dataclass
class SkillContract:
    """A fully-shaped, reviewable skill definition (BE-3 contract surface)."""

    goal: str = ""
    preconditions: list[str] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    procedure: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    policies: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    applicable_when: dict[str, Any] = field(default_factory=dict)
    not_applicable_when: dict[str, Any] = field(default_factory=dict)
    risk_level: str = LOW_RISK

    def is_complete(self) -> bool:
        """True when the four gating elements are declared.

        Mirrors ``DraftSpec.is_complete`` (prompt / steps / tools / io_schema →
        goal / procedure / tools / (inputs|outputs)) so ``DRAFT → CANDIDATE``
        means exactly "the four elements are present".
        """
        return bool(
            self.goal
            and self.procedure
            and self.tools
            and (self.inputs or self.outputs)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SkillContract:
        data = data or {}
        return cls(
            goal=str(data.get("goal", "")),
            preconditions=[str(p) for p in data.get("preconditions", []) or []],
            inputs={str(k): str(v) for k, v in (data.get("inputs", {}) or {}).items()},
            outputs={str(k): str(v) for k, v in (data.get("outputs", {}) or {}).items()},
            procedure=[str(s) for s in data.get("procedure", []) or []],
            tools=[str(t) for t in data.get("tools", []) or []],
            policies=[str(p) for p in data.get("policies", []) or []],
            verification=[str(v) for v in data.get("verification", []) or []],
            applicable_when=dict(data.get("applicable_when", {}) or {}),
            not_applicable_when=dict(data.get("not_applicable_when", {}) or {}),
            risk_level=str(data.get("risk_level", LOW_RISK) or LOW_RISK),
        )

    @classmethod
    def from_draft_spec(cls, draft_spec: dict[str, Any] | None) -> SkillContract:
        """Lift a compiler ``draft_spec`` dict into a :class:`SkillContract`.

        ``prompt → goal``, ``steps → procedure``, ``tools → tools``,
        ``io_schema.{input,output} → inputs/outputs``, ``applicable_when`` is
        carried through. Fields the draft cannot express stay empty — **nothing
        is fabricated** (no invented verification / preconditions).
        """
        spec = draft_spec or {}
        io_schema = spec.get("io_schema", {}) or {}
        inputs = io_schema.get("input", {}) if isinstance(io_schema, dict) else {}
        outputs = io_schema.get("output", {}) if isinstance(io_schema, dict) else {}
        return cls(
            goal=str(spec.get("prompt", "")),
            inputs={str(k): str(v) for k, v in (inputs or {}).items()},
            outputs={str(k): str(v) for k, v in (outputs or {}).items()},
            procedure=[str(s) for s in spec.get("steps", []) or []],
            tools=[str(t) for t in spec.get("tools", []) or []],
            applicable_when=dict(spec.get("applicable_when", {}) or {}),
        )

    def to_draft_spec(self) -> dict[str, Any]:
        """Project back to the compiler's ``draft_spec`` shape (for reuse)."""
        return {
            "prompt": self.goal,
            "steps": list(self.procedure),
            "tools": list(self.tools),
            "io_schema": {
                "input": dict(self.inputs),
                "output": dict(self.outputs),
            },
            "applicable_when": dict(self.applicable_when),
        }


@dataclass
class SkillCritique:
    """An independent review verdict over a contract (jump ④)."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    severity: str = "none"
    must_fix: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [dict(f) for f in self.findings],
            "severity": self.severity,
            "must_fix": list(self.must_fix),
        }


@dataclass
class SkillTestCase:
    """One generated test case (jump ⑤)."""

    id: str = ""
    category: str = "normal"
    input: dict[str, Any] = field(default_factory=dict)
    expectation: str = ""
    assertion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "input": dict(self.input),
            "expectation": self.expectation,
            "assertion": self.assertion,
        }


@dataclass
class SkillTestRun:
    """The deterministic sandbox verdict for one case."""

    case_id: str = ""
    verdict: str = "error"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "verdict": self.verdict, "detail": self.detail}


@dataclass
class SkillEvaluation:
    """Aggregate sandbox result over a case set (jump ⑥)."""

    pass_rate: float = 0.0
    verified_pass_rate: float = 0.0
    failure_modes: list[str] = field(default_factory=list)
    sample_size: int = 0
    ran_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_rate": self.pass_rate,
            "verified_pass_rate": self.verified_pass_rate,
            "failure_modes": list(self.failure_modes),
            "sample_size": self.sample_size,
            "ran_at": self.ran_at,
        }


@dataclass
class SkillRevision:
    """A single repair round (jump ⑦). Never overwrites an existing semver."""

    from_semver: str = ""
    to_semver: str = ""
    reason: str = ""
    diff_summary: dict[str, Any] = field(default_factory=dict)
    round: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_semver": self.from_semver,
            "to_semver": self.to_semver,
            "reason": self.reason,
            "diff_summary": dict(self.diff_summary),
            "round": self.round,
        }
