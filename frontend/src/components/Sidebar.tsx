import { useState } from 'react'
import type { ReactNode } from 'react'
import {
  IconAgents,
  IconCheck,
  IconChevronDown,
  IconCost,
  IconEvals,
  IconGrid,
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

type NavItem = { id: ViewId; label: string; icon: ReactNode }

// The 10 primary destinations (PRD §7.2-A). Order matches the design.
const NAV: NavItem[] = [
  { id: 'home', label: '首页', icon: <IconGrid /> },
  { id: 'tasks', label: '智能任务', icon: <IconWorkflow /> },
  { id: 'agents', label: 'Agent 工作台', icon: <IconAgents /> },
  { id: 'skills', label: '技能中心', icon: <IconEvals /> },
  { id: 'knowledge', label: '知识库', icon: <IconList /> },
  { id: 'memory', label: '记忆管理', icon: <IconMemory /> },
  { id: 'analytics', label: '数据分析', icon: <IconCost /> },
  { id: 'security', label: '安全与权限', icon: <IconShield /> },
  { id: 'ops', label: '运维监控', icon: <IconTools /> },
]

// 系统设置 expands into three read-only entry points.
const SETTINGS_CHILDREN = [
  { label: '用户与角色', href: '/settings' },
  { label: '策略配置', href: '/settings' },
  { label: '审计导出', href: '/audit' },
]

type SidebarProps = {
  active: ViewId
  onSelect: (id: ViewId) => void
}

export function Sidebar({ active, onSelect }: SidebarProps) {
  const [settingsOpen, setSettingsOpen] = useState(active === 'settings')
  return (
    <aside className="sidebar" aria-label="主导航">
      <a className="sidebar-brand" href="/">
        <span className="brand-text">
          <span className="brand-name">ForgeFlow</span>
          <span className="brand-sub">企业级 AI 员工操作系统</span>
        </span>
      </a>

      <div className="group">
        {NAV.map((item) => (
          <a
            key={item.id}
            className={`navlink${item.id === active ? ' active' : ''}`}
            onClick={(e) => {
              e.preventDefault()
              onSelect(item.id)
            }}
            href={`#${item.id}`}
            aria-current={item.id === active ? 'page' : undefined}
          >
            {item.icon}
            {item.label}
          </a>
        ))}

        {/* 系统设置 — expandable */}
        <a
          className={`navlink${active === 'settings' ? ' active' : ''}`}
          onClick={(e) => {
            e.preventDefault()
            setSettingsOpen((v) => !v)
            onSelect('settings')
          }}
          href="#settings"
          aria-expanded={settingsOpen}
        >
          <IconCheck />
          系统设置
          <IconChevronDown className={`chev${settingsOpen ? ' open' : ''}`} />
        </a>
        {settingsOpen &&
          SETTINGS_CHILDREN.map((child) => (
            <a key={child.label} className="sublink" href={child.href}>
              {child.label}
            </a>
          ))}
      </div>

      <div className="sidebar-foot">
        <div className="l1">让企业经验</div>
        <div className="l2">沉淀为可执行的技能</div>
        <div className="l3">让 AI 真正为团队创造价值</div>
      </div>
    </aside>
  )
}
