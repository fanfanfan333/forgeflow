/**
 * SSE client for the AgentFlow run event stream.
 *
 * Uses `fetch` + `ReadableStream` (not `EventSource`) because the RBAC
 * middleware requires an `Authorization: Bearer` header, which `EventSource`
 * cannot send. Falls back gracefully: on any non-OK / network error the caller
 * is told it finished, and the view degrades to polling (see useRunEvents).
 */

import { getToken } from './client'

export type RunEventPayload = {
  run_id: string
  type: string
  data: Record<string, unknown>
  seq: number
  ts: string
}

export type RunEventHandlers = {
  onEvent?: (event: RunEventPayload) => void
  onDone?: () => void
  onError?: (error: unknown) => void
}

/**
 * Subscribe to `/api/runs/{runId}/events`.
 * Returns an unsubscribe function that aborts the underlying request.
 */
export function subscribeRunEvents(
  runId: string,
  handlers: RunEventHandlers,
  options: { signal?: AbortSignal } = {},
): () => void {
  const controller = new AbortController()
  const signal = options.signal ?? controller.signal
  const token = getToken()

  void (async () => {
    try {
      const res = await fetch(`/api/runs/${runId}/events`, {
        headers: token ? { authorization: `Bearer ${token}` } : {},
        signal,
      })
      if (!res.ok || !res.body) {
        handlers.onError?.(new Error(`SSE request failed: ${res.status}`))
        handlers.onDone?.()
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let boundary = buffer.indexOf('\n\n')
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          for (const line of frame.split('\n')) {
            const trimmed = line.trim()
            if (!trimmed.startsWith('data:')) continue
            const payload = trimmed.slice(5).trim()
            if (payload === '[DONE]') {
              handlers.onDone?.()
              return
            }
            try {
              handlers.onEvent?.(JSON.parse(payload) as RunEventPayload)
            } catch {
              // Malformed frame — skip it rather than killing the stream.
            }
          }
          boundary = buffer.indexOf('\n\n')
        }
      }
      handlers.onDone?.()
    } catch (error) {
      if ((error as { name?: string }).name === 'AbortError') return
      handlers.onError?.(error)
      handlers.onDone?.()
    }
  })()

  return () => controller.abort()
}
