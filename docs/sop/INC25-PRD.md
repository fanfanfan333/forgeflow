# INC25 增量 PRD — 任务资源中心（Resource Center） + 代码执行面（Code Execution Plane）

> 文档类型：增量 PRD（需求分析，不含实现细节）
> 上游输入：用户上传的 OpenHands 官方 `software-agent-sdk`（1.49.6，含 `openhands-sdk` / `openhands-tools` / `openhands-workspace` / `openhands-agent-server`，另含 `clients/`、`examples/`）+ 用户架构咨询长文
> 工程基线：`ForgeFlow-main`（FastAPI `forgeflow/` + React 手写 CSS `frontend/src/`）
> 运行档位：A 离线（`storage_backend=memory` + `llm_provider=mock`）/ B 真实（PG5433 + 本机 Ollama `qwen3:8b`，视觉 `qwen2.5vl:3b`）
> 端口：后端 8010 / 前端 5173 / PG 5433
> 本文件位于 `docs/`（本仓不提交的红线区），仅作草稿与评审用。

---

## 0. 现状锚点（先读代码得到的真实基线，作为需求边界）

### 0.1 用户点名的两个裸输入框（本增量 W1 的直接对象）

- `frontend/src/views/runs/RunListPanel.tsx::RunListPanel` —— 新建任务表单只有「一句 intent」+ 两个可选声明输入框，各自的 `data-testid` 为 `run-declare-table` / `run-declare-paths`，占位文案为「数据表名（可选）」「文件/仓库路径（可选，逗号分隔）」。
- `RunListPanel.tsx::submit` —— 既有纪律：**空输入不传键**（不传空串 / 空数组，避免后端把「未声明」当成「已声明」而改变判定）。
- `frontend/src/views/LiveRunsView.tsx::LiveRunsView` —— 该面板的真实宿主页；同页已有 `useCreateTask`（创建）、`useReplanRun`（重跑）两条真实通路，以及 `humanizeError` 的「不吞错」展示约定。
- `frontend/src/styles/runs.css`（`.run-create` 区块）—— 现有样式：仅用 `--border-default` / `--bg-inset` / `--fg-primary` / `--blue-4` 等 token；`frontend/src/styles/tokens.css` 定义全量 oklch token。
  ⇒ 本增量 UI 必须继续走「手写 CSS + token」，**禁止**引入 MUI / Tailwind。

### 0.2 后端现有的「附件」能力（本增量 W1 的既有地基）

- `forgeflow/api/routers/tasks.py::create_task` —— `POST /tasks` 已有 `attachments: list[AttachmentInput]`（增量式声明在路由侧，未拓宽 `hub_schemas`）。
- `forgeflow/runtime/attachments.py::AttachmentInput` —— 字段仅 `kind` / `name` / `data_base64`（base64 内联）。
- `attachments.py::SUPPORTED_KINDS` —— **仅** `("pdf", "image")`；其它 `kind` 记录为 `ignored`（`attachments.py::AttachmentResult` 的 `status`）。
- `attachments.py::prepare_attachments` —— 提取文本后**拼进 intent**；产出 `PreparedTask.context["attachments"]`（`name/kind/status/detail`）。
- `attachments.py::AttachmentTooLargeError` —— 超限即 `ValueError` 子类，路由映射为 **HTTP 413**；上限来自 `forgeflow/config.py::Settings` 的 `multimodal_max_bytes`（默认 5 MB）。
- 既有诚实纪律（必须沿用）：可选依赖缺失时**降级不抛错**，状态记为 `ignored` / `metadata_only`，绝不假装解析成功。
- **能力缺口（本增量要补）**：不支持表格类（CSV / Excel）→ 无行数 / 列数 / 字段名 / 数据质量；不支持代码仓库类（Git / ZIP / 分支）；无「资源」这一等公民概念，只有「一次性附件」。

### 0.3 后端执行面现状（本增量 W2 的缺口证明）

- `forgeflow/runtime/tool_handlers.py::code_run` —— **只做确定性校验**（解析 + 编译 Python 源码），文档注释明确写「never executes arbitrary code」；`paths` / `repo_path` 皆缺时返回 `not_executed`，执行器记为 `blocked`。
  ⇒ 平台**至今没有任何代码执行能力**；这是 W2 存在的根因，也是「OpenHands 补齐代码执行引擎」的正当性来源。
- `forgeflow/runtime/planning.py::TOOL_INPUT_CONTRACT` —— `code.run` / `code.lint` 需 `paths`，`git.diff` 需 `repo_path`；`planning.py::resolve_inputs` 只从 `explicit_inputs` 取表名 / 路径，**绝不推断**。
- `forgeflow/runtime/orchestrator.py::_EXPLICIT_INPUT_KEYS` / `::_declared_inputs` —— 只有调用方**真正声明**的键才会落到运行记录；`orchestrator.py::TaskCreate.context` 是唯一输入通道。
- `forgeflow/runtime/orchestrator.py::run_task` —— 选择执行器：`react` / `llm` / `deterministic`（`orchestrator.py::resolve_agent_runtime_mode`）；四层口径 L1 `plan` / L2 `tool_invocations`（唯一事实源）/ L3 `observations` / L4 `artifacts` 均在此写盘。
- `forgeflow/runtime/artifacts.py::artifacts_from_invocations` —— 产物投影**只认** `artifacts.py::ARTIFACT_TOOL`（`report.render`），kind 仅 `report_markdown`。
  ⇒ 代码任务的 Diff / 测试报告今天**无法**成为 artifact，必须扩投影（且不得破坏既有口径）。
