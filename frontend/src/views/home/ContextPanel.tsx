/**
 * ContextPanel — 工作台左栏「上下文」（Phase B / T-B02 实现）。
 *
 * 数据经 `useSessionObjects(sessionId)`（`GET /workspace/sessions/{id}` + 最新
 * `GET /runs/{id}`）派生对象树；呈现交 `ContextObjects`；资源库由 `ContextResources`
 * 单独挂载（仅在会话存在时，避免空首页多发请求）。
 *
 * 诚实空态（AUDIT §14.10）：
 *   · 未选中会话 ⇒「还没有任务，去中间发起第一个」；
 *   · 会话加载中 ⇒「加载中…」；
 *   · 无 run / 无对象 ⇒ 各分组「本次无…」/「暂无数据」（见 `ContextObjects`）。
 *
 * Props 与 AUDIT §14.6 类图一致（`sessionId` / `selected` / `onSelect`）。
 */
import { ContextObjects, ContextResources } from './ContextObjects'
import { useSessionObjects } from './useSessionObjects'
import type { InspectObject } from './inspect'

type ContextPanelProps = {
  sessionId: string | null
  selected?: InspectObject | null
  onSelect?: (object: InspectObject) => void
}

/** 占位回调：`onSelect` 未接线时点击不产生副作用（不抛错、不臆造）。 */
const noopSelect = (): void => undefined

export function ContextPanel({ sessionId, selected, onSelect }: ContextPanelProps) {
  const { tree, loading, hasData } = useSessionObjects(sessionId)
  return (
    <section className="workbench-panel" data-testid="workbench-context-panel" aria-label="上下文">
      <h2 className="workbench-panel-title">上下文</h2>
      {!sessionId ? (
        <p className="workbench-empty" data-testid="workbench-context-empty">
          还没有任务，去中间发起第一个
        </p>
      ) : loading && !hasData ? (
        <p className="workbench-empty" data-testid="workbench-context-empty">
          加载中…
        </p>
      ) : (
        <>
          <ContextObjects tree={tree} selected={selected ?? null} onSelect={onSelect ?? noopSelect} />
          <ContextResources />
        </>
      )}
    </section>
  )
}
