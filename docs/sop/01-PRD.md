# AgentFlow · Enterprise AI Workforce OS — 产品需求文档（简单 PRD）

> 版本：v1.0 ｜ 阶段：改造立项 ｜ 负责人：许清楚（产品经理）
> 上游：ForgeFlow-main（Apache-2.0，FastAPI + LangGraph + MCP + PostgreSQL/pgvector + React 19/Vite）

---

## 0. 项目信息

| 项 | 内容 |
|---|---|
| Language | 中文 |
| Programming Language | 后端：Python 3.13（FastAPI + LangGraph + MCP + PostgreSQL/pgvector）；前端：React 19 + Vite + MUI + Tailwind CSS；运行时：Docker（可选）+ 本机 venv |
| Project Name | `agentflow`（改造自 `ForgeFlow-main`） |
| 代号 | AgentFlow · Enterprise AI Workforce OS |
| 原始需求复述 | 把已开源的成熟多智能体编排器 ForgeFlow，改造为「企业级 Multi-Agent 智能工作与技能资产平台」。以 **Skill Hub / Memory Hub / Security Hub** 三大 Hub 为底座，新增 **Agent Runtime → MCP Tool Layer → Validation Layer** 运行时链路，形成「任务 → 经验 → Skill」核心闭环；按新设计稿重做首页；满足用户 20 条模块映射与 11 条架构评审硬性意见；符合本机（Windows 11 / 15.2GB RAM / 无 Ollama / 端口受限）的运行约束。 |

### 现状资产盘点（改造基线，非从零建设）

| 层 | 现有实现（可复用） | 缺口（需新建/改造） |
|---|---|---|
| 编排 | `graph/`(LangGraph StateGraph + PG checkpointer)、`agents/`(supervisor/researcher/analyzer/executor)、`state/` | 无「经验」实体；无 Replan 回环治理；Runtime 无 Durable 语义 |
| 工具 | `mcp/`(server 14 tool router + client adapter)、`connectors/`(8 企业连接器) | 工具调用未纳入 Policy/预算约束 |
| 记忆 | `memory/`(pgvector 语义库 + 关系库) | 无分层（Team/User/Episodic/Semantic/Org 五层未拆分）；与「经验」无关联 |
| 治理 | `security/`(pii_redactor/prompt_guard/ssrf_guard/tool_output_guard)、`rbac/`、`middleware/`(auth/audit/rate_limit/security_headers) | 无独立 Policy Engine；无 ABAC；无 DLP；无 Tenant Isolation |
| 可靠 | `resilience/`(retry/circuit_breaker/budget_guard)、`jobs/`(escalation) | 无 SLO 分级与降级策略 |
| 观测 | `observability/`(cost_tracker/metrics_store/prometheus/tracing_provider) | 无成本预算感知调度；首页无 KPI 呈现 |
| 评估 | `evaluation/`(judge/metrics/dataset/regression/runner) | 未与 Skill 版本绑定形成「评估→演进」闭环 |
| 技能 | `marketplace/`(文件式模板注册表) | 无 Skill Registry / Versioning / Evolution / Evaluation |
| 门户 | `frontend/src`（18 views + 8 css） | 首页未按设计稿重做；无 Skill Hub / Memory Hub / Security Hub 页面 |
| 人机 | `api/routers/approvals`、`jobs/escalation` | 未形成统一 HITL 分级策略 |

---

## 1. 产品目标

**一句话定位**：把企业里"会做事的人"的经验，沉淀为"可执行、可治理、可进化"的 AI 技能资产，让 AI 员工队伍安全、可控、持续变强。

### 可衡量目标

| 编号 | 目标 | 衡量指标（可验收） |
|---|---|---|
| G1 | **闭环可用**：跑通「任务 → 经验 → Skill」并可在 UI 全程可视化 | 完成 1 个端到端演示：一次成功任务 + 一次失败任务，均能产出 `Experience` 记录；失败任务经 Replan 后产出 Skill 候选；全链路可在首页「智能执行日志」与 Skill Hub 页回放 |
| G2 | **可信可控**：低风险自动化、高风险 HITL，分层分级 | 所有 P0 工具调用均经 Policy Engine 决策；高风险操作 100% 触发 HITL 审批；越权/跨租户/dangerous tool 调用拦截率 100%（用测试用例验证） |
| G3 | **可沉淀可复用**：Skill 版本化、可评估、受治理演进 | 至少 4 个内置 Skill 具备 ≥1 个版本记录；Skill 演进必须经 Evaluation 通过 + 治理审批方可发布；Marketplace 可检索与安装 |

---

## 2. 目标用户与角色

| 角色 | 典型画像 | 核心诉求 | 关键页面 |
|---|---|---|---|
| **企业 AI 平台运营者** | 建设并运营平台的团队 | 平台可托底（SLO/降级）、成本可控、多租户安全、首页一屏看全局 | 首页、运维监控、系统设置 |
| **业务团队（业务方/一线）** | 销售/客服/财务等业务人员 | 用自然语言发起任务、过程可实时看到（SSE）、结果可信、复用团队技能 | 首页、智能任务、技能中心、知识库 |
| **平台工程师** | 后端/前端/MCP 开发者 | 通过 MCP 接入企业系统、可视化 Agent 编排、封装与调试技能 | Agent 工作台、技能中心、数据分析 |
| **安全合规官** | 风控/合规/审计 | 租户隔离、DLP 防泄露、权限最小化、全量审计、可出具证据 | 安全与权限、知识库、运维监控 |
| **平台管理员** | 系统管理员 | 用户/角色/成员管理、策略配置、审计导出 | 系统设置、安全与权限 |

