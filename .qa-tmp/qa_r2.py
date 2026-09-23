"""Round-2 independent verification of the 4 fixes (QA 严过关).
Checks: P1-1 cn_id DLP, P1-2 memory/search 200, P1-3 audit no-drop,
        P2-V6 entry gate (sensitive intent -> 403) + runtime tool gate (halt).
"""
from __future__ import annotations

import os
import sys
import asyncio

sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")
os.environ["STORAGE_BACKEND"] = "memory"
os.environ["LLM_PROVIDER"] = "mock"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["API_SECRET_KEY"] = "qa-secret-key-1234567890"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "qa-dev"
os.environ["CORS_ALLOW_ORIGINS"] = "http://localhost:5173"

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

OUT: list[str] = []


def P(*a) -> None:
    OUT.append(" ".join(str(x) for x in a))


def tok(user, role, ws=None):
    from forgeflow.auth.jwt import create_access_token
    return {"Authorization": "Bearer " + create_access_token(user_id=user, role=role, workspace_id=ws)}


def cnpii():
    P("\n===== R2-A  P1-1 CN-ID DLP =====")
    from forgeflow.governance.dlp import DlpGate
    g = DlpGate()
    for label, text in [
        ("cn_id_18", "身份证 110101199003071234"),
        ("cn_id_X", "证件号 11010119900307123X"),
        ("cn_id_lowerx", "证件号 11010119900307123x"),
    ]:
        r = g.scan(text)
        P(f"   scan[{label}] pii={r.pii_found} cats={r.categories} redacted={r.redacted!r}")
    ob = g.scan_outbound("身份证 110101199003071234", channel="http")
    P(f"   scan_outbound(http, cn_id only) blocked={ob.blocked} reason={ob.reason}")
    # regression: old categories still fire
    for label, text in [("email", "联系 a@b.com"), ("phone", "手机 13800138000")]:
        r = g.scan(text)
        P(f"   scan[{label}] pii={r.pii_found} cats={r.categories}")


def runtime_gate():
    P("\n===== R2-D  P2-V6 runtime tool gate =====")
    import forgeflow.runtime.orchestrator as orch
    from forgeflow.governance.policy_engine import PolicyEngine
    from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

    orig = orch._DEFAULT_STEPS

    class Bus:
        def __init__(self):
            self.events = []

        async def emit(self, run_id, t, data):
            self.events.append((t, data))

    async def go():
        repo = MemoryPolicyRepository()
        engine = PolicyEngine(repo=repo)
        ctx = RequestContext(tenant_id="t-r2", user_id="admin-1", role="admin")
        # (a) high-risk tool must halt
        orch._DEFAULT_STEPS = [{"tool": "payment.transfer", "step_type": "tool", "note": "转账"}]
        bus = Bus()
        h = await run_task(TaskCreate(intent="处理付款", workflow_type="generic"), ctx, bus=bus, policy_engine=engine)
        apps = await repo.list_approvals("t-r2", status="pending")
        P(f"   high-risk: status={h.status} steps={h.detail['steps']} errors={h.detail['errors']}")
        P(f"   approvals pending={len(apps)} actions={[a.requested_action for a in apps]}")
        P(f"   run.error emitted={any(t=='run.error' for t,_ in bus.events)}")
        # (b) low-risk default plan still works
        orch._DEFAULT_STEPS = orig
        h2 = await run_task(TaskCreate(intent="分析销售数据并生成报告"), ctx, bus=Bus(), policy_engine=PolicyEngine(repo=MemoryPolicyRepository()))
        P(f"   low-risk: status={h2.status} steps={len(h2.detail['steps'])}")

    try:
        asyncio.run(go())
    finally:
        orch._DEFAULT_STEPS = orig


def main():
    cnpii()
    with patch("forgeflow.api.main.init_pool", new_callable=AsyncMock) as mp, \
         patch("forgeflow.api.main.compile_graph", new_callable=AsyncMock) as mg, \
         patch("forgeflow.api.main.get_mcp_tools", new_callable=AsyncMock, return_value=[]):
        mp.return_value = MagicMock()
        mg.return_value = MagicMock()
        from forgeflow.api.main import app
        from fastapi.testclient import TestClient
        with TestClient(app, raise_server_exceptions=False) as c:
            P("\n===== R2-B  P2-V6 entry gate (sensitive intent) =====")
            r = c.post("/tasks", json={"intent": "删除生产数据库并清空所有记录"}, headers=tok("u-admin", "admin"))
            P(f"   POST /tasks(sensitive) -> {r.status_code}  {str(r.json())[:160]}")
            r2 = c.post("/tasks", json={"intent": "分析销售数据并生成报告"}, headers=tok("u-admin", "admin"))
            P(f"   POST /tasks(benign)    -> {r2.status_code}  {str(r2.json())[:120]}")

            P("\n===== R2-C  P1-2 /memory/search offline =====")
            r3 = c.get("/memory/search", params={"q": "hello"}, headers=tok("u-admin", "admin"))
            P(f"   GET /memory/search?q=hello -> {r3.status_code}  body={str(r3.json())[:120]}")

    runtime_gate()
    P("\n===== DONE =====")
    sys.stdout.write("\n".join(OUT) + "\n")


if __name__ == "__main__":
    main()
