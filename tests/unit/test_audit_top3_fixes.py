"""Audit Top 3 fixes (2026-10-29): external agent-server fail-closed,
``/workspaces`` admin-only, and ``/workspace/sessions`` total.

These tests pin the three behaviours, not the whole audit: the other seven
audit items are documented in the audit report and deliberately not changed
here (several are doc/wording fixes or defence-in-depth, and #2's tenant
filter is impossible without a schema migration — see workspaces router).
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from forgeflow.api.routers.workspaces import _require_admin
from forgeflow.codeplane.engine import AgentServerOpenHandsEngine, _AgentServerUnavailable
from forgeflow.rbac.models import UserContext
from forgeflow.workspace.models import WorkspaceRunRecord
from forgeflow.workspace.store import MemoryWorkspaceStore, clear_workspace_store


def _engine(url: str, token: str) -> AgentServerOpenHandsEngine:
    settings = SimpleNamespace(
        codeplane_agent_server_url=url,
        codeplane_agent_server_token=token,
        codeplane_enabled=True,
    )
    return AgentServerOpenHandsEngine(settings=settings)


def test_external_agent_server_without_token_is_unavailable():
    # Non-loopback only: a loopback URL with an empty token is local-only
    # (the on-demand launch and the A5 in-memory-transport tests rely on it),
    # so the fail-closed gate must not fire there. Use a TEST-NET address:
    # the gate fires before any health probe, so nothing is contacted.
    engine = _engine("http://203.0.113.10:8000", "")
    assert engine.available() is False
    assert "token" in engine.reason.lower() or "TOKEN" in engine.reason
    with pytest.raises(_AgentServerUnavailable):
        asyncio.run(engine._ensure_server())


def test_loopback_agent_server_without_token_is_not_token_gated():
    engine = _engine("http://127.0.0.1:9", "")
    engine._health_get = lambda base, token: (True, "")  # noqa: ARG001
    assert engine.available() is True
    base, token = asyncio.run(engine._ensure_server())
    assert base == "http://127.0.0.1:9" and token == ""


def test_external_agent_server_with_token_reaches_health_check():
    engine = _engine("http://127.0.0.1:9", "secret")
    engine._health_get = lambda base, token: (True, "")  # noqa: ARG001
    assert engine.available() is True
    base, token = asyncio.run(engine._ensure_server())
    assert base == "http://127.0.0.1:9" and token == "secret"


def test_workspaces_list_is_admin_only():
    with pytest.raises(HTTPException) as exc:
        _require_admin(UserContext(user_id="m1", role="manager"))
    assert exc.value.status_code == 403
    _require_admin(UserContext(user_id="a1", role="admin"))  # no raise


def _record(run_id: str, session_id: str, actor: str = "u1") -> WorkspaceRunRecord:
    return WorkspaceRunRecord(
        run_id=run_id, tenant_id="t1", session_id=session_id, actor_user_id=actor
    )


def test_sessions_total_is_not_page_size():
    clear_workspace_store()
    store = MemoryWorkspaceStore()
    for i in range(25):
        asyncio.run(store.save(_record(f"r{i}", f"s{i}")))
    page = asyncio.run(store.list_sessions("t1", limit=20, actor_user_id="u1"))
    assert len(page) == 20
    assert asyncio.run(store.count_sessions("t1", actor_user_id="u1")) == 25
    # Actor filter applies to the count exactly as it does to the page.
    assert asyncio.run(store.count_sessions("t1", actor_user_id="other")) == 0
