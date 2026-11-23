"""Regression tests for the eleven reported business defects (BIZ-01 … BIZ-11).

Each test is written to fail on the pre-fix code and pass on the fixed code;
the docstring names the exact behaviour that used to be wrong. They are grouped
by defect so a future regression points straight at the broken guarantee.

Covered:

1.  BIZ-01 审批 pending 时 executor 不得进入执行分支（supervisor 阶段映射 + executor 独立校验）
2.  BIZ-02 流式工作流必须像普通入口一样保存记录并创建审批请求
3.  BIZ-03 FINISH 必须过程序化完成条件，不能由模型一句话结束
4.  BIZ-04 已删除会话在任务完成后不得复活（删除是墓碑）
5.  BIZ-05 持久化失败必须写在运行记录上，并提供补存入口
6.  BIZ-06 历史回填必须先按用户过滤再限量
7.  BIZ-07 评测记录同一时刻写入时，取「最后写入」而不是随机一条
8.  BIZ-08 生命周期事件同一时刻写入时，按追加序而不是随机 id 排序
9.  BIZ-09 重试的耗时预算必须包含第一次执行
10. BIZ-10 MCP 重新发现后，已编译业务图必须拿到新工具集
11. BIZ-11 幂等任务「已登记但未启动」时必须可以重新调度
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

# --------------------------------------------------------------------------- #
# BIZ-01 — a pending approval can never become an execution                    #
# --------------------------------------------------------------------------- #


def _stub_model(**structured_kwargs):
    """A chat-model stub: ``with_structured_output(...).ainvoke()`` is canned."""

    class _Stub:
        model_name = "stub"

        def __init__(self, **payload):
            self._payload = SimpleNamespace(**payload)

        def bind_tools(self, _tools):
            return self

        def with_structured_output(self, _schema, **_kwargs):
            return self

        async def ainvoke(self, *_args, **_kwargs):
            return self._payload

    return _Stub(**structured_kwargs)


def test_pending_approval_cannot_route_the_supervisor_to_executor():
    from forgeflow.agents.supervisor import gate_routing, stage_after

    state = {"current_stage": "approve", "approval_status": "pending"}
    worker, override = gate_routing("executor", "approve", state)
    assert worker == "human_approval"
    assert override == "approval_pending"
    # Crucially the *stage* stays "approve": no path may mint "execute".
    assert stage_after(worker, "approve", state) == "approve"


def test_approved_request_still_reaches_execution():
    from forgeflow.agents.supervisor import gate_routing, stage_after

    state = {"current_stage": "approve", "approval_status": "approved"}
    worker, override = gate_routing("executor", "approve", state)
    assert (worker, override) == ("executor", None)
    assert stage_after(worker, "approve", state) == "execute"


@pytest.mark.asyncio
async def test_supervisor_run_never_writes_execute_while_approval_is_pending():
    from forgeflow.agents.supervisor import SupervisorAgent

    agent = SupervisorAgent(_stub_model(next="executor", reasoning="just do it"))
    state = {
        "current_stage": "approve",
        "approval_status": "pending",
        "messages": [],
        "analysis_scores": [],
        "errors": [],
        "lead_data": {"company_name": "ACME"},
    }

    patch = await agent.run(state)

    assert patch["next_agent"] == "human_approval"
    assert patch["current_stage"] == "approve"
    assert "approval_pending" in patch["messages"][-1].content


@pytest.mark.asyncio
async def test_executor_refuses_the_execute_branch_without_an_approved_decision():
    from forgeflow.agents.executor import ExecutorAgent

    agent = ExecutorAgent(_stub_model())
    # The exact shape the supervisor slip used to produce: stage already
    # "execute", approval still pending.
    state = {
        "current_stage": "execute",
        "approval_status": "pending",
        "messages": [],
        "lead_data": {"company_name": "ACME"},
        "executed_actions": [],
        "errors": [],
    }

    patch = await agent.run(state)

    assert patch["executed_actions"] == ["execution_blocked_approval_pending"]
    assert patch["next_agent"] == "human_approval"
    assert patch["current_stage"] == "approve"
    assert any("approval_status must be 'approved'" in err for err in patch["errors"])


@pytest.mark.asyncio
async def test_refusal_routes_back_into_a_real_interrupt_point():
    """Sending a pending-approval executor back must suspend, not finish.

    ``gate_routing`` returns ``human_approval`` for a pending decision; that is
    only safe because the compiled graph interrupts *before* that node, so the
    run waits for a human instead of ending quietly.
    """
    from forgeflow.graph.builder import compile_graph
    from forgeflow.graph.edges import route_human_approval

    graph = await compile_graph(mcp_tools=[], use_checkpointer=False)

    assert "human_approval" in list(graph.interrupt_before_nodes)
    assert route_human_approval({"approval_status": "pending"}) == "END"


def test_run_detail_is_owner_only_regardless_of_role():
    """``GET /runs/{id}`` is owner-only — role does not buy horizontal access.

    The workflow-history surfaces keep their own oversight policy
    (``_ELEVATED_READ_ROLES``); this test pins only the run-detail contract that
    ``test_inc56_task_owner_isolation`` / ``test_enterprise_input_admission``
    already enforce: no role may read another person's run, and the refusal is a
    404 so the run's existence is never confirmed.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forgeflow.api.dependencies import get_current_user
    from forgeflow.api.hub_deps import resolve_tenant
    from forgeflow.api.routers.runs import router as runs_router
    from forgeflow.rbac.models import UserContext
    from forgeflow.runtime.orchestrator import (
        RunRecord,
        get_run_store,
        reset_run_store,
    )

    reset_run_store()
    record = RunRecord(
        run_id="run-policy-1",
        thread_id="th-policy-1",
        tenant_id="t-policy",
        agent_id=None,
        intent="给 ACME 报价",
        status="completed",
        outcome="success",
        steps=[],
        errors=[],
        created_at=datetime.now(UTC).isoformat(),
        actor_user_id="rep-1",
        actor_role="sales_rep",
    )
    get_run_store().save(record)

    def client(role: str, user_id: str) -> TestClient:
        app = FastAPI()
        app.dependency_overrides[resolve_tenant] = lambda: "t-policy"
        app.dependency_overrides[get_current_user] = lambda: UserContext(user_id=user_id, role=role)
        app.include_router(runs_router, prefix="/runs")
        return TestClient(app)

    assert client("sales_rep", "rep-1").get("/runs/run-policy-1").status_code == 200
    # Vertical privilege does not become horizontal visibility.
    for role, uid in (("manager", "mgr-1"), ("admin", "adm-1"), ("viewer", "vw-1")):
        denied = client(role, uid).get("/runs/run-policy-1")
        assert denied.status_code == 404, (role, denied.status_code, denied.text)
        assert denied.json()["detail"] == "Run not found"
    # Horizontal IDOR: another rep must not learn this run exists.
    other = client("sales_rep", "rep-2").get("/runs/run-policy-1")
    assert other.status_code == 404
    assert other.json()["detail"] == "Run not found"


