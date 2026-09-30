# INC26 侦察基线（主理人实测，2026-09-28）

> 本文件是 INC26 全部成员的共同输入。**所有结论均为现场实测**，不是复述记忆。
> 项目根：`D:\Agent项目\企业级 Multi-Agent 智能工作与技能资产平台\ForgeFlow-main`
> 本文件位于 `docs/`（本仓不提交的红线区）。

---

## 0. 用户本次要求（原文要点）

1. 读 OpenHands `software-agent-sdk`（zip 在桌面），**两项目必须兼容**；
2. **进行上传等功能的优化**；
3. 测试时**代码能力和分析能力必须达到企业级**；
4. **以本机 Ollama 的 qwen 为模型**；
5. **最后进行检查测试**，必须满足以上条件。

用户两段架构长文的落地主张：
- ForgeFlow = 企业控制平面（编排 / Skill / Memory / RBAC / 审计 / 成本 / 验证 / Artifact / HITL）；OpenHands = 代码执行面（Agent loop / 文件编辑 / Terminal / Workspace / 事件流）。
- **Resource Layer**：统一「任务资源」（文件 / 数据库 / Git 仓库 / 知识库 / API），用户不再手敲路径。
- **Artifact Layer**：Markdown 报告之外要有 Diff / 测试报告等真产物。
- **HITL**：代码修改必须人工审批。
- **可观测**：原始事件流 → 业务化时间线，原始 Trace 折叠。

---

## 1. 已建成的事实（INC25，已提交）

- **INC25 已提交**：`d632de3 INC25 — 资源中心(W1) + 代码执行面(W2)`；其上补了一个 corrective commit `c7bbb33`（撤销误纳入的 qa_tmp 探针产物）。
- **当前 HEAD**：`c7bbb33d3ff329bf7c91630dc7354cb37474aa4d`。`git status --porcelain` 仅剩 5 个 `docs/sop/*.md` modified + 一批 untracked 探针产物。
- **已建成的模块**（全部已存在于工作树）：

| 层 | 文件 | 行数 | 职责 |
|---|---|---|---|
| 资源域模型 | `forgeflow/resources/models.py` | 243 | 五类资源 + `ResourceSummary`（未测量即 `None`，**绝不补 0**） |
| 资源服务 | `forgeflow/resources/service.py` | 428 | `register_file/database/code/knowledge_base/api`、`list`、`get`、`preview`、`resolve_task_inputs` |
| 文件落盘 | `forgeflow/resources/storage.py` | 140 | `FileBlobStore`（`put` 内做大小校验 → `ResourceTooLargeError`） |
| 摘要 | `forgeflow/resources/summaries.py` | 398 | 表格行/列/字段/数据质量；文本字符数；PDF 页数 |
| 代码来源 | `forgeflow/resources/code_sources.py` | 276 | github / gitlab / local_path / zip |
| 资源 API | `forgeflow/api/routers/resources.py` | 187 | `POST /resources/files`(multipart) /`database`/`code`/`knowledge_base`/`api`，`GET /resources`，`GET /resources/{id}`，`GET /resources/{id}/preview` |
| 资源 API 契约 | `forgeflow/api/resource_schemas.py` | 81 | 请求/响应模型 |
| 仓储双实现 | `forgeflow/repositories/{memory,postgres}/resource_repo.py` | — | 沿用 `repositories/factory.py::get_resource_repository` |
| PG 迁移 | `alembic/versions/015_resources.py` | 68 | resources 表 |
| 代码执行面 | `forgeflow/codeplane/{engine,protocol,workspace,events,approval,tests_verdict}.py` | 465/154/303/206/229/145 | 引擎门面 / 契约 / 隔离工作区 / 事件标准化 / 审批 / 测试判据 |
| 引擎子进程 | `forgeflow/codeplane/runner/run_code_task.py` | 776 | job 走 stdin、事件走 stdout JSONL；**唯一允许 import openhands 的地方** |
| 编排接入 | `forgeflow/runtime/orchestrator.py` | 1799 | `_assemble_codeplane` 等 |
| 前端资源选择器 | `frontend/src/views/runs/ResourcePicker.tsx` | 309 | 五类资源登记 + 选择（`resource-add` / `resource-kind-*` / `resource-list` / `resource-card` / `resource-empty-note`） |
| 前端 API | `frontend/src/api/client.ts` | 1096 | `registerResource()`（文件走 multipart）+ `hubApi.resources()` |
| 前端 hooks | `frontend/src/api/hooks.ts` | — | `useResources` / `useRegisterResource` |
| 前端样式 | `frontend/src/styles/runs.css`（`.resource-*`，1600 行起） | — | 手写 CSS + oklch token |

