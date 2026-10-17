/**
 * Sidebar — 左栏主导航（INC36 / T06：从「平铺 10 项 + 底部促销块」重构为**分组极简**）。
 *
 * 用户诉求（原话）：普通用户主要看到「新对话 / 最近对话 / 技能 / 知识库」，管理员区
 * 另收「安全 / 审计 / 成本 / 设置」；其余远离日常的功能收进**「更多」折叠组**。
 * 高级感来自**留白 / 字体层级 / 信息密度控制 / 一致性**，而非卡片 / 渐变 / 大图标堆叠。
 *
 * 硬约束（勿违）：
 *   · 既有 ViewId 导航项**保持** `href={`#${id}`}` + `onSelect(id)` 机制（`AppShell` 靠它
 *     切视图）——**不得**改成纯 `<a href>` 而破坏现有行为。
 *   · 所有既有目的地路径**仍可达**（路由全部保留；见下条的角色分层）。
 *   · 删除底部 `sidebar-foot` 促销文案（用户点名的「后台感」装饰）。
 *   · 本组件原本**没有任何 data-testid**，故重构不触及任何 e2e 契约。
 *
 * P1 —— 角色门控（可见性分层；判定源 `auth/roleGate.ts`，与 router 守卫同口径）：
 *   · viewer / sales_rep / 未知角色 ⇒ 只见用户区（工作区 / 任务 / 技能 / 知识库，
 *     未知角色按最低权限兜底）；
 *   · manager                     ⇒ 用户区 + 「更多」折叠组；
 *   · admin                       ⇒ 用户区 + 「更多」+ 管理员区（全部目的地仍可达）。
 * 不删任何路由 / 页面：被分层的入口只是**不在该角色的导航里渲染**，直访时由
 * router.tsx 的守卫一次性中文提示并重定向。
 */
import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useRouterState } from '@tanstack/react-router'
import type { ReactNode } from 'react'
import {
  IconAgents,
  IconChevronDown,
  IconCost,
  IconEvals,
  IconGrid,
  IconList,
  IconMemory,
  IconShield,
  IconTools,
  IconWorkflow,
  IconSearch,
  IconPlus,
} from './icons'
import { useSession } from '../hooks/useSession'
import { ROLE_RANK, roleRank } from '../auth/roleGate'
import { useWorkspaceSessions } from '../api/hooks'
import { SEARCH_HISTORY_EVENT } from '../views/home/navigation'
import { DeleteConversation } from './DeleteConversation'
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
 *   · `testid`（可选）⇒ 落到该链接的 `data-testid`（INC43 新增，仅用户区 5 项）。
 */
type NavItem = { key: string; label: string; icon?: ReactNode; id?: ViewId; href?: string; testid?: string }

// 用户区 —— 所有角色可见的高频入口（工作区 / 新对话 / 最近对话 / 技能 / 知识库）。
// INC43 / P0-1：第二项改为「新对话」、第三项改为「最近对话」；
// `id` / `href={'#'+id}` / `onSelect(id)` 导航机制**不变**（仍是 `id:'tasks'`）。
const USER_NAV: NavItem[] = [
  { key: 'home', label: '工作区', icon: <IconGrid />, id: 'home', testid: 'sidebar-nav-workspace' },
  { key: 'files', label: '文件与数据', icon: <IconList />, href: '/files', testid: 'sidebar-nav-files' },
  { key: 'new', label: '新对话', icon: <IconWorkflow />, id: 'tasks', testid: 'sidebar-nav-new-chat' },
  { key: 'recent', label: '最近对话', icon: <IconList />, href: '/conversations', testid: 'sidebar-nav-recent-chat' },
  { key: 'skills', label: '技能', icon: <IconEvals />, id: 'skills', testid: 'sidebar-nav-skills' },
  { key: 'knowledge', label: '知识库', icon: <IconList />, id: 'knowledge', testid: 'sidebar-nav-knowledge' },
]