def test_codeplane_review_seat_needs_a_pending_approval_to_see_the_run():
    """The code-review exception is an *approval-scoped* entitlement.

    Code review needs the reviewer to open a diff they did not request. That
    must not decay into "admin/manager read anyone's code run": the reviewer
    gets access only while a code-commit approval is actually pending on the
    run. Both halves are pinned here, plus the 409 that distinguishes a
    non-code run from a missing one.
    """
    from fastapi import HTTPException

    from forgeflow.api.routers.codeplane import _load_code_run
    from forgeflow.codeplane.approval import request_code_approval
    from forgeflow.rbac.models import UserContext
    from forgeflow.repositories import get_policy_repository, reset_repositories
    from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store

    reset_run_store()
    reset_repositories()
    get_run_store().save(
        RunRecord(
            run_id="run-code-1",
            thread_id="th-code-1",
            tenant_id="t-code",
            agent_id=None,
            intent="修复 mathlib.add 的失败测试",
            status="awaiting_approval",
            outcome=None,
            steps=[],
            errors=[],
            created_at=datetime.now(UTC).isoformat(),
            actor_user_id="rep-1",
            actor_role="sales_rep",
        )
    )
    get_run_store().get("run-code-1").codeplane = {"workspace": {"workspace_id": "ws-1"}}
    # A run that is NOT a code task, for the 409 branch.
    get_run_store().save(
        RunRecord(
            run_id="run-plain-1",
            thread_id="th-plain-1",
            tenant_id="t-code",
            agent_id=None,
            intent="给 ACME 报价",
            status="completed",
            outcome="success",
            steps=[],
            errors=[],
            created_at=datetime.now(UTC).isoformat(),
            actor_user_id="rep-1",
            actor_role="sales_rep",
        )
    )

    admin = UserContext(user_id="adm-1", role="admin")
    owner = UserContext(user_id="rep-1", role="sales_rep")
    rep2 = UserContext(user_id="rep-2", role="sales_rep")

    async def load(user: UserContext, run_id: str = "run-code-1"):
        return await _load_code_run(run_id, "t-code", user)

    async def scenario() -> None:
        # Owner: always allowed while it is a code run.
        record, codeplane = await load(owner)
        assert record.run_id == "run-code-1"
        assert codeplane["workspace"]["workspace_id"] == "ws-1"

        # Reviewer with NO pending approval: 404 (existence not confirmed).
        for seat in (admin, UserContext(user_id="mgr-1", role="manager")):
            try:
                await load(seat)
            except HTTPException as exc:
                assert exc.status_code == 404, exc.detail
            else:  # pragma: no cover - would be the leak
                raise AssertionError(f"review seat leaked a run without approval: {seat}")

        # A peer with a pending approval is still nobody: the exception is
        # scoped to review seats, not to "anyone holding the approval id".
        await request_code_approval(
            get_policy_repository(),
            tenant_id="t-code",
            run_id="run-code-1",
            requester="rep-1",
            note="commit mathlib",
        )
        try:
            await load(rep2)
        except HTTPException as exc:
            assert exc.status_code == 404, exc.detail
        else:  # pragma: no cover - would be the leak
            raise AssertionError("a peer without a review seat saw someone else's run")

        # Reviewer WITH the pending approval: allowed on that run. On a run with
        # no approval they still 404 — the entitlement is per-run, not per-role.
        record, codeplane = await load(admin)
        assert record.run_id == "run-code-1"
        assert codeplane["workspace"]["workspace_id"] == "ws-1"
        try:
            await load(admin, "run-plain-1")
        except HTTPException as exc:
            assert exc.status_code == 404, exc.detail
        else:  # pragma: no cover
            raise AssertionError("a review seat read a run with no pending approval")

        # Owner of a non-code run: 409 as well, not 200 with an empty payload.
        # (Reachable only *after* access is granted, which is why the reviewer
        # above gets the 404 instead — the check order is the guarantee.)
        try:
            await load(owner, "run-plain-1")
        except HTTPException as exc:
            assert exc.status_code == 409, exc.detail
        else:  # pragma: no cover
            raise AssertionError("a non-code run must not be loaded as a code task")

        # After the decision the reviewer must still see the run: reopening the
        # decision they made is the audit trail (and what the approve flow reads
        # back to project ``codeplane.approval = approved``).
        from forgeflow.codeplane.approval import decide_code_approval

        await decide_code_approval(
            get_policy_repository(),
            tenant_id="t-code",
            run_id="run-code-1",
            decision="approve",
            approver="adm-1",
            role="admin",
        )
        record, _ = await load(admin)
        assert record.run_id == "run-code-1"

        # Cross-tenant is still a 404, and never resolved by role.
        try:
            await _load_code_run("run-code-1", "t-other", admin)
        except HTTPException as exc:
            assert exc.status_code == 404, exc.detail
        else:  # pragma: no cover
            raise AssertionError("cross-tenant read must 404")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# BIZ-02 — the streaming entry point persists exactly like the normal one       #
