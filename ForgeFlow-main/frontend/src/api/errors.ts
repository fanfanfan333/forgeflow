/**
 * Human-readable error text for the console.
 *
 * The fetch wrapper (`client.ts`) builds `ApiError.message` as
 * `${status} ${statusText}: ${body}` — e.g.
 * `503 Service Unavailable: {"detail":"Database pool not initialised"}`.
 * Rendering that verbatim leaks English backend detail into a Chinese UI.
 *
 * `humanizeError` produces a Chinese summary that *keeps the HTTP status code*
 * and maps a small set of known backend phrases. When nothing matches it falls
 * back to the raw message — it must **never** swallow the real detail into a
 * vague "出错了" (honesty rule). The untouched original is returned as `detail`
 * so the caller can surface it through a `title=` tooltip.
 */
import { ApiError } from './client'

export type HumanizedError = {
  /** Chinese summary (with the status code when known); never a vague placeholder. */
  label: string
  /** The raw, untranslated diagnostic string — surface this in `title`. */
  detail?: string
}

/** Known backend phrases → Chinese. Every phrase that matches is included. */
const PHRASES: ReadonlyArray<readonly [RegExp, string]> = [
  [/database pool not initiali[sz]ed/i, '数据库连接池未初始化'],
  [/missing bearer token/i, '缺少 Bearer 令牌'],
  [/invalid or expired token/i, '令牌无效或已过期'],
  [/not authenticated/i, '未认证'],
  [/unauthorized/i, '未授权'],
  [/forbidden/i, '无权限'],
  [/not found/i, '资源不存在'],
  [/service unavailable/i, '服务不可用'],
  [/bad gateway/i, '网关错误'],
  [/gateway timeout/i, '网关超时'],
  [/internal server error/i, '服务端内部错误'],
  [/connection refused/i, '连接被拒绝'],
  [/connection closed/i, '连接被中断'],
  [/timed? ?out/i, '请求超时'],
  [/failed to fetch|network ?error/i, '网络错误'],
]

/** Translate any known English phrases found in `raw`; `null` when none match. */
function translatePhrases(raw: string): string | null {
  const hits = PHRASES.filter(([re]) => re.test(raw)).map(([, zh]) => zh)
  return hits.length > 0 ? [...new Set(hits)].join(' · ') : null
}

/**
 * Strip the redundant `${status} ${statusText}: ` prefix that `client.ts`
 * prepends, so the summary does not repeat the code twice.
 */
function stripStatusPrefix(raw: string): string {
  const m = raw.match(/^\d{3}\s+[^:]*:\s*([\s\S]*)$/)
  return m ? m[1].trim() : raw
}

/**
 * Humanise an unknown thrown value for display.
 *
 * @param err   Whatever was thrown (ApiError, Error, string, …).
 * @param label Chinese verb phrase for the failure, e.g. 「加载失败」.
 */
export function humanizeError(err: unknown, label = '加载失败'): HumanizedError {
  if (err == null) return { label }
  const detail = err instanceof Error ? err.message : String(err)
  const status = err instanceof ApiError ? err.status : undefined
  // Prefer phrases from the response body (after dropping the status prefix) so
  // a specific cause beats the generic "Service Unavailable" status text.
  const body = status != null ? stripStatusPrefix(detail) : detail
  const tail = (translatePhrases(body) ?? translatePhrases(detail) ?? body).slice(0, 200)

  if (status != null) {
    return { label: tail ? `${label}（${status}）：${tail}` : `${label}（${status}）`, detail }
  }
  return { label: tail ? `${label}：${tail}` : label, detail }
}
