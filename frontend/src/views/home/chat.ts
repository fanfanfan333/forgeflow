/**
 * chat.ts — 首页 Chat-first Workspace 的**纯函数派生层**（无 React、无副作用）。
 *
 * 纪律（INC43 / T01，项目红线）：
 *   · 本文件**只做展示转译**；所有「运行状态 / 步骤 / 产物」判定**必须** `import`
 *     自既有纯函数（`realRun.ts` / `conversation.ts`），**严禁**在此重写
 *     `status.includes(...)` 之类的第二套判定。
 *   · 步骤业务语走既有 `conversation.stagesToStepData(detailToStages(detail))`
 *     （其内部经 `roles.stageNameForTool` 完成业务语化）。
 *   · 「是否收口」走既有 `realRun.deliveryState(detail).state !== 'waiting'`，
 *     不新造终态词表。
 *   · 无数据 ⇒ 空数组 / 0（用于计数）——不臆造、不补占位。
 */
import type { RunDetail } from '../../api/client'
import type { RunEventPayload } from '../../api/sse'
import { deriveInjectedContext, detailToStages, deliveryState } from '../runs/realRun'
import { stagesToStepData } from '../runs/conversation'
import { stageNameForTool } from '../runs/roles'
import type { StepStatus } from '../runs/toolLabels'

/* ------------------------------------------------------------------------- *
 * 类型（与设计 §3.1 逐字一致）
 * ------------------------------------------------------------------------- */

export type ChatTurnRole = 'user' | 'agent'

export type ChatTurn = {
  id: string
  role: ChatTurnRole
  /** 用户原文（role==='user'），逐字；agent turn 不填。 */
  text?: string
  /** 绑定的真实 run（agent turn）；未拿到句柄前为 null。 */
  runId: string | null
}

/** 轨迹单步展示态（由既有 `StepStatus` 收敛，不新增后端状态值）。 */
export type TraceStepState = 'done' | 'active' | 'failed' | 'blocked' | 'na' | 'pending'

export type TraceStep = {
  key: string
  /** 业务语（`conversation.stagesToStepData` 已处理）。 */
  label: string
  state: TraceStepState
  detail: string
  /** 独立顺序位（行首圆点用）。 */
  order: number
}

export type TraceSummaryCounts = { steps: number; tools: number; skills: number }

export type ChatTraceView = {
  state: 'running' | 'done'
  /** 运行中 true；完成后 false（完成态默认收缩为一行）。 */
  open: boolean
  /** '正在处理你的诉求' | '已完成'。 */
  title: string
  /** chat-exec-live 行（运行中）。 */
  liveLine: string
  steps: TraceStep[]
  summary: TraceSummaryCounts
}

/* ------------------------------------------------------------------------- *
 * 小工具（纯）
 * ------------------------------------------------------------------------- */

/** 去重并保序，跳过空串。 */
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

/** 既有 `StepStatus` → 轨迹展示态。 */
function stepStatusToTraceState(status: StepStatus): TraceStepState {
  switch (status) {
    case 'done':
      return 'done'
    case 'running':
      return 'active'
    case 'error':
      return 'failed'
    case 'blocked':
      return 'blocked'
    default:
      return 'pending'
  }
}

/**
 * 一次运行中被**真实注入 / 调用**的 Skill 名（去重、保序）。
 *
 * 两个既有来源（皆为后端真实字段）：
 *   1. `codeplane.injected.skills[]`（经 `realRun.deriveInjectedContext`）的 `name || id`；
 *   2. `tool_invocations[] where tool === 'skill.invoke'` 的 `payload.skill_name`。
 * 取不到 ⇒ `[]`（不臆造）。此函数是「Skill 数」计数与轨迹条目的**同一来源**，
 * 因此摘要里的 K 恒等于轨迹中「调用「X」Skill」条目数。
 */
export function invokedSkillNames(detail: RunDetail | undefined): string[] {
  if (!detail) return []
  const names: string[] = []
  for (const s of deriveInjectedContext(detail).skills) {
    const n = (s.name || s.id || '').trim()
    if (n) names.push(n)
  }
  for (const inv of detail.tool_invocations ?? []) {
    if ((inv.tool ?? '') !== 'skill.invoke') continue
    const payload = inv.payload
    const n =
      payload && typeof payload === 'object' && typeof (payload as Record<string, unknown>).skill_name === 'string'
        ? ((payload as Record<string, unknown>).skill_name as string).trim()
        : ''
    if (n) names.push(n)
  }
  return distinct(names)
}

/**
 * SSE 已到达、但 `detail` 尚未落库的**进行中**步骤（展示层转译）。
 *
 * 仅用事件流补齐「已在跑、详情还没刷到」的空窗；状态恒为 `active`（未收口），
 * 不做任何状态判定。已落库的步（index < 已落库步数）不再重复；轮询兜底
 * （`useRunEvents` 的 `synthEvents` 只吐 `run.step.done`）时本函数恒返回空。
 */
