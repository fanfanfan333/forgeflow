import { Link } from '@tanstack/react-router'
/**
 * ChatWorkspace — 首页聊天区根（INC43 / T01，P0-2 / P0-3 / P0-4）。
 *
 * 结构（对齐截图界面一）：
 *   `chat-workspace`
 *    ├─ `chat-agent-header`（🤖 低饱和头像 + `ForgeFlow Agent` + 副标题）
 *    ├─ `chat-welcome`（逐字欢迎语）
 *    ├─ 角色建议 chips（`hero-chips` 无 testid，可自由处理；`to` 存在则是站内链接）
 *    └─ `chat-thread`（消息流：user turn / agent turn）
 *
 * 纯编排：turn 数据由 `useChatThread` 持有；本组件只渲染，不订阅、不派生状态。
 */
import { roleConfigFor } from '../../home/roleConfig'
import { useEffect, useRef } from 'react'
import { IconSparkle } from '../../components/icons'
import { useSession } from '../../hooks/useSession'
import { ChatUserTurn } from './ChatUserTurn'
import { ChatAgentTurn } from './ChatAgentTurn'
import type { ChatThreadState } from './useChatThread'
import type { InspectObject } from './inspect'
import '../../styles/chat.css'

export function ChatWorkspace({
  thread,
  onInspect,
}: {
  thread: ChatThreadState
  /**
   * AgentFlow 2.0 / Phase B / T-B04 —— **可选**「检查对象」回调（透传给 Agent turn →
   * 消息 / 轨迹步骤 / 产物 chip）。同 `ChatAgentTurn`：缺省不传 ⇒ 行为与渲染逐字不变。
   */
  onInspect?: (object: InspectObject) => void
}) {
  const session = useSession()
  const role = roleConfigFor(session?.role)
  const threadElement = useRef<HTMLDivElement>(null)
  const hasConversation = thread.turns.length > 0
  useEffect(() => {
    const element = threadElement.current
    const scroller = document.getElementById('main-content')
    if (!element || !scroller) return
    let follow = true
    const onScroll = () => { follow = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 220 }
    const observer = new ResizeObserver(() => {
      if (follow && hasConversation) scroller.scrollTop = scroller.scrollHeight
    })
    observer.observe(element)
    scroller.addEventListener('scroll', onScroll, { passive: true })
    return () => { observer.disconnect(); scroller.removeEventListener('scroll', onScroll) }
  }, [hasConversation])

  return (
    <section className={`chat-workspace ${hasConversation ? 'is-conversation' : 'is-empty'}`} data-testid="chat-workspace" aria-label="对话工作区">
      <header className="chat-agent-header" data-testid="chat-agent-header">
        <span className="chat-avatar chat-avatar-agent" aria-hidden="true">
          <IconSparkle width={22} height={22} />
        </span>
        <span className="chat-agent-meta">
          <span className="chat-agent-name">{hasConversation ? thread.title : '今天，我们完成什么？'}</span>
          <span className="chat-agent-sub">{hasConversation ? 'ForgeFlow · 智能体协作' : '从一个想法、一份文件，开始你的下一项工作'}</span>
        </span>
      </header>

      <div className="chat-welcome" data-testid="chat-welcome" hidden={hasConversation}>
        你好，我可以帮你分析、编写、修改、生成文件，也可以调用企业技能和工具完成任务。
      </div>

      <div className="chat-chips" hidden={hasConversation}>
        {role.suggestions.map((s) =>
          s.to ? (
            <Link key={s.label} className="chip" to={s.to}>
              {s.label}
            </Link>
          ) : (
            <button
              key={s.label}
              type="button"
              className="chip"
              onClick={() => thread.setDraft(s.label)}
              disabled={!thread.canExecute}
            >
              {s.label}
            </button>
          ),
        )}
      </div>

      {thread.loading && <p className="chat-loading" role="status">正在恢复对话…</p>}
      <div ref={threadElement} className="chat-thread" data-testid="chat-thread" aria-busy={thread.running || thread.busy}>
        {thread.turns.map((t) =>
          t.role === 'user' ? (
            <ChatUserTurn key={t.id} turn={t} />
          ) : (
            <ChatAgentTurn key={t.id} turn={t} onContinue={thread.continueFrom} canContinue={thread.canExecute && !thread.running} onInspect={onInspect} />
          ),
        )}
      </div>

      {thread.error && (
        <p className="chat-error" role="alert">
          {thread.error}
        </p>
      )}
    </section>
  )
}
