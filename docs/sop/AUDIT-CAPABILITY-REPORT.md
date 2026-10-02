# ForgeFlow 能力完整性与「删除面」审计报告

> 角色：软件架构师（静态 / 结构层审计）。**只读审计** —— 本报告未修改任何源码 / 测试，未执行任何 git 写操作。
> 被测对象：`D:/Agentxm/Multi-Agent/ForgeFlow-main`；git 仓库根为其上一级 `D:/Agentxm/Multi-Agent`；审计时 HEAD = `71bb307`。
> 需求基线：`D:/Agentxm/Multi-Agent/目标.md`（自称 Single Source of Truth）。
> 证据纪律：每条结论附 `file::symbol`（符号锚点）。**本报告不引用 `file.py:行号` 形式**（仓库有反漂移钉子测试）。
> 不确定项一律标注「未核实」，不臆测。
> 实跑测试由 QA（严过关）另行负责，本报告只做静态/结构判定。

---

## 0. 一句话结论

| 维度 | 判定 | 最强证据 |
|---|---|---|
| **功能是否齐全** | 基本齐全：`目标.md` §2.1 的 20 条诉求 + §2.2 的 11 条评审意见，**落地文件在 INC39 后仍全部存在**，核心模块均有生产调用方 | 见 §A |
| **是否有删除功能** | ⛔ **极度不足**：后端**真实现 DELETE 端点仅 2 个**（控制面 `memory` 1 个 + 代码执行面内部清理 1 个），**用户可达的删除 = 0**（控制面那个前端无调用方、执行面那个独立进程不可达）；资源域 / 成本域在 repo 层定义了 `delete` 但**零调用方** | 见 §B.1 |
| **是否发生过「能力被删」** | INC39（`d98d0f6`）/ INC39b（`71bb307`）**只删了前端展示层**，**零后端文件被删**；但造成 2 处**能力可达性下降**（后端在、用户点不到） | 见 §B.2 |
| **死代码 / 未接线** | 存在一批：Python 侧 12 项、前端 7 项（真死）+ 若干「仅测试接线」 | 见 §C |
| **分析是否足够** | 核心分析链路可用但**深度有限**：多为「档位开关 + 常量指令」，覆盖面窄、静默回落 | 见 §D |
| **文档漂移** | ⛔ `目标.md` 停在 **INC12 / 1078 tests / HEAD `ed3ebef`**，而代码已到 **INC39 / HEAD `71bb307`** —— SSOT 已失效 | 见 §E |

---

## A. 能力完整性（「功能是否齐全」）

### A.0 方法与口径

对 §2.1 的 20 条诉求与 §2.2 的 11 条评审意见逐条：
1. **落地位置是否仍存在**：按 `目标.md` 给出的路径，逐个确认文件在 INC39 后仍在；
2. **是否有调用方**：对关键后端模块，统计 `forgeflow/**` 内 `import` 该模块的**生产文件**数（排除 `__init__.py` 与自身）；
3. **前端是否有可达入口**：对照 `frontend/src/router.tsx` 已注册路由。

> ⚠️ **对主理人侦查的一处更正**：`frontend/src/router.tsx` 的字面 `path:` 根级路由确有 **7 条**（`/console`、`/welcome`、`/architecture`、`/design-hub`、`/design-system`、`/docs`、`/docs/$slug`），但**其余 20+ 个控制台页面是通过 `shellChild` / `guardedShellChild` 动态注册的**（`frontend/src/router.tsx::shellChildren`）。因此「60+ 视图大多没入口」的假设**不成立** —— 详见 §C.2，`views/` 下**零个孤儿视图**。

### A.1 用户 20 条诉求核对表

状态图例：`[x]` 落地存在 + 有生产调用方；`[~]` 落地存在但调用方/入口未完全核实；`⛔ 不足：…` 明确缺陷。

