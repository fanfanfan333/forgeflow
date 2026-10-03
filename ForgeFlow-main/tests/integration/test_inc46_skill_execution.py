"""INC46 T03 (pg-档) — 选中 Skill 的 procedure 经 ToolExecutor **真调**并落轨迹。

本文件在**真实 dev PostgreSQL**（``localhost:5433``）上证明一件 fake 也证明不了的事：
一个被选中的 Skill 声明的 ``procedure`` 的每一步，真的逐步交到既有的
``ToolExecutor.execute``（真调 → 由 T01 落 ``run_steps``），而不是被"报告为已执行"。

钉死（设计 §1.3 / §7-T03 判据 5/6）：

  * 带 ``procedure`` 的 Skill ⇒ ``_default_executor`` 的计划里出现其声明的工具，
    且这些工具真的被执行（spy 计数 + ``run_steps`` 有对应行）；
  * 工具选择**逐字**取自 ``procedure[i].tool`` 并受 ``PLATFORM_PLAN_TOOLS`` 白名单约束
    （非白名单工具既不执行、也**绝不**替换为别的工具）；
  * 无 ``procedure`` ⇒ 不驱动任何 skill 步（``_skill_candidates`` 为空）。

spy binding 只为**已有** tool 名替换 provider（不新增 catalogue id），随用例恢复。
本用例最后删净自己写入的 ``run_steps`` 行；不可达 PG 时干净 skip。
"""

from __future__ import annotations

import socket
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest

from forgeflow.config import get_settings
from forgeflow.runtime import orchestrator as orch
from forgeflow.runtime import tool_registry
from forgeflow.runtime.events import RunEventBus
from forgeflow.runtime.orchestrator import RequestContext, TaskCreate
from forgeflow.runtime.tool_registry import ToolBinding
from forgeflow.runtime.trace_store import list_for_run, reset_trace_sink

# Captured at import (before conftest's autouse DNS stub) so we reach 127.0.0.1.
_REAL_GETADDRINFO = socket.getaddrinfo
_DSN = get_settings().postgres_sync_url.replace("postgresql+psycopg://", "postgresql://")
_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _postgres_profile(monkeypatch):
    """Exercise the real postgres trace path (the write gate reads this)."""
    monkeypatch.setattr(get_settings(), "storage_backend", "postgres")
    reset_trace_sink()
    yield
    reset_trace_sink()


