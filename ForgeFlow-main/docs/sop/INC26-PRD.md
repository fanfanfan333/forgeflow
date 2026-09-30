# INC26 增量 PRD — 上传体验优化（U） + 代码执行面打通（C） + 企业级双能力验收（V）

> 文档类型：**增量** PRD（只描述相对 INC25 的变更，不含实现细节）
> 上游输入：`docs/sop/INC26-RECON.md`（本次侦察基线，含缺口 A/B/C 与 27 条纪律）、`docs/sop/INC25-PRD.md`（承接口径：编号 / 状态词表 / 验收风格）
> 工程基线：`ForgeFlow-main`（FastAPI `forgeflow/` + React 手写 CSS `frontend/src/`）
> 运行档位：A 离线（`storage_backend=memory` + `llm_provider=mock`）/ B 真实（PG5433 + 本机 Ollama `qwen3:8b`）
> 本次对象：RECON §2 的**三个真实缺口** A / B / C，对应需求族 **R-U / R-C / R-V**（RECON §6 映射表）
> 本文件位于 `docs/`（本仓不提交的红线区），仅作草稿与评审用；**不要 git add**。
> 引文纪律：一律 `文件名::符号名` 锚定；**严禁** `file.py:行号`（被引文件插行即静默失真）。

---

## 0. 现状锚点（先读代码得到的真实基线，作为本增量的需求边界）

### 0.1 缺口 A —— 上传体验只到「能登记」，未到「好用」

- `frontend/src/views/runs/ResourcePicker.tsx::ResourcePicker` —— 文件登记分支**只有一个原生 `<input type="file">`**（单选）：**无拖拽、无多文件、无客户端预检、无上传进度**。其余四类资源走两个文本槽 + 一个 `<select>`。
- `frontend/src/api/client.ts::registerResource` —— 文件走 multipart（字段名 `file`），**一次一个 `File`**；非文件类走 JSON `request`。失败经 `ApiError` 逐字抛出（不吞错）。
- **预览能力已建但前端未接**：后端 `forgeflow/api/routers/resources.py::preview_resource`（`GET /resources/{id}/preview`）已存在，响应模型 `forgeflow/api/resource_schemas.py::ResourcePreviewResponse`，实现 `forgeflow/resources/service.py::ResourceService.preview`（表格类前 N 行 / 文本类前 N 行 / 不可预览类型给 `available=False` 诚实空态）。但 `frontend/src/api/client.ts` **全无 preview 调用** ⇒ 用户无法先看内容再决定用不用（INC25 PRD **P1-2 未做**）。
- **按类型筛选未做**：后端 `forgeflow/api/routers/resources.py::list_resources` 已支持 `kind` 查询参数，`frontend/src/api/client.ts::hubApi.resources` 也已接受 `kind`，但 `ResourcePicker.tsx::ResourcePicker` 只用 `frontend/src/api/hooks.ts::useResources({limit:50})`，**从不传 kind**（INC25 PRD **P1-1 未做**）。
- **单文件上限的单一事实源**在后端：`forgeflow/config.py::Settings.multimodal_max_bytes`（默认 `5 * 1024 * 1024`）；强制点在 `forgeflow/resources/storage.py::FileBlobStore.check_size`（`put` 内先校验），超限抛 `forgeflow/resources/storage.py::ResourceTooLargeError`（消息**同时**含实际字节数与上限值），路由映射 HTTP **413**。类型白名单是 `forgeflow/resources/summaries.py::SUPPORTED_FILE_EXTENSIONS`，判定入口 `summaries.py::is_supported_file` / `summaries.py::content_kind`（不支持类型在落盘**之前**抛错 ⇒ HTTP **400**）。**前端目前没有任何预检，也没有读取这两项的同源入口**。

### 0.2 缺口 B —— 代码执行面在真实档下**永远降级**（「两项目兼容」的堵点）