# --------------------------------------------------------------------------- #


def test_merge_patch_follows_the_state_reducers():
    from forgeflow.workflows.stream_state import merge_patch

    acc: dict = {"errors": ["first"], "current_stage": "qualify"}
    merge_patch(acc, {"errors": ["second"], "current_stage": "research", "messages": ["x"]})
    assert acc["errors"] == ["first", "second"]  # operator.add
    assert acc["current_stage"] == "research"  # last write wins
    assert "messages" not in acc  # withheld on purpose


class _FakeConn:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self._log = log

    async def execute(self, sql, *args):
        self._log.append((" ".join(str(sql).split()), args))
        return "INSERT 0 1"

    async def fetchval(self, sql, *args):
        self._log.append((" ".join(str(sql).split()), args))
        return 1

    async def fetchrow(self, sql, *args):
        self._log.append((" ".join(str(sql).split()), args))
        return None

    async def fetch(self, sql, *args):
        self._log.append((" ".join(str(sql).split()), args))
        return []


class _FakePool:
    """Records every statement so the test can assert what was persisted."""

    def __init__(self) -> None:
        self.log: list[tuple[str, tuple]] = []

    def acquire(self):
        pool = self

        class _Acm:
            async def __aenter__(self):
                return _FakeConn(pool.log)

            async def __aexit__(self, *_exc):
                return False

        return _Acm()


