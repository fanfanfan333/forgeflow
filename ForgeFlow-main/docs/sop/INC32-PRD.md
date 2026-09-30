# INC32 增量 PRD — 从「Agent 控制台」演进为「AI 工作空间」

> 文档类型：**简单** PRD（不做竞品 / 市场分析）
> 增量编号：**INC32**（承 INC25/26/27 系列）
> 上游输入：用户逐条拍板的 **10 项决策（Q1–Q10）**；主理人（team-lead）前置侦察结论；现有代码基线（已逐处核验，见 §2）
> 工程基线：`ForgeFlow-main`（FastAPI `forgeflow/` + 手写 CSS 的 React `frontend/src/`）
> 本文件位于 `docs/sop/`（**不覆盖**任何历史文档；不提交红线区；**不要 git add**）
> 引文纪律：一律 `文件名::符号名` 锚定；**严禁** `file.py:行号`
> 状态：**待架构师（software-architect）评审 → 进入 INC32-DESIGN**

---

## 0. 项目信息

- **Language**：简体中文
- **Programming Language**：前端沿用既有栈（Vite + React 19 + TypeScript + TanStack Router/Query + **手写 CSS**，**禁** MUI / Tailwind / styled-components，**禁**新建 vitest）；后端沿用既有栈（FastAPI + asyncpg 裸 SQL + dataclass + Pydantic 边界），**不比照 SQLAlchemy ORM 重构**。
- **Project Name**：`inc32_ai_workspace_shell`
- **原始需求复述**：把 ForgeFlow 的**前端体验**从「Agent 管理 / 任务控制台」演进为「AI 工作空间」：用户只输入任务 → Agent 自动理解 → 实时展示简洁执行状态 → 产出结构化 Result / Artifact → 用户可继续操作 → Follow-up 复用**真实**上下文。技术细节默认隐藏，需要时经「查看执行详情」展开。**这是产品级重构，但必须是增量演进，不是推倒重写**；现有企业级 Agent 能力与 `/tasks` 路由保持不变。

---

## 1. 产品目标（正交三目标）

| 编号 | 目标 | 可测口径 |
|---|---|---|
| **G1 入口极简** | 打开即用：首页首屏只剩「输入任务 → 发起」，不再像传统后台管理系统 | 首页首屏（1024×768 首屏高度内）可见的**可交互操作**收敛为：1 个输入框 + 1 个提交按钮 + ≤4 个建议 chip + 近期任务列表；KPI/Agent/Skill/安全卡全部在第二屏或折叠区 |
| **G2 会话可续** | `/tasks` 原地演进为「对话 + 执行过程 + 结果/产物」的工作空间，信息层级为产品形态而非后台 | 同一页面内可同时看到：左侧历史任务、中间「我的任务 → 实时执行状态 → 最终结果」、右侧产物预览；技术细节默认折叠 |
| **G3 诚实且可续上下文** | 无数据只给诚实空态；Follow-up 走**真实**上下文（非前端状态拼接） | 全站 0 处 demo/伪造运行数据；Follow-up 产生的 run 在**后端**可回溯其父 run 关系，且新 run 的规划/执行**真的**消费上一轮上下文（可机械断言） |

---

## 2. 现状分析（用户明确要求：先做现状）

> 本节全部落在**已核验**的代码事实上；未核实项一律不写。

### 2.1 现有前端信息架构总览（全部 23 个顶层视图）

| 路由 | 视图组件（`frontend/src/views/`） | 职责 | **本轮处置** |
|---|---|---|---|
| `/` | `HomeView.tsx::HomeView` | 首页：Hero 输入 + 4 KPI + Agent 网格 + 技能行 + 右栏（近期任务 / 智能执行日志 / 安全概览） | **重构（极简化）** |
| `/tasks`、`/runs` | `LiveRunsView.tsx::LiveRunsView` | 智能任务：运行列表 + 结果优先四 Tab | **原地演进（核心会话页，不新增 `/chat`、不替换路由）** |
| `/skills` | `SkillsView.tsx` | 技能中心 | 不动（阶段 1–6 不参与） |
| `/knowledge` | `KnowledgeView.tsx` | 知识库 | 不动 |
| `/memory` | `MemoryView.tsx` | 记忆管理 | 不动 |
| `/security` | `SecurityView.tsx` | 安全与权限 | 不动 |
| `/analytics`、`/cost` | `CostView.tsx` | 数据分析 / 成本 | 不动（阶段 7 范围） |
| `/ops` | `OpsView.tsx` | 运维监控 | 不动（阶段 7 范围） |
| `/settings`、`/rbac` | `RbacView.tsx` | 用户 / 角色 / 策略 | 不动（阶段 7 范围） |
| `/overview` | `OverviewView.tsx` | 平台概览 | 不动 |
| `/approvals` | `ApprovalsView.tsx` | 审批 | 不动 |
| `/agents` | `AgentsView.tsx` | Agent 工作台 | 不动（首页 Agent 卡仍在，见 §3.1） |
| `/evals` | `EvaluationsView.tsx` | 评测 | 不动 |
| `/workflows` | `WorkflowsView.tsx` | 工作流 | 不动 |
| `/tools` | `ToolsView.tsx` | 工具 | 不动 |
| `/marketplace` | `MarketplaceView.tsx` | 市场 | 不动 |
| `/audit` | `AuditView.tsx` | 审计 | 不动（阶段 7 范围） |
| `/clusters` | `ClustersView.tsx` | 集群 | 不动 |
| `/welcome` | `LandingPage.tsx` | 独立落地页（无壳） | 不动 |
| `/architecture` | `ArchitecturePage.tsx` | 架构页（无壳） | 不动 |
| `/design-hub` | `DesignHubPage.tsx` | 设计枢纽（无壳） | 不动 |
| `/design-system` | `DesignSystemPage.tsx` | 设计系统（无壳） | 不动 |
| `/docs`、`/docs/$slug` | `DocsPage.tsx` | 文档（无壳） | 不动 |
| `/console/*` | `router.tsx::consoleRedirectRoute` | 历史链接重定向 | 不动（保留） |

> 设计令牌源：`frontend/src/styles/tokens.css`（全 `oklch()`，**dark-first**，`.btn` `.card` `.badge` `.dot` `.kbd` 等原子类）；全局入口 `main.tsx` 只 import `styles/tokens.css` 与 `styles/dashboard.css`。

### 2.2 过度复杂 / 需删除 / 隐藏 / 合并 / 重构（逐条 + 依据）

