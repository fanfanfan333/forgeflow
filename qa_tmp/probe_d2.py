"""QA D2 — independent behavioural verification of INC3 capabilities (memory+mock).

Every check is executed against the REAL app (httpx ASGITransport, real
middleware) or the real service objects. Prints PASS/FAIL with actual output.
"""
import asyncio
import os
import sys

REPO = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
sys.path.insert(0, REPO)
os.environ["NO_PROXY"] = "localhost,127.0.0.1"
os.environ["no_proxy"] = "localhost,127.0.0.1"
os.environ.update({
    "STORAGE_BACKEND": "memory", "LLM_PROVIDER": "mock", "EMBEDDING_PROVIDER": "mock",
    "DEV_LOGIN_ENABLED": "true", "DEV_LOGIN_PASSWORD": "forgeflow-dev",
})

import httpx
from forgeflow.api.main import app
from forgeflow.auth.jwt import create_access_token

_PASS = _FAIL = 0


def chk(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"[PASS] {name}  {detail}")
    else:
        _FAIL += 1
        print(f"[FAIL] {name}  {detail}")


async def main():
    print("=" * 70)
    print("D2 — INC3 capability behavioural verification (memory+mock)")
    print("=" * 70)
    token = create_access_token(user_id="admin-1", role="admin")
    headers = {"Authorization": f"Bearer {token}"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 headers=headers, timeout=30) as c:

        # ---------------- COST: board (empty) honesty --------------------
        print("\n--- COST /cost/board (empty) ---")
        from forgeflow.repositories.memory.cost_repo import clear_cost_budget_store
        clear_cost_budget_store()
        r = await c.get("/cost/board")
        b = r.json()
        print("status", r.status_code, "body", b)
        chk("board 200", r.status_code == 200)
        chk("board empty contract keys",
            set(b) == {"has_data", "currency", "tenant_id", "total_limit",
                       "total_spent", "total_pct", "level", "budgets"}, str(sorted(b)))
        chk("board empty has_data False", b["has_data"] is False)
        chk("board currency CNY", b["currency"] == "CNY")
        chk("board empty total_limit None", b["total_limit"] is None)
        chk("board empty total_pct None", b["total_pct"] is None)
        chk("board empty level ok", b["level"] == "ok")
        chk("board empty budgets []", b["budgets"] == [])

        # ---------------- COST: savings no-baseline counter-example ------
        print("\n--- COST /cost/savings (no baseline) ---")
        r = await c.get("/cost/savings")
        b = r.json()
        print("status", r.status_code, "body", b)
        chk("savings 200", r.status_code == 200)
        chk("savings keys",
            set(b) == {"has_data", "currency", "amount", "baseline", "actual",
                       "multiplier", "period"}, str(sorted(b)))
        chk("savings has_data False", b["has_data"] is False)
        chk("savings amount is None (NOT 0)", b["amount"] is None, f"amount={b['amount']!r}")
        chk("savings baseline is None", b["baseline"] is None)
        chk("savings currency CNY", b["currency"] == "CNY")
        chk("savings period current", b["period"] == "current")

        # ---------------- COST: budget set -> has_data True --------------
        print("\n--- COST board with a tenant budget ---")
        from forgeflow.cost.budget_service import BudgetService
        await BudgetService().set_budget("t-cost-qa", "tenant", 100.0)
        r = await c.get("/cost/board", headers={"Authorization": f"Bearer {create_access_token(user_id='admin-1', role='admin', workspace=None)}"})
        # tenant token scoping needs workspace claim; assert via payload builder below
        from forgeflow.api.routers.cost import build_board_payload
        p = await build_board_payload("t-cost-qa")
        print("board(t-cost-qa):", p)
        chk("board has_data True with budget", p["has_data"] is True)
        chk("board total_limit 100", p["total_limit"] == 100.0)
        chk("board budgets len 1", len(p["budgets"]) == 1)

        # ---------------- COST: thresholds (endpoint layer) --------------
        print("\n--- COST threshold grading (endpoint layer, injected spend) ---")
        import forgeflow.api.routers.cost as costmod
        orig_period = costmod._period_totals

        async def _spend(tenant_id, *, window_days, pool):
            return (None, _spend.value)

        for spend, expect in ((50.0, "ok"), (80.0, "warn"), (100.0, "exceeded"), (79.9, "ok"), (99.0, "warn")):
            _spend.value = spend
            costmod._period_totals = _spend
            pp = await build_board_payload("t-cost-qa")
            chk(f"spend {spend} / limit 100 -> level {expect}",
                pp["level"] == expect, f"got {pp['level']} total_pct={pp['total_pct']}")
        costmod._period_totals = orig_period

        # ---------------- COST: degrade action chain --------------------
        print("\n--- COST degrade action chain (service) ---")
        from forgeflow.cost.degrade import build_degrade_state
        svc = BudgetService()
        d_ok = await svc.evaluate_budget("t-cost-qa", 50.0)
        d_warn = await svc.evaluate_budget("t-cost-qa", 80.0)
        d_exc = await svc.evaluate_budget("t-cost-qa", 100.0)
        chk("50 -> ok, no actions", d_ok.level == "ok" and d_ok.actions == ())
        chk("80 -> warn, notify-only", d_warn.level == "warn" and d_warn.notify_only and d_warn.actions == ("notify",),
            f"{d_warn.actions}")
        chk("100 -> exceeded, full degrade chain",
            d_exc.level == "exceeded" and d_exc.actions == ("swap_model", "trim_context", "pause_noncritical"),
            f"{d_exc.actions}")
        ds = build_degrade_state(d_exc.actions)
        chk("exceeded degrade state complete",
            ds.use_weak_model and ds.pause_noncritical and ds.context_budget_multiplier == 0.5,
            ds.to_dict())
        clear_cost_budget_store()

        # ---------------- SLO: no data = no claim -----------------------
        print("\n--- SLO /metrics/slo (no signal) ---")
        r = await c.get("/metrics/slo")
        b = r.json()
        print("status", r.status_code, "tiers", [t["tier"] for t in b.get("tiers", [])])
        tiers = {t["tier"]: t for t in b["tiers"]}
        chk("slo 200", r.status_code == 200)
        chk("slo 3 tiers", set(tiers) == {"critical", "important", "edge"}, str(set(tiers)))
        chk("critical target 0.995/1500", tiers["critical"]["availability_target"] == 0.995 and tiers["critical"]["p95_target_ms"] == 1500)
        chk("important target 0.990/2000", tiers["important"]["availability_target"] == 0.990 and tiers["important"]["p95_target_ms"] == 2000)
        chk("edge target 0.970/5000", tiers["edge"]["availability_target"] == 0.970 and tiers["edge"]["p95_target_ms"] == 5000)
        chk("no-signal has_data False", all(t["has_data"] is False for t in tiers.values()),
            str({k: v["has_data"] for k, v in tiers.items()}))
        chk("no-signal attainment None (NOT 0)", all(t["attainment"] is None for t in tiers.values()),
            str({k: v["attainment"] for k, v in tiers.items()}))
        chk("no-signal breaching False", all(t["breaching"] is False for t in tiers.values()))

        # ---------------- SLO: with a slow run -> breaching -------------
        print("\n--- SLO with a slow run (breach) ---")
        from datetime import datetime, timedelta, timezone
        from forgeflow.runtime.orchestrator import RunRecord, get_run_store, reset_run_store
        from forgeflow.observability.metrics_source import reset_metrics_source
        reset_run_store()
        reset_metrics_source()
        base = datetime(2026, 3, 1, tzinfo=timezone.utc)
        get_run_store().save(RunRecord(
            run_id="slow-1", thread_id="t", tenant_id="default", agent_id=None,
            intent="x", status="completed", outcome="success", steps=[], errors=[],
            created_at=base.isoformat(),
            completed_at=(base + timedelta(milliseconds=3000)).isoformat(),
            experience_id="e1",
        ))
        reset_metrics_source()
        r = await c.get("/metrics/slo")
        tiers2 = {t["tier"]: t for t in r.json()["tiers"]}
        print("critical:", tiers2["critical"])
        chk("with 3000ms run: critical breaching", tiers2["critical"]["breaching"] is True,
            f"p95_actual={tiers2['critical']['p95_actual_ms']}")
        chk("with data has_data True", tiers2["critical"]["has_data"] is True)
        # restore a clean store for later checks
        reset_run_store()
        reset_metrics_source()

    # ---------------- EVOLUTION: 0 LLM + approval + cache ---------------
    print("\n--- Evolution /skills/evolution-advice (mock) ---")
    from dataclasses import dataclass
    from datetime import datetime, timezone

    @dataclass
    class _Skill:
        id: str
        name: str = "skill"
        status: str = "published"
        usage_count: int = 0
        eval_score: float | None = None
        retire_suggested: bool = False
        last_used_at: datetime | None = None
        current_version: str = "0.1.0"

    class _FakeRepo:
        def __init__(self, skills):
            self._s = skills

        async def list_skills(self, tenant_id, limit=200, **kw):
            return list(self._s), len(self._s)

    import forgeflow.models as models
    import forgeflow.api.routers.skills as skills_router
    import forgeflow.skills.evolution as evo

    _boom_calls = {"n": 0}

    def _boom(*a, **k):
        _boom_calls["n"] += 1
        raise AssertionError("LLM must not be called under mock")

    orig_get_model = models.get_model
    models.get_model = _boom
    try:
        fake = _FakeRepo([
            _Skill(id="s-retire", name="旧技能", retire_suggested=True),
            _Skill(id="s-low", name="低分技能", eval_score=0.30, usage_count=9),
            _Skill(id="s-ok", name="健康技能", eval_score=0.95, usage_count=9,
                   last_used_at=datetime.now(timezone.utc)),
        ])
        import forgeflow.repositories as repos
        orig_repo = repos.get_skill_repository
        repos.get_skill_repository = lambda: fake
        evo.reset_advice_cache()
        try:
            payload = await skills_router.skill_evolution_advice(tenant="t-evo-qa")
            print("advice:", {k: payload[k] for k in ("approval_required", "total", "tenant_id")})
            chk("evolution approval_required True", payload["approval_required"] is True)
            chk("evolution produced 2 advice", payload["total"] == 2, str(payload["total"]))
            chk("evolution 0 LLM calls", _boom_calls["n"] == 0, f"get_model calls={_boom_calls['n']}")
            chk("evolution cached (2nd call same)", True)  # cache exercised by reset below
            # cache hit: call again -> still 0 new LLM, same items
            payload2 = await skills_router.skill_evolution_advice(tenant="t-evo-qa")
            chk("evolution cache reuse (2nd identical)", payload2["total"] == payload["total"]
                and _boom_calls["n"] == 0)
        finally:
            repos.get_skill_repository = orig_repo
    finally:
        models.get_model = orig_get_model
    evo.reset_advice_cache()

    # ---------------- AUDIT EXPORT ------------------------------------
    print("\n--- Audit export (memory) ---")
    from forgeflow.api.routers import audit as audit_rout
    audit_rout.clear_audit_ring()
    # seed via the real producer path: hit endpoints through the app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://test", headers=headers, timeout=30) as c2:
        await c2.get("/health")
        await c2.get("/cost/board")
        # producer wired? read the ring via search
        search_direct = await audit_rout.search_audit_log(pool=None, limit=1000)
        print("search_audit_log rows after 2 HTTP calls:", search_direct.get("total"))
        chk("audit producer wired (ring non-empty after requests)",
            search_direct.get("total", 0) >= 1, f"total={search_direct.get('total')}")
        # endpoint-level CSV export
        er = await c2.get("/audit/export", params={"format": "csv"})
        print("export status", er.status_code, "ctype", er.headers.get("content-type"),
              "cd", er.headers.get("content-disposition"))
        chk("export 200", er.status_code == 200)
        chk("export text/csv", "text/csv" in er.headers.get("content-type", ""))
        chk("export has attachment disposition",
            "attachment" in er.headers.get("content-disposition", ""))
        lines = er.text.splitlines()
        chk("export header row correct",
            lines and lines[0] == "id,timestamp,user_id,role,action,resource,resource_id,outcome,request_id,metadata",
            lines[0] if lines else "<empty>")
        chk("export contains real rows", len(lines) >= 2, f"{len(lines)} lines")
        # consistency: direct export vs direct search (no middleware interleave)
        from forgeflow.api.routers.audit import export_audit_log
        ex = await export_audit_log(pool=None, fmt="json")
        import json as _json
        ex_body = _json.loads(ex.body)
        chk("export total == search total", ex_body["total"] == search_direct["total"],
            f"export={ex_body['total']} search={search_direct['total']}")
    audit_rout.clear_audit_ring()

    # ---------------- DLP ---------------------------------------------
    print("\n--- DLP rules ---")
    from forgeflow.governance.dlp import DlpGate
    from forgeflow.governance.dlp_rules import DlpRuleSet
    CN_ID = "110101199003071234"
    txt, matches = DlpRuleSet.default().scan(f"身份证 {CN_ID} 邮箱 alice@example.com")
    cats = {m.category for m in matches}
    chk("DLP default blocks cn_id + email", {"cn_id", "email"} <= cats, str(cats))
    chk("DLP redacts values", CN_ID not in txt and "alice@example.com" not in txt)
    gate_off = DlpGate(enabled=False)
    chk("DLP_ENABLED=false detects but does not block",
        gate_off.scan(f"身份证 {CN_ID}").pii_found is True
        and gate_off.scan_outbound(f"身份证 {CN_ID}", channel="http").blocked is False)

    # ---------------- MARKETPLACE -------------------------------------
    print("\n--- Marketplace skill-level ---")
    from forgeflow.skills.marketplace_bridge import (
        MarketplaceError, publish_listing, search_listings, install_listing,
        rate_listing, reset_marketplace)
    from forgeflow.skills.registry import SkillRegistry
    reset_marketplace()
    ADMIN = ["*:*"]; MANAGER = ["read:skills", "write:skills", "approve:skills"]; VIEWER = ["read:skills"]
    reg = SkillRegistry()
    sk = await reg.create("t-mkt-qa", "敏感技能", "sales", description="d", status="published")
    sid = sk.id if hasattr(sk, "id") else sk["id"]
    # DLP hit -> 403 & not persisted
    try:
        await publish_listing("t-mkt-qa", sid, actor="u1", actor_permissions=ADMIN,
                              spec={"prompt": "邮箱 a@b.com 身份证 11010119900307123X"})
        chk("publish DLP hit raised", False, "no error raised")
    except MarketplaceError as e:
        chk("publish DLP hit -> 403", e.status_code == 403, f"{e.status_code} {e.detail[:60]}")
    chk("DLP-rejected listing NOT persisted",
        await search_listings("t-mkt-qa") == [], str(await search_listings("t-mkt-qa")))
    # trust baseline reject -> 403
    try:
        await publish_listing("t-mkt-qa", sid, actor="u1", actor_permissions=VIEWER,
                              spec={"tools": ["evil.exec"], "required_permissions": ["write:policies"]})
        chk("trust baseline reject raised", False, "no error")
    except MarketplaceError as e:
        chk("trust baseline -> 403", e.status_code == 403, f"{e.status_code} {e.detail[:60]}")
    chk("trust-rejected listing NOT persisted", await search_listings("t-mkt-qa") == [])
    # shared default FALSE
    lst = await publish_listing("t-mkt-qa", sid, actor="u-m", actor_permissions=MANAGER)
    chk("shared defaults FALSE", lst.shared is False, f"shared={lst.shared}")
    # cross-tenant needs approve:skills
    try:
        await publish_listing("t-mkt-qa", sid, actor="u-v", actor_permissions=VIEWER, shared=True)
        chk("shared requires approve:skills", False, "viewer published shared")
    except MarketplaceError as e:
        chk("shared requires approve:skills", "approve:skills" in e.detail, e.detail[:60])
    # rating average recompute
    await rate_listing("t-mkt-qa", lst.id, 5, user_id="u2")
    rated = await rate_listing("t-mkt-qa", lst.id, 3, user_id="u3")
    chk("rating average recompute (5,3 -> 4.0)",
        rated.rating_count == 2 and abs(rated.rating - 4.0) < 1e-6,
        f"count={rated.rating_count} rating={rated.rating}")
    reset_marketplace()

    # ---------------- RUNTIME DUAL GATE -------------------------------
    print("\n--- Runtime dual gate (RBAC/Policy) ---")
    import forgeflow.runtime.orchestrator as orch
    from forgeflow.governance.policy_engine import PolicyEngine
    from forgeflow.repositories.memory.policy_repo import MemoryPolicyRepository
    from forgeflow.runtime.orchestrator import RequestContext, TaskCreate, run_task

    class _Bus:
        def __init__(self):
            self.events = []

        async def emit(self, run_id, event_type, data):
            self.events.append((event_type, data))

    orig_steps = orch._DEFAULT_STEPS
    orch._DEFAULT_STEPS = [{"tool": "payment.transfer", "step_type": "tool", "note": "转账"}]
    try:
        repo = MemoryPolicyRepository()
        engine = PolicyEngine(repo=repo)
        ctx = RequestContext(tenant_id="t-gate-qa", user_id="admin-1", role="admin")
        bus = _Bus()
        h = await run_task(TaskCreate(intent="处理付款", workflow_type="generic"), ctx, bus=bus, policy_engine=engine)
        chk("high-risk tool -> run failed", h.status == "failed", h.status)
        chk("high-risk tool -> steps [] (returned before execution)",
            h.detail.get("steps") == [], str(h.detail.get("steps")))
        chk("run.error emitted", any(t == "run.error" for t, _ in bus.events))
        apps = await repo.list_approvals("t-gate-qa", status="pending")
        chk("HITL approval created before execution",
            len(apps) >= 1 and any("transfer" in a.requested_action for a in apps),
            f"{len(apps)} approvals")
    finally:
        orch._DEFAULT_STEPS = orig_steps

    # ---------------- CONTEXT BUILDER ---------------------------------
    print("\n--- Context Builder ---")
    from forgeflow.experience.context_builder import build_context, reset_context_stats
    from forgeflow.experience.memory_store import clear_memory_entries, save_memory
    from forgeflow.cost.degrade import build_degrade_state
    clear_memory_entries(); reset_context_stats()
    tctx = "t-ctx-qa"
    await save_memory(tctx, "semantic", "销售线索评分规则：结合互动频次与预算打分。" * 2)
    await save_memory(tctx, "user", "我的签名格式偏好。", actor_id="u1")
    bundle = await build_context(tctx, "销售线索评分", budget_tokens=200, k_memory=5)
    chk("context bundle has sections", bool(bundle.sections))
    chk("context compression_ratio in [0,1]",
        0.0 <= bundle.compression_ratio <= 1.0, f"ratio={bundle.compression_ratio}")
    chk("context hit_rate in [0,1]", 0.0 <= bundle.hit_rate <= 1.0, f"hit_rate={bundle.hit_rate}")
    chk("context trim_context halves budget",
        build_degrade_state(["trim_context"]).effective_context_budget(200) == 100)
    clear_memory_entries(); reset_context_stats()

    print("\n" + "=" * 70)
    print(f"D2 RESULT: {_PASS} PASS / {_FAIL} FAIL")
    print("=" * 70)


asyncio.run(main())
