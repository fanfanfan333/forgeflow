import os, sys, json, socket, subprocess, platform, traceback, time

ROOT = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台"
LOG = os.path.join(ROOT, ".probe.txt")

def w(s=""):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(str(s) + "\n")

open(LOG, "w", encoding="utf-8").close()

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

def section(name):
    w("\n=== " + name + " ===")

try:
    section("BASIC")
    w(f"{platform.system()} {platform.release()} {platform.machine()} | py={sys.version.split()[0]} | cpu={os.cpu_count()}")

    section("MEM/DISK")
    try:
        import ctypes
        class MSX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MSX(); m.dwLength = ctypes.sizeof(MSX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        w(f"RAM total={m.ullTotalPhys/2**30:.1f}GB free={m.ullAvailPhys/2**30:.1f}GB load={m.dwMemoryLoad}%")
        for d in ("C:\\", "D:\\"):
            fr = ctypes.c_ulonglong(0); tt = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(d, ctypes.byref(fr), ctypes.byref(tt), None)
            w(f"{d} total={tt.value/2**30:.0f}GB free={fr.value/2**30:.1f}GB")
    except Exception:
        w("[EXC]\n" + traceback.format_exc())

    section("DOCKER")
    w("server: " + run(["docker", "version", "--format", "{{.Server.Version}}"]))
    w(run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Image}}|{{.Status}}"]))

    section("OLLAMA")
    w(run(["ollama", "list"]))
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as r:
            w("api/tags: " + r.read().decode("utf-8", "replace")[:1200])
    except Exception as ex:
        w(f"api/tags: [EXC] {ex}")

    section("NODE")
    w("node: " + run(["node", "-v"]))
    w("npm:  " + run(["npm", "-v"]))

    section("PORTS")
    for port in (5432, 8000, 8001, 8501, 5173, 11434, 3000, 9000):
        s = socket.socket(); s.settimeout(0.5)
        r = s.connect_ex(("127.0.0.1", port)); s.close()
        w(f"  {port}: {'OPEN' if r == 0 else 'closed'}")

    section("FRONTEND SRC")
    fe = os.path.join(ROOT, "ForgeFlow-main", "frontend")
    for dp, dn, fn in os.walk(fe):
        dn[:] = [d for d in dn if d not in ("node_modules", ".git", "dist")]
        rel = os.path.relpath(dp, fe)
        if rel.count(os.sep) > 2:
            dn[:] = []; continue
        w(f"[{rel}] " + ", ".join(sorted(fn)[:30]))
    w("node_modules present: " + str(os.path.isdir(os.path.join(fe, "node_modules"))))

    section("FORGEFLOW PKG")
    pk = os.path.join(ROOT, "ForgeFlow-main", "forgeflow")
    for dp, dn, fn in os.walk(pk):
        dn[:] = [d for d in dn if d != "__pycache__"]
        rel = os.path.relpath(dp, pk)
        w(f"[forgeflow/{rel}] " + ", ".join(sorted(fn)[:40]))

    section("TESTS")
    ts = os.path.join(ROOT, "ForgeFlow-main", "tests")
    for dp, dn, fn in os.walk(ts):
        dn[:] = [d for d in dn if d != "__pycache__"]
        rel = os.path.relpath(dp, ts)
        w(f"[tests/{rel}] " + ", ".join(sorted(fn)[:40]))

    section("MANAGED PY ENVS")
    envs = r"C:\Users\18769\.workbuddy\binaries\python\envs"
    w(f"exists={os.path.isdir(envs)} list={os.listdir(envs) if os.path.isdir(envs) else []}")
    py = r"C:\Users\18769\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    w("fastapi: " + run([py, "-c", "import fastapi;print(fastapi.__version__)"]))
    w("langgraph: " + run([py, "-c", "import langgraph;print('ok')"]))
    w("sqlalchemy: " + run([py, "-c", "import sqlalchemy;print(sqlalchemy.__version__)"]))
    w("psycopg: " + run([py, "-c", "import psycopg;print(psycopg.__version__)"]))
    w("pydantic: " + run([py, "-c", "import pydantic;print(pydantic.VERSION)"]))
    w("ollama_py: " + run([py, "-c", "import ollama;print('ok')"]))

    w("\n[COMPLETE]")
except Exception:
    w("\n[FATAL]\n" + traceback.format_exc())
