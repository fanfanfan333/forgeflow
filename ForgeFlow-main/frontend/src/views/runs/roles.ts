/**
 * roles.ts —— 展示层「角色名 / 档位文案 / 模型驱动判定」的**唯一事实源**。
 *
 * ⚠️ 重要声明（勿删）：以下全部为**展示层命名（display name）**，依据为该工具的
 * **真实功能** / **后端 note 原文**；这**不是**后端字段，**不得**被当作后端角色名
 * 引用。后端 run 记录里**没有**结构化的角色字段（`steps[]` 只有
 * `tool / step_type / note / index / status / step_id / applicability`，
 * 生产者见 `forgeflow/runtime/planning.py::PlanStep.to_payload()`）；图3 里的英文角色链
 * （`Planner / Research Agent / … / Verifier`）在 `/runs` 链路**没有**对应字段，
 * 本文件**绝不为它臆造**——角色名一律取自下方映射表 / 后端 `note` 原文 / 真实工具 id。
 *
 * 依据纪律：每一条映射都写明依据来源，并**如实分两类**：
 *   * NOTE_ROLE —— 角色词**直接来自**后端 `note` 原文（如 `orchestrator.py::_DEFAULT_STEPS` 的
 *     「研究助手检索资料」）。
 *   * SEMANTIC  —— 由该工具**真实动作语义**概括（如 `report.render` 的 note
 *     是「生成运行产出报告」，**未含角色词**，其展示名「报告生成」是语义概括）——
 *     **必须显式标注 SEMANTIC，不得冒充 NOTE_ROLE**。
 *
 * 回落铁律：映射表**以外**的工具一律回落**真实工具 id**（禁发明角色）。
 *
 * 覆盖真实工具全集（自检清单，供 QA 的 AC-3 白名单引用）：
 *   * `PLATFORM_TOOL_CATALOGUE`（`forgeflow/runtime/gate.py`）
 *     = research.search / data.query / analysis.score / docs.parse /
 *       report.render / policy.check / git.diff / code.lint
 *   * 受控工具（`forgeflow/runtime/gate.py::TOOL_PERMISSION_MAP`）
 *     = payment.transfer / payment.refund / data.export / policy.grant / skill.publish
 *   * `PLATFORM_TOOLS`（`gate.py::PLATFORM_TOOLS`）= research.search / data.query / code.run
 *   三者并集 = 节点主标识的合法值域；本表的 key 覆盖该并集（未知工具回落真实 id）。
 */

/**
 * 工具 id → 中文展示名（display name）。
 *
 * 覆盖 `gate.py` 的真实工具全集；每条依据见行内注释（NOTE_ROLE / SEMANTIC）。
 */
