# AgentFlow · 框架结构对齐核验（08-FRAMEWORK-ALIGNMENT）

> 目的：回答用户的核心约束 —— **「必须确保我项目结构」**。
> 方法：**读磁盘、枚举文件、跑探针、起真服务打接口**，不采信任何口头/历史自报。
> 核验人：架构师 高见远（本轮 INC3 WS-C）｜ 核验时间：2026-09-23
> 代码根：`ForgeFlow-main/`（改造自开源 ForgeFlow）｜ 后端：`forgeflow/`
>
> **重要**：本版**整篇重核**，上一版（写「71 路由 / 30 登记 / UNMAPPED=9」）中的**路由与缺陷数字已过期**，以本版实测为准（见 §1、§4、§6）。

---

## 0. 一句话结论

用户给定的八层框架 ——
`Skill Hub / Memory Hub / Security Hub → Agent Runtime → MCP Tool Layer → Validation Layer`
**已在代码中完整落位**：六层 + 数据/接口层，每一层都能指到**具体文件与具体符号**，并已用**真服务**验证端到端可用，**不是纸面架构**。

- **路由/权限**：`openapi()` 实测 **76 条 (method,path)**；`ROUTE_PERMISSION_MAP` **33 条**；用**中间件真实解析逻辑**跑差集 **UNMAPPED = 0**（权威判据与出处见 §1）。
- **框架能力**：八层全部命中。

> **🔄 INC4 状态刷新（2026-09-23 晚 · 主理人亲跑复核）**：上一版点名的「落盘≠交付」实例已**全部闭合**——
> ① 离线启动（含**回归护栏** `tests/integration/test_offline_boot.py` 真 lifespan 用例）｜② Skill Evolution → 审批（`POST /skills/evolution-advice/{skill_id}/approval`）｜③ Cost 账本｜④ C1 多模型降级链（`models/provider.py::get_model` 真循环 + `ModelUnavailableError`）｜⑥ `ollama_think`（`_build_ollama` 现在**真的读**该字段）；
> 另由用户拍板新增的 **⑦ Agent 真实使用 LLM**（原 `_default_executor` 是硬编码剧本、从不调 LLM）与 **⑨ 离线认证**（memory 档原来**根本登不进**）也已交付。证据见 **§6.7 / §6.8**，逐条均为「读盘 + 真跑」确认，非口头自报。

---

## 1. 本轮读盘/实测数据（全部本次亲跑，命令与结果可复现）

| 核验项 | 方法（本轮亲跑） | 实测结果 |
|---|---|---|
| 路由总数 | 导入 `forgeflow.api.main:app` → `app.openapi()`，枚举 paths×method | **76 条 (method,path)** |
| RBAC 登记表 | `len(ROUTE_PERMISSION_MAP)` | **33 条** |
| fail-closed 差集 | 用 `RBACMiddleware._resolve_permission()` + `_is_open()` **真实逻辑**跑全部路由 | **UNMAPPED = 0** |
| 有意公开路由 | 同上，命中 `_is_open` 者 | **6 条**：`GET /health` + `POST /auth/{login,logout,refresh,introspect,oidc/exchange}` |
| 真 PostgreSQL | `asyncpg.connect(postgresql://forgeflow:forgeflow@127.0.0.1:5433/forgeflow)` | **连通**；`public` 表 **39 张**；`alembic_version=011`；`pgvector` 扩展已装 |
| 011 迁移落库 | 查 `experiences` 列 | `conflict_with / merged_from / dedup_key / confidence` **四列俱在** |
| 错误凭据对照 | `postgres:postgres@5433` | **失败**：`InvalidPasswordError: password authentication failed for user "postgres"` |
| 维护库对照 | `forgeflow@5433/postgres` | 连通但 **0 张表**（别用错库） |
| Alembic 头部 | `alembic heads` | **`011 (head)`**（`versions/` 仅 001–011，**无 012**） |
| 真 Ollama | `GET http://127.0.0.1:11434/api/tags` | **在线**；模型 `['qwen2.5vl:3b', 'qwen3:8b']` |
| 真服务端到端 | 起 uvicorn（memory+mock，127.0.0.1:8010）后打接口 | `/health` healthy；`/tasks`→`completed`；`/experiences` 落库；`/marketplace/skills/publish` **200**；`/memory/{id}/promote` **200 user→team**；`/audit/export` 113 条；`/metrics/slo` 三档；`/cost/savings` `has_data=false/amount=null` |

> 说明：路由/权限/迁移/Ollama/PG 均为**本次亲跑**。端到端接口为**本次起真服务**实测（非 TestClient）。
>
> **⚠️ UNMAPPED 的权威定义与出处（防误判）**：判据 = `tests/unit/test_route_permissions.py:36 test_every_route_is_mapped_or_intentionally_open` —— 「**未映射**（`RBACMiddleware._resolve_permission(method, path)` 返回 `("", "")`）**且** **非有意公开**（`not RBACMiddleware._is_open(path)`）」= **0**（主理人亲跑门禁：**6 passed**）。那 **6 条有意公开**路由 = `GET /health` + `POST /auth/{login,refresh,logout,introspect,oidc/exchange}`，由 `middleware/auth.py:34 _OPEN_PATHS` / `:38 _OPEN_PREFIXES` 放行。**⚠️ 别用裸探针**：若只按「最长前缀命中」（不叠加 `_is_open`），会把这 **6 条数成「未映射」并误判成缺陷**——本团队已踩过一次，故此处留权威判据。

---

## 2. 用户八层结构 → 逐层落点（文件 + 关键符号，均读盘确认）

### L1 · Skill Hub（技能中枢）

| 能力点 | 用户诉求 | 代码落点（文件 : 关键符号） | 状态 |
|---|---|---|---|
| Skill Registry | 技能管理 | `forgeflow/skills/registry.py` : `class SkillRegistry`（`create/get/list_skills/select/bump_usage/seed_featured`） | ✅ |
| Skill Versioning | 技能迭代 | `forgeflow/skills/versioning.py` : `create_version/rollback/bump_semver/diff_specs/change_summary` | ✅ |
| Skill Evaluation | Agent 变强 | `forgeflow/skills/evaluator.py` : `evaluate_candidate`（`_completeness/_coverage/_tool_diversity`） | ✅ |
| Skill Evolution | Agent 变强 | `forgeflow/skills/evolution.py` : `advise` / `EvolutionAdvice` / `_llm_batch`（规则优先 + 单次批量 LLM + 缓存） | ⚠️ 见 §6.3（建议→审批未接线） |
| 治理闸门 | 可信 | `forgeflow/skills/governance_gate.py` : `promote_candidate` | ✅ |
| 可信基线 | 可信 × 进化 | `forgeflow/skills/trust_baseline.py` : `verify_trust_baseline` / `TrustReport`（`__all__` 存在） | ✅ |
| Skill Marketplace | 技能共享 | `forgeflow/skills/marketplace_bridge.py` : `publish_listing/search_listings/install_listing/rate_listing`（含 `__all__`） | ✅（真服务 200） |
| 候选编译器 | 任务→经验→Skill | `forgeflow/skills/candidate_compiler.py : compile_candidate` + `draft_spec.py : DraftSpec` | ✅ |
| 候选/版本/回滚 API | — | `api/routers/skills.py`：`GET /skills`、`GET /skills/{id}/versions`、`POST /skills/{id}/versions`、`POST /skills/{id}/rollback`、`GET /skills/evolution-advice`、`/skill-candidates*` | ✅ |