class _FakeGraph:
    def __init__(self, patches: list[dict]) -> None:
        self._patches = patches

    async def astream(self, _state, config=None):  # noqa: ARG002 — LangGraph signature
        for patch in self._patches:
            yield patch


@pytest.mark.parametrize("storage_failure", [False, True])
def test_streamed_workflow_persists_its_record_and_approval_request(monkeypatch, storage_failure):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from forgeflow.api.dependencies import (
        get_current_user,
        get_graphs,
        get_pool,
        get_workspace_id,
    )
    from forgeflow.api.hub_deps import resolve_tenant
    from forgeflow.api.routers.workflows import router as workflows_router
    from forgeflow.rbac.models import UserContext

    approval_token = str(uuid.uuid4())
    graph = _FakeGraph(
        [
            {"researcher": {"current_stage": "research", "research_results": [{"a": 1}]}},
            {
                "executor": {
                    # The graph suspended for a human — exactly the case the
                    # stream used to drop on the floor.
                    "current_stage": "approve",
                    "approval_token": approval_token,
                    "proposal": {"company": "ACME"},
                    "total_tokens": 123,
                    "total_cost_usd": 0.01,
                }
            },
        ]
    )
    pool = _FakePool()
    if storage_failure:
        from unittest.mock import AsyncMock

        from fastapi import HTTPException
        monkeypatch.setattr(
            "forgeflow.api.routers.workflows._persist_workflow_outcome",
            AsyncMock(side_effect=HTTPException(status_code=503, detail="storage down")),
        )

    app = FastAPI()
    app.include_router(workflows_router, prefix="/workflows")
    app.dependency_overrides[resolve_tenant] = lambda: "t-biz02"
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="rep-1", role="admin")
    app.dependency_overrides[get_workspace_id] = lambda: None
    app.dependency_overrides[get_graphs] = lambda: {"sales_ops": graph}
    app.dependency_overrides[get_pool] = lambda: pool

    response = TestClient(app).post(
        "/workflows/stream",
        json={
            "workflow_type": "sales_ops",
            "lead_data": {"company_name": "ACME"},
            "dry_run": True,  # skips the Slack side effect, not the persistence
        },
    )

    assert response.status_code == 200
    events = [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ") and "[DONE]" not in line
    ]
    kinds = [event.get("event") for event in events]

    if storage_failure:
        assert "workflow_persist_failed" in kinds
        assert "workflow_complete" not in kinds
        return

    assert kinds.index("workflow_persisted") < kinds.index("workflow_complete")
    assert "workflow_complete" in kinds
    assert "workflow_persisted" in kinds, f"stream did not persist: {kinds}"
    assert "workflow_persist_failed" not in kinds

    statements = [sql for sql, _args in pool.log]
    assert any(sql.startswith("INSERT INTO workflow_runs") for sql in statements), statements
    assert any(sql.startswith("INSERT INTO approval_requests") for sql in statements), statements


# --------------------------------------------------------------------------- #
# BIZ-03 — completion is a program fact, not a model opinion                    #
# --------------------------------------------------------------------------- #


def test_finish_early_in_the_flow_is_downgraded():
    from forgeflow.agents.supervisor import gate_routing

    state = {"current_stage": "research", "research_results": [], "analysis_scores": []}
    worker, override = gate_routing("FINISH", "research", state)
    # After research the documented worker is the analyzer, not FINISH.
    assert worker == "analyzer"
    assert override == "finish_rejected_incomplete"


def test_finish_is_allowed_for_a_disqualified_lead():
    from forgeflow.agents.supervisor import workflow_is_complete

    assert workflow_is_complete("analyze", {"analysis_scores": [{"score": 2.1}]}) is True
    assert workflow_is_complete("analyze", {"analysis_scores": [{"score": 7.5}]}) is False
    assert workflow_is_complete("analyze", {"analysis_scores": []}) is False


def test_finish_is_allowed_after_a_rejected_approval():
    from forgeflow.agents.supervisor import workflow_is_complete

    assert workflow_is_complete("approve", {"approval_status": "rejected"}) is True
    assert workflow_is_complete("approve", {"approval_status": "pending"}) is False
    assert workflow_is_complete("approve", {"approval_status": "approved"}) is False


