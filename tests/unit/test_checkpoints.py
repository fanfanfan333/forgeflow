"""Phase 7: run checkpoints — state, resume gate, stores.

Pure-function tests + InMemory store tests. The Postgres store is
covered by the PG integration test (real table, migration 042).
"""

import pytest

from forgeflow.runtime.checkpoints import (
    Checkpoint,
    CheckpointResumeRefused,
    InMemoryCheckpointStore,
    build_state,
    get_checkpoint_store,
    idempotency_key,
    reset_checkpoint_store,
    set_checkpoint_store,
    verify_resume,
)


def _state(approval=None):
    return build_state(
        intent="do the thing",
        requirements=[{"id": "r1", "satisfied": True}],
        acceptance={"round": 1, "complete": True},
        next_step=None,
        codeplane_ref=None,
        breaker_summary={},
        stuck_summary={},
        approval=approval,
        pending_writes=[],
        trajectory_cursor=7,
    )


def _checkpoint(state=None, schema_version=1):
    return Checkpoint(
        tenant_id="t1",
        run_id="run-1",
        round=2,
        kind="round",
        schema_version=schema_version,
        state=state if state is not None else _state(),
    )


# --- pure helpers ------------------------------------------------------------ #


def test_idempotency_key_format():
    assert idempotency_key("run-1", 2, "call-9") == "run-1:2:call-9"


def test_build_state_carries_schema_version_and_refs_only():
    state = _state()
    assert state["schema_version"] == 1
    assert state["trajectory_cursor"] == 7
    assert state["intent"] == "do the thing"


@pytest.mark.parametrize("status", ["interrupted", "awaiting_approval", "failed"])
def test_verify_resume_ok_for_resumable_statuses(status):
    assert verify_resume(_checkpoint(), run_status=status)["intent"] == "do the thing"


@pytest.mark.parametrize("status", ["running", "completed", "aborted"])
def test_verify_resume_refuses_non_resumable_statuses(status):
    with pytest.raises(CheckpointResumeRefused, match="not resumable"):
        verify_resume(_checkpoint(), run_status=status)


def test_verify_resume_refuses_old_schema():
    with pytest.raises(CheckpointResumeRefused, match="schema_version"):
        verify_resume(_checkpoint(schema_version=2), run_status="interrupted")


def _approved_action(owner="u1", args_hash="abc", expires="2099-01-01T00:00:00+00:00"):
    return {
        "status": "resolved",
        "resolution": "approve",
        "resolved_by": owner,
        "expires_at": expires,
        "payload": {"args_hash": args_hash},
    }


def test_verify_resume_approval_ok():
    approval = {"pending_id": "pa-1", "owner": "u1", "args_hash": "abc"}
    state = verify_resume(
        _checkpoint(state=_state(approval=approval)),
        run_status="awaiting_approval",
        approval_resolver=lambda pid: _approved_action() if pid == "pa-1" else None,
        now_iso="2026-11-04T00:00:00+00:00",
    )
    assert state["approval"]["pending_id"] == "pa-1"


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda a: None, "no longer exists"),
        (lambda a: {**a, "status": "waiting"}, "not approved"),
        (lambda a: {**a, "resolution": "reject"}, "not approved"),
        (lambda a: {**a, "resolved_by": "u2"}, "resolver != checkpoint owner"),
        (lambda a: {**a, "expires_at": "2020-01-01T00:00:00+00:00"}, "expired"),
        (lambda a: {**a, "payload": {"args_hash": "different"}}, "payload hash changed"),
    ],
)
def test_verify_resume_approval_refusals(mutate, match):
    approval = {"pending_id": "pa-1", "owner": "u1", "args_hash": "abc"}
    with pytest.raises(CheckpointResumeRefused, match=match):
        verify_resume(
            _checkpoint(state=_state(approval=approval)),
            run_status="awaiting_approval",
            approval_resolver=lambda pid: mutate(_approved_action()),
            now_iso="2026-11-04T00:00:00+00:00",
        )


