import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { hubApi } from '../api/client'
import { humanizeError } from '../api/errors'
import { useSession } from '../hooks/useSession'
import { roleAtLeast } from '../auth/roleGate'
import { ConfirmDialog } from './ConfirmDialog'

export function DeleteSkill({ id, name, onDeleted, candidate = false }: { id: string; name: string; onDeleted: () => void; candidate?: boolean }) {
  const session = useSession()
  const [open, setOpen] = useState(false)
  const cache = useQueryClient()
  const mutation = useMutation({ mutationFn: () => candidate ? hubApi.archiveCandidate(id) : hubApi.archiveSkill(id), onSuccess: () => {
    void cache.invalidateQueries({ queryKey: ['skills'] }); void cache.invalidateQueries({ queryKey: ['skill-candidates'] }); setOpen(false); onDeleted()
  } })
  if (!roleAtLeast(session?.role, 'manager')) return null
  return <>
    <button type="button" className="conversation-delete" aria-label={`删除技能：${name}`} title="删除技能" onClick={e => { e.stopPropagation(); mutation.reset(); setOpen(true) }}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" /></svg>
    </button>
    <ConfirmDialog open={open} title="删除这个技能？" body={mutation.isError ? humanizeError(mutation.error, '删除失败').label : `“${name}”将从技能库移除并停止使用，历史版本与运行记录保留。`}
      confirmLabel="删除" danger busy={mutation.isPending} onConfirm={() => mutation.mutate()} onCancel={() => setOpen(false)} />
  </>
}