@pytest.mark.asyncio
async def test_supervisor_finish_is_downgraded_when_the_flow_is_incomplete():
    from forgeflow.agents.supervisor import SupervisorAgent

    agent = SupervisorAgent(_stub_model(next="FINISH", reasoning="looks done"))
    state = {
        "current_stage": "research",
        "messages": [],
        "research_results": [],
        "analysis_scores": [],
        "errors": [],
        "lead_data": {"company_name": "ACME"},
    }

    patch = await agent.run(state)

    assert patch["next_agent"] == "analyzer"
    # The downgraded worker decides the stage: the analyzer's own stage.
    assert patch["current_stage"] == "analyze"


# --------------------------------------------------------------------------- #
# BIZ-04 — deletion is a tombstone, not a hint                                  #
# --------------------------------------------------------------------------- #


def test_deleted_session_does_not_resurrect_when_the_run_finishes():
    from forgeflow.workspace.models import WorkspaceRunRecord
    from forgeflow.workspace.store import MemoryWorkspaceStore

    store = MemoryWorkspaceStore()
    tenant, session = "t-biz04", "s-biz04"
    live = WorkspaceRunRecord(
        run_id="run-1", tenant_id=tenant, session_id=session, actor_user_id="u1"
    )
    asyncio.run(store.save(live))
    assert asyncio.run(store.soft_delete_session(tenant, session)) == 1

    # The run finishes *after* the delete: the completion path rebuilds the
    # record from scratch, so deleted_at is None again.
    finished = WorkspaceRunRecord(
        run_id="run-1", tenant_id=tenant, session_id=session, actor_user_id="u1"
    )
    finished.status = "completed"
    finished.outcome = "success"
    asyncio.run(store.save(finished))

    assert asyncio.run(store.get(tenant, "run-1")) is None
    assert asyncio.run(store.list_recent_for_tenant(tenant)) == []
    assert asyncio.run(store.list_sessions(tenant)) == []


# --------------------------------------------------------------------------- #
# BIZ-05 — a failed save must be visible on the record                          #
# --------------------------------------------------------------------------- #


