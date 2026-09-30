/**
 * Theme (light/dark) for the console, persisted to localStorage.
 *
 * Mirrors `views/runs/useViewMode.ts::useViewMode`: the read is wrapped in
 * try/catch so an unavailable storage (private mode) never throws, and an
 * unknown / absent stored value falls back to the safe default `light`.
 *
 * The chosen theme is applied to `document.documentElement.dataset.theme`,
 * which is the single switch `styles/tokens.css` reads (INC32 ADR-07). The
 * initial value is already set synchronously by the `<head>` inline script in
 * `index.html` (anti-FOUC); this hook keeps the React state and the DOM in
 * sync for subsequent toggles.
 */
import { useCallback, useState } from 'react'

export type Theme = 'light' | 'dark'

export const THEME_STORAGE_KEY = 'forgeflow.theme'

function readTheme(): Theme {
  try {
    return window.localStorage.getItem(THEME_STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    // Storage unavailable (private mode) — default to light.
    return 'light'
  }
}

function applyTheme(theme: Theme): void {
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = theme
  }
}

/** Returns the current theme and a setter that applies + persists it. */
export function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(readTheme)
  const set = useCallback((next: Theme) => {
    setTheme(next)
    applyTheme(next)
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch {
      // Storage unavailable — the in-memory state still reflects the choice.
    }
  }, [])
  return [theme, set]
}
