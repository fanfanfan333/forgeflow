/**
 * INC29 / T03 —— 定位纯函数（`scrollIntoView` 封装）。
 *
 * 独立成模块（而非放在 `CodeTaskTimeline.tsx` 里导出）的两个原因：
 *   1. `react-refresh/only-export-components` —— 组件文件只应导出组件，混着导出
 *      普通函数会让 HMR 退化成整页刷新（eslint 门禁一直是红的）；
 *   2. 这两个函数是**纯 DOM 工具**，`ResultContextActions.tsx` 也要用 —— 放在这里
 *      就不必为了一个滚动函数去 import 整个时间线组件模块。
 *
 * 纪律：目标缺失 / 引擎未实现滚动一律返回 `false`（安全 no-op，**绝不抛**），便于在
 * node 里独立真跑（机械证据）。
 */

/** 滚动到元素（`scrollIntoView`）。目标缺失 / 引擎未实现 ⇒ `false`。 */
function scrollTo(el: Element | null): boolean {
  if (!el) return false
  const target = el as HTMLElement
  if (typeof target.scrollIntoView !== 'function') return false
  target.scrollIntoView({ behavior: 'smooth', block: 'start' })
  return true
}

/** 滚动到 `id` 对应的元素（供「查看 Diff」/「查看测试」使用）。 */
export function scrollToId(doc: Document, id: string): boolean {
  return scrollTo(doc.getElementById(id))
}

/**
 * 「查看 Trace」：先把 `#code-trace`（原生 `<details>`）展开（`open = true`），再滚动
 * 过去。目标缺失 ⇒ `false`。展开走 `open` 属性（原生 details 驱动折叠，见折叠纪律）。
 */
export function revealTrace(doc: Document): boolean {
  const el = doc.getElementById('code-trace') as HTMLDetailsElement | null
  if (!el) return false
  el.open = true
  return scrollTo(el)
}
