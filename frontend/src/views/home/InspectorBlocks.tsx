/**
 * InspectorBlocks — 右栏只读事实块的呈现层（Phase B / T-B03）。
 *
 * 复用既有 `skill-assets-insp-*` 的视觉语言，但用自有 `.workbench-insp-*` 前缀
 * （不触碰既有 class；见 AUDIT §14.4 样式纪律）。缺失值（`value === null`）渲染
 * 「—」，**绝不**补 0 / 占位数字。
 *
 * `InspectorSectionList` 是 `ObjectInspector` 的组合入口；`InspectorBlocks` 与
 * 类图（§14.6：`title` + `facts`）逐字对应。
 */
import type { InspectFact } from './inspect'
import type { InspectorSection } from './inspectorSections'

export function InspectorBlocks({ title, facts }: { title: string; facts: InspectFact[] }) {
  return (
    <section className="workbench-insp-block" data-testid="workbench-inspector-block">
      <h3 className="workbench-insp-block-title">{title}</h3>
      <dl className="workbench-insp-facts">
        {facts.map((fact) => (
          <div className="workbench-insp-row" key={fact.key}>
            <dt>{fact.label}</dt>
            <dd className={fact.value === null ? 'is-missing' : undefined}>
              {fact.value === null ? '—' : fact.value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

/** 一组事实段；空数组 ⇒ 不渲染任何块（调用方负责「暂无数据」空态）。 */
export function InspectorSectionList({ sections }: { sections: InspectorSection[] }) {
  return (
    <div className="workbench-insp-sections" data-testid="workbench-inspector-sections">
      {sections.map((section) => (
        <InspectorBlocks key={section.key} title={section.title} facts={section.facts} />
      ))}
    </div>
  )
}
