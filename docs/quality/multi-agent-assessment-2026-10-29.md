# 多智能体能力评估（2026-10-29）

结论先行：**功能基本正常，可以写入简历，但要按实写，不能按“生产多智能体平台”写。**
本页是评估证据，不是宣传页；每一项都注明“已实现 / 部分 / 未实现”，
以及简历上可以写、不能写的话。

## 1. 功能是否正常

| 检查项 | 结果 | 证据 |
| --- | --- | --- |
| 单元测试 | 3572 通过 / 9 失败 / 23 跳过 | 2026-10-29 在 `~/workspace/forgeflow-venv` 全量跑 `tests/unit`；9 个失败经基线 `9b0f242` 重跑确认全部是既有问题（fastmcp 接口漂移、PDF 编码、验证栈、Windows 路径 C18 等），与本次改动无关 |
| 跨项目结果传递 | 已修复并推送 | `64ed648` + `9b0f242`：服务端派生 `handoff_from_run_id`，租户/用户 fail-closed |
| 审计 Top 3 | 已修复并推送 | `aee2d5e` + `445b2e3`：非 loopback 外部 agent-server 空 token 拒绝、`/workspaces` admin-only、`/workspace/sessions` total 修正 |
| 集成 / 真实栈 | 未在本机验证 | 需要 PostgreSQL + Ollama，`tests/realstack` 本来就双重门控，离线测试通过不代表真实模型或企业连接器已验收（README 已如实说明） |
| CI | 有真实门禁 | `.github/workflows/ci.yml`：lint + 类型检查 + 测试，无 `continue-on-error`；真实栈测试明确排除在 CI 外 |

## 2. 是否达到多智能体要求

对照开源项目（LangGraph supervisor / swarm、CrewAI parallel process、
AutoGen group chat）的通用要求逐项核对：

| 要求 | ForgeFlow 现状 | 证据 |
| --- | --- | --- |
| Supervisor 路由 | 已实现 | `forgeflow/graph/builder.py`：Supervisor → Researcher / Analyzer / Executor，hub-and-spoke，工作节点回 Supervisor；`route_supervisor` 有确定性单元测试 |
| 结构化路由决策 | 已实现 | Supervisor 只发结构化 `RoutingDecision`，不直接调工具（`docs/architecture.md`） |
| 角色化专家 | 已实现 | `forgeflow/agents/`：Supervisor、Researcher、Analyzer、Executor；`forgeflow/runtime/delegation.py`：research / document_review / code_review 三个只读专家，工具白名单按角色收敛 |
| 专家委派 | 已实现 | `agent.delegate`：父任务资源/需求校验、服务端预算、禁止嵌套委派、委派记录可追溯 |
| **并行委派（fan-out）** | **2026-10-29 新增** | `agent.delegate` 的 `delegations` 参数：2–4 个专家 `asyncio.gather` 并发，事前全量校验（无效简报不启动任何子任务）、单子任务失败隔离、预算上限事前拒绝；`tests/unit/test_parallel_delegation.py` 5 个测试。注意：这是**委派层并行**，不是 LangGraph 图层并行——Supervisor 图本身仍是顺序路由 |
| 跨任务/跨项目交接 | 已实现 | `runtime/result_handoff.py`：来源运行的真实产物 + 结论 + diff，带来源与截断标记，显式交接优先于派生 |
| A2A 互操作 | 已实现（进程内为主） | `forgeflow/a2a/`：JSON-RPC 2.0、AgentCard 能力发现、节点到能力映射、`/agents` 路由；出进程部署是“可部署”，不是默认部署形态 |
| 人工审批（HITL） | 已实现 | 图在 `human_approval` 处 interrupt 并持久化检查点，`/approvals/{token}/approve` 后从检查点恢复 |
| 检查点 / 可恢复 | 已实现 | LangGraph checkpointer + Postgres；中断后 `interrupted` 而非假装完成 |
| 共享记忆 / 上下文预算 | 已实现 | 统一召回记忆、技能、任务经验，去重排序并控预算；OpenViking 集成 |
| 代码执行隔离 | 已实现 | Docker / agent-server / subprocess 三种 transport；handoff 代码任务仅 Docker，隔离门不为“打通”而放宽 |
| 可观测性 | 已实现 | `forgeflow/observability/`：成本、指标、追踪、SLO；委派、路由、工具调用均有事件 |
| 评估 | 部分 | 有评估/基准/黄金回归（`forgeflow/evaluation/`、`forgeflow/benchmark/`）和 Skill 成对实测（20 案例、token −29.8%），但**没有 supervisor 路由准确率数据集**：`route_supervisor` 的测试是确定性映射，不是 LLM 路由准确率，简历上不能写准确率数字 |
| 群聊 / 辩论 / 共识 | 未实现 | 没有 AutoGen 式 group chat、加权投票或辩论协议；多专家是“分工 + 汇总”，不是“互相辩论后共识” |
| Swarm 点对点交接 | 未实现 | 只有 Supervisor 中心路由，没有 LangGraph swarm 式的 agent 间直接 handoff；多域任务的延迟仍受 Supervisor 串行路由影响（委派层并行只缓解专家审阅段） |