export const TOOL_ROLE_MAP: Record<string, string> = {
  // ── NOTE_ROLE：角色词**直接来自**后端 `orchestrator.py::_DEFAULT_STEPS` 的 `note` 原文 ──
  // 依据：orchestrator.py::_DEFAULT_STEPS note = 「研究助手检索资料」（角色词「研究助手」）
  'research.search': '研究助手',
  // 依据：orchestrator.py::_DEFAULT_STEPS note = 「数据分析师查询数据集」（角色词「数据分析师」）
  'data.query': '数据分析师',
  // 依据：orchestrator.py::_DEFAULT_STEPS note = 「代码开发师执行并验证」（角色词「代码开发师」）
  'code.run': '代码开发师',

  // ── SEMANTIC：由**真实动作语义**概括；后端 note 无角色词，**不得**冒充 NOTE_ROLE ──
  // 依据：report.render 的 note（orchestrator.py::_DEFAULT_STEPS「生成运行产出报告」）**无角色词**，
  // 展示名取自其真实动作语义（渲染本 run 的产出报告）。
  'report.render': '报告生成',
  // 依据：analysis.score 属平台工具集（gate.py::PLATFORM_TOOL_CATALOGUE）的评分工具，note 无角色词，
  // 展示名取自其真实动作语义（对结果打分）。
  'analysis.score': '分析评分',
  // 依据：docs.parse 属平台工具集（gate.py::PLATFORM_TOOL_CATALOGUE）的文档解析工具，语义即「解析文档」。
  'docs.parse': '文档解析',
  // 依据：policy.check 属平台工具集（gate.py::PLATFORM_TOOL_CATALOGUE），合同审查种子技能声明它，
  // 语义即「合规/策略检查」。
  'policy.check': '合规检查',
  // 依据：git.diff 属平台工具集（gate.py::PLATFORM_TOOL_CATALOGUE），语义即「比对代码变更」。
  'git.diff': '代码变更比对',
  // 依据：code.lint 属平台工具集（gate.py::PLATFORM_TOOL_CATALOGUE），代码质量检查技能声明它，
  // 语义即「代码质量检查」。
  'code.lint': '代码质量检查',
  // 依据：payment.transfer 属受控工具（gate.py::TOOL_PERMISSION_MAP，需纵权审批），语义即「支付转账」。
  'payment.transfer': '支付转账',
  // 依据：payment.refund 属受控工具（gate.py::TOOL_PERMISSION_MAP），语义即「支付退款」。
  'payment.refund': '支付退款',
  // 依据：data.export 属受控工具（gate.py::TOOL_PERMISSION_MAP，写 memory 权限），语义即「数据导出」。
  'data.export': '数据导出',
  // 依据：policy.grant 属受控工具（gate.py::TOOL_PERMISSION_MAP，写 policies 权限），语义即「策略授权」。
  'policy.grant': '策略授权',
  // 依据：skill.publish 属受控工具（gate.py::TOOL_PERMISSION_MAP，需纵权审批），语义即「技能发布」。
  'skill.publish': '技能发布',
}

/**
 * 工作链节点「主标识」的**取值链**（严格按序，取到即用）：
 *   1. `TOOL_ROLE_MAP` 的**展示名**；
 *   2. 后端 `step.note` **原文**（逐字，后端真实字段）；
 *   3. **真实工具 id**。
 *
 * ⚠️ 最终**绝不**回落成 `步骤 N`（AC-2 明确要求主标识 ≠ 正则 `^步骤 \d+$`）。
 * 若三者皆空（工具与 note 均缺失——见下），返回空串；该路径不含角色/工具语义，
 * 属 AC-2 的 given（「tool_invocations 非空」）之外的边界，调用方不得据此拼「步骤 N」。
 *
 * @param tool 后端 `step.tool`（原始工具 id）
 * @param note 后端 `step.note`（原始备注；仅当工具未命中映射表时用作回落）
 */
export function stageNameForTool(tool: string | undefined, note?: string): string {
  const t = (tool ?? '').trim()
  const mapped = TOOL_ROLE_MAP[t]
  if (mapped) return mapped
  // 未知工具：优先用后端 note 原文（真实字段，可逐字回溯），否则回落真实工具 id。
  const n = (note ?? '').trim()
  if (n) return n
  return t
}

/**
 * 兼容别名：`roleForTool` == 只返回映射表命中项（未命中返回真实工具 id）。
 *
 * 保留此名以符合设计 §3.3 的接口钉；未知工具回落**真实工具 id**（禁发明角色）。
 */
export function roleForTool(tool: string): string {
  const t = (tool ?? '').trim()
  if (!t) return ''
  return TOOL_ROLE_MAP[t] ?? t
}

/**
 * 运行档位 → 业务表述（Q7）。
 *
 * docstring 依据：`orchestrator.py::run_task`
 *   * `runtime_mode = resolve_agent_runtime_mode()`（"llm" / "react" / 离线默认）
 *   * 存在编译图 ⇒ `runtime_mode = "graph"`
 *   * "react" → react_executor；"llm" → _llm_executor；其余 → _default_executor
 *
 * 硬约束：界面**不得**直出 `deterministic` / `llm` 这类工程值（除 debug 密度）；
 * 未知值**原样**返回，不臆造业务名。
 */
