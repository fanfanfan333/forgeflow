import json, urllib.request, os

LOG = r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\.probe-ollama.txt"
open(LOG, "w", encoding="utf-8").close()
def w(s=""):
    with open(LOG, "a", encoding="utf-8") as f: f.write(str(s) + "\n")

w("=== OLLAMA api/tags ===")
try:
    with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=8) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    models = data.get("models", [])
    if not models:
        w("(no models installed)")
    for m in models:
        w(f"  {m.get('name')}  size={round(m.get('size',0)/2**30,2)}GB  family={m.get('details',{}).get('family')}  params={m.get('details',{}).get('parameter_size')}")
except Exception as ex:
    w(f"[EXC] {type(ex).__name__}: {ex}")

w("\n=== OLLAMA version ===")
try:
    with urllib.request.urlopen("http://localhost:11434/api/version", timeout=8) as r:
        w(r.read().decode("utf-8", "replace"))
except Exception as ex:
    w(f"[EXC] {ex}")

w("\n=== ollama models dir walk ===")
base = r"C:\Users\18769\.ollama"
if os.path.isdir(base):
    for dp, dn, fn in os.walk(base):
        depth = dp[len(base):].count(os.sep)
        if depth > 3:
            dn[:] = []; continue
        w(f"[{os.path.relpath(dp, base)}] dirs={dn[:6]} files={fn[:6]}")
else:
    w("no ~/.ollama")
