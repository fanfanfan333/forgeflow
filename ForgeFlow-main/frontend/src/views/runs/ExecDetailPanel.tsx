/**
 * ExecDetailPanel — INC36 L2「查看执行详情」的结构化字段（挂进既有
 * `workspace-exec-detail` 的 `<details>` 内、`res-tabs` 之上）。
 *
 * 展示五类执行分类的**存在性 + 计数 + 状态**（`conversation.deriveExecCategories`）：
 * `Planner` / `Knowledge Search` / `Skill` / `Tool` / `Memory`。底部给「详细 Trace ›」
 * 入口（跳 L3 执行轨迹 Tab）。
 *
 * 诚实纪律（**核心**）：`present === false` 的类别**不渲染** —— 后端 payload 里没有
 * 这一类，就**不出现**对应的行（绝不臆造一条 `0 次` 的假记录）。五类**全无** ⇒ 出
 * 诚实空态。
 *
 * data-testid（只增不改不删）：`conv-exec-detail`（容器）/ `conv-exec-<id>`（分类行，
 * id ∈ planner|knowledge|skill|tool|memory）/ `conv-exec-trace`（详细 Trace 入口）。
 */
import type { ExecCategory } from './types'

export function ExecDetailPanel({
  categories,
  onTrace,
}: {
  /** 五类结构化字段（`conversation.deriveExecCategories` 输出）。 */
  categories: ExecCategory[]
  /** 「详细 Trace ›」回调 —— 由结果层切到 L3「执行轨迹」Tab。 */
  onTrace: () => void
}) {
  const present = categories.filter((c) => c.present)
  return (
    <div className="conv-exec" data-testid="conv-exec-detail">
      {present.length === 0 ? (
        <p className="conv-exec-empty">本次运行未记录可展示的执行分类</p>
      ) : (
        <ul className="conv-exec-list">
          {present.map((c) => (
            <li className="conv-exec-item" data-testid={`conv-exec-${c.id}`} key={c.id}>
              <span className="conv-exec-label">{c.label}</span>
              <span className="conv-exec-count">{c.count}</span>
              {c.statuses.length > 0 && (
                <span className="conv-exec-status">{c.statuses.join(' · ')}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      <button type="button" className="conv-exec-trace" data-testid="conv-exec-trace" onClick={onTrace}>
        查看执行轨迹 ›
      </button>
    </div>
  )
}