- `forgeflow/config.py::Settings.codeplane_interpreter` 默认 **`""`**，docstring 明确 fail-closed「**不猜解释器**」；`forgeflow/config.py::Settings.codeplane_enabled` 默认 `true`。
- `forgeflow-main/.env`（B 档真实配置）**没有** `FORGEFLOW_CODEPLANE_PYTHON` ⇒ `forgeflow/codeplane/engine.py::SubprocessOpenHandsEngine.available` 返回 `false`，`reason` = 未配置解释器。
- 该降级沿 `forgeflow/runtime/tool_handlers.py`（`code.execute` 的 `engine.available()` 分支）落为 `codeplane.degraded == "engine_unavailable"`、`diff == ""`、`tests.measured == false`、任务非「已完成」。⇒ **INC25 画出的代码闭环在产品路径上从未真实跑通**（仅在 runner 单测 / 直连探针层被证明可用）。
- 好消息：OpenHands SDK **已就位** —— `C:\Users\18769\.workbuddy\binaries\python\envs\openhands\Scripts\python.exe`，`openhands` + `openhands_tools` **1.49.6** 已安装；引擎子进程通过 `forgeflow/codeplane/runner/run_code_task.py` 拉起（**唯一**允许 `import openhands` 的地方）。
- 任务触发口径（**重要**）：`forgeflow/runtime/orchestrator.py::_is_code_task` 判定一个任务是代码任务，仅当：审批复跑（`codeplane_approval` / `codeplane_workspace_id`）、显式声明 `code.execute` / `code.commit`、或声明的 `resources` 解引用出真实 `repo_path` / `paths`。**不依赖 intent 关键词**（纪律 `intent` 只作 `query`/`text`）。`orchestrator.py::_candidates_for` 据此在计划里注入 `code.execute` + `code.commit`。
- 既有可复用件（不需重做）：`forgeflow/codeplane/workspace.py::WorkspaceManager`（工作区根**必须在项目树外**，`create`/`release`/`destroy` 全记录）、`forgeflow/codeplane/engine.py::SubprocessOpenHandsEngine`（代理洁癖 `_env`、子进程隔离、显式降级）、`forgeflow/runtime/artifacts.py::artifacts_from_invocations`（已产出 `artifacts.py::ARTIFACT_KIND_CODE_DIFF` / `ARTIFACT_KIND_CODE_TEST`）、审批闭环 `frontend/src/api/client.ts::hubApi.codeApprove` / `::hubApi.codeReject`。

### 0.3 缺口 C —— 缺企业级双能力验收

- 无「用本机 `qwen3:8b` 真实跑一次代码修复任务并**机械判定成功**」的端到端验收。
- 无「用本机 `qwen3:8b` 真实跑一次数据分析任务并**机械判定成功**」的端到端验收。
- INC25 的 AC-11 / AC-12 / AC-15 / AC-16 / AC-17 / AC-18 / AC-20 / AC-21 全部依赖代码面**真实可用**，在缺口 B 修复前**不可能真正满足**。

### 0.4 本增量的边界

- **不重写** INC25 已满足的需求；本节列出的是**未做 / 未跑通**的部分。
- 双档抽象、四层口径、状态词表、`data-testid` 只增不改不删、手写 CSS + oklch token 等纪律**全部沿用**（RECON §4）。

---

## 1. 产品目标

### 1.1 一句话目标

把 INC25 的「资源中心 + 代码执行面」从**能登记 / 有代码面**推进到**好用 + 真能跑 + 可验收**：上传有拖拽与批量与上传前预检、资源能被看懂与复用；代码执行面在 B 档**真实可用**（保持 fail-closed）；并用本机 `qwen3:8b` 完成**代码能力**与**分析能力**两条**可机械判定**的企业级验收。

### 1.2 三条衡量标准（可测）

| 编号 | 衡量标准 | 判定方式 |
|---|---|---|
| **M1 上传可用** | 用户可**拖拽**并**一次多选**文件上传，**逐文件独立成败**；**上传前**即可被拦（类型 / 大小），且上限与类型白名单与后端**同源**；可就地**预览**资源内容、可**复用**历史资源并**按类型筛选** | AC-1 ~ AC-8 / AC-20 |
| **M2 代码真闭环** | B 档下（PG5433 + Ollama）一次代码任务完整走通「隔离工作区 → 引擎 → Diff → 测试 → 审批 → artifact」，ForgeFlow 仓库工作树前后**无变化**；同时**保持 fail-closed**（未配置即显式降级，绝不猜解释器） | AC-9 ~ AC-13 / AC-21 |
| **M3 双能力企业级** | 用本机 `qwen3:8b` 真跑：**代码能力**（改对一个种子缺陷、测试机械判通过）与**分析能力**（产出等于夹具真实统计量的报告）两条验收**机械判定**；双档全量回归零失败、迁移幂等 | AC-14 ~ AC-18 |

---

## 2. 用户故事

### 上传体验（U）

- **US-1（拖拽 + 批量）** 作为数据工作者，我想把一批 CSV / Excel **拖进**资源区并**一次多选**，每个文件各自显示上传状态与结果，这样我不用一个个点「选择文件」、也不必因某个文件失败就重来整批。
- **US-2（上传前就知道会不会被拒）** 作为用户，我想在**点上传之前**就被明确告知「这个文件太大 / 类型不支持」，这样我不会白白等一个大包传完才拿到 413。
- **US-3（先看内容再决定用不用）** 作为业务分析师，我想在把资源挂到任务前**预览**它的前几行 / 前几段，这样我能确认这份数据/文档是不是我要的那份，而不是只看一个文件名。
- **US-4（复用与筛选）** 作为常客，我想从**历史资源库**里直接挑一个用过的资源、并能**按类型筛选**，这样我不用反复重传同一个文件。