// 管理员区 —— 安全 / 审计 / 成本 / 设置 / 角色权限（审计、成本、角色权限为纯路径目的地）。
// 仅 admin 渲染（router 同口径守卫）。「角色权限」自「更多」组迁入：它与系统设置
// 同源（RbacView），属管理员面，manager 不应可达。
const ADMIN_NAV: NavItem[] = [
  { key: 'security', label: '安全与权限', icon: <IconShield />, id: 'security' },
  { key: 'audit', label: '审计', icon: <IconList />, href: '/audit' },
  { key: 'cost', label: '成本', icon: <IconCost />, href: '/cost' },
  { key: 'settings', label: '系统设置', icon: <IconTools />, id: 'settings' },
  { key: 'rbac', label: '角色权限', href: '/rbac' },
]

// 「更多」折叠组 —— manager 及以上可达的目的地（一个都不删）。
const MORE_NAV: NavItem[] = [
  { key: 'agents', label: '智能体工作台', icon: <IconAgents />, id: 'agents' },
  { key: 'memory', label: '记忆管理', icon: <IconMemory />, id: 'memory' },
  { key: 'analytics', label: '数据分析', icon: <IconCost />, id: 'analytics' },
  { key: 'ops', label: '运维监控', icon: <IconTools />, id: 'ops' },
  { key: 'overview', label: '概览', href: '/overview' },
  { key: 'approvals', label: '审批', href: '/approvals' },
  { key: 'workflows', label: '工作流', href: '/workflows' },
  { key: 'tools', label: '工具', href: '/tools' },
  { key: 'marketplace', label: '技能市场', href: '/marketplace' },
  { key: 'clusters', label: '集群', href: '/clusters' },
  { key: 'evals', label: '评测', href: '/evals' },
  { key: 'runs', label: '运行历史', href: '/runs' },
  // INC46 T22 —— 文档 Diff 预览与确认（manager 及以上，与路由守卫同口径）。
  { key: 'artifacts', label: '文档审阅', href: '/artifacts' },
]

type SidebarProps = {
  active: ViewId
  onSelect: (id: ViewId) => void
  onNewChat: () => void
  onNavigate: () => void
}

/** 「更多」组里被 `active` 命中的 id —— 用于默认展开该组（当前页不藏在折叠区里）。 */
const MORE_IDS: ViewId[] = ['agents', 'memory', 'analytics', 'ops']

