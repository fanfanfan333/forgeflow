"""Reporter — execution summary, failure aggregation and the final report (§42/§72).

Layer L4. Turns a list of :class:`Evidence` into the human-readable artifacts:

* ``execution_summary.json`` — one row per test with its command, environment,
  exit code, duration and artifact paths (§42).
* ``failure_analysis.json`` — the §90 aggregation over the 14 failure classes.
* ``final_report.md`` — the §72 conclusion (a subset for A–C) plus evidence
  pointers.

The verdict rule is the one that matters: **SKIP ≠ PASS**. A run with nothing
that actually passed is ``INCONCLUSIVE`` — never ``PASS`` — and any ``FAIL`` or
``BLOCKED`` dominates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from forgeflow.qa.evidence import EvidenceStore, safe_id
from forgeflow.qa.models import (
    CapabilityInventory,
    Evidence,
    EvidenceStatus,
    FailureClass,
    TestPlanItem,
    Verdict,
)

logger = logging.getLogger(__name__)


class Reporter:
    """Builds the run's summary/analysis artifacts and its verdict."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def execution_summary(self, evidence: list[Evidence]) -> dict[str, Any]:
        """Per-test execution facts (§42)."""
        results: list[dict[str, Any]] = []
        counts: dict[str, int] = {status.value: 0 for status in EvidenceStatus}
        for record in evidence:
            counts[record.status.value] = counts.get(record.status.value, 0) + 1
            results.append(
                {
                    "test_id": record.test_id,
                    "capability_id": record.capability_id,
                    "status": record.status.value,
                    "command": record.command,
                    "cwd": record.environment.get("cwd"),
                    "environment": record.environment,
                    "exit_code": record.exit_code,
                    "duration_ms": record.duration_ms,
                    "stdout_artifact": record.stdout_artifact,
                    "stderr_artifact": record.stderr_artifact,
                    "evidence_path": f"evidence/{safe_id(record.test_id)}.json",
                    "failure_class": (record.failure_class.value if record.failure_class else None),
                }
            )
        return {"total": len(evidence), "counts": counts, "results": results}

    def failure_analysis(self, evidence: list[Evidence]) -> dict[str, Any]:
        """§90 failure aggregation over the 14 classes."""
        by_failure_class: dict[str, int] = {cls.value: 0 for cls in FailureClass}
        by_status: dict[str, int] = {status.value: 0 for status in EvidenceStatus}
        failures: list[dict[str, Any]] = []
        for record in evidence:
            by_status[record.status.value] = by_status.get(record.status.value, 0) + 1
            if record.failure_class is not None:
                by_failure_class[record.failure_class.value] = (
                    by_failure_class.get(record.failure_class.value, 0) + 1
                )
            if record.status in (EvidenceStatus.FAIL, EvidenceStatus.BLOCKED):
                failures.append(
                    {
                        "test_id": record.test_id,
                        "status": record.status.value,
                        "failure_class": (
                            record.failure_class.value if record.failure_class else None
                        ),
                    }
                )
        return {
            "total": len(evidence),
            "by_failure_class": by_failure_class,
            "by_status": by_status,
            "failures": failures,
        }

    def verdict(self, evidence: list[Evidence]) -> Verdict:
        """§72 subset verdict. SKIP/INCONCLUSIVE never count as PASS."""
        if not evidence:
            return Verdict.INCONCLUSIVE

        def _count(status: EvidenceStatus) -> int:
            return sum(1 for record in evidence if record.status is status)

        if _count(EvidenceStatus.FAIL):
            return Verdict.FAIL
        if _count(EvidenceStatus.BLOCKED):
            return Verdict.BLOCKED
        if _count(EvidenceStatus.PASS) == 0:
            return Verdict.INCONCLUSIVE
        if _count(EvidenceStatus.INCONCLUSIVE) or _count(EvidenceStatus.SKIP):
            return Verdict.PASS_WITH_WARNINGS
        return Verdict.PASS

    def write_final_report(
        self,
        *,
        inventory: CapabilityInventory,
        plan: list[TestPlanItem],
        summary: dict[str, Any],
        analysis: dict[str, Any],
        verdict: Verdict,
        regression: dict[str, Any] | None = None,
    ) -> Path:
        """Render and persist ``final_report.md`` (§42/§72 subset).

        ``regression=None`` keeps the A–C output byte-for-byte identical to the
        previous behaviour; passing the Phase D regression report appends a
        "Regression scope & fingerprints" section (opt-in, so existing callers and
        their substring assertions are untouched).
        """
        return self._store.write_text(
            "final_report.md",
            self._render(inventory, plan, summary, analysis, verdict, regression),
        )

    def _render(
        self,
        inventory: CapabilityInventory,
        plan: list[TestPlanItem],
        summary: dict[str, Any],
        analysis: dict[str, Any],
        verdict: Verdict,
        regression: dict[str, Any] | None = None,
    ) -> str:
        lines: list[str] = []
        lines.append("# ForgeFlow QA Report (Phase A–C)")
        lines.append("")
        lines.append(f"- run_id: `{inventory.run_id}`")
        lines.append(f"- project root: `{inventory.root}`")
        lines.append(f"- capabilities detected: {inventory.totals.get('_all', 0)}")
        lines.append(f"- tests planned: {len(plan)}")
        lines.append(f"- **verdict: {verdict.value}**")
        lines.append("")
        lines.append("## Execution summary")
        lines.append("")
        lines.append("| test | status | failure_class | exit | duration_ms | evidence |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for row in summary.get("results", []):
            lines.append(
                "| {test} | {status} | {failure} | {exit_code} | {duration} | {evidence} |".format(
                    test=row.get("test_id"),
                    status=row.get("status"),
                    failure=row.get("failure_class") or "-",
                    exit_code=row.get("exit_code"),
                    duration=row.get("duration_ms"),
                    evidence=row.get("evidence_path"),
                )
            )
        lines.append("")
        lines.append("## Failure analysis (§90)")
        lines.append("")
        nonzero = {
            cls: count for cls, count in analysis.get("by_failure_class", {}).items() if count
        }
        if nonzero:
            for cls, count in nonzero.items():
                lines.append(f"- {cls}: {count}")
        else:
            lines.append("- no failures recorded")
        lines.append("")
        lines.append(
            "> SKIP and INCONCLUSIVE are reported honestly and are never counted as PASS (§72)."
        )
        lines.append("")
        if regression is not None:
            lines.extend(self._render_regression(regression, analysis))
        return "\n".join(lines)

    @staticmethod
    def _render_regression(regression: dict[str, Any], analysis: dict[str, Any]) -> list[str]:
        """Render the Phase D "regression scope & fingerprints" section (§20/§68/§69)."""
        scope = regression.get("scope") or {}
        lines: list[str] = []
        lines.append("## Regression scope & fingerprints (Phase D)")
        lines.append("")
        lines.append(f"- baseline available: {bool(regression.get('baseline_available', False))}")
        lines.append(f"- full-repo fallback: {bool(scope.get('full_repo_fallback', False))}")
        changed = scope.get("changed_files") or []
        lines.append(f"- changed files: {', '.join(changed) if changed else '-'}")
        modules = scope.get("affected_modules") or []
        lines.append(f"- affected modules: {', '.join(modules) if modules else '-'}")
        targets = scope.get("recommended_targets") or []
        lines.append(f"- recommended targets: {', '.join(targets) if targets else '-'}")
        bound = regression.get("pass_rate_lower_bound")
        lines.append(
            f"- pass-rate lower bound (Wilson): {bound if bound is not None else 'not measured'}"
        )
        lines.append(
            f"- environment-attributed failures: {regression.get('environment_attributed', 0)}"
        )
        lines.append(f"- source-bug candidates: {regression.get('source_candidates', 0)}")
        note = scope.get("coverage_note") or regression.get("coverage_note")
        if note:
            lines.append(f"- coverage: {note}")
        lines.append("")
        lines.append("### Failure fingerprints")
        lines.append("")
        fingerprints = analysis.get("fingerprints") or []
        if fingerprints:
            for fingerprint in fingerprints:
                signature = ", ".join((fingerprint.get("signature") or [])[:6])
                lines.append(
                    f"- `{fingerprint.get('fingerprint')}` "
                    f"({fingerprint.get('failure_class')}) x{fingerprint.get('count')} ⇒ {signature}"
                )
        else:
            lines.append("- no failing fingerprints recorded")
        lines.append("")
        return lines


__all__ = ["Reporter"]
