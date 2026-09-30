/**
 * AgentRunSummary — INC36 L1 ③「AI 执行摘要」（已终态运行）。
 *
 * 把这次运行**真实发生过的步骤**（`conversation.stagesToStepData` ← `detailToStages`）
 * 渲染成一串带状态圆点的 ✓ 业务语行（复用既有 `AgentStep`：默认密度只有业务语，
 * 工程值仅在 `debug` 档出现）。
 *
 * 运行中的实时流不在这里（那是既有 `WorkspaceLiveStrip`，消费 SSE）——本组件只负责
 * **终态**的「执行摘要」：让用户一眼看到 Agent 做过哪几步，而不是四张平级 Tab。
 *
 * 诚实纪律：`steps === []` ⇒ 返回 `null`（**不渲染**空容器）——没有真实步骤就没有
 * 摘要，绝不补占位。
 *
 * data-testid（只增不改不删）：`conv-run-summary`。
 */
import { AgentStep } from './AgentStep'
import type { AgentStepData } from './AgentStep'
import type { ViewMode } from './types'

export function AgentRunSummary({ steps, mode }: { steps: AgentStepData[]; mode: ViewMode }) {
  if (steps.length === 0) return null
  return (
    <section className="conv-summary" data-testid="conv-run-summary" aria-label="执行摘要">
      <p className="conv-summary-title">执行摘要</p>
      <ol className="agent-steps">
        {steps.map((s) => (
          <AgentStep key={s.key} step={s} mode={mode} />
        ))}
      </ol>
    </section>
  )
}