---

## 3. 核心闭环定义：「任务 → 经验 → Skill」

### 3.1 缺口诊断（补上"缺失的一环"）

用户指出当前设计「缺了把经验固化成 Skill 中间的那一环」。经分析，缺口有两处：

1. **缺「经验」实体本身**：现有系统只有 `Memory`（记忆），没有独立的 `Experience`（经验）。**「经验」= 一次任务执行的可复用知识总结（做了什么、结果如何、为什么成功/失败、下次该怎么做）**；**「记忆」= 原始事实与上下文的存储（谁在何时说了什么、检索到的原始片段）**。二者分离但关联：`Experience` 由多次 `Memory` 聚合、抽象而来，且 `Experience` 引用其来源 `Memory`。
2. **缺「经验 → Skill」的固化环节**：从"经验"到"可复用 Skill"之间缺一个**候选生成 + 审核固化的转化层**。产品定义该环节为 **Skill Candidate Compiler（技能候选编译器）+ Skill Governance Gate（技能治理闸门）**，即闭环里的 `②→③` 跳。

### 3.2 闭环每一跳定义

| 跳 | 名称 | 输入 | 输出（产物） | 触发条件 | 存储位置 |
|---|---|---|---|---|---|
| ① | 任务执行 | 用户任务描述 + 上下文 | `Task` + `Run` 执行轨迹 + Tool 调用记录 | 用户提交任务 / 定时触发 / 事件触发 | PG `tasks`, `runs`, `run_steps`；热轨迹 Redis |
| ② | 经验提取（Experience Extraction） | Run 轨迹 + 工具的输入/输出 + 评估结论 | **`Experience`（经验记录）**：摘要、关键决策、成败归因、可复用步骤 | 任务**终态达成**（成功/失败/中止均触发）；由 Validation Layer 判定 | PG `experiences`，向量索引入 `semantic memory`（pgvector） |
| ③ | 技能候选编译 **（缺失的一环·补）** | ≥N 条相似 `Experience`（默认 N=3，可配）+ 人工"标记为最佳实践" | **`SkillCandidate`**：提炼出的可复用技能雏形（prompt/步骤/工具集/I-O schema/适用条件） | 相似经验聚类达到阈值，或用户主动"固化为技能" | PG `skill_candidates` |
| ④ | 治理评估（Governance & Evaluation） | `SkillCandidate` | 评估报告 + 治理决策（通过/驳回/需修改） | 候选生成即进入评估队列 | PG `skill_evaluations` + `approvals` |
| ⑤ | 技能固化与版本化（SkillOps） | 通过治理的候选 | **`Skill` + `SkillVersion`**（版本化、可回滚、带 owner） | 治理闸门通过 | PG `skills`, `skill_versions`；Registry 注册 |
| ⑥ | 发布与共享 | 新版本 Skill | 上架 **Skill Marketplace**，可被其他团队/Agent 安装复用 | owner 点击发布 | `marketplace` 注册表 + PG `skill_listings` |
| ⑦ | 运行期复用与再进化 | 已发布 Skill + 新任务 | 新的 Run（复用 Skill）+ 新 `Experience` → 回到 ② | Agent Runtime 选中该 Skill | 回到 ①/② |

> **闭环成功判据**：一条 `Experience` 可在 UI 上追溯到其来源 `Run` 与 `Memory`；一个 `Skill` 可追溯到其来源 `Experience` 与评估报告；一个 `SkillVersion` 可回滚到上一版本。

### 3.3 数据模型（核心实体与关联）

| 实体 | 关键字段（示意） | 关联 |
|---|---|---|
| `Tenant` | id, name, plan, isolation_level | 1:N → User/Skill/Memory/Experience |
| `Task` | id, tenant_id, title, intent, status, created_by | 1:N → Run |
| `Run` | id, task_id, agent_id, status, cost, tokens, started_at, ended_at | 1:N → RunStep；1:1 → Experience |
| `RunStep` | id, run_id, step_type, tool, input, output, latency | N:1 → Run |
| `Experience` | id, tenant_id, team_id, run_id, summary, decisions, outcome(成功/失败), reusable_steps, tags, embedding | N:1 → Run；N:M → Memory（来源）；1:N → SkillCandidate |
| `Memory` | id, tenant_id, scope(team/user/episodic/semantic/org), content, embedding, source | N:M → Experience |
| `SkillCandidate` | id, tenant_id, experience_ids[], draft_spec(prompt/steps/tools/io_schema), similarity_score, status | N:M → Experience；1:1 → SkillEvaluation；0:1 → Skill |
| `Skill` | id, tenant_id, name, domain, owner, current_version, status(草稿/评估中/已发布/已下架) | 1:N → SkillVersion |
| `SkillVersion` | id, skill_id, semver, spec, changelog, eval_score, approved_by, created_at | N:1 → Skill；来源 → Experience |
| `SkillEvaluation` | id, candidate_or_version_id, dataset, metrics, verdict | N:1 → SkillCandidate/SkillVersion |
| `Policy` | id, tenant_id, subject, resource, action, condition(ABAC), effect | 用于 Policy Engine 决策 |
| `Approval`（HITL） | id, run_id/step_id, risk_level, requested_action, approver, decision | N:1 → RunStep |
| `AuditEvent` | id, tenant_id, actor, action, resource, result, ts | 全量审计 |

