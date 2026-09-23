import os, sys, json, socket, subprocess, platform, traceback, urllib.request

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台"
LOG = os.path.join(ROOT, ".probe2.txt")
open(LOG, "w", encoding="utf-8").close()

def w(s=""):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")

def run(args, timeout=15):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        o = p.stdout.decode("utf-8", "replace").strip()
        e = p.stderr.decode("utf-8", "replace").strip()
        return (o + ("\n[ERR] " + e if e else "")).strip() or "(no output)"
    except FileNotFoundError:
        return "[NOT FOUND]"
    except Exception as ex:
        return f"[EXC] {type(ex).__name__}: {ex}"

# 1) Ollama: 只走 HTTP，不启动二进制
w("=== OLLAMA HTTP ===")
try:
    with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
        for m in data.get("models", []):
            w(f"  {m.get('name')}  size={round(m.get('size',0)/2**30,2)}GB")
except Exception as ex:
    w(f"  [EXC] {type(ex).__name__}: {ex}")

# 2) Node / npm
w("\n=== NODE ===")
w("node: " + run(["node", "-v"]))
w("npm:  " + run(["npm", "-v"]))

# 3) 端口
w("\n=== PORTS ===")
for port in (5432, 5433, 8000, 8001, 8501, 5173, 3000, 9000, 11434):
    s = socket.socket(); s.settimeout(0.5)
    r = s.connect_ex(("127.0.0.1", port)); s.close()
    w(f"  {port}: {'OPEN' if r == 0 else 'closed'}")

# 4) forgeflow 包结构
w("\n=== FORGEFLOW PKG ===")
pk = os.path.join(ROOT, "ForgeFlow-main", "forgeflow")
for dp, dn, fn in os.walk(pk):
    dn[:] = [d for d in dn if d != "__pycache__"]
    rel = os.path.relpath(dp, pk).replace("\\", "/")
    w(f"[forgeflow/{rel}] ({len(fn)}) " + ", ".join(sorted(fn)[:40]))

# 5) 前端 src
w("\n=== FRONTEND SRC ===")
fe = os.path.join(ROOT, "ForgeFlow-main", "frontend")
for dp, dn, fn in os.walk(fe):
    dn[:] = [d for d in dn if d not in ("node_modules", ".git", "dist")]
    rel = os.path.relpath(dp, fe).replace("\\", "/")
    if rel.count("/") > 2:
        dn[:] = []; continue
    w(f"[{rel}] ({len(fn)}) " + ", ".join(sorted(fn)[:30]))
w("node_modules: " + str(os.path.isdir(os.path.join(fe, "node_modules"))))

# 6) tests
w("\n=== TESTS ===")
ts = os.path.join(ROOT, "ForgeFlow-main", "tests")
for dp, dn, fn in os.walk(ts):
    dn[:] = [d for d in dn if d != "__pycache__"]
    rel = os.path.relpath(dp, ts).replace("\\", "/")
    w(f"[tests/{rel}] ({len(fn)}) " + ", ".join(sorted(fn)[:40]))

w("\n[COMPLETE]")
