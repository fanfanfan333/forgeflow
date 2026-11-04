"""Phase 9 (A1): checkpoint-driven resume — loop seeding + helper gates.

The helper's positive path goes through the real dispatcher (which
schedules the resumed run in the background), so that test polls the
run store for a terminal status instead of awaiting completion.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage

import forgeflow.config as config_mod
import forgeflow.runtime.orchestrator as orch
from forgeflow.config import get_settings
from forgeflow.runtime import llm_planner
from forgeflow.runtime.checkpoint_resume import (
    CheckpointNotFound,
    resume_run_from_checkpoint,
)
from forgeflow.runtime.checkpoints import (
    CheckpointResumeRefused,
    InMemoryCheckpointStore,
    build_state,
    reset_checkpoint_store,
    set_checkpoint_store,
)
from forgeflow.runtime.orchestrator import (
    RequestContext,
    RunRecord,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


class _FakeReactModel:
    def __init__(self, script: list[AIMessage]) -> None:
        self._script = list(script)
        self.model = "fake-react-model"
        self._llm_type = "fake"

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003
        if self._script:
            return self._script.pop(0)
        return AIMessage(content="已有证据收尾。")


class _SettingsProxy:
    def __init__(self, real, **overrides) -> None:
        self._real = real
        self._overrides = overrides

    def __getattr__(self, item: str):
        return self._overrides.get(item, getattr(self._real, item))


def _patch_react(monkeypatch, fake: _FakeReactModel) -> None:
    proxy = _SettingsProxy(
        get_settings(),
        agent_runtime_mode="react",
        llm_provider="mock",
        agent_tool_loading="direct",
    )
    monkeypatch.setattr(orch, "get_settings", lambda: proxy)
    monkeypatch.setattr(config_mod, "get_settings", lambda: proxy)
    monkeypatch.setattr(
        orch,
        "_build_planner_models",
        lambda: (fake, fake, [{"slot": "strong", "class": "_FakeReactModel", "model": fake.model}]),
    )


@pytest.fixture(autouse=True)
def _clean_state():
    reset_run_store()
    reset_checkpoint_store()
    llm_planner.clear_caches()
    yield
    reset_run_store()
    reset_checkpoint_store()
    llm_planner.clear_caches()


def _ctx() -> RequestContext:
    return RequestContext(tenant_id="t-react", user_id="u-1", role="admin")


def _seeded_records():
    return [
        {"tool": "textfile.inspect", "status": "blocked", "payload": {}, "executed": False},
        {"tool": "document.inspect", "status": "blocked", "payload": {}, "executed": False},
    ]


def _checkpoint_state(cursor=2):
    return build_state(
        intent="请查看 /nope1.txt 的内容",
        requirements=[],
        acceptance={"round": 1, "complete": False, "requirements": []},
        next_step={"kind": "model_call"},
        codeplane_ref=None,
        breaker_summary={},
        stuck_summary={},
        approval=None,
        pending_writes=[],
        trajectory_cursor=cursor,
    )


def _source_record(status="interrupted", invocations=None):
    return RunRecord(
        run_id="src-1",
        thread_id="src-1",
        tenant_id="t-react",
        agent_id=None,
        intent="请查看 /nope1.txt 的内容",
        status=status,
        outcome="",
        steps=[],
        errors=[],
        created_at="2026-11-04T00:00:00+00:00",
        tool_invocations=list(invocations) if invocations is not None else _seeded_records(),
        declared_inputs={},
        workflow_type="generic",
        session_id="",
        actor_user_id="u-1",
        actor_role="admin",
    )


async def _wait_terminal(run_id: str, timeout: float = 20.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        record = get_run_store().get(run_id)
        if record is not None and record.status in ("completed", "failed", "aborted"):
            return record
        await asyncio.sleep(0.2)
    raise AssertionError(f"run {run_id} did not reach a terminal status in {timeout}s")


async def test_loop_seeds_checkpoint_records_and_audit(monkeypatch):
    """Direct run_task with a _checkpoint_resume marker (no dispatcher)."""
    from forgeflow.runtime.checkpoints import build_resume_seed

    fake = _FakeReactModel([])
    _patch_react(monkeypatch, fake)
    store = InMemoryCheckpointStore()
    checkpoint = store.save("t-react", run_id="src-1", round=1, kind="round", state=_checkpoint_state())
    seed = build_resume_seed(checkpoint, _seeded_records())
    task = TaskCreate(intent="请查看 /nope1.txt 的内容", workflow_type="generic")
    # Attribute only: run_task pops tool_invocations / llm_runtime from
    # submitted context, so context seeding would be stripped.
    task._checkpoint_resume = seed
    handle = await run_task(task, _ctx(), bus=_RecordingBus())
    tools = [s.get("tool") for s in handle.detail["steps"]]
    assert "textfile.inspect" in tools and "document.inspect" in tools
    llm = handle.detail["llm"]
    assert llm["resumed_from_checkpoint"]["source_run_id"] == "src-1"
    assert llm["resumed_from_checkpoint"]["checkpoint_round"] == 1
    assert llm["acceptance_history"][0]["round"] == 1


async def test_helper_resumes_interrupted_run_from_latest_checkpoint(monkeypatch):
    fake = _FakeReactModel([])
    _patch_react(monkeypatch, fake)
    get_run_store().save(_source_record())
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    store.save("t-react", run_id="src-1", round=1, kind="round", state=_checkpoint_state())
    handle = await resume_run_from_checkpoint(
        tenant="t-react", user_id="u-1", role="admin", run_id="src-1"
    )
    assert handle.parent_run_id == "src-1"
    record = await _wait_terminal(handle.run_id)
    assert record.status == "completed"
    tools = [s.get("tool") for s in record.steps]
    assert "textfile.inspect" in tools and "document.inspect" in tools
    assert record.llm["resumed_from_checkpoint"]["source_run_id"] == "src-1"


async def test_helper_refuses_completed_run(monkeypatch):
    get_run_store().save(_source_record(status="completed"))
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    store.save("t-react", run_id="src-1", round=1, kind="round", state=_checkpoint_state())
    with pytest.raises(CheckpointResumeRefused, match="not resumable"):
        await resume_run_from_checkpoint(
            tenant="t-react", user_id="u-1", role="admin", run_id="src-1"
        )


async def test_helper_refuses_approval_checkpoint_with_pointer(monkeypatch):
    get_run_store().save(_source_record(status="awaiting_approval"))
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    store.save("t-react", run_id="src-1", round=1, kind="approval", state=_checkpoint_state())
    with pytest.raises(CheckpointResumeRefused, match="pending-actions"):
        await resume_run_from_checkpoint(
            tenant="t-react", user_id="u-1", role="admin", run_id="src-1"
        )


async def test_helper_missing_checkpoint_is_not_found(monkeypatch):
    get_run_store().save(_source_record())
    set_checkpoint_store(InMemoryCheckpointStore())
    with pytest.raises(CheckpointNotFound, match="no checkpoint"):
        await resume_run_from_checkpoint(
            tenant="t-react", user_id="u-1", role="admin", run_id="src-1"
        )


async def test_helper_refuses_when_trajectory_gone(monkeypatch):
    get_run_store().save(_source_record(invocations=[]))
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    store.save("t-react", run_id="src-1", round=1, kind="round", state=_checkpoint_state(cursor=2))
    with pytest.raises(CheckpointResumeRefused, match="trajectory no longer available"):
        await resume_run_from_checkpoint(
            tenant="t-react", user_id="u-1", role="admin", run_id="src-1"
        )


async def test_endpoint_maps_refusals_to_http_codes():
    """Endpoint function called directly (Depends bypassed with real args)."""
    from fastapi import HTTPException

    from forgeflow.api.hub_schemas import CheckpointResumeRequest
    from forgeflow.api.routers.runs import resume_from_checkpoint
    from forgeflow.rbac.models import UserContext

    user = UserContext(user_id="u-1", role="admin")
    # Completed run -> 409.
    get_run_store().save(_source_record(status="completed"))
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    store.save("t-react", run_id="src-1", round=1, kind="round", state=_checkpoint_state())
    with pytest.raises(HTTPException) as excinfo:
        await resume_from_checkpoint("src-1", CheckpointResumeRequest(), user, "t-react")
    assert excinfo.value.status_code == 409
    # No checkpoint -> 404.
    get_run_store().save(_source_record())
    set_checkpoint_store(InMemoryCheckpointStore())
    with pytest.raises(HTTPException) as excinfo:
        await resume_from_checkpoint("src-1", CheckpointResumeRequest(), user, "t-react")
    assert excinfo.value.status_code == 404


async def test_helper_forks_completed_run_without_changing_source(monkeypatch):
    """A3: fork starts a new lineage from a COMPLETED source; source untouched."""
    fake = _FakeReactModel([])
    _patch_react(monkeypatch, fake)
    get_run_store().save(_source_record(status="completed"))
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    store.save("t-react", run_id="src-1", round=1, kind="round", state=_checkpoint_state())
    handle = await resume_run_from_checkpoint(
        tenant="t-react", user_id="u-1", role="admin", run_id="src-1", fork=True
    )
    assert handle.parent_run_id == "src-1"
    record = await _wait_terminal(handle.run_id)
    assert record.status == "completed"
    assert record.llm["resumed_from_checkpoint"]["forked"] is True
    assert get_run_store().get("src-1").status == "completed"


async def test_helper_resumes_named_checkpoint_not_latest(monkeypatch):
    fake = _FakeReactModel([])
    _patch_react(monkeypatch, fake)
    get_run_store().save(_source_record())
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    first = store.save("t-react", run_id="src-1", round=1, kind="round", state=_checkpoint_state())
    store.save("t-react", run_id="src-1", round=2, kind="round", state=_checkpoint_state())
    handle = await resume_run_from_checkpoint(
        tenant="t-react", user_id="u-1", role="admin", run_id="src-1", checkpoint_id=str(first.id)
    )
    record = await _wait_terminal(handle.run_id)
    assert record.llm["resumed_from_checkpoint"]["checkpoint_round"] == 1


async def test_history_endpoint_lists_newest_first():
    """A3: direct call of the history route (Depends bypassed)."""
    from forgeflow.api.routers.runs import list_run_checkpoints
    from forgeflow.rbac.models import UserContext

    get_run_store().save(_source_record())
    store = InMemoryCheckpointStore()
    set_checkpoint_store(store)
    store.save("t-react", run_id="src-1", round=1, kind="round", state=_checkpoint_state())
    store.save("t-react", run_id="src-1", round=2, kind="round", state=_checkpoint_state())
    user = UserContext(user_id="u-1", role="admin")
    rows = await list_run_checkpoints("src-1", limit=20, user=user, tenant="t-react")
    assert [r.round for r in rows] == [2, 1]


async def test_loop_seeds_responded_tool_without_executing(monkeypatch):
    """A4: a human answer joins the trail as an ok record; the tool never runs."""
    fake = _FakeReactModel([])
    _patch_react(monkeypatch, fake)
    task = TaskCreate(intent="转一笔钱", workflow_type="generic")
    task._responded_tool = {
        "tool": "mock.transfer",
        "response_text": "已线下转账",
        "args_hash": "abc",
    }
    handle = await run_task(task, _ctx(), bus=_RecordingBus())
    tools = [s.get("tool") for s in handle.detail["steps"]]
    assert "mock.transfer" in tools
    record = get_run_store().get(handle.run_id)
    synthetic = [r for r in record.tool_invocations if r.get("tool") == "mock.transfer"][0]
    assert synthetic["status"] == "ok"
    assert synthetic["payload"]["human_response"] == "已线下转账"
    assert synthetic["payload"]["responded"] is True
