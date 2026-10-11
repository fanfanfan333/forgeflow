"""INC54 — **独立** QA 验证：技能生命周期（退役 / 恢复 / 删除草稿）。

Owned by QA (严过关). This file is **purely additive** — it does not touch
``tests/unit/test_inc54_skill_lifecycle.py`` (the engineer's own pins), any
production code, or any shared fixture. It attacks the INC54 claims through the
**real** router behind the **real** ``RBACMiddleware`` on the memory backend.

It is deliberately built to be *falsifiable* (item D):

* A — retire ⇒ the persisted ``status`` really becomes ``retired`` (read back
  through the repository, not merely echoed in the response); restore ⇒ back to
  ``published``. The list endpoint is checked too.
* B — ``DELETE /skills/{id}``: draft ⇒ 204 and the id is unreadable afterwards;
  published ⇒ 409 (and survives); unknown ⇒ 404.
* C — tenant isolation: a second tenant's token cannot retire/delete/read the
  first tenant's skill — every attempt is an honest 404, and the owner's row is
  provably unmodified.
* D — counterfactual proof: the A / B assertions are shown to be **non-vacuous**
  by poisoning the code path (retire writes ``published``; delete no-ops while
  returning ``True``) and confirming the same assertion goes RED, then restoring
  the real path.
* E — history replay is not re-execution: a completed run's ``steps`` /
  ``tool_invocations`` / ``artifacts`` counts stay frozen across repeated
  ``GET /runs/{id}`` + artifact downloads, while the real tool choke point
  (``ToolExecutor.execute``) and ``_record_invocation`` are instrumented and
  must record **zero** calls.
* F — frontend static pin (bonus): the INC54 test-ids / ``runOptionLabel`` exist.

Run::

    python -m pytest tests/qa_independent/test_qa_inc54_independent.py \\
        -p no:cacheprovider --basetemp=D:/Temp/pytest-qa-inc54 -q
"""

from __future__ import annotations
from tests.business_auth import authenticate_business_app

import hashlib
import uuid

import httpx
import pytest
from fastapi import FastAPI

from forgeflow.api.routers import runs as runs_router
from forgeflow.api.routers import skills as skills_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.repositories.memory.skill_repo import (
    MemorySkillRepository,
    clear_skill_store,
)
from forgeflow.runtime.orchestrator import (
    RequestContext,
    TaskCreate,
    get_run_store,
    reset_run_store,
    run_task,
)
from forgeflow.runtime.tool_executor import ToolExecutor
from forgeflow.skills.registry import SkillRegistry

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# App / fixtures — same shape the sibling qa_independent files use             #
# --------------------------------------------------------------------------- #
def _build_app() -> FastAPI:
    """Minimal app: the real skills + runs routers behind the real RBAC MW."""
    app = FastAPI()
    authenticate_business_app(app)
    app.add_middleware(RBACMiddleware)
    app.include_router(skills_router.router, prefix="/skills")
    app.include_router(skills_router.candidates_router, prefix="/skill-candidates")
    app.include_router(runs_router.router, prefix="/runs")
    return app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=_build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate(force_memory_backend):
    """Clean skill + run stores and the run-hydration idempotency set."""
    from forgeflow.runtime.dispatcher import reset_run_dispatcher

    clear_skill_store()
    reset_run_store()
    reset_run_dispatcher()
    yield
    clear_skill_store()
    reset_run_store()
    reset_run_dispatcher()


def _tenant() -> str:
    return f"t-qa54-{uuid.uuid4().hex[:8]}"


def _headers(tenant: str, role: str = "manager") -> dict[str, str]:
    token = create_access_token(user_id=f"{role}-1", role=role, workspace_id=tenant)
    return {"Authorization": f"Bearer {token}"}


