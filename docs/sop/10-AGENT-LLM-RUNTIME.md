# 10 · Agent LLM Runtime — 让 Agent 真正使用 Ollama（INC4 §A）

> 负责人：工程师 寇豆码（software-engineer） · 任务：`T0 [P0]`
> 结论：**已跑通**。`POST /tasks` 的默认执行路径现在真的调用配置的 provider（实测 `ChatOllama` / `qwen2.5vl:3b`），计划由模型产出、结果由模型反思、真实 token 落账。
> 本文所有数字与输出行均来自本机真实运行（venv Python 3.13.14，Ollama 11434）。

---

## 1. 问题（为什么要做这件事）

用户最高优先级原话：

> 「我们需要先在这个项目中把 ollama 真正的跑通，让 agent 真正使用它，在这个前提下我们再来看 fallback 的事情。」
> 「你不要绕过这个问题，直接通过修改测试用例让它通过，你要去解决这个问题。」

改造前的真问题（读盘复核确认）：

- `forgeflow/runtime/orchestrator.py` 的 `_default_executor` 是 `POST /tasks` → `run_task` 的默认 Agent 执行路径；
- 它遍历**硬编码常量** `_DEFAULT_STEPS`（`research.search` / `data.query` / `code.run`），每步只做 ① RBAC ② `PolicyEngine.evaluate_tool_call` ③ `await asyncio.sleep(0)` ④ `steps.append({..., "status": "ok"})`；
- **从不调用 LLM、从不规划、从不反思**，步骤无条件标记成功；
- 全仓 `get_model()` 的真实调用方只有 5 处（`graph/builder.py`、`experience/dedup.py`、`multimodal/images.py`、`skills/candidate_compiler.py`、`skills/evolution.py`），**Agent 执行主链路不在其中**。

⇒ **Ollama 配了，但 Agent 根本没用它。**

---

## 2. 改动清单（全部 additive，旧路径原样可达）

| 文件 | 类型 | 说明 |
|---|---|---|
| `forgeflow/runtime/llm_planner.py` | **新增** | `LLMPlanner`：`plan()` / `reflect()`，各 **1 次**批量调用；结构化 JSON 解析 + 宽松兜底；计划/反思缓存；未知工具过滤；usage 抽取 |
| `forgeflow/runtime/orchestrator.py` | 修改（+277/−4） | `resolve_agent_runtime_mode()`；`_llm_executor()`；`run_task()` 按模式选执行器；replan 复用同一执行器；`RunRecord.runtime_mode`；`RunHandle.detail` 增补 `runtime_mode` / `llm` |
| `tests/unit/test_runtime_llm_agent.py` | **新增** | 10 项回归（模式解析 / 离线不变 / 计划来自模型 / 反思判失败 / HITL 拦截 / 幻觉工具丢弃 / token 落账 / 一次批量调用） |

`_default_executor` **未删改**，`auto + mock` 时仍走它 → 离线档行为不变。
`gate.py` / `events.py` 只读未动；`config.py` / `provider.py` / `.env*` 未动（属 engineer-2）。

### 执行链（目标架构对齐）

```
Agent → Provider ┬→ Ollama → 本地 qwen2.5vl:3b / qwen3:8b → 真实 LLM   ← 实测走这条
                 └→ Mock   → 仅供开发/测试
        ↓ Planning（Supervisor 1 次批量调用产出整个计划）
        ↓ 每步 RBAC + PolicyEngine(HITL) 闸门 → 执行
        ↓ Reflection（Supervisor 1 次批量调用判定成败）
        ↓ 真实 token usage → CostTracker → RunRecord → /cost/board、/metrics
```

### 开关（settings）

```python
agent_runtime_mode: str = "auto"   # auto | llm | deterministic
```
- `auto` ⇒ `settings.llm_provider != "mock"` 时走 `llm`，否则走 `deterministic`；
- 读取用 `getattr(settings, "agent_runtime_mode", "auto")` 安全兜底（字段未落地也不崩）；
- 显式 `agent_runtime_mode="llm"` + `LLM_PROVIDER=mock` 也能跑通（该路径可被单测覆盖，不依赖真 Ollama）。

---