### L2 · Memory Hub（记忆中枢）

| 能力点 | 用户诉求 | 代码落点（文件 : 关键符号） | 状态 |
|---|---|---|---|
| Hierarchy（五层） | 不忘记上下文 | `forgeflow/experience/scopes.py` : `class MemoryScope(str,Enum)` = `user/team/episodic/semantic/org` + `SCOPE_RULES` | ✅ |
| Team Memory | 团队经验沉淀 | 同上 → `MemoryScope.TEAM`；`scope_namespace()` 生成 `workspace/{tenant}/team/{team}` | ✅ |
| Organizational Memory | 企业知识沉淀 | `forgeflow/experience/promotion.py` : `promote_memory`（promotable 校验 + org 需 admin + 强制审计） | ✅（真服务 user→team 200） |
| Episodic/Semantic/User | 不忘记上下文 | `scopes.py` 枚举 + `experience/memory_store.py`（`MemoryEntry/save_memory/search/move_memory`） | ✅ |
| 语义检索 | — | `experience/embedding.py` : `embed_text/deterministic_embedding/cosine_similarity`；`repositories/{memory,postgres}/*experience_repo*` | ✅ |
| 经验提取 | 任务→经验 | `experience/extractor.py` : `class ExperienceExtractor` / `extract_experience` | ✅ |
| Experience 一等实体 | — | `experience/models.py` : `class ExperienceRecord` | ✅ |
| 去重 / 冲突消解 | 记忆质量 | `experience/dedup.py` : `class ExperienceDeduplicator` / `dedup_experience` / `dedup_batch` | ✅ |
| Context Builder | 数据/上下文优化 | `experience/context_builder.py` : `build_context` / `ContextBundle`；`experience/token_budget.py` : `estimate_tokens` | ✅（`orchestrator._build_run_context` 真实调用） |
| 记忆读写 API | — | `api/routers/memory.py`：`POST /memory`、`POST /memory/store`、`GET /memory`、`GET /memory/search`、`GET /memory/scopes`、`DELETE /memory/{id}`、`POST /memory/{id}/promote` | ✅ |

### L3 · Security Hub（安全中枢）

| 能力点 | 用户诉求 | 代码落点（文件 : 关键符号） | 状态 |
|---|---|---|---|
| Policy Engine | Agent 权限 | `forgeflow/governance/policy_engine.py` : `class PolicyEngine` / `evaluate_task_entry` / `classify_risk` | ✅ |
| RBAC | Agent 权限 | `forgeflow/rbac/{policies,enforcer}.py`（`ROLE_PERMISSIONS` / `ROUTE_PERMISSION_MAP` / `RBACEnforcer`）+ `middleware/auth.py : class RBACMiddleware` | ✅ |
| ABAC 条件 | Agent 权限 | `forgeflow/governance/conditions.py` : `evaluate_condition/evaluate_operator`（`in/ne/gt/lt/contains/regex` + `all/any`） | ✅ |
| Tenant Isolation | 数据不泄露 | `forgeflow/governance/tenancy.py` : `class TenantIsolation` / `assert_tenant` | ✅ |
| DLP | 数据不泄露 | `forgeflow/governance/dlp.py : class DlpGate` + `dlp_rules.py : class DlpRuleSet` + `security/pii_redactor.py : redact`（含 `cn_id` 规则） | ✅ |
| 工具输出防注入 | 数据不泄露 | `forgeflow/security/tool_output_guard.py`（`<UNTRUSTED_TOOL_OUTPUT name="…">` 包裹） | ✅ |
| 审计留痕 | — | `forgeflow/middleware/audit.py : class AuditMiddleware` | ✅ |
| 审计导出（离线可跑） | — | `api/routers/audit.py`：**内存环形缓冲** `record_audit_entry/_memory_filter/clear_audit_ring` + `GET /audit/export`、`GET /audit/search`、`GET /audit/stats` | ✅（真服务导出 113 条） |

### L4 · Agent Runtime（运行时）

| 能力点 | 用户诉求 | 代码落点（文件 : 关键符号） | 状态 |
|---|---|---|---|
| Supervisor / 多 Agent | 多 Agent（LangGraph） | `graph/builder.py : compile_graph`（`app.state.graphs` 编译 sales_ops/support_ops/finance_recon）；`runtime/orchestrator.py` 的 `_DEFAULT_STEPS` | ✅ |
| Agent 目录 | — | `runtime/agents_catalog.py : PLATFORM_AGENTS`（**6 个**：planner/data/code/research/document/security） | ✅（见 §4 更正） |
| Durable Run | 长期运行 | `graph/checkpointer.py`（PG checkpointer，`langgraph-checkpoint-postgres`） | ✅ |
| SSE 实时过程 | 实时过程 | `runtime/events.py : RunEventBus`；`api/routers/runs.py` → `GET /runs/{id}/events` | ✅（真服务收到 `run.started…`） |
| 运行时 RBAC 复核 | Agent 权限 | `runtime/gate.py` : `check_tool_permission / required_permission / describe_denial` | ✅ |
| 运行时风险闸门 + HITL | 高风险操作 | `runtime/orchestrator.py : _default_executor`（逐步 `PolicyEngine.evaluate_tool_call`，**执行前**拦截）+ `_escalate_hitl` | ✅ |
| 多模态进任务 | — | `runtime/attachments.py : prepare_attachments / AttachmentInput` | ✅ |
| 事件驱动 Run | — | `events/dispatcher.py : EventDispatcher._dispatch_task`（`workflow_type=agentflow_task`） | ✅ |
| Run/Task API | — | `api/routers/tasks.py : POST /tasks`；`api/routers/runs.py : GET /runs`、`GET /runs/{id}`、`POST /runs/{id}/replan` | ✅ |

### L5 · MCP Tool Layer（工具层）

| 能力点 | 用户诉求 | 代码落点（文件 : 关键符号） | 状态 |
|---|---|---|---|
| MCP Server | 工具调用 | `forgeflow/mcp/server/main.py : mcp = FastMCP(...)`，`mcp.mount(...)` × **14** | ✅ |
| MCP Client/Adapter | 工具调用 | `forgeflow/mcp/client/adapter.py : get_mcp_tools`（不可用则降级，不抛错） | ✅ |
| 14 个 tool router | DB/Search/File/Git/Enterprise | `mcp/server/tools/`：`search, crm, email, data, slack, github, jira, hubspot, salesforce, servicenow, sap, quickbooks, msgraph, multimodal` = **14** | ✅ |
| 8 个企业连接器 | Enterprise API | `forgeflow/connectors/`：`github, hubspot, jira, msgraph, quickbooks, salesforce, sap, servicenow` = **8**（`base.py` 为基类） | ✅ |
| 工具输出防注入 | 数据不泄露 | `security/tool_output_guard.py`（同 L3） | ✅ |