| # | 诉求 | 落地位置（现状） | 有生产调用方? | 前端入口 | 状态 | 证据 |
|---|---|---|---|---|---|---|
| 1 | 数据/上下文优化 | `forgeflow/experience/context_builder.py::build_context`、`token_budget.py` | ✅ `runtime/orchestrator.py::_build_run_context` | 未核实（无专属页；`/context` 仅读 API） | [x] | `orchestrator.py::_build_run_context` 调用；`context_builder.py::_mark_memory_reused` 回写 |
| 2 | 技能管理 SkillOps | `skills/registry.py`、`versioning.py`、`governance_gate.py` | ✅ `skills.py`、`orchestrator.py`、`context_builder.py` | ✅ `/skills` → `SkillsView` | [x] | `registry` 被 `orchestrator`/`context_builder`/`main` 引用 |
| 3 | 团队经验沉淀 | `experience/scopes.py::team` scope | ✅ `memory.py`、`memory_store.py`、`promotion.py` | ✅ `/knowledge`、`/memory` | [x] | —
| 4 | 企业知识沉淀 | `experience/promotion.py` + `POST /memory/{id}/promote` | ✅ `memory.py` 引入 `promote_memory` | ✅ `/knowledge` | [x] | `forgeflow/api/routers/memory.py::promote_memory_entry` |
| 5 | 技能共享 Marketplace | `skills/marketplace_bridge.py` | ✅ `api/routers/marketplace.py` | ✅ `/marketplace` → `MarketplaceView` | [x] | `marketplace.py` 引入 `marketplace_bridge` |
| 6 | 技能迭代 Versioning | `skills/versioning.py`、release/rollback | ✅ `skills.py`、`governance_gate.py` | ✅ `/skills` | [x] | `skills.py::create_skill_version` / `rollback_skill` |
| 7 | 长期运行 Durable Runtime | LangGraph checkpointer | ✅ `graph/builder.py` | ✅ `/tasks`、`/runs` | [x] | `main.py` 编译 `graphs` |
| 8 | 不忘记上下文 分层记忆 | `experience/scopes.py` 五层 | ✅ `memory_store.py`、`memory.py` | ✅ `/memory` | [x] | `memory.py::memory_scopes` |
| 9 | 数据不泄露 Tenant+DLP | `governance/dlp.py`、`dlp_rules.py`、`security/pii_redactor.py` | ✅ `dlp`→`marketplace_bridge`/`memory`/`trust_baseline`；`pii_redactor`→`middleware/security.py`、`dlp_rules.py` | ✅ `/security` | [x]（见 §C.4 关于 `tenancy` 的诚实备注） | `governance/tenancy.py` 仅被 `__init__` 再导出（详见 §C.1） |
| 10 | Agent 权限 Policy Engine | `governance/policy_engine.py` | ✅ 8 个生产文件（`orchestrator`/`tasks`/`workspace`/`react_executor`/`policies`/`governance_gate`/`platform_tools`） | ✅ `/settings`、`/rbac` | [x] | —
| 11 | 企业省钱 Cost | `cost/budget_service.py`、`degrade.py`（**注意：路径为 `forgeflow/cost/`，非 SSOT 写的 `forgeflow/cost/` 一致**）、`savings.py` | ✅ `budget_service`→`cost.py`/`orchestrator`；`degrade`→`budget_service`/`slo`/`orchestrator`/`tasks`/`builder` | ✅ `/cost`、`/analytics` | [x] | —
| 12 | Agent 变强 Eval+Evolution | `skills/evolution.py` + `POST /skills/evolution-advice/{id}/approval` | ✅ `skills.py` | ✅ `/evals` → `EvaluationsView` | [x] | `skills.py::skill_evolution_advice` / `submit_evolution_advice_for_approval` |
| 13 | Agent 出错 Loop Breaker | `validation/replan.py`、`loop_breaker.py` | ✅ `orchestrator.py` | ✅ 结果面板 | [x] | `orchestrator.py` 引入 `LoopBreaker`；`_persist_loop_breaker_breadcrumb` |
| 14 | 高风险 HITL | `runtime/gate.py` | ✅ 6 个生产文件（`orchestrator`/`llm_planner`/`react_executor`/`tasks`/`workspace`/`trust_baseline`） | ✅ `/approvals` | [x] | —
| 15 | 工具调用 MCP | `mcp/**` + `mcp/client/adapter.py` | ✅ `main.py::get_mcp_tools` | ✅ `/tools` → `ToolsView` | [x] | —
| 16 | 多 Agent LangGraph | `runtime/orchestrator.py`、`agents_catalog.py` | ✅ `agents.py` 引入 `PLATFORM_AGENTS` | ✅ `/agents` | [x] | —
| 17 | 实时过程 SSE | `GET /runs/{id}/events` | ✅ `runs.py::run_events` | ✅ `frontend/src/hooks/useRunEvents.ts`、`api/sse.ts` | [x] | —
| 18 | 企业系统 FastAPI+PG | `forgeflow/api/main.py`、`alembic/`（16 迁移，最新 `016_workspace_runs.py`） | — (基础设施) | — | [~] | 迁移单头与幂等由 QA 实跑裁定 |
| 19 | **核心闭环 任务→经验→Skill** | `experience/extractor.py::ExperienceExtractor`、`skills/candidate_compiler.py::compile_candidate` | ✅ `orchestrator.py` 调 `ExperienceExtractor`；`skills.py` 调 `compile_candidate`/`promote_candidate` | ✅ `/tasks`+`/knowledge`+`/skills` | [x] | `orchestrator.py` 引入 `ExperienceExtractor` |
| 20 | 按设计图重做首页 | `frontend/src/views/HomeView.tsx`（`/`）、`LandingPage.tsx`（`/welcome`） | n/a | ✅ | [x] | `router.tsx` 中 `/` → HomeView、`/welcome` → LandingPage |