> 设计原则（落评审意见 1、2、10、11）：**Experience 是一等核心资产，与 Memory 分离但通过 `N:M` 关联**；Skill 强制版本化且演进受治理约束；闭环每一跳都有明确的触发条件与产物落库位置。

---

## 4. 用户故事

> 格式：作为【角色】，我希望【能力】，以便【价值】；并给出验收标准（AC）。覆盖六层架构与 11 条评审意见。

| 编号 | 用户故事 | 覆盖 | 验收标准（AC） |
|---|---|---|---|
| US-01 | 作为**业务人员**，我希望用自然语言输入任务并实时看到 Agent 执行过程，以便产出的结果可信、不是"黑箱"。 | Agent Runtime / SSE / 评审3 | 提交后 1s 内建立 SSE 连接；首页「智能执行日志」按时间线实时追加步骤；断线可重连补发。 |
| US-02 | 作为**业务人员**，我希望任务失败时 Agent 能自动 Replan 重试，以便不用我手动重来。 | Agent Loop / 评审10 | 失败步骤触发 Replan 分支；记录每次 replan 原因；达最大重试次数后转 HITL。 |
| US-03 | 作为**平台工程师**，我希望每次任务完成后系统自动沉淀一条「经验」，以便团队可复用而不重复踩坑。 | Experience / 评审1 | 任务终态后自动生成 `Experience`；可在记忆管理页看到并追溯来源 Run 与 Memory。 |
| US-04 | 作为**平台工程师**，我希望把若干相似经验"固化为技能"，以便经验升级为可执行资产。 | 闭环③ Skill Candidate Compiler / 评审11 | 相似经验≥N 时可一键"固化为技能"生成 `SkillCandidate`，含草稿 spec；展示相似经验列表。 |
| US-05 | 作为**业务方/owner**，我希望技能有版本号且可回滚、要经过评估与审批才能发布，以便技能演进可控不失控。 | Skill Versioning / 评审2 | 每次发布生成新 semver；可查看 changelog 与 diff；可回滚到上一版本；未经评估+审批无法置为"已发布"。 |
| US-06 | 作为**安全合规官**，我希望高风险操作（删库、转账、外发邮件等）必须人工审批，低风险自动放行，以便分级管控。 | HITL / 可信 / 评审3 | Policy Engine 按 `risk_level` 分流；高风险 100% 生成 `Approval` 并阻断直至人工决策；低风险自动执行并留痕。 |
| US-07 | 作为**安全合规官**，我希望不同租户的数据、记忆、技能、经验完全隔离，且敏感数据外发被 DLP 拦截，以便数据不泄露。 | Tenant Isolation + DLP / 评审5 | 任何查询自动注入 `tenant_id` 过滤；跨租户访问返回 403；PII/敏感字段外发被 `pii_redactor`/DLP 拦截并告警，拦截率 100%（用例验证）。 |
| US-08 | 作为**平台管理员**，我希望用 Policy Engine 统一管理 Agent/用户的权限（RBAC+ABAC），以便权限最小化、可审计。 | Policy Engine / RBAC·ABAC / 评审5 | 支持按主体-资源-动作-条件配置策略；命中策略的决策写入审计；越权调用拦截率 100%。 |
| US-09 | 作为**平台运营者**，我希望设置成本预算，超预算时自动降级（换小模型/裁剪上下文/暂停非关键任务），以便企业省钱。 | Cost Optimization / 预算感知 / 评审6 | 可配置租户/团队/任务级预算；达阈值触发降级策略并记录；成本看板展示节省金额与环比。 |
| US-10 | 作为**平台运营者**，我希望为不同能力设定 SLO 分级并看到实时达标率，超阈值自动降级，以便做可论证的可用性承诺。 | SLO 分级 / 降级 / 评审4 | 定义核心/重要/边缘三级 SLO；监控页展示达标率与告警；不达标触发对应降级策略。 |
| US-11 | 作为**平台运营者**，我希望 Team Memory 与 Organizational Memory 分层且边界清晰，以便团队私有经验不污染组织知识、组织知识可下沉复用。 | Memory 分层 / 评审7 | Memory 页按 5 层（Team/User/Episodic/Semantic/Org）分区展示；明确各层写入/读取规则；跨层提升需显式操作。 |
| US-12 | 作为**平台工程师**，我希望"数据/上下文优化"作为平台级能力（自动检索、去重、压缩、择优上下文），以便每个 Agent 都受益而非各自实现。 | Data/Context Optimization / 评审9 | 提供统一 Context Builder 接口；所有 Agent 经其获取上下文；记录上下文压缩率与命中率。 |
| US-13 | 作为**平台工程师**，我希望 Agent 通过 MCP 统一调用 DB/Search/File/Git/企业 API，以便工具接入标准化、可治理。 | MCP Tool Layer | 所有工具经 MCP router 暴露；工具调用纳入 Policy 与预算；首页日志显示工具调用次数。 |
| US-14 | 作为**安全合规官**，我希望明确「可信能力」与「Skill 进化」的扩展逻辑（新技能上线需通过可信基线校验），以便能力扩张不突破安全边界。 | 评审8 / 可信×进化 | Skill 发布前必须通过互不信任校验项（工具白名单、越权检测、PII 检查）；不通过则禁止发布。 |
| US-15 | 作为**业务方**，我希望在 Marketplace 里检索/安装团队沉淀的技能，以便跨团队复用、不重复建设。 | Skill Marketplace | 支持关键词检索与领域过滤；一键安装到本租户；展示使用次数与评分。 |
| US-16 | 作为**平台运营者**，我希望首页一屏看到任务/成功率/节省成本/响应时间 KPI 与近期任务、执行日志、安全概览，以便全局掌控。 | 首页重做 / G1 | 首页渲染 4 KPI 卡 + 6 Agent + 4 技能 + 近期任务 + 执行日志 + 安全概览；数据来自真实 API（非硬编码）。 |

