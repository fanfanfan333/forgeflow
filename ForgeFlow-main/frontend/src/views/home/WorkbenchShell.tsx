/**
 * WorkbenchShell — Agent 工作台三栏栅格外壳（AgentFlow 2.0 / Phase B / T-B01）。
 *
 * 纯布局容器（**无数据逻辑**）：左 = 上下文（`ContextPanel`）/ 中 = Agent 工作流·
 * 对话·结果（既有 `ChatWorkspace` + `ChatComposer`）/ 右 = 对象 Inspector
 * （`ObjectInspector`）。`right` 未提供时，右栏渲染诚实空态「选择一个对象查看详情」。
 *
 * 断点（AUDIT §14.1.1 / Q-B4）：`< 1100px` 折叠为单列（左栏入中央列上方、右栏入
 * 下方），中央列始终优先。样式见 `styles/workbench.css`（仅消费 `tokens.css` 语义变量，
 * **不引** MUI / Tailwind / 任何 UI 库）。
 *
 * testid 纪律：一律用**全新** `workbench-*`（只增不改不删既有 `data-testid`）。
 */
import type { ReactNode } from 'react'
import { useState } from 'react'
import '../../styles/workbench.css'

export function WorkbenchShell({
  left,
  center,
  right,
  inspectorOpen = false,
}: {
  /** 左栏：上下文 / 对象树。 */
  left: ReactNode
  /** 中栏：Agent 工作流 · 对话 · 结果（既有聊天区）。 */
  center: ReactNode
  /** 右栏：对象 Inspector。缺省 ⇒ 渲染诚实空态。 */
  right?: ReactNode
  inspectorOpen?: boolean
}) {
  const [contextOpen, setContextOpen] = useState(false)
  return (
    <div className={`workbench${contextOpen ? ' context-open' : ''}${inspectorOpen ? ' inspector-open' : ''}`} data-testid="workbench">
      <button className="workbench-context-toggle" aria-expanded={contextOpen} onClick={() => setContextOpen(!contextOpen)}>{contextOpen ? '收起任务上下文' : '任务上下文与文件'}</button>
      <div
        className="workbench-col workbench-context"
        data-testid="workbench-context"
        role="region"
        aria-label="上下文与对象"
      >
        {left}
      </div>

      <div
        className="workbench-col workbench-center"
        data-testid="workbench-center"
        role="region"
        aria-label="Agent 工作流与结果"
      >
        {center}
      </div>

      <aside className="workbench-col workbench-inspector" data-testid="workbench-inspector" aria-label="对象详情">
        {right ?? (
          <section className="workbench-panel" data-testid="workbench-inspector-panel" aria-label="对象详情">
            <h2 className="workbench-panel-title">对象详情</h2>
            <p className="workbench-empty" data-testid="workbench-inspector-empty">
              选择一个对象查看详情
            </p>
          </section>
        )}
      </aside>
    </div>
  )
}
