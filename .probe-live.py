import os, json, socket, subprocess, shutil, ctypes, platform

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台"
FE = os.path.join(ROOT, "ForgeFlow-main", "frontend")
LOG = os.path.join(ROOT, ".probe-live.txt")
open(LOG, "w", encoding="utf-8").close()

def w(s=""):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")

def run(args, t=25):
    try:
        p = subprocess.run(args, capture_output=True, timeout=t)
        o = p.stdout.decode("utf-8", "replace").strip()
        e = p.stderr.decode("utf-8", "replace").strip()
        return (o + ("\n[ERR]" + e if e else "")).strip() or "(no output)"
    except FileNotFoundError:
        return "[NOT FOUND]"
    except Exception as ex:
        return f"[EXC] {type(ex).__name__}: {ex}"

# 1) 内存
w("=== MEM ===")
try:
    class MSX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MSX(); m.dwLength = ctypes.sizeof(MSX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    w(f"RAM total={m.ullTotalPhys/2**30:.1f}GB avail={m.ullAvailPhys/2**30:.2f}GB load={m.dwMemoryLoad}%")
except Exception as ex:
    w(f"[EXC] {ex}")

# 2) docker 镜像（能否复用 postgres:16-alpine）
w("\n=== DOCKER IMAGES ===")
w(run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}|{{.Size}}"], 40))
w("--- running ---")
w(run(["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"], 40))

# 3) 端口
w("\n=== PORTS ===")
for p in (5432, 5433, 8000, 8010, 8001, 5173, 11434):
    s = socket.socket(); s.settimeout(0.5)
    r = s.connect_ex(("127.0.0.1", p)); s.close()
    w(f"  {p}: {'OPEN' if r==0 else 'closed'}")

# 4) 前端依赖
w("\n=== FRONTEND ===")
w(f"node_modules: {os.path.isdir(os.path.join(FE,'node_modules'))}")
w(f"dist:         {os.path.isdir(os.path.join(FE,'dist'))}")
if os.path.isdir(os.path.join(FE, "dist")):
    w("dist files: " + str(sorted(os.listdir(os.path.join(FE, "dist")))[:12]))

# 5) venv / 关键包
w("\n=== VENV ===")
VPY = r"C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"
w(f"venv python exists: {os.path.isfile(VPY)}")
for mod in ["fastapi", "uvicorn", "sqlalchemy", "asyncpg", "psycopg", "alembic", "pgvector", "langgraph", "pytest"]:
    w(f"  {mod}: " + run([VPY, "-c", f"import {mod};print(getattr({mod},'__version__','ok'))"], 40))

# 6) Ollama 模型（磁盘探测，不启动进程）
w("\n=== OLLAMA MODELS (disk) ===")
for base in [r"C:\Users\18769\.ollama\models", os.path.join(os.environ.get("USERPROFILE",""), ".ollama", "models"),
             r"D:\.ollama\models"]:
    mdir = os.path.join(base, "manifests")
    if os.path.isdir(mdir):
        found = []
        for dp, dn, fn in os.walk(mdir):
            for f in fn:
                found.append(os.path.relpath(os.path.join(dp, f), mdir).replace("\\", "/"))
        w(f"{base} -> {found[:20]}")
    else:
        w(f"{base} -> (no manifests)")

# 7) Playwright / Chrome 可用性（前端实访验证用）
w("\n=== BROWSER ===")
cands = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
         r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
         r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]
for c in cands:
    w(f"  {c}: {os.path.isfile(c)}")
w("  playwright pkg: " + str(os.path.isdir(os.path.join(FE, "node_modules", "playwright")) or
                             os.path.isdir(os.path.join(FE, "node_modules", "@playwright"))))

w("\n[DONE]")
