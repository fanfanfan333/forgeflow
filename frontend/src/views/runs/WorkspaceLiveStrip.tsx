/**
 * WorkspaceLiveStrip — 中列 ②「执行过程」实时流（INC32 / T05 → INC35 升级）。
 *
 * 消费**既有**实时通道（`api/sse.ts::subscribeRunEvents` 经
 * `hooks/useRunEvents.ts::useRunEvents`，**不另写一套 SSE**），把后端的原始事件
 * 翻译为**业务语**步骤（默认 `concise` 密度）。
 *
 * INC35 升级（规格 §7 / §8 / §9）：
 *   · 平铺列表 → **可折叠步骤卡**（`AgentStep`）：状态圆点（✓ / ✕ / ●）+ 业务标题
 *     + 点开才见的详情；工程值（工具 id / 事件名 / 序号）只在 `debug` 档出现。
 *   · 正在执行的那一步改用 **`ThinkingIndicator`**（三点跳动 + 语义化文案），
 *     让用户知道 Agent **此刻在做什么**，而不是一个无限转圈。
 *   · 步骤标题优先用后端 `run.step` 事件自带的 `note`（平台自撰业务描述），
 *     缺 `note` 时经 `toolLabels` 映射为业务动作名；**未覆盖的工具 id 给诚实兜底**，
 *     绝不猜造更好听的名字。
 *
 * 诚实纪律（数据诚实 §4 / AC-18）：
 *   · SSE 订阅失败时 `useRunEvents` **真的**降级为 2s 轮询；本组件同时渲染
 *     `workspace-live-degraded`（「实时连接已断开，正在以轮询方式刷新」）——
 *     **不静默、不假装实时**。
 *   · 事件里没有的东西（工具名等）默认档一律不渲染，绝不猜造。
 *
 * data-testid（只增不改不删，ADR-09）：`workspace-live-strip`（容器）/
 * `workspace-live-step`（单条步骤，由 `AgentStep` 承载）/ `workspace-live-degraded`
 * （降级说明）/ `workspace-thinking`（进行中指示器，新增）。
 */
