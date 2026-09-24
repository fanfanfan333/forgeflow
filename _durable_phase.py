"""U3 —— 真库 Durable Run 跨进程恢复验证（目标.md §A4 / §L6 U3）。

为什么分两个进程：真正的「崩溃/重启恢复」必须是**新的操作系统进程 + 新的 DB 连接**
重读 checkpoint；同进程内重建对象只能证明"内存里有"，证明不了"库里有"。

用法（由 _durable_run.py 依次拉起）：
    _durable_phase.py write <thread_id>   # 编译带 PG checkpointer 的图并跑一次
    _durable_phase.py read  <thread_id>   # 全新进程，用同一 thread_id 读回状态

stdout 结果写回 _durable_<phase>.txt (UTF-8)。
"""
from __future__ import annotations

import asyncio
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "ForgeFlow-main"))

os.environ["STORAGE_BACKEND"] = "postgres"
# 真库在宿主机 5433（agentflow_postgres 容器）；config 默认 5432 是容器内网视口。
os.environ["POSTGRES_URL"] = "postgresql+asyncpg://forgeflow:forgeflow@localhost:5433/forgeflow"
os.environ["POSTGRES_SYNC_URL"] = "postgresql+psycopg://forgeflow:forgeflow@localhost:5433/forgeflow"
os.environ["EMBEDDING_PROVIDER"] = "mock"
# 注意：不能用 mock 跑真实图 —— MockChatModel.with_structured_output() 对含必填字段的
# schema 会退化成返回 {}，supervisor 拿到 dict 后 decision.next 直接 AttributeError。
# 真链路必须走 Ollama（默认），这也是 U3 想验证的生产形态。
os.environ.setdefault("LLM_PROVIDER", "ollama")
# 本机当前只装了 qwen2.5vl:3b / qwen3:8b；config 默认 strong 已改为 qwen2.5vl:3b（旧默认 llama3.1:8b 会 404）。
# qwen3:8b 命中 _THINKING_MODEL_MARKERS，降级链必须绕开，故强/弱都用 qwen2.5vl:3b。
os.environ.setdefault("OLLAMA_MODEL", "qwen2.5vl:3b")
os.environ.setdefault("OLLAMA_MODEL_STRONG", "qwen2.5vl:3b")
os.environ["LANGCHAIN_TRACING_V2"] = "false"

PHASE = sys.argv[1] if len(sys.argv) > 1 else "write"
THREAD = sys.argv[2] if len(sys.argv) > 2 else "durable-probe-0001"
CONFIG: dict = {"configurable": {"thread_id": THREAD}}
# 写入阶段可设一个很小的 recursion_limit 来**确定性地模拟进程崩溃**：步数预算耗尽
# 时图抛 GraphRecursionError，此时 PG 里只留下"跑了一半"的 checkpoint。
# 之后用全新的进程 + 正常预算恢复，即可证明"崩溃/重启后从 checkpointer 继续"。
CRASH_LIMIT = int(os.environ.get("RECURSION_LIMIT", "0")) or None
if CRASH_LIMIT:
    CONFIG["recursion_limit"] = CRASH_LIMIT

out: list[str] = []


def log(msg: str = "") -> None:
    out.append(str(msg))


def dump_state(tag: str, state) -> None:
    log(f"--- {tag} ---")
    if state is None:
        log("  state = None")
        return
    values = getattr(state, "values", None)
    log(f"  checkpoint_id = {getattr(state, 'config', {}) and None}")
    if values is None:
        log(f"  raw = {state}")
        return
    log(f"  keys = {sorted(values.keys())}")
    log(f"  current_stage = {values.get('current_stage')!r}")
    log(f"  messages      = {len(values.get('messages') or [])}")
    log(f"  total_tokens  = {values.get('total_tokens')!r}")


