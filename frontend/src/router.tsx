/* eslint-disable react-refresh/only-export-components --
   This is a router module: it intentionally exports route config (`router`)
   alongside the <Router/> component. Fast-refresh of a route file isn't
   meaningful, so the rule doesn't apply here. */
import { Suspense, lazy } from 'react'
import type { ComponentType, FunctionComponent } from 'react'
import {
  Outlet,
  RouterProvider,
  createRootRoute,
  createRoute,
  createRouter,
  redirect,
} from '@tanstack/react-router'
import { AppShell } from './components/AppShell'
import { RouteError, RouteNotFound } from './components/RouteFallbacks'
import { getSession } from './api/client'
import { announceRoleDenied, roleAtLeast, type MinRole } from './auth/roleGate'
// The design-draft home page is the site root and the first paint — keep it
// eager so the landing view isn't behind an extra chunk fetch.
import { HomeView } from './views/HomeView'

// Everything else is code-split: each view ships as its own chunk and loads on
// navigation, keeping the initial bundle small.
const lazyView = (
  loader: () => Promise<Record<string, ComponentType>>,
  name: string,
): FunctionComponent =>
  lazy(async () => ({ default: (await loader())[name] })) as unknown as FunctionComponent

// Full-screen pages (no console shell).
const LandingPage = lazyView(() => import('./views/LandingPage'), 'LandingPage')
const ArchitecturePage = lazyView(() => import('./views/ArchitecturePage'), 'ArchitecturePage')
const DesignHubPage = lazyView(() => import('./views/DesignHubPage'), 'DesignHubPage')
const DesignSystemPage = lazyView(() => import('./views/DesignSystemPage'), 'DesignSystemPage')
const DocsIndexPage = lazyView(() => import('./views/DocsPage'), 'DocsIndexPage')
const DocsArticlePage = lazyView(() => import('./views/DocsPage'), 'DocsArticlePage')

// Console-shell pages (lazy; HomeView is eager above).
const SkillsView = lazyView(() => import('./views/SkillsView'), 'SkillsView')
const KnowledgeView = lazyView(() => import('./views/KnowledgeView'), 'KnowledgeView')
const SecurityView = lazyView(() => import('./views/SecurityView'), 'SecurityView')
const LiveRunsView = lazyView(() => import('./views/LiveRunsView'), 'LiveRunsView')
const ApprovalsView = lazyView(() => import('./views/ApprovalsView'), 'ApprovalsView')
const AgentsView = lazyView(() => import('./views/AgentsView'), 'AgentsView')
const CostView = lazyView(() => import('./views/CostView'), 'CostView')
const AuditView = lazyView(() => import('./views/AuditView'), 'AuditView')
const MemoryView = lazyView(() => import('./views/MemoryView'), 'MemoryView')
const OverviewView = lazyView(() => import('./views/OverviewView'), 'OverviewView')
const EvaluationsView = lazyView(() => import('./views/EvaluationsView'), 'EvaluationsView')
const WorkflowsView = lazyView(() => import('./views/WorkflowsView'), 'WorkflowsView')
const ClustersView = lazyView(() => import('./views/ClustersView'), 'ClustersView')
const OpsView = lazyView(() => import('./views/OpsView'), 'OpsView')
const ToolsView = lazyView(() => import('./views/ToolsView'), 'ToolsView')
const MarketplaceView = lazyView(() => import('./views/MarketplaceView'), 'MarketplaceView')
const RbacView = lazyView(() => import('./views/RbacView'), 'RbacView')
// INC46 T22 —— 文档 Diff 预览与确认（深链 `/artifacts/<id>/<version>`）。
const DiffReviewView = lazyView(() => import('./views/documents/DiffReview'), 'DiffReviewView')

function RouteFallback() {
  return <div style={{ padding: 24, color: 'var(--fg-muted)', fontFamily: 'var(--font-mono)' }}>加载中…</div>
}

// Root renders <Outlet/> under a single Suspense boundary — it catches every
// lazy child below.
const rootRoute = createRootRoute({
  component: () => (
    <Suspense fallback={<RouteFallback />}>
      <Outlet />
    </Suspense>
  ),
})

// --------------------------------------------------------------------------- //
// Console shell — a pathless layout that gives every working surface the      //
// topbar + sidebar. Because it has no `path`, its children own the top-level  //
// paths (`/`, `/tasks`, …). The design-draft home page therefore lives at `/`. //
// --------------------------------------------------------------------------- //
const shellLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'shell',
  component: () => (
    <AppShell>
      <Outlet />
    </AppShell>
  ),
})

function shellChild(path: string, Component: FunctionComponent) {
  return createRoute({ getParentRoute: () => shellLayoutRoute, path, component: Component })
}

/**
 * P1 —— 角色门控路由守卫（判定源 `auth/roleGate.ts`，与 Sidebar 可见性同口径）。
 *
 * 低于 `minRole` 的角色**直访**（深链 / 手输地址 / 旧书签）时：给一次性中文提示
 * （`role-gate-toast`）并重定向到 `/tasks`。路由**全部保留注册**（不删任何页面），
 * 后端 RBAC 语义不变 —— 这里只做前端可见性分层；角色未知 / 未登录按最低权限兜底。
 */
function guardedShellChild(path: string, Component: FunctionComponent, minRole: MinRole) {
  return createRoute({
    getParentRoute: () => shellLayoutRoute,
    path,
    beforeLoad: () => {
      if (!roleAtLeast(getSession()?.role, minRole)) {
        announceRoleDenied()
        // `to` 走 string 变量（与下方 legacyRedirect 同款写法）：字符串字面量会被
        // 收窄进已注册路由的联合类型，在路由表自注册处触发 TS2322。
        const target: string = '/tasks'
        throw redirect({ to: target })
      }
    },
    component: Component,
  })
}

