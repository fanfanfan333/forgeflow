"""QA orchestrator — the deterministic 7-step closed loop (§6).

Layer L4. It sequences Discover → Plan → Execute → Analyze → Report, writes each
stage's artifact into the run directory, and keeps a :class:`QAState` record of
what happened.

Determinism is a hard requirement (offline, no LLM): this orchestrator is a plain
sequential scheduler, deliberately **not** wired into ``forgeflow/agent/loop.py``.
The agent-loop integration is a later increment; pulling LangGraph/LLM into the
A–C path would break the offlineness that makes the evidence trustworthy.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

from forgeflow.qa.analyzer import FailureClassifier
from forgeflow.qa.config import QaRunConfig
from forgeflow.qa.discovery import RepositoryIntelligence
from forgeflow.qa.environment_guard import EnvFinding, EnvironmentGuard
from forgeflow.qa.evidence import EvidenceStore
from forgeflow.qa.executor import TestExecutor
from forgeflow.qa.models import (
    CapabilityInventory,
    DiscoveryReport,
    Evidence,
    QAState,
    TestPlanItem,
)
from forgeflow.qa.planner import TestPlanner
from forgeflow.qa.process_runner import ProcessRunner
from forgeflow.qa.regression import RegressionAnalyzer, changed_files_from_env
from forgeflow.qa.reporter import Reporter
from forgeflow.qa.task_runner import MakeTaskRunnerAdapter

logger = logging.getLogger(__name__)

Stage = Literal["discover", "plan", "run", "analyze", "report", "full"]

#: Cap on how much of one artifact is read back for failure fingerprinting. The
#: analysis needs the error lines, not the whole multi-megabyte transcript.
_TRANSCRIPT_READ_CAP = 500_000


class QAOrchestrator:
    """Owns one run: its evidence store, its tasks, and its seven-step loop."""

    def __init__(self, config: QaRunConfig) -> None:
        self._config = config
        self._runner = ProcessRunner(
            default_timeout_s=config.timeout_s,
            env_overrides=config.env_overrides(),
        )
        self._store = EvidenceStore.for_run(config)

        make_exe = os.environ.get("QA_MAKE") or shutil.which("make")
        # The interpreter is handed to make as a **forward-slash** path: GNU make
        # on Windows runs `$(PY)` through ``/bin/sh`` when one is on PATH, and sh
        # strips the backslashes out of a ``C:\...`` path (``C:Users...: command
        # not found``), which silently fails the Makefile's interpreter probe and
        # turns every target into a spurious FAIL. Forward slashes are accepted by
        # both ``cmd.exe`` and ``sh``, so the pipeline stays portable across hosts
        # (a no-op on POSIX).
        self._tasks = MakeTaskRunnerAdapter(
            Path(config.project_root) / "Makefile",
            sys.executable.replace("\\", "/"),
            self._runner,
            make_exe=make_exe,
        )
        self._guard = EnvironmentGuard(config)
        self._discovery = RepositoryIntelligence(config.project_root)
        self._planner = TestPlanner(config.project_root, self._tasks)
        self._classifier = FailureClassifier()
        self._executor = TestExecutor(self._runner, self._tasks, self._store, self._classifier)
        self._reporter = Reporter(self._store)
        # Phase D: the regression scope + baseline comparison engine. The default
        # path is offline / git-free; the baseline comes from QA_REGRESSION_BASELINE.
        self._regression = RegressionAnalyzer(config.project_root)

        self._state: QAState = {
            "root_causes": [],
            "change_sets": [],
            "regressions": [],
            "skill_candidates": [],
        }
        self._env_findings: list[EnvFinding] = []
        self._inventory: CapabilityInventory | None = None
        self._plan: list[TestPlanItem] = []
        self._evidence: list[Evidence] = []
        self._analysis: dict[str, Any] | None = None

    @property
    def state(self) -> QAState:
        """The evolving :class:`QAState` for this run."""
        return self._state

    @property
    def store(self) -> EvidenceStore:
        """The run's evidence store (its ``run_dir`` holds every artifact)."""
        return self._store

    # ── stages ───────────────────────────────────────────────────────────
    def discover(self) -> DiscoveryReport:
        """Environment guard + repository intelligence; write inventory/report."""
        self._env_findings = self._guard.run_all()
        report = self._discovery.discovery_report(run_id=self._config.run_id)
        self._store.write_json("capability_inventory.json", report.inventory)
        self._store.write_json("discovery_report.json", report)
        self._inventory = report.inventory
        self._state["project"] = {
            "root": report.inventory.root,
            "run_id": self._config.run_id,
            "profile": self._config.profile.value,
        }
        self._state["capabilities"] = report.inventory
        self._state["observations"] = [finding.model_dump() for finding in self._env_findings]
        return report

    def plan(self) -> list[TestPlanItem]:
        """Build and persist the test plan for the configured profile."""
        if self._inventory is None:
            self.discover()
        assert self._inventory is not None  # set by discover()
        plan = self._planner.plan(self._inventory, profile=self._config.profile)
        self._store.write_json("test_plan.json", [item.model_dump(mode="json") for item in plan])
        self._plan = plan
        self._state["test_plan"] = plan
        self._state["execution_queue"] = [item.test_id for item in plan]
        return plan

    def execute(self) -> list[Evidence]:
        """Run the plan, persist evidence + the execution summary."""
        if not self._plan:
            self.plan()
        evidence = self._executor.execute(
            self._plan,
            run_id=self._config.run_id,
            env_findings=self._env_findings,
        )
        self._store.write_json("execution_summary.json", self._reporter.execution_summary(evidence))
        self._evidence = evidence
        self._state["evidence"] = evidence
        self._state["validation_results"] = [item.model_dump(mode="json") for item in evidence]
        return evidence

    def _transcripts(self) -> dict[str, str]:
        """Read each executed test's real stdout/stderr back for fingerprinting.

        The text is read from the run's own immutable artifacts (bounded to
        ``_TRANSCRIPT_READ_CAP`` chars per file). A missing/unreadable artifact
        simply contributes nothing — the analyzer then falls back to an
        evidence-derived signature rather than fabricating a transcript.
        """
        transcripts: dict[str, str] = {}
        for record in self._evidence:
            parts: list[str] = []
            for rel in (record.stdout_artifact, record.stderr_artifact):
                if not rel:
                    continue
                try:
                    text = (self._store.run_dir / rel).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                parts.append(text[:_TRANSCRIPT_READ_CAP])
            if parts:
                transcripts[record.test_id] = "\n".join(parts)
        return transcripts

    def analyze(self) -> dict:
        """Aggregate the evidence into the Phase D deep failure analysis (§40/§68/§90).

        Appends the regression report: ``regression_report.json`` is written and
        ``state["regressions"]`` is filled from its findings. ``state["root_causes"]``
        deliberately stays ``[]`` — root-cause *candidates* live in
        ``failure_analysis.json`` only (the final verdict belongs to Phase E).
        """
        if not self._evidence:
            self.execute()
        analysis = self._classifier.analyze_deep(self._evidence, transcripts=self._transcripts())
        self._store.write_json("failure_analysis.json", analysis)
        self._state["failures"] = analysis.get("failed_tests", [])
        report = self._regression.report(
            run_id=self._config.run_id,
            changed_files=changed_files_from_env(),
            evidence=self._evidence,
            plan=self._plan,
        )
        self._store.write_json("regression_report.json", report.model_dump(mode="json"))
        self._state["regressions"] = report.findings
        self._analysis = analysis
        return analysis

    def report(self) -> Path:
        """Compute the verdict and write ``final_report.md`` (§42/§72)."""
        if not self._evidence:
            self.execute()
        assert self._inventory is not None  # execute() -> plan() -> discover()
        verdict = self._reporter.verdict(self._evidence)
        summary = self._reporter.execution_summary(self._evidence)
        analysis = self._analysis or self._classifier.analyze_deep(
            self._evidence, transcripts=self._transcripts()
        )
        regression = self._regression.report(
            run_id=self._config.run_id,
            changed_files=changed_files_from_env(),
            evidence=self._evidence,
            plan=self._plan,
        )
        path = self._reporter.write_final_report(
            inventory=self._inventory,
            plan=self._plan,
            summary=summary,
            analysis=analysis,
            verdict=verdict,
            regression=regression.model_dump(mode="json"),
        )
        self._state["final_summary"] = {
            "verdict": verdict.value,
            "report": str(path),
            "total": len(self._evidence),
        }
        logger.info("QA report written: %s (verdict=%s)", path, verdict.value)
        return path

    def run_stage(self, stage: Stage) -> Path | None:
        """Run one stage (or the whole ``full`` loop) and return its artifact."""
        if stage == "discover":
            self.discover()
            return self._store.run_dir
        if stage == "plan":
            self.plan()
            return self._store.run_dir
        if stage == "run":
            self.execute()
            return self._store.run_dir
        if stage == "analyze":
            self.analyze()
            return self._store.run_dir
        if stage == "report":
            return self.report()
        if stage == "full":
            self.discover()
            self.plan()
            self.execute()
            self.analyze()
            return self.report()
        raise ValueError(f"unknown stage: {stage!r}")


__all__ = ["QAOrchestrator", "Stage"]