---

## 5. 需求池（P0 / P1 / P2）

> P0 = 跑通「任务→经验→Skill」闭环 + 六大 Hub 骨架 + 首页；P1 = 治理/成本/SLO 深化；P2 = 进阶演进。

### P0（必须，MVP 闭环 + 骨架 + 首页）

| 编号 | 需求 | 所属模块 | 验收标准 | 优先级 |
|---|---|---|---|---|
| P0-01 | 建立 `Experience` 实体与 CRUD，与 `Memory` 通过 N:M 关联 | Memory Hub / 数据模型 | 任务终态自动落 `experiences`；含来源 run_id 与 memory 关联；可查询 | P0 |
| P0-02 | **闭环跳③ 补缺**：Skill Candidate Compiler（相似经验聚类→生成技能候选草稿） | Skill Hub | ≥N 条相似经验触发，输出 `skill_candidates`（含 prompt/steps/tools/io_schema） | P0 |
| P0-03 | Skill Registry + Skill Versioning（semver、changelog、回滚） | Skill Hub | 技能可注册、版本化、查看 diff、回滚；列表分页 | P0 |
| P0-04 | Skill Evaluation 接入（复用 `evaluation/`，candidate→报告→verdict） | Validation Layer | 候选生成即入评估队列；产出报告；未达标不可发布 | P0 |
| P0-05 | 治理闸门：Skill 发布需经评估 + 审批 | Skill Hub / HITL | 无评估+审批无法置为"已发布"；有审批留痕 | P0 |
| P0-06 | Agent Runtime 骨架：Supervisor → Research/Data/Code Agent（LangGraph） | Agent Runtime | 四类 Agent 可被 Supervisor 路由；LangGraph 图可运行 | P0 |
| P0-07 | Validation Layer：成功/失败判定 + Failure→Replan + Agent Loop | Validation Layer | 失败触发 Replan 分支；记录 replan 原因；有最大重试上限 | P0 |
| P0-08 | MCP Tool Layer：DB/Search/File/Git/企业 API 五类工具经 MCP 暴露 | MCP Tool Layer | 五类工具有 router；调用有审计；输出经 `tool_output_guard` | P0 |
| P0-09 | Policy Engine 骨架：RBAC 策略 + 高风险 HITL 分流 | Security Hub | 策略可配；高风险 100% 转审批；越权 403 | P0 |
| P0-10 | Tenant Isolation：所有查询强制 `tenant_id` 过滤 | Security Hub | 跨租户返回 403（用例验证 100% 拦截） | P0 |
| P0-11 | 首页按设计稿重做（全部区块，数据接真实 API） | 门户/前端 | 见第 7 章组件级要求；无硬编码假数据 | P0 |
| P0-12 | SSE 实时执行流（Run→SSE→首页日志时间线） | Agent Runtime / SSE | 建立连接≤1s；实时追加；断线可重连 | P0 |
| P0-13 | Hierarchical Memory 五层骨架（Team/User/Episodic/Semantic/Org） | Memory Hub / 评审7 | 五层分区展示；写入/读取规则明确 | P0 |
| P0-14 | 本机可运行最小配置（venv + 8010 后端 + 5173 前端 + PG 5433） | 部署/约束 | 按第 8 章配置可本机启动跑通 P0 闭环 | P0 |

### P1（应该，治理 / 成本 / SLO）

| 编号 | 需求 | 所属模块 | 验收标准 | 优先级 |
|---|---|---|---|---|
| P1-01 | DLP：PII/敏感字段检测与外发拦截 | Security Hub / 评审5 | 敏感外发 100% 拦截并告警；可配置规则 | P1 |
| P1-02 | ABAC 条件策略（按属性/环境动态决策） | Security Hub / 评审5 | 支持条件表达式；决策可审计 | P1 |
| P1-03 | Cost Optimization：预算配置 + 预算感知降级调度 | Cost / 评审6 | 达阈值触发降级（换小模型/裁剪上下文/暂停非关键）；成本看板 | P1 |
| P1-04 | SLO 分级 + 实时达标率 + 自动降级 | 可观测/评审4 | 三级 SLO；监控页展示达标率与告警；不达标触发降级 | P1 |
| P1-05 | Data/Context Optimization 平台能力（统一 Context Builder） | 评审9 | 所有 Agent 经统一接口取上下文；记录压缩率/命中率 | P1 |
| P1-06 | Organizational Memory 落地（与 Team Memory 边界清晰、可下沉） | Memory Hub / 评审7 | Org 层可沉淀与检索；跨层提升需显式操作 | P1 |
| P1-07 | 全链路审计与导出（Audit） | 观测/合规 | 关键动作全量留痕；支持按租户/时间导出 | P1 |
| P1-08 | 「可信×进化」扩展逻辑落地（发布前可信基线校验） | 评审8 | 发布前强制通过工具白名单/越权/PII 校验，不通过禁发布 | P1 |

### P2（可选，进阶演进）

