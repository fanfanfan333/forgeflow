/**
 * InlineAnswer — 内联打字机答案渲染器（纯展示 `deltas`，E2「收即渲染」）。
 *
 * 纪律（**钉子，勿改**）：
 *   · 直接渲染 `deltas.answerText`（`{text}` 文本节点）——**没有**占位符、**没有**
 *     `debounce` / `throttle` / `setInterval` / CSS `transition` / 打字机动画。
 *     DOM 文本恒等于已到达 delta 的拼接 ⇒ 可被逐帧证伪（第 n 帧 = 第 n-1 帧的前缀扩展）。
 *   · **无 answer 文本时返回 `null`（不渲染任何 DOM 节点）** —— 这是 `/tasks` 落点的硬约束：
 *     打字机插在 `WorkspaceLiveStrip` **上方**，空文本时不得凭空多出一个节点改变既有
 *     DOM 顺序 / 布局（既有 e2e 依赖该列的 DOM 结构）。
 *
 * data-testid（只增不改不删）：`conv-inline-answer`（+ `data-streaming`）。
 * `data-streaming` 诚实反映 token 通道是否活动（mock / deterministic / 历史回放档恒 `false`，
 * **不伪造打字机**，D12）。
 */
import type { RunDeltaState } from '../../hooks/useRunEvents'

export function InlineAnswer({
  deltas,
  streaming,
}: {
  /** token delta 桶（只读；`answerText` 为答案文本）。 */
  deltas: RunDeltaState
  /** 是否正在流式（决定 `data-streaming`）。 */
  streaming: boolean
}) {
  const text = deltas.answerText
  // 无文本 ⇒ 不渲染任何节点（保持既有 DOM 顺序与布局，见文件头 F2 约束）。
  if (!text) return null
  return (
    <div
      className="conv-inline-answer"
      data-testid="conv-inline-answer"
      data-streaming={streaming ? 'true' : 'false'}
    >
      <p className="conv-inline-answer-text">{text}</p>
    </div>
  )
}