def _run_record(run_id: str = "run-biz05"):
    from forgeflow.runtime.orchestrator import RunRecord

    return RunRecord(
        run_id=run_id,
        thread_id="thread-biz05",
        tenant_id="t-biz05",
        agent_id="platform",
        intent="写一份周报",
        status="completed",
        outcome="success",
        steps=[],
        errors=[],
        created_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_persistence_failure_is_written_onto_the_run(monkeypatch):
    import forgeflow.workspace.store as workspace_store
    from forgeflow.runtime.orchestrator import persist_workspace_record

    class _BrokenStore:
        async def save(self, _record):
            raise ConnectionError("database went away")

    monkeypatch.setattr(workspace_store, "get_workspace_store", lambda: _BrokenStore())
    record = _run_record()

    saved = await persist_workspace_record(record)

    assert saved is False
    assert record.persistence["status"] == "failed"
    assert "ConnectionError" in record.persistence["error"]


@pytest.mark.asyncio
async def test_persistence_success_is_recorded_too(monkeypatch):
    import forgeflow.workspace.store as workspace_store
    from forgeflow.runtime.orchestrator import persist_workspace_record

    stored = []

    class _OkStore:
        async def save(self, record):
            stored.append(record)
            return record

    monkeypatch.setattr(workspace_store, "get_workspace_store", lambda: _OkStore())
    record = _run_record("run-biz05-ok")

    assert await persist_workspace_record(record) is True
    assert record.persistence["status"] == "saved"
    assert [r.run_id for r in stored] == ["run-biz05-ok"]


@pytest.mark.asyncio
async def test_persistence_status_does_not_leak_into_llm_provenance(monkeypatch):
    """``llm`` must keep meaning "a model ran", not "the row was written".

    The deterministic run detail asserts an exact key set for ``llm``
    (``test_runtime_llm_agent.py``) precisely so an offline run cannot look like
    a model-driven one. Persistence is a different fact and has its own channel.
    """
    import forgeflow.workspace.store as workspace_store
    from forgeflow.runtime.orchestrator import persist_workspace_record

    class _OkStore:
        async def save(self, record):
            return record

    monkeypatch.setattr(workspace_store, "get_workspace_store", lambda: _OkStore())
    record = _run_record("run-biz05-channel")
    record.llm = {"execution_budget": {"model_calls": 0}}

    await persist_workspace_record(record)

    assert set(record.llm) == {"execution_budget"}
    assert record.persistence["status"] == "saved"


# --------------------------------------------------------------------------- #
# BIZ-06 — the user predicate belongs before the LIMIT                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_hydration_window_is_per_user_not_per_tenant():
    from forgeflow.runtime.dispatcher import (
        hydrate_run_store,
        reset_hydrated_tenants,
    )
    from forgeflow.runtime.orchestrator import get_run_store, reset_run_store
    from forgeflow.workspace.models import WorkspaceRunRecord
    from forgeflow.workspace.store import get_workspace_store, reset_workspace_store

    reset_workspace_store()
    reset_run_store()
    reset_hydrated_tenants()

    store = get_workspace_store()
    # A colleague with newer history fills the whole tenant window...
    for index in range(5):
        await store.save(
            WorkspaceRunRecord(
                run_id=f"colleague-{index}",
                tenant_id="t-biz06",
                session_id=f"sc-{index}",
                actor_user_id="colleague",
                status="completed",
                created_at=f"2026-02-0{index + 1}T00:00:00+00:00",
            )
        )
    # ...while my one, much older run must still be reachable.
    await store.save(
        WorkspaceRunRecord(
            run_id="mine-1",
            tenant_id="t-biz06",
            session_id="sm-1",
            actor_user_id="me",
            status="completed",
            created_at="2026-01-01T00:00:00+00:00",
        )
    )

    hydrated = await hydrate_run_store("t-biz06", 3, actor_user_id="me")

    assert hydrated == 1
    run_store = get_run_store()
    assert [r.run_id for r in run_store.list("t-biz06", actor_user_id="me")] == ["mine-1"]
    assert run_store.count("t-biz06", actor_user_id="me") == 1


@pytest.mark.asyncio
async def test_hydration_is_stamped_per_user():
    from forgeflow.runtime.dispatcher import (
        ensure_tenant_history,
        reset_hydrated_tenants,
    )
    from forgeflow.runtime.orchestrator import get_run_store, reset_run_store
    from forgeflow.workspace.models import WorkspaceRunRecord
    from forgeflow.workspace.store import get_workspace_store, reset_workspace_store

    reset_workspace_store()
    reset_run_store()
    reset_hydrated_tenants()

    store = get_workspace_store()
    for user in ("alice", "bob"):
        await store.save(
            WorkspaceRunRecord(
                run_id=f"{user}-run",
                tenant_id="t-biz06b",
                session_id=f"s-{user}",
                actor_user_id=user,
                status="completed",
                created_at="2026-03-01T00:00:00+00:00",
            )
        )

    await ensure_tenant_history("t-biz06b", actor_user_id="alice")
    # Alice's backfill must not satisfy Bob's read.
    await ensure_tenant_history("t-biz06b", actor_user_id="bob")

    run_store = get_run_store()
    assert [r.run_id for r in run_store.list("t-biz06b", actor_user_id="bob")] == ["bob-run"]


# --------------------------------------------------------------------------- #
# BIZ-07 — "latest evaluation" is the last one written                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_latest_evaluation_on_a_timestamp_tie_is_the_last_written():
    from forgeflow.repositories.memory.skill_repo import MemorySkillCandidateRepository
    from forgeflow.skills.models import SkillEvaluationRecord

    repo = MemorySkillCandidateRepository()
    same_tick = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)

    first = SkillEvaluationRecord(
        id="eval-first",
        tenant_id="t-biz07",
        target_id="skill-1",
        metrics={"evaluation_kind": "run", "completed_cases": 1, "total_cases": 3},
        verdict="fail",
        created_at=same_tick,
    )
    resumed = SkillEvaluationRecord(
        id="eval-resumed",
        tenant_id="t-biz07",
        target_id="skill-1",
        metrics={"evaluation_kind": "run", "completed_cases": 3, "total_cases": 3},
        verdict="pass",
        created_at=same_tick,
    )
    await repo.save_evaluation(first)
    await repo.save_evaluation(resumed)

    latest = await repo.get_evaluation_for("t-biz07", "skill-1", kind="run")

    assert latest is not None
    assert latest.id == "eval-resumed"
    assert latest.metrics["completed_cases"] == 3


# --------------------------------------------------------------------------- #
# BIZ-08 — lifecycle order is append order, never a random id                   #
# --------------------------------------------------------------------------- #


