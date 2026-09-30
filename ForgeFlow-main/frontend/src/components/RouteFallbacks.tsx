/* 路由级兜底态（INC34 轮3 · 中文化收尾）
 *
 * TanStack Router 内置的错误组件与 404 组件是**英文**（`Something went wrong!` /
 * `Not Found`），与「前端以中文为主」的验收判据冲突。这里覆盖为中文，并遵守
 * **数据诚实**原则：错误详情不隐藏，但默认折叠，点击才展开真实 message。
 *
 * 只走 tokens.css 语义变量 ⇒ 浅/深色自动一致；无新增依赖。
 */
import { useState } from 'react'

/** 路由渲染异常兜底。挂在 `createRouter({ defaultErrorComponent })`。 */
export function RouteError({ error, reset }: { error: unknown; reset?: () => void }) {
  const [open, setOpen] = useState(false)
  const message =
    error instanceof Error ? error.message : typeof error === 'string' ? error : '未知错误'

  return (
    <div className="route-state" data-testid="route-error">
      <div className="route-state-card">
        <p className="route-state-eyebrow">出错了</p>
        <h1>这个页面没能渲染出来</h1>
        <p className="route-state-sub">
          页面在渲染过程中遇到异常。你可以重试，或返回首页继续使用控制台。
        </p>
        <div className="route-state-actions">
          <button type="button" className="btn primary" onClick={() => reset?.()}>
            重试
          </button>
          <a className="btn" href="/">
            返回首页
          </a>
          <button
            type="button"
            className="btn ghost"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? '隐藏错误详情' : '查看错误详情'}
          </button>
        </div>
        {open && (
          <pre className="route-state-detail" data-testid="route-error-detail">
            {message}
          </pre>
        )}
      </div>
    </div>
  )
}

/** 未匹配路由兜底（404）。挂在 `createRouter({ defaultNotFoundComponent })`。 */
export function RouteNotFound({ routeId }: { routeId?: string }) {
  return (
    <div className="route-state" data-testid="route-not-found">
      <div className="route-state-card">
        <p className="route-state-eyebrow">404</p>
        <h1>页面不存在</h1>
        <p className="route-state-sub">
          没有找到这个地址对应的页面。可能是链接已失效，或路径拼写有误。
        </p>
        {routeId && <p className="route-state-path">未匹配的路由：{routeId}</p>}
        <div className="route-state-actions">
          <a className="btn primary" href="/">
            返回首页
          </a>
          <a className="btn" href="/tasks">
            打开智能任务
          </a>
        </div>
      </div>
    </div>
  )
}
