"""死代码审计：对关键新模块/符号，统计「自身文件之外的引用数」。
判定「落盘≠交付」：有 def/class 但仓内无外部调用方。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")
SRC = ROOT / "forgeflow"

# 模块 → 关注符号（用于判断是否被外部调用）
TARGETS: list[tuple[str, list[str]]] = [
    ("forgeflow/observability/metrics_source.py", ["get_metrics_source", "MemoryMetricsSource", "reset_metrics_source"]),
    ("forgeflow/observability/slo.py", ["SloRegistry", "SLO_TIERS", "get_slo", "slo_summary"]),
    ("forgeflow/observability/context_stats.py", ["record_context_stats", "context_stats"]),
    ("forgeflow/observability/cost_tracker.py", ["CostTracker", "calculate_cost", "count_tokens"]),
    ("forgeflow/observability/tracing_provider.py", ["get_tracer"]),
    ("forgeflow/cost/budget_service.py", ["BudgetService"]),
    ("forgeflow/cost/degrade.py", ["trigger_degrade", "evaluate_degrade"]),
    ("forgeflow/cost/savings.py", ["compute_savings", "savings_from_totals"]),
    ("forgeflow/experience/context_builder.py", ["build_context"]),
    ("forgeflow/experience/token_budget.py", ["estimate_tokens", "TokenBudget", "trim"]),
    ("forgeflow/experience/dedup.py", ["dedup", "resolve", "DedupResult", "merge"]),
    ("forgeflow/experience/promotion.py", ["promote_memory", "promote"]),
    ("forgeflow/experience/memory_store.py", ["search_scoped_memories", "MemoryStore"]),
    ("forgeflow/skills/evolution.py", ["advise", "SkillEvolution"]),
    ("forgeflow/skills/marketplace_bridge.py", ["publish", "install", "rate", "MarketplaceBridge"]),
    ("forgeflow/skills/trust_baseline.py", ["verify_trust_baseline"]),
    ("forgeflow/runtime/gate.py", ["check_tool_permission", "evaluate_tool_call", "RuntimeGate"]),
    ("forgeflow/runtime/attachments.py", ["prepare_attachments", "extract_attachment_text", "Attachment"]),
    ("forgeflow/events/dispatcher.py", ["dispatch", "run_task"]),
    ("forgeflow/models/provider.py", ["get_model", "fallback", "MODEL_FALLBACK"]),
    ("forgeflow/governance/conditions.py", ["evaluate_condition", "Condition"]),
    ("forgeflow/governance/dlp_rules.py", ["DlpRuleSet", "load_dlp_rules"]),
    ("forgeflow/api/routers/audit.py", ["record_audit_entry", "_RING"]),
    ("forgeflow/api/routers/marketplace.py", ["router"]),
    ("forgeflow/repositories/base.py", ["CostBudgetRepository"]),
]

py_files = [p for p in SRC.rglob("*.py") if "__pycache__" not in str(p)]
sources: dict[Path, str] = {}
for p in py_files:
    try:
        sources[p] = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

# 也扫描 tests 与 alembic，用于区分「只有测试在用」
extra_files: list[Path] = []
for sub in ("tests", "alembic"):
    d = ROOT / sub
    if d.exists():
        extra_files += [p for p in d.rglob("*.py") if "__pycache__" not in str(p)]
extra_src: dict[Path, str] = {}
for p in extra_files:
    try:
        extra_src[p] = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

report: list[str] = []
for rel, symbols in TARGETS:
    own = ROOT / rel
    if not own.exists():
        report.append(f"MISSING  {rel}")
        continue
    for sym in symbols:
        # 只匹配「作为完整标识符出现」
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(sym) + r"(?![A-Za-z0-9_])")
        src_hits = [str(p.relative_to(ROOT)) for p, t in sources.items() if p != own and pat.search(t)]
        test_hits = [str(p.relative_to(ROOT)) for p, t in extra_src.items() if pat.search(t)]
        # 排除纯定义行（def/class）在同文件
        verdict = "OK   " if src_hits else ("TEST-ONLY" if test_hits else "DEAD ")
        report.append(
            f"{verdict} {rel}::{sym} | src_refs={len(src_hits)} {src_hits[:4]} | test_refs={len(test_hits)} {test_hits[:3]}"
        )

out = ROOT.parent / "qa_tmp" / "_deadcode_audit.txt"
out.write_text("\n".join(report), encoding="utf-8")
print("\n".join(report))
