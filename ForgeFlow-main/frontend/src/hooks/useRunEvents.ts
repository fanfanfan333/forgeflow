/**
 * useRunEvents — TanStack-friendly wrapper around the SSE run stream.
 *
 * Accumulates the run's events into local state (for the timeline) and mirrors
 * them into the query cache; on completion it invalidates metrics/queries so
 * the home page KPIs refresh.
 *
 * Fallback (QA V9): if SSE is unavailable the hook does NOT just report an
 * error — it starts a real polling loop against `GET /runs/{id}` every 2s,
 * synthesises the timeline from the persisted run, and keeps polling until the
 * run reaches a terminal state. The previous version only showed a "degraded"
 * caption with no actual polling.
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

export function useRunEvents(runId: string | null | undefined): RunEventsState {
  const queryClient = useQueryClient()
  const [events, setEvents] = useState<RunEventPayload[]>([])
  const [done, setDone] = useState(false)
  const [error, setError] = useState<unknown>(null)

  useEffect(() => {
    if (!runId) {
      setEvents([])
      setDone(false)
      setError(null)
      return
    }

    setEvents([])
    setDone(false)
    setError(null)
    queryClient.setQueryData(['run', 'events', runId], [])

    let cancelled = false
    let failed = false
    let pollTimer: ReturnType<typeof setInterval> | null = null

    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
      }
    }

    const finalize = () => {
      if (cancelled) return
      setDone(true)
      stopPolling()
      queryClient.invalidateQueries({ queryKey: ['metrics'] })
      queryClient.invalidateQueries({ queryKey: ['run', 'detail', runId] })
      queryClient.invalidateQueries({ queryKey: ['run', 'events', runId] })
      queryClient.invalidateQueries({ queryKey: ['hub-runs'] })
    }

    const poll = async () => {
      try {
        const detail = await hubApi.runDetail(runId)
        if (cancelled) return
        const synth = synthEvents(detail)
        setEvents(synth)
        queryClient.setQueryData(['run', 'events', runId], synth)
        if (TERMINAL.has(detail.status)) finalize()
      } catch {
        // Transient (network blip / run not visible yet) — keep polling.
      }
    }

    const unsubscribe = subscribeRunEvents(runId, {
      onEvent: (event) => {
        setEvents((prev) => {
          const next = [...prev, event]
          queryClient.setQueryData(['run', 'events', runId], next)
          return next
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
        setError(err)
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

  return { events, done, error }
}
