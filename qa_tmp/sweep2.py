"""Second sweep: runs detail, marketplace publish, memory store+promote (live API)."""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8010"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OUT = []


def log(*a):
    OUT.append(" ".join(str(x) for x in a))


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with _opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


# login
st, tok = call("POST", "/auth/login", {"user_id": "admin", "password": "forgeflow-dev"})
token = tok["access_token"]
log(f"login status={st} token_len={len(token)}")

# submit a task + read it back
st, run = call("POST", "/tasks", {"intent": "汇总本季度客户续约风险"}, token)
rid = run["run_id"]
log(f"POST /tasks status={st} run_id={rid} run_status={run['status']}")
st, detail = call("GET", f"/runs/{rid}", token=token)
log(f"GET /runs/{{id}} status={st} -> {json.dumps(detail, ensure_ascii=False)[:320]}")

# SSE events (first lines)
try:
    req = urllib.request.Request(BASE + f"/runs/{rid}/events", method="GET")
    req.add_header("Authorization", "Bearer " + token)
    with _opener.open(req, timeout=8) as r:
        chunk = r.read(400).decode(errors="replace")
    log(f"GET /runs/{{id}}/events first bytes: {chunk[:200]!r}")
except Exception as e:
    log(f"SSE events error: {type(e).__name__}: {e}")

# skills list -> pick one -> publish to marketplace
st, skills = call("GET", "/skills", token=token)
skill_id = skills["items"][0]["id"] if skills.get("items") else None
log(f"GET /skills status={st} count={len(skills.get('items', []))} first={skill_id}")
if skill_id:
    st, pub = call(
        "POST", "/marketplace/skills/publish",
        {"skill_id": skill_id, "description": "共享测试", "shared": False},
        token=token,
    )
    log(f"POST /marketplace/skills/publish status={st} -> {json.dumps(pub, ensure_ascii=False)[:200]}")
    st, lst = call("GET", "/marketplace/skills", token=token)
    log(f"GET /marketplace/skills status={st} total={lst.get('total')}")

# memory store -> promote to team
st, mem = call("POST", "/memory", {"scope": "user", "content": "客户A偏好季度续约"}, token)
log(f"POST /memory status={st} -> {json.dumps(mem, ensure_ascii=False)[:200]}")
mid = mem.get("id") if isinstance(mem, dict) else None
if mid:
    st, pr = call("POST", f"/memory/{mid}/promote", {"to_scope": "team", "reason": "团队复用"}, token=token)
    log(f"POST /memory/{{id}}/promote status={st} -> {json.dumps(pr, ensure_ascii=False)[:200]}")

with open(r"D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\qa_tmp\sweep2_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
print("\n".join(OUT))
