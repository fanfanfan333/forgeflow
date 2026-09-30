/**
 * Sidebar — 左栏主导航（INC36 / T06：从「平铺 10 项 + 底部促销块」重构为**分组极简**）。
 *
 * 用户诉求（原话）：普通用户主要看到「新任务 / 最近任务 / 技能 / 知识库」，管理员区
 * 另收「安全 / 审计 / 成本 / 设置」；其余远离日常的功能收进**「更多」折叠组**。
 * 高级感来自**留白 / 字体层级 / 信息密度控制 / 一致性**，而非卡片 / 渐变 / 大图标堆叠。
 *
 * 硬约束（勿违）：
 *   · 既有 ViewId 导航项**保持** `href={`#${id}`}` + `onSelect(id)` 机制（`AppShell` 靠它
 *     切视图）——**不得**改成纯 `<a href>` 而破坏现有行为。
 *   · 所有既有目的地路径**仍可达**（普通项 + 「更多」组的链接项）；**一个都不删**。
 *   · 删除底部 `sidebar-foot` 促销文案（用户点名的「后台感」装饰）。
 *   · 本组件原本**没有任何 data-testid**，故重构不触及任何 e2e 契约。
 */
import { useState } from 'react'
import type { ReactNode } from 'react'
import {
  IconAgents,
  IconChevronDown,
  IconCost,
  IconEvals,
  IconList,
  IconMemory,
  IconShield,
  IconTools,
  IconWorkflow,
} from './icons'
import '../styles/home.css'

export type ViewId =
  | 'home'
  | 'tasks'
  | 'agents'
  | 'skills'
  | 'knowledge'
  | 'memory'
  | 'analytics'
  | 'security'
  | 'ops'
  | 'settings'

/**
 * 一个导航项：
 *   · 有 `id` ⇒ 走既有 `onSelect(id)` 机制（`href={`#${id}`}`）。
 *   · 只有 `href` ⇒ 纯路径目的地（沿用既有 `<a href>` 的 sublink 模式）。
 */
type NavItem = { key: string; label: string; icon?: ReactNode; id?: ViewId; href?: string }

// 用户区 —— 默认只展示用户当前最需要的四项（新任务 / 最近任务 / 技能 / 知识库）。
const USER_NAV: NavItem[] = [
  { key: 'new', label: '新任务', icon: <IconWorkflow />, id: 'tasks' },
  { key: 'recent', label: '最近任务', icon: <IconList />, id: 'tasks' },
  { key: 'skills', label: '技能', icon: <IconEvals />, id: 'skills' },
  { key: 'knowledge', label: '知识库', icon: <IconList />, id: 'knowledge' },
]

// 管理员区 —— 安全 / 审计 / 成本 / 设置（审计、成本为纯路径目的地）。
const ADMIN_NAV: NavItem[] = [
  { key: 'security', label: '安全与权限', icon: <IconShield />, id: 'security' },
  { key: 'audit', label: '审计', icon: <IconList />, href: '/audit' },
  { key: 'cost', label: '成本', icon: <IconCost />, href: '/cost' },
  { key: 'settings', label: '系统设置', icon: <IconTools />, id: 'settings' },
]

// 「更多」折叠组 —— 远离日常、但**必须仍然可达**的目的地（一个都不删）。
const MORE_NAV: NavItem[] = [
  { key: 'agents', label: 'Agent 工作台', icon: <IconAgents />, id: 'agents' },
  { key: 'memory', label: '记忆管理', icon: <IconMemory />, id: 'memory' },
  { key: 'analytics', label: '数据分析', icon: <IconCost />, id: 'analytics' },
  { key: 'ops', label: '运维监控', icon: <IconTools />, id: 'ops' },
  { key: 'overview', label: '概览', href: '/overview' },
  { key: 'approvals', label: '审批', href: '/approvals' },
  { key: 'workflows', label: '工作流', href: '/workflows' },
  { key: 'tools', label: '工具', href: '/tools' },
  { key: 'marketplace', label: '技能市场', href: '/marketplace' },
  { key: 'clusters', label: '集群', href: '/clusters' },
  { key: 'rbac', label: '角色权限', href: '/rbac' },
  { key: 'evals', label: '评测', href: '/evals' },
  { key: 'runs', label: '运行历史', href: '/runs' },
]

type SidebarProps = {
  active: ViewId
  onSelect: (id: ViewId) => void
}

/** 「更多」组里被 `active` 命中的 id —— 用于默认展开该组（当前页不藏在折叠区里）。 */
const MORE_IDS: ViewId[] = ['agents', 'memory', 'analytics', 'ops']

export function Sidebar({ active, onSelect }: SidebarProps) {
  const [moreOpen, setMoreOpen] = useState(MORE_IDS.includes(active))

  const renderItem = (item: NavItem) => {
    if (item.id) {
      const id = item.id
      return (
        <a
          key={item.key}
          className={`navlink${id === active ? ' active' : ''}`}
          onClick={(e) => {
            e.preventDefault()
            onSelect(id)
          }}
          href={`#${id}`}
          aria-current={id === active ? 'page' : undefined}
        >
          {item.icon}
          {item.label}
        </a>
      )
    }
    return (
      <a key={item.key} className="navlink" href={item.href}>
        {item.icon}
        {item.label}
      </a>
    )
  }

  return (
    <aside className="sidebar" aria-label="主导航">
      <a className="sidebar-brand" href="/">
        <span className="brand-text">
          <span className="brand-name">ForgeFlow</span>
          <span className="brand-sub">企业级 AI 员工操作系统</span>
        </span>
      </a>

      {/* 用户区 —— 默认高频入口。 */}
      <div className="group">{USER_NAV.map(renderItem)}</div>

      {/* 管理员区 —— 分组标题「管理员」。 */}
      <div className="group">
        <div className="group-title">管理员</div>
        {ADMIN_NAV.map(renderItem)}
      </div>

      {/* 「更多」折叠组 —— 其余目的地（一个都不删）。 */}
      <div className="group">
        <details className="sidebar-more" open={moreOpen} onToggle={(e) => setMoreOpen(e.currentTarget.open)}>
          <summary className="navlink sidebar-more-summary">
            更多
            <IconChevronDown className={`chev${moreOpen ? ' open' : ''}`} />
          </summary>
          {MORE_NAV.map(renderItem)}
        </details>
      </div>
    </aside>
  )
}
