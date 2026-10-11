"""Regression scope + baseline comparison (Phase D, §20/§69).

Layer L3. Given the files a change touched (or, failing that, the failing
evidence), this module computes the *affected surface* (§20) and compares the
run's QA metrics against a stored baseline (§69). Two reuse points are wired in
by **aliased import** so this module never re-defines a referenced symbol — it
consumes ``check_dict`` / ``wilson_lower_bound`` rather than re-implementing them:

* :func:`forgeflow.evaluation.regression.check_dict` — the tolerance policy
  (``ok`` / ``warning`` / ``regression``); an *absent* baseline is never a
  regression (the reused policy's own "first run" semantics);
* :func:`forgeflow.rollout.metrics.wilson_lower_bound` — the pass-rate lower
  bound, so a small sample can never look like a real drop.

The default path is offline and deterministic: no LLM, no network, no DB, and
**no ``git`` invocation** — the change signal comes from ``QA_CHANGED_FILES``
(explicit; the entry Phase E's Change Set will reuse) or is inferred from the
failing evidence. Scope is a *narrowing heuristic*; the honest boundary is always
recorded in ``coverage_note``.

Name hygiene (see the Phase D design §1.5 G3): this module **must not** define
``check`` / ``check_dict`` / ``write_baseline`` / ``TOLERANCES`` /
``RegressionFinding`` — those names belong to the reused module. Its own
pydantic models (``RegressionScope`` / ``RegressionReport``) live in
:mod:`forgeflow.qa.models`.
"""

from __future__ import annotations

import ast
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forgeflow.evaluation import regression as eval_regression
from forgeflow.qa.models import (
    CapabilityInventory,
    Evidence,
    EvidenceStatus,
    FailureClass,
    RegressionReport,
    RegressionScope,
    TestPlanItem,
)
from forgeflow.qa.task_runner import KNOWN_TARGETS, parse_makefile_targets
from forgeflow.rollout.metrics import wilson_lower_bound

logger = logging.getLogger(__name__)

#: QA metric tolerances (§69): ``metric → (direction, warn_tol, fail_tol)``.
#: The keys match what :meth:`RegressionAnalyzer.metrics` emits.
QA_METRIC_TOLERANCES: dict[str, tuple[str, float, float]] = {
    "pass_rate": ("higher_is_better", 0.02, 0.05),
    "failure_count": ("lower_is_better", 1.0, 3.0),
    "environment_failure_count": ("lower_is_better", 1.0, 3.0),
}

#: A change to one of these paths touches a *global* surface, so the honest
#: recommendation is the whole-repo gate — not a pretended narrow scope (§20).
_GLOBAL_SURFACE_FILES: frozenset[str] = frozenset(
    {"pyproject.toml", "Makefile", "forgeflow/config.py"}
)
_GLOBAL_SURFACE_PREFIXES: tuple[str, ...] = ("forgeflow/qa/",)

#: Upper bound on how many files the bounded reverse-dependency scan will read.
_REVERSE_SCAN_LIMIT = 4000

#: Environment variable carrying the explicit change signal (comma / newline list).
_CHANGED_FILES_ENV = "QA_CHANGED_FILES"

#: Environment variable carrying the previous run's metric JSON (baseline, §69).
_BASELINE_ENV = "QA_REGRESSION_BASELINE"

#: Path prefixes that mark a *route* / *skill* / *tool* / *policy* surface.
_ROUTERS_PREFIX = "forgeflow/api/routers/"
_SKILLS_PREFIX = "forgeflow/skills/"
_RUNTIME_PREFIX = "forgeflow/runtime/"
_GOVERNANCE_PREFIX = "forgeflow/governance/"
_POLICIES_FILE = "forgeflow/rbac/policies.py"


def _posix(value: str) -> str:
    """Normalise one path to a repo-relative POSIX string (best effort)."""
    item = str(value).strip().replace("\\", "/")
    while item.startswith("./"):
        item = item[2:]
    return item


