"""Cross-platform real-stack runner — ``scripts/run_realstack.py``.

``make realstack`` must exercise the **real** PostgreSQL + Ollama integration
suite under ``tests/realstack/`` on Windows *and* on Linux CI. Setting an
environment variable only for one command is a POSIX shell idiom
(``VAR=1 cmd``) that ``cmd.exe`` does not understand, so the job is done here in
Python instead of in the Makefile — the Makefile stays a thin, portable
orchestrator.

Why ``pytest.main`` and not a subprocess?
-----------------------------------------
Running in-process lets us register a reporting plugin that tallies the genuine
outcomes. That matters for the honesty guard below: the real-stack suite is
double-gated (``FORGEFLOW_REAL_STACK=1`` plus an Ollama / PostgreSQL liveness
probe in ``tests/realstack/conftest.py``). If PostgreSQL or Ollama is not
reachable, every case *skips* — and a suite that skips everything exits ``0``.
Treating that as success would be a **false green**: the exact failure mode the
real-stack suite exists to prevent. So this runner refuses to report success
unless at least one test genuinely **passed**; otherwise it prints
``NOT RUN / ENVIRONMENT UNAVAILABLE`` with the real skip reasons and returns
non-zero.

The liveness probing itself is deliberately **not** re-implemented here — it
lives once, in ``tests/realstack/conftest.py``, which this runner reuses by
simply enabling the gate env var.

Usage::

    python scripts/run_realstack.py                 # run tests/realstack
    python scripts/run_realstack.py -k cost         # extra args pass through to pytest
    python scripts/run_realstack.py --maxfail=1
"""

from __future__ import annotations

import os
import sys
from typing import Any

#: The gate variable read by tests/realstack/conftest.py::_gate_enabled.
GATE_ENV_VAR = "FORGEFLOW_REAL_STACK"

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


class _ResultTally:
    """Counts genuine pytest outcomes via the ``pytest_runtest_logreport`` hook.

    The distinction that matters is *passed* (a test really executed and
    succeeded) versus *skipped* (the gate refused to run it). ``errors`` folds
    in setup/teardown failures so they are never mistaken for passes.
    """

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors = 0
        self.skip_reasons: list[str] = []

    def pytest_runtest_logreport(self, report: Any) -> None:
        """Classify one phase of one test as it completes."""
        when = report.when
        if when == "setup":
            if report.skipped:
                self.skipped += 1
                self.skip_reasons.append(_short_reason(report))
            elif report.failed:
                self.errors += 1
        elif when == "call":
            if report.passed:
                self.passed += 1
            elif report.failed:
                self.failed += 1
        elif when == "teardown" and report.failed:
            self.errors += 1


def _short_reason(report: Any) -> str:
    """Condense a skip report's ``longrepr`` into a single printable line."""
    text = str(getattr(report, "longrepr", "")).strip().replace("\n", " ")
    return text[:200]


def main(argv: list[str] | None = None) -> int:
    """Enable the real-stack gate, run the suite, and enforce the honesty guard.

    Args:
        argv: Extra pytest arguments; defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        pytest's own exit code on a genuine run; ``2`` when pytest is not
        importable; ``3`` when pytest "succeeded" only because everything was
        skipped (environment unavailable).
    """
    extra = list(sys.argv[1:] if argv is None else argv)

    # Enable the real-stack gate *before* pytest imports the conftest, otherwise
    # the conftest's probe short-circuits to "skip".
    os.environ[GATE_ENV_VAR] = "1"

    # Run from the repository root so pytest resolves tests/realstack and the
    # project's pyproject.toml config correctly, and make `forgeflow` importable
    # even when it is not pip-installed into the environment.
    os.chdir(_ROOT)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    try:
        import pytest
    except ImportError as exc:  # pragma: no cover - environment guard
        print(f"[REALSTACK] NOT RUN — pytest is not importable: {exc}")
        print("[REALSTACK] install the dev toolchain with: python -m pip install -e '.[dev]'")
        return 2

    tally = _ResultTally()
    print(
        f"[REALSTACK] {GATE_ENV_VAR}=1 — running tests/realstack against a real PostgreSQL + Ollama"
    )
    exit_code = int(pytest.main(["tests/realstack", "-ra", *extra], plugins=[tally]))

    print()
    print(
        f"[REALSTACK] passed={tally.passed} failed={tally.failed} "
        f"skipped={tally.skipped} errors={tally.errors}"
    )

    if exit_code == 0 and tally.passed == 0:
        print("[REALSTACK] NOT RUN / ENVIRONMENT UNAVAILABLE")
        print(
            "[REALSTACK] pytest exited 0, but no test actually ran — every case "
            "was skipped, so this is NOT a pass."
        )
        for reason in tally.skip_reasons:
            print(f"[REALSTACK]   - {reason}")
        if not tally.skip_reasons:
            print("[REALSTACK]   - no tests were collected from tests/realstack")
        return 3

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
