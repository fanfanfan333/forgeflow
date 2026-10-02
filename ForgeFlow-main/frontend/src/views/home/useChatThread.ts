/**
 * useChatThread — 首页「Chat-first Workspace」的聊天会话态（INC43 / T01）。
 *
 * 职责（设计 §2.1-8 / §4.1）：
 *   · 一次提交 = 追加 `{role:'user'}` 与 `{role:'agent', runId}` 两个 turn（**不跳页**，
 *     URL 仍为 `/`）；
 *   · 提交走既有 `useWorkspaceCreateTask`（`POST /workspace/tasks`），复用「拿到句柄
 *     立即返回」的异步通路；句柄不合法（缺 `run_id`）时该 hook 已诚实抛错；
 *   · 「继续修改 / 追问」复用同一通路，带 `parentRunId`（AC-10 口径）；
 *   · 提交失败如实暴露（`humanizeError`），**绝不**伪造成功、**绝不**追加假 agent turn。
 *
 * 本 hook 只持有**组件态**（不入 URL）；run 的订阅 / 派生在 `ChatAgentTurn` 内完成
 * （单 run 单 SSE）。
 */
import { useCallback, useRef, useState } from 'react'
import { useWorkspaceCreateTask } from '../../api/hooks'
import { humanizeError } from '../../api/errors'
import { useSession } from '../../hooks/useSession'
import { roleConfigFor } from '../../home/roleConfig'
import type { ChatTurn } from './chat'

export type ChatThreadState = {
  turns: ChatTurn[]
  /** 输入框草稿（欢迎区建议 chips 可回填）。 */
  draft: string
  setDraft: (value: string) => void
  /** 提交一条新消息（无父 run）。 */
  submit: (intent: string, context?: Record<string, unknown>) => void
  /** 基于某个 run 继续修改 / 追问（携带 `parentRunId`）。 */
  continueFrom: (runId: string, intent: string) => void
  /** 提交中。 */
  busy: boolean
  /** 提交失败的诚实说明（可空）。 */
  error: string | null
  /** 当前身份是否可发起（对齐后端 `execute:workflows`）。 */
  canExecute: boolean
  /** 当前身份的业务名（副标题用）。 */
  roleLabel: string
}

export function useChatThread(): ChatThreadState {
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [draft, setDraft] = useState('')
  const create = useWorkspaceCreateTask()
  const session = useSession()
  const role = roleConfigFor(session?.role)
  // 稳定、无碰撞的 turn id 生成器（时间戳 + 递增序号）。
  const seq = useRef(0)
  const nextId = useCallback((kind: ChatTurn['role']) => `${kind}-${Date.now()}-${seq.current++}`, [])

  const launch = useCallback(
    (intent: string, context: Record<string, unknown> | undefined, parentRunId?: string) => {
      const text = intent.trim()
      if (!text || create.isPending) return
      // 先落用户 turn（逐字原文）—— UI 即时反馈，不等待网络。
      setTurns((prev) => [...prev, { id: nextId('user'), role: 'user', text, runId: null }])
      if (!parentRunId) setDraft('')
      create.mutate(
        { intent: text, context, parentRunId },
        {
          onSuccess: (handle) => {
            // 拿到真实句柄后才追加 agent turn（带 runId）—— 单 run 单 SSE 的订阅点。
            setTurns((prev) => [
              ...prev,
              { id: nextId('agent'), role: 'agent', runId: handle.run_id },
            ])
          },
          // 失败不追加 agent turn；错误经 `error` 诚实暴露。
        },
      )
    },
    [create, nextId],
  )

  const submit = useCallback(
    (intent: string, context?: Record<string, unknown>) => {
      if (!role.canExecute) return
      launch(intent, context)
    },
    [launch, role.canExecute],
  )

  const continueFrom = useCallback(
    (runId: string, intent: string) => {
      if (!role.canExecute) return
      launch(intent, { continued_from_run_id: runId }, runId)
    },
    [launch, role.canExecute],
  )

  const error = create.isError ? humanizeError(create.error, '发送失败').label : null

  return {
    turns,
    draft,
    setDraft,
    submit,
    continueFrom,
    busy: create.isPending,
    error,
    canExecute: role.canExecute,
    roleLabel: role.label,
  }
}
