import sys
sys.path.insert(0, r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main")
from forgeflow.api.main import app
import forgeflow.api.main as m
print("module file:", m.__file__)
print("num app.routes:", len(app.routes))
for r in app.routes:
    print("ROUTE", type(r).__name__, getattr(r, "path", None), getattr(r, "methods", None))
try:
    paths = list(app.openapi().get("paths", {}).keys())
    print("openapi paths count:", len(paths))
    for p in sorted(paths):
        print("OAPI", p)
except Exception as e:
    print("openapi error:", repr(e))
