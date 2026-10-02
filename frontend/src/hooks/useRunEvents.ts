/**
 * useRunEvents — TanStack-friendly wrapper around the SSE run stream.
 *
 * Accumulates the run's events into local state (for the timeline) and, on
 * completion, invalidates the metrics/queries so the home page KPIs refresh.
 *
 * Fallback (QA V9): if SSE is unavailable the hook does NOT just report an
 * error — it starts a real polling loop against `GET /runs/{id}` every 2s,
 * synthesises the timeline from the persisted run, and keeps polling until the
 * run reaches a terminal state. The previous version only showed a "degraded"
 * caption with no actual polling.
 *
 * INC-AUDIT —— state is kept **per run id** instead of being reset with a
 * synchronous `setState([])` inside the effect body. That reset was flagged by
 * `react-hooks/set-state-in-effect` (and by React's own guidance): it cascades an
 * extra render on every mount. Keying the bucket makes the reset implicit — a run
 * we have not observed yet simply has no bucket and reads the shared `EMPTY`
 * state, so the effect body never touches React state synchronously. It also
 * means going back to a run you already watched does not blank its timeline.
 */

import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { hubApi, type RunDetail } from '../api/client'
import { subscribeRunEvents, type RunEventPayload } from '../api/sse'

export type RunEventsState = {
  events: RunEventPayload[]
  done: boolean
  error: unknown
}

const POLL_INTERVAL_MS = 2000
const TERMINAL = new Set(['completed', 'failed'])

/** The state of a run we have not observed anything for (stable identity). */
const EMPTY: RunEventsState = { events: [], done: false, error: null }

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
    type: detail.status === 'failed' ? 'run.failed' : 'run.completed',
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
      update((cur) => ({ ...cur, done: true }))
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
        update((cur) => ({ ...cur, events: synth }))
        if (TERMINAL.has(detail.status)) finalize()
      } catch {
        // Transient (network blip / run not visible yet) — keep polling.
      }
    }

    const unsubscribe = subscribeRunEvents(runId, {
      onEvent: (event) => {
        update((cur) => ({ ...cur, events: [...cur.events, event] }))
      },
      onDone: () => {
        // subscribeRunEvents fires onDone right after onError; ignore that
        // spurious "done" so the polling fallback can take over.
        if (failed) return
        finalize()
      },
      onError: (err) => {
        failed = true
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