---

## 2. 本次实测发现的**真实缺口**（INC26 的对象）

### 缺口 A — 上传体验只到"能登记"，未到"好用"

- `ResourcePicker.tsx` 文件上传**只有一个原生 `<input type="file">`**（`ResourcePicker.tsx::ResourcePicker` 第 196-204 行区块）：**无拖拽、无多文件、无客户端预检、无上传进度**。
- **预览能力已建但前端未接**：后端 `resources/router` 已有 `GET /resources/{id}/preview`，`frontend/src/api/client.ts` **没有**对应调用（全仓 grep `preview` 在 client.ts 无命中）⇒ 用户无法在看到资源摘要后核对内容（INC25 PRD 的 **P1-2 未做**）。
- **资源库复用/按类型筛选未做**：`GET /resources?kind=` 后端已支持，`hubApi.resources(params)` 也接受 `kind`，但 ResourcePicker 只用 `useResources({limit:50})`，**从不传 kind**（INC25 PRD 的 **P1-1 未做**）。
- 客户端无「单文件上限」预检 ⇒ 用户挑一个 12 MB 文件要等整包传完才拿到 413。

### 缺口 B — 代码执行面在真实档下**永远降级**（"两项目兼容"的堵点）

- `forgeflow/config.py::Settings.codeplane_interpreter` 默认 `""`，docstring 明确「**fail-closed，不猜解释器**」。
- **`.env`（103 行，B 档真实配置）里没有 `FORGEFLOW_CODEPLANE_PYTHON`** ⇒ 实测证据 `_inc25_task_res_probe.txt`：`codeplane.engine.available = false`，`reason = "未配置代码执行面解释器（Settings.codeplane_interpreter / FORGEFLOW_CODEPLANE_PYTHON 均为空）"`，`degraded = "engine_unavailable"`。
- ⇒ **今天在真实档下跑代码任务，永远只会得到降级占位**，Diff = `""`、`tests.measured = false`。INC25 画出来的代码闭环**在产品路径上从未真实跑通**（只在 runner 单测/直连探针层被证明可用：`_cp_runner_e2e.txt` exit=0 / 92.2s / 29 events；`_oh_realrun3.log` 的 C3 配置 `pytest rc=0 / fixed=true`）。
- 好消息：OpenHands SDK **已就位**——`envs\openhands\Scripts\python.exe` 存在，`openhands` + `openhands_tools` **1.49.6** 已安装。

### 缺口 C — 缺企业级双能力验收

- 无「用本机 qwen 真实跑一次代码修复任务并机械判定成功」的端到端验收。
- 无「用本机 qwen 真实跑一次数据分析任务并机械判定成功」的端到端验收。
- INC25 的 24 条 AC 里，AC-11/12/15/16/17/18/20/21 全部依赖代码面**真实可用**，在缺口 B 修复前**不可能真正满足**。

---

## 3. 现场环境（实测）

| 项 | 状态 |
|---|---|
| 本机 Ollama `:11434` | ✅ 在跑。模型：`qwen3:8b`（capabilities `completion/tools/thinking`，ctx 40960）、`qwen2.5vl:3b`（`completion/vision`，**无 tools，不可做 agent**） |
| PG `:5433` | ✅ 在跑 |
| 后端 `:8010` | ❌ 未启动 |
| 前端 `:5173` | ❌ 未启动 |
| MCP `:8001` | ❌ 未启动 |
| `envs\agentflow\Scripts\python.exe` | ✅ 存在（ForgeFlow 依赖环境） |
| `envs\openhands\Scripts\python.exe` | ✅ 存在（OpenHands 1.49.6 独立环境） |

