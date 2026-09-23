"""目标.md 全量读盘核验 —— 文件是否在 / 有没有生产调用方。

只读。结果写 _goal_verify.txt (UTF-8)。
"""
from __future__ import annotations

import io
import os
import re

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台"
FF = os.path.join(ROOT, "ForgeFlow-main")

# (分类, 项, 相对路径) —— 路径可以是文件也可以是目录
FILES = [
    ("A1", "Skill Evolution 实现", r"forgeflow\skills\evolution.py"),
    ("A1", "Skill Marketplace 桥接", r"forgeflow\skills\marketplace_bridge.py"),
    ("A1", "Marketplace 路由", r"forgeflow\api\routers\marketplace.py"),
    ("A2", "Org Memory 提升", r"forgeflow\experience\promotion.py"),
    ("A2", "去重/冲突消解", r"forgeflow\experience\dedup.py"),
    ("A3", "DLP 可配规则", r"forgeflow\governance\dlp_rules.py"),
    ("A3", "ABAC 条件算子", r"forgeflow\governance\conditions.py"),
    ("A3", "审计导出路由", r"forgeflow\api\routers\audit.py"),
    ("B1", "Context Builder", r"forgeflow\experience\context_builder.py"),
    ("B1", "token budget", r"forgeflow\experience\token_budget.py"),
    ("B1", "context_stats 落点", r"forgeflow\observability\context_stats.py"),
    ("B11", "Cost 子包", r"forgeflow\cost"),
    ("C4", "SLO 实现", r"forgeflow\observability\slo.py"),
    ("D2", "metrics_source 抽象", r"forgeflow\observability\metrics_source.py"),
    ("C8", "可信基线", r"forgeflow\skills\trust_baseline.py"),
    ("B5", "运行时 RBAC 闸门", r"forgeflow\runtime\gate.py"),
    ("C2", "事件驱动 dispatcher", r"forgeflow\events\dispatcher.py"),
    ("C4b", "多模态 attachments", r"forgeflow\runtime\attachments.py"),
    ("INC2-27", "INC2 运行手册", r"docs\sop\07-RUNBOOK-INC2.md"),
    ("FE", "CostView", r"frontend\src\views\CostView.tsx"),
    ("FE", "OpsView", r"frontend\src\views\OpsView.tsx"),
    ("FE", "OverviewView", r"frontend\src\views\OverviewView.tsx"),
    ("FE", "roleConfig", r"frontend\src\home\roleConfig.ts"),
    ("FE", "api hooks", r"frontend\src\api\hooks.ts"),
    ("FE", "e2e 回归脚本", r"frontend\scripts\e2e_verify.py"),
    ("QA", "Hero 复验截图", r"docs\sop\shots\home_hero_fixed.png"),
]

# (分类, 项, 搜索根, 正则, 排除文件正则)
CALLS = [
    ("A1", "evolution→审批 有生产调用方", r"forgeflow",
     r"submit_advice_for_approval", r"test_"),
    ("A3", "/audit/export 路由存在", r"forgeflow",
     r"@router\.get\(\"/export\"|/audit/export", r"test_"),
    ("B1", "build_context 有生产调用方", r"forgeflow",
     r"_build_run_context|build_context\(", r"test_"),
    ("C8", "trust_baseline 挂在治理闸门", r"forgeflow",
     r"verify_trust_baseline|trust_baseline", r"test_"),
    ("B5", "orchestrator 复核 RBAC", r"forgeflow",
     r"check_tool_permission", r"test_"),
    ("B2", "conditions 新算子被 policy_engine 用", r"forgeflow",
     r"governance\.conditions|from forgeflow\.governance import conditions", r"test_"),
    ("B4", "dedup 被 extractor 调用", r"forgeflow",
     r"dedup", r"test_"),
    ("C2", "dispatcher 有生产调用方", r"forgeflow",
     r"EventDispatcher|dispatcher", r"test_"),
    ("C4b", "attachments 有生产调用方", r"forgeflow",
     r"attachments", r"test_"),
    ("RBAC", "marketplace POST 已登记", r"forgeflow",
     r"\(\"POST\",\s*\"/marketplace\"\)", r"test_"),
    ("FE", "useHomeKpis 已实现", r"frontend\src",
     r"useHomeKpis", r""),
]


def walk_files(base: str, exts: tuple[str, ...]) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in
                       {"__pycache__", "node_modules", ".git", "dist", ".pytest_cache"}]
        for f in filenames:
            if f.endswith(exts):
                out.append(os.path.join(dirpath, f))
    return out


lines: list[str] = []

lines.append("=" * 78)
lines.append("一、文件是否存在")
lines.append("=" * 78)
for cat, item, rel in FILES:
    p = os.path.join(FF, rel)
    ok = os.path.exists(p)
    kind = "DIR " if os.path.isdir(p) else "FILE"
    lines.append(f"[{'OK ' if ok else 'MISS'}] {cat:8s} {item:28s} {kind} {rel}")

lines.append("")
lines.append("=" * 78)
lines.append("二、是否有生产调用方（排除 tests/）")
lines.append("=" * 78)
for cat, item, rel_base, pattern, exclude in CALLS:
    base = os.path.join(FF, rel_base)
    rx = re.compile(pattern)
    ex = re.compile(exclude) if exclude else None
    hits: list[str] = []
    for f in walk_files(base, (".py", ".ts", ".tsx")):
        rel = os.path.relpath(f, FF)
        if "tests" + os.sep in rel or rel.startswith("tests" + os.sep):
            continue
        if ex and ex.search(rel):
            continue
        try:
            txt = io.open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for m in rx.finditer(txt):
            ln = txt[: m.start()].count("\n") + 1
            hits.append(f"{rel}:{ln}")
            break
    status = "OK " if hits else "NO "
    lines.append(f"[{status}] {cat:8s} {item}")
    for h in hits[:4]:
        lines.append(f"          -> {h}")

with io.open(os.path.join(ROOT, "_goal_verify.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("verify done")