## 3. 实证 A —— 真服务 + 真 `POST /tasks`（验收核心）

命令（本机，venv python）：

```powershell
cd "D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
$env:NO_PROXY="127.0.0.1,localhost"
& "C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe" ..\qa_tmp\eng_llm_e2e_http.py
```

该脚本用 `subprocess` 拉起**真 uvicorn 服务**（`forgeflow.api.main:app`，127.0.0.1:**8013**），
环境：`STORAGE_BACKEND=memory LLM_PROVIDER=ollama OLLAMA_MODEL=_STRONG=qwen2.5vl:3b AGENT_RUNTIME_MODE=llm`；
然后 mint 一枚真 admin JWT，打真 `POST /tasks`，再 `GET /runs/{id}` / `/cost/board` / `/metrics/`。

真实输出关键行（`qa_tmp/eng_llm_e2e_http_out.txt`）：

```
OLLAMA /api/ps (before): [{"name": "qwen2.5vl:3b", "size_vram": 2898274549}]
/health 200: {'status': 'healthy', 'database': 'unavailable', 'graph': 'compiled'}  (after 4.6s)
POST /tasks  intent='分析华南区 Q2 客户流失情况并给出三条挽留建议'
  HTTP 200  wall_time_s=1.58
  runtime_mode = llm
  total_tokens = 528
  detail.llm.models = [{"slot":"strong","class":"ChatOllama","llm_type":"chat-ollama",
                        "model":"qwen2.5vl:3b","base_url":"http://localhost:11434"}, ...]
  detail.llm.plan   = {"source":"llm","steps":["research.search"],
                       "usage":{"input_tokens":306,"output_tokens":31}}
  detail.llm.reflection = {"success":true,"score":0.8,"source":"llm",
                           "usage":{"input_tokens":137,"output_tokens":54}}
OLLAMA /api/ps (after POST): [{"name": "qwen2.5vl:3b", "size_vram": 2898274549}]
GET /runs/{id}   -> 200  steps=[{"tool":"research.search", ...,"status":"ok"}]
GET /cost/board  -> 200  {"has_data":false,"currency":"CNY","total_spent":0.0,"level":"ok","budgets":[]}
GET /metrics/    -> 200  {"total_runs":1,"terminal_runs":1,"success_rate":1.0,
                          "has_data":true,"has_cost":false,"source":"hub_runs"}
```

**证明要点**

1. **真的走到了 Ollama**：`/api/ps` 在 POST 前后都显示 `qwen2.5vl:3b` 已加载进 VRAM（2.90 GB）——这是 Ollama 服务端独立证据，不是自报。
2. **没有静默降级到 mock**：`detail.llm.models[].class == "ChatOllama"`、`llm_type == "chat-ollama"`、`base_url == http://localhost:11434`。mock 的话这里会写 `MockChatModel` / `mock`（见 §5 的反例）。
3. **计划来自模型**（非硬编码）：见 §4 比对。
4. **真实 token 落账**：`total_tokens=528`（计划 337 + 反思 191），且 `/metrics/` 的 `has_data=true`。

---

## 4. 实证 B —— in-process `run_task` 与 `_DEFAULT_STEPS` 比对

命令：

```powershell
& "C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe" ..\qa_tmp\eng_llm_e2e_inproc.py
```

真实输出关键行（`qa_tmp/eng_llm_e2e_inproc2_out.txt`）：

```
SETTINGS: llm_provider=ollama ollama_model=qwen2.5vl:3b agent_runtime_mode=llm  EFFECTIVE MODE=llm
get_model() IDENTITY
  strong=True : class=ChatOllama  _llm_type=chat-ollama  model=qwen2.5vl:3b  base_url=http://localhost:11434
  strong=False: class=ChatOllama  _llm_type=chat-ollama  model=qwen2.5vl:3b  base_url=http://localhost:11434
RUN  status=completed  runtime_mode=llm  wall_time_s=19.12  total_tokens=463
  plan        = {"source":"llm","steps":["research.search"],"usage":{"input_tokens":246,"output_tokens":30}}
  reflection  = {"success":true,"score":0.8,"source":"llm","usage":{"input_tokens":137,"output_tokens":50}}
RUNRECORD: total_tokens=463  total_cost_usd=0.0
  cost_by_agent = {"supervisor":{"tokens":276,"cost_usd":0.0,"calls":1},
                   "reviewer":{"tokens":187,"cost_usd":0.0,"calls":1}}
PLAN vs _DEFAULT_STEPS
  _DEFAULT_STEPS = ['research.search', 'data.query', 'code.run']
  model plan     = ['research.search']
  DIFFERENT      = True
OLLAMA /api/ps (after): [{"name":"qwen2.5vl:3b","size_vram":2898274549, ...}]
```