`ForgeFlow-main\.env` 现状要点：`STORAGE_BACKEND=postgres`、`LLM_PROVIDER=ollama`、`OLLAMA_MODEL=qwen3:8b`、`DEV_LOGIN_PASSWORD=forgeflow-dev`、`API_PORT=8010`、`API_SECRET_KEY`/`TAVILY_API_KEY`/`LANGFUSE_*` 均已填。

---

## 4. 不可违背的既有纪律（违反即返工）

### 4.1 环境与运行

1. **沙箱代理红线**：沙箱设了 `HTTP_PROXY=127.0.0.1:63957`。**启动后端必须摘代理**（`NO_PROXY=127.0.0.1,localhost,::1` 并清 `HTTP_PROXY/HTTPS_PROXY`），否则 langchain/httpx 会把本机 Ollama 也走代理 → 502 → **静默降级 deterministic**（`completed/success/errors=[]` 全正常，真相只在 `llm.degraded`）。
2. Python 侧探本机用 `urllib.request.build_opener(ProxyHandler({}))`。
3. 端口：后端 **8010** / 前端 **5173** / PG **5433** / MCP 8001。登录 `POST /auth/login {"user_id":"manager-1","password":"forgeflow-dev"}`（4 账号 admin/manager-1/rep-1/viewer-1 同密码；**限流 5 次/IP/60s**）。
4. 常驻进程必须 `([wmiclass]"Win32_Process").Create(...)` 脱离沙箱；前端 vite 直启 `node …/vite/bin/vite.js`，**禁 npx**。uvicorn/vite 会周期自掉 ⇒ 测量前先跑幂等 ensure。

### 4.2 测试

5. **双档全量回归必须自己跑**，不采信成员自述。基线 INC24 `43e1ebf`：两档均 **1278/0/0/1**、集合差 0。INC25 后 unit **1189**、integration **110**（109 passed + 1 xfailed）。
6. `EXIT=1` 而 junit 0 failures 是**常态**（pytest 退出码受 warnings/清理影响）；**回归判定以 junit XML 为准**。
7. 依赖存储后端的测试须在签名显式声明档位（`tests/conftest.py::force_memory_backend`）。
8. 交付必跑 `alembic upgrade head` **两次**证幂等。

### 4.3 数据诚实（本仓最高价值）

9. **四层口径**：L1 Task Plan（`/runs/{id}.plan`）→ L2 Execution Record（`tool_invocations`，**唯一事实源**）→ L3 Observation（**仅** `executed is True` 投影）→ L4 Report（`artifacts[0].content`）。
10. **`latency_ms: float|None`，`None` = 未测量**，**绝不写 0**；前端 `null → "—"`。
11. 状态词表：`ok`/`error`/`unavailable`/`refused` + `blocked`/`not_applicable`；**`blocked` 不计 failed、不进 errors**。`skipped` 已退役（映射 `blocked`）。
12. **`intent` 只作 `query`/`text` 取值，绝不做工具模糊匹配**；`table`/`paths`/`repo_path` **只能来自显式输入**。
13. 资源侧：`ResourceSummary` 的 `rows/columns/chars/pages` 未测量即 `None`；`keywords` 无真实来源则**整键省略**；状态 `parsed` 才算真解析，`metadata_only`/`ignored` 是降级**不是成功**。

### 4.4 前端

14. **手写 CSS + oklch token，禁 MUI / Tailwind**。token 见 `frontend/src/styles/tokens.css`。
15. **`data-testid` 只增不改不删**。既有 `run-declare-table` / `run-declare-paths` 必须保留且行为不变。
16. **无前端单测设施 ⇒ 禁新建 vitest**。前端验证走 `frontend/e2e/`（playwright）或临时 harness。
17. ⚠️ **`<details>` 折叠区 wrapper 禁写 `display`**（跨引擎防御，INC24 实测更正）。
18. 显隐**禁**「基础 display + `hidden` 属性」组合（`tokens.css` 有全局 `[hidden]{display:none!important}`）。

### 4.5 Git 与引文