| # | 问题 | 依据（`file::symbol`） | 处置 |
|---|---|---|---|
| A1 | **首页有 7 个并列板块**（Hero / 4 KPI / Agent / Skill / 近期任务 / 智能执行日志 / 安全概览），一屏内同时争夺注意力，读起来像「数据看板」而非「工作入口」 | `HomeView.tsx::HomeView` 的 `home-main` + `home-rail` 结构 | KPI / Agent / Skill / 安全 → **下移第二屏或折叠**；首页核心目标收敛为「输入任务 → 执行」 |
| A2 | **「智能执行日志」在首页作为默认信息出现**，对普通用户是噪音（它是工程事件流的业务化投影） | `HomeView.tsx::ExecutionLog`（消费 SSE `/runs/{id}/events`）+ `HomeView.tsx::toLogLine` | **删除首页展示**，不作为普通用户默认信息（能力保留在会话页的执行过程区） |
| A3 | **首页 Hero 文案是「平台介绍」而非「行动号召」**：`你好，欢迎使用 企业级 Multi-Agent 智能工作与技能资产平台` + 副标题讲技术栈 | `HomeView.tsx::Hero` | **重构文案**：以「输入任务」为中心，副标题收敛为一句身份/能力提示 |
| A4 | **同一信息在多个层级重复**：运行列表（`/tasks` 左上）、首页「近期任务」、首页「智能执行日志」三处都在讲「最近的运行」 | `RunListPanel.tsx::RunListPanel`、`HomeView.tsx::RecentTasks`、`HomeView.tsx::ExecutionLog` | 首页只留一处「近期任务」摘要；执行过程统一归会话页 |
| A5 | **`/tasks` 是「列表 + 单 run Tab 面板」的后台形态**，不是「对话 → 执行 → 结果」的产品形态；没有左侧历史会话分组，也没有右侧产物预览区 | `LiveRunsView.tsx::LiveRunsView`（`RunListPanel` 在上、`RunHeader` + `ResultPanel` 在下，单列堆叠） | **原地演进为三列工作空间**（详见 §3.2），路由与 testid 不变 |
| A6 | **`/tasks` 的示例运行（demo）会冒充真实结果**（见 §2.3，最严重） | `LiveRunsView.tsx::LiveRunsView`（`showDemo` 分支）+ `runs/demoData.ts::DEMO_RUN` + `runs/panels.tsx` | **清除**，改诚实空态 |
| A7 | **孤儿样式文件**：`src/index.css`（Vite 模板残留，含 `#root { width: 1126px }` 等 Vite 默认样式）与 `src/App.css` **无任何 import** | 核验：全仓 `*.ts|*.tsx` 对 `index.css` / `App.css` **零命中**；`main.tsx` 只 import `tokens.css` + `dashboard.css` | **可清理**（低风险；建议由架构师确认后删除，或明确标注为「保留但禁用」） |
| A8 | **无浅色主题，无主题切换**：设计系统 dark-first，全站无 `data-theme` / 无切换入口 | `tokens.css`（`:root` 直接是暗色）+ 全仓 `data-theme` **零命中** | 新增 **light 覆盖层 + 主题切换**（详见 §6） |
| A9 | **首屏技术细节密度**：`RunHeader` 默认把 `run_id + status`（debug 档更含 provider/tokens/cost）暴露在标题下 | `LiveRunsView.tsx::RunHeader` / `::ViewModeToggle` | 复用 `ViewMode`：**默认 `concise`**，把工程事实收进「执行详情」 |

### 2.3 ⚠️ 缺陷正面处理：demo 数据回退（用户已拍板 Q6：清除）

**事实（已核验）**：

- `LiveRunsView.tsx::LiveRunsView` 在租户**没有任何 run** 时（`showDemo = !hubRuns.isLoading && list.length === 0`），会回退渲染一套**固定假数据** `DEMO_RUN`（`runs/demoData.ts::DEMO_RUN`）——含「研究 → 分析 → 起草 → 人工审核 → 发送」5 个阶段、完整工具调用、记忆召回、审批卡（`DEMO_APPROVAL_STAGE_ID`）、指标（`DEMO_METRICS`）、事件流（`DEMO_EVENTS`）、火焰图（`DEMO_TRACE`）、状态 diff（`DEMO_STATE_DIFF`）、Agent 表（`DEMO_AGENTS`）与原始 JSON（`DEMO_RAW_JSON`）。
- `runs/panels.tsx` 顶部注释**自认**「All data is fixed demo content」；`demoData.ts` 注释自认「this is fixed demo content (`wf_8K42n`). The page must always label it as such.」
- 后果：**用户看到的「漂亮结果」可能是假的，而真实跑出来的反而单薄**（真实的 artifact 仅由 `report.render` / `code.commit` 投影产生，见 §2.5）。

**处置（本轮 P0-3）**：

1. **删除 demo 回退路径**：`LiveRunsView.tsx` 的 `showDemo` 分支、`RunDetailDrawer` 的 demo 可达性、以及全部 `DEMO_*` 常量（`demoData.ts` / `panels.tsx` / `views/runs/types.ts::DemoRun`）从**渲染路径**移除。
2. **改为诚实空态**：无 run ⇒ 「暂无运行记录」+ 一个真实的新建入口；run 存在但无产物 ⇒ 沿用既有诚实空态（`ResultDetails.tsx::ResultDetails` 的 `result-empty`）。具体文案见 §7.2。
3. **纪律**：不得以「体验更好」为由保留任何假数据；「暂无」优于「编造」。
4. **`data-testid` 纪律**：demo 分支被移除时，**只删属于 demo 的用法**，不得删改任何真实路径上的既有 `data-testid`（当前 `src/` 内约 **99 处**，已核验与侦察一致）。

### 2.4 已实现的能力（**不要重复造**，本轮复用为主）

> 这是本轮最易做重复功的地方。以下能力**已经存在且经过 INC14–INC29 迭代**，INC32 只做**形态重排**，不做重写。

| # | 已有能力 | 依据（`file::symbol`） | 本轮态度 |
|---|---|---|---|
| B1 | **结果优先四 Tab**：`RunTab = 'result' \| 'evidence' \| 'trace' \| 'cost'`，每类信息只有一处归属，切 Tab 不重复内容 | `runs/types.ts::RunTab` + `ResultPanel.tsx::ResultPanel`（`TABS` + 四个 `role="tabpanel"`） | **复用**（作为「查看执行详情」的内层，见 §3.3） |
| B2 | **技术细节折叠（双密度）**：`ViewMode = 'concise' \| 'debug'`，`concise` 显示业务语、`debug` 显示原始工具名 + model/tokens/cost/checkpoint；且持久化到 `localStorage` | `runs/types.ts::ViewMode` + `RunStageCard.tsx::RunStageCard` + `runs/useViewMode.ts::useViewMode` | **复用**（作为三档中的「简洁 / 管理员」两档） |
| B3 | **执行过程默认折叠**：`ExecutionSection` 为可折叠容器，`<details>` 默认无 `open` | `ExecutionSection.tsx::ExecutionSection`（受控 `open` + `hidden`） | **复用** |
| B4 | **交付状态六态**：`waiting / need_approval / failed / blocked / partial / completed`，**只由既有字段派生** | `runs/types.ts::DeliveryState` + `realRun.ts::deliveryState` | **复用**（会话页的状态徽章直接用它） |
| B5 | **「下一步动作」三档互斥**：`agent`（真调后端）/ `self`（本地编辑态，不调）/ `view-diff`（切 Tab，不调） | `runs/types.ts::NextActionKind` + `ResultNextActions.tsx::ResultNextActions` | **复用**（是 Follow-up 的天然挂载点） |
| B6 | **实时流式 + 轮询降级**：`fetch` + `getReader()` 手写 SSE 解析（因需带 `Authorization` 头）；SSE 失败**真的**降级为每 2s 轮询 `GET /runs/{id}` 并合成时间线 | `api/sse.ts::subscribeRunEvents` + `hooks/useRunEvents.ts::useRunEvents`（`synthEvents`） | **复用**（P0-5 直接基于它） |
| B7 | **证据层 1:1 投影**：一条证据 = 一条 L2（`tool_invocations[i]`），条数严格 1:1，可逐字对齐 | `runs/types.ts::RunEvidence` + `realRun.ts::deriveEvidence` + `ResultPanel.tsx` 的 `result-evidence` 区块 | **复用** |
| B8 | **结构化结果全部派生自产物正文**（按 `##` 切章节、按列表项取发现、按表格取指标）；「正文里没有的，界面上就不出现」 | `runs/types.ts::RunSection/RunMetric` + `realRun.ts::deriveSections/deriveMetrics/deriveFindings` + `deriveConclusions` | **复用**（这是「诚实产物」的基石） |
| B9 | **首页已有任务输入框 + 角色感知建议 chip + 只读身份禁用** | `HomeView.tsx::Hero` + `home/roleConfig.ts::roleConfigFor`（`canExecute`） | **复用**（P0-2 只是把它变成首屏唯一主角） |
| B10 | **运行列表 + 新建任务 + 资源登记/勾选** | `RunListPanel.tsx::RunListPanel` + `ResourcePicker.tsx` | **复用**（左列基础） |
| B11 | **产物本地编辑 / 导出 Markdown / 复制 / 打印 / 存入知识库** | `runs/useArtifactEdit.ts::useArtifactEdit` + `ResultNextActions.tsx` 的「更多操作」 | **复用**（右列产物操作区基础） |
| B12 | **代码执行面**：时间线（stepper）+ diff + 测试结论 + 三动作审批 | `CodeTaskTimeline.tsx::CodeTaskTimeline` + `CodeApproval.tsx::CodeApproval`（挂在 `ResultPanel.tsx` 的 `code-plane`） | **复用** |
| B13 | **真实「重新运行」**：`POST /runs/{id}/replan`，且会重新声明原任务的输入 | `hooks.ts::useReplanRun` + `routers/runs.py::replan_run` | **复用** |
| B14 | **SPA 骨架 / 布局 / 路由 / 主题令牌** | `AppShell.tsx::AppShell` + `Sidebar.tsx` + `Topbar.tsx` + `router.tsx::shellChildren` + `tokens.css` | **复用** |