### L6 · Validation Layer（验证层）

| 能力点 | 用户诉求 | 代码落点（文件 : 关键符号） | 状态 |
|---|---|---|---|
| Success/Failure 判定 | Agent 出错 | `validation/validator.py` : `validate(run) -> Verdict`（`_SUCCESS/_FAILURE/_ABORTED_STATUSES`） | ✅ |
| Failure → Replan | Agent Loop | `validation/replan.py` : `decide_replan / ReplanDecision / record_replan_event`（上限取 `Settings.max_replan_attempts`，默认 2） | ✅ |
| 超限 → HITL | 高风险操作 | `runtime/orchestrator.py : _escalate_hitl`（写 `ApprovalRecord`） | ✅ |
| Memory 回写 | 任务→经验 | `orchestrator.run_task` 终态调 `ExperienceExtractor.extract` | ✅（真服务 `/experiences` 有记录） |
| Skill Update | 经验→Skill | `skills/candidate_compiler.py → evaluator.py → governance_gate.py → versioning.py` | ✅ |

---

## 3. 数据层与接口层

| 项 | 要求 | 落点 | 状态 |
|---|---|---|---|
| FastAPI | 企业系统 | `forgeflow/api/main.py`（`app` 工厂 + lifespan）+ `api/routers/` | ✅ 76 路由 |
| PostgreSQL | 企业系统 | `repositories/postgres/*` + `alembic/versions/`（**001–011**） | ✅ 真库 39 表 |
| DB 迁移 | — | `010_agentflow_hubs.py` + `011_cost_slo_evolution.py`（`revision="011"`, `down_revision="010"`，全部 `IF NOT EXISTS`/`ADD COLUMN IF NOT EXISTS`） | ✅ head=011 |
| 双后端不可破 | 离线可跑 | `STORAGE_BACKEND=memory\|postgres` + `LLM_PROVIDER=mock` + `EMBEDDING_PROVIDER=mock` | ✅ 见 §6.1（**memory 档免 PG 启动已修** · Task #6）；离线**认证**语义见 07-RUNBOOK-INC2.md §5.1（#9，**待落盘**） |
| 仓储抽象 | — | `repositories/base.py`（Protocol）+ `repositories/factory.py`（`get_{experience,skill,skill_candidate,policy,cost}_repository`，按 backend 惰性选实现） | ✅ |

---

## 4. 与用户结构图的差异说明（如实）

用户结构图与代码有 **3 处**值得记录的差异，**均不改变框架语义**：

1. **图里 `Skill Hub / Memory Hub / Security Hub` 是并列三块**。代码里 **Security Hub 被拆成四个子包**：`governance/`（Policy/ABAC/Tenancy/DLP）+ `rbac/`（角色与路由登记）+ `security/`（PII/注入防护）+ `middleware/`（强制拦截点）。**这是实现上的合理细化**——治理策略、权限模型、防护原语、强制点本就应当分离。
2. **图里 `Validation Layer` 之下是 Success/Failure**。代码里 `Validation` 与 `HITL` 都**落在 `runtime/orchestrator.py` 的执行路径上**（`validation/*` 是纯判定/决策函数，`runtime/gate.py` 与 `_default_executor` 是运行时闸门，而非独立进程层）。**结论：HITL 不是旁路审批，是执行链上的强阻断点**——比图更强。
3. **「我的 Agent」数量**：图/旧文档笼统写「data/code/research 三个 agent_id」；实际 `runtime/agents_catalog.py` 的 `PLATFORM_AGENTS` 是 **6 个产品级 Agent**（planner/data/code/research/document/security）。前端六卡由此接口驱动，**不是前端硬编码数组**。（`_DEFAULT_STEPS` 的执行计划用到 3 个平台工具：`research.search/data.query/code.run`——这是**计划步骤**，不是 Agent 数量。）

---

## 5. 用户 20 条诉求 × 代码落点 × 验证状态（四列定稿表）

> 用户本轮原话：**「必须确保我项目结构」**。下表可**逐行核对**。
> 「验证状态」取值：**`已验证（主理人亲验）`**（主理人本人读盘/亲跑确认，最高档）/ **`已独立复验`**（架构师/第三方用命令复跑，并注明是谁、用什么命令）/ **`作者自查`**（有单测、无第三方复跑）/ **`修复在飞（WS-A）`**（附任务号，**不代表完成**）。
> 「代码落点」全部为**架构师本轮读盘亲确认**（文件 + 关键符号）。

