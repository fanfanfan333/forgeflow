/**
 * useRunEvents — TanStack-friendly wrapper around the SSE run stream.
 *
 * Accumulates the run's events into local state (for the timeline) and, on
 * completion, invalidates the metrics/queries so the home page KPIs refresh.
 *
 * INC-INLINE-STREAMING —— 与事件时间线**分离**的 token delta 桶（C5）：
 *   · `events[]` 只装**步骤 / 生命周期**事件（格式与顺序不变）；`run.token` 增量
 *     **绝不**进 `events[]`（否则几千条 fragment 会把 `WorkspaceLiveStrip.buildSteps`
 *     的折叠拖垮）。token / 轮归类帧进独立的 `deltas`（`RunDeltaState`）。
 *   · `deltas` 的更新是**纯字符串拼接**（`prev.text + fragment`）——**没有**任何
 *     `debounce` / `throttle` / `setInterval` / 打字机动画 / CSS 补间。这是 E2
 *     「收即渲染」可判定的前提：第 n 帧渲染的文本恒等于前 n 帧 fragment 的拼接，
 *     且是第 n-1 帧的前缀扩展。
 *
 * Fallback (QA V9): if SSE is unavailable the hook does NOT just report an
 * error — it starts a real polling loop against `GET /runs/{id}` every 2s,
 * synthesises the timeline from the persisted run, and keeps polling until the
 * run reaches a terminal state.
 *
 * INC-AUDIT —— state is kept **per run id** instead of being reset with a
 * synchronous `setState([])` inside the effect body. That reset was flagged by
 * `react-hooks/set-state-in-effect` (and by React's own guidance): it cascades an
 * extra render on every mount. Keying the bucket makes the reset implicit — a run
 * we have not observed yet simply has no bucket and reads the shared `EMPTY`
 * state, so the effect body never touches React state synchronously. It also
 * means going back to a run you already watched does not blank its timeline.
 *
 * F3-3a（降级诚实，**钉子**）—— SSE 失败走 2s 轮询时，轮询只会**替换** `events`
 * （`synthEvents`），**绝不清空 / 绝不伪造** `deltas`：已累积的 token 文本原样保留。
 */

import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { hubApi, type RunDetail } from '../api/client'
import { subscribeRunEvents, type RunEventPayload } from '../api/sse'

/** 一轮模型调用末的归类（`run.turn.channel`）。 */
export type TurnChannel = 'pending' | 'narration' | 'answer' | 'suppressed'

/** 单轮（`turn`）已到达增量的状态。 */
export type TurnDelta = {
  /** 该轮已到达增量的**拼接**（= 已外发 fragment 的并集，**不做任何补间**）。 */
  text: string
  /** 该轮当前的通道归类（流式中为 `pending`，轮末据 `run.turn` 归类）。 */
  channel: TurnChannel
  /** 该轮是否已收口（收到过 `run.turn`）。 */
  done: boolean
  /** 是否因 abort / 失败被迫中断（`run.turn.interrupted`）。 */
  interrupted: boolean
}

/** 与 `events[]` **分离**的 token 增量桶（按 run × turn 累积文本）。 */
export type RunDeltaState = {
  turns: Record<number, TurnDelta>
  order: number[]
  /** 收敛轮（`channel === 'answer'`）的文本；无则取最近 `pending` 轮的非空文本。 */
  answerText: string
  /** 是否正处于 token 通道活动期（收到过 token 帧且未到终态）。 */
  streaming: boolean
  /** 是否已中断（`run.turn.interrupted` 或终态 `run.aborted`）。 */
  interrupted: boolean
}

export type RunEventsState = {
  /** 步骤 / 生命周期事件（**不含** token 帧，C5）。 */
  events: RunEventPayload[]
  /** 独立的 token delta 桶（按 run × turn 累积文本）。 */
  deltas: RunDeltaState
  done: boolean
  error: unknown
}