### 代码执行面（C）

- **US-5（真实跑通）** 作为开发者，我想让代码任务在**真实档**下真的改动代码、真的跑测试、真的给出 Diff，并由我审批后交付，这样「两项目兼容」不是纸面承诺。
- **US-6（诚实降级不回归）** 作为平台运维者，当解释器**未配置**或引擎不可用时，我要的是**显式的降级声明**（而不是悄悄跑出一个「已完成」）；并且没配置时系统**绝不**替我猜一个解释器。

### 企业级验收（V）

- **US-7（代码能力可验收）** 作为产品负责人，我想看到一个**用本机 qwen3:8b 真跑、由机器判**「改对了」的代码任务证据（Diff + pytest 结果），而不是一句「看起来不错」。
- **US-8（分析能力可验收）** 作为产品负责人，我想看到一个**用本机 qwen3:8b 真跑、产物里的数字等于夹具真实统计量**的分析任务证据。

---

## 3. 需求池（P0 必须 / P1 应当 / P2 可以）

> 约定：每条含「做什么」+「验收标准（可机械判定）」。涉及既有代码处一律以 `文件名::符号名` 锚定。**本增量只做相对 INC25 的变更**。

### 3.1 P0 — 上传体验（R-U）

#### P0-1 拖拽上传 + 多文件批量（逐文件独立成败）
- **做什么**：把 `ResourcePicker.tsx::ResourcePicker` 文件分支从「单个原生 `<input type="file">`」升级为**拖拽投放区 + 多选**（`<input type="file" multiple>`）。选中的每个文件形成**一行独立上传项**，各自有文件名、状态（待上传 / 上传中 / 已解析 / 失败 + 原因），**逐文件独立成败**：一个文件失败不得连坐其余文件。上传仍走 `client.ts::registerResource` 的 multipart `POST /resources/files`（一次一个文件，见 Q2）。
- **验收标准**：
  - 存在可寻址的拖拽区；拖入 N≥2 个合法文件后出现 N 行上传项。
  - 构造「1 个合法 + 1 个不支持类型」：合法文件状态为「已解析」，失败文件带逐字原因，**且合法文件仍登记成功**（失败不连坐）。
  - 多文件全部成功后，`GET /resources` 的 `items` 含**全部**新登记资源。

#### P0-2 客户端预检（上传前拦截，且与后端同源）
- **做什么**：在**发出请求之前**校验每个文件的**类型**与**大小**。类型白名单与大小上限**必须来自后端单一事实源**，前端**不得写死**：新增一个只读端点 `GET /resources/limits`（见 Q1），返回 `max_bytes`（源自 `config.py::Settings.multimodal_max_bytes`）与 `supported_extensions`（源自 `resources/summaries.py::SUPPORTED_FILE_EXTENSIONS`）；前端取回后驱动预检。被拦文件**不发起**上传请求，直接在行内显示原因。
- **验收标准**：
  - 选一个超过上限的文件：行内立即显示「超过单文件上限 X 字节」，且**未发生** `POST /resources/files` 请求（按网络请求计数机械判定）。
  - 选一个不支持扩展名的文件：行内立即显示逐字类型原因，同样**未发起**请求。
  - **同源**：`GET /resources/limits` 的 `max_bytes` 与 `Settings.multimodal_max_bytes` 相等、`supported_extensions` 与 `SUPPORTED_FILE_EXTENSIONS` 逐项相等；`frontend/src` 内**不存在**写死的上限字节常量（对前端产物做静态检查）。

#### P0-3 资源预览（接后端 preview）
- **做什么**：把已在后端就绪、前端**从未调用**的 `resources/router::preview_resource` 接起来：新增 `client.ts` 的 preview 调用 + 一个预览入口（弹层或区块）。表格类展示**前 N 行**（表头 + 行），文本类展示**前 N 行文本**；`ResourcePreviewResponse.truncated == true` 时显示**截断提示**；对不可预览类型（database / git_repo / knowledge_base / api）呈现 `available=false` 的**诚实空态**，绝不伪造内容。
- **验收标准**：
  - 表格类文件资源：预览的表头与单元格与真实文件**逐字一致**；超出 N 行时可见截断提示。
  - 文本类文件资源：预览内容与真实文件前 N 行一致。
  - 非文件类资源：可见「该类型资源不支持内容预览」类诚实文案，**不出现**任何伪造内容。
  - 预览**不依赖**重新上传（直接消费已登记资源）。