**§A.1 汇总：20 条中 18 条 `[x]`、2 条 `[~]`（#1 前端入口未核实、#18 属基础设施由 QA 裁定）、0 条 ⛔。**
⚠️ **诚实边界**：本表「有生产调用方」列经**独立 import 扫描**确认；「前端入口」列除已注明者外，**未逐页核实交互可达深度**（如 #1 上下文优化的可视化深度未核实）。

### A.2 用户 11 条评审意见核对表

| # | 意见 | 落地 | 状态 | 证据 |
|---|---|---|---|---|
| 1 | Experience 独立管理、Memory N:M | `experience/models.py`、`memory_store.py`、`memory_types.py` | [x] | `memory_types` 被 `memory.py`/`memory_store.py`/`promotion.py`/`scopes.py` 引用 |
| 2 | Skill 版本化/演进/评估/治理 | `versioning`、`evaluator.py`、`governance_gate`、`evolution`、`release_gate` | [x] | `release_gate` 被 `canary.py`/`governance_gate.py` 引用 |
| 3 | 可信一等公民、风险分层 | `governance/policy_engine.py::classify_risk` | [x] | `policy_engine` 8 个生产调用方 |
| 4 | SLO 可观测 | `observability/slo.py`、`GET /metrics/slo` | [x] | `slo` 被 `api/routers/metrics.py` 引用 |
| 5 | 安全治理底座 | Tenant + DLP + Policy | [x]（结构在；`tenancy` 类见 §C.1） | `pii_redactor`→`middleware/security.py` |
| 6 | Cost 预算意识 | `cost/budget_service.py`、`degrade.py` | [x] | `degrade` 多方引用 |
| 7 | Org/Team Memory 分层 | `promotion.py` + `POST /memory/{id}/promote` | [x] | `memory.py` 引入 `promote_memory` |
| 8 | 可信能力 vs 进化扩展逻辑 | `skills/trust_baseline.py` + `governance_gate.py` | [x] | `trust_baseline` 被 `governance_gate.py`/`marketplace_bridge.py` 引用 |
| 9 | 数据/上下文优化为平台级 | `context_builder` + `token_budget` + 落库 | [x] | `orchestrator.py::_build_run_context` → `persist_context_build` |
| 10 | 任务→经验→Skill 闭环 | `orchestrator` + `extractor` + `candidate_compiler` | [x] | 见 §A.1 #19 |
| 11 | 数据模型 + 缺失一环 | `skills/candidate_compiler.py::compile_candidate` | [x] | `skills.py` 调用 `compile_candidate` |

**§A.2 汇总：11 条全部 `[x]`（结构层）。**「机制化程度」的**运行时真伪**由 QA 实跑裁定，本报告不越界。

---

## B. 「删除面」审计（用户明确问的「是否有删除功能」）

### B.1 产品是否有删除能力 —— 逐域 CRUD 完整性

**全仓路由静态清点（⚠️ 存在两套 API 面，禁止只扫装饰器）**：

- **控制面（FastAPI）**：22 个 `include_router` 装配点（全部位于 `forgeflow/api/main.py`），101 个 `@router.*` 装饰器路由；其中 **DELETE 装饰器仅 1 个** = `forgeflow/api/routers/memory.py::delete_memory`。
- **代码执行面（独立 Starlette 应用，装饰器扫描看不见）**：`forgeflow/codeplane/runner/agent_server/app.py::create_app` 用手写 `Route(...)` 表声明路由（**不是** `@router.delete`）。其中**有一个真实现的 DELETE**：`Route("/api/conversations/{conversation_id}", conversation_item, methods=["GET","DELETE","POST","PUT","PATCH"])`；处理函数 `forgeflow/codeplane/runner/agent_server/app.py::conversation_item` **真做删除（C7，非 501）** —— 未知 id → 404；否则 `runtime.shutdown()` + `request.app.state.runtimes.pop(...)` → 200 `Success`。
  - **该 app 不由控制面挂载**：由**独立进程**启动（`agent_server/__main__.py::main` 用 uvicorn 起 `create_app(token=...)`；`forgeflow/codeplane/engine.py` 以裸脚本方式拉起它，loopback + per-process `SESSION_API_KEY`）。**唯一调用方**是 `forgeflow/codeplane/engine.py` 跑完后的清理 `await client.delete(f"/api/conversations/{conversation_id}")` → **内部清理专用、非用户可达**。
  - 同表另有两条方法列表含 DELETE，**但均为桩 / 未实现**：`conversations_collection`（`Route("/api/conversations", ...)` 的 DELETE 分支直接返回 501）、catch-all `Route("/api/{rest:path}", not_implemented, ...)`（501）。
  - 全仓其余非装饰器路由面仅 `mcp/server/main.py` 的 `@mcp.custom_route("/health", methods=["GET"])`（无 DELETE）。

