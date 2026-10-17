import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ApiError, request } from '../../api/client'
import { humanizeError } from '../../api/errors'
import { runKeys } from '../../api/queryKeys'

type Recovery = { version: number; status: string; can_resume: boolean; pending_verification: boolean }

export function TaskRecovery({ runId, active = false }: { runId: string; active?: boolean }) {
  const [open, setOpen] = useState(false)
  const cache = useQueryClient()
  const checkpoint = useQuery({
    queryKey: runKeys.recovery(runId),
    queryFn: () => request<Recovery>('/runs/' + runId + '/recovery'),
    enabled: open,
    retry: false,
  })
  const pause = useMutation({
    mutationFn: () => request('/runs/' + runId + '/pause', { method: 'POST' }),
    onSuccess: () => cache.invalidateQueries(),
  })
  const resume = useMutation({
    mutationFn: () => request<{ run_id: string }>('/runs/' + runId + '/resume', {
      method: 'POST', body: JSON.stringify({ version: checkpoint.data!.version }),
    }),
    onSuccess: () => cache.invalidateQueries(),
  })
  return <details className="chat-run-reports" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>任务恢复</summary>
    {active && <button className="chat-text-action" type="button" disabled={pause.isPending || pause.isSuccess} onClick={() => pause.mutate()}>
      {pause.isSuccess ? '正在等待安全停点…' : '暂停并保留进度'}
    </button>}
    {pause.error && <p role="alert">{humanizeError(pause.error).label}</p>}
    {checkpoint.isFetching && <p>正在读取已保存的进度…</p>}
    {checkpoint.error && <p>{checkpoint.error instanceof ApiError && checkpoint.error.status === 404
      ? '这条记录没有可恢复的检查点。' : humanizeError(checkpoint.error).label}</p>}
    {checkpoint.data?.pending_verification && <p>中断操作的结果待核实，暂不自动重试。</p>}
    {checkpoint.data?.can_resume && !resume.data && <>
      <p>继续未完成的步骤，沿用已使用的预算，保留原任务记录。</p>
      <button className="chat-text-action" type="button" disabled={resume.isPending} onClick={() => resume.mutate()}>
        {resume.isPending ? '正在恢复…' : '从已保存进度继续'}
      </button>
    </>}
    {!active && checkpoint.data && !checkpoint.data.can_resume && !checkpoint.data.pending_verification && !resume.data && <p>当前记录没有可安全继续的步骤。</p>}
    {resume.error && <p role="alert">{humanizeError(resume.error).label}</p>}
    {resume.data && <Link to={'/tasks/' + resume.data.run_id}>查看续跑任务</Link>}
  </details>
}