- `forgeflow/runtime/tool_executor.py::ToolInvocation` —— 单条统一工具调用记录，含 `status` / `executed` / `invoked` / `development_stub` / `summary` / `latency_ms`（未测量即 `None`，**绝不为 0**）/ `payload`。
- `forgeflow/runtime/gate.py::TOOL_PERMISSION_MAP` / `::PLATFORM_TOOL_CATALOGUE` —— 每步执行前 RBAC 复检；未登记工具 fail-closed 到 `execute:<namespace>`。新增代码执行工具必须登记进这一体系。
- `forgeflow/mcp/server/tools/data_tools.py::query_db` —— **永久** development stub（返回合成行，`tool_handlers.py::data_query` 标注 `development_stub=True`）。⇒ 数据库资源在 A 档**不可能**有真实数仓。

### 0.4 双档抽象范式（资源层必须沿用）

- `forgeflow/repositories/factory.py::_construct` / `::get_experience_repository` —— `storage_backend` 决定 memory / postgres 双实现，延迟导入、进程内缓存、`::reset_repositories` 供测试切换。
  ⇒ 资源层**必须**沿用同一套做法（同一接口 + memory / postgres 双实现），不得另造第二套抽象。

### 0.5 前端既有渲染契约（W2 时间线的落点）

- `frontend/src/views/runs/realRun.ts::detailToStages` —— 真实 run → 舞台卡；`realRun.ts::measuredMs` 把未测量的 `latency_ms` 渲染为「—」；`realRun.ts::stepStatusToStageStatus` 已把 `blocked` 映射为「受阻」而**非**「失败」。
- `frontend/src/views/runs/ExecutionSection.tsx::ExecutionSection` —— 执行层**默认折叠**、`aria-expanded` / `aria-controls` / `hidden` 的可访问性契约。
- `realRun.ts::deriveEvidence`（L2 证据 1:1）、`realRun.ts::deriveDegradeNotice`（降级说明的业务化映射，工程原文只进 `title`）、`realRun.ts::deriveMissingInputs`（受阻步骤 + 原因原文）。
  ⇒ 「任务时间线」应在**这些既有派生函数之上**做呈现升级，不新增第二套状态词表。
- 既有 `data-testid` 纪律：**只增不改不删**。

---

## 1. 产品目标

### 1.1 一句话目标

把「新建任务」从**为测试后端参数而做的输入框**升级为**企业级任务资源中心**，并接入 OpenHands SDK 作为独立**代码执行面**，使知识任务 / 数据任务 / 代码任务三类场景共享同一条闭环（Orchestrator + Tools + Skills + Memory + Security + Verification + Artifact），其中代码任务首次具备「隔离执行 → 证据 → 人工审批 → 交付」的真实能力。

### 1.2 三条衡量标准（可测）

| 编号 | 衡量标准 | 判定方式 |
|---|---|---|
| **M1 资源可理解** | 五类任务资源（文件 / 数据库 / Git 仓库 / 知识库 / API）均可登记；文件类**真实上传并解析出真实资源摘要**（表格类含行数 / 列数 / 字段名 / 数据质量）；用户不再需要手敲逗号分隔路径 | 用固定夹具文件机械比对摘要数值；上传失败 100% 带逐字原因与正确 HTTP 语义 |
| **M2 代码闭环可完成** | 一次代码任务可完整走通「选代码资源 → 隔离工作区执行 → 结构化任务时间线 → Diff + 测试结果 → 人工审批 → Artifact」，且平台仓库工作树在任务前后**无变化** | 见 AC-11 / AC-12 / AC-13 / AC-14 / AC-16 |
| **M3 诚实零违规** | 双档（A 离线 / B 真实）行为一致；任何引擎 / 模型不可用均**显式降级声明**，绝不静默假装成功；`blocked` 不计 failed、`latency_ms` 未测量即 `None`（UI 显示「—」） | 见 AC-17 / AC-18 / AC-19 / AC-20 |

---

## 2. 用户故事

### W1 资源层

- **US-1（数据任务）** 作为业务分析师，我想把 Excel / CSV 直接拖进「任务资源」并立刻看到**行数 / 列数 / 字段名 / 数据质量**，这样我才能判断这份数据是否能支撑 Agent 完成任务，而不是先被告知「缺少表名」。
- **US-2（代码任务）** 作为开发者，我想用「Git 仓库 + 分支」或「本地路径」或「ZIP」把代码登记为任务资源，这样我不用在输入框里敲一串逗号分隔的路径，也不会因为写错分隔符就让整步受阻。
- **US-3（知识任务）** 作为知识工作者，我想选择「企业知识库 / API 接口」资源并看到它对本次任务的**可用范围与连通状态**，这样我清楚 Agent 读得到什么、读不到什么。