**修正后的口径（后端真实现 DELETE 端点 = 2 个）**：
1. 控制面 `DELETE /api/memory/{memory_id}`（`memory.py::delete_memory`）—— **前端零调用方**；
2. 代码执行面 `DELETE /api/conversations/{conversation_id}`（`agent_server/app.py::conversation_item`）—— **内部清理专用、独立进程、非控制面挂载、非用户可达**。

**⇒ 用户可达的删除能力 = 0 个。**

逐域（**控制面**业务域）如下：

| 域 | 建 C | 读 R | 改 U | 删 D | D 证据 | 无删除的实际后果 |
|---|---|---|---|---|---|---|
| **runs** | `POST /workspace/tasks`、`POST /tasks` | `GET /runs`、`/runs/{id}` | `POST /runs/{id}/replan`、`/abort` | ❌ 无 | — | 垃圾 / 失败 run 无法清理，永久堆积；用户无法「撤除我不想要的历史」 |
| **skills** | `POST /skills` | `GET /skills`、`/{id}/versions`、`/export` | `POST /{id}/versions`、`/rollback`、`/canary/resolve` | ❌ 无（`rollback` ≠ 删除） | — | 误建 / 废弃技能**无法移除**；已共享的技能无法彻底撤回（仅能版本回退） |
| **memory** | `POST /memory`、`/store` | `GET /memory`、`/search`、`/scopes`、`/lifecycle` | `POST /{id}/promote`、`/lifecycle/sweep` | ✅ **`DELETE /memory/{memory_id}`**（控制面唯一） | `memory.py::delete_memory` | 仅此一域具备删除；见 B.2 可达性缺陷 |
| **approvals** | 系统自动 | `GET /approvals/pending`、`/{token}` | `POST /{token}/approve`、`/reject`、`/{id}/decision` | ❌ 无 | — | 合理（审批应留痕、不可删）；`reject` 已构成「否决」语义 |
| **resources** | `POST /resources/{files,database,code,knowledge_base,api}` | `GET /resources`、`/{id}`、`/limits`、`/{id}/preview` | ❌ 无 | ❌ 无 | **repo 层有 `delete` 但零调用方**：`repositories/base.py::ResourceRepository.delete`、`repositories/memory/resource_repo.py`、`repositories/postgres/resource_repo.py` | 误注册的资源**无法清理**；且存在「repo 已备删除能力却无 HTTP 面」的**未接线** |
| **workspaces** | `POST /workspaces/` | `GET /workspaces/`、`/{slug}` | ❌ 无 | ❌ 无 | — | 误建 workspace 永久残留 |
| **policies** | `POST /policies` | `GET /policies` | `POST /policies/evaluate` | ❌ 无 | — | **错配策略无法撤除** —— 安全 / 合规风险（与「治理底座」诉求冲突） |
| **experiences** | `POST /experiences` | `GET /experiences`、`/{id}/lineage` | ❌ 无 | ❌ 无 | — | **经验摘要可能含 PII，却无任何删除通路** → 「被遗忘权」诉求无法满足 |
| **cost** | ❌ 无 | `GET /cost/board`、`/cost/savings` | ❌ 无 | ❌ 无（repo 层有 `delete` 零调用方：`repositories/memory/cost_repo.py`、`postgres/cost_repo.py`） | — | 纯读域，无删除属正常；但 repo `delete` 属未接线 |
| **audit** | ❌ | `GET /audit/search`、`/stats`、`/export` | ❌ | ❌ 无 | — | **正确**（审计应不可变） |
| **agents** | ❌（内置目录） | `GET /agents`、`/catalog`、`/dispatch`、`/{id}/status` | `POST /{id}/message` | ❌ 无 | — | 内置目录无需删；正常 |
| **workflows** | `POST /workflows/run`、`/stream` | `GET /workflows/{id}`、`/{id}/trace` | ❌ | ❌ 无 | — | 与 runs 同源问题 |
| **marketplace** | `POST /marketplace/skills/publish` | `GET /skills`、`/templates` | `POST /skills/{id}/install`、`/rate` | ❌ 无 | — | ⛔ **无 unpublish / uninstall**：上架后无法下架、误装无法卸载（见 §F Q2） |
| **skill-candidates** | `POST /skill-candidates` | — | `POST /{id}/promote` | ❌ 无 | — | 候选态无删除 |

**§B.1 结论**：控制面 12 个业务域中，**仅 `memory` 一域有删除能力**（`DELETE /api/memory/{memory_id}`），且其**前端零调用方**（§B.2）；`resources`/`cost` 两域在 repo 层写了 `delete` 但**从未被调用**（未接线）。加上代码执行面的**内部清理 DELETE**（非用户可达），**后端真实现 DELETE 端点共 2 个、用户可达 0 个**。**runs / skills / policies / experiences / marketplace 五域的删除缺席会造成真实后果**（垃圾堆积、下架不可撤、错配策略不可撤、被遗忘权不满足），已在上表逐条写明。