export function Sidebar({ active, onSelect, onNewChat, onNavigate }: SidebarProps) {
  const pathname = useRouterState({ select: s => s.location.pathname })
  const [moreOpen, setMoreOpen] = useState(MORE_IDS.includes(active))
  // P1 —— 角色门控（判定源 roleGate.ts；未知 / 未登录 ⇒ 最低权限 viewer 兜底）：
  //   manager 及以上 ⇒ 「更多」组；仅 admin ⇒ 管理员区。会话角色变化（登录 / 切换）
  // 经 useSession 的 AUTH_CHANGED / storage 监听即时生效。
  const session = useSession()
  const sessions = useWorkspaceSessions(50, !!session)
  const params = useParams({ strict: false }) as { sessionId?: string }
  const [filter, setFilter] = useState('')
  const searchInput = useRef<HTMLInputElement>(null)
  useEffect(() => {
    const focus = () => requestAnimationFrame(() => searchInput.current?.focus())
    window.addEventListener(SEARCH_HISTORY_EVENT, focus)
    return () => window.removeEventListener(SEARCH_HISTORY_EVENT, focus)
  }, [])
  const rank = roleRank(session?.role)
  const showMore = rank >= ROLE_RANK.manager
  const showAdmin = rank >= ROLE_RANK.admin

  const renderItem = (item: NavItem) => {
    if (item.key === 'new') return (
      <button key={item.key} type="button" className="navlink sidebar-new-chat" data-testid={item.testid} onClick={onNewChat}>
        <IconPlus />{item.label}
      </button>
    )
    if (item.id) {
      const id = item.id
      return (
        <a
          key={item.key}
          className={`navlink${id === active && pathname !== '/files' && !pathname.startsWith('/files/') ? ' active' : ''}`}
          onClick={(e) => {
            e.preventDefault()
            onSelect(id)
          }}
          href={`#${id}`}
          data-testid={item.testid}
          aria-current={id === active && pathname !== '/files' && !pathname.startsWith('/files/') ? 'page' : undefined}
        >
          {item.icon}
          {item.label}
        </a>
      )
    }
    return (
      <Link key={item.key} className="navlink" to={item.href ?? '/'} data-testid={item.testid} onClick={onNavigate}>
        {item.icon}
        {item.label}
      </Link>
    )
  }

  return (
    <aside className="sidebar" id="workspace-sidebar" aria-label="主导航">
      <a className="sidebar-brand" href="/">
        <span className="brand-text">
          <span className="brand-name">ForgeFlow</span>
          <span className="brand-sub">多智能体协作空间</span>
        </span>
      </a>

      {/* 用户区 —— 所有角色可见的高频入口。 */}
      <div className="group">{USER_NAV.filter(item=>item.key === 'new').map(renderItem)}</div>
      <nav className="group" aria-label="工作对象">{USER_NAV.filter(item=>item.key !== 'new').map(renderItem)}</nav>

      <section className="sidebar-conversations" aria-label="历史对话">
        <div className="group-title">你的对话</div>
        <label className="sidebar-search">
          <IconSearch width={14} height={14} />
          <input ref={searchInput} value={filter} onChange={(event) => setFilter(event.target.value)}
            placeholder="搜索对话" aria-label="搜索对话" />
        </label>
        {sessions.isLoading && session && <p className="sidebar-empty">正在加载对话…</p>}
        {sessions.isError && <button className="sidebar-empty sidebar-retry" onClick={() => void sessions.refetch()}>加载失败，点击重试</button>}
        {!sessions.isError && !sessions.isLoading && !(sessions.data?.items?.length) &&
          <p className="sidebar-empty">{session ? '开始第一段对话，记录会保存在这里' : '登录后查看你的对话'}</p>}
        {(sessions.data?.items ?? []).filter((item) => item.title.toLocaleLowerCase().includes(filter.toLocaleLowerCase())).map((item) => (
          <div className="sidebar-conversation-row" key={item.session_id}><Link to={'/chat/' + encodeURIComponent(item.session_id)}
            className={`sidebar-conversation${params.sessionId === item.session_id ? ' selected' : ''}`}
            data-testid="sidebar-conversation" title={item.title || '未命名对话'} onClick={onNavigate}>
            <span>{item.title || '未命名对话'}</span>
          </Link>
          <DeleteConversation sessionId={item.session_id} title={item.title || '未命名对话'} /></div>
        ))}
        {filter && sessions.data?.items?.length && !sessions.data.items.some((item) => item.title.toLocaleLowerCase().includes(filter.toLocaleLowerCase())) ?
          <p className="sidebar-empty">没有找到相关对话</p> : null}
      </section>

      {/* 管理员区 —— 仅 admin（P1 角色门控）。 */}
      {showAdmin && (
        <div className="group">
          <div className="group-title">管理员</div>
          {ADMIN_NAV.map(renderItem)}
        </div>
      )}

      {/* 「更多」折叠组 —— manager 及以上（P1 角色门控；条目一个都不删）。 */}
      {showMore && (
        <div className="group">
          <details className="sidebar-more" open={moreOpen} onToggle={(e) => setMoreOpen(e.currentTarget.open)}>
            <summary className="navlink sidebar-more-summary">
              更多
              <IconChevronDown className={`chev${moreOpen ? ' open' : ''}`} />
            </summary>
            {MORE_NAV.map(renderItem)}
          </details>
        </div>
      )}
    </aside>
  )
}
