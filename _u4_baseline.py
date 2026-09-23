"""U4 —— 真实 LLM 的「计划多样性 + 延迟/用量基线」（目标.md §L6 U4）。

与上一轮单次跑通的区别：这里做**多次采样**，回答三个问题：
  1) 不同 intent 下 LLM 产出的计划是否**真的不同**（不是恒定剧本）；
  2) 同一 intent 重复两次，计划是否**稳定**（可复现性）；
  3) plan / reflect 各自的**耗时与 token** 是多少（可引用的基线数字）。

跑真服务（memory 档 + 死端口 PG，证明离线可跑）、真 Ollama。
只读用途，结果写 _u4_baseline.txt (UTF-8)。
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
# 本机当前只有 qwen2.5vl:3b / qwen3:8b；qwen3 命中思考模型黑名单，不能用。
os.environ["OLLAMA_MODEL"] = "qwen2.5vl:3b"
os.environ["OLLAMA_MODEL_STRONG"] = "qwen2.5vl:3b"
os.environ["AGENT_RUNTIME_MODE"] = "llm"
os.environ["DEV_LOGIN_ENABLED"] = "true"
os.environ["DEV_LOGIN_PASSWORD"] = "forgeflow-dev"
os.environ["API_SECRET_KEY"] = "u4-probe-secret"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

INTENTS = [
    "分析 {} 客户的续约风险，给出分数并产出一份简短报告",
    "为 {} 整理一份竞品调研摘要，并挑出最值得跟进的三条线索",
    "核对 {} 本月的对账差异，标出需要人工复核的条目",
]

out: list[str] = []


def log(msg: str = "") -> None:
    out.append(str(msg))


def main() -> None:
    from fastapi.testclient import TestClient

    from forgeflow.api.main import app
    from forgeflow.runtime.orchestrator import _DEFAULT_STEPS

    log(f"deterministic script = {list(_DEFAULT_STEPS)}")
    log()

    with TestClient(app, raise_server_exceptions=False) as client:
        lr = client.post("/auth/login", json={"user_id": "admin", "password": "forgeflow-dev"})
        if lr.status_code != 200:
            log(f"LOGIN FAILED {lr.status_code}: {lr.text[:400]}")
            return
        h = {"Authorization": f"Bearer {lr.json()['access_token']}"}

        rows: list[dict] = []
        # 第 1 个 intent 跑两遍，用于观察"同一输入是否稳定"
        plan_specs = [0, 0, 1, 2]
        for seq, idx in enumerate(plan_specs, start=1):
            tmpl = INTENTS[idx]
            token = uuid.uuid4().hex[:6].upper()
            intent = tmpl.format(token)
            log(f"===== run #{seq} (intent #{idx}) =====")
            log(f"intent = {intent!r}")

            t0 = time.time()
            tr = client.post("/tasks", json={"intent": intent}, headers=h)
            if tr.status_code not in (200, 201, 202):
                log(f"  POST /tasks FAILED {tr.status_code}: {tr.text[:400]}")
                continue
            run_id = tr.json().get("run_id") or tr.json().get("id")
            log(f"  run_id = {run_id}")

            deadline = time.time() + 240
            detail: dict = {}
            while time.time() < deadline:
                rr = client.get(f"/runs/{run_id}", headers=h)
                if rr.status_code == 200:
                    detail = rr.json()
                    if detail.get("status") in ("completed", "failed"):
                        break
                time.sleep(2.0)
            elapsed = time.time() - t0

            llm = detail.get("llm") or {}
            steps = detail.get("steps") or []
            tool_seq = [s.get("tool") for s in steps if isinstance(s, dict)]
            rows.append(
                {
                    "seq": seq,
                    "intent_idx": idx,
                    "status": detail.get("status"),
                    "mode": detail.get("runtime_mode"),
                    "tokens": detail.get("total_tokens"),
                    "steps": tool_seq,
                    "elapsed": round(elapsed, 1),
                    "degraded": llm.get("degraded"),
                    "models": [
                        (m.get("provider") or m.get("llm_type"))
                        for m in (llm.get("models") or [])
                        if isinstance(m, dict)
                    ],
                }
            )
            log(f"  status={detail.get('status')} mode={detail.get('runtime_mode')} "
                f"tokens={detail.get('total_tokens')} elapsed={elapsed:.1f}s")
            log(f"  steps = {tool_seq}")
            log(f"  degraded = {llm.get('degraded')!r}")
            log()

        log("===== 汇总 =====")
        hdr = f"{'#':<3}{'intent':<8}{'status':<11}{'tokens':<8}{'秒':<8}plan"
        log(hdr)
        for r in rows:
            log(f"{r['seq']:<3}{r['intent_idx']:<8}{str(r['status']):<11}"
                f"{str(r['tokens']):<8}{str(r['elapsed']):<8}{r['steps']}")

        seqs = [tuple(r["steps"]) for r in rows]
        log()
        log(f"  distinct plans = {len(set(seqs))} / {len(seqs)}")
        det_tools = tuple(
            s.get("tool") if isinstance(s, dict) else s for s in _DEFAULT_STEPS
        )
        log(f"  any plan == deterministic script? {det_tools in set(seqs)}  (script={list(det_tools)})")
        same_intent = [tuple(r["steps"]) for r in rows if r["intent_idx"] == 0]
        log(f"  same-intent repeat stable? {len(set(same_intent)) == 1}  -> {same_intent}")
        toks = [r["tokens"] for r in rows if isinstance(r["tokens"], int)]
        secs = [r["elapsed"] for r in rows]
        if toks:
            log(f"  tokens   min/median/max = {min(toks)}/{sorted(toks)[len(toks)//2]}/{max(toks)}")
        if secs:
            s = sorted(secs)
            log(f"  seconds  min/median/max = {s[0]}/{s[len(s)//2]}/{s[-1]}")
        log(f"  all non-mock providers  = {all('mock' not in str(p).lower() for r in rows for p in r['models'])}")
        log(f"  all degraded=None       = {all(r['degraded'] is None for r in rows)}")
        log()
        log("RAW=" + json.dumps(rows, ensure_ascii=False))


try:
    main()
except Exception as exc:  # pragma: no cover
    import traceback

    log(f"PROBE CRASHED: {type(exc).__name__}: {exc}")
    log(traceback.format_exc()[-3000:])
finally:
    with open(os.path.join(HERE, "_u4_baseline.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("u4 baseline done")