### B.2 是否发生过「能力被删除」

基线要求：用户当时明确要求「**只改变展示层级，不删除后端能力**」。

**提交 `d98d0f6`（INC39 主体）** `git show --stat` 核对：15 文件变更，**唯一整文件删除 = `frontend/src/views/runs/ResultNextActions.tsx`（前端）**；其余为前端 / 文档 / e2e 的局部删改。**零后端文件、零路由、零后端函数被删。**

**提交 `71bb307`（INC39b 去重）** `git show --stat` 核对：7 文件变更，**全部前端 / 文档 / e2e**（`ResultContextActions.tsx`、`ResultPanel.tsx`、`resultActions.ts`、`types.ts` + e2e + docs）。**零后端文件变更。**

**退役 testid 的后端调用路径核对**：

| 退役 testid | 原后端通路 | 该通路是否仍存在 | 是否仍有前端调用方 | 判定 |
|---|---|---|---|---|
| `result-action-agent`（「让智能体处理」） | `POST /runs/{id}/replan`（`runs.py::replan_run`）或 `POST /tasks` | ✅ 均存在 | ✅ **replan 仍可达**（段④`result-rerun` / 段①`result-env-rerun` / `CodeApproval`「重新分析」）；⚠️ `POST /tasks` **已无前端调用方**（见下） | 后端在；统一入口取消 |
| `result-action-self` | 纯前端进入编辑态（`useArtifactEdit`） | n/a（无后端） | ✅ `ResultContextActions.tsx` 的 编辑/保存 保留 | 无损 |
| `result-action-view-diff` | 纯前端切 Tab | n/a | ✅ 由 `diff` kind 复用 `#code-diff` | 无损 |
| `result-continue` | `POST /tasks` | ✅ 路由在 | ❌ 已改由 `FollowUpComposer`（`conv-followup`）→ `POST /workspace/tasks` | 后端在，通路改变 |
| `result-quick-*` | `POST /tasks` | ✅ | ❌ 已由 `deriveContextualActions` 的 `continue` → `POST /workspace/tasks` 取代 | 后端在，通路改变 |
| `result-next-actions` | 容器（无后端） | n/a | ❌ 容器整体退役，由 `result-contextual-actions` 取代 | 展示层变更 |

**关键发现（能力可达性）**：

1. ⛔ **`DELETE /memory/{memory_id}` 前端零调用方**：`frontend/src/api/client.ts` 全文**不含任何 `DELETE` 方法调用**（仅 `/memory/store`、`/memory/scopes`、`/memory/search`），`MemoryView.tsx` / `KnowledgeView.tsx` 亦无删除交互。→ **控制面唯一的删除能力，用户点不到。**（另一个 DELETE 在代码执行面，属内部清理、本就非用户可达 —— 合起来 **用户可达的删除 = 0**。）

2. ⚠️ **`POST /tasks` 已失去全部 SPA 调用方**：`frontend/src/api/hooks.ts::useCreateTask`（唯一调用 `hubApi.createTask` → `POST /tasks`）**在 `frontend/src` 内无任何 import**（仅被 dev harness `frontend/_harness_inc24.tsx` 引用）。任务创建现统一走 `useWorkspaceCreateTask` → `POST /workspace/tasks`。
   - 属性判定：这是 **INC32 及之后的通路迁移**（`docs/sop/INC32-DESIGN.md` 载明 `result-continue` 改调 `POST /workspace/tasks`），INC39 未触碰后端。
   - 结论：**后端 `POST /tasks` 在、用户点不到**（被 `/workspace/tasks` 取代）—— 属**能力可达性下降**（非能力删除）。

**§B.2 结论**：INC39 / INC39b **严格遵守了「不删除后端能力」** —— 后端代码零删除。但存在两处**可达性**问题：控制面 DELETE 路由前端不可达（用户可达删除 = 0）；`POST /tasks` 前端不可达。`documented` 的 `replan` 通路**完整保留且仍被调用**。

> **审计方法自省（写进本题的教训）**：本报告 B-1 初稿曾把口径写成「全仓唯一 DELETE = `memory.py::delete_memory`」——**该初稿只扫了 `@router.*` 装饰器，漏掉了代码执行面的手写 `Route(...)` 表**。经复核修正为「后端真实现 DELETE 端点 2 个、用户可达 0 个」。**凡「全仓清点」类结论，必须同时扫 (a) 框架装饰器、(b) 手写路由表 `Route(...)`/`add_route`、(c) 其它 ASGI 装配面**；本报告 B-1 已按此三路重扫。

---

## C. 死代码 / 未接线能力

