import type { ReactNode } from 'react'
import { useMatchRoute, useNavigate } from '@tanstack/react-router'
import { Topbar } from './Topbar'
import { AuthBanner } from './AuthControls'
import { Sidebar, type ViewId } from './Sidebar'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

const VIEW_TITLES: Record<ViewId, string> = {
  home: '首页',
  tasks: '智能任务',
  agents: 'Agent 工作台',
  skills: '技能中心',
  knowledge: '知识库',
  memory: '记忆管理',
  analytics: '数据分析',
  security: '安全与权限',
  ops: '运维监控',
  settings: '系统设置',
}

const VIEW_PATHS: Record<ViewId, string> = {
  home: '/',
  tasks: '/tasks',
  agents: '/agents',
  skills: '/skills',
  knowledge: '/knowledge',
  memory: '/memory',
  analytics: '/analytics',
  security: '/security',
  ops: '/ops',
  settings: '/settings',
}

// Aliased paths kept routable so old deep links still resolve to a highlighted
// nav item instead of falling back to 首页.
const VIEW_MATCH: { view: ViewId; path: string }[] = [
  ...(Object.entries(VIEW_PATHS) as [ViewId, string][]).map(([view, path]) => ({ view, path })),
  { view: 'home', path: '/overview' },
  { view: 'tasks', path: '/runs' },
  { view: 'tasks', path: '/approvals' },
  { view: 'tasks', path: '/workflows' },
  { view: 'agents', path: '/tools' },
  { view: 'skills', path: '/marketplace' },
  { view: 'knowledge', path: '/evals' },
  { view: 'analytics', path: '/cost' },
  { view: 'ops', path: '/clusters' },
  { view: 'ops', path: '/audit' },
  { view: 'settings', path: '/rbac' },
]

function useActiveView(): ViewId {
  const match = useMatchRoute()
  for (const { view, path } of VIEW_MATCH) {
    if (match({ to: path, fuzzy: false })) return view
  }
  return 'home'
}

export function AppShell({ children }: { children: ReactNode }) {
  const active = useActiveView()
  const navigate = useNavigate()
  useDocumentTitle(`控制台 · ${VIEW_TITLES[active]}`)
  return (
    <div className="app">
      <a href="#main-content" className="skip-link">跳转到内容</a>
      <Topbar />
      <Sidebar
        active={active}
        onSelect={(id) => navigate({ to: VIEW_PATHS[id] })}
      />
      <main className="main" id="main-content" tabIndex={-1}>
        <AuthBanner />
        {children}
      </main>
    </div>
  )
}