import { useEffect, useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { RunEventPayload } from '../../api/sse'
import { useRunEvents } from '../../hooks/useRunEvents'
import { AgentStep } from './AgentStep'
import type { AgentStepData } from './AgentStep'
import { ThinkingIndicator } from './ThinkingIndicator'
import { stepStatus, toolKind, toolLabel } from './toolLabels'
import type { ViewMode } from './types'

/** 从 `run.observation` / `run.step.done` 的 observation 里取一句可读摘要。 */
function observationSummary(value: unknown): string {
  if (!value || typeof value !== 'object') return ''
  const o = value as Record<string, unknown>
  for (const key of ['summary', 'message', 'output', 'detail', 'text']) {
    const v = o[key]
    if (typeof v === 'string' && v.trim()) return v.trim()
  }
  return ''
}

/**
 * 把后端事件序列折叠为**步骤列表**。每一步都来自真实事件：
 * 步骤本身由 `run.step` 开启、`run.step.done` 收口；生命周期事件（理解任务 /
 * 制定计划 / 收尾）各占一步。**不合成**后端没发过的事件。
 */
function buildSteps(events: RunEventPayload[]): AgentStepData[] {
  const steps: AgentStepData[] = []
  const drafts: { note?: string; tool?: string }[] = []

  const lifecycle = (ev: RunEventPayload, title: string, debug: string) => {
    steps.push({ key: `life-${ev.seq}`, title, status: 'done', kind: 'tool', detail: '', debug })
    drafts.push({})
  }

  for (const ev of events) {
    const d = ev.data ?? {}
    const tool = typeof d.tool === 'string' ? d.tool : undefined
    const note = typeof d.note === 'string' ? d.note : undefined
    const index = typeof d.index === 'number' ? d.index : null
    const debug = `${ev.type}${tool ? ` ${tool}` : ''} #${ev.seq}`

    switch (ev.type) {
      case 'run.started':
        lifecycle(ev, '理解任务', debug)
        break
      case 'run.plan.started':
      case 'run.plan':
        lifecycle(ev, '制定执行计划', debug)
        break
      case 'run.step': {
        steps.push({
          key: `step-${index ?? ev.seq}`,
          title: toolLabel(tool, 'running', note),
          status: 'running',
          kind: toolKind(tool),
          detail: '',
          debug,
        })
        drafts.push({ note, tool })
        break
      }
      case 'run.step.done': {
        const key = `step-${index ?? ev.seq}`
        const at = steps.findIndex((s) => s.key === key)
        if (at >= 0) {
          const status = stepStatus(String(d.status ?? ''))
          steps[at] = {
            ...steps[at],
            status,
            title: toolLabel(drafts[at]?.tool ?? tool, String(d.status ?? ''), drafts[at]?.note ?? note),
            detail: observationSummary(d.observation) || steps[at].detail,
            debug,
          }
        }
        break
      }
      case 'run.warning': {
        const msg = typeof d.message === 'string' ? d.message : '执行过程中出现提示'
        steps.push({ key: `warn-${ev.seq}`, title: msg, status: 'blocked', kind: 'tool', detail: '', debug })
        drafts.push({})
        break
      }
      case 'run.error': {
        const msg = typeof d.message === 'string' ? d.message : '该步骤未成功'
        steps.push({ key: `err-${ev.seq}`, title: msg, status: 'error', kind: 'tool', detail: '', debug })
        drafts.push({})
        break
      }
      case 'run.awaiting_approval': {
        const msg = typeof d.message === 'string' ? d.message : '等待人工审批'
        steps.push({ key: `appr-${ev.seq}`, title: msg, status: 'blocked', kind: 'tool', detail: '', debug })
        drafts.push({})
        break
      }
      default:
        // 其它事件（observation / reflection / replan / 终态…）不单独占一步：
        // 它们的语义已被所属步骤承载，硬造一行反而会让时间线失真。
        break
    }
  }
  return steps
}

/** 当前正在做什么（ThinkingIndicator 的文案）—— 由**真实事件**推导，不轮播。 */
function currentActivity(events: RunEventPayload[], steps: AgentStepData[]): string {
  const running = [...steps].reverse().find((s) => s.status === 'running')
  if (running) return running.title
  const last = events[events.length - 1]
  if (!last) return '正在准备执行'
  switch (last.type) {
    case 'run.started':
      return '正在理解任务'
    case 'run.plan.started':
    case 'run.plan':
      return '正在制定执行计划'
    case 'run.observation':
      return '正在整理检索结果'
    case 'run.reflection':
    case 'replan':
      return '正在调整执行计划'
    default:
      return '正在执行'
  }
}

export function WorkspaceLiveStrip({ runId, mode }: { runId: string; mode: ViewMode }) {
  const { events, done, error } = useRunEvents(runId)
  const queryClient = useQueryClient()

  // 终态后刷新真实运行详情 / 列表，让父组件据此**切到「最终结果」**（AC-20：不残留
  // 「进行中」）。`useRunEvents` 内部失效的是历史键（`['run','detail',id]` /
  // `['hub-runs']`），与本页实际使用的 `['hub', ...]` 不一致，故在此补一次真实刷新。
  useEffect(() => {
    if (done) queryClient.invalidateQueries({ queryKey: ['hub'] })
  }, [done, queryClient])

  const steps = useMemo(() => buildSteps(events), [events])
  // 正在执行的那一步用 ThinkingIndicator 呈现，不进列表（避免同一件事画两遍）。
  const settled = steps.filter((s) => s.status !== 'running')
  const activity = currentActivity(events, steps)

  return (
    <section className="live-strip" data-testid="workspace-live-strip" aria-label="执行过程">
      <div className="live-strip-head">
        <span>执行过程</span>
        {!done && (
          <span className="live-badge">
            <i className="dot live" />
            进行中
          </span>
        )}
      </div>

      {/* AC-18 — 订阅失败时真的降级为轮询（`useRunEvents` 的 poll 路径），在此如实说明。 */}
      {error != null && (
        <p className="live-degraded" data-testid="workspace-live-degraded" role="status">
          实时连接已断开，正在以轮询方式刷新
        </p>
      )}

      {settled.length > 0 && (
        <ol className="agent-steps">
          {settled.map((s) => (
            <AgentStep key={s.key} step={s} mode={mode} />
          ))}
        </ol>
      )}

      {!done ? (
        <ThinkingIndicator label={activity} />
      ) : (
        settled.length === 0 && <p className="live-empty">本次执行没有产生可展示的步骤。</p>
      )}
    </section>
  )
}
