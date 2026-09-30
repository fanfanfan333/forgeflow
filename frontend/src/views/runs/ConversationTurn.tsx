/**
 * ConversationTurn — INC36 L1 ②「用户消息块」（ChatGPT 式对话的起点）。
 *
 * 把用户的**原始意图**（`RunDetail.intent`，后端逐字）渲染成一条「用户」消息：
 * 头像「用户」+ 意图原文 + 提问时间（`created_at` → `fmtTime`）。这是中列对话
 * 时间线的第一段，与后续的 AI 执行摘要 / 结果形成「用户 ← → Agent」的语义分隔。
 *
 * 诚实纪律：意图为空 ⇒ 复用既有诚实文案「（本次运行未记录意图）」（与 `ResultPanel`
 * 的 `result-intent` 同口径）；时间不可解析 ⇒ `fmtTime` 返回「—」。**不编造**。
 *
 * data-testid（只增不改不删）：`conv-user-turn`。
 */
import { fmtTime } from './realRun'

export function ConversationTurn({
  intent,
  createdAt,
}: {
  /** 用户原始意图（`RunDetail.intent`，逐字）。 */
  intent: string
  /** 运行创建时间（`RunDetail.created_at`）。 */
  createdAt: string
}) {
  return (
    <article className="conv-turn" data-testid="conv-user-turn">
      <span className="conv-turn-avatar" aria-hidden="true">
        用户
      </span>
      <div className="conv-turn-body">
        <p className="conv-turn-text">{intent || '（本次运行未记录意图）'}</p>
        <span className="conv-turn-time">{fmtTime(createdAt)}</span>
      </div>
    </article>
  )
}