const shellChildren = [
  // Primary destinations (PRD §7.2-A) — 10 nav items.
  shellChild('/', HomeView),
  shellChild('/tasks', LiveRunsView),
  // INC36 / T04 — 会话深链：`/tasks/<run_id>` 直接打开某个 run 的会话（刷新 / 分享不丢）。
  // 静态 `/tasks` 仍保留（兼容），二者共用一个组件（`LiveRunsView` 读 `useParams` 播种选中）。
  shellChild('/tasks/$runId', LiveRunsView),
  shellChild('/skills', SkillsView),
  shellChild('/knowledge', KnowledgeView),
  // P1 角色门控（roleGate.ts 同口径）：admin = 管理员区目的地；manager = 「更多」组目的地。
  guardedShellChild('/security', SecurityView, 'admin'),
  guardedShellChild('/analytics', CostView, 'manager'),
  guardedShellChild('/ops', OpsView, 'manager'),
  guardedShellChild('/settings', RbacView, 'admin'),
  // Aliased routes kept so deep links keep working.
  guardedShellChild('/overview', OverviewView, 'manager'),
  shellChild('/runs', LiveRunsView),
  // INC36 / T04 — `/runs/<run_id>` 深链（与 `/tasks/<run_id>` 同组件、同语义）。
  shellChild('/runs/$runId', LiveRunsView),
  guardedShellChild('/approvals', ApprovalsView, 'manager'),
  guardedShellChild('/agents', AgentsView, 'manager'),
  guardedShellChild('/memory', MemoryView, 'manager'),
  guardedShellChild('/cost', CostView, 'admin'),
  guardedShellChild('/evals', EvaluationsView, 'manager'),
  guardedShellChild('/workflows', WorkflowsView, 'manager'),
  guardedShellChild('/tools', ToolsView, 'manager'),
  guardedShellChild('/marketplace', MarketplaceView, 'manager'),
  guardedShellChild('/audit', AuditView, 'admin'),
  guardedShellChild('/clusters', ClustersView, 'manager'),
  guardedShellChild('/rbac', RbacView, 'admin'),
  // INC46 T22 —— 文档 Diff 预览与确认：`/artifacts` 为选择入口（无参 ⇒ 诚实空态），
  // `/artifacts/$artifactId/$version` 为深链（直接打开某个待确认版本）。
  guardedShellChild('/artifacts', DiffReviewView, 'manager'),
  guardedShellChild('/artifacts/$artifactId/$version', DiffReviewView, 'manager'),
]

// Paths that used to live under `/console/*` — every one redirects to its new
// top-level home so historical links never 404 or white-screen.
const LEGACY_SHELL_PATHS = [
  '/tasks',
  '/skills',
  '/knowledge',
  '/security',
  '/analytics',
  '/ops',
  '/settings',
  '/overview',
  '/runs',
  '/approvals',
  '/agents',
  '/memory',
  '/cost',
  '/evals',
  '/workflows',
  '/tools',
  '/marketplace',
  '/audit',
  '/clusters',
  '/rbac',
  '/artifacts',
]

// Legacy `/console` layout: a pass-through that redirects each child.
const consoleRedirectRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/console',
  component: () => <Outlet />,
})

function legacyRedirect(path: string, target: string) {
  return createRoute({
    getParentRoute: () => consoleRedirectRoute,
    path,
    beforeLoad: () => {
      throw redirect({ to: target })
    },
  })
}

const consoleRedirectChildren = [
  legacyRedirect('/', '/'),
  ...LEGACY_SHELL_PATHS.map((p) => legacyRedirect(p, p)),
  // Unknown `/console/<anything>` → home (rather than a dead end).
  legacyRedirect('/$', '/'),
]

// --------------------------------------------------------------------------- //
// Standalone pages (no shell).                                                //
// --------------------------------------------------------------------------- //
const welcomeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/welcome',
  component: LandingPage,
})

const architectureRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/architecture',
  component: ArchitecturePage,
})

const designHubRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/design-hub',
  component: DesignHubPage,
})

const designSystemRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/design-system',
  component: DesignSystemPage,
})

const docsIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/docs',
  component: DocsIndexPage,
})

const docsArticleRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/docs/$slug',
  component: DocsArticlePage,
})

const routeTree = rootRoute.addChildren([
  welcomeRoute,
  architectureRoute,
  designHubRoute,
  designSystemRoute,
  docsIndexRoute,
  docsArticleRoute,
  shellLayoutRoute.addChildren(shellChildren),
  consoleRedirectRoute.addChildren(consoleRedirectChildren),
])

// 覆盖 TanStack Router 内置的**英文**错误/404 组件（INC34 轮3 中文化收尾）。
//
// ⚠️ `defaultNotFoundComponent` 必须 `as never`：`NotFoundRouteProps.routeId`
// 的类型是 `RouteIds<RegisteredRouter['routeTree']>`，而本项目在
// `src/router-types.d.ts` 里把 `Register.router` 声明为 `typeof router` —— 只要
// TS 去解析该选项类型，就会形成 `router → options → Register → router` 的循环
// 并报 TS7022。`never` 可赋给任意类型且不触发结构化比较，正好切断这条链；
// 运行期契约（一个接收 props 的 React 组件）完全不变。
export const router = createRouter({
  routeTree,
  defaultErrorComponent: RouteError,
  defaultNotFoundComponent: RouteNotFound as never,
})

export function Router() {
  return <RouterProvider router={router} />
}
