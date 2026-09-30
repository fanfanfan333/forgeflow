# AgentFlow · INC2/INC3 运行手册（07-RUNBOOK-INC2）

> 2026-09-26 INC16：OpenAI/Anthropic provider 已移除，本节为历史记录。

> 适用：`ForgeFlow-main/`（AgentFlow「企业级 Multi-Agent 智能工作与技能资产平台」改造版）
> 定位：**本机（Windows）可复制的三档运行手册**，覆盖「任务 → 经验 → Skill」闭环与 INC2/INC3 新能力。
> 与旧版的关系：`docs/sop/RUNBOOK.md` 是上一轮手册（部分结论已过期）。「**无 PG 也能起服务**」这条的最终口径见 **§0.1 与 §8**（#6 已修：memory 档免 PG 启动）。本文件是**本机实测版**，优先以本文件为准。
> 编写：架构师 高见远（INC3 WS-C）｜ 日期：2026-09-23
>
> **可信度约定**：每节标题后标 `[已亲跑]`（本轮真的执行过）或 `[未验证]`（未执行，仅据代码推断）。

---

## 0. 本机环境与端口一览 `[已亲跑]`

| 项 | 值（本机实测） | 备注 |
|---|---|---|
| 虚拟环境 Python | `C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe` | `Python 3.13.14`；`fastapi 0.141.1` |
| Node | `C:\Users\18769\.workbuddy\binaries\node\versions\22.22.2-3\node.exe` | `v22.22.2` |
| npm | `node <NODE_DIR>\node_modules\npm\bin\npm-cli.js` | `10.9.7`；本机**裸 `npm` 不可用**，必须走这条路 |
| 前端 | `ForgeFlow-main/frontend` | `node_modules` **已存在**；`vite` dev 默认 **5173** |
| 后端端口 | **8010** | ⚠️ **8000 被本机别的容器占用**，务必用 8010 |
| PostgreSQL | **5433**（容器已在跑） | DSN：`postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow` |
| Ollama | **11434**（已在跑） | 已装模型：`qwen3:8b`、`qwen2.5vl:3b` |
| Alembic 头部 | **`011 (head)`** | `versions/` 为 001–011，**无 012** |
| 真库表数 | **39** | `alembic_version=011`、`pgvector` 已装 |

### 0.1 三档速览

| 档 | 后端仓储 | LLM | 嵌入 | 需要 PG？ | 需要 Ollama？ |
|---|---|---|---|---|---|
| **① memory + mock** | `memory`（不落 PG） | `mock`（确定性） | `mock` | **否**（#6 后免 PG 启动；见下） | 否 |
| **② 真 PostgreSQL** | `postgres` | `mock` 或 `ollama` | `mock` | 是（5433） | 视 LLM 而定 |
| **③ 真 Ollama** | `memory` 或 `postgres` | `ollama` | `mock` | 视仓储而定 | 是（11434） |

> ✅ **关键更正（#6 · 已验证（主理人亲验））**：`forgeflow/api/main.py:73-79` 现按后端分支——`memory` 档直接 `app.state.pool = None` **跳过 `init_pool()`**，故**档①无需可达 PG 即可启动**（旧版「无 PG 起不来」已修；历史证据见 `08-FRAMEWORK-ALIGNMENT.md §6.1`）。
> ⚠️ **但「认证」是另一回事**：离线档的「登录」是本轮新增的 **#9**（用户拍板「实现离线认证」），**#9 落盘前，本文不宣称离线可登录**；其安全边界见 **§5.1**。

---

## 1. 环境准备 `[已亲跑]`

### 1.1 Python（已就绪，无需重建）

```powershell
$PY = "C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"
& $PY --version          # 期望：Python 3.13.14
& $PY -c "import fastapi; print(fastapi.__version__)"   # 期望：0.141.1
```

### 1.2 Node（已就绪）

```powershell
$NODE_DIR = "C:\Users\18769\.workbuddy\binaries\node\versions\22.22.2-3"
$NODE = "$NODE_DIR\node.exe"
$NPM  = "$NODE_DIR\node_modules\npm\bin\npm-cli.js"
& $NODE --version                         # v22.22.2
& $NODE $NPM --version                    # 10.9.7
# 前端依赖已装（frontend\node_modules 存在）；如需重装：
# & $NODE $NPM --prefix ".\ForgeFlow-main\frontend" install
```

---

## 2. 起后端（档① memory + mock）`[已亲跑]`

