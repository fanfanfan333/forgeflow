"""INC46 T07 — contract completion (fill *safe defaults with provenance*, never fabricate).

What "completion" means here
----------------------------
A partially-authored seven-segment contract often lacks *derivable* or
*defaultable* values that the platform can supply deterministically — a ``slug``
that can be derived from a display name (A3), or the standard ``assets/evals/``
reference for the Evaluation segment. :func:`complete_contract` fills exactly
those, and records **one provenance entry per action** so a reviewer can see
what was supplied and why.

What completion must **never** do
---------------------------------
It must not invent the *content* of a missing required segment. If Examples,
Knowledge (or any other required segment) is absent, completion leaves it absent
and :func:`validate_contract_document` reports it as an explicit missing-segment
error. There is no path from "absent" to "silently filled".

Every action is one of three, all recorded:

* ``normalised`` — a tolerated segment key was mapped to its canonical key;
* ``derived`` — a value was computed from other declared data (slug ← display
  name);
* ``defaulted`` — a spec-defined default was applied (``evaluation.ref``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from forgeflow.skills import segments as _seg
from forgeflow.skills.schemas import (
    ContractValidationReport,
    SkillContractDocument,
    validate_contract_document,
)
from forgeflow.skills.segments import (
    SEGMENT_EVALUATION,
    SEGMENT_MANIFEST,
    SEGMENT_ORDER,
)
from forgeflow.skills.spec_mapping import suggest_slug

__all__ = [
    "ProvenanceEntry",
    "CompletionResult",
    "complete_contract",
    "missing_required_segments",
]

#: The spec-defined default location for the (spec-unc)overed Evaluation segment.
DEFAULT_EVALUATION_REF = "assets/evals/"


@dataclass(frozen=True)
class ProvenanceEntry:
    """One completion action: what the field is, how, and the concrete detail."""

    field: str
    action: str  # "normalised" | "derived" | "defaulted" | "skipped"
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "action": self.action, "detail": self.detail}


@dataclass
class CompletionResult:
    """The completed (normalised) contract plus its provenance and report.

    ``contract`` is always returned — possibly still incomplete. ``ok`` is True
    only when the completed contract passed validation (no missing required
    segment, no illegal field).
    """

    contract: dict[str, Any] = field(default_factory=dict)
    provenance: list[ProvenanceEntry] = field(default_factory=list)
    report: ContractValidationReport = field(default_factory=ContractValidationReport)

    @property
    def ok(self) -> bool:
        return self.report.ok

    @property
    def errors(self) -> list[str]:
        return list(self.report.errors)

    @property
    def findings(self) -> list[str]:
        return list(self.report.findings)

    @property
    def document(self) -> SkillContractDocument | None:
        return self.report.document

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "findings": self.findings,
            "provenance": [p.to_dict() for p in self.provenance],
        }


def missing_required_segments(data: Mapping[str, Any] | None) -> list[str]:
    """Required segments absent from ``data`` (canonical keys, segment order)."""
    if not isinstance(data, Mapping):
        return list(_seg.REQUIRED_SEGMENTS)
    present = {
        _seg.normalise_segment_key(key)
        for key in data
    }
    return [s for s in _seg.REQUIRED_SEGMENTS if s not in present]


def complete_contract(
    data: Mapping[str, Any] | None,
    *,
    parent_dir: str | None = None,
) -> CompletionResult:
    """Fill derivable / defaultable values with recorded provenance, then validate.

    Actions (each recorded in :attr:`CompletionResult.provenance`):

    1. **normalise** tolerated segment keys (``"Tool bindings"`` →
       ``"tool_bindings"``);
    2. **derive** ``manifest.name`` from ``manifest.display_name`` via
       :func:`forgeflow.skills.spec_mapping.suggest_slug` when the slug is
       missing and derivable (A3); a non-derivable display name (e.g. pure
       Chinese) is *skipped* with an explicit note — it becomes a validation
       error rather than an invented slug;
    3. **default** ``evaluation.ref`` to ``assets/evals/`` only when the
       Evaluation segment is present but its reference is empty.

    Nothing else is filled. A missing required segment stays missing and is
    reported by :func:`validate_contract_document` (禁止静默补全).
    """
    provenance: list[ProvenanceEntry] = []

    norm: dict[str, Any] = {}
    for key, value in (data or {}).items():
        canonical = _seg.normalise_segment_key(key)
        norm[canonical] = _copy_segment(value)
        if canonical != str(key):
            provenance.append(
                ProvenanceEntry(
                    field=f"segment:{key}",
                    action="normalised",
                    detail=f"段键 '{key}' → '{canonical}'",
                )
            )

    manifest = norm.get(SEGMENT_MANIFEST)
    if isinstance(manifest, dict):
        if not str(manifest.get("name") or "").strip() and str(
            manifest.get("display_name") or ""
        ).strip():
            display_name = str(manifest["display_name"])
            try:
                slug = suggest_slug(display_name)
            except ValueError as exc:
                provenance.append(
                    ProvenanceEntry(
                        field="manifest.name",
                        action="skipped",
                        detail=f"无法派生 ASCII slug：{exc}",
                    )
                )
            else:
                manifest["name"] = slug
                provenance.append(
                    ProvenanceEntry(
                        field="manifest.name",
                        action="derived",
                        detail=(
                            f"由 display_name {display_name!r} 派生 slug {slug!r}"
                            "（A3：系统生成并锁定，改名不变目录名）"
                        ),
                    )
                )
        elif isinstance(manifest.get("name"), str):
            manifest["name"] = manifest["name"].strip()
        if parent_dir and not manifest.get("parent_dir"):
            manifest["parent_dir"] = parent_dir
            provenance.append(
                ProvenanceEntry(
                    field="manifest.parent_dir",
                    action="defaulted",
                    detail=f"由调用方提供父目录名 {parent_dir!r}",
                )
            )

    evaluation = norm.get(SEGMENT_EVALUATION)
    if isinstance(evaluation, dict) and not str(evaluation.get("ref") or "").strip():
        evaluation["ref"] = DEFAULT_EVALUATION_REF
        provenance.append(
            ProvenanceEntry(
                field="evaluation.ref",
                action="defaulted",
                detail=(
                    f"缺省评估目录 {DEFAULT_EVALUATION_REF}"
                    "（上游规范未涵盖评估段；权威评估记录在 ForgeFlow DB）"
                ),
            )
        )

    report = validate_contract_document(norm, parent_dir=parent_dir)
    # Keep the completed contract in canonical segment order for stable snapshots.
    ordered = {s: norm[s] for s in SEGMENT_ORDER if s in norm}
    for extra in (k for k in norm if k not in ordered):
        ordered[extra] = norm[extra]
    return CompletionResult(contract=ordered, provenance=provenance, report=report)


def _copy_segment(value: Any) -> Any:
    """Shallow-copy a segment mapping/list so completion never mutates the input."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value