async def main() -> None:
    from forgeflow.config import get_settings

    s = get_settings()
    log(f"=== phase={PHASE} thread_id={THREAD} ===")
    log(f"storage_backend = {s.storage_backend}")

    import forgeflow.graph.checkpointer as cp

    ck = await cp.get_checkpointer()
    log(f"checkpointer    = {type(ck).__module__}.{type(ck).__name__}")

    from forgeflow.graph.builder import compile_graph

    graph = await compile_graph(mcp_tools=[], workflow_type="sales_ops")
    log(f"graph compiled  = {type(graph).__name__}")

    # ---- 核验 checkpointer 真的挂在图上（不是编译了但没用） ----
    attached = getattr(graph, "checkpointer", None)
    log(f"graph.checkpointer = {type(attached).__name__ if attached else None}")

    if PHASE == "write":
        from forgeflow.state.workflow_state import WorkflowState

        initial: WorkflowState = {
            "messages": [],
            "research_results": [],
            "analysis_scores": [],
            "executed_actions": [],
            "errors": [],
            "workflow_id": f"wf-{THREAD}",
            "thread_id": THREAD,
            "current_stage": "qualify",
            "next_agent": None,
            "lead_id": "LEAD-DURABLE-1",
            "lead_data": {"company": "探针科技", "value": 120000},
            "proposal": None,
            "approval_status": None,
            "approval_token": None,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            # dry_run=False → executor 会真的产出 proposal → 图在 human_approval 前
            # interrupt_before 挂起。这正是要跨进程恢复的"未跑完"状态。
            "dry_run": False,
            "run_metadata": {"user_id": "probe", "role": "admin"},
        }
        log()
        log(f">>> invoking the graph (LLM_PROVIDER={os.environ['LLM_PROVIDER']}, dry_run=True) ...")
        final: dict = {}
        try:
            final = await graph.ainvoke(initial, config=CONFIG)
            log("invoke completed normally")
        except Exception as exc:  # noqa: BLE001 — 步数预算耗尽 == 模拟崩溃
            log(f"!! invoke aborted mid-run (simulated crash): "
                f"{type(exc).__name__}: {str(exc)[:160]}")
        log(f"invoke returned keys = {sorted(final.keys())}")
        log(f"final current_stage  = {final.get('current_stage')!r}")
        log(f"final total_tokens   = {final.get('total_tokens')!r}")
        log(f"final executed       = {final.get('executed_actions')}")
        # 是否被 interrupt_before 挂起 —— next 非空即为"没跑完"
        st = await graph.aget_state(CONFIG)
        log(f"pending next (after invoke) = {getattr(st, 'next', None)}")
        log(f"pending stage               = {(st.values or {}).get('current_stage')!r}")
        log(f"proposal present            = {bool((st.values or {}).get('proposal'))}")
    elif PHASE == "resume":
        log()
        log(">>> RESUME in a FRESH process: approve the pending human_approval ...")
        st = await graph.aget_state(CONFIG)
        dump_state("before-resume", st)
        log(f"pending next = {getattr(st, 'next', None)}")
        await graph.aupdate_state(CONFIG, {"approval_status": "approved"})
        log("  aupdate_state(approval_status='approved') OK")
        resume_cfg = {"configurable": {"thread_id": THREAD}, "recursion_limit": 50}
        after = await graph.ainvoke(None, config=resume_cfg)
        log(f"  resumed current_stage = {after.get('current_stage')!r}")
        log(f"  resumed total_tokens  = {after.get('total_tokens')!r}")
        log(f"  resumed executed      = {after.get('executed_actions')}")
        st2 = await graph.aget_state(CONFIG)
        log(f"  pending next (after)  = {getattr(st2, 'next', None)}")
    else:
        log()
        log(">>> reading state back with a FRESH process + FRESH DB connection ...")
        st = await graph.aget_state(CONFIG)
        dump_state("aget_state", st)
        # 历史 checkpoint 数量 —— 证明是"每步落盘"而不是只存了终态
        try:
            hist = [c async for c in graph.aget_state_history(CONFIG)]
            log(f"checkpoint history  = {len(hist)} 条")
            for i, h in enumerate(hist[:6]):
                log(f"  [{i}] next={(getattr(h, 'next', None))} "
                    f"stage={(h.values or {}).get('current_stage')!r}")
        except Exception as exc:  # noqa: BLE001
            log(f"  history unavailable: {type(exc).__name__}: {exc}")


try:
    # Windows + psycopg async 必须跑在 SelectorEventLoop 上（项目既有坑，见 §E）。
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(main())
except Exception as exc:  # pragma: no cover
    import traceback

    log(f"PHASE CRASHED: {type(exc).__name__}: {exc}")
    log(traceback.format_exc()[-2500:])
finally:
    io.open(os.path.join(HERE, f"_durable_{PHASE}.txt"), "w", encoding="utf-8").write(
        "\n".join(out)
    )
    print("phase done")