### W2 代码执行面

- **US-4（代码任务 + HITL）** 作为开发者，我想在 Agent 改完代码后看到「任务时间线 + 代码变更 Diff + 测试结果」，并由我点「批准修改 / 拒绝 / 重新分析」，这样我绝不会被一次静默的改动落到我的仓库上。
- **US-5（诚实降级）** 作为平台运维者，当代码执行引擎或本机模型不可用时，我想看到明确的降级说明**以及受影响的具体步骤**，而不是一条看起来正常的「已完成」。

### 跨场景

- **US-6（统一闭环）** 作为产品负责人，我希望知识 / 数据 / 代码三类任务背后是**同一套**编排、工具、记忆、安全、验证与产物机制，这样平台讲得出一个完整闭环，而不是三个互不相干的 demo。

---

## 3. 需求池（P0 必须 / P1 应当 / P2 可以）

> 约定：每条含「做什么」+「验收标准（可机械判定）」。涉及既有代码处一律以 `文件名::符号名` 锚定。

### 3.1 P0 — 资源层（W1）

#### P0-1 任务资源模型与五类资源的登记入口
- **做什么**：引入「任务资源」这一等公民概念，统一抽象 **文件 / 数据库 / Git 仓库 / 企业知识库 / API 接口** 五类。资源可**登记**（成为可复用条目）并可**随任务声明**进入执行上下文。资源的持久化必须沿用 `repositories/factory.py::_construct` 的 memory / postgres 双实现范式，并提供与 `factory.py::reset_repositories` 同款的测试期重置入口。
- **验收标准**：五类资源各有独立、可寻址的登记入口；登记成功后该资源在「资源列表」可见且带类型标识；A 档（`storage_backend=memory`）与 B 档（`postgres`）下登记 → 列表 → 详情的字段与语义一致。

#### P0-2 真实文件上传
- **做什么**：文件类资源必须支持**真实字节上传**（不再只有 base64 内联的 pdf/image 两态）。上传需经类型校验与大小上限校验；上限沿用 `config.py::Settings` 的 `multimodal_max_bytes` 作为**单文件**上限（除非另有决定，见 Q3）。
- **验收标准**：上传一个合法 CSV / Excel / PDF / 文本文件后，资源详情携带**真实字节数**（与磁盘上的夹具文件大小逐字节一致）；上传内容被真实落盘或真实入库（可被重新读取并复核）。

#### P0-3 资源摘要（表格类 / 文本类）
- **做什么**：
  - **表格类（CSV / Excel）**：摘要必须给出**行数、列数、字段名列表、数据质量检查**。
  - **文本类（text / PDF）**：摘要必须给出**字符数**（PDF 另有**页数**）；关键词仅在有真实来源（如真实解析出的关键词或用户自带）时呈现。
- **验收标准**：
  - 表格：用固定夹具 CSV，摘要的行数 / 列数 / 字段名与该文件真实内容**逐项相等**（机械比对）。
  - 数据质量：至少给出「存在空值的列及其占比」与「是否存在重复主键」两类结论；结论基于真实统计，非模板句。
  - 文本：字符数与 `len(text)` 一致；PDF 页数与 `multimodal/pdf.py::extract_pdf_text` 返回的页数一致。
  - **不得编造**：无真实关键词来源时，不得出现任何关键词字段或占位词。

#### P0-4 资源随任务进入执行上下文，且未声明不得臆造
- **做什么**：被声明的资源必须以**结构化形态**进入 `orchestrator.py::TaskCreate.context`，并被 `orchestrator.py::_declared_inputs` / `planning.py::resolve_inputs` 正确消费（例：Git 仓库资源应能同时满足 `repo_path` 与 `paths` 的语义；表格资源应能提供真实表名 / 文件路径）。**严格沿用**既有两条纪律：`RunListPanel.tsx::submit` 的「空输入不传键」，以及 `planning.py` 的「无声明不推断」。
- **验收标准**：
  - 未声明任何资源时，提交 payload **不含**资源键（键不存在，而非空数组 / 空串）。
  - `GET /runs/{id}`（`api/routers/runs.py::get_run` 的响应模型 `hub_schemas.py::RunDetailResponse`）的 `declared_inputs` **逐字**等于所声明的资源集合，不多一个键。
  - 声明了代码资源后，`planning.py::TOOL_INPUT_CONTRACT` 中依赖 `paths` / `repo_path` 的步骤**不再**因缺输入而受阻（或如实给出真实执行结果）。

