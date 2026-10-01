/**
 * labels.ts — INC37 手写展示标签（**不引入 i18n 框架**，纯静态映射）。
 *
 * 目的：把后端返回的**枚举值 / 标识符**（role / scope / outcome / status）
 * 统一映射为**面向用户的中文**，杜绝英文或 `snake_case` 裸漏到界面上
 * （用户要求：「前端界面都为中文，不要有字符的形式，必须让人看得懂」）。
 *
 * 纪律：
 *   · 只做**展示转译**，绝不改变后端调用的实际取值（`value`/请求体仍用原 key）。
 *   · 认不出的取值一律给**诚实的中文兜底**（如「其他状态」），绝不回退成英文原文。
 */

/** 角色 key（后端 `ROLE_PERMISSIONS`）→ 中文展示名。 */
export const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  manager: '经理',
  sales_rep: '销售代表',
  viewer: '只读访客',
  service: '服务账号',
  anonymous: '匿名访客',
}

/** 记忆层级 key → 中文展示名。 */
export const SCOPE_LABELS: Record<string, string> = {
  user: '用户',
  team: '团队',
  episodic: '情景',
  semantic: '语义',
  org: '组织',
}

/** 运行 / 经验结果 key → 中文展示名。 */
export const OUTCOME_LABELS: Record<string, string> = {
  success: '成功',
  completed: '已完成',
  failed: '失败',
  aborted: '已中止',
  interrupted: '已中断',
  rejected: '已拒绝',
  pending_approval: '待审批',
}

/** 角色 → 中文（未知兜底「其他角色」）。 */
export const roleLabel = (role: string): string => ROLE_LABELS[role] ?? '其他角色'

/** 记忆层级 → 中文（未知兜底「其他层级」）。 */
export const scopeLabel = (scope: string): string => SCOPE_LABELS[scope] ?? '其他层级'

/** 结果 / 状态 → 中文（未知兜底「其他状态」）。 */
export const outcomeLabel = (outcome: string): string => OUTCOME_LABELS[outcome] ?? '其他状态'

/** 运行状态 → 中文（未知兜底「其他状态」）。 */
export const statusLabel = (status: string): string => OUTCOME_LABELS[status] ?? '其他状态'

/** 工作流类型 key（后端 `workflow_type`）→ 中文展示名。 */
export const WORKFLOW_LABELS: Record<string, string> = {
  sales_ops: '销售线索资质评估',
  support_ops: '客户支持工单分流',
  finance_recon: '财务对账',
}

/** 工作流类型 → 中文（未知兜底「其他工作流」）。 */
export const workflowLabel = (wf: string): string => WORKFLOW_LABELS[wf] ?? '其他工作流'