function inFlightSteps(
  detail: RunDetail | undefined,
  events: RunEventPayload[],
): { key: string; label: string }[] {
  const persisted = (detail?.steps ?? []).length
  const started = new Map<number, string>()
  for (const ev of events) {
    const data = ev.data ?? {}
    if (ev.type === 'run.step' || ev.type === 'run.step.started') {
      const idx = typeof data.index === 'number' ? data.index : started.size
      const tool = typeof data.tool === 'string' ? data.tool : ''
      started.set(idx, tool)
    } else if (ev.type === 'run.step.done' || ev.type === 'run.observation') {
      const idx = typeof data.index === 'number' ? data.index : -1
      if (idx >= 0) started.delete(idx)
    }
  }
  const out: { key: string; label: string }[] = []
  for (const [idx, tool] of started) {
    if (idx < persisted) continue
    out.push({ key: `sse-step-${idx}`, label: stageNameForTool(tool, undefined) || '进行中' })
  }
  return out
}

/* ------------------------------------------------------------------------- *
 * 派生函数（对接口）
 * ------------------------------------------------------------------------- */

/**
 * 由真实 run 详情（+ 未落库的 SSE 步）派生**轨迹步骤**。
 *
 * 1. 主序列 = `stagesToStepData(detailToStages(detail))`（业务语、已完成/进行中/失败/受阻）；
 * 2. 附加「调用「X」Skill」条目（`invokedSkillNames`，去重）；
 * 3. 附加 SSE 未收口步（`inFlightSteps`）。
 */
export function buildTraceSteps(
  detail: RunDetail | undefined,
  events: RunEventPayload[],
): TraceStep[] {
  const steps: TraceStep[] = []
  if (detail) {
    const data = stagesToStepData(detailToStages(detail))
    data.forEach((s, i) => {
      steps.push({
        key: s.key,
        label: s.title,
        state: stepStatusToTraceState(s.status),
        detail: s.detail,
        order: i + 1,
      })
    })
  }
  for (const name of invokedSkillNames(detail)) {
    steps.push({
      key: `skill-${name}`,
      label: `调用「${name}」Skill`,
      state: 'done',
      detail: '',
      order: steps.length + 1,
    })
  }
  for (const f of inFlightSteps(detail, events)) {
    steps.push({ key: f.key, label: f.label, state: 'active', detail: '', order: steps.length + 1 })
  }
  return steps
}

/**
 * 轨迹摘要计数（**全部可复算**，与 `GET /runs/{id}` 逐一相等）：
 *   · `steps  = len(detail.steps)`；
 *   · `tools  = |dedupe(tool_invocations[].tool)|`（忽略空串）；
 *   · `skills = |dedupe(invokedSkillNames(detail))|`（与轨迹「调用「X」Skill」条目同源）。
 */
export function traceSummaryCounts(detail: RunDetail): TraceSummaryCounts {
  const steps = (detail.steps ?? []).length
  const tools = distinct((detail.tool_invocations ?? []).map((v) => (v.tool ?? '').trim())).length
  const skills = invokedSkillNames(detail).length
  return { steps, tools, skills }
}

/**
 * 运行是否**已收口**。唯一判据：
 *   · SSE 已收尾（`done === true`），或
 *   · 既有 `realRun.deliveryState(detail).state !== 'waiting'`。
 * 不新造终态词表（`deliveryState` 即既有唯一事实源）。
 */
export function isTraceSettled(detail: RunDetail | undefined, done: boolean): boolean {
  if (done) return true
  if (!detail) return false
  return deliveryState(detail).state !== 'waiting'
}

/**
 * 组装轨迹的两态视图：运行中默认展开、完成后自动收缩为一行摘要。
 */
export function chatTraceView(
  detail: RunDetail | undefined,
  events: RunEventPayload[],
  done: boolean,
): ChatTraceView {
  const settled = isTraceSettled(detail, done)
  return {
    state: settled ? 'done' : 'running',
    open: !settled,
    title: settled ? '已完成' : '正在处理你的诉求',
    liveLine: settled ? '' : '正在生成……',
    steps: buildTraceSteps(detail, events),
    summary: detail ? traceSummaryCounts(detail) : { steps: 0, tools: 0, skills: 0 },
  }
}

/** 单步行首标记（组件与剪贴板**共用**，保证逐字一致）。 */
export function traceStepMarker(state: TraceStepState): string {
  switch (state) {
    case 'done':
      return '✓'
    case 'active':
      return '●'
    case 'failed':
      return '✕'
    case 'blocked':
      return '⊘'
    case 'na':
      return '—'
    default:
      return '○'
  }
}

/** 轨迹的纯文本形态（复制按钮用），与渲染步骤逐字一致。 */
export function traceClipboardText(steps: TraceStep[]): string {
  return steps.map((s) => `${traceStepMarker(s.state)} ${s.label}`).join('\n')
}