#### P0-4 资源库复用 + 按类型筛选
- **做什么**：资源列表接入 `kind` 过滤（`GET /resources?kind=`，后端 `resources/router::list_resources` 已支持）：提供类型筛选入口，并让历史资源可直接勾选复用（复用与本次新登记在 `context.resources` 中语义一致）。
- **验收标准**：
  - 选择某类型后，列表 `items` 的 `kind` **全部**等于所选值；请求确实以该 `kind` 为参数发出。
  - 从历史库勾选的资源，与本次新登记资源一样可随任务声明（提交 payload 的 `context.resources` 含其 id）。

### 3.2 P0 — 代码执行面打通（R-C）

#### P0-5 真实档配置打通（**保持 fail-closed**）
- **做什么**：在 B 档真实配置中把代码执行面解释器接上 `C:\Users\18769\.workbuddy\binaries\python\envs\openhands\Scripts\python.exe`（经 `FORGEFLOW_CODEPLANE_PYTHON` 或 `Settings.codeplane_interpreter`）。**绝不改动** `engine.py::SubprocessOpenHandsEngine.available` 的 fail-closed 语义：未配置时仍返回 `false` + 显式 `reason`，**不得添加内置默认解释器**。运行期仍需 `NO_PROXY`/清代理（`engine.py::SubprocessOpenHandsEngine._env`）以防静默降级为 deterministic。
- **验收标准**：
  - 未设置解释器时：`engine.available() == false`，代码任务 `degraded == "engine_unavailable"`（保持现状，不回归）。
  - 设置指向存在路径后：`engine.available() == true`；代码任务不再走 `engine_unavailable` 分支。
  - 配置缺失时系统**不产生**任何「猜测的解释器」路径（`available` 逻辑不新增默认值）。

#### P0-6 真实档端到端代码任务（隔离工作区 → 引擎 → Diff → 测试 → 审批 → artifact）
- **做什么**：在 B 档下让 `orchestrator.py::_is_code_task` 为真的代码任务走完整链路：`WorkspaceManager.create`（工作区在项目树外）→ `engine.run` 真实执行 → `codeplane.diff` → ForgeFlow 侧复算测试判定（`codeplane/tests_verdict.py::evaluate_test_output`）→ 进入**待审批** → 批准后经 `artifacts.py::artifacts_from_invocations` 产出 `code_diff` + `code_test_report`。
- **验收标准**：
  - 工作区路径**不在** ForgeFlow 项目树内；任务前后 ForgeFlow 仓库 `git status` **一致**。
  - 产出**非空** `codeplane.diff` 且 `codeplane.tests.measured == true`。
  - **代码任务 run**：产出 Diff 后为**待审批**（`status == "awaiting_approval"`，非「已完成」）。
  - **审批回写**：批准后**原 run** 的 `codeplane.approval.status` 翻为 `"approved"`，且 `decided_by` / `decided_at` 非空。
  - **产物**：批准后**approve 响应返回的 resume run**（新 run）的 `artifacts` 至少各含一条 `code_diff` 与 `code_test_report`；既有报告类 run 的 `artifacts` 无回归。
  - （说明：「审批回写」与「产物」是**两个不同的 run** —— 审批回写落在**原 run**，代码产物落在**复跑出的 resume run**；勿混为一个 `{id}`。）

#### P0-7 降级语义不回归
- **做什么**：保留 INC25 的诚实降级口径，并在本增量中**显式回归**：引擎不可用 / 模型不可用 / 未配置解释器，均须显式声明降级、点名受影响步骤、**不得**把确定性结果包装成模型产出。
- **验收标准**：A 档与「B 档但未配置解释器」两种情形下，代码任务仍显式 `degraded`、`diff == ""`、`tests.measured == false`、状态非「已完成」，且**不返回 5xx**。

### 3.3 P0 — 企业级双能力验收（R-V）

> 「企业级」的机械定义（建议阈值，待 Q3 确认）：**真实引擎 + 真实模型 + 固定夹具 + ground truth 精确比对 + 原始证据留存 + 可复现次数门槛**。所有判定为**布尔量计算**，不引入「看起来不错」这类主观项。

