/**
 * toolLabels — 工具 id → 业务语的**适配层**（INC35 · 规格 §8/§9）。
 *
 * 参考 `browser-use/chat-ui-example` 的 `lib/tool-labels.ts` 思路：把后端原始
 * 工具 id 收敛成「业务动作名（进行时 / 完成时）+ 图标类别」，让**默认密度**下
 * 界面只出现业务语，工程值（工具 id / 事件名 / 参数）只在 `debug` 档出现。
 *
 * 诚实纪律：
 *   · 映射**只**覆盖后端真实存在的工具 id（来源 `forgeflow/runtime/orchestrator.py`
 *     的 `_DEFAULT_STEPS` 与注入步）。未覆盖的 id 落到诚实的兜底文案，
 *     **绝不**猜造一个更漂亮的名字。
 *   · 后端 `run.step` 事件本身带 `note`（平台自撰的业务描述）。有 `note` 时**优先**
 *     用 `note`（平台的原始表述 > 前端改写），映射只用于补「进行时 / 完成时」语态。
 */

/** 业务动作对：`[进行时, 完成时]`。 */
const LABELS: Record<string, [string, string]> = {
  // — 默认计划链（orchestrator.py::_DEFAULT_STEPS）—
  'research.search': ['正在检索资料', '已检索资料'],
  'data.query': ['正在查询数据集', '已查询数据集'],
  'code.run': ['正在执行并验证', '已执行并验证'],
  'report.render': ['正在生成结果', '已生成结果'],
  // — 分析任务注入步 —
  'analysis.profile': ['正在分析数据', '已分析数据'],
  // — 代码任务注入步 —
  'code.execute': ['正在隔离工作区执行', '已在隔离工作区执行'],
  'code.commit': ['正在提交变更', '已提交变更'],
  // — 知识 / 技能 / 记忆（存在时按业务语呈现）—
  'knowledge.search': ['正在检索知识库', '已检索知识库'],
  'memory.recall': ['正在读取记忆', '已读取记忆'],
  'skill.invoke': ['正在调用技能', '已调用技能'],
}

/** 工具 id → 图标类别（决定用哪个线性图标，不含颜色语义）。 */
export type ToolKind = 'research' | 'data' | 'code' | 'report' | 'skill' | 'tool'

const KINDS: Record<string, ToolKind> = {
  'research.search': 'research',
  'knowledge.search': 'research',
  'memory.recall': 'research',
  'data.query': 'data',
  'analysis.profile': 'data',
  'code.run': 'code',
  'code.execute': 'code',
  'code.commit': 'code',
  'report.render': 'report',
  'skill.invoke': 'skill',
}

/** 步骤状态 → 是否算「已结束」（用完成时语态）。 */
function isSettled(status: string): boolean {
  return ['ok', 'completed', 'succeeded', 'done', 'error', 'failed', 'blocked', 'refused', 'unavailable'].includes(
    status.toLowerCase(),
  )
}

export function toolKind(tool: string | undefined): ToolKind {
  if (!tool) return 'tool'
  return KINDS[tool] ?? 'tool'
}

/**
 * 业务动作名。`note` 存在时优先用后端自撰的业务描述（不加语态后缀），
 * 否则用映射表；两者都没有时兜底「正在处理 / 已处理」——**不编造**更具体的名字。
 */
export function toolLabel(
  tool: string | undefined,
  status: string,
  note?: string,
): string {
  const settled = isSettled(status)
  if (note && note.trim()) return note.trim()
  const pair = tool ? LABELS[tool] : undefined
  if (pair) return settled ? pair[1] : pair[0]
  return settled ? '已处理' : '正在处理'
}

/** 步骤状态 → 归一化的展示态。 */
export type StepStatus = 'done' | 'running' | 'error' | 'blocked'

export function stepStatus(raw: string): StepStatus {
  const s = (raw || '').toLowerCase()
  if (['error', 'failed', 'failure', 'refused', 'unavailable'].includes(s)) return 'error'
  if (['blocked', 'skipped'].includes(s)) return 'blocked'
  if (['running', 'pending', ''].includes(s)) return 'running'
  return 'done'
}

/** 状态 → 中文短标签（诚实，不含工程值）。 */
export function stepStatusLabel(status: StepStatus): string {
  switch (status) {
    case 'error':
      return '未成功'
    case 'blocked':
      return '受阻'
    case 'running':
      return '进行中'
    default:
      return '已完成'
  }
}
