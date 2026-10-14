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
import { Fragment, useState } from 'react'
import { Link } from '@tanstack/react-router'
import { ChatAvatar } from './ChatAvatar'
import { humanizeError } from '../../api/errors'
import { useRunDetail } from '../../api/hooks'
import { useRunEvents } from '../../hooks/useRunEvents'
import { artifactFinalAnswer, deriveArtifacts, deriveMissingInputs } from '../runs/realRun'
import { ResultMarkdown } from '../runs/ResultMarkdown'
import { chatTraceView } from './chat'
import type { ChatTurn } from './chat'
import { ChatExecTrace } from './ChatExecTrace'
import { ChatArtifactChip } from './ChatArtifactChip'
import { ChatDocDiff } from './ChatDocDiff'
import type { InspectObject } from './inspect'

export function ChatAgentTurn({
  turn,
  onContinue,
  canContinue,
  onInspect,
}: {
  turn: ChatTurn
  onContinue: (runId: string, intent: string) => void
  canContinue: boolean
  /**
   * AgentFlow 2.0 / Phase B / T-B04 —— **可选**「检查对象」回调：点击**消息里的运行**
   * （`检查运行`）/ **轨迹步骤** / **产物 chip**（`详情`）时，把对应可检查对象
   * 交给右栏 Inspector。
   *
   * 🔴 **缺省不传 ⇒ 行为 / 渲染 / props 与既有逐字一致**：不新增任何节点或
   * `data-testid`（新增的 `chat-agent-inspect-run` / `chat-artifact-inspect` 仅在传入
   * `onInspect` 时才渲染；轨迹步骤在未传时保持纯展示、不可点击）。
   */
  onInspect?: (object: InspectObject) => void
}) {
  const runId = turn.runId
  const { events, deltas, done } = useRunEvents(runId)
  const detail = useRunDetail(runId)
  const [copyStatus, setCopyStatus] = useState<string | null>(null)

  const view = chatTraceView(detail.data, events, done)
  const settled = view.state === 'done'

  // 终态使用经过执行结果核对的答复，避免流式草稿掩盖失败结论。
  const reply = (settled && detail.data
    ? (detail.data.llm?.final_answer?.text || artifactFinalAnswer(detail.data) || deltas.answerText)
    : deltas.answerText).trim()
  const artifacts = detail.data ? deriveArtifacts(detail.data) : []
  const files = artifacts.filter(a => a.kind !== 'report_markdown')
  const reports = artifacts.filter(a => a.kind === 'report_markdown')
  const missing = detail.data ? deriveMissingInputs(detail.data) : []
  const copy = async () => {
    try { await navigator.clipboard.writeText(reply); setCopyStatus('已复制') }
    catch { setCopyStatus('复制失败，请选择文字复制') }
  }

  return (
    <div className="chat-turn chat-turn-agent" data-testid="chat-agent-message">
      <ChatAvatar />
      <div className="chat-bubble chat-bubble-agent">
        <ChatExecTrace view={view} runId={runId ?? undefined} onInspect={onInspect} />
        {reply && (
          <div className="chat-agent-reply" data-testid="chat-agent-reply"><ResultMarkdown source={reply} /></div>
        )}
        {settled && detail.data && (
          <details className="chat-run-reports" data-testid="task-acceptance">
            <summary>任务完成情况 · {detail.data.llm?.acceptance?.requirements?.length && detail.data.llm.acceptance.complete ? '已验证' : '未全部验证'}</summary>
            {detail.data.llm?.acceptance?.requirements?.length ? (
              <ul>{detail.data.llm.acceptance.requirements.map(item => (
                <li key={item.id}>
                  {item.status === 'satisfied' ? '已完成' : item.status === 'unsatisfied' ? '未满足' : '未验证'}：{item.description}
                  {item.evidence.length > 0 && <small> · {item.evidence.length} 项执行证据</small>}
                </li>
              ))}</ul>
            ) : <p>这条记录没有逐项需求验收证据，不能据此判断任务已全部完成。</p>}
            {detail.data.llm?.delegations?.map(item => <p key={item.run_id}>子任务 {item.role} · {item.status}</p>)}
            {detail.data.llm?.learning?.status === 'draft' && <p>已整理为技能候选，来源于 {detail.data.llm.learning.verified_sources} 条已验收经验；尚未启用。</p>}
            {detail.data.llm?.learning?.status === 'insufficient' && <p>已保留本次经验，可复用的已验收样本尚不足以整理为技能。</p>}
          </details>
        )}
        {!!detail.data?.llm?.context_sources?.length && <details className="chat-run-reports"><summary>参考资料 · {detail.data.llm.context_sources.length}</summary><ul>{detail.data.llm.context_sources.map((source, index) => <li key={index}>{source.source === 'memory' ? '记忆' : source.source === 'experience' ? '历史经验' : 'OpenViking资料'} · {source.ref_id}</li>)}</ul></details>}
        {detail.data?.llm?.retrieval_trace?.status === 'unavailable' && <p className="chat-loading">外部上下文暂不可用，本次使用本地资料。</p>}
        {detail.data?.llm?.retrieval_trace?.status === 'unconfigured_identity' && <p className="chat-loading">当前用户尚未配置外部上下文，本次使用本地资料。</p>}
        {detail.isError && <p className="chat-error" role="alert">{humanizeError(detail.error, '回复加载失败').label}
          <button className="chat-text-action" onClick={() => void detail.refetch()}>重新加载</button></p>}
        {settled && detail.data?.errors?.length ? <p className="chat-error" role="alert">{detail.data.errors.join('；')}</p> : null}
        {missing.length > 0 && <div className="chat-blockers" role="status">需要补充的信息：
          <ul>{missing.map((item, index) => <li key={index}>{item.reason}</li>)}</ul>
        </div>}
        {settled && !reply && artifacts.length === 0 && !detail.isError &&
          <p className="chat-empty-result">{detail.data?.detail_retained === false ? '本次执行详情未保留，可在执行记录中查看已保存的信息。' : '本次没有返回可展示的回复或文件，请查看执行过程中的说明。'}</p>}
        {artifacts.length > 0 && runId && (
          <div className="chat-artifacts">
            {files.map((a) => (
              // INC43 / T04-C —— 每条产物仍渲染**恰好一个** chip（`chat-artifact-chip`
              // 计数不漂移）；`docx` 产物若带 `diff`，在其 chip **附近**独立渲染
              // `ChatDocDiff`。用 `Fragment` 而非包裹 `<div>`，避免给既有 chip 引入
              // 多余的布局层。
              <Fragment key={a.id}>
                <ChatArtifactChip runId={runId} artifact={a} onContinue={onContinue} onInspect={onInspect} />
                {a.format === 'docx' && a.diff ? <ChatDocDiff diff={a.diff} /> : null}
              </Fragment>
            ))}
            {reports.length > 0 && <details className="chat-run-reports"><summary>运行报告 · {reports.length}</summary>
              {reports.map(a => <ChatArtifactChip key={a.id} runId={runId} artifact={a} onContinue={onContinue} onInspect={onInspect} />)}
            </details>}
          </div>
        )}
        {settled && runId && <div className="chat-reply-actions">
          {reply && <button type="button" className="chat-text-action" onClick={() => void copy()}>{copyStatus || '复制回复'}</button>}
          <Link className="chat-text-action" to={'/tasks/' + runId}>执行记录</Link>
          {/* Phase B / T-B04 —— 「点击消息」→ 右栏检查该运行；仅在传入 `onInspect` 时渲染。 */}
          {onInspect && (
            <button
              type="button"
              className="chat-text-action"
              data-testid="chat-agent-inspect-run"
              onClick={() => onInspect({ kind: 'run', id: runId, label: detail.data?.intent || runId })}
            >
              检查运行
            </button>
          )}
          {canContinue && detail.data && <button type="button" className="chat-text-action" onClick={() => onContinue(runId, detail.data!.intent)}>重新执行</button>}
        </div>}
      </div>
    </div>
  )
}