#### P0-8 企业级**代码能力**验收（真实档 + 本机 qwen3:8b）
- **做什么**：构造一个**确定性代码夹具**（小型 Python 包 + 一个种子缺陷 + 一个在当前代码上**必然失败**的测试），以 `local_path` 资源登记；在 B 档下以真实引擎 + `qwen3:8b`（`temperature=0.0`，默认）运行代码任务，走完 P0-6 全链路。验收=机器判定「改对了」。
- **验收标准**：
  - 单次机械通过 = 同时满足：`codeplane.degraded is None`、`codeplane.tests.measured == true`、`tests.verdict == "passed"`、`tests.exit_code == 0`、`codeplane.diff` 非空且命中目标文件、夹具源仓库工作树不变。
  - **复现门槛**：连续 **3 次**独立运行中 **≥2 次**机械通过（`qwen3:8b` 非确定性，`temperature=0.0` 下仍可能抖动）。**<2 次即验收失败**，如实记录，**不得**上调阈值迁就。
  - 每次运行留存**原始 pytest 输出**与 Diff 供复核。

#### P0-9 企业级**分析能力**验收（真实档 + 本机 qwen3:8b）
- **做什么**：构造一个**固定 CSV 夹具**并**预计算 ground truth**（行数 R、至少一个聚合量 A）。在 B 档下以 `qwen3:8b` 运行一次分析任务，产出 `report_markdown`。验收=机器比对报告中的数字与 ground truth。
- **验收标准**：
  - 产物为 `report_markdown`；其内容**包含**数值等于 R 与 A 的数字（精确比对）。
  - 数字**源于夹具真实内容**（非 stub、非模板句）；若模型未算出正确数字，**判定失败**（不得改判）。
  - 报告为诚实态（`stub` 语义不成立 / 非降级占位）。

#### P0-10 双档回归 + 迁移幂等
- **做什么**：本增量完成后，A 档与 B 档全量回归通过、两档用例集合一致；`alembic upgrade head` 幂等。
- **验收标准**：A 档与 B 档 junit XML 均 **0 failures / 0 errors**（**以 junit 为准**，退出码不作判据，纪律 §4.2-6）；两档用例集合差为 **0**；`alembic upgrade head` 连续执行两次无 schema diff。

### 3.4 P1 — 应当具备

| 编号 | 做什么 | 验收标准 |
|---|---|---|
| P1-1 | 逐文件**上传进度**（百分比或进度条） | 上传中每行可见进度指示；完成即转为状态徽章 |
| P1-2 | 失败文件**单行重试**（不重传整批） | 点击某失败行「重试」只重发该文件 |
| P1-3 | 拖入**不支持类型**时整批内**逐字**标注该行原因（与后端同源） | 混合批中非法行带原因、非法行不产生「已解析」条目 |
| P1-4 | 预览**分页 / 增量加载**（`n` 可调，上限由 `preview_resource` 的 `n` 约束） | 增大 `n` 后预览行数随之增加；超出上限被拒（非静默截断） |
| P1-5 | 资源列表**类型筛选状态**在会话内保持 | 切换 tab / 重开面板后筛选值保持 |
| P1-6 | 代码任务**真实档**的时间线 / Diff / 测试区块在 B 档下真实渲染（复用既有派生函数） | 与 `realRun.ts::detailToStages` 词表一致；`blocked` 显示「受阻」而非「失败」 |

### 3.5 P2 — 可以具备

| 编号 | 做什么 |
|---|---|
| P2-1 | 拖入**目录 / 文件夹**（批量登记） |
| P2-2 | 上传**断点续传 / 分片**（大文件） |
| P2-3 | 预览内**搜索 / 列筛选** |
| P2-4 | 资源指纹去重提示（复用 `FileBlobStore` 的 sha256 内容寻址） |
| P2-5 | 分析验收的**多夹具参数化**（一份夹具集 + 批量判定） |

---

## 4. UI 设计稿（ASCII 线框）

> 视觉约束：沿用现有手写 CSS + oklch token（`frontend/src/styles/tokens.css`）；面板结构沿用 `panel / panel-head / panel-body`、按钮沿用 `.btn.primary`、注意条沿用 `.af-note`（警告 `.af-note.warn`）、状态徽章沿用既有 emerald / amber / red / blue tone。**禁止** MUI / Tailwind；**禁新建 vitest**（前端验证走 `frontend/e2e/` playwright 或临时 harness）。以下线框只表达层级与信息，不表达像素。

### 4.1 新建任务页 · 资源中心（拖拽区 + 多文件列表 + 每项状态）