def test_lifecycle_events_with_the_same_tick_keep_append_order():
    from forgeflow.lifecycle.store import InMemoryLifecycleStore, LifecycleEvent

    store = InMemoryLifecycleStore()
    same_tick = datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)
    flow = [("draft", "candidate"), ("candidate", "published"), ("published", "deprecated")]
    for from_state, to_state in flow:
        store.append_event(
            LifecycleEvent(
                tenant_id="t-biz08",
                skill_id="skill-1",
                from_state=from_state,
                to_state=to_state,
                created_at=same_tick,
            )
        )

    latest = store.latest_event("t-biz08", "skill-1")

    assert latest is not None
    assert latest.to_state == "deprecated"
    assert [e.to_state for e in store.list_events("t-biz08", "skill-1")] == [
        "candidate",
        "published",
        "deprecated",
    ]
    assert [e.seq for e in store.list_events("t-biz08", "skill-1")] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# BIZ-09 — the retry budget measures the whole task, not the retry loop         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retry_budget_includes_the_first_attempt(monkeypatch):
    import forgeflow.runtime.orchestrator as orch
    from forgeflow.config import get_settings

    # A ceiling that the *first* attempt alone already exceeds.
    monkeypatch.setattr(get_settings(), "max_run_seconds", 0.25)

    original = orch._default_executor

    async def slow_failing_executor(task, *args, **kwargs):
        await asyncio.sleep(0.4)
        return await original(task, *args, **kwargs)

    monkeypatch.setattr(orch, "_default_executor", slow_failing_executor)

    handle = await orch.run_task(
        orch.TaskCreate(intent="分析华东销售数据", context={"simulate_failure": True}),
        orch.RequestContext(tenant_id="t-biz09", user_id="admin-1", role="admin"),
    )

    assert handle.status == "failed"
    loop = handle.detail["loop"]
    assert loop["tripped"] is True, "the first attempt's 0.4s was excluded from the budget"
    assert loop["breaches"][0]["dimension"] == "seconds"
    # The breaker vetoes the retry the count ceiling had authorised.
    assert handle.detail["replans"] == 0


# --------------------------------------------------------------------------- #
# BIZ-10 — a recovered MCP server must reach the compiled graphs                #
# --------------------------------------------------------------------------- #


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.asyncio
async def test_graphs_are_rebuilt_only_when_the_tool_set_changes():
    from forgeflow.graph.refresh import rebuild_graphs_if_changed

    state = SimpleNamespace(mcp_tools=[], graphs={}, graph=None)
    compiles: list[tuple[int, str]] = []

    async def fake_compile(*, mcp_tools, workflow_type):
        compiles.append((len(mcp_tools), workflow_type))
        return f"graph::{workflow_type}::{len(mcp_tools)}"

    # Empty → empty is not a change; nothing recompiles.
    assert await rebuild_graphs_if_changed(state, [], fake_compile) is False
    assert compiles == []
    assert state.graphs == {}

    # MCP came up: a genuinely different name set forces the rebuild.
    tools = [_Tool("crm.search"), _Tool("email.send")]
    assert await rebuild_graphs_if_changed(state, tools, fake_compile) is True
    assert sorted(workflow_type for _n, workflow_type in compiles) == [
        "finance_recon",
        "sales_ops",
        "support_ops",
    ]
    assert state.graph == "graph::sales_ops::2"
    assert state.graphs["support_ops"] == "graph::support_ops::2"
    assert [t.name for t in state.mcp_tools] == ["crm.search", "email.send"]

    # Same names discovered again: no churn.
    compiles.clear()
    assert (
        await rebuild_graphs_if_changed(
            state, [_Tool("crm.search"), _Tool("email.send")], fake_compile
        )
        is False
    )
    assert compiles == []


def test_tool_names_ignore_unnameable_entries():
    from forgeflow.graph.refresh import tool_names

    assert tool_names([_Tool("a"), _Tool(""), SimpleNamespace()]) == {"a"}
    assert tool_names(None) == set()


# --------------------------------------------------------------------------- #
# BIZ-11 — "registered but never started" must be re-dispatchable              #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _clean_runtime():
    from forgeflow.runtime.dispatcher import reset_run_dispatcher
    from forgeflow.runtime.events import reset_event_bus
    from forgeflow.runtime.orchestrator import reset_run_store
    from forgeflow.workspace.store import reset_workspace_store

    reset_run_store()
    reset_run_dispatcher()
    reset_event_bus()
    reset_workspace_store()
    yield
    reset_run_store()
    reset_run_dispatcher()
    reset_event_bus()
    reset_workspace_store()


