/**
 * conversation.ts — INC36 会话工作台 L1/L2 的**纯函数派生层**（无 React、无副作用）。
 *
 * 与 `realRun.ts` 家族同款纪律：**只读后端真实字段**，后端没写的就**不产生** ——
 * 绝不臆造、绝不补占位、绝不把「未测量」当 `0`。组件只消费这里派生出的结果，因此
 * 全部可脱离 payload 单测。
 *
 * 四个纯函数：
 *   · `groupRunsByDay(runs, now?)`  —— 左列按真实 `created_at` 分「今天/昨天/更早」。
 *   · `stagesToStepData(stages)`    —— 已终态运行的真实步骤 → `AgentStepData[]`（业务语）。
 *   · `deriveExecCategories(detail)`—— L2「查看执行详情」五类结构化字段（存在性/计数/状态）。
 *   · `deriveSourceRows(detail, limit?)` —— L2「查看来源」行（当前只有真实 URL，诚实）。
 */
import type { RunDetail, RunSummary } from '../../api/client'
import {
  deriveInjectedContext,
  deriveSources,
  detailToStages,
  isPlatformToolId,
  statusLabel,
} from './realRun'
import type { AgentStepData } from './AgentStep'
import { toolKind, toolLabel, type StepStatus } from './toolLabels'
import type {
  DayGroup,
  DayGroupKey,
  ExecCategory,
  ExecCategoryId,
  RunStage,
  RunStageStatus,
} from './types'

/* ------------------------------------------------------------------------- *
 * 左列按天分组
 * ------------------------------------------------------------------------- */

/** 三个桶的固定业务标题（顺序即显示顺序）。 */
const DAY_TITLES: Record<DayGroupKey, string> = {
  today: '今天',
  yesterday: '昨天',
  earlier: '更早',
}

/** 桶的显示顺序（今天 → 昨天 → 更早）。 */
const DAY_ORDER: DayGroupKey[] = ['today', 'yesterday', 'earlier']

/**
 * 把运行列表按**真实 `created_at`** 分入「今天 / 昨天 / 更早」三桶。
 *
 * 纪律：
 *   · 只依据后端真实的 `created_at`；**不可解析**（缺失 / 非法）的运行归入「更早」
 *     （它既不是今天也不是昨天 —— 不猜、不丢）。
 *   · 「今天 / 昨天」边界取**本地日历日**的零点；边界用 `Date(y, m, d-1)` 计算，
 *     故跨月 / 跨年 / 夏令时都不会错位。
 *   · 桶内**保持输入顺序**（后端 `GET /runs` 最新在前）。
 *   · 空桶**不输出**；`runs === []` ⇒ 返回 `[]`（边界）。
 */
export function groupRunsByDay(runs: RunSummary[], now: Date = new Date()): DayGroup[] {
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime()

  const buckets: Record<DayGroupKey, RunSummary[]> = { today: [], yesterday: [], earlier: [] }
  for (const r of runs) {
    const t = Date.parse(r.created_at)
    if (!Number.isNaN(t) && t >= startOfToday) buckets.today.push(r)
    else if (!Number.isNaN(t) && t >= startOfYesterday) buckets.yesterday.push(r)
    else buckets.earlier.push(r)
  }

  return DAY_ORDER.filter((k) => buckets[k].length > 0).map((k) => ({
    key: k,
    title: DAY_TITLES[k],
    runs: buckets[k],
  }))
}

/* ------------------------------------------------------------------------- *
 * 已终态运行的步骤 → 业务语步骤行
 * ------------------------------------------------------------------------- */

/** `RunStageStatus` → `AgentStep` 的四态展示词。 */
function stageStatusToStepStatus(status: RunStageStatus): StepStatus {
  switch (status) {
    case 'done':
      return 'done'
    case 'running':
      return 'running'
    case 'failed':
      return 'error'
    // pending / paused / blocked —— 都**不是**「已完成」，展示为受阻（进行中的单独一态）。
    default:
      return 'blocked'
  }
}

/**
 * 已终态运行的**真实步骤** → `AgentStep` 可渲染的 `AgentStepData[]`（业务语 ✓ 行）。
 *
 * 入参是 `realRun.detailToStages(detail)` 的输出（既有、逐字）。本函数只做**展示转译**：
 *   · 标题取 `stage.name`（已由 `roles.stageNameForTool` 业务语化：角色名 → 后端 note →
 *     真实工具 id）。若它**恰是一个平台工具 id 句型**（`a.b`），改用 `toolLabels.toolLabel`
 *     的业务动作名（`research.search` ⇒「已检索资料」），**绝不**把工程 id 铺在 L1 行上。
 *   · 状态经 `stageStatusToStepStatus` 收敛到四态；`na`（未适用）步骤**整条剔除** ——
 *     它对本任务不适用，既不是成果也不是受阻（与 `realRun.deriveUnrunSteps` 一致）。
 *   · `detail` 取步骤的真实产出摘要（`stage.summary`）；`debug` 保留原始工具 id + 序号，
 *     仅在 `debug` 密度下由 `AgentStep` 渲染。
 */