```
┌─ 运行列表 ────────────────────────────────────────────────────────────────┐
│                                                                            │
│  ┌ 任务意图 ────────────────────────────────────────────┐  ┌───────────┐   │
│  │ 用一句话描述要完成的任务，例如：修复 calc.py 的…      │  │ 运行任务  │   │
│  └──────────────────────────────────────────────────────┘  └───────────┘   │
│                                                                            │
│  任务资源（可选）                              已添加 3 项   [+ 添加资源 ▾] │
│  ┌─ 拖拽文件到此处，或 [ 选择文件 ]（可多选）────────────────────────────┐ │
│  │         ⤓  把 CSV / Excel / PDF / 文本 / 图片 拖到这里                 │ │
│  │         单文件上限 5.0 MB · 支持 .csv .xlsx .pdf .txt .md .json …       │ │
│  ├────────────────────────────────────────────────────────────────────────┤ │
│  │ ▤  销售线索_2026Q1.csv          [██████████] 100%   表格 · 已解析 预览 │ │
│  │ ▤  地区维表.xlsx                [█████░░░░░]  52%   上传中…            │ │
│  │ ⚠  notes.txt.bak                —                   类型不支持：.bak   │ │
│  │                                                      [重试]           │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                            │
│  类型筛选： [全部] [文件] [数据表] [代码仓库] [知识库] [接口]              │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ ☑ ▤  销售线索_2026Q1.csv                                表格 · 已解析  │ │
│  │     1,284 行 · 11 列 · 字段：lead_id, company, owner, amount, stage, … │ │
│  │                                                    [预览] [移除]        │ │
│  │ ☐ ⌥  github.com/acme/portal                        Git 仓库 · main    │ │
│  │     Python · 98 个文件 · 含 tests/                                     │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                            │
│  ⓘ 未添加资源时，Agent 只按任务描述执行，不会读取任何文件、仓库或数据库。   │
└────────────────────────────────────────────────────────────────────────────┘

上传前预检被拦（不发请求，行内逐字）：
┌────────────────────────────────────────────────────────────────────────────┐
│ ⚠  region_dump.csv   12,582,912 字节 > 单文件上限 5,242,880 字节（未上传） │
└────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 资源预览（弹层 / 区块）

```
┌─ 预览：销售线索_2026Q1.csv ───────────────────────── 表格 · 前 20 行 ──────┐
│  lead_id │ company  │ owner │ amount │ stage   │ …                        │
│  ────────┼──────────┼───────┼────────┼─────────┼──────────                │
│  L-0001  │ Acme     │ 王蕾  │ 120000 │ won     │ …                        │
│  L-0002  │ Globex   │ (空)  │  80000 │ open    │ …                        │
│  L-0003  │ Initech  │ 李强  │ 240000 │ won     │ …                        │
│  …                                                                    [×]  │
│  ⓘ 仅预览前 20 行（文件共 1,284 行，已截断）                               │
└────────────────────────────────────────────────────────────────────────────┘

不可预览类型（诚实空态）：
┌────────────────────────────────────────────────────────────────────────────┐
│ ⓘ 该类型资源不支持内容预览（database 资源仅登记可用范围）。               │
└────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 代码任务（真实档）· 证据条 + 待审批