const POLL_INTERVAL_MS = 2000
// FD-2 —— 后端**全部**终态都算「已收尾」，否则 aborted/interrupted/rejected 的 run
// 走轮询兜底时**永不收尾**（面板会拿到一个假的「completed」）。
const TERMINAL = new Set(['completed', 'failed', 'aborted', 'interrupted', 'rejected'])
/** token 通道帧：只进 `deltas`，**绝不**进 `events[]`。 */
const TOKEN_EVENTS = new Set(['run.token', 'run.turn'])
/** 终态事件：据其把 `deltas.streaming` 收起；`run.aborted` 同时置位 `interrupted`。 */
const TERMINAL_EVENTS = new Set(['run.completed', 'run.failed', 'run.aborted'])

/**
 * FD-2（数据诚实）——把后端终态**诚实**映射为事件名：**不得**把非成功终态说成
 * `run.completed`（轮询兜底合成时间线时用；否则 `aborted`/`interrupted`/`rejected`
 * 会被谎报成 completed，半截文本得不到任何警示）。
 */
function terminalEventType(status: string): string {
  if (status === 'failed') return 'run.failed'
  if (status === 'aborted' || status === 'interrupted' || status === 'rejected') return 'run.aborted'
  return 'run.completed'
}

/** 尚未观察到任何状态的 run（稳定身份）。 */
const EMPTY_DELTAS: RunDeltaState = {
  turns: {},
  order: [],
  answerText: '',
  streaming: false,
  interrupted: false,
}
const EMPTY: RunEventsState = { events: [], deltas: EMPTY_DELTAS, done: false, error: null }

/** How many runs' timelines to keep before the oldest bucket is dropped. */
const MAX_TRACKED_RUNS = 8

/** Reconstruct a timeline from the persisted run (polling fallback). */
function synthEvents(detail: RunDetail): RunEventPayload[] {
  const ts = detail.created_at ?? new Date().toISOString()
  const out: RunEventPayload[] = [
    { run_id: detail.run_id, type: 'run.started', data: { intent: detail.intent }, seq: 0, ts },
  ]
  ;(detail.steps ?? []).forEach((s, i) => {
    out.push({
      run_id: detail.run_id,
      type: 'run.step.done',
      data: { index: s.index ?? i, tool: s.tool, status: s.status ?? 'ok' },
      seq: out.length,
      ts,
    })
  })
  out.push({
    run_id: detail.run_id,
    type: terminalEventType(detail.status),
    data: {
      status: detail.status,
      outcome: detail.outcome,
      steps: (detail.steps ?? []).length,
      errors: detail.errors,
    },
    seq: out.length,
    ts,
  })
  return out
}

/**
 * 收敛轮的答案文本：优先 `channel === 'answer'` 的轮（取最后一个），否则取最近一个
 * **非空**的临时轮（`pending` **或** `narration`）。
 *
 * FD-4 —— **不排除 `narration`**：某轮流式中是 `pending`（其文本被当作兜底逐字打出），
 * 轮末归类为 `narration` 后若立刻被排除，`answerText` 会回落到空 ⇒ 刚打出来的字「消失」
 * （DESIGN §P1 自称"不闪烁"，旧实现与之不符）。`pending` / `narration` 都只是**临时**
 * 兜底，answer 轮一出现即被其取代。
 */
function pickAnswerText(turns: Record<number, TurnDelta>, order: number[]): string {
  let answer = ''
  let provisional = ''
  for (const t of order) {
    const d = turns[t]
    if (!d || !d.text) continue
    if (d.channel === 'answer') answer = d.text
    else if (d.channel === 'pending' || d.channel === 'narration') provisional = d.text
  }
  return answer || provisional
}

