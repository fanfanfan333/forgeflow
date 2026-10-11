import { useState } from 'react'
import { useNavigate } from '@tanstack/react-router'
import { useDeleteSession } from '../api/hooks'
import { humanizeError } from '../api/errors'
import { useSession } from '../hooks/useSession'
import { roleRank } from '../auth/roleGate'
import { conversationStorageKey } from '../views/home/useChatThread'
import { requestNewChat } from '../views/home/navigation'
import { ConfirmDialog } from './ConfirmDialog'

export function DeleteConversation({ sessionId, title }: { sessionId: string; title: string }) {
  const session = useSession()
  const mutation = useDeleteSession()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  if (roleRank(session?.role) < 1) return null
  return <>
    <button type="button" className="conversation-delete" aria-label={`删除对话：${title}`} title="删除对话"
      onClick={() => { mutation.reset(); setOpen(true) }} disabled={mutation.isPending}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
        <path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" />
      </svg>
    </button>
    <ConfirmDialog open={open} title="删除这段对话？"
      body={mutation.isError ? humanizeError(mutation.error, '删除失败，请重试').label : `“${title}”及其对话记录将从历史中移除。`}
      confirmLabel="删除" danger busy={mutation.isPending} onCancel={() => setOpen(false)}
      onConfirm={() => mutation.mutate(sessionId, { onSuccess: () => {
        setOpen(false)
        const key = conversationStorageKey(session?.userId)
        const current = sessionStorage.getItem(key) === sessionId || location.pathname === `/chat/${encodeURIComponent(sessionId)}`
        if (current) { sessionStorage.removeItem(key); requestNewChat(); void navigate({ to: '/' }) }
      } })} />
  </>
}
