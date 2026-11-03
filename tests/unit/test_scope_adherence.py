"""Scope adherence: an explicit only-modify restriction must be enforced.

2026-11-03 c6e8ed4 round: task \"只修改 add\", the diff additionally
added ``multiply``, yet the run was judged ``completed/success``.
Acceptance only checked that an edit happened (non-empty diff +
passing tests) — never that the edit stayed inside the requested
scope. So scope was unmeasured by construction.

v1 enforces the narrow, provable case: an explicit restriction
(\"只/仅/only + 修改/modify + symbol\") attached to the edit
requirement, checked against the top-level symbols the terminal
diff ADDS. A run that adds a symbol outside the scope gets a named
scope error and fails instead of succeeding. Body-only changes
never violate (a diff alone cannot attribute a changed line to a
function), and a file reference (\"只修改 calc.py\") is not a symbol
scope.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from forgeflow.runtime.acceptance import (
    _scope_violation,
    assess,
    requirement_list,
)
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    _scope_violation_error,
    get_run_store,
    reset_run_store,
    run_task,
)


_DIFF_ADD_MULTIPLY = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,4 @@
 def add(a, b):
     return a + b
+def multiply(a, b):
+    return a * b
"""

_DIFF_BODY_ONLY = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a + b + 0
"""

_DIFF_NESTED_HELPER = """diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,5 @@
 def add(a, b):
-    return a + b
+    def helper(x):
+        return x
+    return helper(a) + helper(b)
"""


def _code_execute(diff: str) -> dict:
    return {
        "tool": "code.execute",
        "status": "ok",
        "step_id": "s-exec",
        "payload": {
            "ok": True,
            "codeplane": {
                "diff": diff,
                "tests": {"measured": True, "verdict": "passed"},
            },
        },
    }


def _edit_row(intent: str) -> dict:
    rows = requirement_list(intent, {"resources": ["calc.py"]}, None)
    edits = [row for row in rows if row["id"] == "edit"]
    assert edits, rows
    return edits[0]


# --------------------------------------------------------------------------- #
# 1. Scope detection on the requirement                                        #
# --------------------------------------------------------------------------- #
def test_only_modify_names_a_symbol_scope() -> None:
    assert _edit_row("只修改 add")["scope"] == {"only": ["add"]}
    assert _edit_row("请修改文件，只改 add")["scope"] == {"only": ["add"]}
    assert _edit_row("只对 add 做修改")["scope"] == {"only": ["add"]}
    assert _edit_row("please edit the file, only modify add")["scope"] == {"only": ["add"]}


def test_plain_modify_and_file_reference_carry_no_symbol_scope() -> None:
    assert "scope" not in _edit_row("修改 add")
    assert "scope" not in _edit_row("只修改 calc.py")


# --------------------------------------------------------------------------- #
# 2. Diff symbol check                                                         #
# --------------------------------------------------------------------------- #
def test_added_symbol_outside_scope_violates() -> None:
    message = _scope_violation({"only": ["add"]}, _DIFF_ADD_MULTIPLY)
    assert message is not None
    assert "multiply" in message and "add" in message


def test_body_only_and_nested_helper_do_not_violate() -> None:
    assert _scope_violation({"only": ["add"]}, _DIFF_BODY_ONLY) is None
    assert _scope_violation({"only": ["add"]}, _DIFF_NESTED_HELPER) is None
    assert _scope_violation({"only": ["add"]}, "") is None
    assert _scope_violation(None, _DIFF_ADD_MULTIPLY) is None


# --------------------------------------------------------------------------- #
# 3. assess withholds satisfaction on a scope violation                        #
# --------------------------------------------------------------------------- #
def test_assess_unsatisfied_and_flagged_on_violation() -> None:
    row = _edit_row("只修改 add")
    result = assess([row], [_code_execute(_DIFF_ADD_MULTIPLY)])
    assessed = result["requirements"][0]
    assert assessed["status"] != "satisfied"
    assert "multiply" in (assessed.get("scope_violation") or "")


def test_assess_satisfied_when_added_symbol_inside_scope() -> None:
    row = _edit_row("只修改 multiply")
    result = assess([row], [_code_execute(_DIFF_ADD_MULTIPLY)])
    assert result["requirements"][0]["status"] == "satisfied"


# --------------------------------------------------------------------------- #
# 4. The verdict step (pure)                                                   #
# --------------------------------------------------------------------------- #
def test_scope_violation_error_step() -> None:
    task = SimpleNamespace(
        intent="只修改 add",
        context={
            "resources": ["calc.py"],
            "tool_invocations": [_code_execute(_DIFF_ADD_MULTIPLY)],
        },
    )
    assert _scope_violation_error(task, None) is not None
    assert "multiply" in (_scope_violation_error(task, None) or "")

    task.context["tool_invocations"] = [_code_execute(_DIFF_BODY_ONLY)]
    assert _scope_violation_error(task, None) is None

    task.intent = "修改 add"
    task.context["tool_invocations"] = [_code_execute(_DIFF_ADD_MULTIPLY)]
    assert _scope_violation_error(task, None) is None


# --------------------------------------------------------------------------- #
# 5. Wiring: a violating run fails with the scope error                        #
# --------------------------------------------------------------------------- #
class _RecordingBus:
    """Minimal bus stub — ``run_task`` only ever calls ``emit``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    yield
    reset_run_store()


@pytest.mark.asyncio
async def test_violating_run_fails_with_scope_error(monkeypatch) -> None:
    """End to end through _run_task: the terminal verdict carries the
    scope error and the run fails instead of succeeding."""
    import forgeflow.runtime.acceptance as acceptance
    import forgeflow.runtime.orchestrator as orch

    original = acceptance.requirement_list

    def _scoped(intent, context, inputs):
        rows = original(intent, context, inputs)
        rows.append(
            {
                "id": "edit",
                "description": "按原始要求修改并校验文件",
                "original_request": intent,
                "scope": {"only": ["add"]},
                "status": "unverified",
                "evidence": [],
            }
        )
        return rows

    monkeypatch.setattr(acceptance, "requirement_list", _scoped)
    real = orch._default_executor

    async def _exec(task, ctx, bus, run_id, *, policy_engine=None, attempt=0):
        steps, _errors = await real(
            task, ctx, bus, run_id, policy_engine=policy_engine, attempt=attempt
        )
        trail = list(task.context.get("tool_invocations") or [])
        trail.append(_code_execute(_DIFF_ADD_MULTIPLY))
        task.context["tool_invocations"] = trail
        return steps, []

    monkeypatch.setattr(orch, "_default_executor", _exec)
    ctx = RequestContext(tenant_id="t-scope-record", user_id="admin-1", role="admin")
    handle = await run_task(
        TaskCreate(intent="离线分析一次"), ctx, bus=_RecordingBus()
    )
    record = get_run_store().get(handle.run_id)
    assert record is not None
    assert record.status == "failed"
    assert any("超出修改范围" in error and "multiply" in error for error in record.errors)