def test_verify_resume_approval_without_resolver_refuses():
    approval = {"pending_id": "pa-1", "owner": "u1", "args_hash": "abc"}
    with pytest.raises(CheckpointResumeRefused, match="no resolver"):
        verify_resume(
            _checkpoint(state=_state(approval=approval)),
            run_status="awaiting_approval",
        )


# --- InMemory store ---------------------------------------------------------- #


def test_inmemory_save_latest_history_and_tenant_isolation():
    store = InMemoryCheckpointStore()
    store.save("t1", run_id="run-1", round=0, kind="round", state=_state())
    store.save("t1", run_id="run-1", round=1, kind="round", state=_state())
    store.save("t1", run_id="run-1", round=1, kind="approval", state=_state())
    latest = store.latest("t1", "run-1")
    assert latest is not None and latest.kind == "approval" and latest.round == 1
    assert store.latest("t2", "run-1") is None
    assert [row.round for row in store.history("t1", "run-1")] == [1, 1, 0]
    with pytest.raises(ValueError, match="tenant is required"):
        store.save("", run_id="run-1", round=0, kind="round", state=_state())
    with pytest.raises(ValueError, match="unknown checkpoint kind"):
        store.save("t1", run_id="run-1", round=0, kind="weird", state=_state())


def test_inmemory_prune_keeps_latest_rounds_and_all_approvals():
    store = InMemoryCheckpointStore()
    for round_no in range(12):
        store.save("t1", run_id="run-1", round=round_no, kind="round", state=_state())
    store.save("t1", run_id="run-1", round=2, kind="approval", state=_state())
    store.save("t1", run_id="run-1", round=9, kind="approval", state=_state())
    deleted = store.prune("t1", "run-1", keep_latest=10)
    assert deleted == 2
    remaining = store.history("t1", "run-1", limit=50)
    assert len(remaining) == 12  # 10 rounds + 2 approvals
    assert sum(1 for row in remaining if row.kind == "approval") == 2
    assert min(row.round for row in remaining if row.kind == "round") == 2


def test_store_hooks():
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    try:
        assert get_checkpoint_store() is store
    finally:
        reset_checkpoint_store()


# --- build_resume_seed (Phase 9, A1) ------------------------------------------ #


def test_build_resume_seed_slices_at_cursor_and_summarises():
    from forgeflow.runtime.checkpoints import build_resume_seed

    invocations = [
        {"tool": "textfile.inspect", "status": "blocked"},
        {"tool": "document.inspect", "status": "ok"},
        {"tool": "textfile.edit", "status": "ok"},
    ]
    checkpoint = _checkpoint(
        state=_state(approval=None)
        | {
            "trajectory_cursor": 2,
            "acceptance": {"round": 1, "complete": False, "requirements": []},
        }
    )
    seed = build_resume_seed(checkpoint, invocations)
    assert len(seed["records"]) == 2
    assert seed["records"][0]["tool"] == "textfile.inspect"
    assert seed["acceptance_history"][0]["round"] == 1
    assert "textfile.inspect: blocked" in seed["evidence_summary"]
    assert "document.inspect: ok" in seed["evidence_summary"]
    assert "textfile.edit" not in seed["evidence_summary"]


def test_build_resume_seed_truncates_long_evidence():
    from forgeflow.runtime.checkpoints import MAX_EVIDENCE_CHARS, build_resume_seed

    invocations = [{"tool": f"tool{i}", "status": "ok"} for i in range(500)]
    checkpoint = _checkpoint(state=_state() | {"trajectory_cursor": 500})
    seed = build_resume_seed(checkpoint, invocations)
    assert len(seed["evidence_summary"]) <= MAX_EVIDENCE_CHARS + 100
    assert "截断" in seed["evidence_summary"]


def test_checkpoint_store_durability_tiers():
    """Phase 9 B9: durability is explicit, not inferred from the class."""
    from forgeflow.runtime.checkpoints import InMemoryCheckpointStore, PostgresCheckpointStore

    assert InMemoryCheckpointStore.durability == "ephemeral"
    assert PostgresCheckpointStore.durability == "durable"