#### P0-5 上传 / 解析失败必须逐字诚实
- **做什么**：失败必须区分并显式暴露：**超限（客户端错误）**、**类型不支持（客户端错误）**、**解析失败（服务端能力降级）**。超限沿用 `attachments.py::AttachmentTooLargeError` → HTTP **413** 的既有语义；解析失败沿用「降级不抛错」纪律（记 `ignored` / `metadata_only` + 原因），请求本身不得炸。
- **验收标准**：超限时 HTTP 413 且 `detail` 逐字同时包含**实际字节数**与**上限值**两个数字；类型不支持时给出逐字原因且**不产生**「已解析」状态条目；解析依赖缺失时资源状态为降级态（非成功），并带可读原因。

#### P0-6 前端「资源选择器」替换两个裸输入框
- **做什么**：把 `RunListPanel.tsx::RunListPanel` 的 `run-declare-table` / `run-declare-paths` 两个裸输入框升级为**资源选择器**（「+ 添加资源」入口 → 五类资源 → 已添加资源清单 + 资源摘要卡片）。视觉与交互沿用现有手写 CSS + oklch token（`styles/tokens.css` / `styles/runs.css`），禁止 MUI / Tailwind。
- **验收标准**：
  - 新建任务页存在可见的「+ 添加资源」入口，展开后五类资源各自可寻址。
  - 已添加资源以卡片呈现，卡片内可见**类型 + 名称 + 摘要关键值**（表格类须见行数 / 列数 / 字段名）。
  - 存在**空态诚实文案**：未添加资源时明确告知「Agent 不会读取任何文件或数据库」（不得留下空白或暗示已提供资源）。
  - **`data-testid` 纪律**：既有 `run-declare-table` / `run-declare-paths` 不得删除或改名（处置方案见 **Q1**）。
  - 提交后前端的失败展示沿用 `api/errors.ts::humanizeError`（保留状态码与后端 `detail`），**不吞错**。

#### P0-7 代码资源登记（仓库 / 本地路径 / ZIP + 分支）
- **做什么**：代码任务的资源**不得**只支持「上传代码 zip」。需支持：**GitHub / GitLab 仓库**、**本地路径**、**ZIP 包**，并支持指定**分支**（仓库类）。
- **验收标准**：三类代码来源各自可登记（可选分支）；登记后资源摘要至少给出：来源类型、标识（仓库全名 / 路径 / 文件名）、分支（若有）、可识别语言与文件数（真实统计，非估计）；凭据缺失或不可达时给出逐字失败原因，不得伪装成已就绪。

### 3.2 P0 — 代码执行面（W2）

#### P0-8 任务级隔离工作区执行
- **做什么**：代码任务必须在**任务级隔离工作区**内执行，工作区包含该任务所需的 repo / source / tests。**绝不允许** Code Agent 直接在 ForgeFlow 项目目录中改动。工作区生命周期须显式：创建 → 使用 → 产出（diff / 测试结果 / artifact）→ 回收或销毁。
- **验收标准**：在一次完整代码任务前后，ForgeFlow 仓库工作树状态无变化（`git status` 输出一致，无新增 / 修改 / 删除）；工作区的创建与回收在任务可观测范围内有记录。

#### P0-9 执行事件标准化 → 任务时间线（含「查看详细 Trace」）
- **做什么**：OpenHands 的原始事件流必须经 **ForgeFlow 事件适配层**标准化后再到达前端，把「一大坨执行日志」改为**任务时间线**：每步呈现状态图标 + 业务文案（例：`✓ 分析仓库` / `✓ 定位 login.py` / `● 正在运行测试` / `⚠ 2 tests failed`）；原始 Tool / Action / Observation 只在用户点「查看详细 Trace」后展开。
- **验收标准**：
  - 代码任务执行区存在「任务时间线」列表，项数 ≥ 实际发生的关键步骤数，每项含状态图标与文案。
  - 时间线**默认不展示**原始工具名 / Action / Observation 文本；点击「查看详细 Trace」后可见。
  - 时间线状态词表**复用**既有映射（`realRun.ts::stepStatusToStageStatus` 等），不新增第二套词汇；`blocked` 显示为「受阻」而非「失败」。
  - 未测量的耗时显示「—」，**绝不**显示 0。

#### P0-10 Diff 证据
- **做什么**：代码任务必须产出**可查看的代码变更（Diff）**，来源是隔离工作区内的真实改动。
- **验收标准**：任务详情可查看 Diff（按文件分组、含增删行数）；Diff 与工作区内的真实改动逐行一致（用固定夹具任务做机械比对）；无改动时显示诚实空态，不得生成占位 Diff。

#### P0-11 测试结果证据
- **做什么**：代码任务必须产出**测试结果证据**（通过数 / 失败数 / 错误数，以及未通过用例标识与执行命令），并且「通过」的判定权在 ForgeFlow 侧可复核（见 **Q4**）。
- **验收标准**：
  - 任务详情展示的通过 / 失败 / 错误计数与测试真实输出一致（固定夹具比对）。
  - 存在失败用例时，任务结论**不得**为「已完成」；失败用例名称逐字呈现。
  - 输出无法解析时记「未测量」，**不得**默认判为通过。