export function stagesToStepData(stages: RunStage[]): AgentStepData[] {
  return stages
    .filter((s) => s.status !== 'na')
    .map((s) => {
      const title = isPlatformToolId(s.name) ? toolLabel(s.agent, s.status) : s.name
      return {
        key: s.id,
        title,
        status: stageStatusToStepStatus(s.status),
        kind: toolKind(s.agent),
        detail: s.summary,
        debug: `${s.agent || '—'} · #${s.order}`,
      }
    })
}

/* ------------------------------------------------------------------------- *
 * L2「查看执行详情」五类结构化字段
 * ------------------------------------------------------------------------- */

/** 五类的固定顺序 + 展示名。 */
const EXEC_CATEGORY_ORDER: { id: ExecCategoryId; label: string }[] = [
  { id: 'planner', label: 'Planner' },
  { id: 'knowledge', label: 'Knowledge Search' },
  { id: 'skill', label: 'Skill' },
  { id: 'tool', label: 'Tool' },
  { id: 'memory', label: 'Memory' },
]

/** `Tool` 类认定的平台工具 id（其余工具 id 在 `knowledge` / `skill` / `memory` 各自归类）。 */
const TOOL_CATEGORY_TOOLS = new Set(['data.query', 'research.search', 'report.render'])

/** 去重（保序）。 */
function distinct(values: string[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const v of values) {
    const s = (v ?? '').trim()
    if (!s || seen.has(s)) continue
    seen.add(s)
    out.push(s)
  }
  return out
}

/**
 * L2「查看执行详情」的五类结构化字段（**存在性 + 计数 + 状态**）。
 *
 * 数据源（全部为后端真实字段，逐字）：
 *   · `Planner`          ← `detail.plan.steps[]`（`.length`）。
 *   · `Knowledge Search` ← `tool_invocations[] where tool === 'knowledge.search'`。
 *   · `Skill`            ← `codeplane.injected.skills[]` + `tool === 'skill.invoke'` 调用。
 *   · `Tool`             ← `tool_invocations[] where tool ∈ {data.query, research.search, report.render}`。
 *   · `Memory`           ← `codeplane.injected.memory[]` + `tool === 'memory.recall'` 调用。
 *
 * **未命中即 `present=false`**（后端没有这一类就**不渲染**，绝不臆造一条假记录）。
 * `statuses` 为该类调用状态的**业务词**去重列表（`statusLabel`），无则空数组。
 */
export function deriveExecCategories(detail: RunDetail): ExecCategory[] {
  const invocations = detail.tool_invocations ?? []
  const injected = deriveInjectedContext(detail)
  const planSteps = detail.plan?.steps ?? []

  const invocationsOf = (tool: string) => invocations.filter((v) => (v.tool ?? '') === tool)
  const statusesOf = (tool: string): string[] =>
    distinct(invocationsOf(tool).map((v) => statusLabel(v.status)))

  const knowledgeInvs = invocationsOf('knowledge.search')
  const skillInvs = invocationsOf('skill.invoke')
  const memoryInvs = invocationsOf('memory.recall')
  const toolInvs = invocations.filter((v) => TOOL_CATEGORY_TOOLS.has((v.tool ?? '').trim()))

  const counts: Record<ExecCategoryId, { count: number; statuses: string[] }> = {
    planner: { count: planSteps.length, statuses: [] },
    knowledge: { count: knowledgeInvs.length, statuses: statusesOf('knowledge.search') },
    skill: {
      count: injected.skills.length + skillInvs.length,
      statuses: statusesOf('skill.invoke'),
    },
    tool: {
      count: toolInvs.length,
      statuses: distinct(toolInvs.map((v) => statusLabel(v.status))),
    },
    memory: {
      count: injected.memory.length + memoryInvs.length,
      statuses: statusesOf('memory.recall'),
    },
  }

  return EXEC_CATEGORY_ORDER.map(({ id, label }) => ({
    id,
    label,
    present: counts[id].count > 0,
    count: counts[id].count,
    statuses: counts[id].statuses,
  }))
}

/* ------------------------------------------------------------------------- *
 * L2「查看来源」行
 * ------------------------------------------------------------------------- */

/**
 * L2「查看来源」的展示行。
 *
 * **诚实边界（本轮不改后端）**：后端**当前没有**结构化的 `file / sheet / rows`
 * 来源字段（`deriveSources` 只能从真实、非 stub 的工具 payload 里提取 **URL**）。
 * 因此这里**只**产出「来源：<URL>」（+ 可选后端 annotation）—— **绝不**编造
 * `sales_q3.xlsx / Sheet：Sales / Rows：1-248` 这类目录细节（那会是死字段 + 编造风险）。
 *
 * `limit` 省略 ⇒ 返回全部；给出非负整数 ⇒ 只返回前 `limit` 条（供 L2 折叠区展示，
 * 「查看全部来源 ›」再跳 L3 证据 Tab 取全量）。
 */
export function deriveSourceRows(
  detail: RunDetail,
  limit?: number,
): { label: string; detail: string }[] {
  const rows = deriveSources(detail).map((s) => ({ label: s.label, detail: s.detail ?? '' }))
  return typeof limit === 'number' && limit >= 0 ? rows.slice(0, limit) : rows
}

/** 复用：把一次运行装配为它的真实步骤（L1 AI 摘要用），需要时一处取数。 */
export function runStepData(detail: RunDetail): AgentStepData[] {
  return stagesToStepData(detailToStages(detail))
}