| 编号 | 需求 | 所属模块 | 验收标准 | 优先级 |
|---|---|---|---|---|
| P2-01 | Skill Evolution 自动建议（基于评估与使用数据推荐优化/退役） | Skill Hub / 评审2 | 定期产出建议清单；给出依据指标 | P2 |
| P2-02 | Skill Marketplace 评分/评论/使用排行 | Skill Hub | 支持评分与排行；展示使用次数 | P2 |
| P2-03 | 多模型可插拔路由（无 Ollama 时降级到 Mock/远程） | Agent Runtime | 无模型可运行时降级不报错；可切换 provider | P2 |
| P2-04 | 记忆/经验自动去重与冲突消解 | Memory Hub | 相似度高时合并或标记冲突 | P2 |
| P2-05 | 事件层扩展（事件驱动触发任务：Redis/Kafka） | events | 事件可触发 Run | P2 |
| P2-06 | 首页个性化（按角色渲染 KPI/快捷建议） | 门户 | 不同角色看到差异化首页 | P2 |
| P2-07 | 多模态输入（PDF/图片进任务） | multimodal | 可上传 PDF/图片并进入任务上下文 | P2 |

---

## 6. 六大 Hub 功能规格

### 6.1 Skill Hub（技能中枢）

| 项 | 内容 |
|---|---|
| 职责 | 技能的注册、版本、评估、演进与共享；承接闭环 ③④⑤⑥ |
| 关键能力 | Skill Registry（注册/检索/分页）；Skill Versioning（semver、changelog、diff、回滚）；Skill Candidate Compiler（**补缺的一环**）；Skill Evaluation 对接；Skill Governance Gate（发布需评估+审批）；Skill Evolution 建议（P2）；Marketplace 发布（P2） |
| 对外接口（REST） | `GET/POST /api/skills`、`GET /api/skills/{id}/versions`、`POST /api/skills/{id}/versions`、`POST /api/skills/{id}/rollback`、`GET/POST /api/skill-candidates`、`POST /api/skill-candidates/{id}/evaluate`、`POST /api/skill-candidates/{id}/promote`、`GET/POST /api/marketplace/skills` |
| 依赖 | Policy Engine（发布鉴权）、Evaluation、Marketplace 注册表、PG `skills/skill_versions/skill_candidates` |

### 6.2 Memory Hub（记忆中枢）

| 项 | 内容 |
|---|---|
| 职责 | 分层记忆 + 经验的存储与检索；承接闭环 ②⑦ |
| 关键能力 | 五层记忆：User Memory、Team Memory、Episodic Memory、Semantic Memory、Organizational Memory；**Experience 作为一等资产**（与 Memory 分离、N:M 关联）；pgvector 语义检索；Data/Context Optimization（统一 Context Builder，P1）；自动去重（P2） |
| 对外接口 | `GET/POST /api/memory`（按 scope 过滤）、`GET/POST /api/experiences`、`POST /api/memory/search`（语义）、`GET /api/experiences/{id}/lineage`（来源 Run/Memory 追溯）、`GET /api/context/build`（P1 Context Builder） |
| 依赖 | PostgreSQL/pgvector、Tenant Isolation、DLP |

### 6.3 Security Hub（安全中枢，平台底座）

| 项 | 内容 |
|---|---|
| 职责 | 平台级安全治理底座：多租户隔离、权限、数据防泄露、审计 |
| 关键能力 | Policy Engine（RBAC + ABAC 条件策略）；Tenant Isolation（查询强制 tenant 过滤）；DLP（PII 脱敏 + 敏感外发拦截）；HITL 分流；复用现有 `security/`(pii_redactor/prompt_guard/ssrf_guard/tool_output_guard/email_allowlist)、`rbac/`、`middleware/`(auth/audit/rate_limit/security_headers) |
| 对外接口 | `GET/POST /api/policies`、`POST /api/policies/evaluate`、`GET/POST /api/approvals`、`POST /api/approvals/{id}/decision`、`GET /api/audit`；所有 API 走统一鉴权/审计中间件 |
| 依赖 | auth(jwt/mfa/oidc)、tenant 模型、audit 存储 |

### 6.4 Agent Runtime（Agent 运行时）

| 项 | 内容 |
|---|---|
| 职责 | 承载 Supervisor → Research/Data/Code Agent 的编排与执行 |
| 关键能力 | LangGraph StateGraph 编排；Supervisor 路由；Durable 运行（PG checkpointer 持久化、可恢复/可长期运行）；SSE 实时推送；Replan 回环（承接 Validation Layer） |
| 对外接口 | `POST /api/tasks`（创建任务）、`GET /api/runs/{id}`、`GET /api/runs/{id}/events`（SSE）、`POST /api/runs/{id}/replan`、`GET /api/agents` |
| 依赖 | graph/、agents/、state/、events/、SSE、MCP Tool Layer |

### 6.5 MCP Tool Layer（工具层）

| 项 | 内容 |
|---|---|
| 职责 | 统一工具接入：DB / Search / File / Git / 企业 API |
| 关键能力 | 复用 `mcp/`(server 14 tool router + client adapter) 与 `connectors/`(8 企业连接器)；工具调用纳入 Policy 与预算；输出经 `tool_output_guard`；危险工具白名单/黑名单 |
| 对外接口 | 内部 MCP server（建议端口 8001）；工具注册表 `GET /api/tools`；调用审计写入 `run_steps`/`audit` |
| 依赖 | Security Hub（Policy）、resilience（retry/circuit_breaker/budget_guard） |

