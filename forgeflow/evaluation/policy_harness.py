"""Approval-policy eval harness (Phase 9 B6: the harness's second dataset).

The routing harness (Phase 6) froze ONE dataset. This module proves
the harness pattern generalises: a second frozen dataset, this one
for the Phase 4 pre-approval policy — which tools are gated by
default. The predict function is the DEFAULT policy
(``TOOL_PERMISSION_MAP`` membership): registry extensions added at
runtime are additive and out of this frozen set's scope, by design.

Manifest semantics mirror the routing harness exactly: sha256-pinned,
count-pinned, distribution-pinned; drift without a freeze fails
loudly. Tier: gating (a policy regression is a security regression).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DATASET_VERSION = "approval-v1"
DEFAULT_DATASET = Path("tests/data/approval_policy_cases.json")
DEFAULT_MANIFEST = Path("tests/data/approval_policy_cases.manifest.json")


@dataclass(frozen=True)
class PolicyCase:
    """One frozen policy case: is this tool gated by default?"""

    id: str
    tool: str
    expected_gated: bool
    rationale: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PolicyCase:
        return cls(
            id=str(raw["id"]),
            tool=str(raw["tool"]),
            expected_gated=bool(raw["expected_gated"]),
            rationale=str(raw.get("rationale") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "expected_gated": self.expected_gated,
            "rationale": self.rationale,
        }


def load_policy_cases(path: Path) -> list[PolicyCase]:
    """Load + validate a policy dataset (duplicate ids fail loudly)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("policy dataset must be a JSON list")
    cases = [PolicyCase.from_dict(item) for item in raw]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("policy dataset has duplicate case ids")
    return cases


@dataclass(frozen=True)
class PolicyManifest:
    """Frozen manifest for a policy dataset (sha256 + counts + distribution)."""

    version: str
    source: str
    sha256: str
    case_count: int
    gated_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "sha256": self.sha256,
            "case_count": self.case_count,
            "gated_counts": dict(self.gated_counts),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PolicyManifest:
        return cls(
            version=str(raw["version"]),
            source=str(raw["source"]),
            sha256=str(raw["sha256"]),
            case_count=int(raw["case_count"]),
            gated_counts=dict(raw.get("gated_counts") or {}),
        )


def freeze_manifest(dataset_path: Path, version: str = DATASET_VERSION) -> PolicyManifest:
    """Compute the manifest for a policy dataset file (the freeze operation)."""
    data = Path(dataset_path).read_bytes()
    cases = load_policy_cases(dataset_path)
    return PolicyManifest(
        version=version,
        source=str(dataset_path),
        sha256=hashlib.sha256(data).hexdigest(),
        case_count=len(cases),
        gated_counts=dict(Counter(str(case.expected_gated) for case in cases)),
    )


def verify_manifest(manifest: PolicyManifest, dataset_path: Path, cases: list[PolicyCase]) -> None:
    """Raise on any drift between manifest and dataset (fail loudly)."""
    data = Path(dataset_path).read_bytes()
    actual_sha = hashlib.sha256(data).hexdigest()
    if actual_sha != manifest.sha256:
        raise ValueError(
            f"dataset sha256 mismatch: manifest {manifest.sha256} != file {actual_sha} "
            "(dataset changed without a manifest freeze — update via --freeze and review)"
        )
    if len(cases) != manifest.case_count:
        raise ValueError(
            f"dataset case count mismatch: manifest {manifest.case_count} != {len(cases)}"
        )
    actual_counts = dict(Counter(str(case.expected_gated) for case in cases))
    if actual_counts != manifest.gated_counts:
        raise ValueError(
            f"dataset gated distribution mismatch: manifest {manifest.gated_counts} "
            f"!= {actual_counts}"
        )


def default_predict(tool: str) -> bool:
    """The frozen contract: default-policy gating (TOOL_PERMISSION_MAP)."""
    from forgeflow.runtime.approval_policy import TOOL_PERMISSION_MAP

    return tool in TOOL_PERMISSION_MAP


def score_policy_cases(cases: list[PolicyCase], predictions: dict[str, bool]) -> dict[str, Any]:
    """Score predictions: accuracy + false-positive / false-negative ids."""
    correct = 0
    false_positives: list[str] = []
    false_negatives: list[str] = []
    for case in cases:
        predicted = bool(predictions.get(case.id, False))
        if predicted == case.expected_gated:
            correct += 1
        elif predicted and not case.expected_gated:
            false_positives.append(case.id)
        else:
            false_negatives.append(case.id)
    total = len(cases)
    return {
        "total": total,
        "correct": correct,
        "accuracy": (correct / total) if total else 0.0,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "gate": "pass" if total and correct == total else "fail",
    }
