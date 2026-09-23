"""QA D1.3/D1.4 — fail-closed route coverage + prefix resolution probe.

Enumerates app.openapi()["paths"] (method, templated path) and applies the SAME
longest-prefix + open-path rules as RBACMiddleware. Reports the diff set that is
"neither mapped nor open" (expect EMPTY).
"""
import json
import sys

sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")

from forgeflow.api.main import app
from forgeflow.middleware.auth import RBACMiddleware
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP

paths = app.openapi().get("paths", {})
pairs = []
for p, item in paths.items():
    for method, op in item.items():
        if method.lower() not in ("get", "post", "put", "patch", "delete"):
            continue
        pairs.append((method.upper(), p))

open_list, mapped_list, unmapped = [], [], []
for method, path in sorted(pairs):
    if RBACMiddleware._is_open(path):
        open_list.append((method, path))
        continue
    action, resource = RBACMiddleware._resolve_permission(method, path)
    if action:
        mapped_list.append((method, path, f"{action}:{resource}"))
    else:
        unmapped.append((method, path))

print("=== openapi path-items:", len(paths), "| (method,path) pairs:", len(pairs))
print("=== OPEN (bypassed by _is_open):", len(open_list))
for mp in open_list:
    print("   OPEN  ", mp)
print("=== MAPPED:", len(mapped_list))
print("=== UNMAPPED (neither mapped nor open)  [EXPECT 0]:", len(unmapped))
for mp in unmapped:
    print("   UNMAPPED**", mp)

print("\n--- prefix resolution spot-checks (D1.4) ---")
for m, p in [
    ("POST", "/marketplace/skills/publish"),
    ("POST", "/marketplace/skills/abc/install"),
    ("POST", "/marketplace/skills/abc/rate"),
    ("POST", "/marketplace/templates/refresh"),
    ("POST", "/marketplace"),
]:
    print(f"   {m} {p} -> {RBACMiddleware._resolve_permission(m, p)}")

with open(r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\qa_tmp\route_probe.json", "w", encoding="utf-8") as fh:
    json.dump({
        "path_items": len(paths), "pairs": len(pairs),
        "open": open_list, "mapped": mapped_list, "unmapped": unmapped,
    }, fh, ensure_ascii=False, indent=2)