方法：AST 提取 `forgeflow/**` 顶层 `def`/`class` 与 `frontend/src/**` 导出符号，统计全仓（含 `tests/`）引用次数；过滤掉「被装饰器注册」的路由/工具处理器（那些是动态注册，非死代码）。

> **复核（按 B-1 教训做的「方法无关」重扫）**：对 §C.1 的 12 项，改用**与注册方式无关**的检索 —— 在 `forgeflow/**`（含 `codeplane/runner/agent_server/`）与 `tests/**` **全域按符号名搜索**（覆盖 Python 符号、字符串名、装饰器表、手写路由表、非 `.py` 文件），并额外做全仓（排除 `qa_tmp`/`node_modules`）搜索。**结论：12 项在各自定义处之外零出现，逐项维持原判，无翻案。** agent_server 那层对本次死代码判定**不构成盲区**（其顶层 handler 均被 `Route(...)` 表按名引用，故未被判死；而 §C.1 的 12 项与 agent_server 无关，且全树无引用）。

### C.1 后端真死代码（零调用方，含测试）

| # | 符号 | 判定 |
|---|---|---|
| 1 | `forgeflow/graph/edges.py::route_after_analysis` | **真死代码**：条件边函数，全仓无引用（`route_supervisor`/`route_human_approval` 有引用，唯它没有）→ 未接入任何图 |
| 2 | `forgeflow/experience/dedup.py::dedup_experience` | **真死代码**：函数式包装；生产走 `ExperienceDeduplicator` 类（`extractor.py` 引用），此包装无调用方 |
| 3 | `forgeflow/experience/dedup.py::dedup_batch` | 同上 |
| 4 | `forgeflow/resilience/circuit_breaker.py::get_circuit_breaker` | **真死代码**：注册表工厂；`CircuitBreaker` 类被 `agents/base.py` 直接用，工厂无人调 |
| 5 | `forgeflow/observability/cost_tracker.py::is_priced_model` | **真死代码**：无引用 |
| 6 | `forgeflow/auth/membership.py::user_workspaces` | **真死代码**：无引用 |
| 7 | `forgeflow/auth/memory_store.py::MemoryAuthDisabledError` | **真死代码（永不抛出）**：异常类，全仓无 `raise`、无引用 |
| 8 | `forgeflow/governance/dlp_rules.py::rule_set_to_dict` | **真死代码**：无引用 |
| 9 | `forgeflow/api/routers/skills.py::spec_diff` | **真死代码**：无引用 |
| 10 | `forgeflow/experience/embedding.py::as_dict` | **真死代码**：无引用 |
| 11 | `forgeflow/workflows/finance_recon/models.py::MatchResult`、`::ReconciliationReport` | **真死代码**：两个 Pydantic 模型，全仓（含测试）零引用 |
| 12 | `forgeflow/workflows/sales_ops/models.py::LeadStatus`、`forgeflow/workflows/support_ops/models.py::TriageResult`、`::SupportReply` | **真死代码**：三个模型零引用 |

**未接线（repo 层有能力但无调用方）**：

| 符号 | 判定 |
|---|---|
| `forgeflow/repositories/base.py::ResourceRepository.delete`（及 `memory/`、`postgres/` 两实现） | **未接线**：定义了资源删除，**无任何调用方**，也无对应 HTTP 路由 |
| `forgeflow/repositories/memory/cost_repo.py::delete`、`forgeflow/repositories/postgres/cost_repo.py::delete` | **未接线**：同上 |

**仅测试接线（declared，生产零调用，仅单测覆盖）** —— 非严格死代码，但属「有类无生产消费者」的诚实备注：
`forgeflow/governance/tenancy.py::TenantIsolation` / `::assert_tenant`（**仅 `governance/__init__.py` 再导出 + `tests/unit/test_tenant_isolation.py` 使用**）。实际租户隔离由「repository 首参 `tenant_id` 约定 + 路由 `resolve_tenant` 依赖」实现；`TenantIsolation` 类本身**无生产调用点**。⚠️ 与 §A 诉求 9「跨租户零泄露」**不矛盾**（隔离机制走 repo 约定），但该类当前是「声明了没人用」。

### C.2 前端死代码

**视图层（`frontend/src/views/**`）：零孤儿视图。** 全部 `views/*.tsx` 均被 `router.tsx`（`shellChild`/`guardedShellChild` 或 full-screen `createRoute`）或 `LiveRunsView` 等宿主引用。主理人「死 UI」假设**不成立**。

**导出符号层（零引用，`frontend/src` 内无人 import）**：

