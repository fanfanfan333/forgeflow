/**
 * ChatAgentTurn — Agent 的一条消息（INC43 / T01，P0-5 / P0-7 / P0-8）。
 *
 * 结构：Agent 头像 → 气泡（执行轨迹块 + Agent 回复正文 + 产物 chip）。
 *
 * 数据来源（**零新增端点**，全部复用既有 hook / 纯函数）：
 *   · `useRunEvents(runId)` —— **单 run 单 SSE**（唯一订阅点）；
 *   · `useRunDetail(runId)` —— `GET /runs/{id}`（步骤 / 计数 / 产物）；
 *   · `chat.ts::chatTraceView` —— 两态轨迹视图（状态判定全在既有纯函数内）。
 *
 * 回复正文口径（与 `InlineSessionPanel` 同源，不另造）：优先 SSE `deltas.answerText`；
 * 终态且无 SSE 文本时回落到 `realRun.artifactFinalAnswer(detail)`（产物里的「最终答案」）。
 */
import { Fragment } from 'react'
import { useRunDetail } from '../../api/hooks'
import { useRunEvents } from '../../hooks/useRunEvents'
import { artifactFinalAnswer, deriveArtifacts } from '../runs/realRun'
import { chatTraceView } from './chat'
import type { ChatTurn } from './chat'
import { ChatExecTrace } from './ChatExecTrace'
import { ChatArtifactChip } from './ChatArtifactChip'
import { ChatDocDiff } from './ChatDocDiff'

export function ChatAgentTurn({
  turn,
  onContinue,
}: {
  turn: ChatTurn
  onContinue: (runId: string, intent: string) => void
}) {
  const runId = turn.runId
  const { events, deltas, done } = useRunEvents(runId)
  const detail = useRunDetail(runId)

  const view = chatTraceView(detail.data, events, done)
  const settled = view.state === 'done'

  // 回复正文：SSE 打字机文本 → 终态产物「最终答案」兜底（逐字，不拼装）。
  const reply = (deltas.answerText || (settled && detail.data ? artifactFinalAnswer(detail.data) : '')).trim()
  const artifacts = detail.data ? deriveArtifacts(detail.data) : []

  return (
    <div className="chat-turn chat-turn-agent" data-testid="chat-agent-message">
      <span className="chat-avatar chat-avatar-agent" aria-hidden="true">
        🤖
      </span>
      <div className="chat-bubble chat-bubble-agent">
        <ChatExecTrace view={view} />
        {reply && (
          <p className="chat-agent-reply" data-testid="chat-agent-reply">
            {reply}
          </p>
        )}
        {artifacts.length > 0 && runId && (
          <div className="chat-artifacts">
            {artifacts.map((a) => (
              // INC43 / T04-C —— 每条产物仍渲染**恰好一个** chip（`chat-artifact-chip`
              // 计数不漂移）；`docx` 产物若带 `diff`，在其 chip **附近**独立渲染
              // `ChatDocDiff`。用 `Fragment` 而非包裹 `<div>`，避免给既有 chip 引入
              // 多余的布局层。
              <Fragment key={a.id}>
                <ChatArtifactChip runId={runId} artifact={a} onContinue={onContinue} />
                {a.format === 'docx' && a.diff ? <ChatDocDiff diff={a.diff} /> : null}
              </Fragment>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