### 2.5 真正缺失的能力（INC32 要补的）

| # | 缺口 | 依据（`file::symbol`） | 影响 |
|---|---|---|---|
| C1 | **无 `session` / `conversation` / `parent_task` 关系**：全仓 `session_id` 零命中；`conversation_id` 仅存在于**附属**的 OpenHands 风格 code-agent server（`codeplane/runner/agent_server/*`），不是主 API；无 `parent_task_id` / `parent_run_id` / follow-up 字段 | 核验：`forgeflow/` 内 `session_id` 零命中；`conversation_id` 仅 `codeplane/runner/agent_server/`；仅有 `workflow_runs.task_id` / `experiences.run_id` / `run_steps.run_id` 这类**正向**关联 | **Follow-up 无法承重**（P0-10 的核心） |
| C2 | **现有「继续执行」并未真正传递上一轮上下文**：前端把 `continued_from_run_id` / `continued_from_artifact_ref` 塞进 `context`，但编排器只消费**白名单** `_EXPLICIT_INPUT_KEYS`，该键不在白名单内 | `LiveRunsView.tsx::onContinue` + `ResultPanel.tsx::continueWith` + `orchestrator.py::_EXPLICIT_INPUT_KEYS` / `::_capability_context` / `::_declared_inputs`；**代码注释自认**（`ResultNextActions.tsx` 的 `title`：「当前运行时不会将其注入新运行的执行计划」） | 现有「继续」≈**换了个 intent 的全新 run**；用户以为是续聊，实际不是 |
| C3 | **无 stop / abort 端点**：`run.aborted` 在终态词表内，但**无 emit 点** | `runtime/events.py::TERMINAL_EVENTS`（含 `run.aborted`）；`routers/runs.py` 仅有 `list_runs` / `get_run` / `run_events` / `replan_run`，**无** abort | 用户无法停止长任务（P0-9） |
| C4 | **无产物下载 / 预览端点**：全仓无 `FileResponse` / `StaticFiles`（仅 2 处 `StreamingResponse`：SSE 与 workflows stream）；**无 `artifacts` 表**，产物是内存投影 | 核验：`forgeflow/` 内 `FileResponse` 零命中；`runtime/artifacts.py::artifacts_from_invocations`（挂在 `RunRecord.artifacts`） | 产物**无法下载/预览为文件**（P0-8） |
| C5 | **产物类型全是文本**：`kind` 只有 `report_markdown` / `code_diff` / `code_test_report` | `runtime/artifacts.py::ARTIFACT_KIND_MARKDOWN` / `::ARTIFACT_KIND_CODE_DIFF` / `::ARTIFACT_KIND_CODE_TEST` | 「文件/Artifact 预览区」内容单薄（P0-7/P0-8） |
| C6 | **主流程不是异步派发**：`POST /tasks` 在**请求内** `await run_task(...)` 跑到终态才返回句柄，前端为「提交 → 等它跑完 → 再订阅（回放）」 | `routers/tasks.py::create_task`（`handle = await run_task(task, ctx)`）；`RunListPanel.tsx` 注释自认「runs to a terminal state before responding」；`events.py::RunEventBus.stream`（重连会**回放** history 环形缓冲） | **「输入任务 → 实时看它跑」在架构上被阻塞**：SSE 能力是真的，但单用户顺序流程下拿不到「边跑边看」的观感；也**没有可被 abort 定位的运行实体** |
| C7 | **hub run 不持久化**：进程内 `MemoryRunStore`，注释自认「hub runs are not persisted, a restart drops every artifact」 | `orchestrator.py::MemoryRunStore` / `::get_run_store`；`runtime/artifacts.py` 顶部 NOTE | 重启即丢运行/产物；会话历史不牢靠（需评估是否属本轮范围） |
| C8 | **无浅色主题** | 见 A8 | P0-1 |

> **C6 是本轮最大的隐藏风险**：P0-5（实时流式展示）与 P0-9（Stop/Abort）都**依赖**「运行成为一个可寻址、可取消的异步实体」。请架构师在 INC32-DESIGN 中正面给出方案（详见 §9-Q1）。

---

## 3. 新页面信息架构

### 3.1 首页（极简 Agent 入口）

**首屏（唯一主角）**：

```
┌─ 首页 ─────────────────────────────────────────────────────────────┐
│                                                                    │
│                    （淡化的产品名 / 身份提示，一行）                  │
│                                                                    │
│        ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓        │
│        ┃  告诉我你想完成什么任务…                    [ ➤ ] ┃        │
│        ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛        │
│         [ 建议 chip ] [ 建议 chip ] [ 建议 chip ] [ 建议 chip ]      │
│                                                                    │
│        近期任务（≤6 条，或诚实空态）                                 │
│        · 为 Acme 整理销售线索分析摘要            ● 已完成 →          │
│        · 分析本月成本异常                        ● 进行中 →          │
│                                                                    │
├──────────────────────── 第二屏（下移 / 可折叠）─────────────────────┤
│  4 张 KPI 卡 · 我的 Agent（≤6）· 技能中心（4）· 安全与资源概览        │
└────────────────────────────────────────────────────────────────────┘
```