> 后端**不能**直接 `python -m uvicorn ...` 起：Windows 下 uvicorn 的 asyncio loop 工厂**硬编码 `ProactorEventLoop`**，而 psycopg（LangGraph PG checkpointer）拒绝在 Proactor 下运行，会报
> `psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。**必须**覆盖 loop 工厂用 **SelectorEventLoop**。

### 2.1 启动器（Python 3.13 **必须**覆盖 loop 工厂为 SelectorEventLoop）`[已亲跑]`

> **为什么不用 `uvicorn.run(...)`**：`uvicorn.run` 在本机 Python **3.13** 下**忽略 `set_event_loop_policy`**，仍构造 `ProactorEventLoop`；而 psycopg async（LangGraph PG checkpointer）**拒绝在 Proactor 下运行**（见本节末报错）。**即使传 `loop="asyncio"` 也不够**——必须让 server 真正跑在 `SelectorEventLoop` 上。
> 稳定做法（本机已固化于 `qa_tmp\run_api.py`）：**用 `asyncio.Runner(loop_factory=SelectorEventLoop)` 驱动 `uvicorn.Server(config).serve()`**。

新建 `qa_tmp\run_api.py`（本机临时区，可放任意位置）：

```python
import asyncio, os, pathlib, selectors, sys, uvicorn

ROOT = pathlib.Path(__file__).resolve().parents[1]   # <repo>/qa_tmp/.. == repo root
os.chdir(ROOT)                                       # 让 .env 生效
sys.path.insert(0, str(ROOT))                        # forgeflow 未安装为包

def _loop_factory():
    return asyncio.SelectorEventLoop(selectors.SelectSelector())   # ← 关键

async def _serve():
    config = uvicorn.Config("forgeflow.api.main:app",
                            host="127.0.0.1", port=8010,   # 8000 被占，用 8010
                            log_level="info", loop="asyncio")
    await uvicorn.Server(config).serve()

if __name__ == "__main__":
    with asyncio.Runner(loop_factory=_loop_factory) as runner:
        runner.run(_serve())
```

> 备选（等价、更短）：`config.get_loop_factory = lambda: asyncio.SelectorEventLoop` 后 `uvicorn.Server(config).run()`。两者都能把 server 放进 Selector 循环；**本机以 `qa_tmp\run_api.py` 的 Runner 版为准**（不再依赖 `get_loop_factory` 这种会被 uvicorn 版本改动的私有钩子）。
>
> 若忘了这一层，报错形如：`psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。

### 2.2 环境变量 + 启动（档①：memory 仓储 + mock 模型）

```powershell
$root = "D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台"
$PY   = "C:\Users\18769\.workbuddy\binaries\python\envs\agentflow\Scripts\python.exe"
$env:PYTHONPATH          = "$root\ForgeFlow-main"
$env:NO_PROXY            = "127.0.0.1,localhost"
$env:STORAGE_BACKEND     = "memory"
$env:LLM_PROVIDER        = "mock"
$env:EMBEDDING_PROVIDER  = "mock"
$env:TRACING_PROVIDER    = "none"
$env:DEV_LOGIN_ENABLED   = "true"
$env:DEV_LOGIN_PASSWORD  = "forgeflow-dev"
# 以下 PG 变量沿用（切档②/③ 复用）；#6 后 memory 档会跳过 PG 池，不再是启动硬依赖：
$env:POSTGRES_URL        = "postgresql+asyncpg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
$env:POSTGRES_SYNC_URL   = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
Set-Location "$root\ForgeFlow-main"
& $PY "$root\qa_tmp\run_api.py"
```

> 注：`ForgeFlow-main\.env` 已内置本机真栈配置（Ollama + 5433）。上面的 `$env:` 会**覆盖** `.env`，所以档①要显式设 `LLM_PROVIDER=mock` / `STORAGE_BACKEND=memory`。

**期望日志**：`ForgeFlow API ready` + `Seeded 4 featured skills (memory backend)`；`Uvicorn running on http://127.0.0.1:8010`。

### 2.3 自检

```powershell
curl.exe --noproxy "*" -s http://127.0.0.1:8010/health
# 期望：{"status":"healthy","database":"connected","graph":"compiled"}
```

> ⚠️ `curl.exe` 必须带 `--noproxy "*"`：本机沙箱有 `HTTP_PROXY`，不加会报
> `upstream connect failed ... (os error 10061)`。

---

## 3. 起后端（档② 真 PostgreSQL，5433）`[已亲跑]`

