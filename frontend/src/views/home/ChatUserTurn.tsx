/**
 * ChatUserTurn — 用户消息气泡（INC43 / T01）。
 *
 * 逐字渲染用户原文（`chat-user-message`），右侧对齐。用户头像用中性人形符号
 * （`aria-hidden`），不加任何色彩语义。
 */
import type { ChatTurn } from './chat'

export function ChatUserTurn({ turn }: { turn: ChatTurn }) {
  return (
    <div className="chat-turn chat-turn-user">
      <div className="chat-bubble chat-bubble-user" data-testid="chat-user-message">
        {turn.text ?? ''}
      </div>
      <span className="chat-avatar chat-avatar-user" aria-hidden="true">
        🧑
      </span>
    </div>
  )
}
