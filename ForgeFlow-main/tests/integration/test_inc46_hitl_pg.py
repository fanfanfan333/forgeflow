"""INC46 T21 (pg-档) — ``pending_actions`` on **real PostgreSQL**.

Proves, against the live dev PostgreSQL, what an in-memory store cannot:

* a ``confirm_permission`` action a run pauses on is **persisted** — a *process
  restart* (dropping the store cache and re-resolving it) still finds the action
  ``waiting`` and can resolve it, then **still** finds it ``resolved``. This is
  the阳性探针 that proves the state lives in the DB, not in memory (红线 21);
* the table is **tenant scoped** (红线 5): a second tenant observes the empty set
  and is refused when it tries to resolve another tenant's action;
* expiry is **fail-closed** on the real table: a past ``expires_at`` ⇒ the action
  is ``expired`` and a resolve raises (never an implicit approval).

It dials the same dev database the rest of the postgres-profile suite uses and
skips cleanly when it is unreachable (mirroring ``test_inc46_outcome_pg.py``).
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from forgeflow.config import get_settings
from forgeflow.hitl.pending import (
    CONFIRM_PERMISSION,
    EXPIRED,
    RESOLVED,
    WAITING,
    CrossTenantPendingAction,
    PendingActionExpired,
    PostgresPendingActionStore,
    get_pending_store,
    reset_pending_store,
)

_TENANT_A = f"t-t21-a-{uuid.uuid4().hex[:8]}"
_TENANT_B = f"t-t21-b-{uuid.uuid4().hex[:8]}"


def _dsn() -> str:
    return get_settings().postgres_sync_url.replace(
        "postgresql+psycopg://", "postgresql://"
    )


def _pg_reachable() -> bool:
    """Cheap liveness probe — the suite must skip, not fail, without a database."""
    dsn = _dsn()
    host, _, rest = dsn.rpartition("@")[2].partition(":")
    port = int(rest.split("/")[0]) if rest.split("/")[0].isdigit() else 5432
    try:
        with socket.create_connection((host or "127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(), reason="dev PostgreSQL (5433) unreachable"
)


@pytest.fixture()
def store() -> PostgresPendingActionStore:
    return PostgresPendingActionStore(_dsn())


def test_confirm_permission_round_trip_is_tenant_scoped(
    store: PostgresPendingActionStore,
) -> None:
    """阳性 — 写入按 tenant 隔离读回。"""
    run = f"run-{uuid.uuid4().hex[:8]}"
    action = store.create(_TENANT_A, run_id=run, kind=CONFIRM_PERMISSION, payload={"tool": "research.search"})
    assert action.status == WAITING

    fetched = store.get(_TENANT_A, action.pending_id)
    assert fetched is not None
    assert fetched.kind == CONFIRM_PERMISSION
    assert fetched.payload == {"tool": "research.search"}

    # 他租户 ⇒ 空集（不泄露）
    assert store.get(_TENANT_B, action.pending_id) is None
    assert store.list(_TENANT_B) == []


def test_pending_action_survives_a_process_restart() -> None:
    """阳性探针 + **反事实目标（登记）** — 重启后仍可 resolve 并继续。

    证明「状态真的在 DB 而不在内存」：创建 → ``reset_pending_store()``（模拟进程
    重启）→ ``get_pending_store()`` 仍是 PG 且仍能读回 ``waiting`` → resolve →
    再重置仍是 ``resolved``。

    红证：把 ``forgeflow.hitl.pending.get_pending_store`` 的 ``if dsn:`` 改成
    ``if False:``（状态退化为内存态）后本用例必须转红 —— 重启拿到的是一个空的
    内存 store，``get`` 返回 ``None``。
    """
    reset_pending_store()
    store = get_pending_store()
    assert isinstance(store, PostgresPendingActionStore)

    run = f"run-{uuid.uuid4().hex[:8]}"
    action = store.create(_TENANT_A, run_id=run, kind=CONFIRM_PERMISSION)

    # --- simulate a process restart: drop the cache and re-resolve --------- #
    reset_pending_store()
    store_after = get_pending_store()
    assert isinstance(store_after, PostgresPendingActionStore)

    revived = store_after.get(_TENANT_A, action.pending_id)
    assert revived is not None, "pending action lost across restart (state not persisted)"
    assert revived.status == WAITING

    resolved = store_after.resolve(
        _TENANT_A, action.pending_id, decision="permission", actor="u-restart"
    )
    assert resolved.status == RESOLVED

    # --- restart again: the resolution is durable too ---------------------- #
    reset_pending_store()
    final = get_pending_store().get(_TENANT_A, action.pending_id)
    assert final is not None
    assert final.status == RESOLVED
    assert final.resolved_by == "u-restart"
    assert final.run_continues is True


def test_cross_tenant_resolve_is_refused(store: PostgresPendingActionStore) -> None:
    """红线 5 — 他租户 resolve ⇒ 被拒（403 的存储层依据）。"""
    action = store.create(_TENANT_A, run_id=f"run-{uuid.uuid4().hex[:8]}", kind=CONFIRM_PERMISSION)
    with pytest.raises(CrossTenantPendingAction):
        store.resolve(_TENANT_B, action.pending_id)


def test_expired_action_is_fail_closed_on_pg(store: PostgresPendingActionStore) -> None:
    """阴性 — 过期 ⇒ ``expired`` 且 resolve 被拒（红线 21，绝不默认通过）。"""
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    action = store.create(
        _TENANT_A,
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        kind=CONFIRM_PERMISSION,
        expires_at=past,
    )
    with pytest.raises(PendingActionExpired):
        store.resolve(_TENANT_A, action.pending_id)
    stored = store.get(_TENANT_A, action.pending_id)
    assert stored is not None
    assert stored.status == EXPIRED
    assert stored.resolution is None


def test_null_payload_reads_back_as_empty_dict_not_zero(
    store: PostgresPendingActionStore,
) -> None:
    """红线 4 — 未测量的 ``payload`` 读回是 ``{}``（绝不是 ``0`` / ``''``）。"""
    action = store.create(_TENANT_A, run_id=f"run-{uuid.uuid4().hex[:8]}", kind=CONFIRM_PERMISSION)
    stored = store.get(_TENANT_A, action.pending_id)
    assert stored is not None
    assert stored.payload == {}
    assert stored.payload != 0