19. **无 remote ⇒ 本地 commit 是唯一恢复点**。只 `git add <确切路径>`，**禁 `git add -A`**。
20. **红线禁提交**：`docs/`、`qa_tmp/`、`_*`、`.workbuddy`。
21. ⚠️ **引文禁 `file.py:行号`**（被引文件插行即静默失真）⇒ 一律 `file.py::symbol`。审计扫**三层**（注释 + docstring + 字符串字面量）。
22. ⚠️ Bash 工具**无 coreutils**（`head/tail/grep/ls` 不可用），git 输出**别接管道**（会整片丢 stdout，看着像"工作树干净"）⇒ 用 python 直调 git / Read 读文件。
23. PowerShell 工具 stdout **不回显**，结果写文件再 Read；长任务（>120s）改用 Bash + 显式 timeout 或 WMI 脱离沙箱。

### 4.6 OpenHands 引擎三条硬约束（INC25 实测消融结论）

24. **本机只有 `qwen3:8b` 可做 agent**（`qwen2.5vl:3b` 无 `tools` 能力）。
25. **三毒必须同时解，缺一即失败**：
    1. `reasoning_effort` 必须显式 `"none"`（默认 `"high"` 会让 Ollama `think=True`，首轮烧 97s 返回空 ⇒ SDK 只能回「没有工具调用也没有内容」）；
    2. 必须传**极简自定义 system_prompt**（OpenHands 内置提示会让 8B 退化成「我来介绍可用工具」，实测 `STUCK`、0 工具调用）；
    3. 工具面**只留 `terminal` + `file_editor`**（`task_tracker` 会让 8B「看起来很忙」却永不落笔；`get_default_tools()` 必含它）。
    唯一实测能真改对代码的配置 = 三处全改（`_oh_realrun3.log` C3：`pytest rc=0`，42.4s）。
26. **`Conversation.run()` 返回 `None`**（不是事件可迭代）⇒ 引擎事件必须靠 `callbacks=` 采集，辅以 `conversation.state.events`，并**按事件 id 去重**（两通道都到，发 2N 是错的）。
27. **进程隔离红线**：`forgeflow/**`（除 `codeplane/runner/**`）**禁 import openhands**；`codeplane/runner/**` **禁 import forgeflow**。`agentflow` 与 `openhands` 两个 venv **绝不可合并**（`openai==3.19.0` vs OpenHands 要求 `openai<3`）。

---

## 5. 运行档位定义

| 档 | 配置 | 用途 |
|---|---|---|
| **A 离线档** | `STORAGE_BACKEND=memory` + `LLM_PROVIDER=mock` | 双档回归的必测档；**不得**因无 PG / 无 Ollama 而崩溃或 5xx |
| **B 真实档** | `STORAGE_BACKEND=postgres`（PG5433）+ `LLM_PROVIDER=ollama`（qwen3:8b） | 企业级真实能力验收档 |

---

## 5.5 【主理人实测补充】缺口 B 的两个子问题 + 一个配置陷阱

用 `envs\agentflow\python.exe` 真实载入 `forgeflow.config.get_settings()` 与 `codeplane.engine.SubprocessOpenHandsEngine`，实测输出（原始记录见 `_recon_settings.txt`）：

```
codeplane_interpreter  = ''
codeplane_enabled      = True
codeplane_model_name   = 'ollama_chat/qwen3:8b'
codeplane_base         = 'http://localhost:11434'
codeplane_reasoning    = 'none'
codeplane_num_ctx      = 32768
codeplane_temperature  = 0.0
codeplane_max_rounds   = 30
codeplane_timeout_s    = 180
codeplane_test_command = 'python -m pytest -q'      <-- 陷阱 B2
multimodal_max_bytes   = 5242880
storage_backend        = 'postgres'
llm_provider           = 'ollama'
ENV FORGEFLOW_CODEPLANE_PYTHON = None               <-- 陷阱 B* 的直接证据
ENV CODEPLANE_INTERPRETER      = None
engine._interpreter    = ''
engine.available()     = False                      <-- 缺口 B 确认
```

### B1 — `CODEPLANE_INTERPRETER` 未配置 ⇒ 引擎恒不可用

`codeplane.engine.SubprocessOpenHandsEngine.__init__` 的解释器解析链（**顺序不可颠倒**）：

