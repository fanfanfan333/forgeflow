import { useQuery } from '@tanstack/react-query'
import { request } from '../api/client'
import { useSession } from '../hooks/useSession'
import { roleRank } from '../auth/roleGate'
import { useState } from 'react'
type State = { enabled: boolean; configured: boolean; counts: Record<string, number> }
export function ContextSyncStatus() {
  const session = useSession()
  const [retryError, setRetryError] = useState(false)
  const state = useQuery({ queryKey: ['context-sync', session?.userId], queryFn: () => request<State>('/context/openviking'), enabled: !!session, refetchInterval: 10000 })
  if (!state.data?.enabled || !state.data.configured) return null
  const counts = state.data.counts
  if (![counts.pending, counts.waiting, counts.done].every(Number.isFinite)) return <p role="status">知识同步状态暂不可用</p>
  const pending = counts.pending + counts.waiting
  return <p role="status" style={{ color: 'var(--text-secondary)', fontSize: 13 }}>
    {counts.failed ? `知识同步有 ${counts.failed} 项需要重试` : counts.retry ? `知识同步暂未完成：${counts.retry} 项正在重试` : pending ? `正在整理知识与会话记忆 · ${pending} 项` : '知识与会话记忆已同步'}
    {' · '}{counts.done} 项已完成{!!counts.ready && ` · ${counts.ready} 项可检索，正在更新目录摘要`}
    {!!counts.failed && roleRank(session?.role) >= 1 && <button type="button" onClick={() => { setRetryError(false); request('/context/openviking/retry', { method: 'POST' }).then(() => state.refetch()).catch(() => setRetryError(true)) }}>重试同步</button>}
    {retryError && ' · 重试失败，请稍后再试'}
  </p>
}
