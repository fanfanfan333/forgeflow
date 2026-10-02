/**
 * ChatExecTrace — 融入聊天的**可展开执行轨迹块**（INC43 / T01，P0-5 / P0-6 / P0-7）。
 *
 * 两态（由 `chat.ts::chatTraceView` 派生，组件不自判状态）：
 *   · 运行中：`<details open>` 默认展开；标题「正在处理你的诉求」+ 复制按钮；
 *     块下方 `chat-exec-live`「正在生成……」。
 *   · 已完成：默认收起为一行摘要
 *     `⌄ 查看执行过程 · N 个步骤 · M 个工具 · K 个 Skill`（可点击展开回顾）。
 *
 * `open` 行为：以派生态为默认值；用户手动的开合记录在组件态里，且**随 state 变更自动
 * 复位**（运行→完成时回到默认收起），无需副作用。
 */
import { useState } from 'react'
import { traceClipboardText, traceStepMarker } from './chat'
import type { ChatTraceView } from './chat'

export function ChatExecTrace({ view }: { view: ChatTraceView }) {
  // 用户手动开合记录；`state` 变化即失效（回到派生态默认值）。
  const [manual, setManual] = useState<{ state: ChatTraceView['state']; open: boolean } | null>(null)
  const isOpen = manual && manual.state === view.state ? manual.open : view.open
  const [copied, setCopied] = useState(false)

  const copy = () => {
    const text = traceClipboardText(view.steps)
    void navigator.clipboard
      ?.writeText(text)
      .then(() => {
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1500)
      })
      // 剪贴板不可用（权限 / 非安全上下文）时静默——不谎报「已复制」。
      .catch(() => undefined)
  }

  const counts = view.summary

  return (
    <div className="chat-exec">
      <details
        className="chat-exec-trace"
        data-testid="chat-exec-trace"
        open={isOpen}
        onToggle={(e) => setManual({ state: view.state, open: e.currentTarget.open })}
      >
        <summary className="chat-exec-trace-summary" data-testid="chat-exec-trace-summary">
          <span className="chat-exec-caret" aria-hidden="true">
            ⌄
          </span>
          <span className="chat-exec-trace-title">{view.title}</span>
          {view.state === 'done' && (
            <span className="chat-exec-trace-counts">
              查看执行过程 · {counts.steps} 个步骤 · {counts.tools} 个工具 · {counts.skills} 个 Skill
            </span>
          )}
          <button
            type="button"
            className="chat-exec-copy"
            data-testid="chat-exec-copy"
            onClick={(e) => {
              // 防止点击复制按钮时把 `<details>` 一并开合。
              e.preventDefault()
              e.stopPropagation()
              copy()
            }}
            title="复制执行过程"
          >
            {copied ? '已复制' : '复制'}
          </button>
        </summary>

        <ol className="chat-exec-trace-steps">
          {view.steps.map((s) => (
            <li
              key={s.key}
              className={`chat-exec-trace-step st-${s.state}`}
              data-testid="chat-exec-trace-step"
              data-state={s.state}
            >
              <span className="chat-exec-step-mark" aria-hidden="true">
                {traceStepMarker(s.state)}
              </span>
              <span className="chat-exec-step-label">{s.label}</span>
            </li>
          ))}
        </ol>
      </details>

      {view.state === 'running' && (
        <p className="chat-exec-live" data-testid="chat-exec-live">
          {view.liveLine}
        </p>
      )}
    </div>
  )
}