```
显式构造参数  →  Settings.codeplane_interpreter  →  os.environ["FORGEFLOW_CODEPLANE_PYTHON"]
```

可用性判定在 `codeplane.engine._interpreter_ok`：空串 ⇒ `False`；含路径分隔符 ⇒ `Path(interp).exists()`；否则 `shutil.which(interp)`。三条全空 ⇒ `available()` 恒 `False`。

### B2 — `CODEPLANE_TEST_COMMAND` 是裸 `python` ⇒ 测试证据会**假失败**

实测值 `'python -m pytest -q'`。`.env.example` 已自带警告：Windows 上裸 `python` 常是 **WindowsApps 存根**（静默 exit 1）。⇒ 即使 B1 修好、引擎真跑起来，测试命令仍可能静默失败，把「改对了」判成「测试未通过」。**修 B1 必须同时修 B2**，且 B2 的解释器应与被测代码环境的解释器一致（`CODE_PLANE` 测试在被隔离工作区内执行，被测夹具是 Python 工程 ⇒ 指向真实解释器的绝对路径）。

### 陷阱 B* — `.env` 里写 `FORGEFLOW_CODEPLANE_PYTHON` **不会生效**（高危静默失败）

- `forgeflow/config.py::Settings.model_config` = `SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")`，**没有 `env_prefix`** ⇒ Settings 只认**字段名大写**的 env（`codeplane_interpreter` → `CODEPLANE_INTERPRETER`）。
- `FORGEFLOW_CODEPLANE_PYTHON` 只是 `engine.py` 里 `os.environ.get(...)` 的**进程环境兜底**。而 **pydantic-settings 读 `.env` 是填 Settings 对象，不会把值注入 `os.environ`**（实测 `ENV FORGEFLOW_CODEPLANE_PYTHON = None` 即为铁证）。
- ⇒ **只在 `.env` 里写 `FORGEFLOW_CODEPLANE_PYTHON=...` 会两条通道都不命中**，引擎继续降级，且**没有任何报错**——典型的静默降级。
- **正确做法**：`.env` 写 `CODEPLANE_INTERPRETER=<绝对路径>`（走 Settings 字段通道）。

### 已确认健康的部分（不必再修）

- `codeplane_model_name()` 正确派生 `ollama_chat/qwen3:8b`；`codeplane_base` = Ollama；`codeplane_reasoning_effort` = `none`；`codeplane_num_ctx` = 32768；`codeplane_temperature` = 0.0 ⇒ **RECON §4.6 的「三毒」1、2 已在 Settings 层就位**。
- OpenHands SDK 实测可导入：`openhands`、`from openhands.sdk import Agent, Conversation, LLM`、`from openhands.tools.preset.default import get_default_tools` 全部成功（`envs\openhands\...\python.exe`）；`agentflow` venv **正确地没有** openhands。

---

## 6. 缺口 → 需求映射（供 PRD 与设计消费）

| 缺口 | 用户原话对应 | 建议需求编号 |
|---|---|---|
| A 上传体验 | 「进行上传等功能的优化」 | R-U1 拖拽 + 多文件；R-U2 客户端预检；R-U3 资源预览；R-U4 资源库复用/筛选 |
| B1 引擎不可用 | 「这俩项目必须兼容」 | R-C1 解释器配置打通（fail-closed 保持）；R-C2 真实档端到端代码任务跑通；R-C3 降级语义不回归 |
| B2 测试命令陷阱 | 「代码能力必须达到企业级」 | R-C2b `CODEPLANE_TEST_COMMAND` 必须指向真实解释器绝对路径（否则测试证据假失败） |
| C 双能力验收 | 「代码能力和分析必须达到企业级」「用 ollama qwen」「最后检查测试」 | R-V1 代码能力验收；R-V2 分析能力验收；R-V3 双档回归 + 幂等迁移 |

> **给架构师/工程师的强制提醒**：修 B1 时**必须**用 `.env` 的 `CODEPLANE_INTERPRETER`（不是 `FORGEFLOW_CODEPLANE_PYTHON`），且**必须**同批修 B2。只修 B1 会得到「引擎可用但测试判定恒失败」的新假象。