| 层 | 放什么 | **不放什么** |
|---|---|---|
| 首屏 | Hero 标题（一句）+ 任务输入框 + 提交按钮 + 角色感知建议 chip（≤4）+ 近期任务（≤6，真实数据 / 诚实空态） | KPI 卡、Agent 卡、Skill 卡、安全概览、**智能执行日志** |
| 第二屏 | 4 KPI（`useHomeKpis`）、我的 Agent（`useAgentCatalog`）、技能中心（`useFeaturedSkills`）、安全与资源概览（`useSecurityOverview`） | 任何新的聚合看板 |
| 折叠 / 移除 | 安全与资源概览可折叠 | **智能执行日志整块移除**（A2） |

**保留不变**：`HomeView.tsx::Hero` 的 `roleConfigFor` 角色感知（含 `viewer` 只读禁用与提示）与 `HomeView.tsx::Hero` 的输入框 `aria-label`「任务输入」。

### 3.2 核心会话页 `/tasks`（原地演进，三列工作空间）

> **红线**：不新增 `/chat`，不替换 `/tasks`；`LiveRunsView.tsx::LiveRunsView` 文件与导出名不变（`router.tsx` 无需改动）。

```
┌─ 智能任务（AI 工作空间）───────────────────────────────────────────────────┐
│ ┌─ 左列：历史任务/会话 ─┐ ┌─ 中列：对话 + 执行 + 结果 ─┐ ┌─ 右列：产物 ─┐ │
│ │ 新建任务            │ │ ① 我的任务（intent）        │ │ 生成结果     │ │
│ │ ───────────────    │ │ ② Agent 执行中（简洁状态流） │ │ ┌─────────┐ │ │
│ │ 今天               │ │    ● 正在检索资料…          │ │ │ 运行报告 │ │ │
│ │ · 为 Acme…  ●已完成 │ │    ● 正在分析数据…          │ │ │ .md      │ │ │
│ │ · 分析成本… ●进行中 │ │    ● 正在生成报告…          │ │ └─────────┘ │ │
│ │ 昨天               │ │ ③ 最终结果                  │ │ [预览][下载] │ │
│ │ · 起草方案…  ●失败  │ │    一句话结论 + 核心发现     │ │ ─────────  │ │
│ │                    │ │ ④ 下一步（继续执行 / 动作）  │ │ 暂无生成结果 │ │
│ │ （暂无历史任务）    │ │ ▸ 查看执行详情（默认折叠）   │ │ （诚实空态） │ │
│ └────────────────────┘ └────────────────────────────┘ └────────────┘ │
└────────────────────────────────────────────────────────────────────────┘
```

| 列 | 放什么 | **不放什么** |
|---|---|---|
| **左列** | 新建任务入口（复用 `RunListPanel.tsx::RunListPanel` 的声明区）+ 历史任务/会话列表（按时间分组，选中态） | KPI、平台统计、工程字段 |
| **中列** | ① 用户任务（原文，逐字）② **执行中**：业务语步骤流（`concise` 密度）③ **最终结果** = 现有「结果 Tab」六段（状态 / 一句话结论 / 核心发现 / 当前阻塞 / 下一步 / 详细证据入口）④ 下一步动作（`ResultNextActions`）⑤ 「查看执行详情」入口（默认折叠） | 原始工具名、model / tokens / cost / checkpoint（收进「执行详情」）；证据列表与成本表（收进「执行详情」） |
| **右列** | 产物（Artifact）卡：标题 / 类型 / 大小或行数 + **预览** + **下载**（新增能力）+ 无产物时的诚实空态 | 运行级成本、执行账本原文 |

### 3.3 三档密度的边界，以及与**现有四 Tab + `ViewMode`** 的映射（**本轮最易做重复功**）

| INC32 三档 | 产品含义 | **如何实现（复用 / 改造，非替换）** |
|---|---|---|
| **简洁模式**（默认） | 普通用户：只看任务 / 进度 / 结果 / 下一步 | = 现有 `ViewMode = 'concise'`（`runs/types.ts::ViewMode`、`runs/useViewMode.ts::useViewMode`）**原样复用** |
| **执行详情** | 想看过程：步骤、耗时、状态、证据、成本 | = 现有 `RunTab = 'evidence' \| 'trace' \| 'cost'` **三个 Tab 原样复用**，只是**入口位置**从「结果页内嵌 Tab 条」改为**中列 ④ 的「查看执行详情」**；`result` Tab 的内容**升格**为中列 ③ 的主体 |
| **管理员模式** | 开发者 / 管理员：工具原文、模型、token、成本、原始 JSON | = 现有 `ViewMode = 'debug'`（`RunStageCard.tsx` 的 `raw` 分支 + `LiveRunsView.tsx` 的 `run-raw` `<details>`）**原样复用** |

**明确结论（给架构师的硬约束）**：

1. **四 Tab 复用，不替换**。`ResultPanel.tsx::ResultPanel` 的 `res-tabs` 结构、四个 `res-tabpanel`、以及全部 `data-testid`（`result-tab-*` / `result-layer` / `result-delivery` / `result-body` / `execution-layer` / `execution-ledger` / `result-cost` …）**保留**。
2. **`ViewMode` 复用，不新增第三个值**。「执行详情」**不是**新的 `ViewMode`，而是**同一页面内的展开层**（Tab / `<details>`）。三档 = `concise` + 「执行详情展开层」+ `debug`。
3. **不得**为「对话流」再造一套并行的结果渲染（避免与 `ResultPanel` 六段重复）。「对话」中的 Agent 回复 = 现有结果层六段的**同一份 DOM**，只改容器与位置，不改内容与推导。
4. 若架构师认为需要新的 Tab 组合或容器组件，须在 INC32-DESIGN 中给出「`data-testid` 只增不改不删」的逐条映射表。

---

## 4. 需求池（P0 / P1 / P2）

> 约定：**P0 必须 / P1 应当 / P2 可以**；每条含「做什么 + 验收标准（AC）+ 依赖的后端能力」；AC 一律**可机械判定**。既有代码一律 `file::symbol` 锚定。
> **阶段划分**：阶段 1 主题 → 阶段 2 首页 → 阶段 3 诚实空态 → 阶段 4 会话页骨架 → 阶段 5 实时执行 + 结果/产物工作区 → 阶段 6 Stop/Abort + Follow-up。**阶段 7（企业管理后台 UI 重做）本轮不做**。

### 4.1 P0 — 浅色默认主题（阶段 1）

#### P0-1 浅色为默认，保留深色可切换
- **做什么**：在**不破坏 dark-first 设计系统**的前提下，为 `tokens.css` 增加**浅色覆盖层**；默认主题 = 浅色；保留深色并可通过界面切换。切换状态持久化（沿用 `localStorage` 惯用法，参考 `runs/useViewMode.ts::useViewMode` 的 try/catch 读法）。**只允许**改 `tokens.css` + 各 `styles/*.css` + 一个主题挂载点（如 `<html data-theme>`）；**禁止**引入新框架 / 新原子类体系。
- **验收标准**：
  - **AC-1**：首次访问（无存储偏好）渲染为**浅色**；`document.documentElement` 带明确的主题标识（如 `data-theme="light"`）。
  - **AC-2**：切换到深色后，**深色值的视觉结果与改造前一致**（同一组 `--bg-page` / `--fg-primary` / badge / btn 取值）；`tokens.css` 中 `:root` 的暗色变量值本身**不得被改写语义**（改为「深色主题块」需等价）。
  - **AC-3**：切换后刷新页面，主题保持（持久化生效）；存储不可用（隐私模式）时**不抛错**并回落默认。
  - **AC-4**：浅色下所有既有 `data-testid` 节点仍存在且可见（**不因主题而条件渲染**）。
  - **AC-5**：`prefers-color-scheme` 不参与默认判定（默认浅色由产品决定，非跟随系统）——除非架构师另有决定并记录。
