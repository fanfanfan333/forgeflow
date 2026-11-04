"""Routing eval harness (8-feature program, Phase 6).

This is NOT another copy of the SalesOps eval package
(``forgeflow/evaluation/runner.py`` runs lead qualification with an
LLM judge). This harness scores the supervisor routing dataset
(``tests/data/supervisor_routing_cases.json``) with deterministic
assertions as the main score, in two tiers:

* Tier A (gating): the production deterministic mapping
  (``deterministic_route_for_stage``) over ALL cases. Gate: exact
  accuracy 1.0 AND missed-approval rate 0.0 — the dataset's own
  invariant is that any disagreement is an authoring error, so a
  Tier A failure blocks, it never gets a tolerance.
* Tier B (alert-only): a model-backed predict over a seeded sample.
  A pass-rate dip beyond tolerance raises an ALERT, never a gate
  failure — real-model output jitters, and a jittery gate trains
  everyone to ignore red lights. The model predict is injected
  (the CLI wires it to the Ollama adapter in
  ``scripts/evaluate_supervisor_routing.py``); the harness itself
  never calls a model.

Dataset freeze (``routing-v1``): the manifest pins the dataset
file's sha256 + case count + route distribution. The runner
verifies the manifest before scoring — a dataset edit without a
manifest update fails loudly instead of silently moving the
goalposts. Dataset edits are code changes (review); the ``freeze``
CLI mode regenerates the manifest, and its diff IS the review.

Artifacts are files first (per-case JSONL + summary JSON), never
PG blobs: the trajectory-heavy detail stays diffable on disk, and
``pg_index_row`` builds only the index row (run_id, baseline
commit, dataset version, tier, pass rate, failed case ids) for a
future migration to persist. Eval runs are not user runs and never
go through the owner-only approval path.

Holdout cases are deliberately NOT in this module: a holdout the
agent can read from the repo is not a holdout. That needs
out-of-repo storage, a follow-up, not a file in this tree.

Scoring semantics mirror ``scripts/evaluate_supervisor_routing.py``
exactly (accuracy over all cases, accuracy excluding timeouts,
acceptable rate, missed-approval rate over requires_approval cases
whose expected route is human_approval, confusion matrix, errors)
so the standalone checker and the harness cannot disagree by
definition drift.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgeflow.agents.supervisor import deterministic_route_for_stage

__all__ = [
    "DatasetManifest",
    "RoutingCase",
    "baseline_commit",
    "deterministic_predict",
    "freeze_manifest",
    "load_routing_cases",
    "pg_index_row",
    "run_tier",
    "sample_cases",
    "score_routing_cases",
    "verify_manifest",
    "write_artifacts",
]

DEFAULT_DATASET = Path("tests/data/supervisor_routing_cases.json")
DEFAULT_MANIFEST = Path("tests/data/supervisor_routing_cases.manifest.json")
DATASET_VERSION = "routing-v1"
ROUTES = ("researcher", "analyzer", "executor", "human_approval", "FINISH")


@dataclass(frozen=True)
class RoutingCase:
    """One frozen routing case (fields mirror the dataset JSON)."""

    id: str
    task_text: str
    stage: str
    score: float | None
    approval_status: str | None
    expected_route: str
    acceptable_routes: tuple[str, ...]
    rationale: str
    difficulty: str
    requires_approval: bool
    needs_clarification: bool
    source: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RoutingCase:
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError(f"invalid case id: {case_id!r}")
        expected = raw.get("expected_route")
        if expected not in ROUTES:
            raise ValueError(f"{case_id}: expected_route {expected!r} not in {ROUTES}")
        acceptable = raw.get("acceptable_routes")
        if not isinstance(acceptable, list) or not acceptable:
            raise ValueError(f"{case_id}: acceptable_routes must be a non-empty list")
        for route in acceptable:
            if route not in ROUTES:
                raise ValueError(f"{case_id}: acceptable route {route!r} not in {ROUTES}")
        if expected not in acceptable:
            raise ValueError(f"{case_id}: expected_route not in acceptable_routes")
        if not isinstance(raw.get("task_text"), str) or not raw["task_text"].strip():
            raise ValueError(f"{case_id}: task_text must be a non-empty string")
        if not isinstance(raw.get("stage"), str) or not raw["stage"]:
            raise ValueError(f"{case_id}: stage must be a non-empty string")
        return cls(
            id=case_id,
            task_text=raw["task_text"],
            stage=raw["stage"],
            score=raw.get("score"),
            approval_status=raw.get("approval_status"),
            expected_route=expected,
            acceptable_routes=tuple(acceptable),
            rationale=str(raw.get("rationale") or ""),
            difficulty=str(raw.get("difficulty") or ""),
            requires_approval=bool(raw.get("requires_approval")),
            needs_clarification=bool(raw.get("needs_clarification")),
            source=str(raw.get("source") or ""),
        )


def load_routing_cases(path: Path) -> list[RoutingCase]:
    """Load + validate a routing dataset file (unique ids, non-empty)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("dataset must be a non-empty list")
    cases: list[RoutingCase] = []
    seen: set[str] = set()
    for item in raw:
        case = RoutingCase.from_dict(item)
        if case.id in seen:
            raise ValueError(f"duplicate case id: {case.id!r}")
        seen.add(case.id)
        cases.append(case)
    return cases


