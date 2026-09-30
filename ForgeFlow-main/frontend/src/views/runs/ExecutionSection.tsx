/**
 * INC14 — 执行层 (execution section) — the SECOND visual layer of /tasks.
 *
 * The execution path used to be the visual centre of gravity. In the result-first
 * layout it becomes supporting detail: this section wraps the existing
 * `ExecutionPath` in a container that is **collapsed by default**, so the result
 * layer is what the user sees first and the step-by-step trace is one click away.
 *
 * Accessibility contract (mirrors `RunStageCard`):
 *   * the header is a native <button type="button">;
 *   * `aria-expanded` reflects state, `aria-controls` points at the body id;
 *   * the collapsed body carries `hidden`, so its controls leave the tab order.
 *
 * `[hidden]` 纪律: the body's layout properties (display / flex / gap) live in
 * `.exec-body:not([hidden])`, NEVER on the base `.exec-body` rule — an author
 * `display` would unconditionally beat the UA `[hidden] { display: none }`.
 */
import { Fragment, useState } from 'react'
import { IconChevronDown } from '../../components/icons'
import type { RunStage, ViewMode } from './types'
import { RunStageCard } from './RunStageCard'

export function ExecutionSection({
  stages,
  mode,
  open: openProp,
  onToggle,
}: {
  stages: RunStage[]
  mode: ViewMode
  /**
   * INC20 / T04 —— 受控展开态。传入 `open` 即为受控（展开态由父组件持有，
   * 本组件**不再**自己持有）；不传则为非受控（demo 分支沿用，默认折叠）。
   */
  open?: boolean
  /** 受控模式下的展开/折叠切换回调（非受控模式下忽略）。 */
  onToggle?: () => void
}) {
  // 非受控回退（demo 分支：`<ExecutionSection stages=… mode=… />`，行为不退化）。
  const [internalOpen, setInternalOpen] = useState(false)
  const controlled = openProp !== undefined
  const open = controlled ? openProp : internalOpen
  const toggle = () => {
    if (controlled) onToggle?.()
    else setInternalOpen((o) => !o)
  }

  return (
    <section className="exec-panel" data-testid="execution-layer" aria-label="执行过程">
      <button
        type="button"
        className="exec-head"
        data-testid="execution-layer-title"
        aria-expanded={open}
        aria-controls="execution-body"
        onClick={toggle}
      >
        <span className="exec-chev" aria-hidden="true">
          <IconChevronDown width={12} height={12} />
        </span>
        <span className="exec-title">执行过程</span>
        <span className="exec-count">{stages.length} 步</span>
      </button>

      <div id="execution-body" className="exec-body" data-testid="execution-body" hidden={!open}>
        <ExecutionPath stages={stages} mode={mode} />
      </div>
    </section>
  )
}

/** The stage rail + stage cards (the pre-INC14 execution path, unchanged). */
function ExecutionPath({ stages, mode }: { stages: RunStage[]; mode: ViewMode }) {
  return (
    <section className="exec-path" aria-label="执行路径">
      <div className="eyebrow">执行路径</div>
      <StageRail stages={stages} />
      <div className="exec-cards">
        {stages.map((s) => (
          <RunStageCard key={s.id} stage={s} mode={mode} />
        ))}
      </div>
    </section>
  )
}

/**
 * INC21 / G4 — Visual progress rail (status dots + connecting hairline).
 *
 * The rail renders **only** the status dot per step — it no longer repeats the
 * stage name (`s.name`). The name + one-line summary belong to the single
 * vertical chain below (`RunStageCard`); rendering the name twice (rail + card)
 * duplicated the same information. The wrapper keeps `aria-hidden="true"`, so
 * the rail is a purely decorative progress indicator.
 */
function StageRail({ stages }: { stages: RunStage[] }) {
  return (
    <div className="stage-rail" aria-hidden="true">
      {stages.map((s, i) => (
        <Fragment key={s.id}>
          {i > 0 && <span className="rail-line" />}
          <span className={`rail-step ${s.status}`}>
            <span className="rail-dot" />
          </span>
        </Fragment>
      ))}
    </div>
  )
}