def changed_files_from_env() -> list[str]:
    """Read ``QA_CHANGED_FILES`` (comma / newline separated) into a sorted list.

    Returns the repo-relative POSIX paths, de-duplicated. Unset or empty ⇒ ``[]``
    (an honest "no explicit change signal"); the caller then falls back to the
    failing evidence rather than inventing a change.
    """
    raw = os.environ.get(_CHANGED_FILES_ENV, "")
    files = [_posix(chunk) for chunk in raw.replace("\r", "\n").replace(",", "\n").split("\n")]
    return sorted({item for item in files if item})


class RegressionAnalyzer:
    """Computes the affected surface (§20) and the baseline comparison (§69).

    The default path is offline, deterministic and ``git``-free. ``baseline_path``
    defaults to the ``QA_REGRESSION_BASELINE`` env var; when neither is set,
    ``baseline_available`` is honestly ``False`` and **no** regression finding is
    produced (a first run is not a regression).
    """

    def __init__(
        self,
        root: Path,
        *,
        inventory: CapabilityInventory | None = None,
        baseline_path: Path | None = None,
    ) -> None:
        self._root = Path(root)
        self._inventory = inventory
        if baseline_path is None:
            env_path = os.environ.get(_BASELINE_ENV, "").strip()
            baseline_path = Path(env_path) if env_path else None
        self._baseline_path: Path | None = baseline_path
        self._make_targets: set[str] = self._read_make_targets()

    # ── Makefile targets (real ones only) ────────────────────────────────
    def _read_make_targets(self) -> set[str]:
        """The real, whitelisted Make targets this repo defines (may be empty)."""
        makefile = self._root / "Makefile"
        if not makefile.is_file():
            return set()
        parsed = parse_makefile_targets(makefile.read_text(encoding="utf-8", errors="replace"))
        return {target for target in parsed if target in KNOWN_TARGETS}

    def has_target(self, target: str) -> bool:
        """True when ``target`` is a real Make target in this repo."""
        return target in self._make_targets

    # ── §20 affected surface ─────────────────────────────────────────────
    @staticmethod
    def _normalise(paths: list[str]) -> list[str]:
        return sorted({_posix(path) for path in paths if _posix(path)})

    @staticmethod
    def _is_global_surface(path: str) -> bool:
        """True when a change to ``path`` touches a whole-repo surface (§20)."""
        if path in _GLOBAL_SURFACE_FILES:
            return True
        return any(path.startswith(prefix) for prefix in _GLOBAL_SURFACE_PREFIXES)

    def _classify_path(
        self,
        path: str,
        modules: set[str],
        packages: set[str],
        routes: set[str],
        skills: set[str],
        tools: set[str],
        policies: set[str],
        tests: set[str],
    ) -> None:
        """Fold one changed path into the affected-surface buckets."""
        parts = path.split("/")
        if len(parts) >= 2:
            modules.add("/".join(parts[:-1]))
            packages.add(parts[0])
        if path.startswith(_ROUTERS_PREFIX) and path.endswith(".py"):
            stem = parts[-1][:-3]
            if stem != "__init__":
                routes.add(stem)
        if path.startswith(_SKILLS_PREFIX) and len(parts) >= 3:
            skills.add(parts[2])
        if path.startswith(_RUNTIME_PREFIX):
            tools.add(parts[-1][:-3] if path.endswith(".py") else parts[-1])
        if path == _POLICIES_FILE:
            policies.add("rbac")
        elif path.startswith(_GOVERNANCE_PREFIX):
            policies.add("governance")
        if path.startswith("tests/") and parts[-1].startswith("test_"):
            tests.add(path)

    def _reverse_imports(self, module_paths: list[str]) -> list[str]:
        """Test files that import any of ``module_paths`` (bounded, AST-based).

        A deliberately *bounded* reverse-dependency scan: it reads at most
        ``_REVERSE_SCAN_LIMIT`` files under ``tests/`` and matches a module by its
        dotted prefix. It does not solve the whole import graph (see §9 item 1) —
        it exists only to widen the candidate test set for a changed module.
        """
        wanted = {path.replace("/", ".").strip(".") for path in module_paths}
        wanted = {name for name in wanted if name}
        tests_dir = self._root / "tests"
        if not wanted or not tests_dir.is_dir():
            return []
        hits: set[str] = set()
        for index, path in enumerate(sorted(tests_dir.rglob("*.py"))):
            if index >= _REVERSE_SCAN_LIMIT:
                logger.warning("reverse-import scan capped at %d files", _REVERSE_SCAN_LIMIT)
                break
            if self._imports_any(path, wanted):
                hits.add(self._to_rel(path))
        return sorted(hits)

    @staticmethod
    def _imports_any(path: Path, wanted: set[str]) -> bool:
        """True when ``path`` imports any module in ``wanted`` (dotted prefix)."""
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # relative import — resolved inside the package
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                if any(name == want or name.startswith(want + ".") for want in wanted):
                    return True
        return False

    def _to_rel(self, path: Path) -> str:
        try:
            return path.relative_to(self._root).as_posix()
        except ValueError:
            return path.as_posix()

    def _recommend_targets(
        self,
        *,
        full_repo: bool,
        packages: set[str],
        tests: set[str],
        routes: set[str],
        skills: set[str],
        tools: set[str],
        policies: set[str],
    ) -> list[str]:
        """Map the affected surface onto **real** Make targets (§20)."""
        candidates: set[str] = set()
        if full_repo:
            candidates.add("check")
        else:
            if "forgeflow" in packages:
                candidates.update({"lint", "typecheck"})
            if "tests" in packages or tests:
                candidates.add("test-unit")
            if routes or skills or tools or policies:
                candidates.add("test-integration")
        return sorted(target for target in candidates if self.has_target(target))

    def scope(
        self,
        *,
        changed_files: list[str] | None = None,
        failing_evidence: list[Evidence] | None = None,
        plan: list[TestPlanItem] | None = None,
    ) -> RegressionScope:
        """Compute the affected surface (§20) — offline and ``git``-free."""
        changed = self._normalise(changed_files or [])
        failing = [
            record
            for record in (failing_evidence or [])
            if record.status in (EvidenceStatus.FAIL, EvidenceStatus.BLOCKED)
        ]
        planned = plan or []

        if not changed and not failing:
            return RegressionScope(
                coverage_note="no change signal available",
                confidence=0.0,
            )

        rationale: list[str] = []
        modules: set[str] = set()
        packages: set[str] = set()
        routes: set[str] = set()
        skills: set[str] = set()
        tools: set[str] = set()
        policies: set[str] = set()
        tests: set[str] = set()
        full_repo = False

        if changed:
            rationale.append(f"explicit change signal: {len(changed)} file(s)")
            for path in changed:
                self._classify_path(path, modules, packages, routes, skills, tools, policies, tests)
                if self._is_global_surface(path):
                    full_repo = True
                    rationale.append(f"global surface touched: {path}")
        else:
            rationale.append("no explicit change signal; scope inferred from failing evidence")
            tests.update(record.test_id for record in failing)

        reverse = self._reverse_imports(sorted(modules))
        if reverse:
            rationale.append(f"reverse-dependency scan matched {len(reverse)} test file(s)")
            tests.update(reverse)

        if full_repo and planned:
            tests.update(item.test_id for item in planned)
            rationale.append("planned targets included (whole-repo fallback)")

        recommended = self._recommend_targets(
            full_repo=full_repo,
            packages=packages,
            tests=tests,
            routes=routes,
            skills=skills,
            tools=tools,
            policies=policies,
        )
        if full_repo:
            rationale.append("recommending the whole-repo gate (check)")

        confidence = 0.3
        if changed:
            confidence = 0.5 if full_repo else 0.7

        coverage_note = (
            "scope derived from explicit changed files and a bounded reverse-import scan; "
            "a narrowing heuristic, not a proof of sufficiency"
            if changed
            else "scope inferred from failing evidence only (no explicit change signal); "
            "affected modules could not be determined"
        )
        return RegressionScope(
            changed_files=changed,
            affected_modules=sorted(modules),
            affected_packages=sorted(packages),
            affected_routes=sorted(routes),
            affected_skills=sorted(skills),
            affected_tools=sorted(tools),
            affected_policies=sorted(policies),
            affected_tests=sorted(tests),
            recommended_targets=recommended,
            full_repo_fallback=full_repo,
            rationale=rationale,
            confidence=confidence,
            coverage_note=coverage_note,
        )

    # ── §69 metrics + baseline comparison ────────────────────────────────
    def metrics(self, evidence: list[Evidence]) -> dict[str, Any]:
        """This run's QA metrics (``pass_rate`` is ``None`` when unmeasured)."""
        by_status: dict[str, int] = {status.value: 0 for status in EvidenceStatus}
        passes = 0
        failures = 0
        environment_failures = 0
        for record in evidence:
            by_status[record.status.value] = by_status.get(record.status.value, 0) + 1
            if record.status is EvidenceStatus.PASS:
                passes += 1
            if record.status in (EvidenceStatus.FAIL, EvidenceStatus.BLOCKED):
                failures += 1
            if record.failure_class is FailureClass.ENVIRONMENT_FAILURE:
                environment_failures += 1
        total = len(evidence)
        return {
            "pass_rate": (passes / total) if total else None,
            "failure_count": failures,
            "environment_failure_count": environment_failures,
            "total": total,
            "by_status": by_status,
        }

    def compare(self, metrics: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any]:
        """Compare ``metrics`` to ``baseline`` via the reused tolerance policy.

        Non-numeric / unmeasured values (e.g. ``pass_rate`` when ``total == 0``)
        are dropped before comparison, so a ``None`` metric never reaches the
        reused ``check_dict``. An absent baseline yields ``{"passed": True,
        "findings": []}`` — the reused policy's "first run is not a regression"
        semantics, preserved verbatim.
        """
        comparable = {
            key: value for key, value in metrics.items() if isinstance(value, int | float)
        }
        report = eval_regression.check_dict(comparable, baseline, tolerances=QA_METRIC_TOLERANCES)
        return report.to_dict()

    def pass_rate_lower_bound(self, evidence: list[Evidence]) -> float | None:
        """The Wilson lower bound on the pass rate, or ``None`` when unmeasured."""
        total = len(evidence)
        if total <= 0:
            return None
        successes = sum(1 for record in evidence if record.status is EvidenceStatus.PASS)
        return wilson_lower_bound(successes, total)

    def _load_baseline(self) -> dict[str, Any] | None:
        """Load the baseline metric dict, or ``None`` (missing/unreadable = first run)."""
        path = self._baseline_path
        if path is None:
            return None
        try:
            if not path.is_file():
                logger.warning("regression baseline not found: %s", path)
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read regression baseline %s: %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    def report(
        self,
        *,
        run_id: str,
        changed_files: list[str] | None = None,
        evidence: list[Evidence] | None = None,
        plan: list[TestPlanItem] | None = None,
    ) -> RegressionReport:
        """Build the persisted regression report (§42: scope + comparison + bound)."""
        records = list(evidence or [])
        planned = list(plan or [])
        if changed_files is None:
            changed_files = changed_files_from_env()

        scope = self.scope(
            changed_files=changed_files,
            failing_evidence=[
                record
                for record in records
                if record.status in (EvidenceStatus.FAIL, EvidenceStatus.BLOCKED)
            ],
            plan=planned,
        )
        metrics = self.metrics(records)
        baseline = self._load_baseline()
        comparison = self.compare(metrics, baseline)
        findings = list(comparison.get("findings", []))

        return RegressionReport(
            run_id=run_id,
            generated_at=datetime.now(UTC).isoformat(),
            scope=scope,
            baseline_available=baseline is not None,
            baseline_path=str(self._baseline_path) if self._baseline_path is not None else None,
            comparison=comparison,
            metrics=metrics,
            pass_rate_lower_bound=self.pass_rate_lower_bound(records),
            environment_attributed=sum(
                1 for record in records if record.failure_class is FailureClass.ENVIRONMENT_FAILURE
            ),
            source_candidates=sum(
                1 for record in records if record.failure_class is FailureClass.SOURCE_BUG
            ),
            findings=findings,
            coverage_note=scope.coverage_note,
        )


__all__ = ["QA_METRIC_TOLERANCES", "RegressionAnalyzer", "changed_files_from_env"]