### 6.6 Validation Layer（验证层）

| 项 | 内容 |
|---|---|
| 职责 | 判定成功/失败，驱动 Replan 与经验沉淀；承接闭环 ①② |
| 关键能力 | Success/Failure 判定（复用 `evaluation/` judge + metrics）；Failure → Replan 触发；任务终态触发 `Experience` 提取；输出结论写回 Memory Hub |
| 对外接口 | 内部：`validate(run) -> verdict`；事件：`run.completed`/`run.failed` 触发 `Experience` 生成 |
| 依赖 | Evaluation、Agent Runtime、Memory Hub |

---

## 7. UI 设计稿（首页 · 组件级）

> 设计风格：深色科技风。背景 deep navy `#0B1020` 级；强调色 靛紫→蓝 渐变（如 `#6366F1 → #3B82F6`）。布局：**左侧固定侧边栏 + 中部主内容区 + 右侧信息栏** 三栏。参考现有 `frontend/src/*views` 与 `tokens.css`。

### 7.1 布局骨架

| 区域 | 宽度/位置 | 说明 |
|---|---|---|
| 左侧侧边栏 | 固定宽 ~240px，全高，深色 | 品牌区 + 导航 + 底部渐变卡 |
| 顶部栏 | 主内容区顶部，sticky | 全局搜索 + 通知 + 用户信息 |
| 主内容区 | 自适应，可滚动 | Hero + KPI + 我的 Agent + 技能中心 |
| 右侧信息栏 | 固定宽 ~360px，可滚动 | 近期任务 + 智能执行日志 + 安全与资源概览 |

### 7.2 组件清单（含文案、状态、交互）

**A. 左侧侧边栏**

| 组件 | 描述 | 状态/交互 |
|---|---|---|
| 品牌区（左上方） | 图标 + 两行文字：`AgentFlow` / `Enterprise AI Workforce OS` | 图标点击回首页 |
| 主导航（每项带图标 + 文字） | 首页 · 智能任务 · Agent 工作台 · 技能中心 · 知识库 · 记忆管理 · 数据分析 · 安全与权限 · 运维监控 · 系统设置 | **选中态**：左侧高亮条 + 渐变底 + 图标/文字高亮；**悬停**：背景轻微变亮；点击切换路由并高亮；`系统设置` 可展开（子项：用户与角色 / 策略配置 / 审计导出） |
| 侧边栏底部渐变卡 | 渐变背景小卡，文案三行：`让企业经验` / `沉淀为可执行的技能` / `让 AI 真正为团队创造价值` | 悬停微放大/提亮 |

**B. 顶部栏**

| 组件 | 描述 | 状态/交互 |
|---|---|---|
| 全局搜索框 | 占位符 `搜索任务、Agent、技能、知识库…`；右侧显示快捷键徽标 `⌘K` | 聚焦时描边高亮；按 `⌘K`/`Ctrl+K` 聚焦；输入触发搜索结果下拉（加载态显示骨架条） |
| 通知铃铛 | 图标 + 右上红点（未读） | 悬停提示；点击展开通知面板；红点随已读消失 |
| 用户信息 | 头像 + 姓名 `张同学` + 角色 `开发工程师` + 下拉箭头 | 点击展开菜单（个人中心/退出）；箭头悬停旋转 |

**C. 主区 Hero 卡**

| 组件 | 描述 | 状态/交互 |
|---|---|---|
| 大标题 | `你好，欢迎使用 企业级 Multi-Agent 智能工作与技能资产平台`（后段加粗/渐变强调） | 静态 |
| 副标题 | `基于 LangGraph + MCP，构建安全、可控、可持续进化的企业级 AI 员工体系` | 静态 |
| 任务输入框 | 占位符 `告诉我你想完成什么任务…` + 右侧发送按钮（纸飞机/箭头图标） | 输入时按钮由灰变亮（可用态）；提交后按钮变 **加载态**（转圈/禁用），并跳转/内联展示执行；`Enter` 提交 |
| 快捷建议 Chips（4 个） | `分析销售数据并生成报告` · `开发一个新功能` · `查询公司知识库` · `创建新的技能` | 悬停高亮；点击将文案填入输入框 |
| 装饰插画 | 右侧由 4 个**立体渐变方块**组成（`Agent` / `Skills` / `Memory` / `Security`），带轻微悬浮/浮动动画 | 静止或低强度浮动动画 |

**D. 4 个 KPI 统计卡**

| 卡片 | 数值 | 环比 |
|---|---|---|
| 总任务数 | `1,248` | `+12% 较上月` |
| 成功率 | `96.8%` | `+2.3% 较上月` |
| 节省成本 | `¥8,642` | `+32% 较上月` |
| 平均响应时间 | `18.4s` | `+45% 较上月` |

> 每卡 = 图标 + 数值 + 环比文案（涨绿/跌红约定需与设计确认）；数据接真实 API，加载时显示骨架。

**E. 左侧大卡「我的 Agent」**

- 卡片标题 `我的 Agent`，右上 `查看全部 →`；副标题 `智能协作的多智能体团队`。
- **2×3 网格 = 6 张 Agent 卡**，每张：彩色圆角图标 + 名称 + 类别 + 一句话描述 + 绿点`在线`。

