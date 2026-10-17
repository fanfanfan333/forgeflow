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
unless at least one test genuinely **passed**.

The liveness probing itself is deliberately **not** re-implemented here — it
lives once, in ``tests/realstack/conftest.py``, which this runner reuses by
simply enabling the gate env var.

Machine-readable status protocol
--------------------------------
Besides the human-facing ``[REALSTACK] …`` chatter, the runner emits **stable,
grep-able** stdout lines that a human or a script can gate on. The state word
vocabulary is ``PASS | GUARDED | ERROR | FAIL`` and the decision order is fixed:

1. ``REALSTACK_STATUS=FAIL``    — ``failed > 0 or errors > 0`` (highest priority;
   a genuine failure is *never* downgraded to ``GUARDED``). Exit = pytest's own.
2. ``REALSTACK_STATUS=ERROR``   — the run itself broke: **nothing collected**
   (``collected == 0`` or pytest exit ``5`` ⇒ ``REALSTACK_ERROR_REASON=
   NO_TESTS_COLLECTED``) or a **collection error** (pytest exit ``2`` / ``3`` /
   ``4`` ⇒ ``REALSTACK_ERROR_REASON=COLLECTION_ERROR``). This is a *test-harness*
   fault, **not** an environment guard — zero collection is never reported as
   ``GUARDED``. Exit ``4``.
3. ``REALSTACK_STATUS=PASS``    — ``passed > 0 and failed == 0 and errors == 0``.
   Exit = pytest's own.
4. ``REALSTACK_STATUS=GUARDED`` — otherwise: nothing ran because the environment
   precondition was not met, so **no real acceptance happened**. Exit ``3``.

Extra lines:
* ``REALSTACK_SKIPPED=<n>``     — only when ``PASS`` yet some cases were skipped
  (partial coverage made explicit).
* ``REALSTACK_GUARD_REASON=<code>`` — only when ``GUARDED``, from the fixed
  vocabulary ``GATE_DISABLED`` / ``OLLAMA_UNAVAILABLE`` / ``POSTGRES_UNAVAILABLE``
  / ``UNKNOWN_GUARD``.
* ``REALSTACK_ERROR_REASON=<code>`` — only when ``ERROR`` (see above).

Make vs. exit codes
-------------------
GNU Make folds **any** non-zero recipe exit code into ``2`` (``make: *** Error
2``), so the process exit code is *not* a trustworthy machine signal once
``make`` is in the loop. The authoritative machine interface is therefore the
stdout ``REALSTACK_STATUS=…`` lines above — **not** the exit code. The return
codes are kept for direct (non-Make) callers only.

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

#: Status vocabulary emitted on ``REALSTACK_STATUS=``.
_STATUS_FAIL = "FAIL"
_STATUS_ERROR = "ERROR"
_STATUS_GUARDED = "GUARDED"
_STATUS_PASS = "PASS"

#: Error-reason vocabulary emitted on ``REALSTACK_ERROR_REASON=``.
_ERROR_NO_TESTS_COLLECTED = "NO_TESTS_COLLECTED"
_ERROR_COLLECTION_ERROR = "COLLECTION_ERROR"

#: Guard-reason vocabulary emitted on ``REALSTACK_GUARD_REASON=``.
_GUARD_GATE_DISABLED = "GATE_DISABLED"
_GUARD_OLLAMA_UNAVAILABLE = "OLLAMA_UNAVAILABLE"
_GUARD_POSTGRES_UNAVAILABLE = "POSTGRES_UNAVAILABLE"
_GUARD_UNKNOWN = "UNKNOWN_GUARD"

#: pytest's own exit codes that mean "the run itself broke", not "tests failed".
_PYTEST_COLLECTION_EXIT_CODES = (2, 3, 4)
_PYTEST_NO_TESTS_EXIT_CODE = 5

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
        self.collected = 0
        self.skip_reasons: list[str] = []

    def pytest_collection_finish(self, session: Any) -> None:
        """Record how many items were actually collected (for the ERROR state)."""
        self.collected = len(session.items)

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