@pytest.fixture
async def pool(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _REAL_GETADDRINFO)
    from forgeflow.database import _init_connection

    try:
        p = await asyncpg.create_pool(
            _DSN, min_size=1, max_size=3, timeout=4, init=_init_connection
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live Postgres not reachable ({exc})")
    yield p
    await p.close()
    from forgeflow.database import close_pool

    await close_pool()


def _upgrade_head() -> None:
    import os

    child_env = dict(os.environ)
    child_env["POSTGRES_SYNC_URL"] = get_settings().postgres_sync_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=child_env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{result.stderr[-2000:]}")


def _install_spy(monkeypatch, tool: str, calls: list[dict]) -> None:
    """Replace ``tool``'s provider with an in-process spy (same tool id)."""

    async def _handler(args, ctx):  # noqa: ANN001
        calls.append({"tool": tool, "args": dict(args or {})})
        return {"ok": True, "summary": f"spy:{tool}", "provider": "inc46-spy"}

    monkeypatch.setitem(
        tool_registry._REGISTRY,
        tool,
        ToolBinding(
            tool_id=tool,
            handler=_handler,
            kind="real",
            provider="inc46-spy",
            description="INC46 T03 spy binding (existing tool id, alternative provider)",
        ),
    )


def _skill_with_procedure() -> dict:
    return {
        "id": "skill-health",
        "name": "策略健康检查",
        "version": "1.0.0",
        "description": "合规体检并登记产物",
        "steps": ["合规体检", "登记产物"],  # legacy text — must stay untouched
        "procedure": [
            {"purpose": "合规体检", "tool": "policy.check", "input": [], "output": ["verdict"]},
            {"purpose": "登记产物", "tool": "artifact.save", "input": [], "output": ["ref"]},
        ],
    }


async def test_selected_skill_procedure_really_executes_and_persists(pool, monkeypatch):
    _upgrade_head()
    tool_registry.load_default_bindings()

    spy_calls: list[dict] = []
    _install_spy(monkeypatch, "policy.check", spy_calls)
    _install_spy(monkeypatch, "artifact.save", spy_calls)

    tenant = f"t-inc46-t03-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    ctx = RequestContext(
        tenant_id=tenant,
        user_id="u-inc46-t03",
        role="manager",
        injected_skills=[_skill_with_procedure()],
    )
    task = TaskCreate(intent="做一次策略体检", context={})

    try:
        steps, errors = await orch._default_executor(task, ctx, RunEventBus(), run_id)
        assert errors == [], f"procedure 真调不应产生错误：{errors}"

        # 1) The plan really carries the skill's declared tools.
        plan_tools = [
            str(s.get("tool")) for s in (task.context.get("plan") or {}).get("steps", [])
        ]
        assert "policy.check" in plan_tools
        assert "artifact.save" in plan_tools

        # 2) The procedure steps really drove ToolExecutor (spy observed).
        assert {c["tool"] for c in spy_calls} == {"policy.check", "artifact.save"}

        # 3) T01 materialised one run_steps row per real call (tenant-scoped).
        rows = await list_for_run(tenant, run_id)
        by_tool = {}
        for row in rows:
            by_tool.setdefault(row.tool, []).append(row)
        assert "policy.check" in by_tool and "artifact.save" in by_tool
        assert by_tool["policy.check"][0].status == "ok"
        assert by_tool["artifact.save"][0].status == "ok"
        assert by_tool["policy.check"][0].input["args"].get("intent") == "做一次策略体检"
        # The invocation is stored verbatim on the output column.
        assert by_tool["policy.check"][0].output["provider"] == "inc46-spy"

        # 4) Result-first invariant intact: the run still ends on report.render.
        assert plan_tools[-1] == "report.render"
        assert any(str(s.get("tool")) == "report.render" for s in steps)
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM run_steps WHERE tenant_id=$1", tenant)


async def test_whitelist_violation_is_never_substituted(pool, monkeypatch):
    """A step tool outside the whitelist is neither executed nor swapped."""
    tool_registry.load_default_bindings()
    spy_calls: list[dict] = []
    _install_spy(monkeypatch, "policy.check", spy_calls)

    tenant = f"t-inc46-t03w-{uuid.uuid4().hex[:8]}"
    run_id = str(uuid.uuid4())
    skill = _skill_with_procedure()
    skill["procedure"] = [
        {"purpose": "ok", "tool": "policy.check"},
        {"purpose": "越权", "tool": "data.export"},  # NOT in PLATFORM_PLAN_TOOLS
    ]
    ctx = RequestContext(tenant_id=tenant, user_id="u", role="manager", injected_skills=[skill])
    task = TaskCreate(intent="体检", context={})

    try:
        await orch._default_executor(task, ctx, RunEventBus(), run_id)

        plan_tools = [
            str(s.get("tool")) for s in (task.context.get("plan") or {}).get("steps", [])
        ]
        assert "data.export" not in plan_tools, "非白名单工具不得进入计划"
        # The whitelisted step still ran; nothing was substituted for the rejected one.
        assert {c["tool"] for c in spy_calls} == {"policy.check"}

        rows = await list_for_run(tenant, run_id)
        assert "data.export" not in {r.tool for r in rows}
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM run_steps WHERE tenant_id=$1", tenant)


def test_no_procedure_drives_nothing(monkeypatch):
    """A skill with only the legacy ``steps`` text drives no step at all."""
    tool_registry.load_default_bindings()
    legacy_ctx = RequestContext(
        tenant_id="t-1",
        user_id="u",
        role="manager",
        injected_skills=[
            {
                "id": "s-plain",
                "name": "纯文本技能",
                "version": "1.0.0",
                "description": "只有 steps 文本",
                "steps": ["拉取数据", "生成结论"],
            }
        ],
    )
    task = TaskCreate(intent="整理", context={})
    assert orch._skill_candidates(legacy_ctx) == []
    plain = RequestContext(tenant_id="t-1", user_id="u", role="manager")
    assert orch._candidates_for(task, legacy_ctx) == orch._candidates_for(task, plain)