/** 把一帧 token 通道事件（`run.token` / `run.turn`）并入 `deltas`（**纯拼接**，无补间）。 */
function applyTokenEvent(deltas: RunDeltaState, event: RunEventPayload): RunDeltaState {
  const data = event.data ?? {}
  const turn = typeof data.turn === 'number' ? data.turn : 0
  const prev = deltas.turns[turn]
  const order = deltas.order.includes(turn) ? deltas.order : [...deltas.order, turn]

  if (event.type === 'run.token') {
    // ⚠️ 纯字符串拼接 —— 这是 E2「收即渲染」可判定的核心不变量（勿改）。
    const fragment = typeof data.fragment === 'string' ? data.fragment : ''
    const next: TurnDelta = {
      text: (prev?.text ?? '') + fragment,
      channel: prev?.channel ?? 'pending',
      done: prev?.done ?? false,
      interrupted: prev?.interrupted ?? false,
    }
    const turns = { ...deltas.turns, [turn]: next }
    return { ...deltas, turns, order, answerText: pickAnswerText(turns, order), streaming: true }
  }

  // `run.turn` —— 轮末归类：`text` 是该轮**权威全文**（可覆盖流式累积的同一轮文本）。
  const channel = (typeof data.channel === 'string' ? data.channel : 'pending') as TurnChannel
  const text = typeof data.text === 'string' ? data.text : prev?.text ?? ''
  const interrupted = data.interrupted === true
  const next: TurnDelta = { text, channel, done: true, interrupted }
  const turns = { ...deltas.turns, [turn]: next }
  return {
    ...deltas,
    turns,
    order,
    answerText: pickAnswerText(turns, order),
    streaming: true,
    interrupted: deltas.interrupted || interrupted,
  }
}

/**
 * F3-3b（裁定高于 DESIGN §3.5 的「仅当无 token 帧时兜底」措辞）——
 * `run.final_answer` 到达时**始终**以其 `text` 整段校正 `answerText`（**覆盖**）。
 *
 * 理由：`run.final_answer.text` 是后端 **verbatim 权威全文**，且**流式文本恒为其前缀**
 * （后端保证二者同源于 `_content_text(ai)`）⇒
 *   ① 流式完整时该覆盖是**恒等操作**（DOM 不跳变）；
 *   ② 断线导致半截时该覆盖是**前缀扩展式修复**（把截断答案补全）。
 * 因此它**同时**满足 E2「收即渲染」（服务端确已发出该全文，不是伪造）与 AC-3
 * 「前缀单调增长」（覆盖后仍是原串的前缀扩展）。
 *
 * 落点选择：优先写入既有的 `answer` 轮 → 否则写入最近的 token 轮（真实流式的**同一轮**，
 * 覆盖即前缀扩展）→ 无任何 token 轮（mock / deterministic / 历史回放档）时**新建**一个
 * 承载轮：此时 `answerText` **只能**由 `run.final_answer` 提供（**不伪造打字机**，D12）。
 */
function applyFinalAnswer(deltas: RunDeltaState, text: string, iteration: unknown): RunDeltaState {
  const answers = deltas.order.filter((t) => deltas.turns[t]?.channel === 'answer')
  const target =
    answers.length > 0
      ? answers[answers.length - 1]
      : deltas.order.length > 0
        ? deltas.order[deltas.order.length - 1]
        : typeof iteration === 'number'
          ? iteration
          : 0
  const order = deltas.order.includes(target) ? deltas.order : [...deltas.order, target]
  const turns = {
    ...deltas.turns,
    [target]: {
      text,
      channel: 'answer' as TurnChannel,
      done: true,
      // FD-1 —— 权威全文已到达 ⇒ 该轮不再「不完整」（校正即「已完整」），清除粘性标记。
      interrupted: false,
    },
  }
  return {
    ...deltas,
    turns,
    order,
    answerText: text,
    // FD-1 —— 重置该轮标记后，按**各轮重算**全局位：只有仍有轮处于 interrupted 才置位
    // （原先 `interrupted` 是粘性 OR，永不复位 ⇒「完整答案 + 已中断」的自相矛盾会常驻）。
    // `run.aborted` 仍由 `TERMINAL_EVENTS` 分支置位；同一 run 上二者不同时出现，重算安全。
    interrupted: order.some((t) => turns[t]?.interrupted === true),
  }
}