```
┌─ 代码任务（真实档 · qwen3:8b）──────────────────── 引擎：可用 · 未降级 ────┐
│  ✓ 隔离工作区已创建（项目树外）      ✓ Diff 已产出        ⚠ 待人工审批      │
│  ─ 测试结果 ──────────────────────────────────────────────────────────── │
│   ✓ 通过 5   ✗ 失败 0   ⚠ 错误 0      命令：python -m pytest -q           │
│  ─ 变更 ──────────────────────────────────────────────────────────────── │
│   calc.py    +2 / −1                                                     │
│  ─ 需要你的决定 ──────────────────────────────────────────────────────── │
│   未获批准前，代码变更不会写入目标分支。                                  │
│   [ 批准修改 ]   [ 拒绝 ]   [ 重新分析 ]                                 │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. 待确认问题（≤5 条，含默认假设）

| 编号 | 问题 | 我的默认假设（未澄清前按此执行） |
|---|---|---|
| **Q1** | 客户端文件上限 / 类型白名单如何「同源」而不写死？后端已有 `config.py::Settings.multimodal_max_bytes` 与 `resources/summaries.py::SUPPORTED_FILE_EXTENSIONS`，但**没有任何端点把它们暴露给前端**。 | **新增一个只读端点** `GET /resources/limits`，返回 `{max_bytes, supported_extensions}`，值分别取自上述两处单一事实源；前端启动时取一次并驱动预检，**绝不在前端写死字节常量或扩展名列表**。若架构师发现更合适的既有只读端点，应复用并保持同源。 |
| **Q2** | 多文件上传是**并发**还是**串行**？部分失败如何呈现？ | **串行（逐文件依次）** 调 `POST /resources/files`：每个文件一行、状态独立，一个失败**不连坐**其余；失败行显示逐字原因并支持**单行重试（P1-2）**。选串行是为避免并发写入与进程本地索引/DB 争用引入不确定性；若架构师评估并发安全，可改为**有限并发**但不改「逐文件独立成败」的验收口径。 |
| **Q3** | 「企业级代码能力 / 分析能力」的判定阈值如何定，才能**机械判定**而不是「看起来不错」？ | **代码能力（P0-8）**：固定夹具 + 种子缺陷 + 失败测试；单次通过 = `degraded is None` ∧ `tests.measured` ∧ `verdict == "passed"` ∧ `exit_code == 0` ∧ Diff 命中目标文件 ∧ 源仓库工作树不变；**3 次中 ≥2 次通过**。**分析能力（P0-9）**：固定 CSV + 预计算 ground truth（行数 R + 聚合量 A）；报告**精确包含** R 与 A。两项均须留存原始证据、判定为布尔量。 |
| **Q4** | 代码任务的**业务意图**如何触发（用户一句 intent 里怎么识别「这是代码任务」）？ | **不再依赖 intent 关键词**。沿用 `orchestrator.py::_is_code_task` 的既有信号：声明**代码资源**（解引用出 `repo_path`/`paths`）或显式声明 `code.execute`/`code.commit`。intent 仅作 `task_intent` 传给引擎，**不做工具模糊匹配**（纪律：`intent` 只作 `query`/`text`）。UI 上应显式提示「本次为代码任务」，让用户可感知触发原因。 |
| **Q5** | 「分析能力」的**真实执行机制**归谁？现有 `mcp/server/tools/data_tools.py::query_db` 是**永久** development stub（A 档不可能有真实数仓）。 | 分析任务必须走**真实执行路径**才会产生真实数字：由架构师在 DESIGN 选定（react 工具链真实读文件 **或** 复用代码执行面跑分析脚本）。本 PRD 只锁**结果口径**：产物中的数字必须等于夹具真实统计量，stub / 模板一律判失败。**不**要求在本增量新建向量检索或数仓连接器。 |

---

## 6. 非目标（Out of Scope，本次明确不做）

1. **不做 PR 自动创建 / 自动合并**（PR 仍仅列 P2，审批后只 commit 到任务工作区）。
2. **不 fork / 不 vendor OpenHands**：`software-agent-sdk` 源码不进本仓、不作为本仓子系统维护。
3. **不引入 Docker 集群 / Agent Server 容器编排**：不做 `openhands-agent-server` 的 Docker runtime 集群化。
4. **不改四层数据契约**（L1 plan / L2 tool_invocations / L3 observations / L4 artifacts）与既有 API 语义；本增量只在既有口径上**增量扩展**。
5. **不新增 MUI / Tailwind，不新建 vitest**：继续手写 CSS + oklch token；前端验证走 `frontend/e2e/`（playwright）或临时 harness。
6. **不合并两个 venv**（`agentflow` ↔ `openhands` 绝不可合并：`openai==3.19.0` vs OpenHands 要求 `openai<3`）；`forgeflow/**`（除 `codeplane/runner/**`）禁 `import openhands`。
7. **不给每种 Agent 单独造上传按钮**：仍是统一的「任务资源」入口（用户明确要求）。
8. **不做非代码任务的 OpenHands 化**：知识 / 数据任务的编排仍走既有 Orchestrator + 工具链。
9. **不新增向量检索 / RAG / 数仓连接器**：记忆与数据工具沿用既有实现。
10. **不做资源内容的 DLP 深度扫描新规则**：沿用既有 `Settings.dlp_enabled` 与既有规则集。
11. **不自造第二套存储抽象**：资源层继续沿用 `repositories/factory.py` 的 memory / postgres 双实现范式。

---

## 7. 验收标准汇总表（每条可被测试机械判定）

| 编号 | 关联需求 | 验收标准（机械可判定） |
|---|---|---|
| **AC-1** | P0-1 | 资源中心存在可寻址的拖拽区；拖入 N≥2 个合法文件后出现 N 行上传项，每行含文件名 + 独立状态 |
| **AC-2** | P0-1 | 「1 合法 + 1 不支持类型」混合批：合法文件状态「已解析」、失败文件带逐字原因，**合法文件仍登记成功**（失败不连坐） |
| **AC-3** | P0-2 | 选一个超过 `Settings.multimodal_max_bytes` 的文件：行内立即提示上限，且**未发起** `POST /resources/files`（请求计数判定） |
| **AC-4** | P0-2 | `GET /resources/limits` 的 `max_bytes` == `Settings.multimodal_max_bytes`、`supported_extensions` == `SUPPORTED_FILE_EXTENSIONS`；`frontend/src` 不含写死的上限字节常量（静态检查） |
| **AC-5** | P0-3 | 表格类文件预览的表头/单元格与真实文件**逐字一致**；`ResourcePreviewResponse.truncated == true` 时可见截断提示 |
| **AC-6** | P0-3 | 文本类文件预览与真实文件前 N 行一致；非文件类资源显示「不支持内容预览」诚实空态，**无**伪造内容 |
| **AC-7** | P0-4 | 从历史资源库勾选的资源与新登记资源一样可随任务声明（提交 payload 的 `context.resources` 含其 id） |
| **AC-8** | P0-4 | 按类型筛选后列表 `items` 的 `kind` **全部**等于所选值；请求确以该 `kind` 为参数发出 |
| **AC-9** | P0-5 | 未配置解释器 ⇒ `engine.available() == false` 且 `degraded == "engine_unavailable"`（**保持** fail-closed，不猜解释器）；配置指向存在路径后 `available() == true` |
| **AC-10** | P0-6 | B 档代码任务：工作区路径**不在** ForgeFlow 项目树内；任务前后 ForgeFlow 仓库 `git status` **一致** |
| **AC-11** | P0-6 / P0-7 | 真实档代码任务产出**非空** `codeplane.diff` 且 `codeplane.tests.measured == true`；测试判定由 `tests_verdict.py::evaluate_test_output` 复算，与真实 pytest 输出一致 |
| **AC-12** | P0-6 | 代码任务的审批闭环（**区分两个 run**，勿混）：<br>① 产出 Diff 后，**代码任务 run** 为**待审批**（`status == "awaiting_approval"`，非「已完成」）；<br>② 批准后，**原 run** 的 `codeplane.approval.status` 翻为 `"approved"`，且 `decided_by` / `decided_at` 非空；<br>③ 批准后，**approve 响应返回的 resume run**（新 run）的 `artifacts` 至少各含一条 `code_diff` 与 `code_test_report`；<br>④ 既有报告类 run 的 `artifacts` 无回归 |
| **AC-13** | P0-7 | 降级不回归：A 档与「B 档未配置解释器」下，代码任务显式 `degraded`、`diff == ""`、`tests.measured == false`、状态非「已完成」，**不返回 5xx** |
| **AC-14** | P0-8 | 代码能力验收（真实档 + qwen3:8b）：连续 3 次运行中 **≥2 次**满足 `degraded is None` ∧ `verdict == "passed"` ∧ `exit_code == 0` ∧ Diff 命中目标文件 ∧ 源仓库工作树不变；每次留存原始 pytest 输出与 Diff |
| **AC-15** | P0-8 | 代码能力**机械判定**：通过与否由布尔量计算，`<2/3` 即**判定失败**（如实记录，不上调阈值） |
| **AC-16** | P0-9 | 分析能力验收（真实档 + qwen3:8b）：`report_markdown` 内容**精确包含**夹具行数 R 与聚合量 A 的数值 |
| **AC-17** | P0-9 | 分析产物诚实：报告数字源于夹具真实内容（非 stub / 模板）；未算出正确数字即**判定失败** |
| **AC-18** | P0-10 | A 档与 B 档 junit XML 均 0 failures/0 errors（**以 junit 为准**）；两档用例集合差为 0；`alembic upgrade head` 连跑两次幂等 |
| **AC-19** | 通用 | 既有 `data-testid`（`run-declare-table` / `run-declare-paths` / `resource-add` / `resource-kind-*` / `resource-list` / `resource-card` / `resource-empty-note`）仍可寻址且行为不变；本次新增 testid 不覆盖既有同名项 |
| **AC-20** | P0-1/P0-2/P0-3/P0-4 | A 档（memory+mock）下拖拽 / 预检 / 预览 / 筛选四条交互判定与 B 档一致（除真实内容差异）；A 档不返回 5xx |
| **AC-21** | P0-6 | 审批三动作仍为「批准修改 / 拒绝 / 重新分析」且各自可观测；拒绝不留改动（在**真实档**下重新验证 INC25 AC-18 成立） |

---

## 附：需求条目统计

- **P0**：10 条（上传体验 P0-1 ~ P0-4；代码执行面 P0-5 ~ P0-7；双能力验收 P0-8 ~ P0-10）
- **P1**：6 条
- **P2**：5 条
- **验收标准**：AC-1 ~ AC-21，共 **21** 条
- **待确认问题**：Q1 ~ Q5，共 5 条（每条含默认假设）
