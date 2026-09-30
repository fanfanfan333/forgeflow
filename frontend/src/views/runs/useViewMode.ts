/**
 * Two-density view mode for the /tasks page, persisted to localStorage.
 *
 * Mirrors the `DocFeedback` storage idiom in `DocsPage.tsx`: the read is wrapped
 * in try/catch (private mode) and an illegal stored value falls back to the safe
 * default `concise`.
 */
import { useCallback, useState } from 'react'
import type { ViewMode } from './types'

const STORAGE_KEY = 'forgeflow.tasks.viewMode'

function readMode(): ViewMode {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'debug' ? 'debug' : 'concise'
  } catch {
    // Storage unavailable (private mode) — default to concise.
    return 'concise'
  }
}

/** Returns the current mode and a setter that also persists it. */
export function useViewMode(): [ViewMode, (mode: ViewMode) => void] {
  const [mode, setMode] = useState<ViewMode>(readMode)
  const set = useCallback((next: ViewMode) => {
    setMode(next)
    try {
      window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Storage unavailable — the in-memory state still reflects the choice.
    }
  }, [])
  return [mode, set]
}