> 注：`cost_by_agent` 的 `supervisor`（计划）+ `reviewer`（反思）= 463 = `total_tokens`。

---

## 5. 实证 C —— `qwen3:8b` 对比（如实报告）

### 5.1 单次调用能力探测（`qa_tmp/eng_ollama_probe_out.txt`）

| 模型 | 方式 | 冷/热 | 耗时 | 结果 |
|---|---|---|---|---|
| `qwen2.5vl:3b` | `bind(format=json-schema)` | 冷（含加载） | **46.36 s** | ✅ 合法 JSON，3 步计划，`usage_metadata={'input_tokens':105,'output_tokens':82}` |
| `qwen2.5vl:3b` | plain invoke | 热 | **1.11 s** | ✅ 合法 JSON，3 步计划 |
| `qwen3:8b` | `bind(format=json-schema)` | 冷（含加载） | **20.08 s** | ✅ 合法 JSON，**4 步**计划，`usage_metadata={'input_tokens':102,'output_tokens':174}` |

→ 两个模型在 `reasoning=False`（`_build_ollama_model` 默认）下都能产出合法受限 JSON，`usage_metadata` 正常（token 可落账）。
→ **模型加载占大部分耗时**：`qwen2.5vl:3b` 冷启动 46 s、热调用仅 1.1 s。

### 5.2 但 `OLLAMA_MODEL=qwen3:8b` 时出现**静默降级到 mock**（重要缺陷，已如实记录）

以 `E2E_MODEL=qwen3:8b` 复跑 in-process e2e（`qa_tmp/eng_llm_e2e_qwen3_out.txt`）：

```
get_model() IDENTITY
  strong=True : class=MockChatModel  _llm_type=mock  model=None  base_url=None     ← 降级了！
RUN  runtime_mode=llm  total_tokens=0  wall_time_s=0.09
  plan        = {"source":"fallback", ...}          ← 回落到确定性计划
  models      = [{"slot":"strong","class":"MockChatModel","llm_type":"mock",...}]
PLAN vs _DEFAULT_STEPS
  model plan     = ['research.search', 'data.query', 'code.run']   ← 完全等于 _DEFAULT_STEPS
  DIFFERENT      = False
```

**根因定位**（代码级）：`forgeflow/models/provider.py::_ollama_available()` 用
`urllib` 打 `GET {ollama_base_url}/api/tags`，**超时仅 0.5 s**（`_OLLAMA_PROBE_TIMEOUT_SECONDS = 0.5`）。
Ollama 在忙（模型加载/换页）时该探测偶发超时 ⇒ `get_model()` 判定「Ollama unreachable」⇒ 沿
`MODEL_FALLBACK_CHAIN` 落到 `mock`。**同一进程内**我用 5 s 超时探 `/api/tags` 是成功的（本文件与 §5.1 均可复现），说明是探测超时太紧，而非服务不可用。

**影响**：`runtime_mode=llm` 但实际跑在 mock 上，`total_tokens=0`、计划等于 `_DEFAULT_STEPS` —— 正是用户最担心的「静默降级、假装用了 LLM」。
**本轮处理（未越界）**：`provider.py` 属 engineer-2，我**不改**；改为在运行时把这种降级**显式暴露**——
`_llm_executor` 检测到「配置的是真实 provider 但构建出 mock」时，写 `runtime_meta["degraded"]="provider_degraded_to_mock"`、
打 `logger.error`，并发 `run.warning` 事件（含 `configured_provider` 与 `models` 身份）。
**建议 engineer-2**：把 `_OLLAMA_PROBE_TIMEOUT_SECONDS` 提到 2–3 s（或仅在「连接被拒绝」时判不可用 / 延长 TTL），即可消除该偶发降级。