| # | 名称 | 类别 | 描述 | 状态 |
|---|---|---|---|---|
| 1 | 任务规划师 | Planner Agent | 任务拆解、流程规划、资源分配 | 在线 |
| 2 | 数据分析师 | Data Agent | 数据查询、分析、可视化 | 在线 |
| 3 | 代码开发师 | Code Agent | 代码编辑、调试、测试、修复 | 在线 |
| 4 | 研究助手 | Research Agent | 信息检索、资料整理、深度分析 | 在线 |
| 5 | 文档处理师 | Document Agent | 文档解析、知识提取、总结 | 在线 |
| 6 | 安全审计师 | Security Agent | 权限控制、风险检测、合规审计 | 在线 |

> 交互：卡片悬停上浮/描边高亮；点击进入该 Agent 工作台（Agent 工作台视图）。

**F. 左侧「技能中心」卡**

- 卡片标题 `技能中心`，右上 `查看全部 →`；副标题 `沉淀团队经验，构建可复用的技能资产`。
- **4 张技能卡横排**，每张 = 标题 + 领域标签 + 使用次数：

| 标题 | 领域标签 | 使用次数 |
|---|---|---|
| 客户流失分析 | 数据分析 | 128 次 |
| 合同审查 | 企业知识库 | 96 次 |
| 项目复盘总结 | 项目管理 | 74 次 |
| 代码质量检查 | 代码开发 | 52 次 |

> 交互：悬停高亮；点击进入技能详情（技能中心视图）。

**G. 右栏「近期任务」**

- 标题 `近期任务`，右上 `查看全部 →`；列表项 = 彩色图标 + 标题 + 分类标签 + 时间 + 右侧状态标签。

| 标题 | 分类 | 时间 | 状态 |
|---|---|---|---|
| 分析华东地区销售数据并生成报告 | 数据分析 | 2 小时前 | 进行中 |
| 开发用户登录功能 | 代码开发 | 3 小时前 | 进行中 |
| 查询公司规章制度 | 知识问答 | 5 小时前 | 已完成 |
| 生成竞品分析报告 | 研究分析 | 昨天 16:23 | 已完成 |
| 合同风险评估 | 文档处理 | 昨天 14:12 | 已完成 |

> 状态标签配色：进行中=蓝/紫，已完成=绿。

**H. 右栏「智能执行日志」**

- 标题 `智能执行日志`，右上 `查看全部 →`；时间线，每条 = 状态圆点 + 标题 + 描述。
- 条目（自上而下）：
  1. **任务已完成**：生成销售数据分析报告 — `耗时 4 分 32 秒 · 调用 12 次工具`
  2. **Data Agent 正在查询数据库…** — `已获取 3 个数据集，开始数据清洗`
  3. **Analysis Agent 进行数据分析…** — `发现数据：环比下跌 12.3%`
  4. **Report Agent 生成可视化图表…** — `已完成报告，正在生成总结文档`
- 底部链接：`查看完整执行过程 →`（点击进入 LiveRuns 全量回放）。
- **加载态/实时**：进行中的条目圆点带呼吸动画；新步骤经 SSE 实时追加到顶部或有序插入。

**I. 右栏「安全与资源概览」**

- 标题 `安全与资源概览`，右上 `查看详情 →`。
- 状态徽标：`系统运行正常`（绿色）。
- 三个胶囊标签，均显示 `已启用`：`用户隔离` · `数据加密` · `权限控制`。

### 7.3 交互与状态约定（通用）

| 类型 | 约定 |
|---|---|
| 点击 | 导航切换路由；卡片进入对应详情/工作台；`查看全部 →` 进入列表页 |
| 悬停 | 卡片/列表项 上浮 + 描边高亮；按钮提亮；导航背景变亮 |
| 加载态 | 数据卡显示骨架屏；任务提交按钮转圈禁用；搜索显示结果骨架 |
| 空态 | 无数据时显示引导文案 + 主行动按钮（如"发起第一个任务"） |
| 实时态 | SSE 驱动执行日志/任务状态实时更新；进行中带呼吸圆点 |

---

## 8. 非功能需求

### 8.1 性能与 SLO 分级（评审 4）

| 等级 | 覆盖能力 | 目标 SLO | 降级策略 |
|---|---|---|---|
| 核心（Critical） | 任务提交、SSE 推送、Policy 决策、租户隔离、审计写入 | 可用性 ≥ 99.5%；任务提交 P95 ≤ 1.5s；SSE 建连 ≤ 1s | 禁止降级；过载时拒绝新任务（快速失败 + 友好提示），保障已有运行 |
| 重要（Important） | 记忆/经验检索、Skill 安装、评估触发 | 可用性 ≥ 99.0%；检索 P95 ≤ 2s | 检索降级为关键词匹配（非向量）；评估排队延后 |
| 边缘（Edge） | 分析看板刷新、建议、Evolution 建议、多模态 | 可用性 ≥ 97%；P95 ≤ 5s | 直接关闭非关键功能，保核心链路 |

> 监控页展示各等级实时达标率；不达标自动触发对应降级并告警；**不做笼统"99.99%"承诺**。

### 8.2 成本与预算（评审 6）

- 支持租户/团队/任务三级预算配置；提供 `budget_guard`（已有）联动。
- 达预警阈值（默认 80%）→ 记录并提示；达上限（100%）→ 触发降级调度（换小模型 / 裁剪上下文 / 暂停非关键任务）。
- 成本看板展示"节省金额"与环比（对应首页 KPI「节省成本 ¥8,642 +32%」）。
- 无可用 LLM（Ollama 未运行）时必须可降级到 Mock/远程 provider，不得报错崩溃（可插拔）。

### 8.3 安全与合规（评审 5）