| # | 符号 | 文件 | 判定 |
|---|---|---|---|
| 1 | `useHealth` | `frontend/src/api/hooks.ts` | 未接线 hook |
| 2 | `useCostByAgent` | `frontend/src/api/hooks.ts` | 未接线 hook |
| 3 | `useCostByWorkflow` | `frontend/src/api/hooks.ts` | 未接线 hook |
| 4 | `useTopRuns` | `frontend/src/api/hooks.ts` | 未接线 hook |
| 5 | `useCreateTask` | `frontend/src/api/hooks.ts` | **未接线**（唯 `_harness_inc24.tsx` dev harness 用）→ 见 §B.2 |
| 6 | `workspaceSession`（单数） | `frontend/src/api/client.ts` | 未接线（复数 `workspaceSessions` 在用） |
| 7 | `IconStop` | `frontend/src/components/icons.tsx` | 未接线图标 |
| 8 | `GateRole` | `frontend/src/auth/roleGate.ts` | 未引用类型 |
| 9 | `hasCodePlane` | `frontend/src/views/runs/realRun.ts` | 未引用 |
| 10 | `runStepData` | `frontend/src/views/runs/conversation.ts` | 未引用 |

### C.3 路由层

所有 `@router.*` 装饰的处理器均被 FastAPI 动态注册，**非死代码**（AST「零引用」为误报）。**唯一真正「无消费者」的路由能力是 `DELETE /memory/{id}`**（前端不可达，见 §B.2）—— 属**可达性**问题而非死代码。

### C.4 判定说明

- `frontend/src/views/runs/realRun.ts`、`types.ts` 等的注释里仍提及 `ResultNextActions`（历史锚点），**非代码依赖**，不计入死代码。
- `frontend/_harness_inc24.tsx`、`frontend/dist/**` 为 dev / 构建产物，未纳入生产死代码判定。

---

## D. 分析充分性（「分析是否足够」）

挑选两条**产品核心分析链路**做深度评估。

### D.1 `frontend/src/views/runs/resultActions.ts::deriveContextualActions`（任务类型 → 上下文动作派生）

**它做什么**：按「真实产物」把已完成 run 分到 4 档（代码 / 数据 / 知识 / 兜底），派生 **0～3 条**快捷操作。

| 评估维度 | 结论 |
|---|---|
| **分支是否覆盖真实任务类型全集** | 覆盖 4 类（`codeplane.present` → 代码档；`metrics>0 || findings>0` → 数据档；`sources>0` → 知识档；`artifacts>0` → 兜底档）。**但任务类型本身不是被分析出来的** —— 是**按产物计数**的短路判定，非语义分类 |
| **静默回落** | ✅ 有：**五档全不满足 → 返回空数组**（`resultActions.ts::deriveContextualActions` 第 5 分支），UI 整段不进 DOM。用户**看不到「为什么没有后续动作」**的任何解释 → 属**静默**（设计如此，但不可解释） |
| **「看起来在分析、其实是常量」** | ⚠️ **命中**：档位内的**动作标签与指令文本全是硬编码常量**（`INSTRUCTION_FIX`/`INSTRUCTION_DEEP_DIVE`/`INSTRUCTION_CHART`/`INSTRUCTION_FOLLOW_UP`/`INSTRUCTION_SUMMARY`）→ 只有**档位选择**是数据驱动的，**动作内容不是**。所有 `continue` 动作统一走 `POST /workspace/tasks` 的**定值指令** |
| **结论是否被真实使用** | ✅ 是：`ResultContextActions.tsx::ResultContextActions` 直接渲染 `actions`（`result-contextual-actions` / `result-ctx-*`），点击有真实效果（`continue`→建后续 run；`diff`→滚到 `#code-diff`；`export`→本地下载；`sources`/`trace`→切 Tab） |
| **不足以支撑什么场景** | ① **无法区分同一档内的不同任务语义**（「生成图表」与「继续深入分析」对任何数据档任务都是同一批按钮）；② **无法为无产物任务给出可解释的下一步**（静默空）；③ 档位互斥的**优先级是硬编码的**，知识型任务若同时有 metrics 会被强制判为数据档 |

### D.2 `forgeflow/experience/context_builder.py::build_context`（三源召回）

**它做什么**：`memory`（五层 scope）+ `skill`（registry 关键词选）+ `experience`（embedding 相似）三源召回 → 去重 → 预算内压缩 → 排序。

| 评估维度 | 结论 |
|---|---|
| **分支是否覆盖真实任务类型全集** | 无「任务类型」概念 —— 三源均**只按 `intent` 文本召回**。覆盖的是「数据来源」而非「任务语义」 |
| **静默回落** | ⚠️ **命中**：`context_builder.py::_recall` 的 **三个 `try/except` 均为 `logger.warning` 后继续**（`"recall must never crash the build"`）。任一源失败 → **该源静默缺席**，`ContextBundle` 不标记 degraded、不区分「无数据」与「召回失败」 |
| **「看起来在分析、其实是常量」** | ⚠️ **命中**：`_score` 的三元权重中，**skill 源的 `similarity` 硬编码为 1.0**、skill / experience 的 `scope_weight` 恒为 `_DEFAULT_SCOPE_WEIGHT(0.7)` → 这两源的「排序」实际只由 `usage`（skill）与 embedding 相似度（experience）驱动，**大部分排序信号是常量** |
| **结论是否被真实使用** | ✅ 是：`resultActions` 之外的运行时 —— `orchestrator.py::_build_run_context`（`build_context` 的**单一运行时调用点**）→ `persist_context_build` 落库；bundle 注入 run 上下文 |
| **不足以支撑什么场景** | ① **无法解释「为什么召回失败」**（三源静默降级，不可区分故障与空库）；② **跨源优先级靠常量权重**，难以按任务类型调优；③ 无「召回质量」反馈回路（`hit_rate` 仅计数） |

