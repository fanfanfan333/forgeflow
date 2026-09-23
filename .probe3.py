import os, subprocess, json
ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台"
LOG = os.path.join(ROOT, ".probe3.txt")
open(LOG, "w", encoding="utf-8").close()
def w(s=""):
    with open(LOG, "a", encoding="utf-8") as f: f.write(str(s) + "\n")
def run(a, t=20):
    try:
        p = subprocess.run(a, capture_output=True, timeout=t)
        o = p.stdout.decode("utf-8","replace").strip()
        e = p.stderr.decode("utf-8","replace").strip()
        return (o + ("\n[ERR] " + e if e else "")).strip() or "(no output)"
    except FileNotFoundError: return "[NOT FOUND]"
    except Exception as ex: return f"[EXC] {type(ex).__name__}: {ex}"

PY = r"C:\Users\18769\.workbuddy\binaries\python\versions\3.13.12\python.exe"
NODE_DIR = r"C:\Users\18769\.workbuddy\binaries\node\versions\22.22.2-3"

w("=== PY DEPS (managed 3.13.12) ===")
for mod in ["fastapi","uvicorn","pydantic","pydantic_settings","langgraph","langchain_core",
            "sqlalchemy","psycopg","asyncpg","alembic","httpx","tenacity","jwt","passlib",
            "pytest","pytest_asyncio","ruff","mypy","tiktoken","ollama","openai","pgvector","numpy"]:
    w(f"  {mod}: " + run([PY, "-c", f"import {mod}; print(getattr({mod},'__version__','ok'))"]))
w("  pip: " + run([PY, "-m", "pip", "--version"]))

w("\n=== NODE ===")
w("node bin: " + str(os.path.exists(os.path.join(NODE_DIR, "node.exe"))))
for cand in ["npm.cmd","npm","npx.cmd","corepack.cmd"]:
    w(f"  {cand}: {os.path.exists(os.path.join(NODE_DIR, cand))}")
w("  npm-cli.js: " + str(os.path.exists(os.path.join(NODE_DIR, "node_modules", "npm", "bin", "npm-cli.js"))))
w("  npm via node: " + run([os.path.join(NODE_DIR,"node.exe"), os.path.join(NODE_DIR,"node_modules","npm","bin","npm-cli.js"), "-v"]))
w("  list NODE_DIR: " + str(sorted(os.listdir(NODE_DIR))[:30]))

w("\n=== FRONTEND package.json ===")
pkg = os.path.join(ROOT, "ForgeFlow-main", "frontend", "package.json")
w(open(pkg, encoding="utf-8").read() if os.path.exists(pkg) else "MISSING")

w("\n=== root pyproject (dep section) ===")
pp = os.path.join(ROOT, "ForgeFlow-main", "pyproject.toml")
w(open(pp, encoding="utf-8").read()[:3000] if os.path.exists(pp) else "MISSING")

w("\n[COMPLETE]")