- **Tenant Isolation**：所有数据查询强制注入 `tenant_id`；跨租户访问返回 403；隔离等级默认为"逻辑隔离（行级）"，可选"库级隔离"。
- **DLP**：PII 检测与脱敏（`pii_redactor`）；敏感数据外发（邮件/HTTP 出口）拦截（`email_allowlist`/`ssrf_guard`）；提示注入防护（`prompt_guard`）。
- **Policy Engine**：RBAC + ABAC；高风险操作强制 HITL；所有决策写审计。
- **审计**：关键动作全量留痕，支持按租户/时间导出。

### 8.4 可观测性（评审 4）

- 复用 `observability/`（prometheus 指标、tracing）与 `metrics_store`。
- 埋点：任务成功率、P95 延迟、成本、工具调用次数、SLO 达标率、DLP/越权拦截计数、Skill 使用次数。
- 首页 KPI 与监控页数据均来自真实指标接口。

### 8.5 本机运行约束与最低可运行配置

| 约束项 | 实测值 | 设计要求 |
|---|---|---|
| OS/CPU | Windows 11，16 vCPU | 支持本机原生运行（非必须 Docker） |
| RAM | 15.2GB 总量，仅 ~2.2GB 空闲 | **不常驻多容器、不跑本地大模型**；单进程后端 + 单前端 |
| 磁盘 | C: 80.9GB 空闲，D: 200.6GB 空闲 | 项目、venv、数据一律放 **D 盘** |
| Docker | 29.8.0 可用；已有 `rag_postgres`(postgres:16-alpine, healthy) 在跑 | **复用现有 PG 镜像**；本项目 PG 用 **5433**（5432 已被占用） |
| Python | 托管 3.13.12 可用，但 fastapi/langgraph/sqlalchemy/psycopg/pydantic 等**未安装**（仅 pytest） | 建独立 venv（`D:\...\agentflow\.venv`）；依赖离线安装 |
| Node/npm | node v22.22.2 可用；**裸 `npm` 不可用** | 必须走 `node <NODE_DIR>\node_modules\npm\bin\npm-cli.js`；前端依赖离线安装 |
| Ollama | 未运行（11434 无监听） | LLM 可插拔 + 无模型时降级（Mock/远程） |
| 端口 | 8000 已占用；5432/5433/8001/5173 空闲 | 后端 **8010**，MCP **8001**，前端 **5173**，PG **5433** |

**最低可运行配置（MVP）**：Windows 11 + 托管 Python 3.13 venv + 复用 postgres:16-alpine（映射 5433）+ node v22 + 后端 8010 单进程 + 前端 5173（dev）+ LLM 走 Mock/远程降级。**不要求**：K8s/Helm/Terraform、本地大模型、多容器常驻。

### 8.6 兼容与可维护性

- 复用 ForgeFlow 现有 23 个后端子包与 18 个前端 view，改造优先"增量扩展"而非重写。
- 保留 Apache-2.0 许可与署名。
- 现有 unit(36)/integration(7) 测试须持续通过；新增闭环链路的集成测试。

---

## 9. 待确认问题（需用户拍板，最多 8 条）

| # | 问题 | 影响 | 建议默认值 |
|---|---|---|---|
| Q1 | **闭环跳③的触发阈值 N 与"相似"判定**：多少条相似经验才允许固化为技能候选？相似度阈值？ | 决定 Skill Candidate Compiler 行为 | N=3，余弦相似度 ≥ 0.85 |
| Q2 | **Skill 演进的治理闸门强度**：谁有权审批新版本发布（owner / 安全官 / 双人复核）？ | 决定 Governance Gate 与 HITL 流程 | 普通技能 owner 审批；涉安全/财务类需双人复核 |
| Q3 | **HITL 风险分级规则**：哪些操作算"高风险"（删库/转账/外发邮件/批量写）？分级阈值由谁定？ | 决定 Policy Engine 分流 | 提供默认规则集，可配置；破坏性写/资金/外发默认高风险 |
| Q4 | **多租户隔离等级**：默认行级逻辑隔离，还是需要库/schema 级物理隔离？ | 影响数据模型与部署成本 | 默认行级逻辑隔离，可升级库级 |
| Q5 | **SLO 具体数值**：三级 SLO 的目标值（99.5%/99.0%/97%）是否接受？ | 决定降级策略与监控告警 | 采用本文档建议值 |
| Q6 | **成本预算口径**：按 token / 按调用 / 按金额？"节省成本"如何与基线对比计算？ | 决定 Cost KPI 计算方式 | 按金额（元），基线=上期同口径实际成本 |
| Q7 | **LLM provider 与降级顺序**：无 Ollama 时优先用哪个（远程 API / Mock）？是否有可用 API Key？ | 决定 P2-03 与运行可行性 | 优先远程 API，无 Key 时 Mock 演示 |
| Q8 | **Skill Marketplace 共享边界**：技能可跨租户共享，还是仅租户内团队共享？ | 影响隔离与 Marketplace 设计 | 默认租户内共享，跨租户需显式授权 |

---

> **交接说明（给架构师/工程师）**：P0 需求构成可交付的 MVP——六层骨架 + 「任务→经验→Skill」闭环（含补齐的跳③ Skill Candidate Compiler）+ 首页重做；P1 为治理/成本/SLO 增强；P2 为演进项。核心数据模型见第 3.3 节；首页组件级规格见第 7 章；本机运行请严格遵循第 8.5 节端口与路径约束。