| 用户原话 | 框架层 | 代码落点 | 验证状态 |
|---|---|---|---|
| 数据优化 → Data/Context Optimization | L2 | `experience/context_builder.py`、`experience/token_budget.py`、`runtime/orchestrator.py`(`_build_run_context`) | 作者自查（`orchestrator` 已真实调用 `build_context`；未第三方复跑） |
| 技能管理 → Skill Registry / SkillOps | L1 | `skills/registry.py`、`api/routers/skills.py` | 已独立复验（架构师亲跑真服务：`GET /skills`=4 条；`POST /tasks` 后 `GET /experiences` 回写成功） |
| 团队经验沉淀 → Team Memory | L2 | `experience/scopes.py`(`MemoryScope.TEAM`)、`experience/memory_store.py` | 作者自查 |
| 企业知识沉淀 → Organizational Memory | L2 | `experience/promotion.py`、`api/routers/memory.py`(`POST /memory/{id}/promote`) | 已独立复验（架构师亲跑真服务：promote `user→team`=200，namespace 由 `.../user/admin` 重算为 `.../team/default`） |
| 技能共享 → Skill Marketplace | L1 | `skills/marketplace_bridge.py`、`api/routers/marketplace.py` | 已独立复验（架构师亲跑真服务：`POST /marketplace/skills/publish`=200、`GET /marketplace/skills` total=1） |
| 技能迭代 → Skill Versioning | L1 | `skills/versioning.py`、`api/routers/skills.py`(`/skills/{id}/versions`、`/rollback`) | 作者自查 |
| 长期运行 → Durable Agent Runtime | L4 | `graph/checkpointer.py`、`runtime/orchestrator.py` | 作者自查（lifespan 编译 3 图成功；未跑真 LangGraph pipeline） |
| 不忘记上下文 → Hierarchical Memory | L2 | `experience/scopes.py`（五层 `MemoryScope` 枚举）、`experience/memory_store.py` | 作者自查 |
| 数据不泄露 → Tenant Isolation + DLP | L3 | `governance/tenancy.py`、`governance/dlp.py`、`governance/dlp_rules.py`、`security/pii_redactor.py` | 作者自查 |
| Agent 权限 → Policy Engine | L3 | `governance/policy_engine.py`、`governance/conditions.py`、`rbac/{policies,enforcer}.py`、`middleware/auth.py`、`runtime/gate.py` | 已独立复验（架构师亲跑：以 `RBACMiddleware` 真实解析逻辑跑全部 76 路由 → **UNMAPPED=0**） |
| 企业省钱 → Cost Optimization | L4 旁路 + L3 | `cost/{budget_service,degrade,savings,ledger}.py`、`api/routers/cost.py`(`_period_totals`)、`observability/cost_tracker.py`(`calculate_cost`)、`observability/metrics_source.py` | **已验证（主理人亲验）** — `observability/cost_tracker.py:74`（`calculate_cost`：**未知模型计 `0.0` + WARNING**，宁少算不虚增，消除“给免费 ollama 按云价计费”）；`api/routers/cost.py:80`（`_period_totals` **委托 `cost/ledger.py`**，成本生产者已接通）。原「接口在、账本缺」已消除 |
| Agent 变强 → Evaluation + Skill Evolution | L1 | `skills/evaluator.py`、`skills/evolution.py`、`api/routers/skills.py` | **修复在飞（WS-A）** · Task #7 — 评估 `evaluate_candidate` 作者自查；**`advise()` 已接、`submit_advice_for_approval()` 未接**（见 §6.3） |
| Agent 出错 → Agent Loop | L6 | `validation/validator.py`、`validation/replan.py`、`runtime/orchestrator.py` | 作者自查 |
| 高风险操作 → HITL | L6 | `runtime/orchestrator.py`(`_default_executor`/`_escalate_hitl`)、`governance/models.py` | 作者自查（执行前拦截已读盘确认；失败→HITL 未真跑） |
| 工具调用 → MCP | L5 | `mcp/server/main.py`、`mcp/client/adapter.py`、`mcp/server/tools/*`（14）、`connectors/*`（8） | 作者自查（读盘确认 14 个 `mcp.mount(...)`） |
| 多 Agent 基于 LangGraph | L4 | `graph/builder.py`、`runtime/orchestrator.py`(`_DEFAULT_STEPS`)、`runtime/agents_catalog.py` | 作者自查 |
| 实时过程 SSE | L4 | `runtime/events.py`、`api/routers/runs.py`(`GET /runs/{id}/events`) | 已独立复验（架构师亲跑真服务：SSE 首帧收到 `data:{"type":"run.started",...}`） |
| 企业系统 FastAPI + PostgreSQL ／ **`/metrics` 族离线可用** | 数据/接口层 | `api/main.py`、`repositories/postgres/*`、`alembic/versions/001..011`、`api/routers/metrics.py`(`_optional_pool`) | **已验证（主理人亲验）** — `openapi()` 枚举=**76 条**；PG 5433 连通/39 表/`alembic_version=011`；**`/metrics` 族六端点**（`prometheus` + `cost`×3 + `evaluation` + `summary`/`runs`/`slo`）改用 `api/routers/metrics.py::_optional_pool`（**6 端点改依赖，离线档不再 503**）。**可追溯指针**：另见 **§6.5**（测试-mock 失效模式）
| 实现 任务 → 经验 → Skill | 跨层 | `runtime/orchestrator.py`、`experience/extractor.py`、`skills/candidate_compiler.py` | 已独立复验（架构师亲跑真服务：`POST /tasks`→`completed`→`GET /experiences` 落库；候选编译跳③未真跑） |
| 必须确保我项目结构 | — | 全仓（`forgeflow/` 六层 + 数据/接口层） | 已独立复验（本文 §2 逐层读盘 + §6 不一致点名） |

> **补充（用户点名「按设计图重做首页」）**：前端 `/` → `HomeView`，「我的 Agent」六卡来自 `GET /agents/catalog`（**不是前端硬编码数组**）；前端仅消费接口、无 MUI/Tailwind。状态：**作者自查**（前端未在真服务上渲染复验）。

### 5.1 语义澄清（「像 bug 其实不是」——本轮读盘实测）

1. **`/metrics/slo` 的 `degrade` 是「策略标签」不是「当前状态」**。
   `observability/slo.py` 里 `degrade` 来自档位常量 `SLO_TIERS[tier]["degrade"]`（取值 `none` / `keyword_search` / `disable_noncritical`），表示**该档一旦破线“将会执行什么降级动作”**；当前是否真的破线看 **`breaching`** 字段（空窗口下 `has_data=false`、`breaching=false`）。亲跑实测三档 `degrade` 恒为这三个策略名，**不是**“当前正在降级”。归属：`SloTier.to_dict()`。
2. **`viewer` 读 `GET /cost/board` 得 200 是「符合设计」**。
   `/cost` 前缀登记为 `("read","metrics")`（`rbac/policies.py:99`），而 `viewer` 角色**本就持有 `read:metrics`**（`rbac/policies.py:50`）。成本看板是**只读度量**，`viewer` 可读属预期；**不是**越权漏洞。

---

## 6. ⚠️ 落盘≠交付清单（本轮读盘实证；文件/配置在，但行为未接线或未通电）

> **先给结论：本节不是「缺陷流水账」，而是一个「检出方法族」。** 清单里所有条目是**同一族问题的三个变体**——**「存在的证据」与「行为被证明」脱钩**：①–④/⑥ 是「**有文件/单测、无生产调用方**」（§6.1–§6.4）；§6.5 是「**有测试、却把被测对象 mock 掉**」；§6.6 是「**有配置字段、无消费者**」。共同点一句话：**看得见的东西在，但没有任何路径能证明它在跑。**
> 以下 **①–④、⑥ 是「落盘≠交付」**：**文件在、单测在、但生产调用链断着**。已由主理人逐条复核，并**逐条派工**。
> 措辞约定：**仍断链者**标「**已识别 · 修复在飞（WS-A）**」（**不代表已完成**）；**① 与 ③ 已由主理人亲验**，标「**已验证（主理人亲验）**」，但仍留在册以备冻结复核。
> ⑤ 为文档债（**非**本族问题），低优先。逐条证据见 §6.1–§6.6。

