"""Domain errors for the Skill Hub (mapped to HTTP by the routers)."""

from __future__ import annotations


class SkillError(Exception):
    """Base class for Skill Hub domain errors."""


class InsufficientExperiencesError(SkillError):
    """Not enough similar experiences to compile a candidate (jump ③ gate)."""

    def __init__(self, found: int, required: int) -> None:
        self.found = found
        self.required = required
        super().__init__(
            f"insufficient similar experiences: found {found}, required {required}"
        )


class EvaluationError(SkillError):
    """A candidate lacks a passing evaluation."""


class GovernanceError(SkillError):
    """A promotion was rejected by the governance gate (→ 403)."""

    def __init__(self, reason: str, *, status_code: int = 403) -> None:
        self.status_code = status_code
        super().__init__(reason)