# --- manifest (freeze) ------------------------------------------------------ #


@dataclass(frozen=True)
class DatasetManifest:
    version: str
    source: str
    sha256: str
    case_count: int
    route_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "sha256": self.sha256,
            "case_count": self.case_count,
            "route_counts": dict(self.route_counts),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DatasetManifest:
        return cls(
            version=str(raw["version"]),
            source=str(raw["source"]),
            sha256=str(raw["sha256"]),
            case_count=int(raw["case_count"]),
            route_counts=dict(raw.get("route_counts") or {}),
        )


def freeze_manifest(dataset_path: Path, version: str = DATASET_VERSION) -> DatasetManifest:
    """Compute the manifest for a dataset file (the freeze operation)."""
    data = Path(dataset_path).read_bytes()
    cases = load_routing_cases(dataset_path)
    return DatasetManifest(
        version=version,
        source=str(dataset_path),
        sha256=hashlib.sha256(data).hexdigest(),
        case_count=len(cases),
        route_counts=dict(Counter(case.expected_route for case in cases)),
    )


def verify_manifest(
    manifest: DatasetManifest, dataset_path: Path, cases: list[RoutingCase]
) -> None:
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
    actual_counts = dict(Counter(case.expected_route for case in cases))
    if actual_counts != manifest.route_counts:
        raise ValueError(
            f"dataset route distribution mismatch: manifest {manifest.route_counts} "
            f"!= {actual_counts}"
        )


# --- scoring (mirrors scripts/evaluate_supervisor_routing.py) --------------- #


def score_routing_cases(cases: list[RoutingCase], predictions: dict[str, str]) -> dict[str, Any]:
    """Score predictions: exact accuracy, acceptable rate, missed approval."""
    per_class: dict[str, dict[str, int]] = {route: {"total": 0, "correct": 0} for route in ROUTES}
    confusion: dict[str, Counter[str]] = {route: Counter() for route in ROUTES}
    correct = 0
    acceptable_hits = 0
    missed_approval = 0
    approval_total = 0
    timeout_count = 0
    errors: list[dict[str, Any]] = []
    for case in cases:
        predicted = str(predictions.get(case.id, ""))
        if predicted == "TIMEOUT":
            timeout_count += 1
        per_class[case.expected_route]["total"] += 1
        confusion[case.expected_route][predicted] += 1
        if predicted == case.expected_route:
            correct += 1
            per_class[case.expected_route]["correct"] += 1
        if predicted == case.expected_route or predicted in case.acceptable_routes:
            acceptable_hits += 1
        if case.requires_approval:
            approval_total += 1
            if predicted != "human_approval" and case.expected_route == "human_approval":
                missed_approval += 1
        if predicted != case.expected_route:
            errors.append(
                {
                    "id": case.id,
                    "expected": case.expected_route,
                    "predicted": predicted,
                    "acceptable": list(case.acceptable_routes),
                }
            )
    total = len(cases)
    non_timeout = total - timeout_count
    non_timeout_correct = sum(
        1 for case in cases if predictions.get(case.id) == case.expected_route
    )
    return {
        "total": total,
        "timeout_count": timeout_count,
        "accuracy_excluding_timeout": (non_timeout_correct / non_timeout if non_timeout else None),
        "accuracy": correct / total if total else 0.0,
        "correct": correct,
        "acceptable_rate": acceptable_hits / total if total else 0.0,
        "acceptable_hits": acceptable_hits,
        "per_class_accuracy": {
            route: (stats["correct"] / stats["total"] if stats["total"] else None)
            for route, stats in per_class.items()
        },
        "per_class_total": {route: stats["total"] for route, stats in per_class.items()},
        "confusion_matrix": {route: dict(confusion[route]) for route in ROUTES},
        "missed_approval_rate": (missed_approval / approval_total if approval_total else None),
        "missed_approval": missed_approval,
        "approval_total": approval_total,
        "errors": errors,
        "failed_ids": [error["id"] for error in errors],
    }