def _classify_status(tally: _ResultTally, exit_code: int) -> str:
    """Map the tally + pytest exit code onto the fixed status vocabulary.

    The priority order is deliberate and non-negotiable (INC51 T04):

    1. a genuine failure (``FAIL``) outranks everything — a run that both failed
       and skipped can never be laundered into ``GUARDED``;
    2. a broken run — **nothing collected** (``collected == 0`` or pytest exit
       ``5``) or a **collection error** (pytest exit ``2`` / ``3`` / ``4``) — is
       ``ERROR``: that is a *test-harness* fault, **not** an environment guard
       (zero collection must never be reported as ``GUARDED``);
    3. at least one genuine pass ⇒ ``PASS``;
    4. otherwise (everything skipped) ⇒ ``GUARDED`` (environment unavailable).
    """
    if tally.failed > 0 or tally.errors > 0:
        return _STATUS_FAIL
    if tally.collected == 0 or exit_code == _PYTEST_NO_TESTS_EXIT_CODE:
        return _STATUS_ERROR
    if exit_code in _PYTEST_COLLECTION_EXIT_CODES:
        return _STATUS_ERROR
    if tally.passed > 0:
        return _STATUS_PASS
    return _STATUS_GUARDED


def _classify_error_reason(tally: _ResultTally, exit_code: int) -> str:
    """Which kind of ``ERROR`` this is (collection error vs. nothing collected)."""
    if tally.collected == 0 or exit_code == _PYTEST_NO_TESTS_EXIT_CODE:
        return _ERROR_NO_TESTS_COLLECTED
    return _ERROR_COLLECTION_ERROR


def _classify_guard_reason(skip_reasons: list[str]) -> str:
    """Bucket collected skip reasons into the fixed guard-reason vocabulary.

    Matching is substring / case-insensitive and order-preserving: the first
    category that matches wins, so a reason naming the most specific gate is
    attributed to it. An empty list — or nothing recognisable — is reported as
    ``UNKNOWN_GUARD`` rather than silently passed off as a pass.
    """
    text = " ".join(skip_reasons).casefold()
    if GATE_ENV_VAR.casefold() in text or "未启用" in text:
        return _GUARD_GATE_DISABLED
    if "ollama" in text:
        return _GUARD_OLLAMA_UNAVAILABLE
    if "postgresql" in text:
        return _GUARD_POSTGRES_UNAVAILABLE
    return _GUARD_UNKNOWN


def main(argv: list[str] | None = None) -> int:
    """Enable the real-stack gate, run the suite, and enforce the honesty guard.

    Args:
        argv: Extra pytest arguments; defaults to ``sys.argv[1:]`` when ``None``.

    Returns:
        pytest's own exit code on a genuine run (``PASS`` / ``FAIL``); ``2`` when
        pytest is not importable; ``3`` when nothing genuinely ran (``GUARDED`` —
        the environment was unavailable); ``4`` when the run itself broke
        (``ERROR`` — a collection error or nothing collected). Note that ``make``
        folds every non-zero code to ``2``, so callers that go through Make must
        read the ``REALSTACK_STATUS=`` stdout line instead of the exit code.
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
        f"skipped={tally.skipped} errors={tally.errors} collected={tally.collected}"
    )

    # --- Machine-readable status protocol (grep-able; see module docstring) ---
    status = _classify_status(tally, exit_code)
    print(f"REALSTACK_STATUS={status}")

    # Ordering is fixed (see _classify_status): FAIL > ERROR > PASS > GUARDED.
    if status == _STATUS_FAIL:
        return exit_code

    if status == _STATUS_ERROR:
        print(f"REALSTACK_ERROR_REASON={_classify_error_reason(tally, exit_code)}")
        print(
            "[REALSTACK] ERROR — the run itself broke (nothing collected or a "
            "collection error); this is a test-harness fault, NOT an environment "
            "guard, and it is NOT a pass."
        )
        return 4

    if status == _STATUS_PASS:
        if tally.skipped > 0:
            print(f"REALSTACK_SKIPPED={tally.skipped}")
        return exit_code

    # Remaining: GUARDED — nothing genuinely ran (all skipped).
    print("[REALSTACK] NOT RUN / ENVIRONMENT UNAVAILABLE")
    print("[REALSTACK] no test actually ran — every case was skipped, so this is NOT a pass.")
    for reason in tally.skip_reasons:
        print(f"[REALSTACK]   - {reason}")
    if not tally.skip_reasons:
        print("[REALSTACK]   - no skip reasons were collected")
    print(f"REALSTACK_GUARD_REASON={_classify_guard_reason(tally.skip_reasons)}")
    return 3


if __name__ == "__main__":
    sys.exit(main())
