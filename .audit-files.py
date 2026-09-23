import os
ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
checks = {
    "Cost Optimization (P1-b)": [
        "forgeflow/cost/__init__.py", "forgeflow/cost/budget.py",
        "forgeflow/resilience/budget_guard.py",
    ],
    "Skill Evolution (P2-a)": [
        "forgeflow/skills/evolution.py",
    ],
    "Context Builder (P1-d)": [
        "forgeflow/experience/context_builder.py", "forgeflow/governance/context.py",
    ],
    "SLO 分级 (P1-c)": [
        "forgeflow/observability/slo.py",
    ],
    "Org Memory (P1-e)": [
        "forgeflow/experience/scopes.py",
    ],
    "Skill Marketplace (P2-b)": [
        "forgeflow/marketplace/registry.py", "forgeflow/api/routers/marketplace.py",
    ],
    "Runtime/Validation": [
        "forgeflow/runtime/orchestrator.py", "forgeflow/runtime/events.py",
        "forgeflow/runtime/agents_catalog.py", "forgeflow/validation/validator.py",
        "forgeflow/validation/replan.py",
    ],
    "Governance": [
        "forgeflow/governance/policy_engine.py", "forgeflow/governance/tenancy.py",
        "forgeflow/governance/dlp.py", "forgeflow/governance/models.py",
    ],
    "Experience": [
        "forgeflow/experience/models.py", "forgeflow/experience/embedding.py",
        "forgeflow/experience/memory_store.py", "forgeflow/experience/extractor.py",
        "forgeflow/experience/scopes.py",
    ],
    "Skills": [
        "forgeflow/skills/draft_spec.py", "forgeflow/skills/versioning.py",
        "forgeflow/skills/registry.py", "forgeflow/skills/candidate_compiler.py",
        "forgeflow/skills/evaluator.py", "forgeflow/skills/governance_gate.py",
        "forgeflow/skills/models.py", "forgeflow/skills/errors.py",
    ],
    "Repositories": [
        "forgeflow/repositories/base.py", "forgeflow/repositories/factory.py",
        "forgeflow/repositories/memory/experience_repo.py",
        "forgeflow/repositories/memory/skill_repo.py",
        "forgeflow/repositories/memory/policy_repo.py",
        "forgeflow/repositories/postgres/experience_repo.py",
        "forgeflow/repositories/postgres/skill_repo.py",
        "forgeflow/repositories/postgres/policy_repo.py",
    ],
    "API routers (new)": [
        "forgeflow/api/routers/tasks.py", "forgeflow/api/routers/runs.py",
        "forgeflow/api/routers/experiences.py", "forgeflow/api/routers/skills.py",
        "forgeflow/api/routers/policies.py", "forgeflow/api/routers/security.py",
        "forgeflow/api/routers/approvals_hub.py",
        "forgeflow/api/hub_schemas.py", "forgeflow/api/hub_deps.py",
    ],
    "Frontend views (new)": [
        "frontend/src/views/HomeView.tsx", "frontend/src/views/SkillsView.tsx",
        "frontend/src/views/KnowledgeView.tsx", "frontend/src/views/SecurityView.tsx",
        "frontend/src/styles/home.css", "frontend/src/styles/skills.css",
        "frontend/src/api/sse.ts", "frontend/src/hooks/useRunEvents.ts",
    ],
    "Docs": [
        "docs/sop/01-PRD.md", "docs/sop/02-ARCHITECTURE.md",
        "docs/sop/03-TASKS.md", "docs/sop/04-QA-REPORT.md",
        "docs/sop/RUNBOOK.md",
        "alembic/versions/010_agentflow_hubs.py",
    ],
}
lines = []
for group, paths in checks.items():
    lines.append(f"\n## {group}")
    for p in paths:
        full = os.path.join(ROOT, p.replace("/", os.sep))
        lines.append(f"  {'EXISTS ' if os.path.isfile(full) else 'MISSING'} {p}")

# 额外：列出 skills / experience / governance / runtime / validation 实际文件
for pkg in ["skills", "experience", "governance", "runtime", "validation", "repositories", "cost"]:
    d = os.path.join(ROOT, "forgeflow", pkg)
    if os.path.isdir(d):
        found = []
        for dp, dn, fn in os.walk(d):
            dn[:] = [x for x in dn if x != "__pycache__"]
            for f in fn:
                if f.endswith(".py"):
                    found.append(os.path.relpath(os.path.join(dp, f), d).replace("\\", "/"))
        lines.append(f"\n### forgeflow/{pkg}/ -> {sorted(found)}")
    else:
        lines.append(f"\n### forgeflow/{pkg}/ -> DIR NOT EXIST")

with open(os.path.join(os.path.dirname(ROOT), ".audit-files.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("ok")