/**
 * Write `next` for `key`, then drop the oldest buckets past `MAX_TRACKED_RUNS`.
 * The written key is always re-inserted last so a long-lived run is never the
 * one evicted while it is still being watched.
 */
function put(
  prev: Record<string, RunEventsState>,
  key: string,
  next: RunEventsState,
): Record<string, RunEventsState> {
  const merged: Record<string, RunEventsState> = {}
  for (const [k, v] of Object.entries(prev)) {
    if (k !== key) merged[k] = v
  }
  merged[key] = next
  const keys = Object.keys(merged)
  if (keys.length > MAX_TRACKED_RUNS) {
    for (const stale of keys.slice(0, keys.length - MAX_TRACKED_RUNS)) delete merged[stale]
  }
  return merged
}

export function useRunEvents(runId: string | null | undefined): RunEventsState {
  const queryClient = useQueryClient()
  const [store, setStore] = useState<Record<string, RunEventsState>>({})
  const state = runId ? store[runId] ?? EMPTY : EMPTY

  useEffect(() => {
    if (!runId) return

    let cancelled = false
    let failed = false
    let pollTimer: ReturnType<typeof setInterval> | null = null

    /** Patch this run's bucket. Only ever called from async callbacks. */
    const update = (fn: (cur: RunEventsState) => RunEventsState) => {
      setStore((prev) => put(prev, runId, fn(prev[runId] ?? EMPTY)))
    }

    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
      }
    }

    const finalize = () => {
      if (cancelled) return
      // 终态：收起 `streaming`（token 通道活动期结束），但**保留**已累积的 `deltas`。
      update((cur) => ({ ...cur, done: true, deltas: { ...cur.deltas, streaming: false } }))
      stopPolling()
      queryClient.invalidateQueries({ queryKey: ['metrics'] })
      queryClient.invalidateQueries({ queryKey: ['run', 'detail', runId] })
      queryClient.invalidateQueries({ queryKey: ['hub-runs'] })
    }

    const poll = async () => {
      try {
        const detail = await hubApi.runDetail(runId)
        if (cancelled) return
        const synth = synthEvents(detail)
        // F3-3a —— 轮询只**替换**事件时间线；`deltas` **原样保留**（不随 events 被清空/伪造）。
        update((cur) => ({ ...cur, events: synth }))
        if (TERMINAL.has(detail.status)) finalize()
      } catch {
        // Transient (network blip / run not visible yet) — keep polling.
      }
    }

    const unsubscribe = subscribeRunEvents(runId, {
      onEvent: (event) => {
        update((cur) => {
          if (TOKEN_EVENTS.has(event.type)) {
            // C5 —— token 增量**绝不**进 `events[]`。
            return { ...cur, deltas: applyTokenEvent(cur.deltas, event) }
          }
          const events = [...cur.events, event]
          let deltas = cur.deltas
          if (event.type === 'run.final_answer') {
            const text = typeof event.data.text === 'string' ? event.data.text : ''
            // F3-3b —— 始终以 verbatim 全文校正（覆盖）。
            deltas = applyFinalAnswer(deltas, text, event.data.iteration)
          }
          if (TERMINAL_EVENTS.has(event.type)) {
            const aborted = event.type === 'run.aborted'
            deltas = { ...deltas, streaming: false, interrupted: deltas.interrupted || aborted }
          }
          return { ...cur, events, deltas }
        })
      },
      onDone: () => {
        // subscribeRunEvents fires onDone right after onError; ignore that
        // spurious "done" so the polling fallback can take over.
        if (failed) return
        finalize()
      },
      onError: (err) => {
        failed = true
        // F3-3a —— 记下错误但**不清空** `deltas`（已累积的 token 文本必须原样保留）。
        update((cur) => ({ ...cur, error: err }))
        if (!pollTimer) {
          void poll()
          pollTimer = setInterval(poll, POLL_INTERVAL_MS)
        }
      },
    })

    return () => {
      cancelled = true
      stopPolling()
      unsubscribe()
    }
  }, [runId, queryClient])

  return state
}