async def _create_draft(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post(
        "/skills",
        json={"name": f"skill-{uuid.uuid4().hex[:8]}", "domain": "general"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _publish(tenant: str, skill_id: str) -> None:
    updated = await SkillRegistry().set_status(tenant, skill_id, "published")
    assert updated is not None and updated.status == "published"


async def _read(tenant: str, skill_id: str):
    return await SkillRegistry().get(tenant, skill_id)


async def _readable(client: httpx.AsyncClient, skill_id: str, headers) -> int:
    """HTTP status of the single-skill read route (200 present / 404 gone)."""
    return (await client.get(f"/skills/{skill_id}/export", headers=headers)).status_code


# ========================================================================== #
# A — lifecycle truth (persisted status, not just the HTTP code)             #
# ========================================================================== #
async def test_A_retire_then_restore_status_is_persisted(client):
    tenant, headers = _tenant(), None
    headers = _headers(tenant)
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)

    # -- retire: published -> retired, in the RESPONSE *and* the store --------
    r = await client.post(f"/skills/{skill_id}/retire", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "retired", r.text
    stored = await _read(tenant, skill_id)
    assert stored is not None and stored.status == "retired"

    # list endpoint agrees (not a response-only artefact)
    listing = await client.get("/skills", headers=headers)
    assert listing.status_code == 200, listing.text
    row = next(i for i in listing.json()["items"] if i["id"] == skill_id)
    assert row["status"] == "retired", row

    # -- restore: retired -> published ---------------------------------------
    r2 = await client.post(f"/skills/{skill_id}/restore", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "published", r2.text
    stored2 = await _read(tenant, skill_id)
    assert stored2 is not None and stored2.status == "published"


# ========================================================================== #
# B — delete boundary                                                        #
# ========================================================================== #
async def test_B_delete_draft_204_then_unreadable(client):
    tenant = _tenant()
    headers = _headers(tenant)
    skill_id = await _create_draft(client, headers)
    assert await _readable(client, skill_id, headers) == 200  # present before

    r = await client.delete(f"/skills/{skill_id}", headers=headers)
    assert r.status_code == 204, r.text
    assert r.content == b""  # 204 carries no body

    assert await _read(tenant, skill_id) is None
    assert await _readable(client, skill_id, headers) == 404  # gone


async def test_B_delete_published_409_and_survives(client):
    tenant = _tenant()
    headers = _headers(tenant)
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)

    r = await client.delete(f"/skills/{skill_id}", headers=headers)
    assert r.status_code == 409, r.text
    body = r.json()
    assert "退役" in body["detail"], body

    # refused ⇒ the skill is really still there (not a silent delete)
    assert await _read(tenant, skill_id) is not None
    assert await _readable(client, skill_id, headers) == 200


async def test_B_delete_unknown_404(client):
    _tenant_id = _tenant()
    headers = _headers(_tenant_id)
    r = await client.delete("/skills/does-not-exist", headers=headers)
    assert r.status_code == 404, r.text


# ========================================================================== #
# C — tenant isolation (fail-closed, no leak)                                #
# ========================================================================== #
async def test_C_cross_tenant_retire_delete_and_read_are_404(client):
    owner = _tenant()
    owner_headers = _headers(owner)
    intruder = _tenant()
    intruder_headers = _headers(intruder)

    # published skill owned by `owner`
    published_id = await _create_draft(client, owner_headers)
    await _publish(owner, published_id)

    # draft skill owned by `owner`
    draft_id = await _create_draft(client, owner_headers)

    # 1) intruder cannot even READ the owner's skills
    assert await _readable(client, published_id, intruder_headers) == 404
    assert await _readable(client, draft_id, intruder_headers) == 404

    # 2) intruder cannot RETIRE the owner's published skill
    r = await client.post(f"/skills/{published_id}/retire", headers=intruder_headers)
    assert r.status_code == 404, r.text
    assert (await _read(owner, published_id)).status == "published"  # untouched

    # 3) intruder cannot DELETE the owner's draft skill
    r2 = await client.delete(f"/skills/{draft_id}", headers=intruder_headers)
    assert r2.status_code == 404, r2.text
    survivor = await _read(owner, draft_id)
    assert survivor is not None and survivor.status == "draft"  # untouched

    # positive control: the owner still can (so the 404s above aren't a dead app)
    assert await _readable(client, draft_id, owner_headers) == 200


# ========================================================================== #
# D — counterfactual: prove A / B assertions are NOT vacuously true          #
# ========================================================================== #
async def test_D_counterfactual_A_status_assertion_has_teeth(client):
    """If retire silently failed to change status, the A assertion goes RED."""
    tenant = _tenant()
    headers = _headers(tenant)
    skill_id = await _create_draft(client, headers)
    await _publish(tenant, skill_id)

    real = SkillRegistry.set_status

    async def _poisoned(self, tenant_id, sid, status):  # noqa: ANN001
        # Ignore the requested status and keep writing "published".
        return await real(self, tenant_id, sid, "published")

    SkillRegistry.set_status = _poisoned
    try:
        r = await client.post(f"/skills/{skill_id}/retire", headers=headers)
        # A naive HTTP-only check is vacuous — it still passes under the poison:
        assert r.status_code == 200
        # The poison really took effect (wrong value stored):
        assert r.json()["status"] == "published"
        stored = await _read(tenant, skill_id)
        assert stored.status == "published"
        # …therefore the REAL A assertion is falsifiable — it fails here:
        with pytest.raises(AssertionError) as excinfo:
            assert r.json()["status"] == "retired"
        print(
            f"[COUNTERFACTUAL-A] expected 'retired' but status is "
            f"{r.json()['status']!r} -> AssertionError: {excinfo.value}"
        )
    finally:
        SkillRegistry.set_status = real

    # control: with the real code path restored, retire DOES yield "retired".
    r3 = await client.post(f"/skills/{skill_id}/retire", headers=headers)
    assert r3.status_code == 200 and r3.json()["status"] == "retired", r3.text


async def test_D_counterfactual_B_delete_assertion_has_teeth(client):
    """If delete no-ops while returning True, the B 'gone afterwards' pin is RED."""
    tenant = _tenant()
    headers = _headers(tenant)
    skill_id = await _create_draft(client, headers)

    real = MemorySkillRepository.delete_skill

    async def _noop(self, tenant_id, sid):  # noqa: ANN001
        return True  # claim success, delete nothing

    MemorySkillRepository.delete_skill = _noop
    try:
        r = await client.delete(f"/skills/{skill_id}", headers=headers)
        assert r.status_code == 204  # naive check still passes …
        # … but the REAL B assertion (id unreadable afterwards) goes RED:
        assert await _read(tenant, skill_id) is not None
        with pytest.raises(AssertionError) as excinfo:
            assert await _readable(client, skill_id, headers) == 404
        print(
            f"[COUNTERFACTUAL-B] delete claimed 204 but id still readable "
            f"(GET export == 200) -> AssertionError: {excinfo.value}"
        )
    finally:
        MemorySkillRepository.delete_skill = real

    # control: the real path really removes the row.
    r2 = await client.delete(f"/skills/{skill_id}", headers=headers)
    assert r2.status_code == 204
    assert await _read(tenant, skill_id) is None


# ========================================================================== #
# E — history replay ≠ re-execution                                          #
# ========================================================================== #
class _RecordingBus:
    """Minimal bus stub — ``run_task`` only calls ``emit``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, run_id: str, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


async def test_E_replay_does_not_reexecute(client, monkeypatch):
    tenant = _tenant()
    headers = _headers(tenant)

    ctx = RequestContext(tenant_id=tenant, user_id="qa-e", role="manager")
    handle = await run_task(
        TaskCreate(intent="为 Acme 整理一份销售线索分析摘要", workflow_type="generic"),
        ctx,
        bus=_RecordingBus(),
    )
    assert handle.status == "completed", handle.status

    # Snapshot the frozen history through the REAL route.
    first = (await client.get(f"/runs/{handle.run_id}", headers=headers)).json()
    steps0 = len(first["steps"])
    tools0 = len(first["tool_invocations"])
    arts0 = len(first["artifacts"])
    assert arts0 >= 1, "a completed generic run should have produced an artifact"
    snap = {a["id"]: a.get("content") for a in first["artifacts"]}

    # Instrument the real execution choke points: any re-execution trips these.
    calls = {"tool": 0, "invocation": 0, "run_task": 0}

    real_exec = ToolExecutor.execute

    async def _count_exec(self, *a, **k):  # noqa: ANN001
        calls["tool"] += 1
        return await real_exec(self, *a, **k)

    monkeypatch.setattr(ToolExecutor, "execute", _count_exec)

    import forgeflow.runtime.orchestrator as orch

    real_rec = orch._record_invocation

    def _count_rec(*a, **k):  # noqa: ANN001
        calls["invocation"] += 1
        return real_rec(*a, **k)

    monkeypatch.setattr(orch, "_record_invocation", _count_rec)

    async def _count_run(*a, **k):  # noqa: ANN001
        calls["run_task"] += 1
        return await real_run_task(*a, **k)

    real_run_task = orch.run_task
    monkeypatch.setattr(orch, "run_task", _count_run)

    # Repeated reads + artifact downloads — the "replay" surface.
    for _ in range(4):
        body = (await client.get(f"/runs/{handle.run_id}", headers=headers)).json()
        assert len(body["steps"]) == steps0, "replay grew the step list"
        assert len(body["tool_invocations"]) == tools0, "replay grew invocations"
        assert len(body["artifacts"]) == arts0, "replay grew artifacts"
        for art in body["artifacts"]:
            dl = await client.get(
                f"/runs/{handle.run_id}/artifacts/{art['id']}", headers=headers
            )
            assert dl.status_code == 200, dl.text
            if snap.get(art["id"]) is not None:
                assert dl.text == snap[art["id"]] or hashlib.sha256(
                    dl.content
                ).hexdigest() == hashlib.sha256(
                    str(snap[art["id"]]).encode("utf-8")
                ).hexdigest()

    # No executor / tool / recorder fired during any read.
    assert calls == {"tool": 0, "invocation": 0, "run_task": 0}, calls

    # And the store itself is unchanged (same object identity + counts).
    rec = get_run_store().get(handle.run_id)
    assert len(rec.steps) == steps0
    assert len(getattr(rec, "tool_invocations", []) or []) == tools0
    assert len(getattr(rec, "artifacts", []) or []) == arts0


# ========================================================================== #
# F — frontend static pins (bonus; file-level, no browser required)          #
# ========================================================================== #
async def test_F_frontend_inc54_testids_present():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "frontend" / "src" / "views"
    inspector = (root / "skills" / "SkillInspector.tsx").read_text(encoding="utf-8")
    for tid in ("skill-action-retire", "skill-action-restore", "skill-action-delete"):
        assert tid in inspector, tid

    graph = (root / "skills" / "ExecFlowGraph.tsx").read_text(encoding="utf-8")
    assert "runOptionLabel" in graph

    panel = (root / "runs" / "InlineSessionPanel.tsx").read_text(encoding="utf-8")
    # the inline history panel must reference persisted steps / artifacts
    assert "steps" in panel and "artifact" in panel.lower()