```powershell
$env:STORAGE_BACKEND = "postgres"
$env:LLM_PROVIDER    = "mock"          # 先跑通链路；换真模型见档③
$env:EMBEDDING_PROVIDER = "mock"       # 无 OpenAI key，用确定性本地向量
# 正确凭据（注意是 forgeflow，不是 postgres）：
$env:POSTGRES_URL      = "postgresql+asyncpg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"
$env:POSTGRES_SYNC_URL = "postgresql+psycopg://forgeflow:forgeflow@127.0.0.1:5433/forgeflow"  # 必须带 +psycopg
```

### 3.1 迁移

```powershell
Set-Location "$root\ForgeFlow-main"
& $PY -m alembic heads      # 期望：011 (head)
& $PY -m alembic current    # 期望：011（库已到位）
& $PY -m alembic upgrade head   # 001 → 011
```

> 本机真库**已经**在 `011`：`experiences` 表已含 `conflict_with/merged_from/dedup_key/confidence` 四列（本轮查过）。`upgrade head` 可重入（全部 `IF NOT EXISTS`）。

### 3.2 凭据与库名（**本机实测**，别踩坑）

| DSN | 结果 |
|---|---|
| `postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow` | ✅ **可连**，39 张表（本项目库） |
| `postgresql://postgres:postgres@127.0.0.1:5433/postgres` | ❌ `password authentication failed for user "postgres"` |
| `postgresql://forgeflow:forgeflow@127.0.0.1:5433/postgres` | ⚠️ 可连但 **0 张表**（维护库，**别用**） |

---

## 4. 起后端（档③ 真 Ollama，11434）`[部分已亲跑]`

```powershell
$env:LLM_PROVIDER      = "ollama"
$env:OLLAMA_BASE_URL   = "http://127.0.0.1:11434"
$env:OLLAMA_MODEL      = "qwen3:8b"
$env:OLLAMA_MODEL_STRONG = "qwen3:8b"
$env:NO_PROXY          = "127.0.0.1,localhost"   # ⚠️ 不加会 502（沙箱代理劫持 localhost）
$env:no_proxy          = "127.0.0.1,localhost"
```

- **模型已在线**：`GET http://127.0.0.1:11434/api/tags` → `['qwen2.5vl:3b','qwen3:8b']` `[已亲跑]`。
- ⚠️ `qwen3:8b` 加载约 **6GB**，本机内存**临界**。降级路径：
  1. 换小模型：`$env:OLLAMA_MODEL="qwen2.5vl:3b"`（更省内存）；
  2. 直接回档①：`$env:LLM_PROVIDER="mock"`（零内存、确定性）；
  3. 结构化输出走 `_build_ollama` 的 `reasoning=False`（关思考）——**否则 qwen3 会吃光 `num_predict` 返回空串**。该属性在 `forgeflow/models/provider.py` 中**硬编码**，无需额外开关 `[已读盘]`。
- ⚠️ **注意**：`settings.model_fallback_chain`（`ollama,mock`）**当前无人读取**（详见 `08-FRAMEWORK-ALIGNMENT.md §6.4`）；**Ollama 失败不会自动回落 mock**，需要**手动**改 `LLM_PROVIDER=mock`。

---

## 5. 登录拿 Token（三档通用）`[已亲跑]`

`DEV_LOGIN_ENABLED=true` 时启动会种入演示用户：`admin`/`manager-1`/`rep-1`/`viewer-1`，密码 = `DEV_LOGIN_PASSWORD`（本机 `forgeflow-dev`）。

```powershell
$TOKEN = (& curl.exe --noproxy "*" -s http://127.0.0.1:8010/auth/login `
  -H "content-type: application/json" `
  -d '{"user_id":"admin","password":"forgeflow-dev"}' | ConvertFrom-Json).access_token
"Bearer $TOKEN"     # 期望：非空（本轮实测长度 308）
```

后续所有受保护接口都加 `-H "Authorization: Bearer $TOKEN"`。

### 5.1 ⚠️ 离线认证的安全边界（**#9 落盘后定稿；当前状态：待定**）`[待定]`

> ⚠️ **一句话给读者**：**「离线也能登录」= 离线专用的内存实现，仅限 `memory` 档、`prod` 下必须不可达；绝不等于「生产也能用内存认证」。** 生产认证仍走 **PostgreSQL / OIDC**。
> 本节采自团队裁决（Task #9「实现离线认证」）与 `forgeflow/config.py` 的既有 fail-closed 精神；**最终落盘口径以代码为准**（文档回写代码）。