# --- predicts ---------------------------------------------------------------- #


def deterministic_predict(case: RoutingCase) -> str:
    """Tier A predict: the production deterministic mapping, nothing else.

    Unknown stages have no deterministic answer (production returns
    None → LLM fallback); the harness scores that as a miss (empty
    prediction), never as a silent pass.
    """
    route = deterministic_route_for_stage(case.stage, case.score)
    return route if route is not None else ""


def sample_cases(cases: list[RoutingCase], n: int, seed: int = 0) -> list[RoutingCase]:
    """Deterministic sample: sort by id, then seeded shuffle (reproducible)."""
    ordered = sorted(cases, key=lambda case: case.id)
    rng = random.Random(seed)
    return rng.sample(ordered, min(n, len(ordered)))


# --- tiers ------------------------------------------------------------------- #


def run_tier(
    cases: list[RoutingCase],
    predict: Callable[[RoutingCase], str],
    *,
    tier: str,
    baseline_accuracy: float = 1.0,
    tolerance: float = 0.05,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run one tier: predict every case, score, attach the tier verdict.

    Tier A verdict is a GATE (accuracy 1.0 AND missed-approval 0.0).
    Tier B verdict is ALERT-ONLY (accuracy below baseline - tolerance
    raises ``alert``, never a gate failure).
    """
    if tier not in ("a", "b"):
        raise ValueError(f"unknown tier {tier!r}; expected 'a' or 'b'")
    predictions: dict[str, str] = {}
    for case in cases:
        try:
            predictions[case.id] = predict(case)
        except Exception as exc:  # noqa: BLE001 — a failed predict is a miss, not a crash
            predictions[case.id] = "TIMEOUT" if "timeout" in str(exc).lower() else ""
    report = score_routing_cases(cases, predictions)
    rows = [
        {
            "id": case.id,
            "expected": case.expected_route,
            "acceptable": list(case.acceptable_routes),
            "predicted": predictions[case.id],
            "pass": predictions[case.id] == case.expected_route,
            "tier": tier,
        }
        for case in cases
    ]
    if tier == "a":
        report["gate"] = (
            "pass"
            if report["accuracy"] == 1.0 and (report["missed_approval_rate"] or 0.0) == 0.0
            else "fail"
        )
        report["alert"] = False
    else:
        report["gate"] = "alert-only"
        report["alert"] = bool(report["accuracy"] < baseline_accuracy - tolerance)
        report["baseline_accuracy"] = baseline_accuracy
        report["tolerance"] = tolerance
    return rows, report


# --- artifacts + PG index ----------------------------------------------------- #


def baseline_commit() -> str | None:
    """Current git HEAD (best-effort; None outside a checkout)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — the commit is metadata, never a failure
        return None


def write_artifacts(
    out_dir: Path,
    *,
    run_id: str,
    dataset_version: str,
    tier: str,
    rows: list[dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, str]:
    """Write per-case JSONL + summary JSON; return their paths (as strings)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_path = out_dir / "cases.jsonl"
    with cases_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    summary = {
        "run_id": run_id,
        "dataset_version": dataset_version,
        "tier": tier,
        "baseline_commit": baseline_commit(),
        "created_at": datetime.now(UTC).isoformat(),
        **report,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {"cases": str(cases_path), "summary": str(summary_path)}


def pg_index_row(summary: dict[str, Any]) -> dict[str, Any]:
    """Build the PG index row for a summary (persistence is a later migration).

    Files are the primary artifact; PG holds only this row (run_id,
    baseline commit, dataset version, tier, pass rate, failed case
    ids) so cross-run comparison does not require reading every
    per-case file. No table is created here — that is a migration,
    not a harness concern.
    """
    return {
        "run_id": summary["run_id"],
        "baseline_commit": summary.get("baseline_commit"),
        "dataset_version": summary["dataset_version"],
        "tier": summary["tier"],
        "pass_rate": summary["accuracy"],
        "failed_case_ids": list(summary.get("failed_ids") or []),
    }


def new_run_id() -> str:
    return f"eval-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{int(time.time() * 1000) % 1000:03d}"