### 5.3 结论

- **推荐 `qwen2.5vl:3b`**（非 thinking、非视觉任务下同样能规划、内存占用 2.9 GB、热调用 ~1 s）；
- `qwen3:8b`（thinking，5.2 GB）**单次调用本身可用且质量更高（4 步计划）**，但**经过 `get_model()` 时会被 0.5 s 探测偶发判为不可达而降级到 mock**；本机内存临界，不建议作默认规划模型。

---

## 6. 安全：LLM 路径同样逐步过闸门（硬要求）

`_llm_executor` 照抄 `_default_executor` 的顺序与事件：

```
for step in plan.steps:
    if not check_tool_permission(ctx.role, tool):   # ① RBAC 先判 → run.error + 立即返回
        ...
    decision = await PolicyEngine.evaluate_tool_call(...)  # ② 风险分级
    if decision.requires_approval:                  # ③ 高风险 → HITL 拦截，执行前中止
        ...
    await bus.emit("run.step"); await asyncio.sleep(0)
    steps.append({..., "status":"ok"}); await bus.emit("run.step.done")
```

- 计划是**不可信输出**：模型给的工具名先按「该角色可执行工具集」过滤，幻觉工具名丢弃并记入 `plan.dropped_tools`，**永不执行**；
- 单测证据：`payment.transfer`（高风险）在 LLM 路径下 `status=failed`、`steps==[]`、`approvals>=1`、事件含 `run.error`；
- 单测证据：`sales_rep` 计划里的 `analysis.score`（需 `execute:analysis`，该角色没有）被丢弃。

---

## 7. Token / 成本落账契约

- LLM 路径把每次真实调用的 usage 追加进 `TaskCreate.context["llm_usage"]`：
  `[{"agent","model"?,"input_tokens","output_tokens"}]`；
- 既有 `_record_usage()` → `CostTracker.record()` 消费它 → `RunRecord.total_tokens / total_cost_usd / cost_by_agent`；
- 实测 `cost_by_agent = {"supervisor":{tokens:276,...},"reviewer":{tokens:187,...}}`，`total_tokens=463`；
- **`total_cost_usd=0.0` 是正确且诚实的**：`forgeflow/observability/cost_tracker.py` 的定价策略规定
  `ollama/qwen/llama/...` 等本地模型单价为 0（「宁可少算也不虚增」），不伪造云端费率。
  成本管道本身可用——纯函数自检：`gpt-4o-mini` 1000+500 token → `total_cost_usd=0.00045`（见 e2e 输出末尾）。
- 因此 `/cost/board` 在纯本地模型下 `has_data=false, total_spent=0.0`（无虚增）；`/metrics/` 的 `total_runs/success_rate` 已由真实运行填充。

---

## 8. 回归证据（离线档不新增失败）

命令：

```powershell
$env:STORAGE_BACKEND="memory"; $env:LLM_PROVIDER="mock"; $env:EMBEDDING_PROVIDER="mock"
& "...\python.exe" -m pytest tests -q
```

结果（`qa_tmp/_eng_full_offline.txt`）：

```
1 failed, 697 passed, 85 warnings in 25.78s
FAILED tests/integration/test_offline_boot.py::test_postgres_profile_fails_fast_with_unreachable_postgres
```

**归因与洗清**：

- 该失败**不是本任务引入**：`tests/integration/test_offline_boot.py` 在基线提交 `ecd7c1b` 中**不存在**（`git status` 显示 `?? `，为同事新增的 lifespan 护栏用例）；
- 单独运行该文件 **2 passed**（`qa_tmp/_eng_boot_only.txt`），说明是**用例间状态隔离**导致的顺序相关偶发（`ConnectionRefusedError` 类型与期望不符），与 runtime 无关；
- 本任务作用域回归：`pytest tests -q -k "runtime or orchestrat or cost or metric or replan or validator or gate or runs"` ⇒ **145 passed, 0 failed**（`qa_tmp/_eng_runtime_scope.txt`）；
- 新增单测：`pytest tests/unit/test_runtime_llm_agent.py -q` ⇒ **10 passed**（`qa_tmp/_eng_llm_tests.txt`）。

