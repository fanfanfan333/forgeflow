"""Code execution must not re-run on a retry, and a later degraded
execution must not erase an earlier success from the final record.

Regression for the B run at 8857e4b (8a34c87d): code.execute ran
successfully twice, a third call degraded, the orchestrator hit the
24-model-call ceiling, and the final run detail kept the degraded,
empty codeplane payload instead of the successful diff/tests.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime.tool_handlers import code_execute


def _inv(tool: str, *, status: str, payload: dict) -> dict:
    return {"tool": tool, "status": status, "payload": payload}


def _cp(*, outcome: str, diff: str = "", tests: dict | None = None) -> dict:
    return {
        "outcome": outcome,
        "diff": diff,
        "tests": tests or {},
        "engine": {"available": True},
        "workspace": {"workspace_id": "ws-1"},
        "timeline": [],
    }


def test_assemble_prefers_succeeded_over_later_degraded():
    records = [
        _inv(
            "code.execute",
            status="ok",
            payload={
                "ok": True,
                "codeplane": _cp(
                    outcome="succeeded", diff="diff --git a/calc.py", tests={"verdict": "passed"}
                ),
            },
        ),
        _inv(
            "code.execute",
            status="ok",
            payload={
                "ok": True,
                "codeplane": _cp(outcome="succeeded", diff="diff --git a/calc.py"),
            },
        ),
        _inv(
            "code.execute",
            status="error",
            payload={"ok": False, "codeplane": _cp(outcome="degraded")},
        ),
    ]
    merged = orch._assemble_codeplane(records, [])
    assert merged["diff"] == "diff --git a/calc.py"
    assert merged["tests"] == {"verdict": "passed"}
    assert merged["outcome"] == "succeeded"


def test_assemble_without_success_keeps_last_honest_result():
    records = [
        _inv(
            "code.execute",
            status="error",
            payload={"ok": False, "codeplane": _cp(outcome="degraded")},
        ),
    ]
    merged = orch._assemble_codeplane(records, [])
    assert merged["outcome"] == "degraded"
    assert merged["diff"] == ""


def test_second_code_execute_reuses_prior_success_without_engine():
    prior = {
        "ok": True,
        "outcome": "succeeded",
        "codeplane": _cp(outcome="succeeded", diff="diff --git a/calc.py"),
    }
    task = SimpleNamespace(
        context={"tool_invocations": [_inv("code.execute", status="ok", payload=prior)]}
    )
    ctx = SimpleNamespace(run_id="r1", intent="实现 add", tenant_id="t1", task_context=task)
    result = asyncio.run(code_execute({}, ctx))
    assert result["reused"] is True
    assert result["ok"] is True
    assert result["codeplane"]["diff"] == "diff --git a/calc.py"


def test_force_rerun_bypasses_reuse(monkeypatch):
    # With force_rerun, the guard must not short-circuit: the call then
    # reaches the Docker gate for a handoff task and is refused, rather
    # than returning the prior success.
    prior = {
        "ok": True,
        "outcome": "succeeded",
        "codeplane": _cp(outcome="succeeded", diff="diff --git a/calc.py"),
    }
    task = SimpleNamespace(
        context={"tool_invocations": [_inv("code.execute", status="ok", payload=prior)]}
    )
    ctx = SimpleNamespace(run_id="r1", intent="实现 add", tenant_id="t1", task_context=task)

    class _FakeEngine:
        pass

    monkeypatch.setattr("forgeflow.codeplane.engine.get_code_engine", lambda: _FakeEngine())
    result = asyncio.run(
        code_execute(
            {
                "force_rerun": True,
                "handoff_results": [{"kind": "document", "source": "s", "content": "x"}],
            },
            ctx,
        )
    )
    assert result.get("reused") is not True
    assert result["ok"] is False
    assert result["unavailable"] is True
