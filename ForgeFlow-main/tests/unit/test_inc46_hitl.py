"""INC46 T21 — the generic pause-for-human (HITL) primitive.

Scope (阳性 / 阴性 / 反事实 / 红线)
---------------------------------
* **阳性** — 触发 ``confirm_permission`` 的动作落为 ``waiting``；resolve 后
  ``resolved`` 且 ``run_continues``；低风险 WRITE 的租户自动批准**留审计记录**。
* **阴性** — ① 超时 ⇒ ``expired`` 且 resolve 被拒（fail-closed，绝不默认通过，
  红线 21）；② 跨租户 resolve ⇒ 403；③ DANGEROUS 无 admin 策略 ⇒ 直接
  ``deny``，**不产生 pending**；④ 重复 resolve ⇒ 幂等（返回同结果，不重复计数）；
  ⑤ 无租户 ⇒ 空集 / 拒绝写入。
* **反事实（真跑，登记）** —
  ``test_expired_action_is_fail_closed`` 是登记的红证目标：把
  ``PendingActionStore.resolve`` 里 ``if status == EXPIRED: raise
  PendingActionExpired`` 改成「过期即通过」（``return self._row_to_action(row)``）
  后本用例必须转红。
* **红线 4** — 未测量 / 未配置 ⇒ ``None`` / ``{}``，绝不写 ``0`` / ``""``。
* **红线 21** — EXTERNAL 必须确认，DANGEROUS 默认拒绝，超时一律 fail-closed。

Every test drives the real functions (never a re-implementation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from forgeflow.api.main import app as forgeflow_app
from forgeflow.api.routers import pending_actions as pending_router
from forgeflow.auth.jwt import create_access_token
from forgeflow.hitl import pending as P
from forgeflow.hitl import policy
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP

_TENANT = "t-inc46-t21"
_OTHER = "t-inc46-t21-other"
_NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _fresh_store():
    """Pin a fresh in-memory pending store per test (no DB, fully offline)."""
    store = P.InMemoryPendingActionStore()
    P.set_pending_store(store)
    yield store
    P.reset_pending_store()


# --------------------------------------------------------------------------- #
# Pure deciders                                                                #
# --------------------------------------------------------------------------- #
def test_is_expired_handles_deadline_and_none() -> None:
    """``None`` 截止时间 = 未配置 ⇒ 永不过期（不是立即过期，也不是默认通过）。"""
    assert P.is_expired(_NOW, _NOW - timedelta(seconds=1)) is True
    assert P.is_expired(_NOW, _NOW) is True
    assert P.is_expired(_NOW, _NOW + timedelta(seconds=1)) is False
    assert P.is_expired(_NOW, None) is False


def test_default_expiry_is_24h() -> None:
    """默认超时 = 24h。"""
    assert P.DEFAULT_TIMEOUT_HOURS == 24
    assert P.default_expiry(_NOW) == _NOW + timedelta(hours=24)


@pytest.mark.parametrize(
    ("level", "action", "kind"),
    [
        (P.READ, P.ACTION_AUTO, None),
        (P.WRITE, P.ACTION_PENDING_APPROVAL, P.APPROVE_DIFF),
        (P.EXTERNAL, P.ACTION_CONFIRM_PERMISSION, P.CONFIRM_PERMISSION),
        (P.DANGEROUS, P.ACTION_DENY, None),
    ],
)
def test_four_level_mapping(level: str, action: str, kind: str | None) -> None:
    """四级权限 → HITL 动作（READ 自动；WRITE 批 diff；EXTERNAL 确认；DANGEROUS 拒绝）。"""
    decision = P.decide_permission_level(level)
    assert decision["action"] == action
    assert decision["kind"] == kind


def test_dangerous_requires_admin_policy_and_confirms_each_time() -> None:
    """DANGEROUS 默认拒绝；仅 admin 显式策略 ⇒ 逐次 confirm_permission。"""
    assert P.decide_permission_level(P.DANGEROUS)["action"] == P.ACTION_DENY
    admin = P.decide_permission_level(P.DANGEROUS, is_admin_policy=True)
    assert admin["action"] == P.ACTION_CONFIRM_PERMISSION
    assert admin["kind"] == P.CONFIRM_PERMISSION


def test_write_tenant_auto_approve_low_risk() -> None:
    """租户低风险自动批准 ⇒ WRITE 直接 auto（但必须留审计）。"""
    decision = P.decide_permission_level(
        P.WRITE, tenant_policy={"auto_approve_low_risk": True}
    )
    assert decision["action"] == P.ACTION_AUTO
    assert decision["kind"] is None


def test_unknown_level_fails_closed_to_deny() -> None:
    """未知级别 ⇒ ``deny``（fail closed），绝不 auto。"""
    assert P.decide_permission_level("WHAT")["action"] == P.ACTION_DENY


# --------------------------------------------------------------------------- #
# Policy → PendingAction mapping                                               #
# --------------------------------------------------------------------------- #
def test_external_plan_produces_confirm_permission_pending() -> None:
    """阳性 — EXTERNAL 工具的计划里带着一个 ``confirm_permission`` 的待办。"""
    result = policy.plan_for_tool(
        "research.search", run_id="run-1", tenant_id=_TENANT, now=_NOW
    )
    assert result["level"] == P.EXTERNAL
    action = result["pending"]
    assert action is not None
    assert action.kind == P.CONFIRM_PERMISSION
    assert action.status == P.WAITING
    assert action.expires_at == _NOW + timedelta(hours=24)


def test_dangerous_without_admin_creates_no_pending() -> None:
    """阴性③ — DANGEROUS 无 admin 策略 ⇒ ``deny`` 且**不产生** pending。"""
    result = policy.plan_for_tool("code.commit", run_id="run-2", tenant_id=_TENANT)
    assert result["level"] == P.DANGEROUS
    assert result["pending"] is None
    assert result["decision"]["action"] == P.ACTION_DENY


def test_auto_approval_records_an_audit_entry() -> None:
    """红线 — 低风险 WRITE 自动批准必须有审计记录（action=auto）。"""
    result = policy.plan(
        P.WRITE,
        run_id="run-3",
        tenant_id=_TENANT,
        tenant_policy={"auto_approve_low_risk": True},
        now=_NOW,
    )
    assert result["pending"] is None
    audit = result["audit"]
    assert audit["event"] == "hitl_decision"
    assert audit["action"] == P.ACTION_AUTO
    assert audit["level"] == P.WRITE
    assert audit["run_id"] == "run-3"
    assert audit["tenant_id"] == _TENANT


def test_unknown_policy_keys_are_ignored() -> None:
    """未知策略键不得悄悄放行（读为 False）。"""
    assert policy.read_tenant_policy({"dangerous_ok": True}) == {
        "auto_approve_low_risk": False
    }


# --------------------------------------------------------------------------- #
# Store behaviour                                                              #
# --------------------------------------------------------------------------- #
def test_create_then_resolve_lets_the_run_continue(_fresh_store) -> None:
    """阳性 — 创建 → ``waiting``；resolve → ``resolved`` 且 run 继续。"""
    action = _fresh_store.create(
        _TENANT, run_id="run-4", kind=P.CONFIRM_PERMISSION, now=_NOW
    )
    assert action.status == P.WAITING
    assert action.run_status == P.RUN_STATUS_PAUSED
    assert action.run_continues is False

    resolved = _fresh_store.resolve(
        _TENANT, action.pending_id, decision="permission", actor="u1", now=_NOW
    )
    assert resolved.status == P.RESOLVED
    assert resolved.run_continues is True
    assert resolved.run_status == P.RUN_STATUS_RESUMED
    assert resolved.resolved_by == "u1"
    assert resolved.resolution == "permission"


def test_resolve_is_idempotent(_fresh_store) -> None:
    """阴性④ — 重复 resolve ⇒ 返回**同**结果，不重复计数。"""
    action = _fresh_store.create(_TENANT, run_id="run-5", kind=P.CLARIFY, now=_NOW)
    first = _fresh_store.resolve(_TENANT, action.pending_id, decision="approve", actor="a", now=_NOW)
    second = _fresh_store.resolve(_TENANT, action.pending_id, decision="reject", actor="b", now=_NOW)
    assert second.status == P.RESOLVED
    assert second.resolution == first.resolution == "approve"  # 第一次的决定保留
    assert second.resolved_by == "a"


def test_expired_action_is_fail_closed(_fresh_store) -> None:
    """阴性① + **反事实目标（登记）** — 超时 ⇒ ``expired`` 且 resolve 被拒。

    红线 21：过期**绝不**默认通过。红线反事实：把
    ``PendingActionStore.resolve`` 的 ``if status == EXPIRED: raise
    PendingActionExpired(pending_id)`` 改成「过期即通过」后本用例必须转红
    （``pytest.raises`` 不再触发）。
    """
    past = _NOW - timedelta(seconds=1)
    action = _fresh_store.create(
        _TENANT, run_id="run-6", kind=P.CONFIRM_PERMISSION, expires_at=past, now=_NOW
    )
    with pytest.raises(P.PendingActionExpired):
        _fresh_store.resolve(_TENANT, action.pending_id, now=_NOW)

    # 状态与 run 状态都必须是 expired，且不得被视为已通过。
    stored = _fresh_store.get(_TENANT, action.pending_id)
    assert stored is not None
    assert stored.status == P.EXPIRED
    assert stored.run_status == P.RUN_STATUS_EXPIRED
    assert stored.run_continues is False
    assert stored.resolution is None


def test_expire_overdue_sweep_marks_expired(_fresh_store) -> None:
    """超时清扫把 overdue 的 ``waiting`` 翻成 ``expired``。"""
    past = _NOW - timedelta(hours=1)
    _fresh_store.create(
        _TENANT, run_id="run-7", kind=P.CLARIFY, expires_at=past, now=_NOW - timedelta(days=2)
    )
    flipped = _fresh_store.expire_overdue(_TENANT, now=_NOW)
    assert [a.status for a in flipped] == [P.EXPIRED]
    assert flipped[0].run_status == P.RUN_STATUS_EXPIRED


def test_cross_tenant_reads_are_empty_and_resolve_refused(_fresh_store) -> None:
    """阴性② — 他租户读不到（空集），resolve 被拒（403 的存储层依据，红线 5）。"""
    action = _fresh_store.create(_TENANT, run_id="run-8", kind=P.CONFIRM_PERMISSION, now=_NOW)
    assert _fresh_store.list(_OTHER) == []
    assert _fresh_store.get(_OTHER, action.pending_id) is None
    with pytest.raises(P.CrossTenantPendingAction):
        _fresh_store.resolve(_OTHER, action.pending_id, now=_NOW)


def test_falsy_tenant_fails_closed(_fresh_store) -> None:
    """红线 5 — 无租户读为**空集**，且拒绝写入。"""
    assert _fresh_store.list(None) == []
    assert _fresh_store.get(None, "pa-x") is None
    with pytest.raises(P.PendingActionNotFound):
        _fresh_store.resolve(None, "pa-x")
    with pytest.raises(ValueError):
        _fresh_store.create(None, run_id="r", kind=P.CLARIFY)


def test_unknown_kind_is_rejected(_fresh_store) -> None:
    """未知 kind ⇒ ValueError（不落一条语义不明的 pending）。"""
    with pytest.raises(ValueError):
        _fresh_store.create(_TENANT, run_id="r", kind="explode")


# --------------------------------------------------------------------------- #
# API surface + RBAC (真中间件，无桩)                                            #
# --------------------------------------------------------------------------- #
def _client() -> TestClient:
    """The real pending router behind the real RBAC middleware (no stubs)."""
    minimal = FastAPI()
    minimal.add_middleware(RBACMiddleware)
    minimal.include_router(pending_router.router, prefix="/pending-actions")
    return TestClient(minimal)


def _auth(tenant: str = _TENANT, role: str = "admin") -> dict[str, str]:
    token = create_access_token(user_id="eng-t21", role=role, workspace_id=tenant)
    return {"Authorization": f"Bearer {token}"}


def test_routes_are_registered_and_mapped() -> None:
    """两个新路由都在 RBAC 表内（additive），最长前缀命中 resolve。"""
    assert ROUTE_PERMISSION_MAP[("GET", "/pending-actions")] == ("read", "skills")
    assert ROUTE_PERMISSION_MAP[("POST", "/pending-actions")] == ("write", "skills")
    assert RBACMiddleware._resolve_permission(
        "POST", "/pending-actions/xyz/resolve"
    ) == ("write", "skills")
    assert "/pending-actions" in forgeflow_app.openapi().get("paths", {})
    assert "/pending-actions/{pending_id}/resolve" in forgeflow_app.openapi().get("paths", {})


def test_unauthenticated_list_is_401() -> None:
    """无 token ⇒ 401（HITL 面从不公开）。"""
    assert _client().get("/pending-actions").status_code == 401


def test_list_and_resolve_round_trip(_fresh_store) -> None:
    """阳性 — GET 列出 waiting；POST resolve 后 run 继续。"""
    action = _fresh_store.create(_TENANT, run_id="run-api", kind=P.CONFIRM_PERMISSION)
    client = _client()

    listed = client.get("/pending-actions", headers=_auth())
    assert listed.status_code == 200, listed.text
    ids = [item["pending_id"] for item in listed.json()]
    assert action.pending_id in ids
    assert listed.json()[0]["status"] == P.WAITING
    assert listed.json()[0]["run_status"] == P.RUN_STATUS_PAUSED

    resolved = client.post(
        f"/pending-actions/{action.pending_id}/resolve",
        headers=_auth(),
        json={"decision": "permission", "actor": "u9"},
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == P.RESOLVED
    assert body["run_continues"] is True
    assert body["run_status"] == P.RUN_STATUS_RESUMED


def test_cross_tenant_resolve_is_403(_fresh_store) -> None:
    """阴性② — 他租户 resolve 别人的 pending ⇒ 403（红线 5）。"""
    action = _fresh_store.create(_TENANT, run_id="run-api-2", kind=P.CONFIRM_PERMISSION)
    resp = _client().post(
        f"/pending-actions/{action.pending_id}/resolve",
        headers=_auth(_OTHER),
        json={"decision": "permission"},
    )
    assert resp.status_code == 403, resp.text


def test_unknown_pending_resolve_is_404() -> None:
    """未知 id ⇒ 404（不伪造）。"""
    resp = _client().post(
        "/pending-actions/nope/resolve", headers=_auth(), json={"decision": "approve"}
    )
    assert resp.status_code == 404, resp.text


def test_expired_resolve_is_409(_fresh_store) -> None:
    """阴性① — 过期后 resolve ⇒ 409（fail-closed，绝不默认通过）。"""
    action = _fresh_store.create(
        _TENANT,
        run_id="run-api-3",
        kind=P.CONFIRM_PERMISSION,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    resp = _client().post(
        f"/pending-actions/{action.pending_id}/resolve",
        headers=_auth(),
        json={"decision": "permission"},
    )
    assert resp.status_code == 409, resp.text