| # | 能力 | 有文件 | 有单测 | 缺什么（生产调用方 / 数据生产者 / 实现体） | 任务号 |
|---|---|---|---|---|---|
| ① | 全离线启动（memory 档免 PG） | `forgeflow/api/main.py`(lifespan)、`forgeflow/database.py`(`init_pool`) | `tests/integration/test_api.py`、`test_memory_offline_search.py`、`test_runs_sse.py`、`tests/unit/test_enterprise_hardening.py` —— **均以 `patch("forgeflow.api.main.init_pool")` 绕过**（4 文件 / 5 处，**详见 §6.5**） | ~~缺「memory 后端短路」的生产调用链~~ → **已修**：`main.py:73-79` memory 档 `pool=None`、跳过 `init_pool()`。**⚠️ 回归护栏（真 lifespan 测试）在飞** | **已验证（主理人亲验）** · Task #6 |
| ② | 进化建议 → 待审 Approval（架构 §7.5） | `forgeflow/skills/evolution.py`（`advise()` **已接** / `submit_advice_for_approval()` **未接**）、`forgeflow/api/routers/skills.py` | `tests/unit/test_skill_evolution.py`、`test_evolution_api.py` | 缺**生产调用方**：全仓无任何路由/运行时调用 `submit_advice_for_approval` | **修复在飞（WS-A）** · Task #7 |
| ③ | 成本台账（企业省钱） | `forgeflow/cost/{budget_service,degrade,savings,ledger}.py`、`forgeflow/api/routers/cost.py`(`_period_totals`)、`forgeflow/observability/cost_tracker.py`、`forgeflow/observability/metrics_source.py` | `tests/unit/test_cost_api.py`、`test_cost_degrade.py`、`test_cost_savings.py`、`test_cost_producer.py` | ~~缺**成本数据生产者**：`_period_totals` 原硬编码 `(None, 0.0)`~~ → **已接通**：`_period_totals` 委托 `cost/ledger.py`；`calculate_cost` 未知模型计 `0.0` | **已验证（主理人亲验）** · Task #2 |
| ④ | C1 多模型降级链 | `forgeflow/config.py`(`model_fallback_chain`)、`forgeflow/models/provider.py`(`get_model()`，**无降级循环**) | `tests/unit/test_config_inc2.py`（**仅断言解析**） | 缺**实现体**：`get_model()` 只按 `llm_provider` 单点分派，无 fallback 循环 | **修复在飞（WS-A）** · Task #8 |
| ⑤ | 在线文档/注释不同步 | `forgeflow/api/routers/memory.py:300`（陈旧 `# TODO(INC2-08)`）、审计环形缓冲归属描述（实为 `forgeflow/api/routers/audit.py`） | — | 文档债（**非**本族问题） | 低优先，暂不改码 |
| ⑥ | Ollama「思考模式」旋钮（`ollama_think`） | `forgeflow/config.py:525`（字段）、`forgeflow/models/provider.py:144-173`（`_build_ollama`） | `tests/unit/test_config_inc2.py`（**仅断言字段解析**） | 缺**消费者**：`_build_ollama` **硬编码 `reasoning=False`**（`provider.py:166`），从不读 `settings.ollama_think` → 旋钮空转（**详见 §6.6**） | **修复在飞（WS-A）** |

> 关键点：**②④⑥** 的共同模式是「**有文件/配置 + 有单测 + 无生产消费方**」——单测调用的是**函数/字段本身**，而**没有任何生产路径会走到它**（①③ 曾属同一模式，**已亲验接通/已修**）。这正是用户要求「确保项目结构」时最需要看见的真相：**看起来有了，实际没通电。**（§6.5 补充第二种：**测试把被测对象 mock 掉**，连“没通电”都测不出来；§6.6 补充第三种：**配置字段无消费者**。）

### 6.1 ✅ 「memory 档免 PG 启动」已修（**已验证 · 已加回归护栏** · Task #6；后续：#9 离线认证亦已交付）

- **原缺陷（本轮曾亲跑复现）**：旧 lifespan **无条件** `await init_pool()`（旧 `main.py:68`）→ `POSTGRES_URL` 不可达即 `LIFESPAN_FAIL ... ConnectionRefusedError`。机理：`init_pool()` 未被 `STORAGE_BACKEND` 分支包裹。
- **修复现状（本轮读盘，与主理人判断一致）**：`forgeflow/api/main.py` lifespan 现按后端分支——
  ```python
  offline = _startup_settings.storage_backend.lower() == "memory"
  if offline:
      app.state.pool = None          # 跳过 init_pool()
  else:
      app.state.pool = await init_pool()
  ```
  **memory 档不再建 PG 池**；`pool=None` 是一等状态（各读取点以 `getattr(app.state, "pool", None)` 兜底）。**故「离线档无需可达 PG 即可启动」成立。**
- **✅ 回归护栏已落地（原「在飞 · WS-A」已闭合）**：`tests/integration/test_offline_boot.py` **不 patch `init_pool`**，跑**真 lifespan**，并用**死端口 PG DSN** 做阳性/阴性对照 —— memory 档 → 200 且 `pool is None`；postgres 档 → `ConnectionRefusedError`。这是 §6.5 那条判据的正面回应：**不再 mock 被测者**。
- **✅ #9 离线认证已交付（原「本文不宣称离线可登录」已作废）**：离线档过去只是「**能起来**」，但 `POST /auth/login` 直连 `get_pool()` → **503，根本登不进**，SPA 卡在登录页。现按项目既有「Repository 抽象 + 双实现」手法（同 `graph/checkpointer.py` 选 `AsyncPostgresSaver` / `InMemorySaver`）**换后端而不分叉逻辑**——详见 **§6.8**。
- **⚠️ 隐藏成因（见 §6.5）**：旧用例之所以「全绿」，是因为它们用 `patch("forgeflow.api.main.init_pool", AsyncMock)` **把唯一会崩的调用整个 mock 掉**——即**测试把被测对象自身替换了**。「全绿」与「真能离线起」**本就可以同时不同真**。
- **详见 07-RUNBOOK-INC2.md 档①。**

### 6.2 ✅ Cost Optimization（企业省钱）接口 + 账本已接通（**已验证（主理人亲验）** · Task #2）

- **证据（亲跑 + 读盘）**：WS-B 落地**前**，真服务 `GET /cost/savings` 返回 `{"has_data":false,"amount":null,...}`、`GET /cost/board` 返回 `{"total_spent":0.0,"budgets":[]}`；源码 `api/routers/cost.py::_period_totals()` **原硬编码返回 `(None, 0.0)`**（注释自陈「当前**没有**任何持久化的租户级成本账本」），`observability/metrics_source.py::MemoryMetricsSource` 亦 `has_cost=False`。
- **闭合现状（本轮读盘 + 主理人亲验）**：`_period_totals()` 已**委托 `forgeflow.cost.ledger.period_totals`**（`cost/ledger.py`：memory 从 hub run store、PG 从 `workflow_runs` 读成本），异常时仍降级 `(None, 0.0)`（never-500）；`observability/cost_tracker.py::calculate_cost`（`:74`）对**未知模型计 `0.0` + WARNING**（`宁少算不虚增`，消除“免费 ollama 按云价计费”）；新增 `tests/unit/test_cost_producer.py`。
- **机理**：`cost/` 模块（`budget_service/degrade/savings/models`）、`cost_budgets` 表、`/cost/*` 端点都真实存在且逻辑正确；**此前上游无任何成本写入方**，省钱/预算看板曾**永远诚实空值**——**现写入方已接**。
- **结论**：用户诉求 **#11「企业省钱 → Cost Optimization」** 曾为「**接口已通电、数据未通电**」（诚实空值，未伪造 0）；**现：已验证（主理人亲验）· Task #2**。（仍保留 §6.2 痕迹以备冻结复核。）

### 6.3 ✅ Skill Evolution 建议 → 审批 **已接线**（原 Task #7「修复在飞」已闭合）

