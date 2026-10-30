"""Code-plane gate must require a *successful* code.execute, not just an invocation.

Regression for clean B 0112529d (3eff1b1): code.execute was invoked
and timed out (tests unmeasured), yet the run was marked
completed/success, because the old gate only checked "invoked at all"
and a tool failure alone does not enter the run's errors list.
"""

from types import SimpleNamespace

from forgeflow.runtime import orchestrator as orch


def _task():
    return SimpleNamespace(context={"repo_path": "/tmp/repo"})


def test_gate_fires_when_exec_failed():
    inv = [
        {
            "tool": "code.execute",
            "payload": {"ok": False, "codeplane": {"diff": "", "tests": {"measured": False}}},
        }
    ]
    errors = orch._code_execution_gate_errors(_task(), None, inv)
    assert errors


def test_gate_passes_when_exec_succeeded():
    inv = [
        {
            "tool": "code.execute",
            "payload": {
                "ok": True,
                "codeplane": {"diff": "diff", "tests": {"measured": True, "verdict": "passed"}},
            },
        }
    ]
    assert orch._code_execution_gate_errors(_task(), None, inv) == []


def test_gate_fires_when_exec_never_called():
    assert orch._code_execution_gate_errors(_task(), None, [])