**达标判断：** 满足“Supervisor + 角色专家 + 委派 + A2A + HITL + 检查点 +
评估”的生产型多智能体骨架，达到可以写进简历的程度；不满足
“swarm / 群聊 / 路由准确率已量化”的更高一档要求，那三项不要宣称。

## 3. 是否可以写入简历

可以，按下面这种写法写，数字都有出处：

> 多智能体协作平台 ForgeFlow（LangGraph + FastAPI + MCP + A2A）：
> Supervisor 路由 Researcher / Analyzer / Executor，检查点 + 人工审批可恢复；
> 实现只读专家委派与 2–4 专家并行委派（事前校验、失败隔离、服务端预算），
> 跨项目结果服务端派生交接；技能成对实测 20 案例 token 减少 29.8%（63,955 →
> 44,904），单元测试 3572 通过；租户隔离、RBAC、审批与审计齐全。
> 仓库：https://github.com/fanfanfan333/forgeflow

**不要写的话：**

- 不要写“生产上线 / 服务 X 用户 / QPS”——没有这类证据。
- 不要写“路由准确率 X%”——没有 LLM 路由准确率数据集。
- 不要写“并行提速 X 倍”——并行委派有测试证明并发与隔离，没有真实
  模型任务的并行耗时对照。
- 不要把模板工作流（`support_ops` / `finance_recon`）、演示数据、mock
  模式写成生产验收结果——README 已明确区分。

## 4. 本次优化（2026-10-29）

1. **并行委派**：`forgeflow/runtime/delegation.py` 新增 `delegate_parallel`，
   `agent.delegate` 用同一工具 id 以 `delegations` 参数进入并行模式；
   `forgeflow/runtime/react_executor.py` 的工具 schema 同步新增该参数，
   工具描述同步说明并行语义。原有单专家调用完全不变。
2. **委派摘要截断标记**：`delegate` 返回新增 `summary_truncated`，
   并行合并结果同样携带；审计第 8 项的“摘要丢尾且无标记”至此关闭，
   产物与证据本身本来就不丢。
3. **本评估文档**：把“能写什么、不能写什么”钉死，避免以后简历或
   README 夸大。

## 5. 下一步（按优先级，不在本批）

1. Supervisor 路由准确率数据集：先写 eval 再写第二个 agent（开源项目
   的共识做法），否则路由回归只能靠确定性映射测试发现。
2. 图层并行：把 Supervisor 的独立工作节点改为 fan-out，委派层并行
   只是第一步。
3. Swarm / 群聊：只有在“多域延迟已成瓶颈且误路由率低”有数据时再做，
   否则中心路由的可审计性更重要。
4. 其余审计项：事件日志租户谓词、产物级续聊、预览截断标记等 7 项
   （见审计报告），按低/中低顺序处理。
