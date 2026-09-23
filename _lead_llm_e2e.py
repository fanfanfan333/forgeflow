"""主理人独立验证：Agent 执行主链路是否真的用了 Ollama（qwen2.5vl:3b）。

要点：
  * 真 lifespan 起服务（STORAGE_BACKEND=memory + 死端口 PG），和真实部署同一条路径。
  * 通过 HTTP `POST /tasks` 提交任务 → 轮询 `/runs/{id}` 到终态。
  * 断言三件事（缺一不可）：
      1) `llm.models` 里是真 ollama（llm_type/provider 不是 mock）
      2) total_tokens > 0（真调用，不是空账）
      3) 步骤序列 ≠ 硬编码剧本 _DEFAULT_STEPS（计划确实来自 LLM）

只读用途。结果写 _lead_llm_e2e.txt (UTF-8)。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "ForgeFlow-main"))

os.environ["STORAGE_BACKEND"] = "memory"
os.environ["POSTGRES_URL"] = "postgresql://nobody:nobody@127.0.0.1:59999/none"
os.environ["POSTGRES_SYNC_URL"] = "postgresql+psycopg://nobody:nobody@127.0.0.1:59999/none"
os.environ["EMBEDDING_PROVIDER"] = "mock"
os.environ["LLM_PROVIDER"] = "ollama"
os.environ["OLLAMA_MODEL"] = "qwen2.5vl:3b"
os.environ["AGENT_RUNTIME_MODE"] = "llm"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "forgeflow-dev"
os.environ["API_SECRET_KEY"] = "lead-probe-secret"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

out: list[str] = []


def log(msg: str = "") -> None:
    out.append(str(msg))


def main() -> None:
    import httpx
    from fastapi.testclient import TestClient

    log("=== 0) Ollama daemon 存活性（直连 HTTP，不走 CLI） ===")
    try:
        r = httpx.get("http://127.0.0.1:11434/api/tags", timeout=10.0)
        tags = [m.get("name") for m in (r.json().get("models") or [])]
        log(f"status={r.status_code} models={tags}")
    except Exception as exc:  # noqa: BLE001
        log(f"Ollama UNREACHABLE: {type(exc).__name__}: {exc}")

    from forgeflow.runtime.orchestrator import _DEFAULT_STEPS, resolve_agent_runtime_mode

    log()
    log(f"resolve_agent_runtime_mode() = {resolve_agent_runtime_mode()}")
    log(f"hard-coded deterministic steps = {_DEFAULT_STEPS}")

    from forgeflow.api.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        log()
        log("=== 1) login (offline credential store, pool=None) ===")
        lr = client.post(
            "/auth/login", json={"user_id": "admin", "password": "forgeflow-dev"}
        )
        log(f"status={lr.status_code}")
        if lr.status_code != 200:
            log(f"body={lr.text[:400]}")
            return
        token = lr.json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}

        intent = f"分析 {uuid.uuid4().hex[:6]} 客户的续约风险，给出分数并产出一份简短报告"
        log()
        log(f"=== 2) POST /tasks intent={intent!r} ===")
        tr = client.post("/tasks", json={"intent": intent}, headers=h)
        log(f"status={tr.status_code}")
        if tr.status_code not in (200, 201, 202):
            log(f"body={tr.text[:600]}")
            return
        body = tr.json()
        run_id = body.get("run_id") or body.get("id") or body.get("task_id")
        log(f"run_id={run_id}")

        deadline = time.time() + 180
        detail: dict = {}
        while time.time() < deadline:
            rr = client.get(f"/runs/{run_id}", headers=h)
            if rr.status_code == 200:
                detail = rr.json()
                if detail.get("status") in ("completed", "failed"):
                    break
            time.sleep(2.0)

        log()
        log("=== 3) run detail ===")
        log(f"status   = {detail.get('status')}")
        log(f"mode     = {detail.get('runtime_mode')}")
        log(f"tokens   = {detail.get('total_tokens')}")
        log(f"cost_usd = {detail.get('total_cost_usd')}")
        steps = detail.get("steps") or []
        log(f"steps({len(steps)}) = {[s.get('tool') if isinstance(s, dict) else s for s in steps]}")
        log(f"errors   = {detail.get('errors')}")

        llm = detail.get("llm") or {}
        log()
        log("=== 4) llm runtime meta ===")
        log(json.dumps(llm, ensure_ascii=False, indent=2)[:2500])

        models = llm.get("models") or []
        provider_names = [
            (m.get("provider") or m.get("llm_type") or "") for m in models if isinstance(m, dict)
        ]
        tokens = int(detail.get("total_tokens") or 0)
        tool_seq = [s.get("tool") for s in steps if isinstance(s, dict)]
        deterministic_seq = list(_DEFAULT_STEPS)

        log()
        log("=== 5) VERDICT ===")
        real = provider_names and all("mock" not in str(p).lower() for p in provider_names)
        log(f"  [1] 模型身份真实（非 mock）: {real}  providers={provider_names}")
        log(f"  [2] 真实 token 计费 > 0    : {tokens > 0}  tokens={tokens}")
        log(f"  [3] 计划来自 LLM（≠剧本）  : {tool_seq != deterministic_seq}")
        log(f"        llm plan      = {tool_seq}")
        log(f"        deterministic = {deterministic_seq}")
        log(f"  [4] 无降级标记            : {llm.get('degraded') is None}  degraded={llm.get('degraded')!r}")
        log(f"  >>> OVERALL: {'PASS' if (real and tokens > 0 and tool_seq != deterministic_seq) else 'FAIL'}")


try:
    main()
except Exception as exc:  # pragma: no cover
    import traceback

    log(f"PROBE CRASHED: {type(exc).__name__}: {exc}")
    log(traceback.format_exc()[-3000:])
finally:
    with open(os.path.join(HERE, "_lead_llm_e2e.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("lead llm e2e done")