@pytest.mark.asyncio
async def test_idempotent_run_registered_but_never_started_is_redispatched(
    _clean_runtime, monkeypatch
):
    import forgeflow.runtime.orchestrator as orch
    from forgeflow.runtime.dispatcher import get_run_dispatcher
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate

    started = asyncio.Event()

    async def fake_run_task(task, ctx, run_id=None, thread_id=None, **kwargs):  # noqa: ARG001
        started.set()
        await asyncio.sleep(30)  # cancelled by the test

    monkeypatch.setattr(orch, "run_task", fake_run_task)

    dispatcher = get_run_dispatcher()
    ctx = RequestContext(tenant_id="t-biz11", user_id="u-biz11", role="admin")
    from forgeflow.runtime.dispatcher import request_run_id
    from forgeflow.workspace.models import WorkspaceRunRecord
    from forgeflow.workspace.store import get_workspace_store
    run_id = request_run_id(ctx.tenant_id, ctx.user_id, "key-biz11")
    await get_workspace_store().claim(WorkspaceRunRecord(
        run_id=run_id, tenant_id=ctx.tenant_id, session_id=run_id,
        actor_user_id=ctx.user_id, request_digest="same-digest",
        detail={"dispatch_started": False},
    ))
    replay = await dispatcher.dispatch(
        TaskCreate(intent="同一件事"), ctx, request_key="key-biz11",
        request_digest="same-digest",
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert replay.run_id == run_id
        assert replay.run_id in dispatcher._tasks
        header = await get_workspace_store().get(ctx.tenant_id, run_id)
        assert header.detail["dispatch_started"] is True
    finally:
        await dispatcher.abort(ctx.tenant_id, run_id)


def test_approved_finish_must_execute_first():
    from forgeflow.agents.supervisor import gate_routing
    assert gate_routing("FINISH", "approve", {"approval_status": "approved"})[0] == "executor"


@pytest.mark.asyncio
async def test_workspace_retry_dispatches_a_stranded_request(_clean_runtime, monkeypatch):
    from unittest.mock import AsyncMock

    from forgeflow.api.hub_schemas import WorkspaceTaskCreateRequest
    from forgeflow.api.routers import workspace
    from forgeflow.rbac.models import UserContext
    from forgeflow.runtime.dispatcher import get_run_dispatcher, request_run_id
    from forgeflow.runtime.orchestrator import RunHandle
    from forgeflow.workspace.models import WorkspaceRunRecord
    from forgeflow.workspace.store import get_workspace_store

    request = WorkspaceTaskCreateRequest(intent="resume stranded task", request_key="stranded-api")
    user = UserContext(user_id="owner", role="admin")
    tenant = "stranded-api-tenant"
    run_id = request_run_id(tenant, user.user_id, request.request_key)
    await get_workspace_store().claim(WorkspaceRunRecord(
        run_id=run_id, tenant_id=tenant, session_id=run_id,
        actor_user_id=user.user_id, status="interrupted",
        detail={"dispatch_started": False},
        request_digest=workspace._request_digest(request),
    ))
    monkeypatch.setattr(workspace, "evaluate_task_entry", AsyncMock(
        return_value=SimpleNamespace(allowed=True, requires_approval=False)))
    monkeypatch.setattr(workspace, "_admission_guard", lambda *_: None)
    dispatcher = get_run_dispatcher()
    dispatch = AsyncMock(return_value=RunHandle(
        run_id=run_id, thread_id=run_id, status="running", session_id=run_id))
    monkeypatch.setattr(dispatcher, "dispatch", dispatch)
    response = await workspace.create_workspace_task(request, user, tenant)
    assert response.status == "running"
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["request_key"] == request.request_key


@pytest.mark.parametrize("status, detail", [
    ("awaiting_approval", {"dispatch_started": False}),
    ("interrupted", {"dispatch_started": True}),
    ("interrupted", {}),
])
def test_retry_never_restarts_started_or_unverified_work(status, detail):
    from forgeflow.runtime.dispatcher import RunDispatcher
    prior = SimpleNamespace(status=status, detail=detail)
    assert not RunDispatcher()._can_redispatch("r", prior)