- **原缺陷（读盘 + grep）**：`skills/evolution.py : submit_advice_for_approval()` **有实现、有单测**（`tests/unit/test_skill_evolution.py:172`），但**生产代码零调用方**——`grep` 全仓仅命中定义与测试。`GET /skills/evolution-advice`（`api/routers/skills.py:104`）**无副作用**，只返回 `approval_required:true`，**并不创建 Approval**。
- **现状（本轮读盘）**：新增 `POST /skills/evolution-advice/{skill_id}/approval`（`api/routers/skills.py:128`，`status_code=201`），内部经 `advise()` 匹配到具体建议后调用 `submit_advice_for_approval(tenant, matched, requester=...)`（`:147`、`:153`）→ **落入 `agent_approvals` 审批队列**。这也解释了 §8 记录的路由数 **75→76**。
- **结论**：架构 §7.5 的「生成建议后**自动创建 Approval 待审**」两段（函数 + 端点）**均已通电** → **已交付**。
- （附）`agent_approvals.kind` 列、`EvolutionAdvice.approval_kind` 均已就位，**缺的只是 API 层那次调用**。

### 6.4 其余「配置在、行为未接线」（补充点名，均 grep 实证；C1 = **修复在飞（WS-A）** · Task #8）

| 项 | 落盘位置 | 状况 |
|---|---|---|
| `settings.model_fallback_chain`（C1 多模型降级路由） | `config.py:517` + `models/provider.py::_fallback_candidates`(`:196`) / `get_model`(`:271`) | **✅ 已闭合**：`get_model()` 现已按 `[llm_provider, *model_fallback_chain]` 构造候选链并**逐个尝试**，链耗尽在 **生产环境抛 `ModelUnavailableError`**（非可达到 `mock`）。默认值亦由含 `mock` 改为 `["ollama"]` —— **Mock 降级为「环境受控的开发兜底」，不再是生产兜底**（对应用户 §用户 pkt 2）。 |
| `settings.ollama_think` | `config.py:525` + `provider.py:399` | **✅ 已闭合**：`ChatOllama(..., reasoning=settings.ollama_think)` —— 旋钮**有消费者**了；且 `validate_runtime()` 对 **prod + `ollama_think=true`** 判 **fatal**（与 docstring「MUST stay false」对齐）。另见 **§6.6**。 |
| `POST /memory/{memory_id}/promote` 的登记注释 | `api/routers/memory.py:300` `# TODO(INC2-08): register …` | **陈旧注释**：该路由**实际已登记**（经前缀 `/memory` → `("write","memory")`，真服务 promote 200）。注释与代码不同步，属文档债。 |
| 审计“内存环形缓冲”的落点 | 旧文档写 `middleware/audit.py` | **实际在 `api/routers/audit.py`**（`record_audit_entry/_memory_filter/clear_audit_ring`）。功能对、归属描述需更正。 |

### 6.5 ⚠️ 测试把「被测对象」mock 掉 → 全绿 ≠ 功能可用（Task #6 的隐藏成因）

> 这是**第二类失效模式**（区别于 §6 表把「文件/配置在、调用链断」列为「落盘≠交付」）：**用例存在、且全绿，却把要验证的那个失败点从被测路径里删掉了**。

- **现象**：`tests/` 里「离线 / 不依赖 PG」的用例**全部绿色**，但它们**并不能证明**服务真能在无 PG 时启动——因为它们把**唯一会因无 PG 而崩掉的那次调用**替换成了 `AsyncMock`。
- **证据（本轮 grep 亲查）**：`patch("forgeflow.api.main.init_pool", new_callable=AsyncMock)` 命中 **4 个文件 / 5 处**：

  | 文件 | 行 |
  |---|---|
  | `tests/integration/test_api.py` | `:14`、`:49` |
  | `tests/integration/test_memory_offline_search.py` | `:32` |
  | `tests/integration/test_runs_sse.py` | `:14` |
  | `tests/unit/test_enterprise_hardening.py` | `:115` |

  它们统一形如：
  ```python
  with patch("forgeflow.api.main.init_pool", new_callable=AsyncMock) as mock_pool, ...:
      ...  # 进入 lifespan / 打接口
  ```
  即：**测试替身把 `forgeflow.api.main.init_pool`（§6.1 里唯一会 `ConnectionRefusedError` 的执行点）整个换成了假的**。于是「memory 档能起」在测试里**永远为真**，**与真实启动无关**。
- **为什么这是失效模式**：`init_pool` 正是 §6.1 中「无条件连 PG、连不上就崩」的那一行。把**被测对象本身**（`main.init_pool`）mock 掉，等于**把要验证的那个失败点从被测路径里删除**——绿的是「被替换后的假实现」，不是产品代码。**658 用例全绿 与 §6.1 的离线启动缺陷，可以同时为真。**
- **通用识别方法（可复用，非本仓专属）**：
  1. `grep -rn 'patch("<module>.<entry>"' tests/`，把 `<module>.<entry>` 换成**被测功能的入口符号**（本例 `forgeflow.api.main.init_pool`；也可能是 `app.state.pool`、目标 service 方法）。
  2. 命中即警觉，问一句：**测试替换掉的是不是「被测对象自身」？**
  3. 判据一句话：**若被 mock 的东西一坏、该功能就会坏 → 这个测试证明不了那个功能**。mock 的是**协作者**（DB / 网络 / 计时器）才合格；mock 的是**被测者**则**无效**。

**⚠️ 防误用（本判据只打该打的，别扩大化）**：mock **协作者**（外部 HTTP / LLM / DB / 计时器）是**合格的、甚至必要的**；**只有** mock **被测者本身**（入口函数、被断言行为的载体）才使测试失效。**不得**据此把「测试里有 mock」一律判为不可信——那会误伤大量**正当**的依赖替身用例。本条的靶心只有一个：**测试替换掉了它正要证明的那个失败点**。

- **结论**：Task #6 落地**前**，「离线可用」在本轮**从未被真正证明**过（真服务实测见 §6.1：无可达 PG 即启动失败）——这正是为什么 §6 表 ① 的「有单测」**不等于**「已交付」。**现状**：#6 已修（§6.1，已验证），但**能防回退的真 lifespan 测试仍在飞（WS-A）**——**§6.5 记载的模式不因修复而消失**，它一直提醒：**绿 ≠ 行为被证明**。

### 6.6 ⚠️ `ollama_think`：配置字段无消费者（第 6 个「落盘≠交付」实例；**修复在飞（WS-A）**）

> 这是同族的**第三个变体**：**「有配置字段、无消费者」**——**存在的证据（字段）在，行为被证明（读取它的执行点）不在**。

- **证据（本轮读盘；software-engineer-2 发现，主理人独立核实，架构师再行确认）**：
  - `forgeflow/config.py:525` 定义 `ollama_think: bool = Field(False, description="… MUST stay false: qwen3 thinking models can consume the whole num_predict budget and return an empty string.")`。
  - `forgeflow/models/provider.py:144-173` 的 `_build_ollama()` **硬编码 `reasoning=False`**（`provider.py:166`，`ChatOllama(..., reasoning=False)`），**全仓无任何代码读取 `settings.ollama_think`**。
