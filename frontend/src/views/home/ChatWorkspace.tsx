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
import { useSession } from '../../hooks/useSession'
import { ChatUserTurn } from './ChatUserTurn'
import { ChatAgentTurn } from './ChatAgentTurn'
import type { ChatThreadState } from './useChatThread'
import '../../styles/chat.css'

export function ChatWorkspace({ thread }: { thread: ChatThreadState }) {
  const session = useSession()
  const role = roleConfigFor(session?.role)

  return (
    <section className="chat-workspace" data-testid="chat-workspace" aria-label="对话工作区">
      <header className="chat-agent-header" data-testid="chat-agent-header">
        <span className="chat-avatar chat-avatar-agent" aria-hidden="true">
          🤖
        </span>
        <span className="chat-agent-meta">
          <span className="chat-agent-name">ForgeFlow Agent</span>
          <span className="chat-agent-sub">随时告诉我你想完成什么</span>
        </span>
      </header>

      <div className="chat-welcome" data-testid="chat-welcome">
        你好，我可以帮你分析、编写、修改、生成文件，也可以调用企业技能和工具完成任务。
      </div>

      <div className="chat-chips">
        {role.suggestions.map((s) =>
          s.to ? (
            <a key={s.label} className="chip" href={s.to}>
              {s.label}
            </a>
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

      <div className="chat-thread" data-testid="chat-thread">
        {thread.turns.map((t) =>
          t.role === 'user' ? (
            <ChatUserTurn key={t.id} turn={t} />
          ) : (
            <ChatAgentTurn key={t.id} turn={t} onContinue={thread.continueFrom} />
          ),
        )}
      </div>

      {thread.error && (
        <p className="chat-error" role="alert">
          发送失败：{thread.error}
        </p>
      )}
    </section>
  )
}
