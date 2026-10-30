"""A code task that never calls code.execute must never be marked success.

Regression for B run 8c72292b at dd6289e: the run was marked
``completed / success`` with an empty codeplane, a ``textfile.edit``
artifact (``calc.edited.py``, not a repo modification) and a
``code.run`` AST-only check, while the final answer claimed a modified
file and passed pytest. The model bypassed the code plane entirely:
no delegate, no container, no diff, no pytest output.
"""

from types import SimpleNamespace

from forgeflow.runtime import orchestrator as orch


def _task(context):
    return SimpleNamespace(context=context)


def _ctx():
    return SimpleNamespace()


def test_requires_code_execution_covers_explicit_repo_and_handoff():
    assert orch._requires_code_execution(_task({"repo_path": "/tmp/repo"}), _ctx()) is True
    assert orch._requires_code_execution(_task({"handoff_from_run_id": "a-run"}), _ctx()) is True
    assert (
        orch._requires_code_execution(_task({"declared_tools": ["code.execute"]}), _ctx()) is True
    )
    assert orch._requires_code_execution(_task({}), _ctx()) is False
    assert orch._requires_code_execution(_task({"paths": ["/tmp/x.py"]}), _ctx()) is False


def test_gate_fires_only_when_code_execute_never_called():
    task = _task({"repo_path": "/tmp/repo"})
    bypass = [
        {"tool": "textfile.edit", "status": "ok", "payload": {"file": "calc.edited.py"}},
        {"tool": "code.run", "status": "ok", "payload": {"provider": "stdlib-ast"}},
    ]
    assert orch._code_execution_gate_errors(task, _ctx(), bypass) != []
    assert orch._has_code_execute(bypass) is False
    with_execute = bypass + [{"tool": "code.execute", "status": "error", "payload": {"ok": False}}]
    # A code.execute that ran and failed is already a failure through the
    # existing error verdicts; the gate is only for the bypass case.
    assert orch._code_execution_gate_errors(task, _ctx(), with_execute) == []
    assert orch._has_code_execute(with_execute) is True
    # A plain (non-code) task is never gated, even with no code.execute.
    assert orch._code_execution_gate_errors(_task({}), _ctx(), bypass) == []