- **为什么算缺陷**：一个自称「MUST stay false」的安全相关开关，却**没有任何执行点消费它**——既不能真的打开思考模式，也无法在执行期对它校验/告警。**旋钮是空转的。**
- **归属 / 修复方向（已派 · 修复在飞（WS-A））**，三选一：
  1. **接线**：`_build_ollama` 改用 `reasoning=settings.ollama_think`（让开关真起作用）；
  2. **删字段**：承认无用、移除 `config.py:525`；
  3. **加护栏**：在 `validate_runtime()` 对 **prod 下 `ollama_think=true` 告警**（与其 docstring「MUST stay false」对齐）——与 `config.py:637` 对 `dev_login_enabled` 的 **fail-closed 同精神**。
- **在此之前，现状以「空转旋钮」为准。**

### 6.7 ✅ ⑦ Agent 执行主链路**真的用了配置的 LLM**（INC4 §A · 用户最高优先级）

> 这是本轮**最重的一个缺口**，且属于 §6 同族的第四种变体：**「链条上游有 Provider、有 Monitoring，但真正消费它的那一步被写死了」**。

- **原缺陷（读盘确认）**：`forgeflow/runtime/orchestrator.py::_default_executor` 是**硬编码平台图**——直接遍历常量 `_DEFAULT_STEPS = [research.search → data.query → code.run]`，**全程零 LLM 调用**，既不规划也不反思。也就是说：**配置了 Ollama，Agent 主链路一次都没用它。**
- **修复后的架构（对齐用户给定的目标图）**：

  ```
  Agent → Provider ┬→ Ollama（本地 qwen2.5vl:3b / qwen3:8b …）→ 真 LLM
                   └→ Mock（**仅**开发/测试；生产永不作为兜底）
       ↓ Tool Calling / Planning / Memory / Reflection
              ↓ Agent 执行
  ```

  | 环节 | 落点 |
  |---|---|
  | 路径选择 | `orchestrator.resolve_agent_runtime_mode()`（`agent_runtime_mode: auto｜llm｜deterministic`） |
  | LLM 执行器 | `orchestrator._llm_executor()` |
  | Planning / Reflection | `runtime/llm_planner.py :: LLMPlanner.plan()` / `.reflect()` —— **各 1 次批量调用**（遵守 18.6s/次硬约束，**无逐步调用**） |
  | 真实计费 | 计划的 usage 经 `_record_usage()` → `CostTracker` 落账（`RunRecord.total_tokens` / `cost_by_agent` / `/cost/board`） |
  | Mock 定位 | 仅 `agent_runtime_mode=auto 且 llm_provider in ("", "mock")` 时走确定性路径；生产环境下链耗尽抛 `ModelUnavailableError` |

- **主理人亲跑实证（真 Ollama，非 stub）**：`LLM_PROVIDER=ollama` + `AGENT_RUNTIME_MODE=llm`，起**真服务**、走 `POST /tasks` → 轮询到 `completed`：

  | 判据 | 实测 |
  |---|---|
  | 模型身份 | `models = [{slot: strong, llm_type: chat-ollama, model: llama3.1:8b}, {slot: worker, model: qwen2.5vl:3b}]` —— **不是 mock** |
  | 真实用量 | `total_tokens = 644`（plan 351+103 ... 两次调用的真实 usage） |
  | 计划来自 LLM | 产出 `research.search → data.query → analysis.score → report.render`，**≠** 硬编码的 `[research.search, data.query, code.run]`（多出 `analysis.score` / `report.render`，少掉 `code.run`） |
  | 反思由 LLM 判定 | `reflection.source = "llm"`, `success = true`, `score = 0.9` |
  | 无静默降级 | `degraded = None`；且代码内置 `provider_degraded_to_mock` 哨兵——配置真 provider 却构建出 mock 时**发 `run.warning` 事件**，不静默改跑 stub |

- **安全契约未削弱**：LLM 输出被视为**不可信输入**——先按角色权限收敛工具白名单（`_plan_tool_allowlist`），再对每一步走 `check_tool_permission`（RBAC）→ `PolicyEngine.evaluate_tool_call`（HITL），与确定性路径**同序**（单测覆盖：幻觉工具丢弃、越权工具丢弃、高危工具被拦截且不产生副作用）。
- **附带修掉一个同类缺口**：`GET /runs/{id}` 原先**丢掉了** `runtime_mode / total_tokens / total_cost_usd / llm`（SSE 事件里有、REST 详情里没有）→ 消费方无法分辨「真 LLM 运行」与「确定性剧本」。现已补齐（`api/hub_schemas.py::RunDetailResponse`），并由 `tests/unit/test_runtime_llm_agent.py` 两条用例（阳性 + 阴性对照）钉死。

### 6.8 ✅ ⑨ 离线认证：memory 档**真的能登录**（INC4 T9 · 用户拍板）

- **原缺陷**：`POST /auth/login` 直连 `api.dependencies.get_pool()`，无 PG 池即 **503**。而离线档**故意**以 `pool = None` 启动（§6.1），结果是「**能起来，但登不进去**」——SPA 卡在登录页，离线故事止步于门口。
- **修法：换后端，不分叉逻辑**（复用项目既有的 Repository 抽象 + 双实现手法，与 `graph/checkpointer.py` 选 `AsyncPostgresSaver` / `InMemorySaver` 同构）：

  | 层 | 落点 |
  |---|---|
  | 统一门面 | `auth/store.py :: get_auth_store()` —— 有池→`PgAuthStore`；离线档→`MemoryAuthAdapter`；都不满足→**503（绝不静默降级）** |
  | 离线实现 | `auth/memory_store.py :: MemoryAuthStore` —— **同款 Argon2id**、**同款 SHA-256 存 token**、**同款轮换 + 复用检测（reuse detection 烧掉整族）** |
  | 单一名册 | `auth/demo_users.py :: DEMO_USERS` —— PG 与内存**共用同一份**，防止两档漂移出不同账号集 |
  | 路由 | `api/routers/auth.py` 六个端点（login / refresh / logout / mfa_enroll / mfa_verify / oidc_exchange）全部改消费 `store`，**无 `if offline:` 分支** |

- **fail-closed 边界（另起-test 钉死）**：内存凭证存储**仅**在 `storage_backend == "memory"` **且**非生产形态（`Settings.is_production()`）时可达；生产 + 内存 ⇒ **503，绝不退化成进程内凭证库**。离线的工作区声明（workspace claim）**只在等于用户自身默认工作区时才被接受**——否则拒绝，不在离线档重开 SECURITY_AUDIT 的 C-4。
- **主理人亲跑实证**：**真 lifespan** 起服务 + `STORAGE_BACKEND=memory` + **死端口 PG DSN** → `app.state.pool = None` → `admin / manager-1 / rep-1 / viewer-1` **全部 200 登录**，拿到的 token 可访问 `/skills`、`/cost/board`；`POST /auth/refresh` 轮换成功，**重放旧 refresh → 401**（复用检测生效）。> 注：探针里第 4 个角色返回 429，来自**既有的全局限流中间件** `middleware/rate_limit.py`（与本改动无关）。
- **回归护栏**：`tests/unit/test_offline_auth.py`（**16 条**）——含「memory 登录 200 而不是 503」「错误口令 401」「生产形态禁止内存认证」「无池 + postgres 后端 ⇒ 503」「工作区声明拒绝」「refresh 复用检测」等。

