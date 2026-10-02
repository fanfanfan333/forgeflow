/**
 * ChatDocDiff — Agent 消息内的**文档修改摘要**（INC43 / T04-C，P0-8 扩展）。
 *
 * 职责：把 `GET /runs/{id}` 里 `format === "docx"` 产物上新增的 `diff`
 * （`DocDiff`）渲染成「修改 N 处 / 新增 X / 删除 Y / 数字变化 Z」四项。
 *
 * 诚实纪律（项目红线，与 `realRun.ts::measuredMs` 同口径）：
 *   · 四个数字**逐字来自后端**，一个都不加工；
 *   · 字段**缺失**（`undefined` / `null`）⇒ 该渲染「—」，**绝不**用 `0` 兜底
 *     ——那会把「未测量」谎报成「没有变化」；
 *   · `0` **只**在后端确有一个等于 `0` 的字段时才会出现（真实零值 ≠ 缺失）。
 *
 * 本组件**不发任何请求**（产物与 diff 已随 `GET /runs/{id}` 返回，`ChatAgentTurn`
 * 内的 `useRunDetail` 已取回）；纯展示，无副作用。
 */
import type { DocDiff } from '../../api/client'

/**
 * 单个计数的展示串：真实有限数逐字转字符串；缺失 / 非法值 ⇒「—」。
 *
 * ⚠️ 参数类型放宽到 `number | null | undefined`：`DocDiff` 的四字段在**类型**上是
 * 必填数字，但运行时可能缺键（老后端 / 部分写入），故此处按运行时真相防御。
 * **绝不** `?? 0`。
 */
function diffCount(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '—'
}

export function ChatDocDiff({ diff }: { diff: DocDiff }) {
  return (
    <div className="chat-doc-diff" data-testid="chat-doc-diff">
      <span className="chat-doc-diff-title">文档修改</span>
      <ul className="chat-doc-diff-items">
        <li className="chat-doc-diff-item" data-testid="chat-doc-diff-modified">
          {`修改 ${diffCount(diff.modified)} 处`}
        </li>
        <li className="chat-doc-diff-item" data-testid="chat-doc-diff-added">
          {`新增 ${diffCount(diff.added)}`}
        </li>
        <li className="chat-doc-diff-item" data-testid="chat-doc-diff-removed">
          {`删除 ${diffCount(diff.removed)}`}
        </li>
        <li className="chat-doc-diff-item" data-testid="chat-doc-diff-numeric">
          {`数字变化 ${diffCount(diff.numeric_changes)}`}
        </li>
      </ul>
    </div>
  )
}
