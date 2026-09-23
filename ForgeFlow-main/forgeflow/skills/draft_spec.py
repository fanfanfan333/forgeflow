"""DraftSpec — the four-element reusable skill draft (jump ③ output).

The spec is what a human reviews/edits before a candidate is evaluated and
promoted. It intentionally stays JSON-serialisable so it can be stored in
``skill_candidates.draft_spec`` (JSONB) verbatim.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class DraftSpec:
    """Compiled skill draft: prompt / steps / tools / io_schema (+ conditions)."""

    prompt: str = ""
    steps: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    io_schema: dict[str, Any] = field(default_factory=dict)
    applicable_when: dict[str, Any] = field(default_factory=dict)

    def is_complete(self) -> bool:
        """True when all four required elements are non-empty."""
        return bool(
            self.prompt
            and self.steps
            and self.tools
            and isinstance(self.io_schema, dict)
            and self.io_schema
        )

    def missing(self) -> list[str]:
        """Names of the required elements that are empty (for diagnostics)."""
        gaps: list[str] = []
        if not self.prompt:
            gaps.append("prompt")
        if not self.steps:
            gaps.append("steps")
        if not self.tools:
            gaps.append("tools")
        if not self.io_schema:
            gaps.append("io_schema")
        return gaps

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DraftSpec":
        data = data or {}
        return cls(
            prompt=str(data.get("prompt", "")),
            steps=list(data.get("steps", [])),
            tools=list(data.get("tools", [])),
            io_schema=dict(data.get("io_schema", {})),
            applicable_when=dict(data.get("applicable_when", {})),
        )