> 基线说明：任务书给的基线是「668 collected / 1 failed（`test_provider_fallback::test_thinking_models_are_never_built`）」；
> 现全量已变为 698 collected（同事并行新增 ~30 用例），且那个 `test_provider_fallback` 用例已由 engineer-2 修复通过。
> 本轮**未新增任何失败**。

---

## 9. 复现步骤

```powershell
cd "D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main"
$PY = "C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"
$env:NO_PROXY="127.0.0.1,localhost"; $env:no_proxy="127.0.0.1,localhost"

# 0) 前置：Ollama 在线、模型已 pull
Invoke-RestMethod http://127.0.0.1:11434/api/tags        # 期望含 qwen2.5vl:3b / qwen3:8b

# 1) 单测（离线，不需要 Ollama）
$env:STORAGE_BACKEND="memory"; $env:LLM_PROVIDER="mock"; $env:EMBEDDING_PROVIDER="mock"
& $PY -m pytest tests/unit/test_runtime_llm_agent.py -q

# 2) 真 Ollama，in-process
& $PY ..\qa_tmp\eng_llm_e2e_inproc.py                    # 输出 qa_tmp/eng_llm_e2e_inproc_out.txt

# 3) 真服务 + 真 POST /tasks（8013）
& $PY ..\qa_tmp\eng_llm_e2e_http.py                      # 输出 qa_tmp/eng_llm_e2e_http_out.txt

# 4) qwen3:8b 对比
$env:E2E_MODEL="qwen3:8b"; & $PY ..\qa_tmp\eng_llm_e2e_inproc.py
```

回退（保持旧行为）：

```powershell
$env:AGENT_RUNTIME_MODE="deterministic"   # 或直接 LLM_PROVIDER=mock（auto 也会走确定性路径）
```

---

## 10. 失败与限制（如实）

1. **`qwen2.5vl:3b` 倾向产出粗粒度「单步」计划**：多次实测（含在提示词中已硬性要求「2-5 步」）仍只给 1 步 `research.search`。
   计划**确实来自模型**且 ≠ `_DEFAULT_STEPS`，但业务上偏弱。→ 后续：换更强的规划模型 / few-shot 约束 / 计划校验重问。
2. **`qwen3:8b` 经 `get_model()` 会偶发静默降级为 mock**（见 §5.2，0.5 s 探测超时）。已在运行时显式告警；
   根治需 engineer-2 调整 `provider.py` 探测超时。
3. **未接入真实 MCP 工具执行**：LLM 路径的**规划与反思是真实的**，但每个计划步骤仍是「按计划标记完成」，
   未真正调用 `forgeflow/mcp/server/tools/*` 里的工具（runtime 主链路尚未接 MCP 工具层）。
   这是**已知限制**，已在代码注释与本文标注，不宣称已实现。
4. **`_default_executor` 的 `simulate_failure` 语义**在 LLM 路径中保留（`"失败" in intent` 或 `context.simulate_failure`）。
5. `agent_runtime_mode` 字段需 engineer-2 落进 `config.py`（当前用 `getattr` 安全读取，缺失时按 `auto` 处理）。

---

## 附：证据文件索引（均在本机 `qa_tmp/`）

| 文件 | 内容 |
|---|---|
| `eng_llm_e2e_http_out.txt` | 真服务 HTTP `POST /tasks` 全量输出 |
| `eng_llm_e2e_inproc2_out.txt` | in-process qwen2.5vl:3b（计划 vs `_DEFAULT_STEPS`） |
| `eng_llm_e2e_qwen3_out.txt` | qwen3:8b 对比（mock 降级反例） |
| `eng_ollama_probe_out.txt` | 单次调用耗时 / `usage_metadata` / JSON 受限解码 |
| `_eng_full_offline.txt` | 离线全量 pytest（697 passed / 1 failed 归因） |
| `_eng_runtime_scope.txt` | 作用域回归 145 passed |
| `_eng_llm_tests.txt` | 新增单测 10 passed |
| `_eng_boot_only.txt` | 失败用例单独复跑 2 passed（洗清） |