#### P0-12 代码修改必须 Human-in-the-loop
- **做什么**：`Agent 生成 Diff → 测试验证 → [批准修改][拒绝][重新分析] → commit / PR`。未获人工批准前，代码变更**不得**写入目标分支 / 目标仓库。
- **验收标准**：
  - 产出 Diff 后任务进入**待审批**状态（不得显示为已完成）；目标分支在工作区外**未被写入**。
  - UI 提供且仅提供三个动作入口：批准修改 / 拒绝 / 重新分析；三者各自产生可观测的状态变化（批准 → 交付；拒绝 → 终止且不留改动；重新分析 → 产生新的一轮执行）。
  - 审批动作与结果写入既有审计通道。

#### P0-13 Artifact 交付（Diff + 测试报告）
- **做什么**：批准后产出可交付 Artifact（至少含 **Diff** 与 **测试结果报告**），并可从任务详情查看 / 下载原文。产物投影需从当前「只认 `artifacts.py::ARTIFACT_TOOL`（`report.render`）」扩展为**增量覆盖**代码任务产出（新增产物 kind），且**不得**破坏既有 `artifacts.py::ARTIFACT_KIND_MARKDOWN` 口径与 `report.render` 的既有投影行为。
- **验收标准**：批准后 `GET /runs/{id}` 的 `artifacts` 中出现至少一条代码任务产物（含 Diff 内容与测试结论）；既有报告类 run 的 artifacts 数量与内容**不回归**（回归测试机械判定）；无产物时为诚实空态。

#### P0-14 引擎 / 模型不可用时诚实降级并显式声明
- **做什么**：当 OpenHands 代码执行引擎不可用、或本机 Ollama 模型不可用 / 未就绪时，必须**显式声明降级**并说明**受影响的具体步骤**与**已完成的部分**；绝不静默假装成功。降级信息须落在既有可读字段上（对标 `orchestrator.py::_llm_executor` 写 `runtime_meta["degraded"]` 的做法与前端 `realRun.ts::deriveDegradeNotice` 的消费口径），并沿用「工程原文只进 `title` 悬浮诊断、可见行只出业务表述」的约定。
- **验收标准**：
  - 引擎不可用：任务状态**不是**「已完成」；存在显式降级标识；UI 可见「已降级执行」类业务说明；受影响步骤被逐一点名。
  - 模型不可用：同上，且不得把确定性执行结果包装成模型产出。
  - 未登记的降级值：只做中性说明，**不编造**原因。

#### P0-15 双档行为一致（A 离线 / B 真实）
- **做什么**：A 档（`storage_backend=memory` + `llm_provider=mock`）与 B 档（PG5433 + Ollama）下，资源层与代码执行面的**行为与判定口径**必须一致；差异只允许出现在「内容由真实模型 / 真实仓库产生」这一层。
- **验收标准**：AC-2 / AC-5 / AC-7 / AC-9 / AC-14 在 A、B 两档下判定结果一致（除真实内容差异）；A 档下不得因无 PG / 无 Ollama 而崩溃或返回 5xx。

### 3.3 P1 — 应当具备

| 编号 | 做什么 | 验收标准 |
|---|---|---|
| P1-1 | 资源复用：从已登记资源库中挑选历史资源，而非每次重传 | 选择历史资源后任务声明的资源键与首次一致；重传同一文件不产生语义冲突（可识别为同一资源或明确新建） |
| P1-2 | 资源预览：表格类展示前 N 行、文本类展示前 N 字符 | 预览内容与真实文件逐字一致；超大文件有明确截断提示，不得静默截断 |
| P1-3 | 资源访问的 RBAC 校验：谁能用哪个资源 | 无权限角色登记 / 使用受控资源时被拒，且原因可读；被拒行为写入审计 |
| P1-4 | 资源 → 步骤的可追溯：任务详情中可见「哪个步骤用了哪个资源」 | 每个真实使用资源的步骤可追溯到资源标识；未使用资源的步骤不得挂资源 |
| P1-5 | 代码任务失败后的「重新分析」与有限次自动重试 | 重试次数受既有 `config.py::Settings` 的 `max_replan_attempts` 与既有 loop breaker 约束；超限转人工，不得无限重试 |
| P1-6 | 工作区生命周期可见 | 任务详情可见工作区的创建 / 回收 / 销毁时间点与保留期规则 |
| P1-7 | 代码任务成本与时延可见 | 沿用既有 `total_tokens` / `total_cost_usd` / `latency_ms` 口径；未测量即「—」，不为 0 |

### 3.4 P2 — 可以具备

| 编号 | 做什么 |
|---|---|
| P2-1 | 资源版本 / 指纹：同文件再上传可识别一致性 |
| P2-2 | 资源标签与搜索 |
| P2-3 | 审批通过后创建 PR / MR（**不**自动合并） |
| P2-4 | 代码任务会话续跑（跨任务保留工作区上下文） |
| P2-5 | 知识库 / API 资源的连通性自检报告 |
| P2-6 | 时间线按步骤分组折叠、按失败筛选 |