- **依赖后端能力**：无。

### 4.2 P0 — 首页极简化（阶段 2）

#### P0-2 首页首屏收敛为「输入任务 → 发起」
- **做什么**：按 §3.1 重排 `HomeView.tsx::HomeView`：Hero（文案重构）+ 建议 chip + 近期任务 → 首屏；KPI / Agent / Skill / 安全概览 → 第二屏或折叠；**删除** `HomeView.tsx::ExecutionLog`（智能执行日志）的首页渲染。
- **验收标准**：
  - **AC-6**：首屏（1024×768 视口内）**不再出现** KPI 卡、Agent 卡、Skill 卡、安全概览、执行日志（可对首屏 DOM 断言或不出现于前 N 个板块）。
  - **AC-7**：`HomeView.tsx::Hero` 的输入框 + 提交按钮仍可用；`viewer` 身份仍禁用并给出提示（沿用 `roleConfigFor(session?.role).canExecute`）。
  - **AC-8**：「智能执行日志」相关 DOM（标题 + `log-line` 列表）**不在首页**出现。
  - **AC-9**：首屏无任何 demo / 占位数字（空数据时 KPI 显示 `—` + 「暂无…」，延续既有 `buildKpi` 口径）。
- **依赖后端能力**：无。

### 4.3 P0 — 清除 demo 回退，改诚实空态（阶段 3）

#### P0-3 删除 `/tasks` 的 demo 回退
- **做什么**：移除 `LiveRunsView.tsx::LiveRunsView` 的 `showDemo` 渲染分支与全部 `DEMO_*` 常量在渲染路径的使用（`runs/demoData.ts`、`runs/panels.tsx`、`runs/types.ts::DemoRun`）；无真实 run ⇒ 诚实空态。
- **验收标准**：
  - **AC-10**：租户无 run 时，页面显示**诚实空态**（文案见 §7.2），**不出现**任何 `DEMO_RUN` 内容（可机械断言：空态下 DOM 文本不含 `wf_8K42n` / `Stripe` / `gpt-4o` 等 demo 特征串）。
  - **AC-11**：`grep` 全 `frontend/src` **不再有** `DEMO_RUN` / `DEMO_EVENTS` / `DEMO_TRACE` / `DEMO_STATE_DIFF` / `DEMO_AGENTS` / `DEMO_METRICS` / `DEMO_RAW_JSON` 的**渲染引用**。
  - **AC-12**：**既有 `data-testid` 一个不少**（真实路径）；仅 demo 专属节点消失。
  - **AC-13**：真实 run 结果层行为**逐字不回归**（`result-layer` / `result-body` / `result-tab-*` / `execution-layer` 等仍按既有语义渲染）。
- **依赖后端能力**：无。

### 4.4 P0 — 会话页「对话 + 执行过程」（阶段 4）

#### P0-4 `/tasks` 原地演进为三列工作空间
- **做什么**：重排 `LiveRunsView.tsx::LiveRunsView` 为 §3.2 三列布局：左列历史任务/会话（复用 `RunListPanel.tsx::RunListPanel`）、中列「我的任务 → 执行中 → 最终结果」（复用 `ResultPanel.tsx::ResultPanel` 的 `result` Tab 内容 + `RunStageCard`）、右列产物预览（新增，见 P0-7/P0-8）。
- **验收标准**：
  - **AC-14**：路由不变——`/tasks` 与 `/runs` 仍指向 `LiveRunsView`（`router.tsx::shellChildren` 未改）；**不存在** `/chat` 路由。
  - **AC-15**：同一屏内可同时定位到三列（左:历史、中:任务+执行+结果、右:产物）——用 `data-testid` 或稳定结构锚点断言。
  - **AC-16**：中列顺序为「我的任务 → 执行状态 → 最终结果」（DOM 顺序即语义顺序，可断言）。
  - **AC-17**：全部既有 `data-testid`（含 `ResultNextActions` 21 个、`ResultPanel` 18 个、`ResourcePicker` 14 个、`CodeTaskTimeline` 10 个、`CodeApproval` 9 个等）**只增不改不删**；布局变化不改 testid。
- **依赖后端能力**：无（纯前端重排）；若左列要做「会话分组」，需要 C1 落地（见 P0-10）。

### 4.5 P0 — 执行状态实时流式展示（阶段 5）

#### P0-5 中列实时展示简洁执行状态
- **做什么**：中列 ② 消费现有 SSE（`api/sse.ts::subscribeRunEvents`）与降级轮询（`hooks/useRunEvents.ts::useRunEvents`），把事件映射为**业务语**步骤流（沿用 `HomeView.tsx::toLogLine` 已有的业务化映射思路，默认 `concise` 密度）；**不展示**原始工具名 / 模型字段。
- **验收标准**：
  - **AC-18**：订阅失败时**真的**降级为轮询（`useRunEvents` 的 `poll` 路径），并在界面上如实说明（不静默、不假装实时）。
  - **AC-19**：执行中每条可见状态为业务语（**不含**工程术语，P0-5 文案纪律）；工程值只在 `debug` 档出现。
  - **AC-20**：运行到终态后，流**终止**且界面切到「最终结果」（不残留「进行中」）。
- **依赖后端能力**：**依赖 C6 的解决**（见 §9-Q1）。若维持同步派发，「实时」退化为「回放」。**这是本需求的真实前提，不接受用前端动画伪造「实时」。**

### 4.6 P0 — 技术细节默认折叠（阶段 5）

#### P0-6 默认折叠工程细节
- **做什么**：默认 `concise`；「查看执行详情」作为中列 ④ 的展开入口，展开后复用四 Tab（`evidence` / `trace` / `cost`）；`debug` 档作为管理员模式（复用 `ViewMode`）。
- **验收标准**：
  - **AC-21**：首屏（未展开未切档）**不出现**原始工具名、`model` / `token` / `cost` / `checkpoint` 字段。
  - **AC-22**：「查看执行详情」默认**折叠**（无 `open`）；展开后 `evidence` / `trace` / `cost` 可访问且内容与现状一致。
  - **AC-23**：`ViewMode` 持久化行为不回归（`localStorage` key 语义保持）。
- **依赖后端能力**：无。

### 4.7 P0 — Result / Artifact 工作区（阶段 5）

#### P0-7 结果工作区（中列）
- **做什么**：把现有 `result` Tab 六段（`result-layer-title` / `result-headline` / `result-metrics` / `result-findings` / `result-conclusions` / `result-blockers` / `result-next-actions` / `result-details`）**原样**升格为中列主体；右侧提供产物入口。
- **验收标准**：
  - **AC-24**：六段内容与推导**逐字不变**（`ResultPanel.tsx` 的 `derive*` 系列逻辑不改语义）。
  - **AC-25**：`result-conclusions` 的不变式仍成立：`[result-headline] + [...result-conclusions li]` === `conclusions` 完整序列（无重复、无丢失）。
  - **AC-26**：产物入口在**有产物**时可见、**无产物**时给诚实空态（`result-empty` 语义保留）。
