/**
 * SourcesDisclosure — INC36 L2「查看来源」内联展开。
 *
 * 一个原生 `<details>`（**默认折叠，无 `open`**）。展开后展示 ≤N 条**真实来源行**，
 * 并在有更多时给出「查看全部来源 ›」（跳 L3 证据 Tab 取全量）。
 *
 * 诚实纪律（本轮不改后端）：
 *   · 后端**当前没有**结构化的 `file / sheet / rows` 来源字段 ⇒ 行文案是
 *     **诚实的 URL 列表**（`来源：<URL>`），由 `conversation.deriveSourceRows` 只出后端
 *     真实有的东西 —— **绝不**编造目录细节。
 *   · 无来源 ⇒ 展示**诚实空态**「本次运行未记录可展示的来源」，且**绝不**出现
 *     「0 个来源」这类伪造计数（沿用 `ResultPanel` 证据 Tab 的同口径）。
 *
 * data-testid（只增不改不删）：`conv-sources`。
 */
export function SourcesDisclosure({
  rows,
  total,
  onOpenAll,
}: {
  /** 折叠区内展示的来源行（已按 limit 截断）。 */
  rows: { label: string; detail: string }[]
  /** 后端真实来源**总数**（`deriveSources` 全量长度），用于判断是否给「查看全部」。 */
  total: number
  /** 「查看全部来源 ›」回调（跳 L3 证据 Tab）。 */
  onOpenAll: () => void
}) {
  return (
    <details className="conv-sources" data-testid="conv-sources">
      <summary className="conv-sources-summary">查看来源</summary>
      {rows.length === 0 ? (
        <p className="conv-sources-empty">本次运行未记录可展示的来源</p>
      ) : (
        <>
          <ul className="conv-source-list">
            {rows.map((r, i) => (
              <li className="conv-source-item" key={`${r.label}-${i}`}>
                <span className="conv-source-label">来源：{r.label}</span>
                {r.detail && <span className="conv-source-detail">{r.detail}</span>}
              </li>
            ))}
          </ul>
          {total > rows.length && (
            <button type="button" className="conv-sources-all" onClick={onOpenAll}>
              查看全部来源 ›
            </button>
          )}
        </>
      )}
    </details>
  )
}