**§D 核心判断**：两条链路**都能跑、结论都被真实消费**，但**分析深度均停留在「开关 / 档位」级别**：分支覆盖面窄（按计数或按文本，而非语义），且**都存在静默回落**，且**都含有「看似派生实为常量」的成分**。**不足以支撑**「需要按任务语义差异化、且需可解释为何无下一步 / 为何召回为空」的场景。

---

## E. 需求基线漂移（审计发现）

| 项 | `目标.md` 声称 | 代码现状（HEAD `71bb307`） | 判定 |
|---|---|---|---|
| 最后更新 | 「2026-09-25（INC12，HEAD `ed3ebef`）」 | INC39 / HEAD `71bb307` | ⛔ **SSOT 停在 INC12，落后约 27 个增量** |
| 测试基线 | 1078 tests | 代码已到 INC39（测试数未在本次静态审计重算，由 QA 实跑） | ⚠️ 数字不可信 |
| §2.1/§2.2 状态 | 均为快照时点 | 未随 INC25/26/27/32/34/36/39 更新 | ⚠️ 结构映射仍真实，但**状态列已过期** |

**后果**：`目标.md` 作为「唯一事实源」**已失效** —— 任何以它为验收基线的人都看不到 INC39 的展示层重构与能力可达性变化（§B.2）。

---

## F. 主理人需要用户裁断的问题

以下为**产品决策**（非技术判断），需用户拍板：

1. **「删除功能」要做到什么程度？** 当前仅 `memory` 域有删除，且前端不可达。需要明确范围：
   - (a) 仅补齐「前端可达 `memory` 删除」即可？
   - (b) 还是要为 **runs / skills / resources / policies / experiences / workspaces** 逐域补删除（软删 or 硬删）？
   - (c) 是否要求**合规级「被遗忘权」**（按租户 / 按用户级联删除 experience / memory / artifacts）？
   → 影响工程量与合规边界，属产品决策。

2. **Marketplace 是否需要「下架 / 卸载」？** 现无 unpublish / uninstall。已上架的技能与已安装的实例**无法撤回**。是否需要？（安全事件下的「紧急下架」是常见合规诉求。）

3. **`目标.md` 是否仍是 SSOT？** 它已停在 INC12。是否要求 (a) 追平到 INC39，或 (b) 改用 `docs/sop/` 下的增量文档为准，或 (c) 冻结为历史快照 + 另立新 SSOT？—— 这决定后续所有验收以谁为准。

4. **`POST /tasks` vs `POST /workspace/tasks` 是否保留双路由？** 前者已无 SPA 调用方。是 (a) 保留为兼容 API（对外集成仍可用），还是 (b) 标记 deprecated / 移除？

5. **「死代码」是否清理？** §C 列出的后端 12 项 + 前端 7 项。部分可能是「未来预留 / 对外 API」。是否授权清理，或要求逐条登记为「预留并说明用途」？

6. **`deriveContextualActions` 的静默空态是否可接受？** 无产物任务现在**不给任何后续提示**（用户可能困惑「Agent 卡住了吗」）。是否需要可解释的空态（如「暂无后续动作」）？

---

## 附：本报告证据来源（只读）

- 路由清点：`forgeflow/api/main.py`（22 个 `include_router`）+ `forgeflow/api/routers/*.py`（101 个 `@router.*`）
- 删除面：`forgeflow/api/routers/memory.py::delete_memory`（控制面）；`forgeflow/codeplane/runner/agent_server/app.py::create_app` 手写 `Route(...)` 表中的 `conversation_item`（代码执行面，DELETE 在 `methods=` 列表内）；`forgeflow/repositories/base.py::ResourceRepository.delete`（零调用方）
- 前端不可达：`frontend/src/api/client.ts`（无 DELETE）；`frontend/src/api/hooks.ts::useCreateTask`（无 import）
- 提交审计：`git show --stat d98d0f6`、`git show --stat 71bb307`（合法只读）
- 分析链路：`frontend/src/views/runs/resultActions.ts::deriveContextualActions`；`forgeflow/experience/context_builder.py::build_context`
- 死代码：AST 全仓引用扫描（`forgeflow/**` + `frontend/src/**`）