---

## 4. UI 设计稿（ASCII 线框）

> 视觉约束：沿用现有手写 CSS + oklch token（`styles/tokens.css`）；面板结构沿用既有 `panel / panel-head / panel-body`、按钮沿用 `.btn.primary`、注意条沿用 `.af-note`（警告用 `.af-note.warn`）、状态徽章沿用既有的 emerald / amber / red / blue tone 命名。**禁止** MUI / Tailwind。以下线框只表达层级与信息，不表达像素。

### 4.1 新建任务页（含资源选择器与资源摘要卡）

```
┌─ 运行列表 ─────────────────────────────────────────────────────────────────┐
│                                                                            │
│  ┌ 任务意图 ────────────────────────────────────────────┐  ┌───────────┐   │
│  │ 用一句话描述要完成的任务，例如：为 Acme 整理一份…      │  │ 运行任务  │   │
│  └──────────────────────────────────────────────────────┘  └───────────┘   │
│                                                                            │
│  任务资源（可选）                              已添加 2 项   [+ 添加资源 ▾] │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ ▤  销售线索_2026Q1.csv                                    表格 · 已解析 │ │
│  │    1,284 行 · 11 列 · 字段：lead_id, company, owner, amount, stage, …   │ │
│  │    数据质量：owner 空值 12.4% · stage 空值 0.8% · 主键 lead_id 唯一      │ │
│  │                                                          [预览] [移除] │ │
│  ├────────────────────────────────────────────────────────────────────────┤ │
│  │ ⌥  github.com/acme/portal                         Git 仓库 · 分支 main  │ │
│  │    Python · 98 个文件 · 含 tests/ · 最近提交 3f2a91c                    │ │
│  │                                                          [预览] [移除] │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│                                                                            │
│  ⓘ 未添加资源时，Agent 只按任务描述执行，不会读取任何文件、仓库或数据库。   │
└────────────────────────────────────────────────────────────────────────────┘

「+ 添加资源」展开：
┌─ 添加资源 ─────────────────────────┐
│  ▤   上传文件                      │   Excel / CSV / PDF / 文本
│  ⛁   选择数据表                    │   已登记的库表（离线档无真实数仓）
│  ⌥   Git 仓库                      │   GitHub / GitLab / 本地路径 / ZIP
│  ⌘   企业知识库                    │   文档集 / 知识库
│  ⇄   API 接口                      │   已配置的连接器与接口
└────────────────────────────────────┘

上传失败（诚实态，逐字）：
┌────────────────────────────────────────────────────────────────────────────┐
│ ⚠ 上传失败：文件为 12,582,912 字节，超过单文件上限 5,242,880 字节。        │
└────────────────────────────────────────────────────────────────────────────┘
```

### 4.2 代码任务执行时间线卡片

```
┌─ 任务时间线 ───────────────────────────────────── 6 步 · 用时 2 分 14 秒 ──┐
│                                                                           │
│  ✓  分析仓库结构                                              12 秒        │
│     已识别 98 个 Python 文件、3 个入口模块、测试目录 tests/                │
│                                                                           │
│  ✓  定位登录逻辑                                              31 秒        │
│     命中 auth/login.py、auth/session.py                                   │
│                                                                           │
│  ✓  生成修改方案                                              48 秒        │
│     计划修改 2 个文件：auth/login.py、tests/test_login.py                  │
│                                                                           │
│  ●  正在运行测试                                              —           │
│     pytest -q                                                             │
│                                                                           │
│  ⚠  2 个测试未通过                                            —           │
│     tests/test_session.py::test_refresh_expired                           │
│                                                                           │
│  ○  等待人工审批                                              —           │
│                                                                           │
│  ───────────────────────────────────────────────────────────────────────  │
│  [ 查看详细 Trace ▾ ]   展开后显示 Tool / Action / Observation 原文        │
└───────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Diff / 测试 / 审批区块

```
┌─ 代码变更 ─────────────────────────────────────────── 2 文件 · +41 / −17 ─┐
│  auth/login.py                                                  [展开 ▾]  │
│    @@ -12,7 +12,9 @@                                                      │
│    -      if token is None:                                               │
│    +      if token is None or token.expired:                              │
│             raise AuthError("invalid token")                              │
│                                                                           │
│  tests/test_login.py                                            [展开 ▾]  │
│    （+18 / −0）                                                           │
├───────────────────────────────────────────────────────────────────────────┤
│ ─ 测试结果 ────────────────────────────────────────────────────────────── │
│  ✓ 12 通过      ⚠ 2 未通过      ✗ 0 错误        命令：pytest -q            │
│                                                                           │
│  未通过：                                                                  │
│    tests/test_session.py::test_refresh_expired                            │
│    tests/test_session.py::test_logout_clears_cookie                       │
├───────────────────────────────────────────────────────────────────────────┤
│ ─ 需要你的决定 ────────────────────────────────────────────────────────── │
│  未获批准前，代码变更不会写入目标分支。                                    │
│                                                                           │
│  [  批准修改  ]   [  拒绝  ]   [  重新分析  ]                              │
└───────────────────────────────────────────────────────────────────────────┘
```

### 4.4 降级声明区块（引擎 / 模型不可用）

```
┌─ 降级说明 ───────────────────────────────────────────────────────────────┐
│ ⚠ 代码执行引擎当前不可用，本次未执行任何代码改动。                        │
│                                                                          │
│   受影响步骤（3 步未执行）：定位登录逻辑 · 生成修改方案 · 运行测试         │
│   已完成部分：分析仓库结构                                                │
│   说明：启用代码执行引擎后重新运行可获得完整的变更与测试证据。             │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 5. 待确认问题（最多 5 条，含默认假设）