- **依赖后端能力**：无。

#### P0-8 Artifact 卡与预览（右列）
- **做什么**：右列渲染产物卡列表（标题 / 类型 / 来源 / 创建时间，逐字后端字段），支持**预览**；预览内容逐字来自 `artifacts[i].content`。
- **验收标准**：
  - **AC-27**：产物卡条数 == `artifacts.length`（严格 1:1，不编造、不补占位）。
  - **AC-28**：预览文本逐字 == 对应 `artifacts[i].content`（可机械对比）。
  - **AC-29**：无产物 ⇒ 右列显示「暂无生成结果」（诚实空态），且**不渲染**空卡片。
  - **AC-30**：预览**不得**改变正文字面（延续 `ResultDetails.tsx` 的口径：预览用 `<pre>` 原文，不经 Markdown 渲染而丢失 `##` 等字面）。
- **依赖后端能力**：① 产物**下载**需新增端点（C4）；② 产物类型扩展需后端（C5，见 P1）。若本轮只做「预览既有文本产物」，仅依赖现有 `artifacts`；**下载**按 §4.9 P0-8b 单独验收。

#### P0-8b 产物下载（延伸，需后端）
- **做什么**：新增产物下载能力（依赖 Q5 决策 ③ 的「Artifact 下载/预览」后端增量）。
- **验收标准**：
  - **AC-31**：对存在产物的 run，前端能取得**真实文件内容**（HTTP 200 + 非空 body，与 `artifacts[i].content` 一致）。
  - **AC-32**：无产物时下载入口**不存在**（不是死按钮）；越权/不存在 run 返回**403/404**且不产生副作用。
  - **AC-33**：租户隔离不回归（跨租户读 → 404；沿用既有应用层行级过滤口径）。
- **依赖后端能力**：**是**（C4：新增 artifact 下载/预览端点；不在现有 95 个端点内）。

### 4.8 P0 — Stop / Abort（阶段 6）

#### P0-9 停止任务
- **做什么**：中列「执行中」状态提供**停止**动作；后端新增 abort 端点，emit `run.aborted`（终态词表已含，`runtime/events.py::TERMINAL_EVENTS`），并让运行实体**真的**停止。
- **验收标准**：
  - **AC-34**：请求停止后，前端在合理时限内切到「已停止」终态，SSE 流终止（收到 `run.aborted` 或 `[DONE]`）。
  - **AC-35**：停止是**终态**且**不可逆**（不为停止提供「恢复」，见 P2）。
  - **AC-36**：停止后**不得**再产生新的产物/步骤（后端可断言无新增 `tool_invocations`）。
  - **AC-37**：无权限角色调用停止 ⇒ **403**，且运行**不受影响**（不产生副作用）；越权尝试写既有审计通道。
  - **AC-38**：非**运行中**的 run 调用停止 ⇒ 幂等 / 明确拒绝（不报 500、不静默成功）。
- **依赖后端能力**：**是**（C3 + C6：abort 端点 + 可寻址的运行实体）。

### 4.9 P0 — 真实 Follow-up / Continue Task（阶段 6）

#### P0-10 真实上下文续聊（**不接受前端拼接**）
- **做什么**：为 Follow-up 增加**真正的** `session / conversation / parent_task` 关系（Q5 决策 ①），使后续 run 在**后端**可回溯父 run，并把上一轮上下文**真正注入**新 run 的规划/执行（修正 C2）。
- **验收标准**：
  - **AC-39**：续聊产生的新 run 在**后端**可查出其父 run / 会话归属（可机械读取；非前端 state 拼接）。
  - **AC-40**：新 run 的规划/执行**真的**消费上一轮上下文——**反向对照**：移除该上下文注入后，相关断言**必须变红**（证明承重，而非装饰）。
  - **AC-41**：续聊入口复用现有 `ResultNextActions.tsx` 的 `result-continue`（不新增假按钮）；点击后**确有**一次真实请求。
  - **AC-42**：会话历史按真实父子关系分组展示（左列）；无历史 ⇒ 「暂无历史任务」。
  - **AC-43**：**不得**在 UI 上把「新 run」表述为「同一会话的延续」而其后端无父子关系——文案与后端事实必须一致（诚实纪律）。
- **依赖后端能力**：**是**（C1：新增可持续的 session / conversation / parent_task 关系；Q5 范围 ①）。**排除**：为 UI 重构做大规模后端重构。

### 4.10 P1 — 应当具备

| 编号 | 需求 | 验收标准（AC） | 依赖 |
|---|---|---|---|
| **P1-1** | 会话/任务历史按时间分组（今天 / 昨天 / 更早） | 分组边界由真实 `created_at` 派生；无数据显示「暂无历史任务」 | C1（可选） |
| **P1-2** | 产物类型扩展（Q5 决策 ③：非纯文本产物，如表格/图片/文件） | 新类型的 `kind` 可在右列渲染并预览；无数据不占位；**不新增第二套状态词表** | C5（后端） |
| **P1-3** | 深色主题切换入口落在 `Topbar.tsx` / 设置区，且状态全局一致 | 任意页面切换后主题一致；刷新保持 | 无 |
| **P1-4** | 只读（`viewer`）身份的会话页降级：可看历史/结果/产物，**不可**发起、**不可**停止、**不可**续聊 | 只读身份下对应按钮**不存在**（非禁用死按钮）；不产生任何 403 请求 | 无 |

### 4.11 P2 — 可以具备（本轮**设计预留，不实现**）

| 编号 | 需求 | 说明 |
|---|---|---|
| **P2-1** | Pause / Resume（暂定 / 恢复） | **仅产品设计预留**：本轮不实现、不新增端点、不出现按钮。设计上须为其预留「与 Stop 互斥」「Resume 需真实上下文」的语义位置（参考 `web-ui-main` 的交互理念，**不复用其代码**） |
| **P2-2** | 阶段 7：企业管理后台 / RBAC / 审计 / 成本 的 UI 重构 | 本轮**不参与**（Q7）；沿用现有 23 个视图 |

---

## 5. 用户故事（含验收场景）

- **US-1（新用户 · 首次打开）** 作为**第一次访问**的用户，我希望打开首页就只看到一个输入框和最近的几条任务，以便我立刻明白「这里可以让我把任务交给 Agent」，而不是先学习一堆图表。
  - 场景：首次访问 ⇒ 浅色主题；首屏无 KPI/Agent/Skill/安全卡/执行日志；输入框可聚焦。
- **US-2（用户 · 发起任务）** 作为用户，我希望输入一句话就能发起任务，并且能顺带声明输入（数据表 / 路径 / 资源）。
  - 场景：输入任务 → 提交 ⇒ 左列出现新任务并选中；中列进入「我的任务 → 执行中」。
- **US-3（用户 · 看实时进度）** 作为用户，我希望看到**业务语**的实时进度（「正在检索资料…」），而不是工具名与 token 数。
  - 场景：执行中 ⇒ 中列出现业务语步骤流；无工程术语；SSE 失败时如实显示已降级为轮询。