- **目标语义（#9 落盘后）**：**memory 档无需 PG 即可启动并完成认证** —— 用户明确要求「修复功能，可以正常使用」，故离线档**不仅能启动，还能登录并使用**（旧表述「能起但登不上」作废）。
- **实现性质（务必看清）**：离线认证 = **内存用户 / 令牌存储**，是**离线专用的第二实现**，**不是**生产可用的认证路径。
- **硬边界（两条，缺一不可）**：
  1. **仅当 `storage_backend == "memory"` 可用**；`postgres` 档**不走**内存认证。
  2. **`environment == "prod"` 下必须不可达** —— 与 `validate_runtime()` 对 `dev_login_enabled` 的 **fail-closed 同一精神**（`forgeflow/config.py:637`：`if prod and self.dev_login_enabled: problems.append("DEV_LOGIN_ENABLED must be false in production (use OIDC)")`）。
- **当前状态（本轮读盘）**：`forgeflow/api/main.py:86-87` **仍跳过离线档的演示用户种入**（`if dev_login_enabled and app.state.pool is not None:` —— 凭据存储是 PG-backed），且**全仓尚无内存用户/令牌存储**。故 **#9 落盘前，本文不把离线登录写成可用**。
- **归属**：Task #9（修复在飞）。落盘后本节去掉「待定」并补齐可复制 curl。

---

## 6. 「任务 → 经验 → Skill」闭环验证 `[已亲跑]`

```powershell
# ① 提交任务
$run = & curl.exe --noproxy "*" -s http://127.0.0.1:8010/tasks `
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" `
  -d '{"intent":"分析华东地区销售数据并生成报告"}' | ConvertFrom-Json
$RUN = $run.run_id
# 期望：status=completed、detail.outcome=success、3 个 step、experience_id 非空

# ② Run 详情
& curl.exe --noproxy "*" -s "http://127.0.0.1:8010/runs/$RUN" -H "Authorization: Bearer $TOKEN"

# ③ SSE 实时事件（应见 run.started → run.step... → run.completed → [DONE]）
& curl.exe --noproxy "*" -N "http://127.0.0.1:8010/runs/$RUN/events" -H "Authorization: Bearer $TOKEN"

# ④ 自动沉淀的经验
& curl.exe --noproxy "*" -s "http://127.0.0.1:8010/experiences" -H "Authorization: Bearer $TOKEN"

# ⑤ 失败任务 → Replan → 达上限转 HITL（意图含「失败」即可触发）
& curl.exe --noproxy "*" -s http://127.0.0.1:8010/tasks `
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" `
  -d '{"intent":"模拟失败任务"}'

# ⑥ 从经验编译候选（跳③）→ 评估 → 固化
$cand = & curl.exe --noproxy "*" -s http://127.0.0.1:8010/skill-candidates `
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" `
  -d '{"mode":"auto"}' | ConvertFrom-Json
