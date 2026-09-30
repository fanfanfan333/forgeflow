"""Test-result verification — the pass/fail verdict lives in ForgeFlow (INC25 W2).

Q4 decision: the OpenHands runner **reports raw output** (stdout + exit code +
optional structured case results); ForgeFlow **reviews** it here and owns the
verdict. If the output cannot be parsed, the result is *unmeasured* — it is
**never** defaulted to "passed" (AC-16).

The parser is deliberately conservative:

  * counts come from a real pytest summary token (``12 passed, 2 failed, 1 error``);
  * a non-zero exit code is never reported as a pass, even if a summary token is
    missing;
  * "no tests ran" (no count tokens) is *unmeasured*, not a pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["TestResult", "evaluate_test_output"]

_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|warnings?)\b"
)
_CASE_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)", re.MULTILINE)

#: Map a raw pytest token to our count bucket.
_BUCKETS: dict[str, str] = {
    "passed": "passed",
    "failed": "failed",
    "error": "errors",
    "errors": "errors",
}


@dataclass
class TestResult:
    """The reviewed test outcome for one code task."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    failed_cases: list[str] = field(default_factory=list)
    command: str = ""
    verdict: str = "unmeasured"   # passed | failed | unmeasured
    measured: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "failed_cases": list(self.failed_cases),
            "command": self.command,
            "verdict": self.verdict,
            "measured": self.measured,
        }


def _counts_from_stdout(stdout: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "errors": 0}
    for match in _COUNT_RE.finditer(stdout or ""):
        bucket = _BUCKETS.get(match.group(2))
        if bucket:
            counts[bucket] += int(match.group(1))
    return counts


def _cases_from_stdout(stdout: str) -> list[str]:
    seen: list[str] = []
    for _kind, name in _CASE_RE.findall(stdout or ""):
        if name not in seen:
            seen.append(name)
    return seen


def evaluate_test_output(
    stdout: str,
    exit_code: int | None,
    cases: list[dict[str, Any]] | None = None,
    *,
    command: str = "",
) -> TestResult:
    """Review raw test output and return a :class:`TestResult`.

    Args:
        stdout: the raw test stdout.
        exit_code: the test command's exit code (``None`` when it never ran).
        cases: optional structured per-case results (``{"name", "outcome"}``); when
            present they are authoritative over the stdout text.
        command: the executed test command (echoed verbatim).

    Returns:
        A ``TestResult``. ``measured`` is ``False`` (and ``verdict`` is
        ``"unmeasured"``) whenever no real count could be established — the
        harness must then treat the result as NOT passed.
    """
    if cases:
        passed = failed = errors = 0
        failed_cases: list[str] = []
        for case in cases:
            outcome = str(case.get("outcome") or case.get("status") or "").strip().lower()
            name = str(case.get("name") or case.get("id") or "").strip()
            if outcome in ("passed", "pass", "ok", "success"):
                passed += 1
            elif outcome in ("failed", "fail", "failure"):
                failed += 1
                if name:
                    failed_cases.append(name)
            elif outcome in ("error", "errored", "exception"):
                errors += 1
                if name:
                    failed_cases.append(name)
        verdict = "failed" if (failed or errors) else "passed"
        return TestResult(
            passed=passed, failed=failed, errors=errors,
            failed_cases=failed_cases, command=command, verdict=verdict, measured=True,
        )

    counts = _counts_from_stdout(stdout)
    matched = counts["passed"] + counts["failed"] + counts["errors"] > 0
    if not matched:
        # Nothing parseable ⇒ unmeasured. NOT a pass (AC-16).
        return TestResult(command=command, verdict="unmeasured", measured=False)

    failed_cases = _cases_from_stdout(stdout)
    failed_total = counts["failed"] + counts["errors"]
    if failed_total > 0:
        verdict = "failed"
    elif exit_code not in (0, None):
        # A non-zero exit is never a pass, even when only "passed N" was seen.
        verdict = "failed"
    else:
        verdict = "passed"
    return TestResult(
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["errors"],
        failed_cases=failed_cases,
        command=command,
        verdict=verdict,
        measured=True,
    )
