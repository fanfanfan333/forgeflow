from types import SimpleNamespace

import pytest

from forgeflow.runtime.recovery import RecoveryBlocked, RecoveryStore, input_versions


def state():
    return {"messages": [{"type": "human", "data": {"content": "resume"}}], "inflight": False}


def test_checkpoint_is_durable_and_tenant_actor_scoped(tmp_path):
    path = tmp_path / "journal.sqlite3"
    store = RecoveryStore(path)
    assert store.save("one", "alice", "run", state(), status="paused") == 1
    assert RecoveryStore(path).get("one", "alice", "run")["version"] == 1
    for tenant, actor in [("two", "alice"), ("one", "bob")]:
        with pytest.raises(RecoveryBlocked):
            store.get(tenant, actor, "run")


def test_resume_claim_is_compare_and_swap(tmp_path):
    store = RecoveryStore(tmp_path / "journal.sqlite3")
    store.save("one", "alice", "run", state(), status="paused")
    assert store.claim("one", "alice", "run", 1)["messages"] == state()["messages"]
    with pytest.raises(RecoveryBlocked):
        store.claim("one", "alice", "run", 1)


@pytest.mark.parametrize("change", [{"inflight": True}, {"messages": []}, {"unsupported": True}])
def test_unknown_effect_or_missing_context_cannot_be_replayed(tmp_path, change):
    store = RecoveryStore(tmp_path / "journal.sqlite3")
    store.save("one", "alice", "run", {**state(), **change}, status="paused")
    with pytest.raises(RecoveryBlocked):
        store.claim("one", "alice", "run", 1)
    assert store.get("one", "alice", "run")["status"] == "paused"


def test_input_version_changes_are_detected(tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("before")
    scope = SimpleNamespace(inherited_inputs={"text_paths": [str(path)]})
    before = input_versions(scope)
    path.write_text("after")
    assert before != input_versions(scope)


def test_dead_worker_checkpoint_becomes_paused_but_unknown_effect_stays_blocked(tmp_path):
    import json

    store = RecoveryStore(tmp_path / "journal.sqlite3")
    for name, inflight in [("safe", False), ("uncertain", True)]:
        store.save("one", "alice", name, {**state(), "inflight": inflight})
        with store.connect() as conn:
            conn.execute(
                "UPDATE checkpoints SET body=? WHERE run=?",
                (json.dumps({**state(), "owner": "f" * 32, "inflight": inflight}), name),
            )
        saved = store.get("one", "alice", name)
        assert saved["status"] == "paused" and saved["version"] == 2
        if inflight:
            with pytest.raises(RecoveryBlocked, match="待核实"):
                store.claim("one", "alice", name, saved["version"])
        else:
            store.claim("one", "alice", name, saved["version"])


def test_live_worker_is_never_marked_paused(tmp_path):
    store = RecoveryStore(tmp_path / "journal.sqlite3")
    store.save("one", "alice", "run", state())
    assert store.get("one", "alice", "run")["status"] == "active"


def test_pii_filter_never_mutates_resource_identifiers():
    from forgeflow.middleware.security import _walk

    identifier = "f1234567-1380-0138-0001-138001380001"
    value, _, _ = _walk(
        {"context": {"resources": [identifier, "res_" + identifier]}, "intent": "电话13800138000"}
    )
    assert value["context"]["resources"] == [identifier, "res_" + identifier]
    assert "13800138000" not in value["intent"]


def test_workspace_identity_is_not_redacted_as_a_phone_number():
    from forgeflow.middleware.security import _walk

    identity = "bench-0bad73a4c4a13800138000eda7572b3bf"
    assert _walk({"workspace_id": identity})[0]["workspace_id"] == identity
    assert _walk({"intent": "联系13800138000"})[0]["intent"] != "联系13800138000"


def test_checkpoint_survives_actual_worker_process_exit(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "process.sqlite3"
    code = "from pathlib import Path; from forgeflow.runtime.recovery import RecoveryStore; import sys,os; RecoveryStore(Path(sys.argv[1])).save('one','alice','run', {'messages':[{'type':'human','data':{'content':'continue'}}],'inflight':False}); os._exit(19)"
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)], capture_output=True, timeout=15
    )
    assert result.returncode == 19
    store = RecoveryStore(path)
    saved = store.get("one", "alice", "run")
    assert saved["status"] == "paused"
    assert store.claim("one", "alice", "run", saved["version"])["messages"]


async def test_missing_checkpoint_and_revoked_input_are_conflicts_not_server_errors(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from forgeflow.api.hub_schemas import RecoveryResumeRequest
    from forgeflow.api.routers import runs
    from forgeflow.runtime.input_scope import InputReferenceError

    user = SimpleNamespace(user_id="alice", role="manager")
    monkeypatch.setattr(
        runs, "_load_run", AsyncMock(return_value=SimpleNamespace(status="aborted"))
    )

    class Missing:
        def get(self, *args):
            raise RecoveryBlocked("missing")

    monkeypatch.setattr("forgeflow.runtime.recovery.RecoveryStore", Missing)
    with pytest.raises(HTTPException) as error:
        await runs.resume_run("run", RecoveryResumeRequest(version=1), user, "one")
    assert error.value.status_code == 409

    class Revoked:
        def get(self, *args):
            return {
                "state": {"task": {"intent": "continue", "context": {"resources": ["revoked"]}}}
            }

    monkeypatch.setattr("forgeflow.runtime.recovery.RecoveryStore", Revoked)
    monkeypatch.setattr(
        "forgeflow.runtime.input_scope.prepare_input_scope",
        AsyncMock(side_effect=InputReferenceError("Input unavailable")),
    )
    with pytest.raises(HTTPException) as error:
        await runs.resume_run("run", RecoveryResumeRequest(version=1), user, "one")
    assert error.value.status_code == 409


def test_every_budget_mutation_is_persisted_before_an_operation(tmp_path):
    from forgeflow.runtime.execution_budget import ExecutionBudget, current_budget
    from forgeflow.runtime.recovery import current_recovery

    store = RecoveryStore(tmp_path / "budget.sqlite3")
    store.save("one", "alice", "run", state())
    budget = ExecutionBudget()
    budget_token = current_budget.set(budget)
    recovery_token = current_recovery.set((store, "one", "alice", "run"))
    try:
        budget.consume("model")
        budget.consume("tool")
        budget.consume("delegation")
        saved = store.get("one", "alice", "run")["state"]["budget"]
        assert (saved["model_calls"], saved["tool_calls"], saved["delegations"]) == (1, 1, 1)
        lease = budget.reserve_code()
        assert (
            store.get("one", "alice", "run")["state"]["budget"]["reserved_model_calls"]
            == lease["model"]
        )
        budget.settle_code(lease, {"model": 2, "tool": 3})
        saved = store.get("one", "alice", "run")["state"]["budget"]
        assert (
            saved["model_calls"] == 3
            and saved["tool_calls"] == 4
            and saved["reserved_model_calls"] == 0
        )
    finally:
        current_recovery.reset(recovery_token)
        current_budget.reset(budget_token)