---

## 7. 验证状态标注（诚实边界）

| 类别 | 内容 | 验证状态 |
|---|---|---|
| **读盘验证（符号级）** | §2/§3 全部「文件 : 符号」 | ✅ 已用 Glob/Grep/Read 确认文件存在且类/函数/`__all__`/装饰器真实存在 |
| **数值实测** | 76 路由 / 33 登记 / UNMAPPED=0 / 6 公开 / 39 表 / alembic=011 / pgvector / Ollama 模型表 | ✅ 本次亲跑（**UNMAPPED 判据 + 门禁 6 passed 见 §1**） |
| **端到端行为** | `/health`、`/tasks`→`/runs`→`/experiences`、`/runs/{id}/events`(SSE)、`/skills/evolution-advice`、`/marketplace/skills/publish`、`/memory/store+promote`、`/audit/export`、`/metrics/slo`、`/cost/*` | ✅ 本次**起真服务**（memory+mock）实测 |
| **INC4 新增 e2e（主理人亲跑）** | **真 Ollama**（`qwen2.5vl:3b` + `llama3.1:8b`）驱动 `POST /tasks` 全流程：mode=llm / 644 真实 tokens / LLM 计划 ≠ 硬编码剧本 / LLM 反思 success=true；离线档（memory + 死端口 PG）真 lifespan 下四角色登录 + refresh 轮换 + 重放 401 | ✅ 见 **§6.7 / §6.8** |
| **回归基线** | 双档全量：**memory 716 passed / 0 failed**；**postgres 716 passed / 0 failed**（`STORAGE_BACKEND` 分别为 `memory` / `postgres`，同一份代码） | ✅ 本次亲跑（含 §6.8 的 16 条离线认证 + §6.7 的 2 条详情可见性护栏） |
| **未验证（行为级）** | ① LLM/视觉真模型（Ollama → 结构化输出、`qwen2.5vl:3b` 图片描述）的成功率与延迟；② 真 PG 档下的全接口回归（本轮 live 服务跑的是 memory 档）；③ LangGraph 内建 pipeline（sales_ops/support_ops/finance_recon）的**业务正确性**；④ 前端页面渲染与 SSE 断线降级；⑤ 跨租户隔离/DLP 的对抗性测试 | ⚠️ **未验证** |
| **能力级「已落位」判定** | §2 中 ✅ 者 | 依据：文件 + 关键符号存在 +（多数）真服务可达。**功能级正确性仍以 QA 独立复验报告为准**。 |

> §6 的**「落盘≠交付」实例（①–④、⑥）** 是**读盘 + 真跑 / grep** 双重确认的**否定性结论**，可信度高于普通「已落位」判定。当前状态：**①（离线启动 · Task #6）与 ③（Cost · Task #2）已由主理人亲验**（① 的**回归测试在飞 · WS-A**）；**② Task #7 / ④ Task #8 / ⑥ `ollama_think`** 为「**已识别 · 修复在飞（WS-A）**」；⑤ 为文档债（非本族）。
> **§6.5 + §6.6** 是同族问题的另两个变体：**测试 mock 掉被测者**（§6.5）、**配置字段无消费者**（§6.6）——三者共同点是「**存在的证据**」与「**行为被证明**」脱钩。

---

## 8. 结论

- **框架结构满足用户要求**：六层 + 数据/接口层全部落位，能力点均能找到**真实代码归属**；路由/权限为 **76 / 33 / UNMAPPED=0**（历史上那 3 个 marketplace 真缺陷**已修复**）。
- **无「层缺失/层错位」**；`§4` 的差异均为**合理细化**。
- **「落盘≠交付」共 5 例（①–④、⑥）+ 1 文档债（⑤）——全部已收口**：① 离线启动（**含真 lifespan 回归护栏**）｜② Skill Evolution → 审批｜③ Cost 账本｜④ C1 降级链（**Mock 降为「环境受控的开发兜底」，生产不再可达**）｜⑥ `ollama_think` 旋钮**已有消费者**；⑤ 文档债未改码。§6.5（测试 mock 被测者）、§6.6（配置无消费者）作为同族方法论留档。
- **另 🦴 新识别并修复 2 例**：**⑦ Agent 执行主链路从不调用 LLM**（硬编码 `_DEFAULT_STEPS` 剧本）→ 已按用户目标图接通真 Ollama（**§6.7**）；**⑨ 离线档登不进**（`/auth/login` 直连 `get_pool` → 503）→ 已按 Repository 双实现换后端解决（**§6.8**）。这三者（甚至包括本次「详情接口丢掉 token/模式」的子缺口）是**同一个失效模式的四个切面**：**链路建了、度量有了，但消费方或可见性断了**。
- **未替自己或团队圆场**：本节每条结论均由「读盘 + 真跑」得出；本轮唯一一次 CI 失败（`test_offline_default_is_still_deterministic`）是**我的改动破坏了事件契约**（把「无 LLM 溯源」从 `None` 改成 `{}`）——**改的是代码不是测试**，详见 §6.7 附注。
- **本版与上一版的关键修正**：路由 **71→75→76**（#7 新增 `POST /skills/evolution-advice/{skill_id}/approval`）、登记 **30→33**、**UNMAPPED 9→0**（marketplace 三端点已修）；「无 PG 可跑」此前被误述——经 **#6 修复后 memory 档免 PG 启动已成立**（历史误述与证据见 §6.1）。
- **INC4 数字基线（供后续 diff）**：双档全量 **716 passed / 0 failed**（memory 与 postgres 同为 716）；较 INC3 基线 698 净增 18 条（16 条离线认证 + 2 条运行时详情可见性）。新增 commit：`8928d2d`（thinking 守卫）· `8439ee9`（生产护栏 + `agent_runtime_mode`）· `3d087a6`（离线启动护栏）· `4e5c19c`（离线认证）· `6e1418d`（LLM 运行时 + KPI/Cost）· `6a0879c`（运行详情可见性 + 文档刷新）。
- **仍未验证（行为级，不在本轮承诺内）**：① 真 Ollama 的**成功率/长尾延迟**（本轮只跑了 2 次成功样本）；② 真 PG 档下的**全接口**回归（本轮 e2e 跑的是 memory 档）；③ LangGraph 三条内置 pipeline 的**业务正确性**；④ 前端页面渲染与 SSE 断线降级；⑤ 跨租户隔离 / DLP 的**对抗性**测试。
