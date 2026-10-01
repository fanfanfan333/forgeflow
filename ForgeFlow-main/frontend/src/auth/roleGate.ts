/**
 * roleGate.ts — 角色门控（P1：可见性分层）的**单一判定源**。
 *
 * Sidebar（导航可见性）与 router.tsx（路由守卫）都从这里取判定，两处绝不各自
 * 发明口径。层级沿用既有角色语义（`home/roleConfig.ts` 与后端 `ROLE_PERMISSIONS`
 * 的四角色：viewer / sales_rep / manager / admin），不发明新角色：
 *
 *   viewer(0) < sales_rep(1) < manager(2) < admin(3)
 *
 * 可见性分层（与 Sidebar 分组一一对应）：
 *   · 用户区（工作区 / 任务 / 技能 / 知识库）…… 所有角色（含未登录 / 未知角色）
 *   · 「更多」折叠组目的地 ……………………………… manager 及以上
 *   · 管理员区目的地 ………………………………………… 仅 admin
 *
 * 兜底纪律：角色缺失 / 未知一律按**最低权限**（viewer = 0）处理，绝不臆造更高
 * 权限。注意这只是**前端可见性分层**：后端 RBAC 仍是真正的权限边界，本模块不
 * 改变、也不替代任何后端判定。
 */

/** 既有四角色的权限层级（值即秩，越大权限越高）。 */
export const ROLE_RANK = { viewer: 0, sales_rep: 1, manager: 2, admin: 3 } as const

export type GateRole = keyof typeof ROLE_RANK

/** 路由守卫的最低角色档（与 Sidebar 的分组档一一对应）。 */
export type MinRole = 'manager' | 'admin'

/**
 * 原始角色字符串 → 权限秩。大小写 / 空白不敏感；**未知或缺失 ⇒ viewer(0)**，
 * 即「角色未知时按最低权限」的兜底。
 */
export function roleRank(role: string | null | undefined): number {
  const key = (role ?? '').trim().toLowerCase()
  return (ROLE_RANK as Record<string, number>)[key] ?? ROLE_RANK.viewer
}

/** 该角色是否达到 `min` 档（router 守卫与 Sidebar 分组共用）。 */
export function roleAtLeast(role: string | null | undefined, min: MinRole): boolean {
  return roleRank(role) >= ROLE_RANK[min]
}

// ---- 一次性中文提示（路由守卫重定向时） --------------------------------------

let activeToast: HTMLDivElement | null = null

/**
 * 一次性中文提示：无权访问被重定向时，在视口底部短暂浮现一条说明，约 4s 后
 * 自动消失。同一时刻**只保留一条**（连点多个受限入口不会堆叠）。样式在
 * `styles/dashboard.css`（`.role-gate-toast`，双主题 token 驱动）。
 *
 * 不走 React 渲染树的原因：守卫在 `beforeLoad`（渲染之外）触发重定向，这里用
 * 最小 DOM 注入即可；`data-testid="role-gate-toast"` 供 e2e 钉住。
 */
export function announceRoleDenied(
  message = '当前角色无权访问该页面，已回到任务页',
): void {
  if (typeof document === 'undefined') return
  if (activeToast && document.body.contains(activeToast)) return
  const el = document.createElement('div')
  el.className = 'role-gate-toast'
  el.setAttribute('role', 'alert')
  el.setAttribute('data-testid', 'role-gate-toast')
  el.textContent = message
  document.body.appendChild(el)
  activeToast = el
  window.setTimeout(() => {
    el.remove()
    if (activeToast === el) activeToast = null
  }, 4000)
}