| 编号 | 问题 | 我的默认假设（未澄清前按此执行） |
|---|---|---|
| **Q1** | 既有 `data-testid` `run-declare-table` / `run-declare-paths` 的处置：本仓纪律为「`data-testid` 只增不改不删」，而本增量要求把这两个裸输入框**升级**为资源选择器。二者存在冲突。 | **保留**旧控件为「兼容 / 高级」折叠入口，`data-testid` 与 `RunListPanel.tsx::submit` 的「空输入不传键」行为**逐字不变**；资源选择器以**新增** `data-testid` 增量并列存在，两个入口提交到**同一个** context 契约（键名与取值语义一致）。若架构师决定迁移 testid，须显式记录迁移清单并经批准。 |
| **Q2** | 数据库资源在 A 档（`storage_backend=memory`）没有真实数仓 —— `mcp/server/tools/data_tools.py::query_db` 是**永久** development stub（`tool_handlers.py::data_query` 标 `development_stub=True`）。A 档下「数据库资源」应做到什么程度？ | A 档下数据库资源**只能登记 + 显示「离线档无真实数仓」**，并如实标注为 development stub；**不得**产出任何"看起来真实"的数据行。B 档才允许连接 PG5433 上的既有表。 |
| **Q3** | 文件上传的传输方式：现有 `POST /tasks` 的 `attachments` 是 **base64 内联**、仅 pdf/image、上限 `config.py::Settings` 的 `multimodal_max_bytes`（5 MB）。资源层是**新增独立上传入口**（先上传得资源 ID，任务体只引用 ID），还是继续内联？ | **新增独立「资源登记 / 上传」入口**，任务体只引用资源 ID（避免大文件内联进 `POST /tasks`）；`multimodal_max_bytes` 作为**单文件**上限沿用；`attachments` 既有契约保持向后兼容、不删除。 |
| **Q4** | 「测试通过」的判定权归属：由 OpenHands 侧上报结论，还是 ForgeFlow 侧按原始输出复核？ | **OpenHANDS 上报原始输出**（stdout / 退出码 / 结构化用例结果），**ForgeFlow 侧复核**并标注口径；无法解析时为「未测量」，**绝不**默认判为通过。 |
| **Q5** | 审批后的落库语义：commit 到哪个分支、是否允许直接 push 到主线？ | 批准后 commit 到**任务工作区分支**，产出 Diff + 测试报告 Artifact；**不** push 主线、**不**自动开 PR（PR 列为 P2）。拒绝则不留任何改动。 |

---

## 6. 非目标（Out of Scope，本次明确不做）

1. **不 fork / 不 vendor OpenHands**：不把 `software-agent-sdk` 源码复制进本仓，不作为本仓子系统维护。
2. **不引入 Agent Server + Docker 集群编排**：不做容器编排、不做多机调度、不做 `openhands-agent-server` 的 Docker runtime 集群化部署。
3. **不做 CRM / ERP 连接器矩阵扩展**：不新增 Salesforce / Jira / SAP / QuickBooks / Microsoft Graph 等连接器，也不做连接器市场的横向铺开。
4. **不改既有四层数据契约与既有 API 语义**：L1/L2/L3/L4 口径、`hub_schemas.py::RunDetailResponse` 字段、既有 `attachments` 契约均为**增量为限**，不删不改语义。
5. **不做资源层的第二套抽象**：不另造与 `repositories/factory.py` 平行的存储抽象。
6. **不做工作区多人共享 / 实时协作编辑**。
7. **不自动合并代码**：不做自动 merge、不做自动 PR（PR 仅 P2）。
8. **不做非代码任务的 OpenHands 化**：知识任务 / 数据任务仍走既有 Orchestrator + 工具链，不强行接入代码执行引擎。
9. **不新增向量检索 / RAG 机制**：记忆与检索沿用既有实现。
10. **不做资源内容的 DLP 深度扫描新规则**：沿用既有 `Settings.dlp_enabled` 与既有规则集。

---

## 7. 验收标准汇总表（每条可被测试机械判定）