- **US-4（用户 · 拿结果）** 作为用户，我希望执行结束后立刻看到一句话结论、核心发现与下一步，并且能展开看证据。
  - 场景：终态 ⇒ 中列六段结果；「查看执行详情」默认折叠，展开后可见证据/轨迹/成本。
- **US-5（用户 · 继续追问）** 作为用户，我希望在结果下方继续输入下一步指令，并且**Agent 真的记得上一轮**。
  - 场景：输入续聊指令 ⇒ 新建 run，后端可查其父 run，且**反向对照**证明上一轮上下文是承重的（AC-40）。
- **US-6（用户 · 停止任务）** 作为用户，我希望长任务能被我停掉，且停掉就是停掉。
  - 场景：点「停止」⇒ 终态「已停止」；不再产生新产物；无「恢复」按钮。
- **US-7（新租户 · 无数据）** 作为**没有任何运行**的用户，我希望看到诚实的空态，而不是一套演示数据。
  - 场景：无 run ⇒ 左列「暂无历史任务」、中列空态、右列「暂无生成结果」；页面**不含** demo 特征串。
- **US-8（用户 · 主题）** 作为用户，我希望默认就是浅色，也能切到深色。
  - 场景：默认浅色；切深色 ⇒ 视觉与既有暗色一致；刷新保持。
- **US-9（只读身份 · 降级）** 作为 `viewer`，我希望我能看但不能改。
  - 场景：`viewer` 登录 ⇒ 首屏输入框禁用并提示「只读身份无法发起任务」（沿用 `HomeView.tsx::Hero`）；会话页无「停止 / 继续执行」按钮；不发出会被 403 的请求。
- **US-10（开发者 · 管理员模式）** 作为开发者，我希望需要时能看到原始工具名、模型、token、成本与原始 JSON。
  - 场景：切 `debug` ⇒ 出现工程字段与「原始运行数据」`<details>`；切回 `concise` ⇒ 工程字段消失。

---

## 6. UI 设计方向（浅色默认）

> **硬约束**：必须**复用 / 扩展** `frontend/src/styles/tokens.css` 的变量体系；**不得引入新框架**（无 MUI / Tailwind / styled-components / CSS-in-JS）。

### 6.1 浅色取值思路（覆盖层，不改暗色语义）

做法：在 `tokens.css` 内新增**浅色覆盖块**（建议 `:root[data-theme='light'] { … }`），仅覆盖**语义变量**，其余（Signal / Type / Space / Radii / Motion）**沿用同一组**，只调整需要对比度的层级色。示意（**取值待架构师与视觉确认，此处只给方向**）：

| 语义变量 | 深色现状（`tokens.css`，不改） | 浅色方向（新增覆盖） | 理由 |
|---|---|---|---|
| `--bg-page` | `oklch(0.145 0.012 250)` | 白底、极浅冷灰（如 L≈0.99） | 白底为主，避免纯白刺眼 |
| `--bg-canvas` | `oklch(0.165 0.012 250)` | 极浅灰（L≈0.98） | 卡片与页面的**微差**层级 |
| `--bg-elevated` | `oklch(0.205 0.013 250)` | 浅灰（L≈0.96） | 浮层 / 按钮底 |
| `--bg-inset` | `oklch(0.125 0.010 250)` | 更浅灰（L≈0.97） | 内嵌区（如代码块底） |
| `--border-subtle/-default/-strong` | 高 L 的暗色边框 | 低 L 的浅灰边框（逐步加深） | 层级靠**边框 + 留白**，不靠阴影堆叠 |
| `--fg-primary/-secondary/-muted/-subtle/-faint` | 亮 → 暗 | 反相：近黑 → 中灰 → 浅灰 | 正文对比度达标（≥ 4.5:1） |
| Signal（blue/purple/emerald/amber/red） | 现有 1..5 阶 | **保持色相与阶数**，仅选取更适配浅底的那一阶用于文字/图标 | 状态语义不换词、不换色相 |
| `--shadow-*` | 内发光 + 深投影 | 改为**极淡**投影 | 浅底不宜堆重阴影 |

**要求**：① 浅色下 `:focus-visible` 焦点环仍清晰（沿用 `tokens.css` 的 focus 规则）；② 浅色下**不得**出现暗色残留（如硬编码 `var(--fg-primary)` 在白底上不可读——需通过覆盖，不得改动调用点）；③ 不改任何 `.btn` / `.badge` / `.card` 的**类名与结构**，只让其在浅色下取到正确值。

### 6.2 层级 / 留白 / 按钮收敛

| 项 | 要求 |
|---|---|
| **层级** | 每屏**最多 3 级**视觉层级：主内容 → 次级信息 → 折叠细节。禁止「同屏 5 种卡片样式」 |
| **留白** | 沿用 `--s-*` 间距刻度；板块间距 ≥ `--s-8`；卡片内边距沿用现有 `p-*` 尺度，不新造 |
| **按钮收敛** | 同一屏**主按钮 ≤1 个**（如首页「提交」、会话页「继续执行」）；其余降级为 `.btn.ghost` / 文本链；工程操作（导出/复制/打印/存知识库）**统一收进**「更多操作」折叠（沿用 `ResultNextActions.tsx` 既有做法） |
| **卡片数量** | 首页首屏卡片数 = 0（只有输入框 + 列表）；会话页右列产物卡 ≤ 一屏可读（超出滚动） |
| **禁止** | 新造装饰性渐变 / 光斑（`tokens.css` 的 `.spotlight` / `.grid-bg` 仅按需在首页保留一处）；新造图标体系（沿用 `components/icons.tsx`，**无 emoji**） |

---

## 7. 数据诚实要求（单列一节）

### 7.1 延续的纪律（不许放松）

1. **未测量 ≠ 0**：`latency_ms` / `durationMs` / `runDurationMs` 等为 `null` 表示**未测量**，UI 渲染「—」，**绝不写 0**（依据：`runs/types.ts::RunToolCall.ms`、`RunStageCard.tsx::formatMs` / `::formatDuration`、`ResultDetails.tsx` 页脚「耗时」规则）。
2. **判错只用结构化字段**：`ok` 仅当 `outcome === "succeeded"`；不靠文案 / 长度 / 猜测。
3. **产物正文优先（P0-2）**：产物是后端产品内容，**逐字渲染**；即使含工程词也不改写（依据：`ResultPanel.tsx` 顶部「P0-2 vs P0-5 边界」）。
4. **平台自撰文案（P0-5）**：我们自己写的中文字面量**不得含工程术语**（工具 id / 模型名 / 枚举值）。
5. **空态优于编造**：数据源为空 ⇒ 诚实空态，**禁止**占位/默认/demo 数据。

### 7.2 空态文案清单（本轮统一口径）