export function runtimeModeLabel(mode: string | undefined | null): string {
  switch ((mode ?? '').trim()) {
    case 'deterministic':
      return '平台编排'
    case 'llm':
      return '模型驱动'
    case 'react':
      return '模型驱动'
    case 'graph':
      return '多智能体编排'
    default:
      return (mode ?? '').trim()
  }
}

/**
 * INC21 / G5 —— 平台 `workflow_type` → 业务标签（展示层）。
 *
 * ⚠️ 与 `runtimeModeLabel` 是**同一既有模式**：已知值映射业务表述、**未知值原样返回**、
 * **绝不臆造**。本函数是 **SEMANTIC（按标识符语义概括）**，**不是**后端字段原文，
 * **不得**被当作后端 `workflow_type` 的字面值对外引用（对照本文件顶部的
 * NOTE_ROLE / SEMANTIC 纪律）。
 *
 * 依据（逐条，文件:行号）：
 *   * `generic` —— `forgeflow/api/hub_schemas.py::TaskCreateRequest.workflow_type`
 *     `workflow_type: str = "generic"` 的默认值，即「无特定域模板时的通用流程」；
 *     该默认值经 `forgeflow/runtime/orchestrator.py::run_task` `"tags": [task.workflow_type]`
 *     原样写进运行记录，故经验标签里会出现字面 `generic`（平台内部标识符）。
 *   * `sales_ops` / `support_ops` / `finance_recon` —— 三个域模板名，见
 *     `forgeflow/api/main.py`（`app.state.graphs` 的 `compile_graph(workflow_type=…)`）与
 *     `forgeflow/api/schemas.py::WorkflowRunRequest.workflow_type`（`workflow_type: WorkflowType = "sales_ops"`；
 *     其字面域见 `forgeflow/api/schemas.py::WorkflowType` 的 `Literal[...]`）。
 *
 * 取值纪律：已知值映射业务表述；其它值**原样返回**（`trim` 后，空串返回空串）。
 */
export function workflowTypeLabel(t: string | undefined | null): string {
  switch ((t ?? '').trim()) {
    case 'generic':
      return '通用流程'
    case 'sales_ops':
      return '销售运营'
    case 'support_ops':
      return '客户支持'
    case 'finance_recon':
      return '财务对账'
    default:
      // 未知值**原样返回**（trim 后），不臆造业务名。
      return (t ?? '').trim()
  }
}

/**
 * 「模型驱动」档位集合 —— `runtime_mode` 的**唯一**判据来源（orchestrator.py::run_task）。
 */
export const MODEL_RUNTIME_MODES: ReadonlySet<string> = new Set(['llm', 'react', 'graph'])

/**
 * 本次运行是否**真的调用过模型**——「未测量 ≠ 0」的**唯一有效判据**（Q4 / AC-5）。
 *
 * 判据（二者取或）：
 *   1. `runtime_mode` ∈ {`llm`, `react`, `graph`}（`orchestrator.py::run_task`）；
 *   2. `llm` 字典非空（`hub_schemas.py::RunDetailResponse.llm` / `orchestrator.py::run_task` 的 `llm=dict(llm_meta)`）。
 *
 * ⚠️ **不得**改用 `typeof total_tokens === 'number'`：生产者在确定性档也写 `0`
 * （残余不可分性，见 `hub_schemas.py` 的 INC20/T01 注释），数值本身无法判「是否跑过模型」。
 */
export function isModelDriven(
  runtimeMode: string | undefined | null,
  llm: Record<string, unknown> | undefined | null,
): boolean {
  if (MODEL_RUNTIME_MODES.has((runtimeMode ?? '').trim())) return true
  return Object.keys(llm ?? {}).length > 0
}