| 编号 | 关联需求 | 验收标准（机械可判定） |
|---|---|---|
| **AC-1** | P0-1 / P0-6 | 新建任务页可见「+ 添加资源」入口；展开后**五类**资源（文件 / 数据库 / Git 仓库 / 知识库 / API）各有独立可寻址的 `data-testid`，点击后进入各自的登记流程 |
| **AC-2** | P0-2 | 上传夹具 CSV（已知字节数）后，资源详情/任务详情的资源条目携带**真实字节数**，与磁盘文件大小**逐字节相等**；资源状态为「已解析」而非「仅元数据」 |
| **AC-3** | P0-3 | 夹具 CSV 的资源摘要中，行数 / 列数 / 字段名列表与该文件真实内容**逐项相等**（机械比对，非模糊匹配） |
| **AC-4** | P0-3 | 数据质量结论基于真实统计：空值列及其占比与真实统计一致；存在重复主键时如实报出，不存在时不报 |
| **AC-5** | P0-3 | 文本类资源摘要含字符数且等于真实字符数；PDF 资源摘要含页数且等于真实页数；无真实关键词来源时**不存在**关键词字段或占位词 |
| **AC-6** | P0-5 | 上传超上限文件 → HTTP **413**，`detail` 中**同时**出现实际字节数与上限值两个数字（逐字） |
| **AC-7** | P0-5 | 上传不支持类型 → 请求被拒并给出逐字原因；该资源**不出现**「已解析」状态条目 |
| **AC-8** | P0-5 | 解析依赖缺失时，资源状态为降级态（`ignored` / `metadata_only` 语义）并带原因；HTTP 响应**不是** 5xx |
| **AC-9** | P0-4 | **不选任何资源**提交时，请求 payload 中不包含资源键（键不存在）；运行结果的 `declared_inputs` 为空对象 |
| **AC-10** | P0-4 | 声明了代码资源后，`GET /runs/{id}` 的 `declared_inputs` **逐字**等于所声明资源，无多余键；依赖 `paths` / `repo_path` 的步骤不再因缺输入受阻（或给出真实执行结果） |
| **AC-11** | P0-8 | 一次完整代码任务前后，ForgeFlow 仓库工作树状态一致（`git status` 输出相同，无新增/修改/删除） |
| **AC-12** | P0-8 / P1-6 | 任务可观测范围内存在工作区创建与回收记录；工作区路径不在 ForgeFlow 项目目录内 |
| **AC-13** | P0-9 | 代码任务执行区存在「任务时间线」列表（项数 ≥ 实际关键步骤数），每项含状态图标 + 业务文案；**默认**页面可见文本中不出现原始工具名 / Action / Observation；点「查看详细 Trace」后可见 |
| **AC-14** | P0-9 | 时间线中未测量耗时显示「—」，页面中不存在该步骤的 `0 ms` / `0 秒`；`blocked` 步骤显示为「受阻」且**不计入**失败数 |
| **AC-15** | P0-10 | 任务详情可查看按文件分组的 Diff（含增删行数）；Diff 内容与隔离工作区内的真实改动**逐行一致**；无改动时为诚实空态 |
| **AC-16** | P0-11 | 测试结果计数（通过/失败/错误）与真实测试输出一致；存在失败用例时任务结论**不是**「已完成」，且失败用例名逐字呈现；输出不可解析时记「未测量」而非通过 |
| **AC-17** | P0-12 | 产出 Diff 后任务状态为**待审批**（非已完成）；目标分支在工作区外未被写入 |
| **AC-18** | P0-12 | UI 存在且仅存在三个审批动作入口（批准修改 / 拒绝 / 重新分析）；三者各自产生可观测的状态变化；拒绝后不留下任何改动 |
| **AC-19** | P0-12 | 审批动作及其结果写入既有审计通道（可按 run 检索到） |
| **AC-20** | P0-13 | 批准后 `GET /runs/{id}` 的 `artifacts` 至少含一条代码任务产物（含 Diff 内容与测试结论）；既有报告类 run 的 artifacts 数量与内容**无回归** |
| **AC-21** | P0-14 | 引擎不可用时：任务状态**不是**「已完成」；存在显式降级标识；UI 可见业务化降级说明；受影响步骤被逐一点名 |
| **AC-22** | P0-14 | 模型不可用时：同上；确定性执行结果**不得**被包装为模型产出；未登记降级值只做中性说明（不编造原因） |
| **AC-23** | P0-15 | AC-2 / AC-6 / AC-9 / AC-13 / AC-17 在 A 档（`memory` + `mock`）与 B 档（PG5433 + Ollama）下判定结果一致（除真实内容差异）；A 档不返回 5xx |
| **AC-24** | P0-6 | 既有 `data-testid` `run-declare-table` / `run-declare-paths` 仍可寻址且行为不变（或按 Q1 结论迁移并有书面记录）；本次改造新增的 `data-testid` 不覆盖既有同名项 |

---

## 附：需求条目统计

- **P0**：15 条（资源层 P0-1 ~ P0-7；代码执行面 P0-8 ~ P0-14；跨档 P0-15）
- **P1**：7 条
- **P2**：6 条
- **验收标准**：AC-1 ~ AC-24，共 **24** 条
