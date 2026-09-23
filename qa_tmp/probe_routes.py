"""WS-C route + RBAC probe. Read-only: imports the app factory objects only."""
import sys, os, json

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from forgeflow.api.main import app
from forgeflow.rbac.policies import ROUTE_PERMISSION_MAP, ROLE_PERMISSIONS
from forgeflow.middleware import auth as authmod
from forgeflow.middleware.auth import RBACMiddleware

# 1) enumerate (method, path) from openapi
spec = app.openapi()
routes = []
for path, item in spec["paths"].items():
    for method in item.keys():
        m = method.upper()
        if m in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"):
            routes.append((m, path))
routes.sort()
print(f"TOTAL_ROUTES={len(routes)}")

# also raw app.routes for sanity
raw = [(getattr(r, "methods", None), getattr(r, "path", None)) for r in app.routes]
print(f"RAW_APP_ROUTES={len(raw)}")

# 2) use the REAL middleware logic (exact startswith semantics)
def resolve(method, path):
    perm = RBACMiddleware._resolve_permission(method, path)  # staticmethod
    return perm if perm and perm[0] else None

op = getattr(authmod, "_OPEN_PATHS", None)
opref = getattr(authmod, "_OPEN_PREFIXES", None)
print(f"OPEN_PATHS={sorted(op) if op else op}")
print(f"OPEN_PREFIXES={opref}")

def is_open(path):
    return RBACMiddleware._is_open(path)

unmapped = []
for m, path in routes:
    if path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
        continue
    if is_open(path):
        continue
    perm = resolve(m, path)
    if perm is None:
        unmapped.append((m, path))

print(f"UNMAPPED_COUNT={len(unmapped)}")
for u in unmapped:
    print("UNMAPPED:", u)

print(f"MAP_ENTRIES={len(ROUTE_PERMISSION_MAP)}")
print("ROLES=" + ",".join(sorted(ROLE_PERMISSIONS.keys())))

print("\n--- ALL ROUTES (method path -> perm) ---")
for m, path in routes:
    if path in ("/openapi.json", "/docs", "/redoc"):
        continue
    tag = "OPEN" if is_open(path) else (resolve(m, path) or "UNMAPPED")
    print(f"{m:6} {path:55} {tag}")

print("\n--- marketplace checks ---")
for m, path in routes:
    if "marketplace" in path or "cost" in path or "skills" in path:
        print(f"{m:6} {path:55} -> {resolve(m, path)}")