| 位置 | 文案 | 备注 |
|---|---|---|
| 首页 · 近期任务 | 「还没有任务，去上方发起第一个任务吧」 | 沿用 `HomeView.tsx::RecentTasks` |
| 首页 · KPI | `—` + 「暂无任务」/「暂无终态任务」/「暂无基线，无法估算节省」 | 沿用 `HomeView.tsx::buildKpi` |
| 首页 · Agent / 技能 | 「暂无 Agent」/「暂无技能」 | 沿用 |
| 会话页 · 左列历史 | 「暂无历史任务」 | **新增**（替换 A 类杂乱空态） |
| 会话页 · 左列运行列表 | 「还没有任何运行——用上面的输入框运行第一个任务。」 | 沿用 `RunListPanel.tsx::RunListPanel` |
| 会话页 · 中列（无 run） | 「暂无运行记录」 | **新增**（替代 demo） |
| 会话页 · 中列 · 无交付正文 | 「本次交付正文为空（平台执行账本见「执行轨迹」）。」 | 沿用 `ResultDetails.tsx` |
| 会话页 · 中列 · 无产物 | 「本次运行未产出可展示的结果」 | 沿用 `result-empty` |
| 会话页 · 右列产物 | 「暂无生成结果」 | **新增** |
| 会话页 · 证据 / 来源 / 成本 / 经验 | 沿用既有诚实空态句 | `ResultPanel.tsx` |

### 7.3 禁止 demo / 伪造数据的适用面

适用**全站**，尤其：
- `/tasks` 全部展示路径（**含**新三列布局的左/中/右三列）；
- 首页所有板块；
- **新增**的 Artifact 卡与预览、实时执行状态流、会话历史分组。

**例外登记（唯一）**：`tokens.css` / `styles/*.css` 中的**结构性骨架屏**（`skel-*` / `ResultSkeleton`）是**结构占位**（无数据值），允许保留；但**不得**在其中填入任何数值或文案。

---

## 8. 范围边界（明确不做）

1. **不做阶段 7**：企业管理后台 / RBAC / 审计 / 成本 的 UI 重构**不参与**本轮（Q7）；相关 23 个视图保持现状。
2. **不实现 Pause / Resume**：本轮**仅产品设计预留**，不新增端点、不出现按钮（Q9）。
3. **不复用 `web-ui-main`（Browser Use WebUI）的任何代码 / 组件 / 样式**：它只作**交互理念与产品形态参考**（Q9）。已核实其为 Python + Gradio，与本站技术栈无关。
4. **不为 UI 重构做大规模后端重构**：后端增量**严格限定三项**（Q5）——① session/conversation/parent_task 关系；② stop/abort；③ artifact 类型 + 下载/预览。**不得**顺带重构 orchestrator / 数据契约 / 既有 API 语义。
5. **不新增 `/chat` 路由，不替换 `/tasks`**（Q4）；不破坏现有路由与 3 个 Playwright spec。
6. **不推倒现有四 Tab 与 `ViewMode`**：复用为主（§3.3）；旧 99 个 `data-testid` **只增不改不删**。
7. **不引入** MUI / Tailwind / styled-components / 新图标体系 / vitest（项目纪律）。
8. **不改企业级 Agent 能力**：编排、技能、记忆、RBAC、租户隔离、代码执行面（`codeplane`）等既有能力**保持不变**。
9. **不改四层数据契约**（L1 plan / L2 tool_invocations / L3 observations / L4 artifacts）与既有 API 语义，只在既有口径上**增量扩展**。
10. **不自动生成交付报告**（Q10 默认口径）。

---

## 9. 待确认问题（含默认假设）

| 编号 | 问题 | 我的默认假设（未澄清前按此执行） |
|---|---|---|
| **Q1**（**最高优先**） | **「实时」的前提**：现状 `POST /tasks` 在请求内 `await run_task(...)` 跑到终态才返回（`routers/tasks.py::create_task`），前端拿不到运行中的 `run_id`，SSE 实际是**回放**（`events.py::RunEventBus.stream`）。**是否本轮引入「异步派发 + 立即返回 run 句柄」**？这是 P0-5（实时流式）与 P0-9（Stop/Abort）的**共同前提**，也决定 Q5 后端增量的实际范围。 | **必须引入**（否则 P0-5 与 P0-9 均无法真验收）。建议并入 Q5 决策②「stop/abort」一并设计（abort 需要**可寻址的运行实体**）。若架构师判定本轮不可做，则 P0-5 降级为「结果回放 + 诚实标注非实时」，P0-9 降级为 P2——**请架构师明确取舍，不接受用前端动画伪造实时**。 |
| **Q2** | **hub run 不持久化**（`orchestrator.py::MemoryRunStore`，注释自认重启即丢产物）。会话历史 / 父子关系（Q5 决策①）若要**可持续**，是否必须落库？ | **必须落库**（否则「会话」重启即断，Follow-up 与左列历史都不可靠）。范围仍限定为「新增关系表/字段」，**不**重构既有 run 存储。请架构师确认 Alembic 迁移编号（当前 head = `015`）。 |
| **Q3** | **左列「会话」的粒度**：一个 run = 一个会话？还是一个 session 下挂多轮 run？ | **一个 session（会话）下挂多轮 run**（用户心智是「聊一个话题」）；默认以「首轮 intent」作会话标题，无标题则用时间。 |
| **Q4** | **三档的切换入口**：`concise`/`debug` 已由 `ViewMode` 承担；「执行详情」是**展开层**。是否需要把这三者统一成一个显式控件（如右上角分段控件）？ | **保持分离**：`ViewMode` 分段控件沿用现有（`LiveRunsView.tsx::ViewModeToggle`）；「查看执行详情」是中列 ④ 的**折叠入口**。理由：避免新增第三个 `ViewMode` 值与破坏既有语义。 |
| **Q5** | **产物下载的形态**：内容寻址 `FileBlobStore` 在项目树之外；是否需要真正的 `FileResponse` / 静态服务？ | **需要**（否则 AC-31 无法成立）。仅在 `routers/runs.py`（或新 `artifacts` 子路由）新增**只读下载端点**，复用现有租户行级过滤；**不**引入静态目录挂载。 |
| **Q6** | **孤儿样式**（`src/index.css` / `src/App.css`，无 import）是否本轮回滚删除？ | **建议删除**（含 Vite 模板残留的 `#root { width: 1126px }` 会误导后续维护）；若担心风险，先在 INC32-DESIGN 中登记为「保留但禁用」。请架构师拍板。 |
| **Q7** | **README/文档**是否需要在 `docs/sop/` 之外同步一份面向用户的「AI 工作空间」说明？ | **不需要**（Q10：默认不自动生成交付报告）；INC32 三件套（PRD/DESIGN/QA-REPORT）即可。 |

---

## 附：本增量统计

- **现状分析**：路由/视图表 23 行；过度复杂条目 **A1–A9（9 条）**；已实现能力 **B1–B14（14 条）**；真正缺失 **C1–C8（8 条）**。
- **需求条目**：P0 **11 条**（P0-1 主题 / P0-2 首页 / P0-3 诚实空态 / P0-4 会话页 / P0-5 实时 / P0-6 折叠 / P0-7 结果区 / P0-8 产物卡预览 / P0-8b 产物下载 / P0-9 Stop / P0-10 真实 Follow-up）+ P1 **4 条** + P2 **2 条**，共 **17 条**。
- **验收标准**：**AC-1 ~ AC-43**，共 **43 条**。
- **用户故事**：US-1 ~ US-10，共 **10 条**。
- **待确认问题**：Q1 ~ Q7，共 **7 条**（每条含默认假设）。
- **最大风险**：**Q1**（同步派发 ⇒ 「实时」与「Stop」双双落空）与 **Q2**（hub run 不持久化 ⇒ 「会话」不可持续）。
