import { useCallback, useEffect, useRef, useState } from 'react'
import { useAbortRun, useRunDetail, useWorkspaceCreateTask, useWorkspaceSession } from '../../api/hooks'
import { humanizeError } from '../../api/errors'
import { useSession } from '../../hooks/useSession'
import { roleConfigFor } from '../../home/roleConfig'
import { deliveryState } from '../runs/realRun'
import type { ChatTurn } from './chat'

export type ChatThreadState = {
  turns: ChatTurn[]
  draft: string
  setDraft: (value: string) => void
  submit: (intent: string, context?: Record<string, unknown>) => Promise<boolean>
  continueFrom: (runId: string, intent: string) => void
  busy: boolean
  running: boolean
  stopping: boolean
  stop: () => void
  loading: boolean
  error: string | null
  canExecute: boolean
  roleLabel: string
  sessionId: string | null
  title: string
}

export function conversationStorageKey(userId: string | undefined) {
  return `forgeflow.conversation.${userId ?? 'anonymous'}`
}

export function useChatThread(requestedSessionId?: string, requestedSkillId?: string, requestedResourceId?: string): ChatThreadState {
  const session = useSession()
  const storageKey = conversationStorageKey(session?.userId)
  const [sessionId, setSessionId] = useState<string | null>(() =>
    requestedSessionId || (!requestedSkillId && !requestedResourceId && session ? sessionStorage.getItem(storageKey) : null),
  )
  const [localTurns, setLocalTurns] = useState<ChatTurn[]>([])
  const [draft, setDraft] = useState('')
  const create = useWorkspaceCreateTask()
  const abort = useAbortRun()
  const history = useWorkspaceSession(session ? sessionId : null)
  useEffect(() => {
    if (history.data?.session_id && session) sessionStorage.setItem(storageKey, history.data.session_id)
  }, [history.data?.session_id, session, storageKey])
  const role = roleConfigFor(session?.role)
  const inFlight = useRef(false)
  const sequence = useRef(0)
  const localRunIds = new Set(localTurns.filter((turn) => turn.role === 'agent').map((turn) => turn.runId))
  const restored: ChatTurn[] = (history.data?.runs ?? []).filter((run) => !localRunIds.has(run.run_id))
    .flatMap((run): ChatTurn[] => [
      { id: `${run.run_id}-user`, role: 'user', text: run.intent, runId: run.run_id },
      { id: `${run.run_id}-agent`, role: 'agent', runId: run.run_id },
    ])
  const turns = [...restored, ...localTurns]
  const latestRunId = [...turns].reverse().find((turn) => turn.role === 'agent')?.runId ?? null
  const latest = useRunDetail(latestRunId)
  const running = !!latestRunId && !latest.isError &&
    (!latest.data || deliveryState(latest.data).state === 'waiting')
  const loading = !!sessionId && history.isPending

  const launch = useCallback(async (
    intent: string, context: Record<string, unknown> | undefined, parentRunId?: string,
  ) => {
    const text = intent.trim()
    if (!text || inFlight.current || running || loading) return false
    inFlight.current = true
    const userTurnId = `user-${Date.now()}-${sequence.current++}`
    setLocalTurns((prev) => [...prev, { id: userTurnId, role: 'user', text, runId: null }])
    setDraft('')
    const parent = parentRunId || latestRunId || undefined
    try {
      const handle = await create.mutateAsync({
        intent: text,
        context: { ...context, ...(requestedSkillId ? { skill_ids: [requestedSkillId] } : {}), ...(parent ? { continued_from_run_id: parent } : {}) },
        sessionId: sessionId || undefined,
        parentRunId: parent,
      })
      const acceptedSession = handle.session_id || sessionId || handle.run_id
      sessionStorage.setItem(storageKey, acceptedSession)
      setSessionId(acceptedSession)
      setLocalTurns((prev) => [
        ...prev.map((turn) => turn.id === userTurnId ? { ...turn, runId: handle.run_id } : turn),
        { id: `agent-${handle.run_id}`, role: 'agent', runId: handle.run_id },
      ])
      return true
    } catch {
      setLocalTurns((prev) => prev.filter((turn) => turn.id !== userTurnId))
      setDraft((current) => current || text)
      return false
    } finally {
      inFlight.current = false
    }
  }, [create, latestRunId, loading, running, sessionId, storageKey, requestedSkillId])

  const submit = useCallback(async (intent: string, context?: Record<string, unknown>) => {
    if (!role.canExecute || history.isError) return false
    return launch(intent, context)
  }, [history.isError, launch, role.canExecute])

  const continueFrom = useCallback((runId: string, intent: string) => {
    if (role.canExecute) void launch(intent, undefined, runId)
  }, [launch, role.canExecute])

  const stop = () => {
    if (role.canExecute && running && latestRunId && !abort.isPending) abort.mutate(latestRunId)
  }
  const error = history.isError ? humanizeError(history.error, '对话加载失败，请重试或开始新对话').label
    : abort.isError ? humanizeError(abort.error, '停止失败').label
      : create.isError ? humanizeError(create.error, '发送失败').label : null

  return {
    turns, draft, setDraft, submit, continueFrom, stop, sessionId,
    busy: create.isPending || loading,
    running, stopping: abort.isPending, loading, error,
    canExecute: role.canExecute && !history.isError,
    roleLabel: role.label,
    title: history.data?.title || localTurns.find((turn) => turn.role === 'user')?.text?.slice(0, 40) || '新对话',
  }
}
