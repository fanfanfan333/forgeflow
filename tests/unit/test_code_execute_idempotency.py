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


def _stub_engine(monkeypatch, jobs):
    # A fake engine that records each job's intent and returns a
    # successful diff, without Docker or a real workspace.
    from forgeflow.codeplane.engine import CodeRunResult
    from forgeflow.codeplane.tests_verdict import TestResult

    class _FakeEngine:
        def available(self):
            return True

        interpreter = ""

        async def run(self, job):
            jobs.append(job)
            return CodeRunResult(
                status="ok",
                diff="diff --git a/calc.py",
                tests=TestResult(passed=3, failed=0, errors=0, verdict="passed", measured=True),
                summary={},
                raw={},
                transport="subprocess",
            )

    class _FakeWS:
        path = "/tmp/ws-stub"
        workspace_id = "ws-2"

        def to_dict(self):
            return {"workspace_id": "ws-2"}

    class _FakeWM:
        def create(self, run_id, source, tenant_id=None):
            return _FakeWS()

    monkeypatch.setattr("forgeflow.codeplane.engine.get_code_engine", lambda: _FakeEngine())
    monkeypatch.setattr(
        "forgeflow.codeplane.workspace.get_workspace_manager", lambda: _FakeWM()
    )


def test_second_code_execute_after_failure_runs_one_corrected_retry(monkeypatch):
    # 2026-10-31 clean-B round: a retry after no_change used to return
    # the same failure verbatim, dooming the run. Now the first retry
    # is a real re-execution with a platform correction in the intent.
    prior = {
        "ok": False,
        "outcome": "no_change",
        "codeplane": _cp(outcome="no_change", diff=""),
    }
    task = SimpleNamespace(
        context={"tool_invocations": [_inv("code.execute", status="error", payload=prior)]}
    )
    ctx = SimpleNamespace(run_id="r1", intent="实现 add", tenant_id="t1", task_context=task)
    jobs = []
    _stub_engine(monkeypatch, jobs)
    result = asyncio.run(code_execute({}, ctx))
    assert result.get("reused") is not True
    assert result["ok"] is True
    assert result["codeplane"]["outcome"] == "succeeded"
    assert len(jobs) == 1
    assert "平台纠正" in jobs[0].task_intent
    assert "no_change" in jobs[0].task_intent
    assert task.context["_code_corrected_retry_used"] is True


def test_third_code_execute_reuses_corrected_retry_result():
    # After the corrected-retry slot is used, further retries reuse
    # the latest result verbatim — at most two real engine runs.
    prior = {
        "ok": False,
        "outcome": "failed",
        "codeplane": _cp(outcome="failed", diff=""),
    }
    task = SimpleNamespace(
        context={
            "tool_invocations": [_inv("code.execute", status="error", payload=prior)],
            "_code_corrected_retry_used": True,
        }
    )
    ctx = SimpleNamespace(run_id="r1", intent="实现 add", tenant_id="t1", task_context=task)
    result = asyncio.run(code_execute({}, ctx))
    assert result["reused"] is True
    assert result["ok"] is False
    assert result["codeplane"]["outcome"] == "failed"


def test_gate_refusal_does_not_count_as_execution(monkeypatch):
    # A Docker-gate refusal carries no workspace, so a later real call
    # must not be short-circuited by it: it reaches the gate again.
    prior = {"ok": False, "unavailable": True, "reason": "需要隔离容器"}
    task = SimpleNamespace(
        context={"tool_invocations": [_inv("code.execute", status="error", payload=prior)]}
    )
    ctx = SimpleNamespace(run_id="r1", intent="实现 add", tenant_id="t1", task_context=task)

    class _FakeEngine:
        pass

    monkeypatch.setattr("forgeflow.codeplane.engine.get_code_engine", lambda: _FakeEngine())
    result = asyncio.run(
        code_execute(
            {"handoff_results": [{"kind": "document", "source": "s", "content": "x"}]},
            ctx,
        )
    )
    assert result.get("reused") is not True
    assert result["ok"] is False
    assert result["unavailable"] is True


def test_second_code_execute_after_unverified_runs_one_corrected_retry(monkeypatch):
    # 2026-11-01 second-B shape (6fad659 round): first execution kept a
    # calc.py diff but tests were never measured, so its outcome was
    # "unverified". The old reuse scan skipped that outcome, so a retry
    # re-ran the engine with no correction at all. Now it is a real
    # corrected re-execution that must verify with tests.
    prior = {
        "ok": False,
        "outcome": "unverified",
        "codeplane": _cp(
            outcome="unverified",
            diff="diff --git a/calc.py",
            tests={"verdict": "passed", "measured": False},
        ),
    }
    task = SimpleNamespace(
        context={"tool_invocations": [_inv("code.execute", status="ok", payload=prior)]}
    )
    ctx = SimpleNamespace(run_id="r1", intent="实现 add", tenant_id="t1", task_context=task)
    jobs = []
    _stub_engine(monkeypatch, jobs)
    result = asyncio.run(code_execute({}, ctx))
    assert result.get("reused") is not True
    assert result["ok"] is True
    assert result["codeplane"]["outcome"] == "succeeded"
    assert len(jobs) == 1
    assert "平台纠正" in jobs[0].task_intent
    assert "unverified" in jobs[0].task_intent
    assert "测试" in jobs[0].task_intent
    assert task.context["_code_corrected_retry_used"] is True


def test_third_code_execute_after_unverified_reuses_corrected_retry_result():
    # After the corrected-retry slot is used, an unverified result is
    # reused verbatim like the other non-success outcomes.
    prior = {
        "ok": False,
        "outcome": "unverified",
        "codeplane": _cp(
            outcome="unverified",
            diff="diff --git a/calc.py",
            tests={"verdict": "passed", "measured": False},
        ),
    }
    task = SimpleNamespace(
        context={
            "tool_invocations": [_inv("code.execute", status="ok", payload=prior)],
            "_code_corrected_retry_used": True,
        }
    )
    ctx = SimpleNamespace(run_id="r1", intent="实现 add", tenant_id="t1", task_context=task)
    result = asyncio.run(code_execute({}, ctx))
    assert result["reused"] is True
    assert result["ok"] is False
    assert result["codeplane"]["outcome"] == "unverified"