& curl.exe --noproxy "*" -s -X POST "http://127.0.0.1:8010/skill-candidates/$($cand.id)/evaluate" -H "Authorization: Bearer $TOKEN"
& curl.exe --noproxy "*" -s -X POST "http://127.0.0.1:8010/skill-candidates/$($cand.id)/promote"  -H "Authorization: Bearer $TOKEN"
```

> 经验不足（< `SKILL_CANDIDATE_MIN_EXPERIENCES=3`）时编译返回 `status="insufficient"`（HTTP 200），多跑几个同类任务即可。
> 本轮实测：`POST /tasks` → `completed`；`GET /experiences` 有 1 条（含 `reusable_steps`）。

---

## 7. INC2/INC3 新能力怎么验（可复制 curl）`[已亲跑]`

> 前置：`$TOKEN` 见 §5。所有 curl 带 `--noproxy "*"`。

### 7.1 Cost Optimization（企业省钱）

```powershell
& curl.exe --noproxy "*" -s http://127.0.0.1:8010/cost/board   -H "Authorization: Bearer $TOKEN"
& curl.exe --noproxy "*" -s http://127.0.0.1:8010/cost/savings -H "Authorization: Bearer $TOKEN"
```
- 实测返回（**空窗口**）：`/cost/savings → {"has_data":false,"amount":null,...}`；`/cost/board → {"total_spent":0.0,"budgets":[]}`。**这是未被伪造的诚实值**。
- **账本已接通（Task #2）**：`api/routers/cost.py::_period_totals` 现**委托 `cost/ledger.py`**（memory 从 hub run store、PG 从 `workflow_runs` 读成本）；跑过带成本的 run 后数字即出现。`calculate_cost`（`observability/cost_tracker.py:74`）对**未知模型计 `0.0` + WARNING**（宁少算不虚增）。状态：**已验证（主理人亲验）· Task #2**。详见 `08 §6.2`。

### 7.2 SLO 分级

```powershell
& curl.exe --noproxy "*" -s http://127.0.0.1:8010/metrics/slo -H "Authorization: Bearer $TOKEN"
```
- 实测：`tiers` = `critical(99.5%/1500ms)`、`important(99.0%/2000ms)`、`edge(97.0%/5000ms)`；空窗口下 `has_data=false / attainment=null`（不伪造）。

### 7.3 审计导出

```powershell
& curl.exe --noproxy "*" -s "http://127.0.0.1:8010/audit/export?format=json&limit=5" -H "Authorization: Bearer $TOKEN"
& curl.exe --noproxy "*" -s "http://127.0.0.1:8010/audit/export?format=csv&limit=5"  -H "Authorization: Bearer $TOKEN"
```
- 实测：memory 档由**内存环形缓冲**导出（本轮 `total=113`）。CSV 走 `format=csv`。

### 7.4 Skill Evolution 建议

```powershell
& curl.exe --noproxy "*" -s http://127.0.0.1:8010/skills/evolution-advice -H "Authorization: Bearer $TOKEN"
```
- 实测：返回 `approval_required:true` + `items[]`（本轮 4 条 `kind=optimize, source=rule`）。
- ⚠️ **该 GET 无副作用，不会创建 Approval**；“建议→审批”尚未接线（`08 §6.3`）。

### 7.5 Skill Marketplace（技能上架 / 列表）

```powershell
# 取一个 skill_id
$sid = (& curl.exe --noproxy "*" -s http://127.0.0.1:8010/skills -H "Authorization: Bearer $TOKEN" | ConvertFrom-Json).items[0].id
# 上架（真服务实测 200，published=true）
& curl.exe --noproxy "*" -s -X POST http://127.0.0.1:8010/marketplace/skills/publish `
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" `
  -d "{\"skill_id\":\"$sid\",\"description\":\"共享测试\",\"shared\":false}"
# 列表
& curl.exe --noproxy "*" -s http://127.0.0.1:8010/marketplace/skills -H "Authorization: Bearer $TOKEN"
```
- ⚠️ `shared=true`（跨租户）需显式 `approve:skills`，`admin` 有；否则被拒（设计如此）。

### 7.6 Organizational Memory —— 记忆提升

```powershell
# 先写一条 user 记忆
$mid = (& curl.exe --noproxy "*" -s http://127.0.0.1:8010/memory `
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" `
  -d '{"scope":"user","content":"客户A偏好季度续约"}' | ConvertFrom-Json).id
# 提升到 team（真服务实测 200，namespace workspace/default/user/admin → workspace/default/team/default）
& curl.exe --noproxy "*" -s -X POST "http://127.0.0.1:8010/memory/$mid/promote" `
  -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" `
  -d '{"to_scope":"team","reason":"团队复用"}'
# 提升到 org 需 admin 角色；EPISODIC/ORG 不可作为来源。
```

---

## 8. 本机已知坑（本轮实测）`[已亲跑]`

| 坑 | 现象 | 处理 |
|---|---|---|
| **Bash 工具残缺** | `ls/grep/tail/dirname: command not found` | 用 **PowerShell 工具 / Grep / Glob / Read**，别用 Bash |
| **PowerShell stdout 不回显** | 命令 exit 0 但看不到输出 | `... | Out-File <file> -Encoding UTF8` 再用 **Read** 读回 |
| **`curl.exe` 被代理劫持** | `upstream connect failed ... (os error 10061)` | 每条 curl 加 **`--noproxy "*"`** |
| **uvicorn ProactorEventLoop 崩溃** | `psycopg.InterfaceError: ... 'ProactorEventLoop'` | 用 §2.1 启动器（`asyncio.Runner(loop_factory=SelectorEventLoop)` 驱动 `Server.serve()`）——**已验证**；见 §8.1 P-3 |
| **（历史）「无 PG 也能起服务」曾不成立** | 旧 lifespan 无条件连 PG（旧 `main.py:68`）→ 不可达即崩 | **Task #6 已修**：`main.py:73-79` memory 档 `pool=None` 跳过 `init_pool()` → **免 PG 可启动**；防回退的真 lifespan 测试**在飞** |
| **`Remove-Item` 被安全策略拦截** | 命令整体被阻断 | 避免删除；用 `Out-File` 覆写、或留文件不动 |
| **`cmd.exe` 被拦截** | 无法调用 | 用 PowerShell / 直接 exe 调用 |
| **pytest 退出码被 safe-delete 钩子改写** | 退出码非 0，但用例其实全过 | 看用例统计行，**别只看退出码** |
| **`TestClient` 不要进 `with`** | 进入上下文会跑 lifespan；**postgres 档**下强制连 PG（**memory 档 #6 后已安全**） | 单元测试优先不进 lifespan；或确保 PG 可达 |
| **Ollama 被沙箱代理打断** | 502 | 设 `NO_PROXY=127.0.0.1,localhost`（大小写都设） |
| **`alembic current` 输出为空** | 有时 stdout 不回显 | 用 `alembic heads`（实测 `011 (head)`）+ 直接查库 `alembic_version` |

### 8.1 骨架占位 · 3 个坑位（**待「源码已冻结」后定稿**）`[已定位，未定稿]`

> 下列 3 条**本轮已定位、但措辞/命令待冻结后回填**（它们会随 **#6 / #9** 及 lifespan 回归测试的落地而变化，故先占位、**不写死**）。

| 槽位 | 坑（占位标题） | 现状（本轮所见） | 冻结后需回填 |
|---|---|---|---|
| **P-1** | **限流(429) 在审计里记为 `outcome=error`，不是 `denied`** | `forgeflow/middleware/audit.py`：`outcome = "allowed" if status<400 else ("denied" if status in (401,403) else "error")` → **429 落入 `error`**。**若按 `outcome=denied` 过滤审计，会把限流/429 全漏掉**（它记在 `error` 里） | 补一条“如何正确捞 429”的 curl/SQL 示例；确认与告警过滤口径一致 |
| **P-2** | **离线档启动免 PG（#6 已修）+ 离线认证（#9 待落盘）** | #6 已落盘：`main.py:73-79` memory 档 `pool=None` 跳过 `init_pool()`；#9 待落盘：`main.py:86-87` **仍跳过**离线演示用户种入，**内存用户/令牌存储尚未落盘** | 冻结后本处定稿为「**memory 档无需 PG 即可启动并完成认证**」；同时把 §5.1 去掉「待定」、补离线登录 curl |
| **P-3** | **Python 3.13：`uvicorn.run(loop="asyncio")` 拒绝 Selector** | 需**显式**让 server 跑在 Selector 循环；稳定做法已固化在 `qa_tmp\run_api.py`：`asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)` 驱动 `uvicorn.Server(config).serve()` | 冻结后并入 §2.1，作为**唯一推荐启动方式**（删除“等价备选”表述） |

> 说明：P-1 / P-2 / P-3 **均为占位，非最终结论**；定稿以 `08-FRAMEWORK-ALIGNMENT.md` 同步后的**冻结版**为准。

---

## 9. 未验证事项（诚实边界）

- **档②/③ 的“全接口 live 回归”未跑**：本轮的端到端接口是在**档①（memory+mock）**服务实测的；真 PG 档只验证到「迁移=011、表=39、`upgrade head` 可重入、lifespan 可启动（Selector 下）」，**未在 postgres 档逐个打接口**。
- **真 Ollama 的生成质量/延迟未测**：只验证了 11434 在线与模型清单；`qwen3:8b` 在结构化输出下是否稳定返回非空 **未验证**。
- **多模态附件（`POST /tasks` 带 pdf/image）未跑**：`runtime/attachments.py` 已读盘确认存在，接口未在本轮实测。
- **前端 5173 页面渲染未跑**：`vite` 代理目标默认 `http://localhost:8000`，指向本机后端需 `VITE_API_TARGET=http://localhost:8010`（见 `vite.config.ts`）。**未实际启动前端**。
- **迁移 012**：`versions/` **无** 012，head 即 011；若后续新增，以 `alembic heads` 为准。
- **Cost 数字**：见 §7.1。账本**已接通**（Task #2，`_period_totals` 委托 `cost/ledger.py`），但本轮 live 服务**未累积出真实节省/预算数字**（空窗口 → 诚实空值）。
- **离线认证（#9）未落盘**：见 §5.1。离线档当前「**能启动**」（#6 已修）但**离线登录尚未实现**（`main.py:86-87` 跳过演示用户种入、全仓无内存用户/令牌存储）；**#9 落盘前，本文不宣称离线可登录**。
